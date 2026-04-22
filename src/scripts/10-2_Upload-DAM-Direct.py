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


def get_exif_category(filepath):
    """
    Liest XMP:Subject Keywords aus der Bilddatei und gibt die passende
    DAM-Kategorie-ID zurück. Fallback wenn kein Unterordner bekannt ist.
    """
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
        # XMP-Paket suchen (im JPEG-Body enthalten)
        start = raw.find(b'<rdf:RDF')
        end   = raw.find(b'</rdf:RDF>')
        if start == -1 or end == -1:
            return None
        xmp = raw[start:end + len('</rdf:RDF>')].decode('utf-8', errors='ignore')
        # Keywords aus <rdf:li>...</rdf:li> extrahieren
        keywords = re.findall(r'<rdf:li[^>]*>([^<]+)</rdf:li>', xmp)
        for kw in keywords:
            cat_id = KEYWORD_TO_CATEGORY_ID.get(kw.strip())
            if cat_id:
                logger.info(f"  EXIF-Keyword '{kw}' → Kategorie {cat_id}")
                return cat_id
    except Exception:
        pass
    return None


def update_asset_after_upload(unique_id, filename, subfolder=None, filepath=None):
    """
    Nach erfolgreichem Insert: requestKey (SKU), Titel und webEnabled setzen.
    Kategorie: 1. aus Unterordner-Name, 2. aus EXIF-Keywords (gesetzt von 02-1_filenaming.py)
    """
    try:
        filename_no_ext = os.path.splitext(filename)[0]

        # SKU aus Dateiname extrahieren (erste 7-8 Ziffern vor dem ersten _)
        sku_match = re.match(r'^(\d{7,8})', filename_no_ext)
        sku = sku_match.group(1) if sku_match else None

        headers = get_dam_headers()

        # Asset-Update: Titel, Produkt (requestKey) und webEnabled
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
            logger.warning(f"  Metadaten-Update fehlgeschlagen {filename}: HTTP {resp.status_code}")

        # Kategorie bestimmen: 1. Unterordner, 2. EXIF-Keywords
        category_id = SUBFOLDER_TO_CATEGORY.get(subfolder) if subfolder else None
        if not category_id and filepath and os.path.exists(filepath):
            category_id = get_exif_category(filepath)

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

    except Exception as e:
        logger.error(f"  Fehler beim Asset-Update nach Upload {filename}: {e}")


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

def upload_single_image(filepath, category_id, subfolder=None):
    """Upload image to SFTP, then register with DAM via source-URL.
    Afterwards: set requestKey (SKU), webEnabled=True and correct category."""
    filename = os.path.basename(filepath)
    try:
        # Step 0: Resize image to max 1800x1800
        resize_image_to_1800(filepath)

        # Step 1: Upload to SFTP
        if not upload_to_sftp(filepath, filename):
            logger.warning(f"  SFTP-Upload fehlgeschlagen: {filename}")
            return False

        # Step 2: Construct HTTPS URL for DAM API
        sftp_url = f"https://clup01.cliplister.com/files/{config['SFTP_USERNAME']}{config['SFTP_REMOTE_DIR']}/{filename}".replace('\\', '/')

        # Step 3: Register with DAM
        headers = get_dam_headers()
        payload = {
            "fileName": filename,
            "source": sftp_url,
            "categories": [{"id": category_id}],
        }

        response = requests.put(
            DAM_ASSET_INSERT,
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code == 401:
            logger.warning("Token abgelaufen, erneuere...")
            invalidate_dam_token()
            headers = get_dam_headers()
            response = requests.put(DAM_ASSET_INSERT, headers=headers, json=payload, timeout=30)

        if response.status_code in [200, 201]:
            result = response.json() if response.text else {}
            unique_id = result.get('uniqueId', '') or result.get('unique_id', '')
            logger.info(f"  Upload OK: {filename}" + (f" (ID: {unique_id})" if unique_id else ""))

            # Step 4: Set requestKey, title, webEnabled and correct category
            if unique_id:
                update_asset_after_upload(unique_id, filename, subfolder, filepath)

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

        # Pruefen ob Bilder im Upload-Ordner liegen
        if not os.path.exists(upload_folder):
            logger.error(f"Upload-Ordner existiert nicht: {upload_folder}")
            sys.exit(1)

        image_files = [
            f for f in os.listdir(upload_folder)
            if not f.startswith('.') and f.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff'))
        ]

        if not image_files:
            logger.error("Keine Bilder im Upload-Ordner gefunden!")
            logger.error(f"Ordner: {upload_folder}")
            sys.exit(1)

        logger.info(f"{len(image_files)} Bilder im Upload-Ordner gefunden")

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
