"""
02 🏷️ Klassifizieren & Metadaten
- Benennt Dateien nach SKU und Position
- Klassifiziert Bilder per Exiftool-Keywords
- Sendet Klassifikation an DAM-API
- Aktualisiert Titel im DAM
"""

import os
import re
import sys
import json
import asyncio
import aiohttp
import logging
import time
import subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    get_dam_token, invalidate_dam_token,
    sync_lightroom,
    validate_directory_exists, validate_file_exists
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
directory = paths['input_batchfiles']
json_file_path = os.path.join(paths['json'], "DAM-Request-Download.json")

base_url = "https://api-rs.mycliplister.com/v2.2/apis"

# Kategorie-Mapping für DAM
category_mapping = {
    "C-Detail": "408721",
    "A10-Mood": "408719",
    "B20-Clipping": "408735",
    "D-Technical": "408722",
    "B30-Dimensions": "408736",
    "B40-Neutral": "408720",
    "C50-Shade": "408753",
    "C60-Material": "408752",
    "C70-Switch": "408751",
    "C80-Base_Stand": "408750",
    "C90-Cable": "408749",
    "C95-Split": "408747",
    "D100-Lightsource_Socket": "",
    "D110-Remote": "408756",
    "D120-Accesories": "408755",
    "E130-Graphics": "408723",
    "F-Graphics": "408723",
    "E130-Graphics_DE": "408778",
    "E130-Graphics_INT": "408777",
    "E130-Graphics_ENG": "408776",
    "F-Group": "408762",
    "F140-Group": "408762",
    "G-UGC": "408760",
}

# ============================================================
# PHASE 1: DATEIEN UMBENENNEN
# ============================================================

def rename_files(directory):
    """Rename files based on folder prefix and counter"""
    try:
        for subdir, dirs, files in os.walk(directory):
            files.sort()
            folder_name = os.path.basename(subdir)

            # Extract prefix from folder name
            if '-' in folder_name:
                parts = folder_name.split('-')
                prefix = parts[0]
                if len(prefix) == 4:
                    suffix = prefix[:3]
                elif len(prefix) == 3:
                    suffix = prefix[:2]
                else:
                    suffix = prefix
            else:
                suffix = ''

            counters = {}
            for file in files:
                try:
                    file_path = os.path.join(subdir, file)
                    if not os.path.isfile(file_path):
                        continue

                    file_name, file_ext = os.path.splitext(file)

                    # Skip if already renamed with suffix (e.g., 8505786_E130.jpg)
                    if re.match(r'^\d{7,8}_[A-Z]\d+$', file_name):
                        logger.info(f"Übersprungen (bereits kategorisiert): {file}")
                        continue

                    id_match = re.search("#(.+?)#", file_name)
                    id_part = id_match.group(0) if id_match else ''

                    # Extract ONLY the SKU number (first 7-8 digits)
                    sku_match = re.search(r'^\d{7,8}', file_name)
                    base_part = sku_match.group(0) if sku_match else file_name.split('#')[0]

                    if base_part not in counters:
                        counters[base_part] = 0

                    base_part_clean = base_part.strip('_').strip()
                    suffix_counter = f"{suffix}{counters[base_part]}" if suffix or counters[base_part] else ''
                    id_part_clean = id_part.strip('_').strip()

                    components = [c for c in [base_part_clean, suffix_counter, id_part_clean] if c]
                    new_file_name_base = '_'.join(components).strip('_').strip()
                    new_file_name_base = re.sub(r'_#', '#', new_file_name_base)
                    new_file_name = new_file_name_base + file_ext
                    new_file_path = os.path.join(subdir, new_file_name)

                    # Only rename if the name actually changed
                    if file != new_file_name:
                        os.rename(file_path, new_file_path)
                        counters[base_part] += 1
                        logger.info(f"Umbenannt: {file} → {new_file_name}")
                except Exception as e:
                    logger.warning(f"Fehler beim Umbenennen von {file}: {e}")
                    continue

        logger.info("Phase 1: Dateien-Umbenennung abgeschlossen")
        return True
    except Exception as e:
        logger.error(f"Fehler bei Dateien-Umbenennung: {e}")
        return False

