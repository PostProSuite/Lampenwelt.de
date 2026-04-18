"""
20-1 Cliplister Webshop-Import
==============================
Lokaler Upload-Ordner → DemoUp Cliplister DAM.

Flow:
- User tippt im PostPro-Rollout die Creamedia-Ticketnummer ein (z. B. "CREAMEDIA-12345"
  oder nur "12345" — Prefix wird automatisch ergaenzt).
- Dieses Script:
    1) Liest alle Bilder aus workspace/03-Upload/
    2) Findet oder erstellt eine Sub-Kategorie "CREAMEDIA-xxxxx"
       unter der Eltern-Kategorie WEBSHOP_PARENT_CATEGORY (591672).
    3) Laedt alle Bilder via Cliplister-REST-API hoch
       - Asset-Name  = Dateiname ohne Extension
       (Keywords & weitere Metadaten werden beim Ticket-Abschluss gesetzt.)

Kein Jira-Update, kein Cleanup — bewusst minimal fokussiert.
"""

import os
import sys
import logging
import requests
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from _utils import load_config, setup_logging, get_paths, get_dam_token

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
upload_folder = paths['upload']

# ── Cliplister API ──────────────────────────────────────────
DAM_API_BASE        = "https://api-rs.mycliplister.com/v2.2/apis"
DAM_ASSET_INSERT    = f"{DAM_API_BASE}/asset/insert"
DAM_CATEGORY_LIST   = f"{DAM_API_BASE}/category/list"
DAM_CATEGORY_INSERT = f"{DAM_API_BASE}/category/insert"

# ── Ziel-Eltern-Kategorie (Webshopbilder) ──────────────────
# Kann per .env-Variable WEBSHOP_PARENT_CATEGORY_ID ueberschrieben werden.
WEBSHOP_PARENT_CATEGORY = int(os.getenv('WEBSHOP_PARENT_CATEGORY_ID', 591672))

TICKET_PREFIX = config.get('JIRA_TICKET_PREFIX', 'CREAMEDIA')

SUPPORTED_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp')

# ============================================================
# HELPERS
# ============================================================

def get_dam_headers():
    token = get_dam_token(config)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json; charset=utf-8",
    }


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


def normalize_ticket(value: str) -> str:
    """'12345' -> 'CREAMEDIA-12345'; 'creamedia-5175' -> 'CREAMEDIA-5175'."""
    v = value.strip().upper().replace(' ', '')
    if not v:
        return v
    if '-' in v:
        return v
    return f"{TICKET_PREFIX.upper()}-{v}"


