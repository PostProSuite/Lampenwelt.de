"""
02 📥 Kategorie-basierter DAM Import
- Holt Assets aus Cliplister DAM basierend auf Kategorie-ID
- Leert vorher den Input-Ordner automatisch
- Lädt Bilder herunter und organisiert sie nach Kategorie
- Klassifiziert Bilder per Keywords und ML-Model
- Synchronisiert automatisch mit Lightroom
"""

import sys
print("Initialisiere...", flush=True)

import os
import time
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import io
import requests
import logging

sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    get_dam_token, invalidate_dam_token,
    ask_input,
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

base_api_url = "https://api-rs.mycliplister.com/v2.2/apis/asset/list"

# ============================================================
# PHASE 0: INPUT-ORDNER LEEREN
# ============================================================

def clear_input_batchfiles():
    try:
        if os.path.exists(input_batchfiles_folder):
            for item in os.listdir(input_batchfiles_folder):
                item_path = os.path.join(input_batchfiles_folder, item)
                try:
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                except Exception as e:
                    logger.warning(f"Fehler beim Löschen von {item}: {e}")
            logger.info(f"Input-Ordner geleert: {input_batchfiles_folder}")
        else:
            os.makedirs(input_batchfiles_folder, exist_ok=True)
            logger.info(f"Input-Ordner erstellt: {input_batchfiles_folder}")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Löschen des Input-Ordners: {e}")
        return False

# ============================================================
# PHASE 1: API
# ============================================================

def get_api_response(category_id, offset, retry_count=0, max_retries=3):
    try:
        token = get_dam_token(config)
        api_url = (
            f"{base_api_url}?include_subcategories=true&limit=250"
            f"&category_id={category_id}&include_meta=true&requestkey=*&offset={offset}"
        )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.get(api_url, headers=headers, timeout=config['API_REQUEST_TIMEOUT'])
        if response.status_code == 401:
            invalidate_dam_token()
            return get_api_response(category_id, offset, retry_count, max_retries)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        if retry_count < max_retries:
            retry_count += 1
            logger.warning(f"API-Timeout für Category {category_id}, Offset {offset} - Versuche {retry_count}/{max_retries}...")
            time.sleep(2 ** retry_count)  # Exponential backoff: 2s, 4s, 8s
            return get_api_response(category_id, offset, retry_count, max_retries)
        else:
            logger.error(f"API-Timeout für Category {category_id}, Offset {offset} - Alle {max_retries} Versuche fehlgeschlagen")
            raise
    except requests.exceptions.RequestException as e:
        logger.error(f"API-Fehler für Category {category_id}: {e}")
        raise

def download_all_assets(category_id):
    all_data = []
    offset = 0
    max_iterations = 1000  # Schutz vor unbegrenztem Loop
    iterations = 0

    try:
        while iterations < max_iterations:
            iterations += 1
            logger.info(f"API-Abfrage: Offset {offset}")
            data = get_api_response(category_id, offset)

            if isinstance(data, dict) and 'assets' in data:
                assets = data['assets']
            elif isinstance(data, list):
                assets = data
            else:
                logger.warning(f"Unerwartetes API-Format, breche ab")
                break

            if not assets:
                logger.info(f"Keine weiteren Assets gefunden (Offset {offset})")
                break

            all_data.extend(assets)
            offset += 250
            time.sleep(config['API_REQUEST_DELAY'])

        logger.info(f"Insgesamt {len(all_data)} Assets geladen")
        return all_data
    except Exception as e:
        logger.error(f"Fehler beim Asset-Download: {e}")
        return all_data if all_data else None

def save_response(data):
    try:
        os.makedirs(paths['json'], exist_ok=True)
        output_path = os.path.join(paths['json'], "DAM-Request-Download.json")
        with open(output_path, "w") as file:
            json.dump(data, file, indent=4)
        logger.info(f"API-Antwort gespeichert: {output_path}")
        return True
    except IOError as e:
        logger.error(f"Fehler beim Speichern: {e}")
        return False

# ============================================================
# PHASE 2: BILDER HERUNTERLADEN
# ============================================================

# Nur TIF/TIFF werden lokal zu JPG konvertiert – PNG/JPG/WEBP/BMP bleiben unveraendert
CONVERT_TO_JPG = {"tif", "tiff"}
JPG_QUALITY = 95  # Hohe Qualität – visuell verlustfrei
DOWNLOAD_CONCURRENCY = 8  # Parallele Downloads


