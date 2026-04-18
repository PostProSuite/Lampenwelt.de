"""
04 ✅ Ticket abschließen
- Organisiert Bilder nach Kategorien
- Erstellt Web-Ordner und optimiert für Web
- Generiert Excel-Report mit Bildmetadaten
- Aktualisiert Jira-Ticket mit Bildcount
- Kopiert Web-Bilder zu 10-Upload
"""

import os
import sys
import shutil
import re
import logging
import datetime
import subprocess
import imagehash
import hashlib
from concurrent.futures import ThreadPoolExecutor
from openpyxl import Workbook
from PIL import Image
from PIL.ExifTags import TAGS

# Import utilities
sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths, validate_directory_exists,
    validate_numeric_input, validate_input_not_empty
)

try:
    from jira import JIRA
except ImportError:
    logger = logging.getLogger()
    logger.error("jira package nicht installiert. Bitte: pip install jira")
    sys.exit(1)

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
base_folder_path = paths['base']
bilds_path = os.path.join(base_folder_path, "09-Bildsammlung")
webcheck_path = os.path.join(base_folder_path, "05-web-check")
webmood1_path = os.path.join(webcheck_path, "A00-Mood")
webmood2_path = os.path.join(webcheck_path, "00-Standard-1")
neutralweb_path = os.path.join(webcheck_path, "B-Neutral")
web_pathfinal = os.path.join(base_folder_path, "08-FINAL-Images", "web")
moveexcel_folder_path = os.path.join(base_folder_path, "02-excel-input")
upload_folder_path = paths['upload']  # 10-Upload

# Jira Configuration
jira_server = config['JIRA_SERVER']
jira_email = config['JIRA_EMAIL']
jira_token = config['JIRA_API_TOKEN']

# Regex pattern for SKU extraction
pattern = re.compile(r'\d{7,8}')

# Image classification keywords
freisteller_keywords = ["Freisteller", "Clipping"]
ambiente_keywords = ["Mood", "Ambiente"]
dimensions_keywords = ["Dimensions"]
graphics_keywords = ["Graphics"]
details_keywords = ["Detail"]
technical_keywords = ["Technical", "Accesories", "Accessories"]
neutral_keywords = ["Neutral"]

# ============================================================
# PHASE 1: DELETE HIDDEN FILES
# ============================================================

def delete_hidden_files(folder_path):
    """Delete hidden files and folders starting with '.'"""
    try:
        if not os.path.exists(folder_path):
            logger.info(f"Ordner existiert nicht: {folder_path}")
            return True

        deleted_count = 0
        for root, dirs, files in os.walk(folder_path):
            # Delete hidden files
            for file in files:
                if file.startswith('.'):
                    try:
                        file_path = os.path.join(root, file)
                        os.remove(file_path)
                        logger.info(f"Versteckte Datei gelöscht: {file_path}")
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Fehler beim Löschen von {file}: {e}")

            # Delete hidden directories
            for dir in dirs:
                if dir.startswith('.'):
                    try:
                        dir_path = os.path.join(root, dir)
                        shutil.rmtree(dir_path)
                        logger.info(f"Versteckten Ordner gelöscht: {dir_path}")
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Fehler beim Löschen von {dir}: {e}")

        logger.info(f"Phase 1: {deleted_count} versteckte Objekte gelöscht")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Löschen versteckter Dateien: {e}")
        return False

# ============================================================
# PHASE 2: RENAME WEB FILES & ORGANIZE
# ============================================================

