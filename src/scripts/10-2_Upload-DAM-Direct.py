"""
10-2 Upload ins DAM (via SFTP + REST)
=====================================
Laedt Webshop-Bilder aus 03-Upload zu Cliplister und registriert sie im DAM.

Architektur (orientiert sich am alten 10-1_Upload-FTP-Folder.py — "ratz fatz"):
  Phase A: SFTP parallel — alle Bilder hochladen mit Connection-Pool
  Phase B: DAM-Insert parallel — alle Bilder per HTTP einfuegen, sammelt unique_ids
  Phase C: Einmaliger Sleep — DAM-Indexierung Zeit geben
  Phase D: Kategorisierung parallel — alle Bilder gleichzeitig kategorisieren

KEIN Duplikat-Check (Wunsch von Gerry — bei 70k Assets in 591672 dauert das
alleine 60-90s). Wenn ein File mit gleichem Namen schon im DAM ist, wird trotzdem
hochgeladen — das DAM hat dann zwei Versionen.

KEIN Resize (Lightroom-Export liefert die richtige Pixelgroesse).
KEIN cleanup_after_upload (Workspace wird nur beim naechsten Download-RAW geleert).

Wichtig:
- Filename-Konvention: {SKU}_{position}.ext  (z.B. 8505786_1.jpg)
  → DAM erkennt SKU + Position automatisch aus dem Dateinamen.
- Keywords kommen aus XMP:Subject / IPTC:Keywords (Lightroom-Export)
  → DAM extrahiert sie automatisch beim Insert.
"""

import sys
print("Initialisiere...", flush=True)

import os
import re
import logging
import time
import json
import subprocess
import threading
import requests
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    get_dam_token, invalidate_dam_token, find_exiftool,
)

# ============================================================
# SETUP
# ============================================================

try:
    config = load_config()
    logger = setup_logging(config['LOG_FILE'], config['LOG_LEVEL'])
except Exception as e:
    print(f"FATAL: Konfiguration konnte nicht geladen werden: {e}")
    sys.exit(1)

paths = get_paths()
upload_folder = paths['upload']  # 03-Upload

# DAM API
DAM_API_BASE     = "https://api-rs.mycliplister.com/v2.2/apis"
DAM_ASSET_INSERT = f"{DAM_API_BASE}/asset/insert"

# Insert-Kategorie wie im alten 10-1_Upload-FTP-Folder.py.
DEFAULT_CATEGORY_ID = 591672

# Bildkategorien im DAM (Mood, Clipping, Detail etc.).
KEYWORD_TO_CATEGORY_ID = {
    'Clipping':      408735, 'Freisteller':  408735,
    'Neutral':       408720, 'Produktbild':  408720,
    'Mood':          408719, 'Ambiente':     408719,
    'Technical':     408722, 'Technisch':    408722,
    'Dimensions':    408736,
    'Detail':        408721,
    'Shade':         408753, 'Schirm':       408753,
    'Material':      408752,
    'Switch':        408751, 'Schalter':     408751,
    'Base_Stand':    408750,
    'Cable':         408749, 'Kabel':        408749,
    'Split':         408747,
    'Remote':        408756, 'Fernbedienung': 408756,
    'Accessories':   408755, 'Zubehoer':     408755,
    'Group':         408762, 'Gruppe':       408762,
    'UGC':           408760,
    'Graphics':      408723, 'Grafik':       408723,
}

CONCURRENCY = max(4, min(int(config.get('ASYNC_TASK_CONCURRENCY', 8)), 8))
INDEXING_WAIT_SEC = 3   # zwischen Insert-Phase und Kategorie-Phase
CATEGORY_RETRY = 2      # max. Retries pro Kategorie wenn 400 "not found"

IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')


# ============================================================
# HELPERS
# ============================================================

def get_dam_headers():
    """Auth-Header fuer DAM API."""
    token = get_dam_token(config)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }


