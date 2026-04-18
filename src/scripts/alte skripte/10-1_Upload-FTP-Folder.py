"""
05 🚀 Upload
- Leert FTP-Verzeichnis
- Lädt lokale Dateien zu FTP hoch
- Synchronisiert Dateien ins DAM
- Leert lokalen Upload-Ordner
- Aktualisiert Jira-Ticket (Zuordnung + Kommentar)
- Löscht Final-Images Ordner (Cleanup)
"""

import os
import sys
import shutil
import logging
import base64
import time
import requests
import paramiko
import subprocess
from stat import S_ISDIR
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Import utilities
sys.path.insert(0, os.path.dirname(__file__))
from _utils import (
    load_config, setup_logging, get_paths, validate_directory_exists
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
local_root_directory = paths['upload']

# SFTP Configuration
sftp_host = config['SFTP_HOST']
sftp_port = config['SFTP_PORT']
sftp_username = config['SFTP_USERNAME']
sftp_password = config['SFTP_PASSWORD']
sftp_directory = config['SFTP_REMOTE_DIR']

# Cliplister API Configuration
client_id = config['CLIPLISTER_CLIENT_ID']
client_secret = config['CLIPLISTER_CLIENT_SECRET']
dam_upload_url = "https://api-rs.mycliplister.com/v2.2/apis/asset/insert"

# Jira Configuration
jira_server = config['JIRA_SERVER']
jira_email = config['JIRA_EMAIL']
jira_token = config['JIRA_API_TOKEN']

# Connection pool with thread lock
connection_pool = []
connection_pool_lock = Lock()
max_pool_size = 3

# ============================================================
# PHASE 1: EMPTY FTP DIRECTORY
# ============================================================

def empty_ftp_directory():
    """Empty FTP directory before upload"""
    try:
        logger.info("FTP-Verzeichnis wird geleert...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(sftp_host, port=sftp_port, username=sftp_username,
                       password=sftp_password, timeout=30)
            sftp = ssh.open_sftp()

            try:
                files = sftp.listdir(sftp_directory)
                deleted_count = 0
                for file_name in files:
                    remote_path = f"{sftp_directory}/{file_name}"
                    try:
                        sftp.remove(remote_path)
                        logger.info(f"Gelöscht: {remote_path}")
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Fehler beim Löschen von {remote_path}: {e}")

                logger.info(f"Phase 1: {deleted_count} Dateien aus FTP gelöscht")
                return True
            finally:
                sftp.close()
        finally:
            ssh.close()
    except paramiko.SSHException as e:
        logger.error(f"SFTP-Verbindungsfehler: {e}")
        return False
    except Exception as e:
        logger.error(f"Fehler beim Leeren des FTP-Verzeichnisses: {e}")
        return False

# ============================================================
# PHASE 2: UPLOAD LOCAL FILES TO FTP
# ============================================================

def create_sftp_client_with_retry(max_retries=3, initial_wait_time=5):
    """Create SFTP client with retry logic"""
    retries = 0
    wait_time = initial_wait_time

    while retries < max_retries:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(sftp_host, port=sftp_port, username=sftp_username,
                       password=sftp_password, timeout=30)
            logger.debug(f"SFTP-Verbindung hergestellt (Versuch {retries + 1})")
            return ssh.open_sftp()
        except (paramiko.SSHException, paramiko.ssh_exception.SSHException) as e:
            logger.warning(f"Verbindungsfehler: {e}. Versuch {retries + 1} von {max_retries}. Warte {wait_time}s.")
            time.sleep(wait_time)
            retries += 1
            wait_time = min(wait_time * 2, 30)  # Cap at 30 seconds

    logger.error(f"Maximale Verbindungsversuche ({max_retries}) erreicht")
    raise Exception("Konnte keine SFTP-Verbindung hergestellen")

def get_sftp_client():
    """Get SFTP client from pool or create new one"""
    with connection_pool_lock:
        if connection_pool:
            return connection_pool.pop()

    return create_sftp_client_with_retry()

def release_sftp_client(sftp):
    """Release SFTP client back to pool"""
    try:
        with connection_pool_lock:
            if len(connection_pool) < max_pool_size:
                connection_pool.append(sftp)
                return

        sftp.close()
    except Exception as e:
        logger.warning(f"Fehler beim Schließen des SFTP-Clients: {e}")

def upload_file_with_retry(local_path, remote_path, max_attempts=3):
    """Upload file with retry logic"""
    attempt = 0
    wait_time = 1

    while attempt < max_attempts:
        sftp = None
        try:
            sftp = get_sftp_client()
            sftp.put(local_path, remote_path)
            logger.info(f"Hochgeladen: {os.path.basename(local_path)} → {remote_path}")
            release_sftp_client(sftp)
            return True
        except Exception as e:
            logger.warning(f"Upload-Fehler für {os.path.basename(local_path)} (Versuch {attempt + 1}/{max_attempts}): {e}")
            time.sleep(wait_time)
            wait_time = min(wait_time * 2, 30)
            attempt += 1
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass

    logger.error(f"Upload fehlgeschlagen nach {max_attempts} Versuchen: {local_path}")
    return False

def upload_directory_to_ftp(local_directory, remote_directory, max_workers=10):
    """Upload directory to FTP recursively"""
    try:
        futures = []
        uploaded_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for filename in os.listdir(local_directory):
                if filename.startswith('.'):
                    continue

                local_path = os.path.join(local_directory, filename)
                remote_path = f"{remote_directory}/{filename}"

                try:
                    if os.path.isdir(local_path):
                        # Create remote directory
                        sftp = get_sftp_client()
                        try:
                            sftp.stat(remote_path)
                        except IOError:
                            try:
                                sftp.mkdir(remote_path)
                                logger.info(f"Verzeichnis erstellt: {remote_path}")
                            except Exception as e:
                                logger.warning(f"Fehler beim Erstellen von {remote_path}: {e}")
                        finally:
                            release_sftp_client(sftp)

                        # Recursively upload subdirectory
                        upload_directory_to_ftp(local_path, remote_path, max_workers)
                    else:
                        # Upload file
                        futures.append(executor.submit(upload_file_with_retry, local_path, remote_path))

                except Exception as e:
                    logger.warning(f"Fehler beim Verarbeiten von {filename}: {e}")

            # Wait for all uploads to complete
            for future in as_completed(futures):
                try:
                    if future.result():
                        uploaded_count += 1
                except Exception as e:
                    logger.warning(f"Upload-Task-Fehler: {e}")

        logger.info(f"Phase 2: {uploaded_count} Dateien zu FTP hochgeladen")
        return uploaded_count > 0
    except Exception as e:
        logger.error(f"Fehler beim FTP-Upload: {e}")
        return False

# ============================================================
# PHASE 3: FTP → DAM (Cliplister)
# ============================================================

def authenticate_dam():
    """Get DAM token via shared cache."""
    from _utils import get_dam_token as _get_dam_token
    return _get_dam_token(config)

def get_sftp_file_list(directory=sftp_directory):
    """Get list of files from SFTP directory"""
    file_list = []
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(sftp_host, port=sftp_port, username=sftp_username,
                   password=sftp_password, timeout=30)

        with ssh.open_sftp() as sftp:
            try:
                sftp.chdir(directory)
                for entry in sftp.listdir_attr():
                    filepath = f"{directory}/{entry.filename}"
                    if S_ISDIR(entry.st_mode):
                        # Recursively get files from subdirectory
                        file_list.extend(get_sftp_file_list(filepath))
                    else:
                        if not entry.filename.startswith('.'):
                            file_list.append(filepath)
            except Exception as e:
                logger.warning(f"Fehler beim Lesen von {directory}: {e}")
    except paramiko.SSHException as e:
        logger.error(f"SFTP-Fehler beim Datei-Auflisten: {e}")
    except Exception as e:
        logger.error(f"Fehler beim Auflisten von Dateien: {e}")
    finally:
        ssh.close()

    return file_list

def upload_image_to_dam(access_token, filepath):
    """Upload image to Cliplister DAM"""
    try:
        relative_path = filepath.replace(sftp_directory + "/", "")
        image_url = f"https://{sftp_host}/files/{sftp_username}/upload/SVB/{relative_path}"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=utf-8"}
        payload = {
            "source": image_url,
            "categories": [{"id": 591672}],
            "fileName": os.path.basename(filepath)
        }

        response = requests.put(dam_upload_url, headers=headers, json=payload,
                               timeout=config['API_REQUEST_TIMEOUT'])

        if response.status_code == 201:
            logger.info(f"DAM-Upload: {os.path.basename(filepath)} erfolgreich")
            return True
        else:
            logger.warning(f"DAM-Upload-Fehler für {os.path.basename(filepath)}: {response.status_code}")
            return False
    except requests.exceptions.Timeout:
        logger.error(f"Timeout beim DAM-Upload für {os.path.basename(filepath)}")
        return False
    except Exception as e:
        logger.error(f"DAM-Upload-Fehler für {os.path.basename(filepath)}: {e}")
        return False

def upload_ftp_to_dam():
    """Upload all FTP files to DAM"""
    try:
        logger.info("Lade Dateien vom FTP ins DAM...")
        access_token = authenticate_dam()
        file_list = get_sftp_file_list()

        if not file_list:
            logger.info("Keine Dateien auf FTP gefunden")
            return True

        uploaded_count = 0
        with ThreadPoolExecutor(max_workers=config['ASYNC_TASK_CONCURRENCY']) as executor:
            futures = [executor.submit(upload_image_to_dam, access_token, fp) for fp in file_list]

            for future in as_completed(futures):
                try:
                    if future.result():
                        uploaded_count += 1
                except Exception as e:
                    logger.warning(f"DAM-Upload-Task-Fehler: {e}")

        executor.shutdown(wait=True)
        logger.info(f"Phase 3: {uploaded_count}/{len(file_list)} Dateien ins DAM hochgeladen")
        return True
    except Exception as e:
        logger.error(f"Fehler beim FTP→DAM-Upload: {e}")
        return False

# ============================================================
# PHASE 4: CLEAR LOCAL UPLOAD FOLDER
# ============================================================

def clear_local_upload_folder():
    """Clear local upload folder after successful upload"""
    try:
        folder = local_root_directory
        if not os.path.exists(folder):
            logger.info(f"Ordner nicht gefunden: {folder}")
            return True

        deleted_count = 0
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                    logger.info(f"Datei gelöscht: {filename}")
                    deleted_count += 1
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                    logger.info(f"Ordner gelöscht: {filename}")
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Fehler beim Löschen von {file_path}: {e}")

        logger.info(f"Phase 4: {deleted_count} Objekte aus lokalem Upload-Ordner gelöscht")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Leeren des lokalen Upload-Ordners: {e}")
        return False

# ============================================================
# PHASE 5: UPDATE JIRA TICKET
# ============================================================

def update_jira_after_upload(ticket_key):
    """Update Jira ticket after successful upload"""
    try:
        if not jira_token or not jira_email:
            logger.warning("Jira-Credentials nicht konfiguriert - überspringe Jira-Update")
            return False

        logger.info(f"Aktualisiere Jira-Ticket: {ticket_key}")
        jira_options = {'server': jira_server}
        jira = JIRA(options=jira_options, basic_auth=(jira_email, jira_token))

        # Get issue and reporter
        try:
            issue = jira.issue(ticket_key)
            reporter = issue.fields.reporter

            if not reporter:
                logger.warning(f"Kein Autor für {ticket_key} gefunden")
                return False

            reporter_name = reporter.displayName
            reporter_accountid = reporter.accountId if hasattr(reporter, 'accountId') else reporter.name

            # Assign ticket to reporter
            try:
                issue.update(assignee={'id': reporter_accountid})
                logger.info(f"Ticket {ticket_key} zugewiesen an: {reporter_name}")
            except Exception as e:
                logger.warning(f"Fehler beim Zuweisen zu {reporter_name}: {e}")

            # Add comment with mention (using accountId for better compatibility)
            try:
                comment_text = f"[~{reporter_accountid}] Upload Done 🦄"
                jira.add_comment(ticket_key, comment_text)
                logger.info(f"Kommentar zu {ticket_key} hinzugefügt")
            except Exception as e:
                logger.warning(f"Fehler beim Kommentar-Hinzufügen: {e}")

            # Transition ticket to "Genehmigung" status
            try:
                transitions = jira.transitions(ticket_key)
                genehmigung_transition = None
                for transition in transitions:
                    if 'genehmigung' in transition['name'].lower():
                        genehmigung_transition = transition['id']
                        break

                if genehmigung_transition:
                    jira.transition_issue(ticket_key, genehmigung_transition)
                    logger.info(f"Ticket {ticket_key} Status auf 'Genehmigung' gesetzt")
                else:
                    logger.debug(f"Status 'Genehmigung' nicht verfügbar für {ticket_key}")
            except Exception as e:
                logger.warning(f"Fehler beim Status-Update: {e}")

            logger.info(f"✅ Jira-Ticket {ticket_key} aktualisiert")
            return True

        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Tickets {ticket_key}: {e}")
            return False

    except Exception as e:
        logger.error(f"Fehler beim Jira-Update für {ticket_key}: {e}")
        return False

# ============================================================
# PHASE 6: CLEANUP FINAL IMAGES
# ============================================================

def cleanup_final_images():
    """Delete Final Images folder completely after successful upload"""
    try:
        final_images_path = os.path.join(base_folder_path, "08-FINAL-Images")

        if not os.path.exists(final_images_path):
            logger.debug("08-FINAL-Images Ordner existiert nicht (normal)")
            return True

        # Count files before deletion
        file_count = 0
        for root, dirs, files in os.walk(final_images_path):
            file_count += len(files)

        # Delete entire folder
        shutil.rmtree(final_images_path)

        logger.debug(f"08-FINAL-Images Ordner gelöscht ({file_count} Dateien)")
        return True

    except Exception as e:
        logger.error(f"Fehler beim Löschen des Final-Images Ordners: {e}")
        return False

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("="*70)
        logger.info("05 🚀 Upload gestartet")
        logger.info("="*70)

        # Validate directories
        try:
            validate_directory_exists(local_root_directory, create_if_missing=True)
        except Exception as e:
            logger.error(f"Fehler mit Upload-Verzeichnis: {e}")
            sys.exit(1)

        # Phase 1: Empty FTP directory
        if not empty_ftp_directory():
            logger.warning("Phase 1 mit Fehlern abgeschlossen")

        # Phase 2: Upload local files to FTP
        logger.info("Starte Upload zu FTP...")
        if not upload_directory_to_ftp(local_root_directory, sftp_directory):
            logger.warning("Phase 2 mit Fehlern abgeschlossen")

        # Close remaining connections in pool
        with connection_pool_lock:
            for sftp in connection_pool:
                try:
                    sftp.close()
                except Exception:
                    pass
            connection_pool.clear()

        logger.info("FTP-Upload abgeschlossen")

        # Phase 3: Upload FTP files to DAM
        if not upload_ftp_to_dam():
            logger.warning("Phase 3 mit Fehlern abgeschlossen")

        # Read ticket key BEFORE clearing the folder (Phase 4 would delete it!)
        ticket_key = None
        ticket_key_file = os.path.join(local_root_directory, ".ticket_key")
        if os.path.exists(ticket_key_file):
            try:
                with open(ticket_key_file, 'r') as f:
                    ticket_key = f.read().strip() or None
                logger.info(f"Ticket-Key gelesen: {ticket_key}")
            except Exception as e:
                logger.warning(f"Fehler beim Lesen der Ticket-Key Datei: {e}")

        # Phase 4: Clear local folder
        if not clear_local_upload_folder():
            logger.warning("Phase 4 mit Fehlern abgeschlossen")

        # Phase 5: Update Jira ticket
        logger.info("Starte Phase 5: Aktualisiere Jira-Ticket...")
        if ticket_key:
            if update_jira_after_upload(ticket_key):
                logger.info("✅ Phase 5: Jira-Ticket aktualisiert")
            else:
                logger.warning("⚠️ Phase 5: Fehler beim Jira-Update")
        else:
            logger.info("Phase 5: Kein Ticket-Key gefunden - überspringe Jira-Update")

        # Phase 6: Cleanup Final Images
        logger.info("Starte Phase 6: Aufräumen der Final-Images...")
        if cleanup_final_images():
            logger.info("✅ Phase 6: Final-Images-Ordner erfolgreich gelöscht")
        else:
            logger.warning("⚠️ Phase 6: Fehler beim Löschen des Final-Images-Ordners")

        logger.info("="*70)
        logger.info("✅ Upload-Prozess erfolgreich abgeschlossen")
        logger.info("="*70)

    except KeyboardInterrupt:
        logger.info("Upload durch Benutzer unterbrochen")

        # Close remaining connections
        with connection_pool_lock:
            for sftp in connection_pool:
                try:
                    sftp.close()
                except Exception:
                    pass
            connection_pool.clear()

        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)

        # Close remaining connections
        with connection_pool_lock:
            for sftp in connection_pool:
                try:
                    sftp.close()
                except Exception:
                    pass
            connection_pool.clear()

        sys.exit(1)
