"""
10-2 Upload ins DAM (via SFTP + REST)
=====================================
Laedt Webshop-Bilder aus 03-Upload zu Cliplister und registriert sie im DAM.

Verhalten matcht das alte funktionierende Skript 10-1_Upload-FTP-Folder.py:
  1. Resize auf max 1800x1800   (Metadaten – XMP/IPTC/EXIF – bleiben erhalten!)
  2. Duplikat-Check             (bereits im DAM vorhandene Dateinamen ueberspringen)
  3. SFTP-Upload nach clup01.cliplister.com
  4. DAM-Insert MINIMAL         (source/fileName/categories) → Kategorie 591672
  5. Cleanup 03-Upload

Wichtig zu wissen:
- Filename-Konvention: {SKU}_{position}.ext  (z.B. 8505786_1.jpg)
  → DAM erkennt SKU + Position automatisch aus dem Dateinamen.
- Keywords kommen aus XMP:Subject / IPTC:Keywords (Lightroom-Export)
  → DAM extrahiert sie automatisch beim Insert.
- Beides funktioniert NUR wenn die Metadaten beim Resize NICHT verloren gehen.
- KEINE manuellen Post-Insert Updates fuer products[]/tags[] mehr — die haben
  den DAM-Workflow durchbrochen, der die Auto-Extraktion macht.
"""

import sys
print("Initialisiere...", flush=True)

import os
import re
import shutil
import logging
import time
import json
import subprocess
import requests
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

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

# Insert-Kategorie wie im alten funktionierenden Skript 10-1_Upload-FTP-Folder.py.
# Nur diese Kategorie triggert NICHT den problematischen DAM-Workflow-Error.
# DAM extrahiert auf dieser Kategorie automatisch:
#   - Keywords aus XMP:Subject / IPTC:Keywords
#   - SKU + Position aus Dateinamen ({SKU}_{position}.ext)
DEFAULT_CATEGORY_ID = 591672

# Bildkategorien im DAM (Mood, Clipping, Detail etc.).
# NACH dem Insert weisen wir dem Asset zusaetzlich die passende Kategorie zu —
# anhand der Bild-Keywords (XMP:Subject) — via asset/category/add.
# Genau das gleiche Verhalten wie altes 02-1_filenaming.py Phase 2.
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
    """Liest Keywords aus Bilddatei – nur fuer Logging/Diagnose."""
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


