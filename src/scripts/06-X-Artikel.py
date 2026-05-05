"""
06-X-Artikel.py
===============
Automatische X-Artikel-Bilderzuordnung via Jira + Cliplister DAM API.

Workflow:
1. Jira-Tickets mit "X-Artikel vorhanden" abfragen
2. SKU + Hauptartikel aus der Ticket-Beschreibung parsen
3. DAM-API: Bestes Bild pro Hauptartikel finden
4. DAM-API: X-Artikel-SKU dem Asset zuweisen
5. Report ausgeben

Benötigt: config.env mit CLIPLISTER_CLIENT_ID, CLIPLISTER_CLIENT_SECRET,
          JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN
"""

import os
import re
import sys
import json
import logging
import requests
import concurrent.futures
from collections import defaultdict

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from _utils import load_config, setup_logging, get_dam_token, invalidate_dam_token

config = load_config()
logger = setup_logging(config['LOG_FILE'], config['LOG_LEVEL'])

JIRA_SERVER  = config['JIRA_SERVER']
JIRA_EMAIL   = config['JIRA_EMAIL']
JIRA_API_TOKEN = config['JIRA_API_TOKEN']
BASE_URL = "https://api-rs.mycliplister.com/v2.2/apis"

# Done-X-Artikel Kategorie
DONE_X_ARTIKEL_CATEGORY = {
    "id": 661698,
    "parentId": 664480,
    "type": "asset",
    "name": "Done-X-Artikel",
    "titles": [{"value": "Done-X-Artikel", "language": "de"}],
    "activated": True,
    "path": "Status/Done-X-Artikel"
}


# ============================================================
# PHASE 1: JIRA TICKETS ABFRAGEN
# ============================================================

def get_xartikel_tickets():
    """Alle offenen Tickets mit 'X-Artikel vorhanden' Checkbox aus Jira holen."""
    logger.info("Suche Jira-Tickets mit X-Artikel...")

    url = f"{JIRA_SERVER}/rest/api/3/search"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    jql = (
        'project = CREAMEDIA AND issuetype = "Post Production" '
        'AND cf[10307] = "X-Artikel vorhanden" '
        'AND status != "Fertig"'
    )

    params = {
        "jql": jql,
        "fields": "summary,description,status,customfield_10307",
        "maxResults": 50
    }

    response = requests.get(url, params=params, auth=auth)
    response.raise_for_status()
    data = response.json()

    tickets = data.get("issues", [])
    logger.info(f"  {len(tickets)} offene X-Artikel-Tickets gefunden")
    return tickets


# ============================================================
# PHASE 2: BESCHREIBUNG PARSEN
# ============================================================

def parse_description_table(description):
    """
    Extrahiert SKU + Hauptartikel Paare aus der Markdown-Tabelle
    in der Jira-Ticket-Beschreibung.

    Unterstützt:
    - Tabellen mit Header-Zeile (SKU | Hauptartikel | ...)
    - Tabellen ohne Header (numerische Spalten)
    - Diverse Spaltenformate und Trennzeichen
    """
    if not description:
        return []

    lines = description.split("\n")
    table_lines = [line.strip() for line in lines if line.strip().startswith("|")]

    if not table_lines:
        logger.warning("  Keine Tabelle in der Beschreibung gefunden")
        return []

    # Header-Zeile finden
    header_line = table_lines[0] if table_lines else ""
    headers = [h.strip().lower() for h in header_line.split("|") if h.strip()]

    # Spalten-Indizes ermitteln
    sku_idx = None
    hauptartikel_idx = None

    for i, h in enumerate(headers):
        if h in ("sku", "artikelnummer", "artikel"):
            sku_idx = i
        elif h in ("hauptartikel", "main article", "haupt-artikel", "parent"):
            hauptartikel_idx = i

    # Datenzeilen verarbeiten (Trennzeilen überspringen)
    pairs = []
    for line in table_lines[1:]:
        # Trennzeile überspringen (| --- | --- |)
        if re.match(r'^\|[\s\-:]+\|', line):
            continue

        cols = [c.strip() for c in line.split("|") if c.strip()]
        if len(cols) < 2:
            continue

        if sku_idx is not None and hauptartikel_idx is not None:
            # Header-basiertes Parsing
            if sku_idx < len(cols) and hauptartikel_idx < len(cols):
                sku = cols[sku_idx].replace("X", "").strip()
                hauptartikel = cols[hauptartikel_idx].strip()
                if sku.isdigit() and hauptartikel.isdigit():
                    pairs.append({"SKU": sku, "Hauptartikel": hauptartikel})
        else:
            # Fallback: Erste zwei numerische Spalten verwenden
            numeric_cols = [c.replace("X", "").strip() for c in cols if c.replace("X", "").strip().isdigit()]
            if len(numeric_cols) >= 2:
                pairs.append({"SKU": numeric_cols[0], "Hauptartikel": numeric_cols[1]})

    return pairs