def get_image_keywords(filepath):
    """Liest Keywords aus Bilddatei (Lightroom XMP:Subject/IPTC:Keywords)."""
    keywords = []
    fname = os.path.basename(filepath)
    try:
        exiftool = find_exiftool()
        if not exiftool:
            return keywords
        result = subprocess.run(
            [exiftool, '-j',
             '-XMP:Subject', '-IPTC:Keywords', '-Keywords',
             '-XMP-lr:HierarchicalSubject', '-XMP-dc:Subject',
             filepath],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                if isinstance(data, list) and data:
                    meta = data[0]
                    for field in ('Subject', 'Keywords', 'HierarchicalSubject',
                                  'XMP-lr:HierarchicalSubject', 'XMP-dc:Subject'):
                        val = meta.get(field)
                        if not val:
                            continue
                        vals = val if isinstance(val, list) else re.split(r'[,;]', str(val))
                        for kw in vals:
                            kw = str(kw).strip()
                            if '|' in kw:
                                kw = kw.split('|')[-1]
                            if kw and kw not in keywords:
                                keywords.append(kw)
            except json.JSONDecodeError:
                pass
    except Exception as e:
        logger.debug(f"  exiftool keyword-read fehlgeschlagen ({fname}): {e}")
    return keywords


def _parse_sku_and_position(filename):
    """Filename `{SKU}_{position}.{ext}` → (sku, position) — fuer Logging."""
    name_no_ext = os.path.splitext(filename)[0]
    m = re.match(r'^(\d{7,8})_(\d{1,2})(?:[_#].*)?$', name_no_ext)
    if m:
        return m.group(1), int(m.group(2))
    m2 = re.match(r'^(\d{7,8})', name_no_ext)
    if m2:
        return m2.group(1), None
    return None, None


def _collect_image_files(folder):
    image_files = []
    for root, _, filenames in os.walk(folder):
        for f in filenames:
            if f.startswith('.'):
                continue
            if f.lower().endswith(IMG_EXTENSIONS):
                image_files.append(os.path.join(root, f))
    return image_files


# ============================================================
# SFTP CONNECTION POOL
# ============================================================

_pool_lock = threading.Lock()
_pool = []
_POOL_MAX = 4


def _create_sftp():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        config['SFTP_HOST'],
        port=config['SFTP_PORT'],
        username=config['SFTP_USERNAME'],
        password=config['SFTP_PASSWORD'],
        timeout=30
    )
    return ssh, ssh.open_sftp()


def _get_sftp():
    with _pool_lock:
        if _pool:
            return _pool.pop()
    return _create_sftp()


def _release_sftp(conn):
    with _pool_lock:
        if len(_pool) < _POOL_MAX:
            _pool.append(conn)
            return
    try:
        ssh, sftp = conn
        sftp.close()
        ssh.close()
    except Exception:
        pass


def _close_pool():
    with _pool_lock:
        for ssh, sftp in _pool:
            try:
                sftp.close()
                ssh.close()
            except Exception:
                pass
        _pool.clear()


# ============================================================
# PHASE A: SFTP UPLOAD
# ============================================================

def _sftp_upload(filepath):
    filename = os.path.basename(filepath)
    conn = None
    try:
        conn = _get_sftp()
        ssh, sftp = conn
        remote_path = os.path.join(config['SFTP_REMOTE_DIR'], filename).replace('\\', '/')
        sftp.put(filepath, remote_path)
        _release_sftp(conn)
        logger.info(f"  ↑ SFTP OK: {filename}")
        return True
    except Exception as e:
        logger.error(f"  SFTP-Fehler {filename}: {e}")
        if conn:
            try:
                ssh, sftp = conn
                sftp.close()
                ssh.close()
            except Exception:
                pass
        return False


def phase_a_sftp_upload(image_files):
    """Phase A: SFTP-Upload aller Files parallel mit Pool."""
    logger.info(f"Phase A: SFTP-Upload ({len(image_files)} Files, concurrency={CONCURRENCY})...")
    t0 = time.time()
    uploaded = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(_sftp_upload, fp): fp for fp in image_files}
        for future in as_completed(futures):
            fp = futures[future]
            try:
                if future.result():
                    uploaded.append(fp)
            except Exception as e:
                logger.warning(f"SFTP-Task-Fehler ({os.path.basename(fp)}): {e}")
    dt = time.time() - t0
    logger.info(f"Phase A fertig in {dt:.1f}s: {len(uploaded)}/{len(image_files)} hochgeladen")
    return uploaded


# ============================================================
# PHASE B: DAM-INSERT
# ============================================================

