"""
10-2 Upload ins DAM (via SFTP + API)
- Lädt Webshop-Bilder aus 03-Upload per SFTP zu Cliplister
- Sendet Source-URL per REST-API an DAM
- Weist sie der Kategorie "Input LWDE" (660250) zu
- Räumt lokale Upload-Ordner auf

Workflow:
  1) Bilder lokal vorbereiten (03-Upload)
  2) Per SFTP zu clup01.cliplister.com hochladen
  3) HTTPS-URL der API mitteilen
  4) Asset-Kategorisierung in DAM
  5) Cleanup
"""

import sys
print("Initialisiere...", flush=True)

import os
import shutil
import logging
import time
import json
import requests
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Ziel-Kategorie: "Input LWDE" im DAM
# https://lampenwelt.demoup-cliplister.com/channel/categoryId=660250
INPUT_LWDE_CATEGORY_ID = 660250


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

def upload_single_image(filepath, category_id):
    """Upload image to SFTP, then register with DAM via source-URL."""
    filename = os.path.basename(filepath)
    try:
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

        if response.status_code in [200, 201]:
            result = response.json() if response.text else {}
            unique_id = result.get('uniqueId', '') or result.get('unique_id', '')
            logger.info(f"  OK: {filename}" + (f" (ID: {unique_id})" if unique_id else ""))
            return True

        elif response.status_code == 401:
            logger.warning("Token abgelaufen, erneuere...")
            invalidate_dam_token()
            headers = get_dam_headers()
            response = requests.put(
                DAM_ASSET_INSERT,
                headers=headers,
                json=payload,
                timeout=30
            )
            if response.status_code in [200, 201]:
                logger.info(f"  OK: {filename} (nach Token-Refresh)")
                return True
            else:
                logger.warning(f"  FEHLER: {filename}: HTTP {response.status_code}")
                return False
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
    """Upload all images from 03-Upload to DAM."""
    if not os.path.exists(upload_folder):
        logger.warning(f"Upload-Ordner nicht gefunden: {upload_folder}")
        return 0

    files = []
    for root, dirs, filenames in os.walk(upload_folder):
        for f in filenames:
            if f.startswith('.'):
                continue
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')):
                files.append(os.path.join(root, f))

    if not files:
        logger.info("Keine Bilder zum Hochladen gefunden")
        return 0

    logger.info(f"{len(files)} Bilder gefunden, starte Upload...")

    uploaded = 0
    total = len(files)
    concurrency = min(config.get('ASYNC_TASK_CONCURRENCY', 4), 6)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(upload_single_image, fp, category_id): fp
            for fp in files
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
