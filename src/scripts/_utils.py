"""
Utility functions for PostPro-Scripts
- Config management
- Logging
- DAM API authentication (shared token cache)
- osascript dialog helpers
- Shared workflow functions (Lightroom sync, keyword-move, category mapping)
- Error handling
"""

import os
import sys
import base64
import logging
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ============================================================
# CONFIGURATION LOADING
# ============================================================

def load_config():
    """Load configuration from .env file"""
    script_dir = os.path.dirname(__file__)
    # Suche config.env: zuerst config/config.env, dann direkt im Skriptordner
    env_file = os.path.join(script_dir, "config", "config.env")
    if not os.path.exists(env_file):
        env_file = os.path.join(script_dir, "config.env")
    if not os.path.exists(env_file):
        raise FileNotFoundError(
            f"config.env nicht gefunden!\n"
            f"Erwartet unter: {os.path.join(script_dir, 'config', 'config.env')}\n"
            f"oder direkt in: {script_dir}\n"
            f"Bitte config.env dort ablegen und Zugangsdaten eintragen."
        )
    load_dotenv(env_file)
    return {
        'CLIPLISTER_CLIENT_ID':     os.getenv('CLIPLISTER_CLIENT_ID'),
        'CLIPLISTER_CLIENT_SECRET': os.getenv('CLIPLISTER_CLIENT_SECRET'),
        'SFTP_HOST':                os.getenv('SFTP_HOST', 'clup01.cliplister.com'),
        'SFTP_PORT':                int(os.getenv('SFTP_PORT', 4545)),
        'SFTP_USERNAME':            os.getenv('SFTP_USERNAME', 'lw01'),
        'SFTP_PASSWORD':            os.getenv('SFTP_PASSWORD'),
        'SFTP_REMOTE_DIR':          os.getenv('SFTP_REMOTE_DIR', '/upload/SVB'),
        'JIRA_SERVER':              os.getenv('JIRA_SERVER', 'https://lampenwelt.atlassian.net'),
        'JIRA_EMAIL':               os.getenv('JIRA_EMAIL'),
        'JIRA_API_TOKEN':           os.getenv('JIRA_API_TOKEN'),
        'JIRA_TICKET_PREFIX':       os.getenv('JIRA_TICKET_PREFIX', 'CREAMEDIA'),
        'LOG_LEVEL':                os.getenv('LOG_LEVEL', 'INFO'),
        'LOG_FILE':                 os.getenv('LOG_FILE', os.path.join(os.path.dirname(__file__), '..', 'logs', 'postpro.log')),
        'API_REQUEST_TIMEOUT':      int(os.getenv('API_REQUEST_TIMEOUT', 120)),
        'LIGHTROOM_STARTUP_DELAY':  int(os.getenv('LIGHTROOM_STARTUP_DELAY', 8)),
        'API_REQUEST_DELAY':        float(os.getenv('API_REQUEST_DELAY', 1)),
        'ASYNC_TASK_CONCURRENCY':   int(os.getenv('ASYNC_TASK_CONCURRENCY', 4)),
        'POSTPRO_WORKSPACE':        os.getenv('POSTPRO_WORKSPACE', ''),
    }

# ============================================================
# LOGGING SETUP
# ============================================================

def setup_logging(log_file=None, log_level='INFO'):
    """Setup logging with file and console output"""
    if log_file and log_file != '/dev/null':
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Avoid adding duplicate handlers
    if root_logger.handlers:
        root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file and log_file != '/dev/null':
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger(__name__)

# ============================================================
# DAM API AUTHENTICATION (shared token cache)
# ============================================================

_dam_token = None
_dam_token_expires = datetime.utcnow()
_dam_token_lock = threading.Lock()

DAM_AUTH_URL = "https://api-as.mycliplister.com/oauth2/token"