def _download_single_asset(asset, retry_count=0, max_retries=2):
    """Download + optional JPG-Konvertierung eines einzelnen Assets.
    Wird parallel ausgeführt. Return: (status, filename_or_error)"""
    try:
        if asset.get('fileType') != "Picture":
            return ('skip', None)

        request_keys = [p.get('requestKey') for p in asset.get('products', [])]
        unique_id = f"#{asset.get('uniqueId')}#"
        file_ext = asset.get('fileExt', '').lower()

        if not file_ext:
            return ('skip', f"Kein Dateityp: {asset.get('title')}")

        links = asset.get('links', [])
        if not links:
            return ('skip', f"Keine Links: {asset.get('title')}")

        image_url = links[0].get('location') + "?format=source&strip=no&quality=100"
        try:
            response = requests.get(image_url, timeout=config['API_REQUEST_TIMEOUT'])
            response.raise_for_status()
        except requests.exceptions.Timeout:
            if retry_count < max_retries:
                retry_count += 1
                time.sleep(1)  # Kurze Pause vor Retry
                return _download_single_asset(asset, retry_count, max_retries)
            else:
                return ('error', f"{asset.get('title', '?')}: Timeout nach {max_retries} Versuchen")

        subfolder = None
        for category in asset.get('categories', []):
            if category.get('id') in CATEGORY_ID_TO_SUBFOLDER:
                subfolder = CATEGORY_ID_TO_SUBFOLDER[category.get('id')]
                break

        subfolder_path = os.path.join(input_batchfiles_folder, subfolder) if subfolder else input_batchfiles_folder
        os.makedirs(subfolder_path, exist_ok=True)

        request_keys_str = "_".join(request_keys) if request_keys else "no_keys"

        if file_ext in CONVERT_TO_JPG:
            filename = f"{request_keys_str}_{unique_id}.jpg"
            save_path = os.path.join(subfolder_path, filename)
            img = Image.open(io.BytesIO(response.content)).convert("RGB")
            img.save(save_path, format="JPEG", quality=JPG_QUALITY)
            return ('ok', f"{file_ext.upper()} → JPG: {filename}")
        else:
            filename = f"{request_keys_str}_{unique_id}.{file_ext}"
            save_path = os.path.join(subfolder_path, filename)
            with open(save_path, 'wb') as img_file:
                img_file.write(response.content)
            return ('ok', filename)
    except Exception as e:
        return ('error', f"{asset.get('title', '?')}: {e}")


def download_images_from_json():
    try:
        json_file = os.path.join(paths['json'], "DAM-Request-Download.json")
        validate_file_exists(json_file, "DAM-JSON")

        with open(json_file, 'r') as f:
            data = json.load(f)

        downloaded_count = 0
        skipped_count = 0

        # PARALLEL Download – 8 gleichzeitige Worker
        with ThreadPoolExecutor(max_workers=DOWNLOAD_CONCURRENCY) as executor:
            futures = {executor.submit(_download_single_asset, asset): asset for asset in data}
            for future in futures:
                try:
                    status, msg = future.result()
                    if status == 'ok':
                        downloaded_count += 1
                        if msg:
                            logger.info(msg)
                    elif status == 'error':
                        skipped_count += 1
                        if msg:
                            logger.warning(msg)
                    # 'skip' still counts as skipped but no increment needed for "Picture"-filtered
                except Exception as e:
                    logger.warning(f"Worker-Fehler: {e}")
                    skipped_count += 1

        logger.info(f"Download: {downloaded_count} erfolgreich, {skipped_count} übersprungen (parallel: {DOWNLOAD_CONCURRENCY})")
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

        logger.info(f"ML-Klassifikation: {skipped_count} übersprungen")
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

def request_category_id():
    # App übergibt Wert via POSTPRO_INPUT (kein Dialog nötig)
    env_val = os.environ.get("POSTPRO_INPUT", "").strip()
    if env_val:
        return env_val
    # Fallback: nativer macOS Dialog
    result = subprocess.run(
        ['osascript', '-e',
         'text returned of (display dialog "Bitte Category ID eingeben:" '
         'default answer "" with title "📥 Kategorie-basierter Import")'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("="*70)
        logger.info("02 📥 Kategorie-basierter DAM Import gestartet")
        logger.info("="*70)

        # Phase 0: Input-Ordner leeren (immer, vor dem Import)
        if not clear_input_batchfiles():
            logger.warning("Input-Ordner konnte nicht geleert werden")

        # Input
        category_id = request_category_id()
        if not category_id:
            logger.info("Import abgebrochen")
            sys.exit(0)

        try:
            category_id = int(category_id)
        except ValueError:
            logger.error(f"Ungültige Category ID: {category_id}")
            sys.exit(1)

        # Phase 1-2: API-Daten holen (Token wird intern via get_dam_token geholt)
        all_data = download_all_assets(category_id)
        if not all_data:
            logger.error("Keine Assets vom DAM geholt")
            sys.exit(1)

        if not save_response(all_data):
            logger.error("Assets konnten nicht gespeichert werden")
            sys.exit(1)

        # Phase 2: Bilder herunterladen
        if not download_images_from_json():
            logger.warning("Download mit Fehlern abgeschlossen")

        # Phase 3: Nach Keywords verschieben
        move_files_by_keywords(input_batchfiles_folder, logger, config['ASYNC_TASK_CONCURRENCY'])

        # Phase 4: ML-Klassifikation
        run_ml_classification()

        # Phase 5: Lightroom Sync wird jetzt über UI ausgelöst (kein osascript Popup mehr)
        print("##LIGHTROOM_READY##", flush=True)

        logger.info("="*70)
        logger.info("✅ Import erfolgreich abgeschlossen")
        logger.info("="*70)

    except KeyboardInterrupt:
        logger.info("Import durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
