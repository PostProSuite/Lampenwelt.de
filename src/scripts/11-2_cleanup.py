"""
06 Cleanup
- Leert Input-Batchfiles Ordner
- Leert Webcheck Ordner (05-web-check)
- Leert Upload Ordner (10-Upload)
- Entfernt temporaere Dateien

Damit bleibt der Mac sauber und kein Datenmuell sammelt sich an.
"""

import os
import sys
import shutil
import logging

sys.path.insert(0, os.path.dirname(__file__))
from _utils import load_config, setup_logging, get_paths

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

# Ordner die geleert werden
CLEANUP_DIRS = [
    ('01-Input-Batchfiles', paths['input_batchfiles']),
    ('05-web-check', paths['web_check']),
    ('10-Upload', paths['upload']),
]

# ============================================================
# CLEANUP FUNCTIONS
# ============================================================

def clear_directory(name, path):
    """Clear all contents of a directory, keeping the directory itself."""
    if not os.path.exists(path):
        logger.info(f"  {name}: nicht vorhanden (OK)")
        return 0

    count = 0
    for item in os.listdir(path):
        if item.startswith('.') and item != '.DS_Store':
            continue  # Skip hidden files except .DS_Store

        item_path = os.path.join(path, item)
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.unlink(item_path)
                count += 1
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
                count += 1
        except Exception as e:
            logger.warning(f"  Fehler beim Loeschen von {item}: {e}")

    logger.info(f"  {name}: {count} Objekte entfernt")
    return count


def cleanup_final_images():
    """Remove 08-FINAL-Images folder completely."""
    final_path = os.path.join(paths['base'], "08-FINAL-Images")
    if os.path.exists(final_path):
        try:
            file_count = sum(len(files) for _, _, files in os.walk(final_path))
            shutil.rmtree(final_path)
            logger.info(f"  08-FINAL-Images: {file_count} Dateien geloescht")
            return file_count
        except Exception as e:
            logger.warning(f"  Fehler beim Loeschen von 08-FINAL-Images: {e}")
            return 0
    else:
        logger.info("  08-FINAL-Images: nicht vorhanden (OK)")
        return 0


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    try:
        logger.info("=" * 70)
        logger.info("06 Cleanup gestartet")
        logger.info("=" * 70)

        total = 0

        for name, path in CLEANUP_DIRS:
            total += clear_directory(name, path)

        total += cleanup_final_images()

        logger.info("=" * 70)
        logger.info(f"Cleanup abgeschlossen: {total} Objekte entfernt")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.info("Cleanup durch Benutzer unterbrochen")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Kritischer Fehler: {e}", exc_info=True)
        sys.exit(1)