def _fetch_dam_token(client_id, client_secret, timeout=30):
    """Fetch a fresh DAM OAuth2 token."""
    import requests
    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    headers = {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    response = requests.post(
        DAM_AUTH_URL, headers=headers,
        data={'grant_type': 'client_credentials'}, timeout=timeout
    )
    response.raise_for_status()
    data = response.json()
    expires = datetime.utcnow() + timedelta(seconds=data['expires_in'])
    return data['access_token'], expires


def get_dam_token(config):
    """
    Thread-safe DAM token with automatic renewal.
    Use this instead of local authenticate() functions in each script.
    """
    global _dam_token, _dam_token_expires
    with _dam_token_lock:
        if _dam_token is None or datetime.utcnow() >= _dam_token_expires:
            _dam_token, _dam_token_expires = _fetch_dam_token(
                config['CLIPLISTER_CLIENT_ID'],
                config['CLIPLISTER_CLIENT_SECRET'],
                timeout=config.get('API_REQUEST_TIMEOUT', 30)
            )
    return _dam_token


def invalidate_dam_token():
    """Force token renewal on next call (e.g. after 401)."""
    global _dam_token, _dam_token_expires
    with _dam_token_lock:
        _dam_token = None
        _dam_token_expires = datetime.utcnow()

# ============================================================
# NATIVE macOS DIALOG HELPERS (osascript)
# ============================================================

def ask_input(title, message, default=""):
    """
    Liest zuerst POSTPRO_INPUT aus der Umgebung (gesetzt von der App).
    Nur wenn nicht gesetzt, öffnet sich der native macOS Dialog.
    """
    import os
    env_val = os.environ.get("POSTPRO_INPUT", "").strip()
    if env_val:
        return env_val
    script = (
        f'text returned of (display dialog "{message}" '
        f'default answer "{default}" with title "{title}")'
    )
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ask_confirm(title, message, ok_button="OK", cancel_button="Abbrechen"):
    """
    Show a native macOS confirmation dialog.
    Returns True if the user clicked ok_button, False otherwise.
    """
    script = (
        f'button returned of (display dialog "{message}" '
        f'buttons {{"{cancel_button}", "{ok_button}"}} '
        f'default button "{ok_button}" with title "{title}")'
    )
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == ok_button


def show_alert(title, message, is_error=False):
    """Show a native macOS alert (informational or error)."""
    alert_type = "as critical" if is_error else ""
    script = f'display alert "{title}" message "{message}" {alert_type}'
    subprocess.run(['osascript', '-e', script], capture_output=True)

# ============================================================
# SHARED WORKFLOW: CATEGORY MAPPING
# ============================================================

CATEGORY_ID_TO_SUBFOLDER = {
    408719: 'A10-Mood',
    408735: 'B20-Clipping',
    408736: 'B30-Dimensions',
    408720: 'B40-Neutral',
    408721: 'C-Detail',
    408753: 'C50-Shade',
    408752: 'C60-Material',
    408751: 'C70-Switch',
    408750: 'C80-Base_Stand',
    408749: 'C90-Cable',
    408747: 'C95-Split',       # war fälschlicherweise 'C95-Splitscreen'
    408722: 'D-Technical',
    408756: 'D110-Remote',
    408755: 'D120-Accesories',
    408723: 'E130-Graphics',
    408778: 'E130-Graphics_DE',
    408777: 'E130-Graphics_INT',
    408776: 'E130-Graphics_ENG',
    408762: 'F140-Group',
    408760: 'G-UGC',
    408724: 'F-Inspirative',
}

# ============================================================
# SHARED WORKFLOW: KEYWORD-BASED FILE MOVING
# ============================================================

KEYWORD_MAP = {
    "freisteller": ["Freisteller", "Clipping"],
    "ambiente":    ["Mood", "Ambiente"],
    "dimensions":  ["Dimensions"],
    "graphics":    ["Graphics"],
    "detail":      ["Detail"],
    "technical":   ["Technical", "Accesories", "Accessories"],
    "neutral":     ["Neutral"],
    "split":       ["Split"],
}

SUBFOLDER_MAP = {
    "freisteller": "B20-Clipping",
    "ambiente":    "A10-Mood",
    "dimensions":  "B30-Dimensions",
    "graphics":    "E130-Graphics",
    "detail":      "C-Detail",
    "technical":   "D-Technical",
    "neutral":     "B40-Neutral",
    "split":       "C95-Split",
    "shade":       "C50-Shade",
    "material":    "C60-Material",
    "switch":      "C70-Switch",
    "base_stand":  "C80-Base_Stand",
    "cable":       "C90-Cable",
    "remote":      "D110-Remote",
    "accessories": "D120-Accesories",
    "group":       "F140-Group",
    "ugc":         "G-UGC",
}


def _move_single_file_by_keywords(file_path, logger=None):
    """Move one file to the correct subfolder based on its exiftool keywords."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/exiftool", "-keywords", file_path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return

        kws = result.stdout.strip().split(": ")
        keywords = kws[1].split(", ") if len(kws) > 1 else []

        subdir = None
        for key, key_list in KEYWORD_MAP.items():
            if any(kw.lower() in [k.lower() for k in keywords] for kw in key_list):
                subdir = SUBFOLDER_MAP[key]
                break

        if subdir:
            destination_dir = os.path.join(os.path.dirname(file_path), subdir)
            os.makedirs(destination_dir, exist_ok=True)
            os.rename(file_path, os.path.join(destination_dir, os.path.basename(file_path)))
    except subprocess.TimeoutExpired:
        if logger:
            logger.warning(f"exiftool Timeout für {file_path}")
    except Exception as e:
        if logger:
            logger.warning(f"Fehler beim Verschieben von {file_path}: {e}")


def move_files_by_keywords(input_folder, logger=None, concurrency=4):
    """
    Move all files in input_folder to keyword-based subfolders.
    Shared by scripts 01 and 02.
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for root, dirs, files in os.walk(input_folder):
                for file in files:
                    executor.submit(
                        _move_single_file_by_keywords,
                        os.path.join(root, file),
                        logger
                    )

        # Remove now-empty directories
        for root, dirs, files in os.walk(input_folder):
            for directory in dirs:
                try:
                    os.rmdir(os.path.join(root, directory))
                except OSError:
                    pass

        if logger:
            logger.info("Keyword-Klassifikation abgeschlossen")
        return True
    except Exception as e:
        if logger:
            logger.error(f"Fehler bei Keyword-Klassifikation: {e}")
        return False

# ============================================================
# SHARED WORKFLOW: LIGHTROOM SYNC
# ============================================================

def sync_lightroom(logger=None, ask_first=True):
    """
    Sync Lightroom folder via AppleScript.
    If ask_first=True, shows a confirmation dialog before opening Lightroom.
    Shared by scripts 01, 02, and 03.
    """
    if ask_first:
        confirmed = ask_confirm(
            "Lightroom Sync",
            "Synchronisiere 01-Input-Batchfiles mit Lightroom?\n\nLightroom wird geöffnet – bitte Sync-Dialog bestätigen."
        )
        if not confirmed:
            if logger:
                logger.info("Lightroom Sync durch Nutzer abgebrochen")
            return False

    try:
        import subprocess
        script = (
            # Lightroom öffnen und in den Vordergrund bringen
            'tell application "Adobe Lightroom Classic" to activate\n'
            'delay 5\n'
            'tell application "System Events"\n'
            '    tell process "Adobe Lightroom Classic"\n'
            # Tastenkürzel G = Bibliothek Rasteransicht (kein falscher Klick)
            '        key code 5\n'
            '        delay 1\n'
            # Menü "Bibliothek" → "Ordner synchronisieren..."
            '        click menu item "Ordner synchronisieren..." of menu "Bibliothek" of menu bar 1\n'
            '        delay 2\n'
            # Sync-Dialog automatisch bestätigen
            '        tell window 1\n'
            '            if exists button "Synchronisieren" then\n'
            '                click button "Synchronisieren"\n'
            '            else if exists button "Synchronize" then\n'
            '                click button "Synchronize"\n'
            '            end if\n'
            '        end tell\n'
            '    end tell\n'
            'end tell'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            if logger:
                logger.info("Lightroom Sync abgeschlossen")
            return True
        else:
            if logger:
                logger.warning(f"Lightroom Sync Fehler: {result.stderr}")
            return False
    except Exception as e:
        if logger:
            logger.error(f"Fehler beim Lightroom Sync: {e}")
        return False

def validate_file_exists(file_path, file_type=""):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"{file_type} nicht gefunden: {file_path}")
    return file_path

def validate_directory_exists(dir_path, create_if_missing=False):
    if not os.path.exists(dir_path):
        if create_if_missing:
            os.makedirs(dir_path, exist_ok=True)
            return dir_path
        raise NotADirectoryError(f"Ordner nicht gefunden: {dir_path}")
    return dir_path

def validate_input_not_empty(value, field_name):
    if not value or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field_name} darf nicht leer sein")
    return value

