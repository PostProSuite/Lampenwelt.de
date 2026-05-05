"""
04 Ticket abschliessen
=====================
Nimmt die bereits klassifizierten Bilder aus 02-Webcheck und:

1) Benennt Dateien fuer Webshop um:  {SKU}_{Position}.ext  (z.B. 8505786_1.jpg)
   WICHTIG: Keywords werden NICHT modifiziert. Lightroom/Image-Classification
   hat die Keywords bereits korrekt gesetzt – die werden 1:1 uebernommen.
2) Erstellt Excel-Report mit Bildmetadaten
3) Aktualisiert Jira-Ticket (Bildcount, Zuweisung an Reporter, Transition QA,
   Kommentar "Upload done 🚀" als aktueller Bearbeiter)
4) Kopiert alle umbenannten Bilder nach 03-Upload (fuer DAM-Upload)
5) Speichert Ticket-Key fuer Upload-Script

KEIN Cleanup mehr — der Workspace wird ausschliesslich beim naechsten
Download-RAW-Lauf bereinigt (server.js cleanupBeforeDownloadRaw).
Bilder bleiben hier liegen zur Inspektion.
"""

import os
import sys
import shutil
import re
import logging
import datetime
import subprocess
import hashlib
from concurrent.futures import ThreadPoolExecutor
from openpyxl import Workbook
from PIL import Image
from PIL.ExifTags import TAGS