def find_or_create_subcategory(ticket_key: str) -> int | None:
    """Find or create sub-category `ticket_key` under WEBSHOP_PARENT_CATEGORY."""
    try:
        headers = get_dam_headers()

        # 1) Liste der bestehenden Kinder-Kategorien holen
        r = requests.get(
            f"{DAM_CATEGORY_LIST}?parent_id={WEBSHOP_PARENT_CATEGORY}&limit=500",
            headers=headers,
            timeout=config['API_REQUEST_TIMEOUT'],
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and 'categories' in data:
            existing = data['categories']
        elif isinstance(data, list):
            existing = data
        else:
            existing = []

        for cat in existing:
            name = (cat.get('name') or cat.get('title') or '').strip().upper()
            if name == ticket_key.upper():
                cat_id = cat.get('id')
                logger.info(f"Kategorie bereits vorhanden: {ticket_key} (ID {cat_id})")
                print(f"✓ Kategorie existiert: {ticket_key}")
                return cat_id

        # 2) Nicht da → anlegen
        payload = {
            "name":     ticket_key,
            "parentId": WEBSHOP_PARENT_CATEGORY,
        }
        r = requests.put(
            DAM_CATEGORY_INSERT,
            headers=headers,
            json=payload,
            timeout=config['API_REQUEST_TIMEOUT'],
        )
        if r.status_code in (200, 201):
            result = r.json()
            cat_id = result.get('id') or result.get('categoryId')
            logger.info(f"Kategorie erstellt: {ticket_key} (ID {cat_id})")
            print(f"✓ Kategorie angelegt: {ticket_key}")
            return cat_id

        logger.error(f"Create-Kategorie fehlgeschlagen: {r.status_code} — {r.text}")
        print(f"✗ Kategorie konnte nicht angelegt werden (HTTP {r.status_code})")
        return None

    except Exception as e:
        logger.error(f"find_or_create_subcategory: {e}", exc_info=True)
        print(f"✗ Fehler bei Kategorie: {e}")
        return None


def upload_single_image(filepath: str, category_id: int) -> bool:
    """Upload one image via SFTP, then register with DAM via source-URL."""
    filename   = os.path.basename(filepath)
    asset_name = os.path.splitext(filename)[0]

    try:
        # Step 1: Upload to SFTP
        if not upload_to_sftp(filepath, filename):
            logger.warning(f"  SFTP-Upload fehlgeschlagen: {filename}")
            return False

        # Step 2: Construct HTTPS URL for DAM API
        sftp_url = f"https://clup01.cliplister.com/files/{config['SFTP_USERNAME']}{config['SFTP_REMOTE_DIR']}/{filename}".replace('\\', '/')

        # Step 3: Register with DAM
        payload = {
            "fileName":   filename,
            "name":       asset_name,
            "source":     sftp_url,
            "categories": [{"id": category_id}],
        }

        r = requests.put(
            DAM_ASSET_INSERT,
            headers=get_dam_headers(),
            json=payload,
            timeout=30,
        )

        if r.status_code in (200, 201):
            logger.info(f"Upload OK: {filename}")
            return True

        logger.warning(f"Upload-Fehler {filename}: HTTP {r.status_code} — {r.text[:200]}")
        return False

    except requests.exceptions.Timeout:
        logger.error(f"Timeout beim Upload von {filename}")
        return False
    except Exception as e:
        logger.error(f"Upload-Exception {filename}: {e}")
        return False


def collect_images(folder: str) -> list[str]:
    """Recursively collect image files from folder, ignoring dotfiles."""
    out = []
    if not os.path.isdir(folder):
        return out
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.startswith('.'):
                continue
            if fn.lower().endswith(SUPPORTED_EXTS):
                out.append(os.path.join(root, fn))
    return sorted(out)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("=" * 70)
        logger.info("20-1 Cliplister Webshop-Import gestartet")
        logger.info("=" * 70)

        # 1) Ticket
        raw_input_val = os.environ.get("POSTPRO_INPUT", "").strip()
        if not raw_input_val:
            print("Bitte Creamedia-Ticketnummer eingeben (z. B. CREAMEDIA-12345)")
            sys.exit(1)

        ticket_key = normalize_ticket(raw_input_val)
        logger.info(f"Ticket: {ticket_key}")
        print(f"▶ Ticket: {ticket_key}")
        print(f"▶ Parent-Kategorie (Webshopbilder): {WEBSHOP_PARENT_CATEGORY}")

        # 2) Bilder sammeln
        images = collect_images(upload_folder)
        if not images:
            print(f"Keine Bilder in {upload_folder}")
            logger.info("Keine Bilder zum Hochladen gefunden — Ende.")
            sys.exit(0)

        print(f"▶ {len(images)} Bild(er) im Upload-Ordner gefunden")

        # 3) Zielkategorie vorbereiten
        print("▶ Kategorie in DAM vorbereiten …")
        category_id = find_or_create_subcategory(ticket_key)
        if not category_id:
            print("✗ Abbruch — Kategorie konnte nicht vorbereitet werden.")
            sys.exit(1)

        # 4) Upload
        print(f"▶ Upload startet ({len(images)} Bilder) …")
        uploaded = 0
        concurrency = min(config.get('ASYNC_TASK_CONCURRENCY', 4), 6)

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {
                ex.submit(upload_single_image, fp, category_id): fp
                for fp in images
            }
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        uploaded += 1
                except Exception as e:
                    logger.warning(f"Future-Exception: {e}")
                done = sum(1 for f in futures if f.done())
                print(f"Upload: {done}/{len(images)} ({uploaded} OK)")

        # 5) Fazit
        print("")
        print(f"✓ Fertig: {uploaded}/{len(images)} hochgeladen in '{ticket_key}'")
        logger.info(f"Fertig: {uploaded}/{len(images)} in Kategorie {ticket_key} (ID {category_id})")

        sys.exit(0 if uploaded == len(images) else 2)

    except KeyboardInterrupt:
        print("Abgebrochen.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unerwarteter Fehler: {e}", exc_info=True)
        print(f"FATAL: {e}")
        sys.exit(1)
