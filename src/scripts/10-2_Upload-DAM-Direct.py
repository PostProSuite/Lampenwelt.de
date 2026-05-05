"""
10-2 Upload ins DAM (via SFTP + REST)
=====================================
Laedt Webshop-Bilder aus 03-Upload zu Cliplister und registriert sie im DAM.

Ablauf:
  1. Duplikat-Check parallel    (alle Asset-Listen-Pages gleichzeitig)
  2. Bei vorhandenen Duplikaten: Dialog "alle ueberschreiben / ueberspringen / abbrechen"
     Bei Ueberschreiben: alte Assets per asset/delete entfernen, dann neu uploaden
  3. SFTP-Upload via Connection-Pool (eine SSH-Connection wird pro Worker recycled)
  4. DAM-Insert MINIMAL          (source/fileName/categories) → Kategorie 591672
  5. Sleep 1s + asset/category/add fuer 408xxx-Spezialkategorie (mit Retry)

NICHT mehr enthalten (gegenueber alter Version):
- KEIN Resize (Lightroom-Export macht das vorher mit eingestellter Pixelgroesse)
- KEIN cleanup_after_upload (Workspace wird nur beim naechsten Download-RAW geleert,
  via cleanupBeforeDownloadRaw in server.js)

Wichtig zu wissen:
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
DAM_ASSET_LIST   = f"{DAM_API_BASE}/asset/list"
DAM_ASSET_DELETE = f"{DAM_API_BASE}/asset/delete"

# Insert-Kategorie wie im alten 10-1_Upload-FTP-Folder.py.
# Nur diese Kategorie triggert NICHT den problematischen DAM-Workflow-Error.
DEFAULT_CATEGORY_ID = 591672

# Bildkategorien im DAM (Mood, Clipping, Detail etc.).
# NACH dem Insert weisen wir dem Asset zusaetzlich die passende Kategorie zu —
# anhand der Bild-Keywords (XMP:Subject) — via asset/category/add.
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
    """Sammelt rekursiv alle Bilder unterhalb von folder."""
    image_files = []
    for root, _, filenames in os.walk(folder):
        for f in filenames:
            if f.startswith('.'):
                continue
            if f.lower().endswith(IMG_EXTENSIONS):
                image_files.append(os.path.join(root, f))
    return image_files


# ============================================================
# DAM DUPLIKAT-CHECK (parallel)
# ============================================================

def _fetch_asset_page(category_id, offset, timeout):
    """Eine Page der Asset-Liste holen — fuer parallel Pagination."""
    headers = get_dam_headers()
    params = {
        "category_id": category_id,
        "limit": 250,
        "offset": offset,
        "include_meta": "true",
    }
    resp = requests.get(DAM_ASSET_LIST, headers=headers, params=params, timeout=timeout)
    if resp.status_code == 401:
        invalidate_dam_token()
        headers = get_dam_headers()
        resp = requests.get(DAM_ASSET_LIST, headers=headers, params=params, timeout=timeout)
    if resp.status_code != 200:
        logger.warning(f"DAM-Listenabfrage Fehler offset={offset}: HTTP {resp.status_code}")
        return []
    data = resp.json()
    return data.get('assets', data) if isinstance(data, dict) else data


def get_existing_dam_assets(category_id=DEFAULT_CATEGORY_ID):
    """
    Liefert dict {filename: unique_id} aller Assets in category_id.
    Parallel-Pagination um Duplikat-Check zu beschleunigen (113 Pages → ~12s
    statt ~60s sequenziell).
    """
    timeout = config.get('API_REQUEST_TIMEOUT', 120)

    # Erste Page holen um Total zu erfahren (heuristik via Page-Size)
    page0 = _fetch_asset_page(category_id, 0, timeout)
    if not page0:
        logger.info("DAM: Keine Assets in Kategorie oder Listenabfrage fehlgeschlagen")
        return {}

    name_to_id = {}
    for asset in page0:
        title = asset.get('title') or asset.get('fileName') or ''
        uid = asset.get('uniqueId') or asset.get('unique_id') or ''
        if title and uid:
            name_to_id[title] = uid
            name_to_id[os.path.splitext(title)[0]] = uid

    if len(page0) < 250:
        logger.info(f"DAM: {len(name_to_id)//2} bestehende Assets geladen (Duplikat-Check)")
        return name_to_id

    # Mehr Pages parallel holen
    # Wir holen erstmal in Bloecken weil wir kein Total kennen
    BLOCK = 8  # 8 Pages = 2000 Assets pro Block
    offset = 250
    while True:
        offsets = [offset + i * 250 for i in range(BLOCK)]
        with ThreadPoolExecutor(max_workers=BLOCK) as executor:
            futures = {executor.submit(_fetch_asset_page, category_id, off, timeout): off for off in offsets}
            results = []
            for fut in as_completed(futures):
                results.append((futures[fut], fut.result()))

        results.sort(key=lambda x: x[0])
        any_full_page = False
        for off, page in results:
            if not page:
                continue
            for asset in page:
                title = asset.get('title') or asset.get('fileName') or ''
                uid = asset.get('uniqueId') or asset.get('unique_id') or ''
                if title and uid:
                    name_to_id[title] = uid
                    name_to_id[os.path.splitext(title)[0]] = uid
            if len(page) >= 250:
                any_full_page = True

        if not any_full_page:
            break
        offset += BLOCK * 250

    logger.info(f"DAM: {len(name_to_id)//2} bestehende Assets geladen (Duplikat-Check, parallel)")
    return name_to_id


def delete_dam_asset(unique_id, filename):
    """Loescht ein DAM-Asset vor dem Re-Upload (Ueberschreib-Logik)."""
    timeout = config.get('API_REQUEST_TIMEOUT', 120)
    headers = get_dam_headers()
    try:
        url = f"{DAM_ASSET_DELETE}?unique_id={unique_id}"
        resp = requests.put(url, headers=headers, json={}, timeout=timeout)
        if resp.status_code == 401:
            invalidate_dam_token()
            headers = get_dam_headers()
            resp = requests.put(url, headers=headers, json={}, timeout=timeout)
        if resp.status_code in (200, 204):
            logger.info(f"  🗑  Altes Asset geloescht: {filename} (uid={unique_id})")
            return True
        logger.warning(f"  Loeschen fehlgeschlagen ({filename}): HTTP {resp.status_code} - {resp.text[:150]}")
    except Exception as e:
        logger.warning(f"  Loeschen Exception ({filename}): {e}")
    return False


# ============================================================
# UEBERSCHREIB-DIALOG
# ============================================================

def ask_overwrite_dialog(duplicate_filenames):
    """
    Zeigt einen macOS-Dialog mit Liste der Duplikate.
    Returns: 'overwrite' | 'skip' | 'cancel'
    """
    # Liste auf max 15 Files kuerzen damit Dialog nicht endlos wird
    display_list = duplicate_filenames[:15]
    extra = len(duplicate_filenames) - len(display_list)
    list_text = "\\n  • ".join(display_list)
    if extra > 0:
        list_text += f"\\n  ... und {extra} weitere"

    msg = (
        f"{len(duplicate_filenames)} Bilder existieren bereits im DAM:\\n\\n"
        f"  • {list_text}\\n\\n"
        "Wie soll vorgegangen werden?"
    )

    script = (
        f'set userChoice to button returned of (display dialog "{msg}" '
        f'with title "Duplikate im DAM gefunden" '
        f'buttons {{"Abbrechen", "Ueberspringen", "Ueberschreiben"}} '
        f'default button "Ueberspringen" '
        f'cancel button "Abbrechen" with icon caution)'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=300
        )
    except Exception as e:
        logger.warning(f"Dialog-Fehler: {e} — fahre mit 'skip' fort")
        return 'skip'

    if result.returncode != 0:
        return 'cancel'
    answer = result.stdout.strip().lower()
    if 'ueberschreiben' in answer or 'überschreiben' in answer:
        return 'overwrite'
    if 'ueberspringen' in answer or 'überspringen' in answer:
        return 'skip'
    return 'cancel'


# ============================================================
# SFTP CONNECTION POOL
# ============================================================
# Pool wiederverwendet SSH-Verbindungen quer ueber Worker-Threads.
# Statt pro Bild SSH-Handshake (~3-5s) → ein Handshake pro Worker-Slot.

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
    """Verbindung in den Pool zurueck oder schliessen wenn Pool voll."""
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


def upload_to_sftp(local_path, sftp_filename):
    """Datei zum Cliplister-SFTP hochladen via Connection-Pool."""
    conn = None
    try:
        conn = _get_sftp()
        ssh, sftp = conn
        remote_path = os.path.join(config['SFTP_REMOTE_DIR'], sftp_filename).replace('\\', '/')
        sftp.put(local_path, remote_path)
        _release_sftp(conn)
        return True
    except Exception as e:
        logger.error(f"SFTP-Upload-Fehler {sftp_filename}: {e}")
        # Verbindung war kaputt — nicht zurueck in Pool
        if conn:
            try:
                ssh, sftp = conn
                sftp.close()
                ssh.close()
            except Exception:
                pass
        return False


# ============================================================
# POST-INSERT KATEGORISIERUNG
# ============================================================

def _assign_categories_from_keywords(unique_id, keywords, filename, max_retries=4):
    """
    Weist dem DAM-Asset zusaetzliche Kategorien zu (Mood/Clipping/Detail/...).
    via asset/category/add — wie altes 02-1_filenaming.py Phase 2.

    RETRY-LOGIK: Direkt nach Insert ist das Asset im DAM oft noch nicht
    indexiert → `category/add` liefert dann HTTP 400 "asset cannot be found".
    Wir warten initial 1s und probieren dann ggf. mit exponential backoff.
    """
    if not unique_id or not keywords:
        return

    cat_ids = []
    for kw in keywords:
        cat_id = KEYWORD_TO_CATEGORY_ID.get(kw)
        if cat_id and cat_id not in cat_ids:
            cat_ids.append(cat_id)

    if not cat_ids:
        logger.info(f"  ⚠ Keine Kategorie-Mapping fuer Keywords {keywords} ({filename})")
        return

    # Initial-Sleep damit DAM-Indexierung Zeit hat
    time.sleep(1)

    headers = get_dam_headers()
    timeout = config.get('API_REQUEST_TIMEOUT', 120)
    for cat_id in cat_ids:
        for attempt in range(max_retries):
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
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                        logger.info(f"  ⏳ Asset noch nicht indexiert ({filename}), warte {wait}s...")
                        time.sleep(wait)
                        continue
                logger.warning(f"  Kategorie {cat_id} fehlgeschlagen ({filename}): HTTP {resp.status_code} - {resp.text[:150]}")
                break
            except Exception as e:
                logger.warning(f"  Kategorie {cat_id} Exception ({filename}): {e}")
                break


# ============================================================
# UPLOAD EIN BILD INS DAM
# ============================================================

def upload_single_image(filepath):
    """
    Upload (so wie altes 10-1 Skript): MINIMAL Insert.
    DAM extrahiert Keywords + SKU/Position automatisch aus dem Bild + Filename.
    KEIN Resize — Lightroom-Export liefert die richtige Pixelgroesse.
    """
    filename = os.path.basename(filepath)
    try:
        # Diagnostisches Logging: Keywords + SKU/Position
        sku, position = _parse_sku_and_position(filename)
        keywords = get_image_keywords(filepath)
        if keywords:
            logger.info(f"  📌 Keywords ({filename}): {keywords}")
        else:
            logger.warning(f"  ⚪ KEINE Keywords im Bild gefunden ({filename}) — DAM kann nichts extrahieren!")
        if sku is not None:
            pos_log = f", Pos {position}" if position is not None else ""
            logger.info(f"  Filename codiert: SKU {sku}{pos_log}")

        # SFTP-Upload via Connection-Pool
        if not upload_to_sftp(filepath, filename):
            logger.warning(f"  SFTP-Upload fehlgeschlagen: {filename}")
            return False

        # DAM-Insert (minimal, exakt wie altes Skript)
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
        response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=timeout)

        if response.status_code == 401:
            invalidate_dam_token()
            headers = get_dam_headers()
            response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=timeout)

        if response.status_code in (200, 201):
            unique_id = ''
            try:
                result = response.json() if response.text else {}
                unique_id = result.get('uniqueId') or result.get('unique_id') or ''
            except Exception:
                pass
            logger.info(f"  ✓ Upload OK: {filename}")

            if unique_id and keywords:
                _assign_categories_from_keywords(unique_id, keywords, filename)
            elif not unique_id:
                logger.warning(f"  Keine unique_id in Insert-Response ({filename}) — Kategorie nicht zuweisbar")
            return True
        else:
            logger.warning(f"  FEHLER: {filename}: HTTP {response.status_code} - {response.text[:200]}")
            return False

    except requests.exceptions.Timeout:
        logger.error(f"  TIMEOUT: {filename}")
        return False
    except Exception as e:
        logger.error(f"  FEHLER: {filename}: {e}")
        return False


# ============================================================
# UPLOAD ALL
# ============================================================

def upload_all_images():
    """Upload aller Bilder aus 03-Upload ins DAM."""
    if not os.path.exists(upload_folder):
        logger.warning(f"Upload-Ordner nicht gefunden: {upload_folder}")
        return 0, 0

    image_files = _collect_image_files(upload_folder)
    if not image_files:
        logger.info("Keine Bilder zum Hochladen gefunden")
        return 0, 0

    # Duplikat-Check (parallel) — liefert {filename: unique_id}
    existing = get_existing_dam_assets(DEFAULT_CATEGORY_ID)

    # Welche unserer Files sind Duplikate?
    duplicates = []   # [(filepath, unique_id), ...]
    fresh = []        # [filepath, ...]
    for fp in image_files:
        base = os.path.basename(fp)
        base_no_ext = os.path.splitext(base)[0]
        uid = existing.get(base) or existing.get(base_no_ext)
        if uid:
            duplicates.append((fp, uid))
        else:
            fresh.append(fp)

    # Wenn Duplikate vorhanden: User fragen
    overwrite_mode = False
    if duplicates:
        dup_names = [os.path.basename(fp) for fp, _ in duplicates]
        choice = ask_overwrite_dialog(dup_names)
        if choice == 'cancel':
            logger.info("Upload durch Benutzer abgebrochen (Dialog)")
            return 0, 0
        if choice == 'overwrite':
            overwrite_mode = True
            logger.info(f"Benutzer-Wahl: Alle {len(duplicates)} Duplikate ueberschreiben")
            # Alte Assets loeschen — danach koennen wir sie normal hochladen
            for fp, uid in duplicates:
                delete_dam_asset(uid, os.path.basename(fp))
            # In to_upload Liste aufnehmen
            fresh.extend(fp for fp, _ in duplicates)
        else:
            logger.info(f"Benutzer-Wahl: {len(duplicates)} Duplikate ueberspringen")

    if not fresh:
        logger.info("Keine neuen Bilder hochzuladen.")
        return 0, len(duplicates) if not overwrite_mode else 0

    logger.info(f"{len(fresh)} Bilder werden hochgeladen (concurrency={CONCURRENCY})...")

    uploaded = 0
    total = len(fresh)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(upload_single_image, fp): fp for fp in fresh}
        for future in as_completed(futures):
            try:
                if future.result():
                    uploaded += 1
            except Exception as e:
                logger.warning(f"Upload-Task-Fehler: {e}")

            done_count = sum(1 for f in futures if f.done())
            pct = round(done_count / total * 100)
            print(f"Upload: {done_count}/{total} ({pct}%) — {uploaded} erfolgreich", flush=True)

    skipped = len(duplicates) if not overwrite_mode else 0
    logger.info(f"Upload abgeschlossen: {uploaded}/{total} hochgeladen, {skipped} uebersprungen")
    return uploaded, skipped


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("=" * 70)
        logger.info("Upload ins DAM gestartet")
        logger.info("=" * 70)

        # Ticket-Key (optional, fuer Logging)
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

        # Übersicht
        folder_counts = {}
        for fp in image_files:
            folder = os.path.basename(os.path.dirname(fp))
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
        folder_summary = ", ".join(f"{c} in {f}" for f, c in folder_counts.items())
        logger.info(f"{len(image_files)} Bilder gefunden ({folder_summary})")

        # Upload
        logger.info("Bilder ins DAM hochladen...")
        uploaded, skipped = upload_all_images()

        # SFTP-Pool aufraeumen
        _close_pool()

        # KEIN cleanup mehr — der Workspace wird beim naechsten Download-RAW
        # via cleanupBeforeDownloadRaw() in server.js bereinigt.

        logger.info("=" * 70)
        if uploaded == len(image_files):
            logger.info(f"✓ ERFOLG: {uploaded} Bilder hochgeladen")
        elif uploaded > 0 and skipped > 0 and uploaded + skipped == len(image_files):
            logger.info(f"✓ FERTIG: {uploaded} hochgeladen, {skipped} uebersprungen (Duplikate)")
        else:
            failed = len(image_files) - uploaded - skipped
            logger.warning(f"⚠ TEILWEISE: {uploaded} hochgeladen, {skipped} uebersprungen, {failed} fehlgeschlagen")
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
