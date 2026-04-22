#!/usr/bin/env python3
"""
11 🚫 Web Enabled = Nein
- Liest SKUs via Dialog
- Sucht alle Assets im DAM
- Setzt web_enabled = False für alle gefundenen Assets
"""

import os
import sys
import requests

sys.path.insert(0, os.path.dirname(__file__))
from _utils import load_config, setup_logging, get_dam_token, ask_input, invalidate_dam_token

BASE_URL = "https://api-rs.mycliplister.com/v2.2/apis"

try:
    config = load_config()
    logger = setup_logging(config['LOG_FILE'], config['LOG_LEVEL'])
except Exception as e:
    print(f"FATAL: Konfiguration konnte nicht geladen werden: {e}")
    sys.exit(1)


def get_unique_ids_for_sku(sku):
    """Alle Asset-IDs für eine SKU aus dem DAM holen."""
    token = get_dam_token(config)
    url = f"{BASE_URL}/asset/list"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": 500, "requestkey": sku, "include_meta": "true"}

    r = requests.get(url, headers=headers, params=params,
                     timeout=config['API_REQUEST_TIMEOUT'])

    if r.status_code == 401:
        invalidate_dam_token()
        return get_unique_ids_for_sku(sku)

    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("items", [])
    return [asset["uniqueId"] for asset in items if asset.get("uniqueId")]


def set_web_enabled_false(unique_id):
    """Setzt web_enabled = False für ein Asset."""
    token = get_dam_token(config)
    url = f"{BASE_URL}/asset/update?unique_id={unique_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    r = requests.put(url, headers=headers, json={"webEnabled": False},
                     timeout=config['API_REQUEST_TIMEOUT'])

    if r.ok:
        logger.info(f"  {unique_id}: Web Enabled → Nein")
        return True
    else:
        logger.warning(f"  {unique_id}: Fehler {r.status_code} – {r.text}")
        return False


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("11 🚫 Web Enabled = Nein")
    logger.info("=" * 60)

    user_input = ask_input(
        "🚫 Web Enabled = Nein",
        "SKUs eingeben (durch Leerzeichen oder Zeilenumbrüche getrennt):"
    )

    if not user_input:
        logger.info("Abgebrochen.")
        sys.exit(0)

    skus = [s.strip() for s in user_input.split() if s.strip()]
    if not skus:
        logger.info("Keine SKUs eingegeben.")
        sys.exit(0)

    logger.info(f"{len(skus)} SKU(s) werden verarbeitet...")
    total = 0

    for sku in skus:
        logger.info(f"Suche Assets für SKU {sku}...")
        try:
            unique_ids = get_unique_ids_for_sku(sku)
            if not unique_ids:
                logger.warning(f"  Keine Assets für SKU {sku} gefunden")
                continue
            for uid in unique_ids:
                if set_web_enabled_false(uid):
                    total += 1
        except Exception as e:
            logger.error(f"  Fehler bei SKU {sku}: {e}")

    logger.info("=" * 60)
    logger.info(f"✅ Fertig — {len(skus)} SKU(s), {total} Asset(s) auf Web Enabled = Nein gesetzt.")
    logger.info("=" * 60)
