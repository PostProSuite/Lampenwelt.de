"""
01 📥 SKU-basierter DAM Import
- Holt Assets aus Cliplister DAM basierend auf Request Keys
- Lädt Bilder herunter und organisiert sie nach Kategorie
- Klassifiziert Bilder per Keywords und ML-Model
- Synchronisiert automatisch mit Lightroom
"""

import os
import sys
import time
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import io
import requests
import logging

# Import utilities
sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    get_dam_token, invalidate_dam_token,
    ask_input, ask_confirm,
    move_files_by_keywords, sync_lightroom,
    CATEGORY_ID_TO_SUBFOLDER,
    validate_file_exists, validate_directory_exists, validate_input_not_empty
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
input_batchfiles_folder = paths['input_batchfiles']

# API Configuration
client_id = config['CLIPLISTER_CLIENT_ID']
client_secret = config['CLIPLISTER_CLIENT_SECRET']

# ============================================================
# PHASE 1: API
# ============================================================

def get_api_response(request_key):
    try:
        token = get_dam_token(config)
        api_url = f"https://api-rs.mycliplister.com/v2.2/apis/asset/list?limit=1000&requestkey={request_key.strip()}&include_meta=true&include_directories=true"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.get(api_url, headers=headers, timeout=config['API_REQUEST_TIMEOUT'])
        if response.status_code == 401:
            invalidate_dam_token()
            return get_api_response(request_key)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        logger.error(f"API-Timeout für Request Key {request_key}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"API-Fehler für {request_key}: {e}")
        raise

def save_all_assets(all_assets):
    try:
        os.makedirs(paths['json'], exist_ok=True)
        output_path = os.path.join(paths['json'], "DAM-Request-Download.json")
        with open(output_path, "w") as file:
            json.dump(all_assets, file, indent=4)
        logger.info(f"Assets gespeichert: {output_path}")
    except IOError as e:
        logger.error(f"Fehler beim Speichern der Assets: {e}")
        raise

# ============================================================
# PHASE 2: BILDER HERUNTERLADEN
# ============================================================

def download_images_from_json():
    try:
        json_file = os.path.join(paths['json'], "DAM-Request-Download.json")
        validate_file_exists(json_file, "DAM-JSON")

        with open(json_file, 'r') as f:
            data = json.load(f)

        url_parameters = "?format=source&strip=no&quality=100"
        downloaded_count = 0
        skipped_count = 0

        for asset in data:
            try:
                if asset.get('fileType') != "Picture":
                    continue

                request_keys = [p.get('requestKey') for p in asset.get('products', [])]
                unique_id = f"#{asset.get('uniqueId')}#"
                file_ext = asset.get('fileExt', '').lower()

                if not file_ext:
                    logger.warning(f"Asset ohne Dateityp: {asset.get('title')}")
                    continue

                # Nur den ersten Link herunterladen (mehrere Links = gleiche Datei in verschiedenen Auflösungen)
                links = asset.get('links', [])
                if not links:
                    logger.warning(f"Keine Links für Asset: {asset.get('title')}")
                    skipped_count += 1
                    continue

                image_url = links[0].get('location') + url_parameters
                try:
                    response = requests.get(image_url, timeout=config['API_REQUEST_TIMEOUT'])
                    response.raise_for_status()

                    # Subfolder bestimmen
                    subfolder = None
                    for category in asset.get('categories', []):
                        if category.get('id') in CATEGORY_ID_TO_SUBFOLDER:
                            subfolder = CATEGORY_ID_TO_SUBFOLDER[category.get('id')]
                            break

                    subfolder_path = os.path.join(input_batchfiles_folder, subfolder) if subfolder else input_batchfiles_folder
                    os.makedirs(subfolder_path, exist_ok=True)

                    request_keys_str = "_".join(request_keys) if request_keys else "no_keys"

                    if file_ext == "webp":
                        filename = f"{request_keys_str}_{unique_id}.jpg"
                        save_path = os.path.join(subfolder_path, filename)
                        try:
                            img = Image.open(io.BytesIO(response.content)).convert("RGB")
                            img.save(save_path, format="JPEG", quality=85)
                            downloaded_count += 1
                            logger.info(f"WebP konvertiert: {filename}")
                        except Exception as e:
                            logger.error(f"Konvertierungsfehler für {filename}: {e}")
                            skipped_count += 1
                    else:
                        filename = f"{request_keys_str}_{unique_id}.{file_ext}"
                        save_path = os.path.join(subfolder_path, filename)
                        with open(save_path, 'wb') as img_file:
                            img_file.write(response.content)
                        downloaded_count += 1
                        logger.info(f"Heruntergeladen: {filename}")
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Fehler beim Herunterladen von {image_url}: {e}")
                    skipped_count += 1
            except Exception as e:
                logger.warning(f"Fehler bei Asset {asset.get('title', '?')}: {e}")
                skipped_count += 1

        logger.info(f"Download abgeschlossen: {downloaded_count} erfolgreich, {skipped_count} übersprungen")
        return downloaded_count > 0
    except Exception as e:
        logger.error(f"Fehler beim Image Download: {e}")
        return False

# PHASE 3: Dateien nach Keywords verschieben → aus _utils

# ============================================================
# PHASE 4: ML AUTO-KLASSIFIKATION
# ============================================================

def run_ml_classification():
    try:
        import coremltools
        model_path = os.path.join(paths['scripts'], "2023-Lawe-Main-Classes-V5.mlmodel")

        if not os.path.exists(model_path):
            logger.warning("ML-Modell nicht gefunden – überspringe ML-Klassifikation")
            return True

        model = coremltools.models.MLModel(model_path)
        moved_images_path = os.path.join(input_batchfiles_folder, "00-Check")
        skipped_count = 0
        required_size = (299, 299)

        for filename in os.listdir(input_batchfiles_folder):
            if filename.startswith('.') or filename.startswith('00-'):
                continue

            if filename.lower().endswith((".jpg", ".jpeg", ".png", ".psd", ".tif", ".tiff", ".gif")):
                file_path = os.path.join(input_batchfiles_folder, filename)
                try:
                    image = Image.open(file_path).convert('RGB').resize(required_size)
                    predictions = model.predict({'image': image})
                    top_prediction = max(predictions['classLabelProbs'], key=predictions['classLabelProbs'].get)
                    target_folder = os.path.join(moved_images_path, top_prediction)
                    os.makedirs(target_folder, exist_ok=True)
                    shutil.move(file_path, os.path.join(target_folder, filename))
                except Exception as e:
                    skipped_count += 1
                    logger.warning(f"ML-Fehler für {filename}: {e}")

        logger.info(f"ML-Klassifikation abgeschlossen ({skipped_count} übersprungen)")
        return True
    except ImportError:
        logger.warning("coremltools nicht installiert – überspringe ML-Klassifikation")
        return True
    except Exception as e:
        logger.error(f"Fehler bei ML-Klassifikation: {e}")
        return False

# PHASE 5: Lightroom Sync → aus _utils

# ============================================================
# INPUT
# ============================================================

def request_skus():
    # App übergibt Wert via POSTPRO_INPUT (kein Dialog nötig)
    env_val = os.environ.get("POSTPRO_INPUT", "").strip()
    if env_val:
        return env_val
    # Fallback: nativer macOS Dialog
    result = subprocess.run(
        ['osascript', '-e',
         'text returned of (display dialog "Request Keys eingeben (durch Leerzeichen oder Zeilenumbrüche getrennt):" '
         'default answer "" with title "📥 SKU-basierter Import")'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None

# ============================================================
# MAIN
# ============================================================

def clear_input_batchfiles():
    """Leert den Input-Batchfiles Ordner vor dem Download"""
    try:
        paths = get_paths()
        input_folder = paths['input']
        if not os.path.exists(input_folder):
            logger.info(f"Input-Ordner existiert nicht: {input_folder}")
            return True
        for item in os.listdir(input_folder):
            item_path = os.path.join(input_folder, item)
            try:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                logger.warning(f"Fehler beim Löschen von {item}: {e}")
        logger.info(f"Input-Ordner geleert: {input_folder}")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Leeren des Input-Ordners: {e}")
        return False

if __name__ == "__main__":
    try:
        logger.info("="*70)
        logger.info("01 📥 SKU-basierter DAM Import gestartet")
        logger.info("="*70)

        # Phase 0: Input-Ordner leeren
        if not clear_input_batchfiles():
            logger.warning("Input-Ordner konnte nicht geleert werden")

        # Input
        request_keys_input = request_skus()
        if not request_keys_input:
            logger.info("Import abgebrochen")
            sys.exit(0)

        request_keys = [key.strip() for key in request_keys_input.split() if key.strip()]
        if not request_keys:
            logger.error("Keine gültigen Request Keys eingegeben")
            sys.exit(1)

        logger.info(f"Import für {len(request_keys)} Request Key(s) gestartet")

        # Phase 1-2: API-Daten holen
        all_assets = []
        for request_key in request_keys:
            try:
                response_data = get_api_response(request_key)
                if isinstance(response_data, list):
                    all_assets.extend(response_data)
                else:
                    logger.warning(f"Unerwartetes Format für {request_key}")
            except Exception as e:
                logger.error(f"Fehler bei {request_key}: {e}")
                continue
            time.sleep(config['API_REQUEST_DELAY'])

        if not all_assets:
            logger.error("Keine Assets vom DAM geholt")
            sys.exit(1)

        save_all_assets(all_assets)

        # Phase 2: Bilder herunterladen
        if not download_images_from_json():
            logger.warning("Download mit Fehlern abgeschlossen")

        # Phase 3: Nach Keywords verschieben
        move_files_by_keywords(input_batchfiles_folder, logger, config['ASYNC_TASK_CONCURRENCY'])

        # Phase 4: ML-Klassifikation
        run_ml_classification()

        # Phase 5: Lightroom synchronisieren
        sync_lightroom(logger, ask_first=True)

        logger.info("="*70)
        logger.info("✅ Import erfolgreich abgeschlossen")
        logger.info("="*70)

    except KeyboardInterrupt:
        logger.info("Import durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