def _dam_insert(filepath):
    """DAM-Insert. Returns (filepath, unique_id, keywords) oder None."""
    filename = os.path.basename(filepath)
    try:
        sku, position = _parse_sku_and_position(filename)
        keywords = get_image_keywords(filepath)
        if keywords:
            logger.info(f"  📌 Keywords ({filename}): {keywords}")
        else:
            logger.warning(f"  ⚪ KEINE Keywords im Bild gefunden ({filename})")
        if sku is not None:
            pos_log = f", Pos {position}" if position is not None else ""
            logger.info(f"  Filename codiert: SKU {sku}{pos_log}")

        sftp_url = (
            f"https://clup01.cliplister.com/files/"
            f"{config['SFTP_USERNAME']}{config['SFTP_REMOTE_DIR']}/{filename}"
        ).replace('\\', '/')

        payload = {
            "source": sftp_url,
            "fileName": filename,
            "categories": [{"id": DEFAULT_CATEGORY_ID}],
        }

        headers = get_dam_headers()
        timeout = config.get('API_REQUEST_TIMEOUT', 120)
        resp = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 401:
            invalidate_dam_token()
            headers = get_dam_headers()
            resp = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=timeout)

        if resp.status_code in (200, 201):
            unique_id = ''
            try:
                result = resp.json() if resp.text else {}
                unique_id = result.get('uniqueId') or result.get('unique_id') or ''
            except Exception:
                pass
            logger.info(f"  ✓ DAM-Insert OK: {filename}")
            return (filepath, unique_id, keywords)
        else:
            logger.warning(f"  DAM-Insert FEHLER {filename}: HTTP {resp.status_code} - {resp.text[:200]}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"  DAM-Insert TIMEOUT: {filename}")
        return None
    except Exception as e:
        logger.error(f"  DAM-Insert FEHLER {filename}: {e}")
        return None


def phase_b_dam_insert(uploaded_files):
    """Phase B: DAM-Insert aller hochgeladenen Files parallel."""
    if not uploaded_files:
        return []
    logger.info(f"Phase B: DAM-Insert ({len(uploaded_files)} Files, concurrency={CONCURRENCY})...")
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(_dam_insert, fp): fp for fp in uploaded_files}
        for future in as_completed(futures):
            try:
                r = future.result()
                if r:
                    results.append(r)
            except Exception as e:
                logger.warning(f"DAM-Insert Task-Fehler: {e}")
    dt = time.time() - t0
    logger.info(f"Phase B fertig in {dt:.1f}s: {len(results)}/{len(uploaded_files)} eingefuegt")
    return results


# ============================================================
# PHASE D: KATEGORISIERUNG
# ============================================================

def _assign_categories(unique_id, keywords, filename):
    """Setze Spezial-Kategorien per asset/category/add."""
    if not unique_id or not keywords:
        return

    cat_ids = []
    for kw in keywords:
        cat_id = KEYWORD_TO_CATEGORY_ID.get(kw)
        if cat_id and cat_id not in cat_ids:
            cat_ids.append(cat_id)

    if not cat_ids:
        return

    headers = get_dam_headers()
    timeout = config.get('API_REQUEST_TIMEOUT', 120)
    for cat_id in cat_ids:
        for attempt in range(CATEGORY_RETRY):
            try:
                url = f"{DAM_API_BASE}/asset/category/add?unique_id={unique_id}&category_id={cat_id}"
                resp = requests.put(url, headers=headers, json={}, timeout=timeout)
                if resp.status_code == 401:
                    invalidate_dam_token()
                    headers = get_dam_headers()
                    resp = requests.put(url, headers=headers, json={}, timeout=timeout)
                if resp.status_code in (200, 204):
                    logger.info(f"  + Kategorie {cat_id} zugewiesen ({filename})")
                    break
                if resp.status_code == 400 and 'cannot be found' in resp.text.lower():
                    if attempt < CATEGORY_RETRY - 1:
                        time.sleep(2)
                        continue
                logger.warning(f"  Kategorie {cat_id} fehlgeschlagen ({filename}): HTTP {resp.status_code}")
                break
            except Exception as e:
                logger.warning(f"  Kategorie {cat_id} Exception ({filename}): {e}")
                break