sys.path.append(os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths,
    validate_directory_exists
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
workspace          = paths['workspace']
input_path         = paths['input_batchfiles']   # 01-Input RAW files
webcheck_path      = paths['web_check']           # 02-Webcheck
upload_path        = paths['upload']               # 03-Upload
# Excel-Exports IM WORKSPACE - so kann die App sie anzeigen + löschen
excel_exports_path = os.path.join(workspace, 'Exports')
os.makedirs(excel_exports_path, exist_ok=True)

from _utils import find_exiftool
EXIFTOOL = os.environ.get('EXIFTOOL_PATH') or find_exiftool() or '/opt/homebrew/bin/exiftool'

# Jira
jira_server = config['JIRA_SERVER']
jira_email  = config['JIRA_EMAIL']
jira_token  = config['JIRA_API_TOKEN']

# SKU-Regex
pattern = re.compile(r'\d{7,8}')

# Bildformate
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.psd', '.gif', '.webp')

# ── Ordner -> Keywords Mapping ───────────────────────────────
FOLDER_KEYWORD_MAP = {
    # Webcheck-Ordner (02-Webcheck)
    "01-Mainimage":            ["Freisteller", "Clipping"],
    "02-Mood":                 ["Mood", "Ambiente"],
    "03-Pos4-X":               [],
    "A10-Mood":                ["Mood", "Ambiente"],
    "B20-Clipping":            ["Freisteller", "Clipping"],
    "B30-Dimensions":          ["Dimensions"],
    "B40-Neutral":             ["Neutral"],
    "C-Detail":                ["Detail"],
    "C50-Shade":               ["Shade", "Detail"],
    "C60-Material":            ["Material", "Detail"],
    "C70-Switch":              ["Switch", "Detail"],
    "C80-Base_Stand":          ["Base_Stand", "Detail"],
    "C90-Cable":               ["Cable", "Detail"],
    "C95-Split":               ["Split", "Detail"],
    "D-Technical":             ["Technical"],
    "D100-Lightsource_Socket": ["Technical", "Lightsource_Socket"],
    "D110-Remote":             ["Technical", "Remote"],
    "D120-Accesories":         ["Technical", "Accessories"],
    "E130-Graphics":           ["Graphics"],
    "E130-Graphics_DE":        ["Graphics", "DE"],
    "E130-Graphics_INT":       ["Graphics", "INT"],
    "E130-Graphics_ENG":       ["Graphics", "ENG"],
    "F-Group":                 ["Group"],
    "F140-Group":              ["Group"],
    "G-UGC":                   ["UGC"],
}

# ── Ordner -> Datei-Suffix Mapping ───────────────────────────
CATEGORY_SUFFIX_MAP = {
    "A10-Mood":                "A10",
    "B20-Clipping":            "B20",
    "B30-Dimensions":          "B30",
    "B40-Neutral":             "B40",
    "C-Detail":                "C",
    "C50-Shade":               "C50",
    "C60-Material":            "C60",
    "C70-Switch":              "C70",
    "C80-Base_Stand":          "C80",
    "C90-Cable":               "C90",
    "C95-Split":               "C95",
    "D-Technical":             "D",
    "D100-Lightsource_Socket": "D100",
    "D110-Remote":             "D110",
    "D120-Accesories":         "D120",
    "E130-Graphics":           "E130",
    "E130-Graphics_DE":        "E130",
    "E130-Graphics_INT":       "E130",
    "E130-Graphics_ENG":       "E130",
    "F-Group":                 "F140",
    "F140-Group":              "F140",
    "G-UGC":                   "G",
}


# ============================================================
# PHASE 1: RENAME + KEYWORDS
# ============================================================

# Position-Logik:
#   B20-Clipping  →  _1        (Mainimage, max 1 pro SKU)
#   A10-Mood      →  _2, _3   (Mood, max 2 pro SKU)
#   Alles andere  →  _4, _5, _6 … (Pos 4-x, zaehlt pro SKU uebergreifend)

MAINIMAGE_FOLDER = "01-Mainimage"
MOOD_FOLDER      = "02-Mood"
MAINIMAGE_POS    = 1
MOOD_START_POS   = 2
MOOD_MAX         = 2
OTHER_START_POS  = 4


def _do_rename(folder, old_name, new_name):
    """Datei umbenennen. Gibt neuen Pfad zurueck oder None bei Fehler."""
    old_path = os.path.join(folder, old_name)
    new_path = os.path.join(folder, new_name)
    if old_path == new_path:
        return new_path
    # Kollisionsschutz
    if os.path.exists(new_path):
        base, ext = os.path.splitext(new_name)
        c = 2
        while os.path.exists(os.path.join(folder, f"{base}_{c}{ext}")):
            c += 1
        new_path = os.path.join(folder, f"{base}_{c}{ext}")
    try:
        os.rename(old_path, new_path)
        logger.info(f"Umbenannt: {old_name} → {os.path.basename(new_path)}")
        return new_path
    except Exception as e:
        logger.warning(f"Rename-Fehler {old_name}: {e}")
        return None


def _has_existing_keywords(file_path):
    """Prüft ob das Bild bereits Keywords hat (z.B. aus Lightroom-Export)."""
    try:
        result = subprocess.run(
            [EXIFTOOL, '-s', '-s', '-s', '-XMP:Subject', '-IPTC:Keywords', file_path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass
    return False


def _set_keywords(file_path, keywords, force=False):
    """
    Keywords per exiftool in Datei schreiben.

    WICHTIG: Wenn das Bild BEREITS Keywords hat (z.B. aus Lightroom-Export),
    werden die NICHT überschrieben - Lightroom-Keywords sind die Quelle der Wahrheit.

    Mit force=True werden Keywords trotzdem ergänzt.
    """
    if not keywords:
        return

    # Existing keywords aus Lightroom respektieren
    if not force and _has_existing_keywords(file_path):
        logger.debug(f"  Keywords aus Bild übernommen (kein Überschreiben): {os.path.basename(file_path)}")
        return

    try:
        # Schreibe in beide: XMP:Subject UND IPTC:Keywords (für DAM-Kompatibilität)
        cmd = [EXIFTOOL, "-overwrite_original"]
        for kw in keywords:
            cmd.append(f"-XMP:Subject+={kw}")
            cmd.append(f"-IPTC:Keywords+={kw}")
        cmd.append(file_path)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logger.warning(f"Exiftool-Fehler {os.path.basename(file_path)}: {result.stderr.strip()[:200]}")
        else:
            logger.info(f"  Fallback-Keywords gesetzt (Bild hatte keine): {keywords} → {os.path.basename(file_path)}")
    except subprocess.TimeoutExpired:
        logger.warning(f"Exiftool-Timeout: {os.path.basename(file_path)}")
    except Exception as e:
        logger.warning(f"Keyword-Fehler {os.path.basename(file_path)}: {e}")


def _collect_folder_files(base_path):
    """Alle Bilddateien geordnet nach Ordnername sammeln.
    Gibt dict { folder_name: [(root, filename), ...] } zurueck."""
    result = {}
    for root, dirs, files in os.walk(base_path):
        folder_name = os.path.basename(root)
        if root == base_path:
            continue  # Root-Ebene ueberspringen
        for filename in sorted(files):
            if filename.startswith('.'):
                continue
            if not filename.lower().endswith(IMAGE_EXTS):
                continue
            result.setdefault(folder_name, []).append((root, filename))
    return result


def process_and_rename_files():
    """
    Benennt alle Bilder in 02-Webcheck nach dem Positions-Schema um:

      01-Mainimage  →  {SKU}_1.ext          (Mainimage, max 1)
      02-Mood       →  {SKU}_2.ext, _3.ext  (Mood, max 2)
      03-Pos4-X     →  {SKU}_4.ext, _5, ... (Pos 4-x, fortlaufend pro SKU)

    KEINE Keyword-Modifikation: Lightroom/Image-Classification haben die
    Keywords bereits korrekt geschrieben - die bleiben unangetastet.
    """
    logger.info("Phase 1: Dateien umbenennen (Keywords bleiben unveraendert)...")
    renamed = 0

    folder_files = _collect_folder_files(webcheck_path)

    # ── Pass 1: Mainimage (01-Mainimage) → _1 ──────────────────
    sku_clip_count = {}
    for root, filename in folder_files.get(MAINIMAGE_FOLDER, []):
        sku_match = pattern.search(filename)
        if not sku_match:
            continue
        sku = sku_match.group(0)
        sku_clip_count[sku] = sku_clip_count.get(sku, 0) + 1
        if sku_clip_count[sku] > 1:
            logger.warning(f"Mehr als 1 Mainimage fuer SKU {sku} – ueberspringe {filename}")
            continue
        ext = os.path.splitext(filename)[1]
        new_path = _do_rename(root, filename, f"{sku}_{MAINIMAGE_POS}{ext}")
        if new_path:
            renamed += 1

    # ── Pass 2: Mood (02-Mood) → _2, _3 ────────────────────────
    sku_mood_count = {}
    for root, filename in folder_files.get(MOOD_FOLDER, []):
        sku_match = pattern.search(filename)
        if not sku_match:
            continue
        sku = sku_match.group(0)
        sku_mood_count[sku] = sku_mood_count.get(sku, 0) + 1
        if sku_mood_count[sku] > MOOD_MAX:
            logger.warning(f"Mehr als {MOOD_MAX} Mood-Bilder fuer SKU {sku} – ueberspringe {filename}")
            continue
        pos = MOOD_START_POS + sku_mood_count[sku] - 1
        ext = os.path.splitext(filename)[1]
        new_path = _do_rename(root, filename, f"{sku}_{pos}{ext}")
        if new_path:
            renamed += 1

    # ── Pass 3: Alle anderen Ordner → _4, _5, _6 … ────────────
    # Ordner alphabetisch sortieren fuer reproduzierbare Reihenfolge
    sku_other_count = {}
    for folder_name in sorted(folder_files.keys()):
        if folder_name in (MAINIMAGE_FOLDER, MOOD_FOLDER):
            continue
        for root, filename in folder_files[folder_name]:
            sku_match = pattern.search(filename)
            if not sku_match:
                continue
            sku = sku_match.group(0)
            sku_other_count[sku] = sku_other_count.get(sku, 0) + 1
            pos = OTHER_START_POS + sku_other_count[sku] - 1
            ext = os.path.splitext(filename)[1]
            new_path = _do_rename(root, filename, f"{sku}_{pos}{ext}")
            if new_path:
                renamed += 1

    logger.info(f"Phase 1: {renamed} Dateien umbenannt (Keywords aus Lightroom uebernommen)")
    return True


# ============================================================
# PHASE 2: COPY TO UPLOAD
# ============================================================

def copy_images_to_upload():
    """
    Kopiert alle Bilder aus 02-Webcheck (inkl. Unterordner) flach nach 03-Upload.
    Die Bilder haben zu diesem Zeitpunkt schon die Webshop-Benennung
    ({SKU}_{Counter}.jpg) aus Phase 1.
    """
    logger.info("Phase 2: Kopiere Bilder zu 03-Upload...")

    try:
        os.makedirs(upload_path, exist_ok=True)
        copied = 0

        for root, _, files in os.walk(webcheck_path):
            for filename in sorted(files):
                if filename.startswith('.'):
                    continue
                if not filename.lower().endswith(IMAGE_EXTS):
                    continue

                source = os.path.join(root, filename)
                dest = os.path.join(upload_path, filename)

                # Kollisions-Handling
                if os.path.exists(dest):
                    base, ext = os.path.splitext(filename)
                    counter = 2
                    while os.path.exists(dest):
                        dest = os.path.join(upload_path, f"{base}_{counter}{ext}")
                        counter += 1

                try:
                    shutil.copy2(source, dest)
                    copied += 1
                except Exception as e:
                    logger.warning(f"Kopier-Fehler {filename}: {e}")

        logger.info(f"Phase 2: {copied} Bilder nach 03-Upload kopiert")
        return copied

    except Exception as e:
        logger.error(f"Fehler in Phase 2: {e}")
        return 0




# ============================================================
# PHASE 3: EXCEL REPORT
# ============================================================

def get_keywords_from_file(file_path):
    """Keywords per exiftool aus Datei lesen."""
    try:
        result = subprocess.run(
            [EXIFTOOL, "-keywords", "-XMP:Subject", file_path],
            capture_output=True, text=True, timeout=5
        )
        kws = set()
        for line in result.stdout.strip().split("\n"):
            if ": " in line:
                vals = line.split(": ", 1)[1]
                for v in vals.split(", "):
                    v = v.strip()
                    if v:
                        kws.add(v)
        return sorted(kws)
    except Exception:
        return []


def create_excel_report(ticket_key):
    """Erstellt Excel-Report ueber alle Bilder in 01-Input."""
    logger.info("Phase 3: Excel-Report erstellen...")
    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "Bilder"
        ws.append([
            'Dateiname', 'SKU', 'Kategorie', 'Keywords',
            'Dateigr. (KB)', 'Aufnahmedatum', 'Aenderungsdatum',
            'Breite', 'Hoehe', 'Aufloesung (MP)', 'MD5'
        ])

        sku_set = set()
        total = 0

        for root, dirs, files in os.walk(webcheck_path):
            folder_name = os.path.basename(root)
            for filename in sorted(files):
                if filename.startswith('.'):
                    continue
                if not filename.lower().endswith(IMAGE_EXTS):
                    continue

                file_path = os.path.join(root, filename)
                total += 1

                sku_match = pattern.search(filename)
                sku = sku_match.group(0) if sku_match else ""
                if sku:
                    sku_set.add(sku)

                # Dateigroesse
                size_kb = round(os.path.getsize(file_path) / 1024, 1)

                # Datum
                mod_date = datetime.datetime.fromtimestamp(
                    os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M')

                # Aufnahmedatum aus EXIF
                capture_date = ""
                if filename.lower().endswith(('.jpg', '.jpeg')):
                    try:
                        with Image.open(file_path) as img:
                            exif = img._getexif()
                            if exif:
                                for tag_id, val in exif.items():
                                    if TAGS.get(tag_id) == 'DateTimeOriginal':
                                        capture_date = str(val)
                                        break
                    except Exception:
                        pass

                # Bildgroesse
                px_w = px_h = mp = ""
                try:
                    with Image.open(file_path) as img:
                        px_w, px_h = img.size
                        mp = round((px_w * px_h) / 1_000_000, 2)
                except Exception:
                    pass

                # Keywords
                keywords = get_keywords_from_file(file_path)

                # MD5
                try:
                    h = hashlib.md5()
                    with open(file_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            h.update(chunk)
                    md5 = h.hexdigest()
                except Exception:
                    md5 = ""

                # Kategorie aus Ordnername
                category = CATEGORY_SUFFIX_MAP.get(folder_name, folder_name)

                ws.append([
                    filename, sku, category, ", ".join(keywords),
                    size_kb, capture_date, mod_date,
                    px_w, px_h, mp, md5
                ])

        # SKU-Spalte als Zahl formatieren
        for cell in ws['B'][1:]:
            cell.number_format = '0'

        os.makedirs(excel_exports_path, exist_ok=True)
        excel_file = os.path.join(excel_exports_path, f"{ticket_key}.xlsx")
        wb.save(excel_file)
        logger.info(f"Excel-Report: {excel_file}")
        logger.info(f"  {total} Bilder, {len(sku_set)} Artikel")
        return total, len(sku_set)

    except Exception as e:
        logger.error(f"Fehler beim Excel-Report: {e}")
        return 0, 0


# ============================================================
# PHASE 4: JIRA UPDATE
# ============================================================

def update_jira_ticket(ticket_key, image_count, article_count):
    """
    Jira-Ticket aktualisieren:
    - customfield_10303 = Bildanzahl
    - customfield_10299 = Artikelanzahl
    - Assignee = Reporter (Ticket-Ersteller)
    - Kommentar "Upload done 🚀" - Autor ist automatisch der aktuell
      authentifizierte User (JIRA_EMAIL aus config.env)
    - Transition auf 'QA'
    """
    try:
        logger.info(f"Phase 4: Jira-Ticket {ticket_key} aktualisieren...")
        jira = JIRA(options={'server': jira_server},
                    basic_auth=(jira_email, jira_token))

        issue = jira.issue(ticket_key)

        # Reporter auslesen (nur fuer Assignee, nicht fuer Mention)
        reporter = issue.fields.reporter
        if not reporter:
            logger.warning(f"Kein Reporter für {ticket_key} - überspringe Assign")
            reporter_id = None
            reporter_name = None
        else:
            reporter_id = getattr(reporter, 'accountId', None) or getattr(reporter, 'name', None)
            reporter_name = reporter.displayName

        # Felder aktualisieren + Ticket dem Reporter zuweisen
        update_fields = {
            'customfield_10303': image_count,
            'customfield_10299': article_count,
        }
        if reporter_id:
            update_fields['assignee'] = {'accountId': reporter_id}
        try:
            issue.update(fields=update_fields)
            logger.info(f"Ticket {ticket_key} aktualisiert (Bilder={image_count}, Artikel={article_count})")
            if reporter_name:
                logger.info(f"Ticket zugewiesen an: {reporter_name}")
        except Exception as e:
            logger.warning(f"Issue-Update teilweise fehlgeschlagen: {e}")

        # Kommentar posten - Autor wird automatisch der aktuelle User
        # (JIRA_EMAIL aus config.env, also der der die App gerade benutzt)
        try:
            jira.add_comment(ticket_key, "Upload done 🚀")
            logger.info(f"Kommentar gepostet als {jira_email}: 'Upload done 🚀'")
        except Exception as e:
            logger.warning(f"Kommentar fehlgeschlagen: {e}")

        # Transition zu QA
        try:
            transitions = jira.transitions(ticket_key)
            transition_id = None
            transition_name = None
            # Priorität: 'QA', dann 'Genehmigung'
            for keyword in ('qa', 'genehmigung'):
                for t in transitions:
                    if keyword in t['name'].lower():
                        transition_id = t['id']
                        transition_name = t['name']
                        break
                if transition_id:
                    break

            if transition_id:
                jira.transition_issue(ticket_key, transition_id)
                logger.info(f"Ticket auf '{transition_name}' gesetzt")
            else:
                available = [t['name'] for t in transitions]
                logger.warning(f"Transition 'QA'/'Genehmigung' nicht gefunden. Verfügbar: {available}")
        except Exception as e:
            logger.warning(f"Transition fehlgeschlagen: {e}")

        logger.info(f"Jira-Update OK: {image_count} Bilder, {article_count} Artikel")
        return True

    except Exception as e:
        logger.error(f"Fehler beim Jira-Update: {e}")
        return False


# Cleanup-Phase wurde entfernt — Workspace wird ausschliesslich beim naechsten
# Download-RAW-Lauf via cleanupBeforeDownloadRaw() in server.js bereinigt.


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("=" * 70)
        logger.info("04 Ticket abschliessen gestartet")
        logger.info("=" * 70)

        # Ticket-Nummer — App uebergibt via POSTPRO_INPUT, sonst Dialog
        ticket_number = os.environ.get("POSTPRO_INPUT", "").strip()
        if ticket_number:
            if "-" in ticket_number:
                ticket_number = ticket_number.split("-")[-1]
        else:
            while True:
                result = subprocess.run(
                    ['osascript', '-e',
                     'text returned of (display dialog "Ticketnummer eingeben:" '
                     'default answer "" with title "Ticket abschliessen")'],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    logger.info("Durch Benutzer abgebrochen")
                    sys.exit(0)
                ticket_number = result.stdout.strip()
                if ticket_number.isdigit():
                    break
                subprocess.run(
                    ['osascript', '-e',
                     'display alert "Ungueltige Eingabe" message '
                     '"Die Ticketnummer darf nur Zahlen enthalten." as critical'],
                    capture_output=True
                )

        ticket_key = f"CREAMEDIA-{ticket_number}"
        logger.info(f"Ticket: {ticket_key}")
        logger.info(f"Webcheck: {webcheck_path}")
        logger.info(f"Upload:   {upload_path}")

        # Pruefen ob Bilder vorhanden
        image_count_check = 0
        if os.path.exists(webcheck_path):
            for r, d, f in os.walk(webcheck_path):
                for fn in f:
                    if not fn.startswith('.') and fn.lower().endswith(IMAGE_EXTS):
                        image_count_check += 1

        if image_count_check == 0:
            logger.error("Keine Bilder in 02-Webcheck gefunden!")
            logger.error(f"Pfad: {webcheck_path}")
            print(f"FEHLER: Keine Bilder in 02-Webcheck!\nPfad: {webcheck_path}")
            sys.exit(1)

        logger.info(f"{image_count_check} Bilder in Input gefunden")

        # Phase 1: Umbenennen + Keywords
        process_and_rename_files()

        # Phase 2: Kopiere nach 03-Upload
        copied = copy_images_to_upload()
        if copied == 0:
            logger.warning("Keine Bilder kopiert!")

        # Phase 3: Excel-Report
        img_count, art_count = create_excel_report(ticket_key)

        # Phase 4: Jira aktualisieren
        if jira_token and jira_email:
            jira_ok = update_jira_ticket(ticket_key, img_count, art_count)
            if not jira_ok:
                subprocess.run(
                    ['osascript', '-e',
                     f'display alert "Jira-Update fehlgeschlagen" '
                     f'message "Ticket {ticket_key} konnte nicht aktualisiert werden.'
                     f'\\n\\nBitte manuell pruefen." as critical'],
                    capture_output=True
                )
        else:
            logger.warning("Jira-Credentials nicht konfiguriert")

        # Cleanup wird NICHT mehr hier gemacht – nur noch beim naechsten
        # Download-RAW-Lauf via cleanupBeforeDownloadRaw() in server.js.
        # So bleiben Bilder zur Inspektion liegen.

        # Ticket-Key fuer Upload-Script speichern
        try:
            os.makedirs(upload_path, exist_ok=True)
            ticket_file = os.path.join(upload_path, ".ticket_key")
            with open(ticket_file, 'w') as f:
                f.write(ticket_key)
            logger.info(f"Ticket-Key gespeichert: {ticket_key}")
        except Exception as e:
            logger.warning(f"Fehler beim Speichern des Ticket-Keys: {e}")

        logger.info("=" * 70)
        logger.info(f"Ticket abschliessen fertig: {ticket_key}")
        logger.info(f"  {img_count} Bilder, {art_count} Artikel -> 03-Upload")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.info("Durch Benutzer abgebrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