def rename_web_files():
    """Rename Mood files to A00 and organize web structure"""
    try:
        # Ensure directories exist
        os.makedirs(webmood1_path, exist_ok=True)
        os.makedirs(webmood2_path, exist_ok=True)
        os.makedirs(neutralweb_path, exist_ok=True)

        # Rename Mood Startimages to A00
        mood_rename_count = 0
        if os.path.exists(webmood1_path):
            for filename in os.listdir(webmood1_path):
                if filename.endswith(('.jpg', '.png')):
                    try:
                        prefix = filename.split('_')[0]
                        new_filename = prefix + '_A00' + filename[-4:]
                        old_path = os.path.join(webmood1_path, filename)
                        new_path = os.path.join(webmood1_path, new_filename)
                        os.rename(old_path, new_path)
                        shutil.move(new_path, os.path.join(webmood2_path, new_filename))
                        logger.info(f"Mood-Datei umbenannt: {filename} → {new_filename}")
                        mood_rename_count += 1
                    except Exception as e:
                        logger.warning(f"Fehler beim Umbenennen von {filename}: {e}")

            # Delete empty mood folder
            try:
                os.rmdir(webmood1_path)
                logger.info("Leerer A00-Mood Ordner gelöscht")
            except OSError:
                pass

        logger.info(f"Phase 2a: {mood_rename_count} Mood-Dateien umbenannt")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Web-Datei-Umbenennen: {e}")
        return False

def rename_standard_files(folder_path, start_suffix=1):
    """Rename files with numeric suffixes"""
    try:
        rename_count = 0
        previous_number = None
        k = start_suffix

        for subdir, dirs, files in os.walk(folder_path):
            files = sorted(files)
            for file in files:
                try:
                    file_path = os.path.join(subdir, file)
                    file_name, file_ext = os.path.splitext(file)
                    match = re.search("^([0-9]{7,8})_(.*)", file_name)

                    if match:
                        current_number = match.group(1)
                        if current_number != previous_number:
                            k = start_suffix

                        new_file_name = f"{match.group(1)}_{k}{file_ext}"
                        new_file_path = os.path.join(subdir, new_file_name)

                        while os.path.exists(new_file_path):
                            k += 1
                            new_file_name = f"{match.group(1)}_{k}{file_ext}"
                            new_file_path = os.path.join(subdir, new_file_name)

                        os.rename(file_path, new_file_path)
                        logger.info(f"Datei umbenannt: {file} → {new_file_name}")
                        previous_number = current_number
                        rename_count += 1
                except Exception as e:
                    logger.warning(f"Fehler beim Umbenennen von {file}: {e}")

        return rename_count
    except Exception as e:
        logger.error(f"Fehler beim Standard-Umbenennen: {e}")
        return 0

# ============================================================
# PHASE 3: ORGANIZE FOLDERS
# ============================================================

def move_file_by_keywords(file_path):
    """Move file to appropriate folder based on keywords"""
    try:
        result = subprocess.run(["exiftool", "-keywords", file_path],
                              capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None

        keywords_str = result.stdout.strip().split(": ")
        keywords = keywords_str[1].split(", ") if len(keywords_str) > 1 else []

        # Determine destination folder based on keywords
        destination_dir_name = None
        if any(kw.lower() in [k.lower() for k in keywords] for kw in freisteller_keywords):
            destination_dir_name = "Freisteller"
        elif any(kw.lower() in [k.lower() for k in keywords] for kw in ambiente_keywords):
            destination_dir_name = "Ambiente"
        elif any(kw.lower() in [k.lower() for k in keywords] for kw in graphics_keywords):
            destination_dir_name = "Graphics"
        elif any(kw.lower() in [k.lower() for k in keywords] for kw in dimensions_keywords):
            destination_dir_name = "Dimensions"
        elif not any(kw.lower() in [k.lower() for k in keywords] for kw in ["Clipping", "Mood", "Graphics"]):
            destination_dir_name = "Neutral"

        if destination_dir_name:
            file_dir = os.path.dirname(file_path)
            destination_dir = os.path.join(file_dir, destination_dir_name)
            os.makedirs(destination_dir, exist_ok=True)
            os.rename(file_path, os.path.join(destination_dir, os.path.basename(file_path)))
            return destination_dir_name
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"Exiftool Timeout für {file_path}")
        return None
    except Exception as e:
        logger.warning(f"Fehler beim Verschieben von {file_path}: {e}")
        return None