def extract_all_pairs(tickets):
    """Alle SKU-Hauptartikel-Paare aus allen Tickets extrahieren."""
    all_pairs = []
    ticket_map = {}  # Mapping: SKU -> Ticket-Key für Report

    for ticket in tickets:
        key = ticket["key"]
        fields = ticket["fields"]
        description = fields.get("description", "") or ""
        summary = fields.get("summary", "")

        logger.info(f"  Verarbeite {key}: {summary}")

        # Jira ADF (Atlassian Document Format) zu Plaintext konvertieren
        if isinstance(description, dict):
            description = adf_to_plaintext(description)

        pairs = parse_description_table(description)
        logger.info(f"    {len(pairs)} SKU-Hauptartikel-Paare gefunden")

        for pair in pairs:
            pair["ticket"] = key
            ticket_map[pair["SKU"]] = key

        all_pairs.extend(pairs)

    return all_pairs, ticket_map


def adf_to_plaintext(adf):
    """Konvertiert Atlassian Document Format (JSON) zu Plaintext mit Markdown-Tabellen."""
    if isinstance(adf, str):
        return adf

    text_parts = []

    def walk(node):
        if isinstance(node, str):
            text_parts.append(node)
            return

        if isinstance(node, dict):
            node_type = node.get("type", "")

            if node_type == "text":
                text_parts.append(node.get("text", ""))
            elif node_type == "hardBreak":
                text_parts.append("\n")
            elif node_type == "tableRow":
                cells = node.get("content", [])
                row_texts = []
                for cell in cells:
                    cell_text = []
                    for child in cell.get("content", []):
                        for inline in child.get("content", []):
                            if inline.get("type") == "text":
                                cell_text.append(inline.get("text", ""))
                    row_texts.append(" ".join(cell_text).strip())
                text_parts.append("| " + " | ".join(row_texts) + " |")
                text_parts.append("\n")
            elif node_type == "table":
                for child in node.get("content", []):
                    walk(child)
            else:
                for child in node.get("content", []):
                    walk(child)

        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(adf.get("content", adf))
    return "\n".join(text_parts)


# ============================================================
# PHASE 3: DAM-API AUTHENTIFIZIERUNG → via _utils
# ============================================================

def get_valid_token():
    """Thread-safe Token via shared cache."""
    return get_dam_token(config)


# ============================================================
# PHASE 4: ASSETS PRO HAUPTARTIKEL ABFRAGEN
# ============================================================

