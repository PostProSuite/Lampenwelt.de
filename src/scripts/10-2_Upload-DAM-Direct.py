"""
10-2 Upload ins DAM (via SFTP + API)
- Lädt Webshop-Bilder aus 03-Upload per SFTP zu Cliplister
- Sendet Source-URL per REST-API an DAM
- Weist sie der korrekten Kategorie zu (anhand Unterordner)
- Setzt requestKey (SKU aus Dateiname) und webEnabled=True
- Räumt lokale Upload-Ordner auf

Workflow:
  1) Bilder lokal vorbereiten (03-Upload)
  2) Per SFTP zu clup01.cliplister.com hochladen
  3) HTTPS-URL der API mitteilen → Asset-Insert
  4) Asset-Update: requestKey, webEnabled, Titel setzen
  5) Cleanup
"""

import sys
print("Initialisiere...", flush=True)

import os
import re
import shutil
import logging
import time
import json
import requests
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    get_dam_token, invalidate_dam_token,
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

# DAM API — gleiche Basis wie alle funktionierenden Scripts
DAM_API_BASE = "https://api-rs.mycliplister.com/v2.2/apis"
DAM_ASSET_INSERT = f"{DAM_API_BASE}/asset/insert"
DAM_ASSET_CATEGORY_ADD = f"{DAM_API_BASE}/asset/category/add"

# Ziel-Kategorie: "Input LWDE" im DAM (Fallback wenn kein Unterordner erkannt)
# https://lampenwelt.demoup-cliplister.com/channel/categoryId=660250
INPUT_LWDE_CATEGORY_ID = 660250

# Subfolder-Name → DAM-Kategorie-ID (identisch mit 02-1_filenaming.py)
SUBFOLDER_TO_CATEGORY = {
    "A10-Mood":           408719,
    "B20-Clipping":       408735,
    "B30-Dimensions":     408736,
    "B40-Neutral":        408720,
    "C-Detail":           408721,
    "C50-Shade":          408753,
    "C60-Material":       408752,
    "C70-Switch":         408751,
    "C80-Base_Stand":     408750,
    "C90-Cable":          408749,
    "C95-Split":          408747,
    "D-Technical":        408722,
    "D110-Remote":        408756,
    "D120-Accesories":    408755,
    "E130-Graphics":      408723,
    "E130-Graphics_DE":   408778,
    "E130-Graphics_INT":  408777,
    "E130-Graphics_ENG":  408776,
    "F140-Group":         408762,
    "F-Group":            408762,
    "G-UGC":              408760,
}


# ============================================================
# DAM API HELPERS
# ============================================================

def get_dam_headers():
    """Get authorization headers for DAM API."""
    token = get_dam_token(config)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }


# EXIF-Keyword → DAM-Kategorie-ID
# 02-1_filenaming.py schreibt diese Keywords per exiftool in Phase 5
KEYWORD_TO_CATEGORY_ID = {
    'Clipping':    408735, 'Freisteller': 408735,
    'Neutral':     408720, 'Produktbild': 408720,
    'Mood':        408719, 'Ambiente':    408719,
    'Technical':   408722, 'Technisch':   408722,
    'Dimensions':  408736,
    'Detail':      408721,
    'Shade':       408753, 'Schirm':      408753,
    'Material':    408752,
    'Switch':      408751, 'Schalter':    408751,
    'Base_Stand':  408750,
    'Cable':       408749, 'Kabel':       408749,
    'Split':       408747,
    'Remote':      408756, 'Fernbedienung': 408756,
    'Accessories': 408755, 'Zubehoer':    408755,
    'Group':       408762, 'Gruppe':      408762,
    'UGC':         408760,
    'Graphics':    408723, 'Grafik':      408723,
}