def organize_folders():
    """Organize files by keywords into categorized folders"""
    try:
        logger.info("Organisiere Ordner nach Keywords...")

        with ThreadPoolExecutor(max_workers=config['ASYNC_TASK_CONCURRENCY']) as executor:
            futures = []
            for root, dirs, files in os.walk(webcheck_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    if os.path.isfile(file_path):
                        futures.append(executor.submit(move_file_by_keywords, file_path))

            moved_count = sum(1 for future in futures if future.result() is not None)

        executor.shutdown(wait=True)

        # Remove empty directories
        for root, dirs, files in os.walk(webcheck_path):
            for directory in dirs:
                full_dir_path = os.path.join(root, directory)
                try:
                    if os.path.isdir(full_dir_path) and not os.listdir(full_dir_path):
                        os.rmdir(full_dir_path)
                except OSError:
                    pass

        logger.info(f"Phase 3: {moved_count} Dateien organisiert")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Organisieren von Ordnern: {e}")
        return False

# ============================================================
# PHASE 4: MOVE TO FINAL & EXCEL EXPORT
# ============================================================

def consolidate_to_final():
    """Move all web files to final folder"""
    try:
        os.makedirs(web_pathfinal, exist_ok=True)

        for item in os.listdir(webcheck_path):
            item_path = os.path.join(webcheck_path, item)
            if os.path.isdir(item_path) or os.path.isfile(item_path):
                try:
                    dest_path = os.path.join(web_pathfinal, item)
                    if os.path.exists(dest_path):
                        if os.path.isdir(dest_path):
                            shutil.rmtree(dest_path)
                        else:
                            os.remove(dest_path)
                    shutil.move(item_path, dest_path)
                except Exception as e:
                    logger.warning(f"Fehler beim Verschieben von {item}: {e}")

        # Move Excel files
        os.makedirs(web_pathfinal, exist_ok=True)
        if os.path.exists(moveexcel_folder_path):
            for item in os.listdir(moveexcel_folder_path):
                item_path = os.path.join(moveexcel_folder_path, item)
                try:
                    dest_path = os.path.join(web_pathfinal, item)
                    if os.path.exists(dest_path):
                        if os.path.isdir(dest_path):
                            shutil.rmtree(dest_path)
                        else:
                            os.remove(dest_path)
                    shutil.move(item_path, dest_path)
                except Exception as e:
                    logger.warning(f"Fehler beim Verschieben von Excel-Datei {item}: {e}")

        # Remove empty directories
        for root, dirs, files in os.walk(web_pathfinal):
            for directory in dirs:
                try:
                    full_dir_path = os.path.join(root, directory)
                    if not os.listdir(full_dir_path):
                        os.rmdir(full_dir_path)
                except OSError:
                    pass

        logger.info("Phase 4: Dateien in finalen Ordner konsolidiert")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Konsolidieren: {e}")
        return False

# ============================================================
# PHASE 5: IMAGE METADATA EXTRACTION
# ============================================================

def get_image_phash(image_path):
    """Get perceptual hash of image"""
    try:
        with Image.open(image_path) as image:
            return str(imagehash.phash(image, hash_size=64))
    except Exception as e:
        logger.warning(f"Fehler beim pHash für {image_path}: {e}")
        return ""

def get_image_averagehash(image_path):
    """Get average hash of image"""
    try:
        with Image.open(image_path) as image:
            return str(imagehash.average_hash(image))
    except Exception as e:
        logger.warning(f"Fehler beim averageHash für {image_path}: {e}")
        return ""

def get_md5_hash(file_path):
    """Get MD5 hash of file"""
    try:
        hash_md5 = hashlib.md5()
        with open(file_path, 'rb') as file:
            for chunk in iter(lambda: file.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logger.warning(f"Fehler beim MD5 für {file_path}: {e}")
        return ""

def get_keywords(image_path):
    """Extract keywords from image metadata"""
    try:
        result = subprocess.run(["exiftool", "-keywords", image_path],
                              capture_output=True, text=True, timeout=5)
        keywords_str = result.stdout.strip().split(": ")
        if len(keywords_str) > 1:
            return keywords_str[1].split(", ")
        return []
    except Exception as e:
        logger.warning(f"Fehler beim Keyword-Auslesen für {image_path}: {e}")
        return []

def get_image_class(keywords):
    """Determine image class based on keywords"""
    if any(kw.lower() in [k.lower() for k in keywords] for kw in freisteller_keywords):
        return "B20-Clipping"
    elif all(kw.lower() in [k.lower() for k in keywords] for kw in ambiente_keywords):
        return "A10-Mood"
    elif any(kw.lower() in [k.lower() for k in keywords] for kw in dimensions_keywords):
        return "B30-Dimensions"
    elif any(kw.lower() in [k.lower() for k in keywords] for kw in graphics_keywords):
        return "F-Graphics"
    elif any(kw.lower() in [k.lower() for k in keywords] for kw in details_keywords):
        return "C-Detail"
    elif any(kw.lower() in [k.lower() for k in keywords] for kw in technical_keywords):
        return "D-Technical"
    else:
        return ""

def create_excel_report(folder_path, ticket_key):
    """Create Excel report with image metadata"""
    try:
        wb = Workbook()
        ws = wb.active
        ws.append(['Dateiname', 'SKU', 'pHash', 'averageHash', 'md5 Hash', 'Aufnahmedatum',
                   'Erstellungsdatum', 'Last Modified', 'Bildklasse', 'Schlagworte', 'Ticketnummer',
                   'Anzahl Fotos je Artikel', 'Anzahl Artikel', 'Anzahl Fotos gesamt',
                   'Auflösung', 'Pixelhöhe', 'Pixelbreite'])

        num_photos_per_item = {}
        num_photos_total = 0

        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff')):
                    try:
                        file_path = os.path.join(root, file)
                        sku_match = pattern.search(file)
                        sku = int(sku_match.group(0)) if sku_match else None

                        if sku:
                            num_photos_per_item[sku] = num_photos_per_item.get(sku, 0) + 1
                        num_photos_total += 1

                        # Extract metadata
                        keywords = get_keywords(file_path)
                        image_class = get_image_class(keywords)

                        # Get image dimensions
                        resolution = ""
                        pixel_height = ""
                        pixel_width = ""
                        if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                            try:
                                with Image.open(file_path) as image:
                                    pixel_width, pixel_height = image.size
                                    resolution = round((pixel_width * pixel_height) / 1000000, 2)
                            except Exception as e:
                                logger.warning(f"Fehler bei Bildabmessungen für {file}: {e}")

                        # Get EXIF date
                        capture_date = ""
                        if file.lower().endswith(('.jpg', '.jpeg')):
                            try:
                                with Image.open(file_path) as image:
                                    exif_data = image._getexif()
                                    if exif_data:
                                        for tag_id, value in exif_data.items():
                                            tag_name = TAGS.get(tag_id, tag_id)
                                            if tag_name == 'DateTimeOriginal':
                                                capture_date = value
                                                break
                            except Exception:
                                pass

                        creation_date = datetime.datetime.fromtimestamp(
                            os.path.getctime(file_path)).strftime('%Y-%m-%d %H:%M:%S')
                        modified_date = datetime.datetime.fromtimestamp(
                            os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M:%S')

                        ws.append([
                            file, sku, get_image_phash(file_path), get_image_averagehash(file_path),
                            get_md5_hash(file_path), capture_date, creation_date, modified_date,
                            image_class, ', '.join(keywords), ticket_key,
                            num_photos_per_item.get(sku, 0), len(num_photos_per_item), num_photos_total,
                            resolution, pixel_height, pixel_width
                        ])
                    except Exception as e:
                        logger.warning(f"Fehler bei Metadaten für {file}: {e}")

        # Format SKU column
        for cell in ws['B'][1:]:
            cell.number_format = '0'

        # Create Excel Exports folder if not exists
        excel_exports_folder = os.path.join(base_folder_path, "Excel Exports")
        os.makedirs(excel_exports_folder, exist_ok=True)

        excel_path = os.path.join(excel_exports_folder, f"{ticket_key}.xlsx")
        wb.save(excel_path)
        logger.info(f"Excel-Report erstellt: {excel_path}")
        return num_photos_total, len(num_photos_per_item)
    except Exception as e:
        logger.error(f"Fehler beim Excel-Report: {e}")
        return 0, 0

# ============================================================
# PHASE 6: JIRA UPDATE
# ============================================================

def update_jira_ticket(ticket_key, jpg_count, unique_count):
    """Update Jira ticket with image counts, comment, assignee and transition"""
    try:
        jira_options = {'server': jira_server}
        jira = JIRA(options=jira_options, basic_auth=(jira_email, jira_token))

        issue = jira.issue(ticket_key)

        # Autor (Reporter) des Tickets auslesen
        reporter = issue.fields.reporter
        reporter_account_id = reporter.accountId
        reporter_display_name = reporter.displayName

        # Felder aktualisieren + Autor zuweisen
        issue.update(fields={
            'customfield_10303': jpg_count,
            'customfield_10299': unique_count,
            'assignee': {'accountId': reporter_account_id}
        })
        logger.info(f"Ticket {ticket_key} dem Autor zugewiesen: {reporter_display_name}")

        # Kommentar mit @Mention posten
        comment_body = f"[~accountid:{reporter_account_id}] Upload Done 🚀"
        jira.add_comment(ticket_key, comment_body)
        logger.info(f"Kommentar gepostet: {comment_body}")

        # Workflow-Transition zu "Genehmigung"
        transitions = jira.transitions(ticket_key)
        transition_id = None
        for t in transitions:
            if "genehmigung" in t['name'].lower() or "approval" in t['name'].lower() or "qa" in t['name'].lower():
                transition_id = t['id']
                logger.info(f"Transition gefunden: {t['name']} (ID: {t['id']})")
                break

        if transition_id:
            jira.transition_issue(ticket_key, transition_id)
            logger.info(f"Ticket {ticket_key} auf 'Genehmigung' gesetzt")
        else:
            # Alle verfügbaren Transitions loggen damit wir den Namen sehen
            available = [t['name'] for t in transitions]
            logger.warning(f"Transition 'Genehmigung' nicht gefunden. Verfügbar: {available}")

        logger.info(f"Jira-Ticket {ticket_key} aktualisiert: {jpg_count} Bilder, {unique_count} Artikel")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Jira-Update: {e}")
        return False

# ============================================================
# COPY WEB IMAGES TO UPLOAD FOLDER
# ============================================================

def copy_web_images_to_upload(source_folder, destination_folder):
    """Copy web images from 08-FINAL-Images/web to 10-Upload"""
    try:
        if not os.path.exists(source_folder):
            logger.warning(f"Web-Ordner nicht gefunden: {source_folder}")
            return False

        # Erstelle Upload-Ordner wenn nötig
        os.makedirs(destination_folder, exist_ok=True)

        copied_count = 0
        for root, dirs, files in os.walk(source_folder):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    source_path = os.path.join(root, file)
                    dest_path = os.path.join(destination_folder, file)

                    try:
                        shutil.copy2(source_path, dest_path)
                        copied_count += 1
                        logger.info(f"Web-Bild kopiert: {file} → 10-Upload")
                    except Exception as e:
                        logger.warning(f"Fehler beim Kopieren von {file}: {e}")

        if copied_count > 0:
            logger.info(f"✅ {copied_count} Web-Bilder zu 10-Upload kopiert")
            return True
        else:
            logger.warning("Keine Web-Bilder zum Kopieren gefunden")
            return False

    except Exception as e:
        logger.error(f"Fehler beim Kopieren der Web-Bilder: {e}")
        return False

# ============================================================
# LIGHTROOM SYNC
# ============================================================

def sync_lightroom_folder():
    try:
        logger.info("Öffne Lightroom und synchronisiere Ordner...")
        applescript = '''
            tell application "Adobe Lightroom Classic"
                activate
            end tell
            delay 6
            tell application "System Events"
                tell process "Adobe Lightroom Classic"
                    try
                        click menu item "Ordner synchronisieren..." of menu "Bibliothek" of menu bar 1
                        delay 2
                        click button "Synchronisieren" of sheet 1 of window 1
                        log "Lightroom Sync erfolgreich."
                    on error errMsg
                        log "Lightroom Sync Fehler: " & errMsg
                    end try
                end tell
            end tell
        '''
        result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info("Lightroom Sync gestartet")
            return True
        else:
            logger.warning(f"Lightroom Sync Fehler: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Lightroom Sync Timeout")
        return False
    except Exception as e:
        logger.error(f"Fehler beim Lightroom Sync: {e}")
        return False

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("="*70)
        logger.info("04 ✅ Ticket abschließen gestartet")
        logger.info("="*70)

        # Get ticket number - App übergibt via POSTPRO_INPUT, sonst Dialog
        ticket_number = os.environ.get("POSTPRO_INPUT", "").strip()
        if ticket_number:
            # App kann "CREAMEDIA-1234" oder nur "1234" übergeben
            if "-" in ticket_number:
                ticket_number = ticket_number.split("-")[-1]
        else:
            while True:
                result = subprocess.run(
                    ['osascript', '-e',
                     'text returned of (display dialog "Ticketnummer eingeben:" '
                     'default answer "" with title "✅ Ticket abschließen")'],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    logger.info("Skript durch Benutzer abgebrochen")
                    sys.exit(0)
                ticket_number = result.stdout.strip()
                if ticket_number.isdigit():
                    break
                else:
                    subprocess.run(
                        ['osascript', '-e',
                         'display alert "Ungültige Eingabe" message "Die Ticketnummer darf nur Zahlen enthalten." as critical'],
                        capture_output=True
                    )

        ticket_key = f"CREAMEDIA-{ticket_number}"

        # Phase 1: Delete hidden files
        delete_hidden_files(webcheck_path)

        # Phase 2: Rename and organize web files
        rename_web_files()
        rename_web_count = rename_standard_files(webmood2_path, 1)
        rename_neutral_count = rename_standard_files(neutralweb_path, 4)
        logger.info(f"Umbenannt: {rename_web_count + rename_neutral_count} Dateien")

        # Phase 3: Organize by keywords
        organize_folders()

        # Phase 4: Move to final
        consolidate_to_final()

        # Rename final folder to ticket key
        if os.path.exists(web_pathfinal):
            new_folder_path = os.path.join(base_folder_path, "08-FINAL-Images", ticket_key)
            try:
                if os.path.exists(new_folder_path):
                    shutil.rmtree(new_folder_path)
                os.rename(web_pathfinal, new_folder_path)
                web_pathfinal = new_folder_path
                logger.info(f"Ordner umbenannt zu: {ticket_key}")
            except Exception as e:
                logger.warning(f"Fehler beim Umbenennen des Ordners: {e}")

        # Phase 5: Create Excel report
        jpg_count, unique_count = create_excel_report(web_pathfinal, ticket_key)

        # Phase 6: Update Jira (automatisch, bei Fehler Popup)
        if jira_token and jira_email:
            jira_ok = update_jira_ticket(ticket_key, jpg_count, unique_count)
            if not jira_ok:
                subprocess.run(
                    ['osascript', '-e',
                     f'display alert "Jira-Update fehlgeschlagen" '
                     f'message "Das Ticket {ticket_key} konnte nicht aktualisiert werden.\\n\\nBitte manuell in Jira prüfen." '
                     f'as critical'],
                    capture_output=True
                )
        else:
            logger.warning("Jira-Credentials nicht konfiguriert - überspringe Jira-Update")

        # Phase 7: Copy web images to upload folder
        logger.info("Starte Phase 7: Web-Bilder zu 10-Upload kopieren...")
        if copy_web_images_to_upload(web_pathfinal, upload_folder_path):
            logger.info("✅ Phase 7: Web-Bilder erfolgreich kopiert")
        else:
            logger.warning("⚠️ Phase 7: Web-Bilder konnten nicht kopiert werden")

        # Phase 8: Save ticket key for upload script
        logger.info("Starte Phase 8: Speichere Ticket-Key für Upload...")
        try:
            ticket_key_file = os.path.join(upload_folder_path, ".ticket_key")
            with open(ticket_key_file, 'w') as f:
                f.write(ticket_key)
            logger.info(f"✅ Phase 8: Ticket-Key gespeichert: {ticket_key}")
        except Exception as e:
            logger.warning(f"⚠️ Phase 8: Fehler beim Speichern des Ticket-Keys: {e}")

        logger.info("="*70)
        logger.info("✅ Ticket abschließen erfolgreich abgeschlossen")
        logger.info("="*70)

    except KeyboardInterrupt:
        logger.info("Finalisierung durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