# ============================================================
# PHASE 2: KLASSIFIKATION AN DAM SENDEN (async)
# ============================================================

async def send_remove_request_async(session, unique_id, category_id):
    """Remove a category from asset in DAM (before setting new one)"""
    try:
        _access_token = get_dam_token(config)
        url = f"https://api-rs.mycliplister.com/v2.2/apis/asset/category/remove?unique_id={unique_id}&category_id={category_id}"
        headers = {'Authorization': f'Bearer {_access_token}', 'Content-Type': 'application/json; charset=utf-8'}
        async with session.put(url, headers=headers, json={},
                              timeout=aiohttp.ClientTimeout(total=config['API_REQUEST_TIMEOUT'])) as response:
            if response.status in [200, 204]:
                logger.info(f"Alte Kategorie {category_id} entfernt für {unique_id}")
    except Exception:
        pass  # Ignore – category may not have been set

async def send_put_request_async(session, unique_id, target_category_id):
    """Remove all other categories, then set the correct one in DAM"""
    try:
        _access_token = get_dam_token(config)

        # Step 1: Remove from all OTHER known categories first (prevents duplicates)
        all_other_ids = [cid for cid in category_mapping.values() if cid and cid != target_category_id]
        remove_tasks = [send_remove_request_async(session, unique_id, cid) for cid in all_other_ids]
        await asyncio.gather(*remove_tasks, return_exceptions=True)

        # Step 2: Add the correct category
        url = f"https://api-rs.mycliplister.com/v2.2/apis/asset/category/add?unique_id={unique_id}&category_id={target_category_id}"
        headers = {'Authorization': f'Bearer {_access_token}', 'Content-Type': 'application/json; charset=utf-8'}

        async with session.put(url, headers=headers, json={},
                              timeout=aiohttp.ClientTimeout(total=config['API_REQUEST_TIMEOUT'])) as response:
            if response.status in [200, 204]:
                logger.info(f"Kategorie {target_category_id} gesetzt für {unique_id}")
            else:
                content = await response.text()
                logger.warning(f"Fehler für {unique_id}: {response.status} - {content}")
    except asyncio.TimeoutError:
        logger.error(f"Timeout bei PUT-Request für {unique_id}")
    except Exception as e:
        logger.warning(f"Fehler bei PUT-Request für {unique_id}: {e}")

async def process_folder_async(session, folder_name, category_id):
    """Process files in a folder and update their DAM category"""
    try:
        folder_path = os.path.join(directory, folder_name)
        if not os.path.isdir(folder_path):
            return
        files = [f for f in os.listdir(folder_path) if not f.startswith('.')]
        if not files:
            return
        logger.info(f"Verarbeite {len(files)} Dateien in {folder_name} → Kategorie {category_id}...")
        for filename in files:
            parts = filename.split('#')
            if len(parts) > 1:
                unique_id = parts[1]
                await send_put_request_async(session, unique_id, category_id)
                await asyncio.sleep(config['API_REQUEST_DELAY'])
    except Exception as e:
        logger.warning(f"Fehler bei Ordner-Verarbeitung {folder_name}: {e}")

async def send_classification_to_dam():
    """Send all classifications to DAM (remove old + set new category per file)"""
    try:
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Sequentiell pro Ordner um Rate-Limits zu vermeiden
            for folder_name, cat_id in category_mapping.items():
                if cat_id:
                    await process_folder_async(session, folder_name, cat_id)
        logger.info("Klassifikation für alle Bilder an DAM übermittelt")
        return True
    except Exception as e:
        logger.error(f"Fehler bei DAM-Klassifikation: {e}")
        return False

# ============================================================
# PHASE 3: TITEL IM DAM AKTUALISIEREN (threaded)
# ============================================================

def get_access_token_sync():
    """Get access token synchronously via shared cache."""
    return get_dam_token(config)