def get_image_keywords(filepath):
    """
    Liest ALLE Keywords aus der Bilddatei (XMP:Subject, IPTC:Keywords, EXIF).
    Mit ausführlichem Logging - damit wir genau sehen was passiert.
    Gibt Liste der eindeutigen Keywords zurück.
    """
    keywords = []
    fname = os.path.basename(filepath)

    # Strategy 1: exiftool (am zuverlässigsten - liest XMP, IPTC, EXIF)
    # Liest ALLE möglichen Keyword-Felder (Lightroom nutzt verschiedene)
    exiftool_used = False
    try:
        from _utils import find_exiftool
        exiftool = find_exiftool()
        if exiftool:
            exiftool_used = True
            import subprocess as sp
            # -j: JSON output, -G: group names (XMP:, IPTC:, etc)
            # Liest alle Keyword-Varianten die Lightroom oder andere Tools setzen
            result = sp.run(
                [exiftool, '-j',
                 '-XMP:Subject',           # Standard XMP keywords (Lightroom default)
                 '-IPTC:Keywords',         # IPTC-IIM keywords
                 '-Keywords',              # Generic keywords
                 '-XMP-lr:HierarchicalSubject',  # Lightroom hierarchical
                 '-XMP-dc:Subject',        # Dublin Core (alternative XMP)
                 filepath],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    if isinstance(data, list) and data:
                        meta = data[0]
                        # Alle möglichen Felder durchgehen
                        for field in ('Subject', 'Keywords', 'HierarchicalSubject',
                                      'XMP-lr:HierarchicalSubject', 'XMP-dc:Subject'):
                            val = meta.get(field)
                            if not val:
                                continue
                            if isinstance(val, list):
                                for kw in val:
                                    kw_str = str(kw).strip()
                                    # HierarchicalSubject hat Format "Parent|Child"
                                    if '|' in kw_str:
                                        kw_str = kw_str.split('|')[-1]
                                    if kw_str and kw_str not in keywords:
                                        keywords.append(kw_str)
                            else:
                                # String form: comma- or semicolon-separated
                                for kw in re.split(r'[,;]', str(val)):
                                    kw = kw.strip()
                                    if '|' in kw:
                                        kw = kw.split('|')[-1]
                                    if kw and kw not in keywords:
                                        keywords.append(kw)
                        # Diagnose: zeige rohe Metadaten wenn nichts gefunden
                        if not keywords:
                            logger.debug(f"  exiftool fand keine Keywords. Raw meta: {meta}")
                except json.JSONDecodeError as e:
                    logger.debug(f"  exiftool JSON parse error für {fname}: {e}")
            else:
                logger.debug(f"  exiftool returncode={result.returncode} stderr={result.stderr[:100]}")
    except Exception as e:
        logger.debug(f"  exiftool keyword-read fehlgeschlagen ({fname}): {e}")

    # Strategy 2: XMP via Raw-Read (auch wenn exiftool was gefunden hat - findet evtl noch mehr)
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
        # Suche das XMP-Paket
        start = raw.find(b'<rdf:RDF')
        end   = raw.find(b'</rdf:RDF>')
        if start != -1 and end != -1:
            xmp = raw[start:end + len('</rdf:RDF>')].decode('utf-8', errors='ignore')
            # Suche speziell nach dc:subject (das ist wo Lightroom Keywords ablegt)
            subject_match = re.search(
                r'<dc:subject>(.*?)</dc:subject>',
                xmp, re.DOTALL
            )
            if subject_match:
                subject_block = subject_match.group(1)
                for kw in re.findall(r'<rdf:li[^>]*>([^<]+)</rdf:li>', subject_block):
                    kw = kw.strip()
                    if kw and kw not in keywords:
                        keywords.append(kw)
    except Exception as e:
        logger.debug(f"  XMP keyword-read fehlgeschlagen ({fname}): {e}")

    # Diagnose-Log: wir wollen wissen was passiert ist
    if keywords:
        logger.info(f"  📌 Keywords aus Bild gelesen ({fname}): {keywords}" +
                    (f" [via exiftool]" if exiftool_used else " [via XMP-Raw]"))
    else:
        logger.info(f"  ⚪ KEINE Keywords im Bild gefunden ({fname})" +
                    (f" — exiftool getestet" if exiftool_used else " — KEIN exiftool!"))

    return keywords


def get_exif_category(filepath):
    """
    Gibt DAM-Kategorie-ID basierend auf Image-Keywords zurück.
    Nutzt get_image_keywords() für robustes Lesen.
    """
    keywords = get_image_keywords(filepath)
    for kw in keywords:
        cat_id = KEYWORD_TO_CATEGORY_ID.get(kw)
        if cat_id:
            logger.info(f"  EXIF-Keyword '{kw}' → Kategorie {cat_id}")
            return cat_id
    return None


def update_asset_keywords(unique_id, keywords, filename):
    """
    Setzt Tags/Keywords auf das DAM-Asset.
    Versucht MEHRERE Strategien parallel - mit ausführlichem Logging
    damit wir genau sehen welche Cliplister-Variante funktioniert.
    """
    if not keywords:
        return False

    base = f"{DAM_API_BASE}/asset/update?unique_id={unique_id}"

    # Verschiedene Format-Varianten die Cliplister haben könnte
    strategies = [
        # Format A: tags als Array von Strings
        ('tags-array',     {"tags": keywords}),
        # Format B: keywords als Array von Strings
        ('keywords-array', {"keywords": keywords}),
        # Format C: tags als comma-separated string
        ('tags-csv',       {"tags": ", ".join(keywords)}),
        # Format D: keywords als comma-separated string
        ('keywords-csv',   {"keywords": ", ".join(keywords)}),
        # Format E: tags als Array von Objekten {name: ...}
        ('tags-objects',   {"tags": [{"name": k} for k in keywords]}),
        # Format F: meta.keywords (verschachtelt)
        ('meta-keywords',  {"meta": {"keywords": keywords}}),
    ]

    for label, payload in strategies:
        try:
            resp = requests.put(base, headers=get_dam_headers(), json=payload, timeout=30)
            body = resp.text[:200] if resp.text else '(empty)'
            logger.info(f"  🏷️  Tag-Strategy [{label}] → HTTP {resp.status_code} {body}")
            if resp.status_code in [200, 204]:
                # Verify it actually saved by GET-ing the asset
                if _verify_keywords_saved(unique_id, keywords):
                    logger.info(f"  ✅ Keywords ({len(keywords)}) erfolgreich gespeichert via [{label}]")
                    return True
                else:
                    logger.info(f"  ⚠ HTTP {resp.status_code} aber Keywords nicht gefunden bei GET — versuche nächste Strategy")
        except Exception as e:
            logger.debug(f"  Tag-Strategy [{label}] Exception: {e}")

    # Strategy G: einzelner tag/add Endpoint pro Keyword
    success_count = 0
    for kw in keywords:
        for endpoint_template in [
            f"{DAM_API_BASE}/asset/tag/add?unique_id={unique_id}&tag={requests.utils.quote(kw)}",
            f"{DAM_API_BASE}/asset/keyword/add?unique_id={unique_id}&keyword={requests.utils.quote(kw)}",
        ]:
            try:
                resp = requests.put(endpoint_template, headers=get_dam_headers(), json={}, timeout=15)
                if resp.status_code in [200, 204]:
                    success_count += 1
                    break
                else:
                    logger.debug(f"  /tag/add [{kw}] → HTTP {resp.status_code}")
            except Exception as e:
                logger.debug(f"  /tag/add [{kw}] Exception: {e}")

    if success_count > 0:
        logger.info(f"  ✅ Tags: {success_count}/{len(keywords)} via einzelne tag/add Calls gesetzt")
        return True

    logger.warning(f"  ⚠ ALLE Strategien für Keyword-Übermittlung fehlgeschlagen ({filename})")
    logger.warning(f"     Keywords waren: {keywords}")
    return False


def _verify_keywords_saved(unique_id, expected_keywords):
    """GET das Asset und prüfe ob Keywords/Tags wirklich gespeichert sind."""
    try:
        url = f"{DAM_API_BASE}/asset/list?unique_id={unique_id}&limit=1"
        resp = requests.get(url, headers=get_dam_headers(), timeout=15)
        if resp.status_code != 200:
            return False
        data = resp.json()
        # Asset kann an verschiedenen Stellen sein
        assets = data.get('assets') if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not assets:
            return False
        asset = assets[0]
        # Suche in mehreren möglichen Feldern
        for field in ('tags', 'keywords', 'subject'):
            val = asset.get(field)
            if val:
                # Vergleiche - mindestens eines unserer Keywords muss da sein
                str_val = json.dumps(val).lower() if not isinstance(val, str) else val.lower()
                if any(kw.lower() in str_val for kw in expected_keywords):
                    return True
        return False
    except Exception as e:
        logger.debug(f"  Verify-GET fehlgeschlagen: {e}")
        return False


def update_asset_after_upload(unique_id, filename, subfolder=None, filepath=None):
    """
    Nach erfolgreichem Insert in DAM:
      1. Basis-Update (Titel, SKU, webEnabled) — OHNE Tags (verhindert "Workflow-Error")
      2. Kategorie zuweisen
      3. Keywords/Tags separat setzen (eigene API-Calls mit Format-Detection)

    Wichtig: Tags müssen SEPARAT gesetzt werden! Wenn Tags inline mit webEnabled
    kommen, triggert der DAM-Workflow einen "Workflow-Error".
    """
    try:
        filename_no_ext = os.path.splitext(filename)[0]

        # SKU aus Dateiname extrahieren (erste 7-8 Ziffern vor dem ersten _)
        sku_match = re.match(r'^(\d{7,8})', filename_no_ext)
        sku = sku_match.group(1) if sku_match else None

        # ───── PHASE 1: Keywords aus dem Bild lesen ─────
        image_keywords = []
        if filepath and os.path.exists(filepath):
            image_keywords = get_image_keywords(filepath)

        # ───── PHASE 2: Basis-Update (KEINE Tags!) ─────
        headers = get_dam_headers()
        update_url = f"{DAM_API_BASE}/asset/update?unique_id={unique_id}"
        data = {
            "title": filename_no_ext,
            "webEnabled": True,
        }
        if sku:
            data["products"] = [
                {"requestKey": sku, "title": filename_no_ext, "keyType": 100}
            ]

        resp = requests.put(update_url, headers=headers, json=data, timeout=30)
        if resp.status_code in [200, 204]:
            logger.info(f"  Metadaten: {filename_no_ext} | SKU={sku} | webEnabled=True ✓")
        else:
            logger.warning(f"  Metadaten-Update fehlgeschlagen {filename}: HTTP {resp.status_code} - {resp.text[:200]}")

        # ───── PHASE 3: Kategorie zuweisen ─────
        category_id = SUBFOLDER_TO_CATEGORY.get(subfolder) if subfolder else None
        if not category_id and image_keywords:
            for kw in image_keywords:
                cat_id = KEYWORD_TO_CATEGORY_ID.get(kw)
                if cat_id:
                    logger.info(f"  EXIF-Keyword '{kw}' → Kategorie {cat_id}")
                    category_id = cat_id
                    break

        if category_id:
            # Input-LWDE-Kategorie entfernen
            remove_url = f"{DAM_API_BASE}/asset/category/remove?unique_id={unique_id}&category_id={INPUT_LWDE_CATEGORY_ID}"
            requests.put(remove_url, headers=get_dam_headers(), json={}, timeout=15)

            # Korrekte Kategorie setzen
            add_url = f"{DAM_API_BASE}/asset/category/add?unique_id={unique_id}&category_id={category_id}"
            resp2 = requests.put(add_url, headers=get_dam_headers(), json={}, timeout=15)
            if resp2.status_code in [200, 204]:
                cat_src = subfolder if subfolder else 'EXIF'
                logger.info(f"  Kategorie {category_id} gesetzt [{cat_src}] ✓")
            else:
                logger.warning(f"  Kategorie-Update fehlgeschlagen: HTTP {resp2.status_code}")
        else:
            logger.warning(f"  Keine Kategorie erkannt für {filename} — bleibt in Input LWDE")

        # ───── PHASE 4: Keywords/Tags separat setzen ─────
        if image_keywords:
            update_asset_keywords(unique_id, image_keywords, filename)

    except Exception as e:
        logger.error(f"  Fehler beim Asset-Update nach Upload {filename}: {e}")
        import traceback
        logger.debug(traceback.format_exc())


# ============================================================
# IMAGE RESIZING
# ============================================================

def resize_image_to_1800(image_path):
    """Resize image to max 1800x1800 pixels, maintaining aspect ratio."""
    try:
        img = Image.open(image_path)

        # Get original dimensions
        original_width, original_height = img.size

        # If already smaller than 1800x1800, don't resize
        if original_width <= 1800 and original_height <= 1800:
            return True

        # Calculate new dimensions maintaining aspect ratio
        max_size = 1800
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        # Save resized image (overwrite original)
        img.save(image_path, quality=95, optimize=True)
        logger.info(f"Bild verkleinert: {image_path} ({original_width}x{original_height} → {img.size[0]}x{img.size[1]})")
        return True
    except Exception as e:
        logger.error(f"Image-Resizing-Fehler {image_path}: {e}")
        return False


# ============================================================
# SFTP UPLOAD
# ============================================================

def upload_to_sftp(local_path, sftp_filename):
    """Upload file to SFTP server."""
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
# BILDER INS DAM HOCHLADEN (via SFTP-URL)
# ============================================================

def upload_single_image(filepath, default_category_id, subfolder=None):
    """
    Upload image to SFTP, then register with DAM.
    ALLE Metadaten im EINEN Insert-Call statt separate Update-Calls
    (das alte Skript hat das auch so gemacht, daher hat es funktioniert).
    """
    filename = os.path.basename(filepath)
    filename_no_ext = os.path.splitext(filename)[0]
    try:
        # Step 0: Resize image to max 1800x1800
        resize_image_to_1800(filepath)

        # Step 1: Read keywords aus Bild (BEVOR Resize könnte sie zerstören - aber Resize behält EXIF)
        image_keywords = get_image_keywords(filepath)

        # Step 2: SKU aus Filename
        sku_match = re.match(r'^(\d{7,8})', filename_no_ext)
        sku = sku_match.group(1) if sku_match else None

        # Step 3: Korrekte Kategorie bestimmen (Subfolder > Keywords > Input LWDE Fallback)
        category_id = SUBFOLDER_TO_CATEGORY.get(subfolder) if subfolder else None
        if not category_id and image_keywords:
            for kw in image_keywords:
                cat_id = KEYWORD_TO_CATEGORY_ID.get(kw)
                if cat_id:
                    logger.info(f"  EXIF-Keyword '{kw}' → Kategorie {cat_id}")
                    category_id = cat_id
                    break
        if not category_id:
            category_id = default_category_id  # Input LWDE als Fallback

        # Step 4: Upload to SFTP
        if not upload_to_sftp(filepath, filename):
            logger.warning(f"  SFTP-Upload fehlgeschlagen: {filename}")
            return False

        # Step 5: Construct HTTPS URL for DAM API
        sftp_url = f"https://clup01.cliplister.com/files/{config['SFTP_USERNAME']}{config['SFTP_REMOTE_DIR']}/{filename}".replace('\\', '/')

        # Step 6: Register with DAM - ALLE Metadaten in EINEM Insert-Call
        # (Wie das alte Skript! Update danach hat HTTP 500 verursacht)
        headers = get_dam_headers()
        payload = {
            "fileName": filename,
            "source": sftp_url,
            "title": filename_no_ext,
            "categories": [{"id": category_id}],
            "webEnabled": True,
        }
        if sku:
            payload["products"] = [
                {"requestKey": sku, "title": filename_no_ext, "keyType": 100}
            ]
        if image_keywords:
            payload["tags"] = image_keywords

        response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=30)

        # Token-Refresh on 401
        if response.status_code == 401:
            logger.warning("Token abgelaufen, erneuere...")
            invalidate_dam_token()
            headers = get_dam_headers()
            response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=30)

        # Insert mit allem klappt? Perfekt, fertig.
        if response.status_code in [200, 201]:
            result = response.json() if response.text else {}
            unique_id = result.get('uniqueId', '') or result.get('unique_id', '')
            kw_log = f" | {len(image_keywords)} Keywords" if image_keywords else ""
            logger.info(f"  Upload OK: {filename} | Kategorie {category_id}{kw_log} ✓")

            # Verify keywords falls API sie still ignoriert hat
            if image_keywords and unique_id:
                if not _verify_keywords_saved(unique_id, image_keywords):
                    logger.info(f"  ⚠ Keywords beim Insert nicht gespeichert - versuche separat...")
                    update_asset_keywords(unique_id, image_keywords, filename)
            return True

        # Fallback bei Fehler: Minimal-Insert (wie altes Skript)
        logger.warning(f"  Insert mit Metadaten fehlgeschlagen ({response.status_code}) - versuche Fallback...")
        minimal_payload = {
            "fileName": filename,
            "source": sftp_url,
            "categories": [{"id": category_id}],
        }
        response = requests.put(DAM_ASSET_INSERT, headers=headers, json=minimal_payload, timeout=30)

        if response.status_code in [200, 201]:
            result = response.json() if response.text else {}
            unique_id = result.get('uniqueId', '') or result.get('unique_id', '')
            logger.info(f"  Upload OK [Fallback]: {filename} | Kategorie {category_id}")
            # Versuche Tags separat zu setzen
            if image_keywords and unique_id:
                update_asset_keywords(unique_id, image_keywords, filename)
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