def validate_numeric_input(value, field_name, min_val=None, max_val=None):
    try:
        num = int(value)
        if min_val is not None and num < min_val:
            raise ValueError(f"{field_name} muss größer als {min_val} sein")
        if max_val is not None and num > max_val:
            raise ValueError(f"{field_name} muss kleiner als {max_val} sein")
        return num
    except ValueError:
        raise ValueError(f"{field_name} muss eine Zahl sein")

# ============================================================
# ERROR HANDLING DECORATOR
# ============================================================

def handle_errors(logger=None, default_return=None):
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if logger:
                    logger.error(f"Fehler in {func.__name__}: {str(e)}", exc_info=True)
                else:
                    print(f"Fehler in {func.__name__}: {str(e)}")
                return default_return
        return wrapper
    return decorator

# ============================================================
# API HELPERS
# ============================================================

def get_api_timeout(config):
    return config.get('API_REQUEST_TIMEOUT', 30)

def get_api_delay(config):
    return config.get('API_REQUEST_DELAY', 1)

# ============================================================
# PATH HELPERS
# ============================================================

def get_base_folder():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def get_folder(folder_name):
    return os.path.join(get_base_folder(), folder_name)

def get_paths():
    base = get_base_folder()
    scripts_dir = os.path.join(base, 'scripts')

    # Workspace wird bevorzugt aus config.env (POSTPRO_WORKSPACE) gelesen.
    # Das macht den Pfad unabhaengig davon, von wo das Skript gestartet wird
    # (Terminal, XAMPP, App, Automator, etc.).
    workspace = os.getenv('POSTPRO_WORKSPACE', '').strip()
    if workspace:
        workspace = os.path.expanduser(workspace)
    else:
        # Fallback: Schwester-Ordner 'PostPro Suite' neben dem Skript-Ordner
        workspace = os.path.join(os.path.dirname(base), 'PostPro Suite')
    os.makedirs(workspace, exist_ok=True)
    return {
        'base':             base,
        'workspace':        workspace,
        'scripts':          scripts_dir,
        'input_batchfiles': os.path.join(workspace, '01-Input RAW files'),
        'web_check':        os.path.join(workspace, '02-Webcheck'),
        'upload':           os.path.join(workspace, '03-Upload'),
        'json':             os.path.join(scripts_dir, 'JSON'),
        'logs':             os.path.join(workspace, 'logs'),
    }