def get_request_keys_from_json(unique_id):
    """Extract request keys from JSON file"""
    try:
        validate_file_exists(json_file_path, "DAM-JSON")
        with open(json_file_path, 'r') as file:
            data = json.load(file)
            for item in data:
                if item.get('uniqueId') == unique_id:
                    if 'products' in item and len(item['products']) > 0:
                        return [product['requestKey'] for product in item['products']]
        return []
    except Exception as e:
        logger.warning(f"Fehler beim Lesen von JSON für {unique_id}: {e}")
        return []

def update_asset(unique_id, new_title, token):
    """Update asset title in DAM"""
    try:
        request_keys = get_request_keys_from_json(unique_id)
        if request_keys:
            update_api_url = f"{base_url}/asset/update?unique_id={unique_id}"
            products_data = [{"title": new_title, "requestKey": rk, "keyType": 100} for rk in request_keys]
            data = {"title": new_title, "products": products_data}
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            response = requests.put(update_api_url, headers=headers, json=data,
                                   timeout=config['API_REQUEST_TIMEOUT'])
            if response.status_code in [200, 204]:
                logger.info(f"Titel für {unique_id} aktualisiert")
            else:
                logger.warning(f"Fehler bei {unique_id}: {response.status_code}")
        else:
            logger.warning(f"Keine requestKeys für {unique_id} gefunden")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout beim Titel-Update für {unique_id}")
    except Exception as e:
        logger.warning(f"Fehler beim Titel-Update für {unique_id}: {e}")

def update_titles_in_dam():
    """Update all asset titles in DAM"""
    try:
        token = get_access_token_sync()

        with ThreadPoolExecutor(max_workers=config['ASYNC_TASK_CONCURRENCY']) as executor:
            futures = []
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.lower().endswith(('.jpg', '.jpeg')):
                        file_path = os.path.join(root, file)
                        if "#" in file:
                            unique_id = file.split("#")[1].split(".")[0]
                            new_title = file.split("#")[0]
                            futures.append(executor.submit(update_asset, unique_id, new_title, token))

            # Wait for all tasks
            for future in futures:
                try:
                    future.result(timeout=30)
                except Exception as e:
                    logger.warning(f"Task-Fehler: {e}")

        executor.shutdown(wait=True)
        logger.info("Alle Titel im DAM aktualisiert")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Titel-Update: {e}")
        return False

# ============================================================
# PHASE 4: UNIQUE IDs AUS DATEINAMEN ENTFERNEN
# ============================================================

def remove_unique_ids():
    """Remove unique IDs from filenames"""
    try:
        removed_count = 0
        for dirpath, dirnames, filenames in os.walk(directory):
            for dateiname in filenames:
                if dateiname.endswith(('.jpg', '.jpeg', '.png', '.webp', '.tif', '.pdf')):
                    start_index = dateiname.find('#')
                    end_index = dateiname.find('#', start_index + 1)
                    if start_index != -1 and end_index != -1:
                        try:
                            neuer_dateiname = dateiname[:start_index] + dateiname[end_index + 1:]
                            aktueller_pfad = os.path.join(dirpath, dateiname)
                            neuer_pfad = os.path.join(dirpath, neuer_dateiname)
                            os.rename(aktueller_pfad, neuer_pfad)
                            logger.info(f"Umbenannt: {dateiname} → {neuer_dateiname}")
                            removed_count += 1
                        except Exception as e:
                            logger.warning(f"Fehler beim Umbenennen von {dateiname}: {e}")

        logger.info(f"Phase 4: {removed_count} Unique IDs entfernt")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Entfernen von Unique IDs: {e}")
        return False

# ============================================================
# PHASE 5: KEYWORDS PER EXIFTOOL HINZUFÜGEN
# ============================================================

def add_keywords_to_file(file_path, keywords):
    """Add keywords to file via exiftool (works for JPG, PNG, TIF)"""
    try:
        import subprocess
        for keyword in keywords:
            try:
                # Use XMP:Subject (works for JPG, PNG, TIF, etc.)
                subprocess.run(
                    ["/opt/homebrew/bin/exiftool", "-overwrite_original", f"-XMP:Subject+={keyword}", file_path],
                    capture_output=True, timeout=10, check=True
                )
            except subprocess.TimeoutExpired:
                logger.warning(f"Exiftool Timeout für {file_path}")
            except subprocess.CalledProcessError as e:
                logger.warning(f"Exiftool Fehler für {file_path}: {e}")
    except Exception as e:
        logger.warning(f"Fehler beim Keyword-Hinzufügen zu {file_path}: {e}")