def upload_all_images(category_id):
    """Upload all images from 03-Upload to DAM.
    Subfolder name (e.g. B20-Clipping) is passed to upload_single_image
    so it can set the correct DAM category and remove 'Input LWDE'."""
    if not os.path.exists(upload_folder):
        logger.warning(f"Upload-Ordner nicht gefunden: {upload_folder}")
        return 0

    # Collect (filepath, subfolder_name) pairs
    file_pairs = []
    for root, dirs, filenames in os.walk(upload_folder):
        subfolder = os.path.basename(root)
        # Only treat it as a known subfolder if it's in our mapping
        known_subfolder = subfolder if subfolder in SUBFOLDER_TO_CATEGORY else None
        for f in filenames:
            if f.startswith('.'):
                continue
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')):
                file_pairs.append((os.path.join(root, f), known_subfolder))

    if not file_pairs:
        logger.info("Keine Bilder zum Hochladen gefunden")
        return 0

    logger.info(f"{len(file_pairs)} Bilder gefunden, starte Upload...")

    uploaded = 0
    total = len(file_pairs)
    concurrency = min(config.get('ASYNC_TASK_CONCURRENCY', 4), 4)  # capped at 4 to avoid rate limits

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(upload_single_image, fp, category_id, subfolder): fp
            for fp, subfolder in file_pairs
        }
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
    """Clean up local Upload folder after DAM upload."""
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

        # Ticket-Key aus Input lesen (optional, fuer Logging)
        ticket_key = os.environ.get("POSTPRO_INPUT", "").strip()

        if not ticket_key:
            ticket_file = os.path.join(upload_folder, ".ticket_key")
            if os.path.exists(ticket_file):
                with open(ticket_file, 'r') as f:
                    ticket_key = f.read().strip()

        # Prefix sicherstellen
        if ticket_key and '-' not in ticket_key:
            ticket_key = f"CREAMEDIA-{ticket_key}"

        if ticket_key:
            logger.info(f"Ticket: {ticket_key}")

        logger.info(f"Ziel-Kategorie: Input LWDE (ID {INPUT_LWDE_CATEGORY_ID})")

        # Pruefen ob Bilder im Upload-Ordner liegen (REKURSIV - inkl. Unterordner!)
        # Bilder können nach 02-1_filenaming.py in Unterordnern liegen (B20-Clipping, A10-Mood, etc.)
        if not os.path.exists(upload_folder):
            logger.error(f"Upload-Ordner existiert nicht: {upload_folder}")
            sys.exit(1)

        image_files = []
        IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')
        for root, dirs, filenames in os.walk(upload_folder):
            for f in filenames:
                if f.startswith('.'):
                    continue
                if f.lower().endswith(IMG_EXTENSIONS):
                    image_files.append(os.path.join(root, f))

        if not image_files:
            logger.error("Keine Bilder im Upload-Ordner gefunden!")
            logger.error(f"Ordner: {upload_folder}")
            logger.error("Tipp: Bilder müssen in 03-Upload (oder Unterordnern wie B20-Clipping) liegen")
            sys.exit(1)

        # Zeige Zusammenfassung: wie viele Bilder, in welchen Ordnern
        folder_counts = {}
        for fp in image_files:
            folder = os.path.basename(os.path.dirname(fp))
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
        folder_summary = ", ".join(f"{c} in {f}" for f, c in folder_counts.items())
        logger.info(f"{len(image_files)} Bilder gefunden ({folder_summary})")

        # Upload
        logger.info("Bilder ins DAM hochladen...")
        uploaded = upload_all_images(INPUT_LWDE_CATEGORY_ID)

        if uploaded == 0:
            logger.error("Keine Bilder konnten hochgeladen werden!")
            sys.exit(1)

        # Cleanup
        logger.info("Aufraeumen...")
        cleanup_after_upload()

        logger.info("=" * 70)
        if uploaded == len(image_files):
            logger.info(f"✓ ERFOLG: {uploaded} Bilder hochgeladen")
        else:
            failed = len(image_files) - uploaded
            logger.warning(f"⚠ TEILWEISE: {uploaded}/{len(image_files)} erfolgreich, {failed} fehlgeschlagen")
        logger.info(f"Ziel-Kategorie: Input LWDE (ID {INPUT_LWDE_CATEGORY_ID})")
        if ticket_key:
            logger.info(f"Ticket: {ticket_key}")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.info("Upload durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