def get_assets_for_sku(sku):
    """Alle Bild-Assets für eine SKU von der DAM-API holen."""
    token = get_valid_token()

    url = (
        f"{BASE_URL}/asset/list"
        f"?limit=1000"
        f"&requestkey={sku}"
        f"&include_meta=true"
        f"&include_directories=true"
        f'&search={{"object_type":"picture"}}'
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 401:
        # Token abgelaufen, nochmal versuchen
        authenticate()
        return get_assets_for_sku(sku)
    else:
        logger.error(f"  DAM-API Fehler für SKU {sku}: {response.status_code}")
        return []


# ============================================================
# PHASE 5: BESTES BILD FINDEN
# ============================================================

def is_web_enabled(asset):
    """Prüft ob das Asset web_enabled = ja/yes hat."""
    for meta in asset.get("customMeta", []):
        if meta.get("cmf_name") == "web_enabled":
            for value in meta.get("cmfvalues", []):
                for label in value.get("labels", []):
                    if label.get("label", "").lower() in ("ja", "yes"):
                        return True
    return False


def is_correct_format(asset, valid_formats=("jpg", "png")):
    """Prüft ob das Asset ein gültiges Bildformat hat."""
    for prop in asset.get("properties", []):
        if prop.get("key") == "format" and prop.get("value", "").lower() in valid_formats:
            return True
    return False


def get_position(asset):
    """Liest die Position aus den Metadaten (als Integer)."""
    for meta in asset.get("customMeta", []):
        if meta.get("cmf_name") == "position":
            try:
                label = meta["cmfvalues"][0]["labels"][0]["label"]
                return int(label)
            except (IndexError, KeyError, ValueError):
                pass
    return float("inf")


def find_best_asset(assets):
    """
    Findet das optimale Asset: web_enabled, korrektes Format,
    niedrigste Position.
    """
    best = None
    lowest_position = float("inf")

    for asset in assets:
        if not is_web_enabled(asset):
            continue
        if not is_correct_format(asset):
            continue

        position = get_position(asset)
        if position < lowest_position:
            lowest_position = position
            best = asset

    return best


# ============================================================
# PHASE 6: X-ARTIKEL-SKU ZUWEISEN
# ============================================================

def update_asset(unique_id, asset_data):
    """Aktualisiert ein Asset in der DAM via API."""
    token = get_valid_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    url = f"{BASE_URL}/asset/update?unique_id={unique_id}"
    response = requests.put(url, headers=headers, json=asset_data)

    if response.status_code in (200, 204):
        return True
    else:
        logger.error(f"  Update fehlgeschlagen für {unique_id}: {response.status_code} - {response.text}")
        return False


def add_xartikel_to_asset(asset, xartikel_sku):
    """
    Fügt die X-Artikel-SKU als Product-Zuordnung zum Asset hinzu
    und setzt die Kategorie 'Done-X-Artikel'.
    """
    unique_id = asset.get("uniqueId")
    if not unique_id:
        return False

    # Bestehende Products beibehalten und X-Artikel-SKU hinzufügen
    products = asset.get("products", [])
    existing_keys = {p.get("requestKey") for p in products}

    if xartikel_sku not in existing_keys:
        products.append({"requestKey": str(xartikel_sku)})

    # Kategorie "Done-X-Artikel" hinzufügen falls nicht vorhanden
    directories = asset.get("directories", [])
    existing_dirs = {d.get("id") for d in directories}

    if DONE_X_ARTIKEL_CATEGORY["id"] not in existing_dirs:
        directories.append(DONE_X_ARTIKEL_CATEGORY)

    # Update-Payload zusammenbauen
    asset["products"] = products
    asset["directories"] = directories

    return update_asset(unique_id, asset)


# ============================================================
# PHASE 7: DOPPELTE POSITIONEN ERKENNEN
# ============================================================

def check_duplicate_positions(all_assets_by_sku):
    """Prüft auf doppelte Positionsnummern pro SKU."""
    duplicates = []

    for sku, assets in all_assets_by_sku.items():
        positions = defaultdict(list)
        for asset in assets:
            if is_web_enabled(asset) and is_correct_format(asset):
                pos = get_position(asset)
                if pos != float("inf"):
                    positions[pos].append(asset.get("uniqueId", "unknown"))

        for pos, ids in positions.items():
            if len(ids) > 1:
                duplicates.append({
                    "SKU": sku,
                    "Position": pos,
                    "Assets": ids,
                    "Count": len(ids)
                })

    return duplicates


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("  06 - X-Artikel Bilderzuordnung")
    print("=" * 60 + "\n")

    # Konfiguration prüfen
    missing = [k for k in ('CLIPLISTER_CLIENT_ID', 'CLIPLISTER_CLIENT_SECRET', 'JIRA_EMAIL', 'JIRA_API_TOKEN')
               if not config.get(k)]
    if missing:
        logger.error(f"Fehlende Config-Werte: {', '.join(missing)}")
        sys.exit(1)

    # Phase 1: Jira-Tickets abfragen
    logger.info("Phase 1: Jira-Tickets abfragen...")
    try:
        tickets = get_xartikel_tickets()
    except Exception as e:
        logger.error(f"Jira-Abfrage fehlgeschlagen: {e}")
        sys.exit(1)

    if not tickets:
        logger.info("Keine offenen X-Artikel-Tickets gefunden. Fertig!")
        return

    # Phase 2: Beschreibungen parsen
    logger.info("Phase 2: Ticket-Beschreibungen parsen...")
    all_pairs, ticket_map = extract_all_pairs(tickets)

    if not all_pairs:
        logger.warning("Keine SKU-Hauptartikel-Paare gefunden!")
        return

    logger.info(f"  Gesamt: {len(all_pairs)} X-Artikel-Zuordnungen aus {len(tickets)} Tickets")
    logger.info("Phase 3: DAM-API Token wird bei erster Abfrage geholt...")

    # Phase 4 + 5: Assets abfragen und bestes Bild finden
    logger.info("Phase 4: Assets abfragen und bestes Bild finden...")

    # Einzigartige Hauptartikel sammeln
    hauptartikel_set = set(p["Hauptartikel"] for p in all_pairs)
    logger.info(f"  {len(hauptartikel_set)} einzigartige Hauptartikel abzufragen")

    # Assets pro Hauptartikel laden
    assets_by_hauptartikel = {}
    all_assets_by_sku = {}

    for ha in hauptartikel_set:
        assets = get_assets_for_sku(ha)
        assets_by_hauptartikel[ha] = assets
        all_assets_by_sku[ha] = assets
        if assets:
            logger.info(f"    {ha}: {len(assets)} Assets gefunden")
        else:
            logger.warning(f"    {ha}: Keine Assets im DAM!")

    # Bestes Bild pro Hauptartikel
    best_assets = {}
    for ha, assets in assets_by_hauptartikel.items():
        best = find_best_asset(assets)
        if best:
            best_assets[ha] = best
            logger.info(f"    {ha}: Bestes Bild = {best.get('uniqueId')} (Pos. {get_position(best)})")
        else:
            logger.warning(f"    {ha}: Kein passendes Bild gefunden (web_enabled + JPG/PNG)!")

    # Phase 6: X-Artikel-SKU zuweisen
    logger.info("Phase 6: X-Artikel-SKUs zuweisen...")

    success_count = 0
    error_count = 0
    skipped_count = 0
    results = []

    def process_pair(pair):
        nonlocal success_count, error_count, skipped_count
        sku = pair["SKU"]
        ha = pair["Hauptartikel"]
        ticket = pair.get("ticket", "?")

        if ha not in best_assets:
            logger.warning(f"    {sku} → {ha}: Kein Bild vorhanden, übersprungen")
            skipped_count += 1
            return {"sku": sku, "hauptartikel": ha, "status": "skipped", "reason": "Kein Bild"}

        asset = best_assets[ha]
        if add_xartikel_to_asset(asset, sku):
            success_count += 1
            logger.info(f"    ✅ {sku} → {ha} ({asset.get('uniqueId')})")
            return {"sku": sku, "hauptartikel": ha, "status": "success", "asset_id": asset.get("uniqueId")}
        else:
            error_count += 1
            logger.error(f"    ❌ {sku} → {ha}: Update fehlgeschlagen")
            return {"sku": sku, "hauptartikel": ha, "status": "error"}

    # Concurrent Processing
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(process_pair, pair) for pair in all_pairs]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    # Phase 7: Doppelte Positionen prüfen
    logger.info("Phase 7: Prüfe auf doppelte Positionen...")
    duplicates = check_duplicate_positions(all_assets_by_sku)

    if duplicates:
        logger.warning(f"  {len(duplicates)} doppelte Positionen gefunden:")
        for dup in duplicates:
            logger.warning(f"    SKU {dup['SKU']}: Position {dup['Position']} → {dup['Count']}x")

    # ============================================================
    # REPORT
    # ============================================================
    print("\n" + "=" * 60)
    print("  ERGEBNIS")
    print("=" * 60)
    print(f"  Tickets verarbeitet:     {len(tickets)}")
    print(f"  X-Artikel-Zuordnungen:   {len(all_pairs)}")
    print(f"  Hauptartikel abgefragt:  {len(hauptartikel_set)}")
    print(f"  ✅ Erfolgreich:           {success_count}")
    print(f"  ⏭️  Übersprungen:          {skipped_count}")
    print(f"  ❌ Fehler:                {error_count}")

    if duplicates:
        print(f"  ⚠️  Doppelte Positionen:  {len(duplicates)}")

    print("=" * 60 + "\n")

    if error_count > 0:
        logger.warning("Es gab Fehler! Prüfe die Ausgabe oben.")
    else:
        logger.info("Alle X-Artikel erfolgreich zugeordnet! 🎉")


if __name__ == "__main__":
    main()