def _assign_categories_from_keywords(unique_id, keywords, filename):
    """
    Weist dem DAM-Asset zusaetzliche Kategorien zu, abhaengig von den Bild-Keywords.

    Ablauf wie altes 02-1_filenaming.py Phase 2 — `asset/category/add` Endpoint.
    Funktioniert weil Insert in 591672 (kein Workflow-Error) UND wir danach
    spezifische Kategorien (408xxx) per category/add ergaenzen — kein update auf
    products[]/tags[] (das hatte den Workflow gebrochen).

    Mehrere Keywords koennen mehrere Kategorien zuweisen
    (z.B. ['Detail', 'Shade'] → 408721 + 408753).
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

    headers = get_dam_headers()
    timeout = config.get('API_REQUEST_TIMEOUT', 120)
    for cat_id in cat_ids:
        try:
            url = f"{DAM_API_BASE}/asset/category/add?unique_id={unique_id}&category_id={cat_id}"
            resp = requests.put(url, headers=headers, json={}, timeout=timeout)
            if resp.status_code == 401:
                invalidate_dam_token()
                headers = get_dam_headers()
                resp = requests.put(url, headers=headers, json={}, timeout=timeout)
            if resp.status_code in (200, 204):
                logger.info(f"  + Kategorie {cat_id} zugewiesen ({filename})")
            else:
                logger.warning(f"  Kategorie {cat_id} fehlgeschlagen ({filename}): HTTP {resp.status_code} - {resp.text[:150]}")
        except Exception as e:
            logger.warning(f"  Kategorie {cat_id} Exception ({filename}): {e}")


# ============================================================
# IMAGE RESIZE (Metadaten-erhaltend!)
# ============================================================

def resize_image_to_1800(image_path):
    """
    Resize auf max 1800x1800 (Aspect Ratio bleibt) MIT Metadaten-Erhalt.

    Wichtig: PIL `Image.save()` strippt XMP/IPTC/EXIF wenn nicht explizit
    durchgereicht. Wir sichern die Metadaten doppelt:
      1. PIL kriegt exif/xmp/icc_profile aus img.info beim Speichern.
      2. exiftool kopiert anschliessend ALLE Metadaten vom Original
         in die resized Datei – als Sicherheitsnetz, falls PIL XMP/IPTC
         nicht sauber durchgereicht hat.
    """
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            if w <= 1800 and h <= 1800:
                # Kein Resize noetig — Originaldatei bleibt unangetastet, Keywords drin
                return True

            xmp_blob  = img.info.get('xmp')
            exif_blob = img.info.get('exif')
            icc_blob  = img.info.get('icc_profile')

            img.thumbnail((1800, 1800), Image.Resampling.LANCZOS)

            save_kwargs = {'quality': 95, 'optimize': True}
            if exif_blob:
                save_kwargs['exif'] = exif_blob
            if xmp_blob:
                save_kwargs['xmp'] = xmp_blob
            if icc_blob:
                save_kwargs['icc_profile'] = icc_blob

            tmp_path = image_path + '.resized.tmp'
            img.save(tmp_path, **save_kwargs)
            new_w, new_h = img.size

        # Belt-and-suspenders: exiftool kopiert ALL metadata Original → resized
        # (PIL ist bei XMP/IPTC inkonsistent, exiftool ist die Referenz)
        try:
            exiftool = find_exiftool()
            if exiftool:
                subprocess.run(
                    [exiftool, '-overwrite_original',
                     '-TagsFromFile', image_path,
                     '-XMP:all', '-IPTC:all', '-EXIF:all',
                     tmp_path],
                    capture_output=True, timeout=15
                )
        except Exception as e:
            logger.debug(f"  exiftool metadata-copy fehlgeschlagen: {e}")

        os.replace(tmp_path, image_path)
        logger.info(f"  Resize: {os.path.basename(image_path)} ({w}x{h} → {new_w}x{new_h}) — Metadaten erhalten")
        return True
    except Exception as e:
        logger.error(f"Image-Resize-Fehler {image_path}: {e}")
        # Tmp aufraeumen falls vorhanden
        try:
            tmp = image_path + '.resized.tmp'
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return False


# ============================================================
# SFTP UPLOAD
# ============================================================

def upload_to_sftp(local_path, sftp_filename):
    """Datei zum Cliplister-SFTP hochladen."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            config['SFTP_HOST'],
            port=config['SFTP_PORT'],
            username=config['SFTP_USERNAME'],
            password=config['SFTP_PASSWORD'],
            timeout=30
        )
        sftp = ssh.open_sftp()
        remote_path = os.path.join(config['SFTP_REMOTE_DIR'], sftp_filename).replace('\\', '/')
        sftp.put(local_path, remote_path)
        sftp.close()
        ssh.close()
        return True
    except Exception as e:
        logger.error(f"SFTP-Upload-Fehler {sftp_filename}: {e}")
        return False


# ============================================================
# DAM DUPLIKAT-CHECK (Port aus altem Skript)
# ============================================================

def get_existing_dam_filenames(category_id=DEFAULT_CATEGORY_ID):
    """
    Liefert ein Set aller Dateinamen, die bereits im DAM unter category_id liegen.
    Verhindert Doppel-Uploads.
    """
    existing = set()
    headers = get_dam_headers()
    offset = 0
    timeout = config.get('API_REQUEST_TIMEOUT', 120)
    try:
        while True:
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
                logger.warning(f"DAM-Listenabfrage Fehler: HTTP {resp.status_code}")
                break
            data = resp.json()
            assets = data.get('assets', data) if isinstance(data, dict) else data
            if not assets:
                break
            for asset in assets:
                title = asset.get('title') or asset.get('fileName') or ''
                if title:
                    existing.add(title)
                    existing.add(os.path.splitext(title)[0])
            if len(assets) < 250:
                break
            offset += 250
        logger.info(f"DAM: {len(existing)} bestehende Eintraege geladen (Duplikat-Check)")
    except Exception as e:
        logger.warning(f"Fehler beim DAM-Duplikat-Check: {e} — fahre ohne Check fort")
    return existing


# ============================================================
# UPLOAD EIN BILD INS DAM
# ============================================================