def phase_d_categorize(insert_results):
    """Phase D: Kategorisierung aller Bilder parallel."""
    if not insert_results:
        return
    to_process = []
    for fp, uid, kws in insert_results:
        if not uid or not kws:
            continue
        if any(KEYWORD_TO_CATEGORY_ID.get(k) for k in kws):
            to_process.append((fp, uid, kws))

    if not to_process:
        logger.info("Phase D: Keine Kategorien zu setzen")
        return

    logger.info(f"Phase D: Kategorisierung ({len(to_process)} Files, concurrency={CONCURRENCY})...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [
            executor.submit(_assign_categories, uid, kws, os.path.basename(fp))
            for fp, uid, kws in to_process
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                logger.warning(f"Kategorie Task-Fehler: {e}")
    dt = time.time() - t0
    logger.info(f"Phase D fertig in {dt:.1f}s")


# ============================================================
# UPLOAD ALL — Orchestrierung der 4 Phasen
# ============================================================

def upload_all_images():
    """Upload aller Bilder aus 03-Upload ins DAM in 4 Phasen."""
    if not os.path.exists(upload_folder):
        logger.warning(f"Upload-Ordner nicht gefunden: {upload_folder}")
        return 0

    image_files = _collect_image_files(upload_folder)
    if not image_files:
        logger.info("Keine Bilder zum Hochladen gefunden")
        return 0

    total_t0 = time.time()

    # Phase A: SFTP
    uploaded = phase_a_sftp_upload(image_files)
    if not uploaded:
        logger.error("Phase A: keine Bilder erfolgreich hochgeladen")
        return 0

    # Phase B: DAM-Insert
    insert_results = phase_b_dam_insert(uploaded)
    if not insert_results:
        logger.error("Phase B: keine Inserts erfolgreich")
        return 0

    # Phase C: Sleep, damit DAM-Indexierung aufholt
    logger.info(f"Phase C: warte {INDEXING_WAIT_SEC}s auf DAM-Indexierung...")
    time.sleep(INDEXING_WAIT_SEC)

    # Phase D: Kategorisierung parallel
    phase_d_categorize(insert_results)

    total_dt = time.time() - total_t0
    inserted = len(insert_results)
    logger.info(f"Upload komplett: {inserted}/{len(image_files)} in {total_dt:.1f}s")
    return inserted


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("=" * 70)
        logger.info("Upload ins DAM gestartet")
        logger.info("=" * 70)

        ticket_key = os.environ.get("POSTPRO_INPUT", "").strip()
        if not ticket_key:
            ticket_file = os.path.join(upload_folder, ".ticket_key")
            if os.path.exists(ticket_file):
                with open(ticket_file, 'r') as f:
                    ticket_key = f.read().strip()
        if ticket_key and '-' not in ticket_key:
            ticket_key = f"CREAMEDIA-{ticket_key}"
        if ticket_key:
            logger.info(f"Ticket: {ticket_key}")

        logger.info(f"Default-Kategorie: ID {DEFAULT_CATEGORY_ID} (DAM extrahiert Keywords + Position automatisch)")

        if not os.path.exists(upload_folder):
            logger.error(f"Upload-Ordner existiert nicht: {upload_folder}")
            sys.exit(1)

        image_files = _collect_image_files(upload_folder)
        if not image_files:
            logger.error("Keine Bilder im Upload-Ordner gefunden!")
            logger.error(f"Ordner: {upload_folder}")
            sys.exit(1)

        folder_counts = {}
        for fp in image_files:
            folder = os.path.basename(os.path.dirname(fp))
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
        folder_summary = ", ".join(f"{c} in {f}" for f, c in folder_counts.items())
        logger.info(f"{len(image_files)} Bilder gefunden ({folder_summary})")

        uploaded = upload_all_images()

        _close_pool()

        logger.info("=" * 70)
        if uploaded == len(image_files):
            logger.info(f"✓ ERFOLG: {uploaded} Bilder hochgeladen")
        else:
            failed = len(image_files) - uploaded
            logger.warning(f"⚠ TEILWEISE: {uploaded}/{len(image_files)} erfolgreich, {failed} fehlgeschlagen")
        if ticket_key:
            logger.info(f"Ticket: {ticket_key}")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.info("Upload durch Benutzer unterbrochen")
        _close_pool()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        _close_pool()
        sys.exit(1)