def get_keywords_for_filename(filename, mappings):
    """Get keywords based on filename"""
    keywords = []
    for key, value in mappings.items():
        if key in filename:
            keywords.extend(value)
    return keywords

def add_keywords_by_folder():
    """Add keywords to all files based on the subfolder they are in"""
    try:
        # Ordnername → Keywords
        folder_keyword_map = {
            "A10-Mood":              ["Mood", "Ambiente"],
            "B20-Clipping":          ["Freisteller", "Clipping"],
            "B30-Dimensions":        ["Dimensions", "Maßbild"],
            "B40-Neutral":           ["Neutral", "Produktbild"],
            "C-Detail":              ["Detail"],
            "C50-Shade":             ["Shade", "Detail", "Schirm"],
            "C60-Material":          ["Material", "Detail"],
            "C70-Switch":            ["Switch", "Detail", "Schalter"],
            "C80-Base_Stand":        ["Base_Stand", "Detail", "Fuss"],
            "C90-Cable":             ["Cable", "Detail", "Kabel"],
            "C95-Split":             ["Split", "Detail"],
            "D-Technical":           ["Technical", "Technisch"],
            "D100-Lightsource_Socket": ["Technical", "Lightsource_Socket", "Sockel"],
            "D110-Remote":           ["Technical", "Remote", "Fernbedienung"],
            "D120-Accesories":       ["Technical", "Accessories", "Zubehoer"],
            "E130-Graphics":         ["Graphics", "Grafik"],
            "E130-Graphics_DE":      ["Graphics", "Grafik", "DE"],
            "E130-Graphics_INT":     ["Graphics", "Grafik", "INT"],
            "E130-Graphics_ENG":     ["Graphics", "Grafik", "ENG"],
            "F-Group":               ["Group", "Gruppe"],
            "F140-Group":            ["Group", "Gruppe"],
            "G-UGC":                 ["UGC", "User_Generated"],
        }

        keyword_count = 0
        image_exts = (".jpg", ".jpeg", ".JPG", ".JPEG", ".png", ".PNG",
                      ".tif", ".tiff", ".TIF", ".TIFF", ".psd")

        for subdir, dirs, files in os.walk(directory):
            folder_name = os.path.basename(subdir)
            keywords = folder_keyword_map.get(folder_name)
            if not keywords:
                continue

            for file in files:
                if file.startswith('.'):
                    continue
                if file.lower().endswith(image_exts):
                    file_path = os.path.join(subdir, file)
                    add_keywords_to_file(file_path, keywords)
                    keyword_count += 1
                    logger.info(f"Folder-Keywords {keywords} → {file}")

        logger.info(f"Phase 5a: Ordner-Keywords zu {keyword_count} Dateien hinzugefügt")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Ordner-Keyword-Hinzufügen: {e}")
        return False

def add_keywords_by_filename():
    """Add keywords to all files based on filename"""
    try:
        mappings = {
            "_A":   ["Ambiente", "Mood"],
            "_B2":  ["Freisteller", "Clipping"],
            "_C":   ["Detail"],
            "_C5":  ["Shade", "Detail"],
            "_C6":  ["Detail", "Material"],
            "_C7":  ["Detail", "Switch"],
            "C8":   ["Detail", "Base_Stand"],
            "_C9":  ["Detail", "Cable"],
            "_D":   ["Technical"],
            "_D10": ["Technical", "Lightsource_Mount"],
            "_D11": ["Technical", "Remote"],
            "_D12": ["Technical", "Accessories"],
            "_E13": ["Graphics"],
            "_B3":  ["Dimensions"],
            "_B4":  ["Neutral"],
        }

        keyword_count = 0
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith((".jpg", ".tif", ".TIF", ".png", ".PNG", ".JPG", ".psd", ".jpeg", ".JPEG")):
                    file_path = os.path.join(root, file)
                    keywords = get_keywords_for_filename(file, mappings)
                    if keywords:
                        add_keywords_to_file(file_path, keywords)
                        keyword_count += 1

        logger.info(f"Phase 5: Keywords zu {keyword_count} Dateien hinzugefügt")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Keyword-Hinzufügen: {e}")
        return False