def upload_single_image(filepath):
    """
    Upload (so wie altes Skript): MINIMAL Insert.
    DAM extrahiert Keywords + SKU/Position automatisch aus dem Bild + Filename.
    """
    filename = os.path.basename(filepath)
    try:
        # 1) Resize (Metadaten bleiben dank exiftool-Copy erhalten)
        resize_image_to_1800(filepath)

        # 2) Diagnostisches Logging: Keywords + SKU/Position
        sku, position = _parse_sku_and_position(filename)
        keywords = get_image_keywords(filepath)
        if keywords:
            logger.info(f"  📌 Keywords ({filename}): {keywords}")
        else:
            logger.warning(f"  ⚪ KEINE Keywords im Bild gefunden ({filename}) — DAM kann nichts extrahieren!")
        if sku is not None:
            pos_log = f", Pos {position}" if position is not None else ""
            logger.info(f"  Filename codiert: SKU {sku}{pos_log}")

        # 3) SFTP-Upload
        if not upload_to_sftp(filepath, filename):
            logger.warning(f"  SFTP-Upload fehlgeschlagen: {filename}")
            return False

        # 4) DAM-Insert (minimal, exakt wie altes Skript)
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

        # Token-Refresh on 401
        if response.status_code == 401:
            logger.warning("Token abgelaufen, erneuere...")
            invalidate_dam_token()
            headers = get_dam_headers()
            response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=timeout)

        if response.status_code in (200, 201):
            # unique_id aus Insert-Response holen fuer Post-Insert Kategorisierung
            unique_id = ''
            try:
                result = response.json() if response.text else {}
                unique_id = result.get('uniqueId') or result.get('unique_id') or ''
            except Exception:
                pass
            logger.info(f"  ✓ Upload OK: {filename}")

            # Spezifische Kategorie zuweisen (Mood, Clipping, Detail etc.)
            # via asset/category/add — wie altes 02-1_filenaming.py Phase 2
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

IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')


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


def upload_all_images():
    """Upload aller Bilder aus 03-Upload ins DAM. Ueberspringt bereits vorhandene."""
    if not os.path.exists(upload_folder):
        logger.warning(f"Upload-Ordner nicht gefunden: {upload_folder}")
        return 0

    image_files = _collect_image_files(upload_folder)
    if not image_files:
        logger.info("Keine Bilder zum Hochladen gefunden")
        return 0

    # Duplikat-Check ein einziges Mal vorab
    existing = get_existing_dam_filenames(DEFAULT_CATEGORY_ID)
    to_upload = []
    skipped = 0
    for fp in image_files:
        base = os.path.basename(fp)
        base_no_ext = os.path.splitext(base)[0]
        if base in existing or base_no_ext in existing:
            logger.info(f"Übersprungen (bereits im DAM): {base}")
            skipped += 1
        else:
            to_upload.append(fp)

    if skipped:
        logger.info(f"{skipped} bereits vorhandene Assets übersprungen")

    if not to_upload:
        return 0

    logger.info(f"{len(to_upload)} Bilder werden hochgeladen...")

    uploaded = 0
    total = len(to_upload)
    concurrency = min(config.get('ASYNC_TASK_CONCURRENCY', 4), 4)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(upload_single_image, fp): fp for fp in to_upload}
        for future in as_completed(futures):
            try:
                if future.result():
                    uploaded += 1
            except Exception as e:
                logger.warning(f"Upload-Task-Fehler: {e}")

            done_count = sum(1 for f in futures if f.done())
            pct = round(done_count / total * 100)
            print(f"Upload: {done_count}/{total} ({pct}%) — {uploaded} erfolgreich", flush=True)

    logger.info(f"Upload abgeschlossen: {uploaded}/{total} erfolgreich")
    return uploaded


# ============================================================
# CLEANUP
# ============================================================

def cleanup_after_upload():
    """Lokalen 03-Upload Ordner leeren nach erfolgreichem Upload."""
    cleaned = 0
    if os.path.exists(upload_folder):
        for item in os.listdir(upload_folder):
            if item.startswith('.'):
                continue
            path = os.path.join(upload_folder, item)
            try:
                if os.path.isfile(path):
                    os.unlink(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                cleaned += 1
            except Exception as e:
                logger.warning(f"Cleanup-Fehler {item}: {e}")
    logger.info(f"Cleanup: {cleaned} Objekte aus Upload-Ordner entfernt")


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
        uploaded = upload_all_images()

        # Cleanup
        logger.info("Aufraeumen...")
        cleanup_after_upload()

        logger.info("=" * 70)
        if uploaded == len(image_files):
            logger.info(f"✓ ERFOLG: {uploaded} Bilder hochgeladen")
        else:
            failed = len(image_files) - uploaded
            logger.warning(f"⚠ TEILWEISE: {uploaded}/{len(image_files)} erfolgreich, {failed} fehlgeschlagen oder uebersprungen")
        if ticket_key:
            logger.info(f"Ticket: {ticket_key}")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.info("Upload durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