# ============================================================
# PHASE 6: CLIPPING-CHECK
# ============================================================

def check_clippings(directory):
    """
    Vergleicht alle SKUs im input_batchfiles-Ordner mit dem B20-Clipping-Ordner.
    Gibt fehlende SKUs zurück und schreibt eine maschinenlesbare Zeile nach stdout,
    die die App abfängt und im Result-Rollout anzeigt.
    """
    try:
        image_exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd",
                      ".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF", ".PSD")

        all_skus = set()
        clipping_skus = set()
        clipping_folder = os.path.join(directory, "B20-Clipping")

        # Alle SKUs aus allen Unterordnern sammeln
        for subdir, dirs, files in os.walk(directory):
            for file in files:
                if file.startswith('.'):
                    continue
                if file.lower().endswith(image_exts):
                    sku_match = re.search(r'^(\d{7,8})', file)
                    if sku_match:
                        all_skus.add(sku_match.group(1))

        # SKUs mit vorhandenen Clippings
        if os.path.isdir(clipping_folder):
            for file in os.listdir(clipping_folder):
                if file.startswith('.'):
                    continue
                if file.lower().endswith(image_exts):
                    sku_match = re.search(r'^(\d{7,8})', file)
                    if sku_match:
                        clipping_skus.add(sku_match.group(1))

        missing = all_skus - clipping_skus

        if missing:
            missing_str = ", ".join(sorted(missing))
            logger.warning(f"Clipping-Check: Fehlende SKUs → {missing_str}")
            # Maschinenlesbare Zeile für die App
            print(f"##CLIPPING_CHECK##:missing:{missing_str}", flush=True)
        else:
            if all_skus:
                logger.info("Clipping-Check: Alle SKUs haben Clippings ✓")
                print("##CLIPPING_CHECK##:complete", flush=True)
            else:
                logger.info("Clipping-Check: Keine Bilder gefunden")

        return missing

    except Exception as e:
        logger.error(f"Fehler beim Clipping-Check: {e}")
        return set()


# ============================================================
# LIGHTROOM SYNC
# ============================================================

# Lightroom Sync → aus _utils

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("="*70)
        logger.info("02 🏷️ Klassifizieren & Metadaten gestartet")
        logger.info("="*70)

        # Phase 1: Dateien umbenennen
        if not rename_files(directory):
            logger.warning("Phase 1 mit Fehlern abgeschlossen")

        # Phase 2: Klassifikation an DAM senden
        if asyncio.run(send_classification_to_dam()):
            logger.info("Phase 2 erfolgreich")
        else:
            logger.warning("Phase 2 mit Fehlern abgeschlossen")

        # Phase 3: Titel aktualisieren
        if not update_titles_in_dam():
            logger.warning("Phase 3 mit Fehlern abgeschlossen")

        # Phase 4: Unique IDs entfernen
        if not remove_unique_ids():
            logger.warning("Phase 4 mit Fehlern abgeschlossen")

        # Phase 5a: Keywords per Ordnername in Bilder schreiben
        if not add_keywords_by_folder():
            logger.warning("Phase 5a mit Fehlern abgeschlossen")

        # Phase 5b: Keywords per Dateiname in Bilder schreiben
        if not add_keywords_by_filename():
            logger.warning("Phase 5b mit Fehlern abgeschlossen")

        # Phase 6: Clipping-Check
        check_clippings(directory)

        # Phase 7: Lightroom Sync wird jetzt über UI ausgelöst (kein osascript Popup mehr)
        print("##LIGHTROOM_READY##", flush=True)

        logger.info("="*70)
        logger.info("✅ Klassifizierung erfolgreich abgeschlossen")
        logger.info("="*70)

    except KeyboardInterrupt:
        logger.info("Klassifizierung durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
