import json
import re
import sqlite3
import os
import time
import urllib.parse
import urllib.request

import anthropic
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "kringum.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL,
            story TEXT,
            tag   TEXT,
            reference TEXT,
            source TEXT,
            gps   TEXT,
            link  TEXT,
            portid INTEGER REFERENCES ports(id)
        )
    """)
    # Add portid column to existing databases that lack it
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "portid" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN portid INTEGER REFERENCES ports(id)")
    if "address" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN address TEXT DEFAULT ''")
    if "geocoded" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN geocoded INTEGER DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            country     TEXT,
            gps         TEXT,
            description TEXT,
            tag         TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Seed default settings
    existing = conn.execute(
        "SELECT key FROM settings WHERE key = 'PROMPT_FILLPORT'"
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("PROMPT_FILLPORT", ""),
        )
    # Seed items with one example row if the table is empty
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO items (name, story, tag, reference, source, gps, link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "Skálholt",
                "Skálholt is one of Iceland's most important historical sites, "
                "located in the southern lowlands near the river Hvítá. For over "
                "700 years, from 1056 until 1785, it served as the seat of one of "
                "Iceland's two bishoprics and was the country's undisputed centre "
                "of learning, culture, and political power. At its peak, Skálholt "
                "was the largest settlement in Iceland with a cathedral, a school, "
                "and dozens of buildings bustling with clergy and students.\n\n"
                "Today, Skálholt is home to a modern cathedral built in 1963, an "
                "excavated medieval tunnel, and a small museum that chronicles the "
                "site's rich past. The grounds host summer concerts and cultural "
                "events that draw visitors from across the country. Surrounded by "
                "the gentle hills and fertile farmland of southern Iceland, "
                "Skálholt remains a place of quiet reflection, connecting modern "
                "Icelanders to the deep roots of their heritage.",
                "Culture",
                "",
                "",
                "64.1272,-20.5269",
                "",
            ),
        )
    # Seed ports from Cruise Europe data
    port_count = conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]
    if port_count == 0:
        from ports_data import PORTS
        conn.executemany(
            "INSERT INTO ports (name, country, gps, description, tag) "
            "VALUES (?, ?, ?, ?, ?)",
            PORTS,
        )
    conn.commit()
    conn.close()


def has_house_number(address):
    """Checks for a standalone 1-5 digit number in the address."""
    return bool(re.search(r'\b\d{1,5}\b', address)) if address else False


def geocode_address(address):
    """Calls Nominatim search API, returns (lat, lon) or None."""
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": address, "format": "json", "limit": "1"}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        app.logger.warning("Geocoding failed for '%s': %s", address, e)
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/items")
def api_items():
    conn = get_db()
    rows = conn.execute("SELECT id, name, story, tag, gps, portid, address, geocoded FROM items").fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    return jsonify(items)


@app.route("/api/ports")
def api_ports():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, country, gps, description, tag FROM ports"
    ).fetchall()
    conn.close()
    ports = [dict(r) for r in rows]
    return jsonify(ports)


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json()
    conn = get_db()
    for key, value in data.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ports/<int:port_id>/items", methods=["GET"])
def api_port_items_count(port_id):
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM items WHERE portid = ?", (port_id,)
    ).fetchone()
    conn.close()
    return jsonify({"count": row["cnt"]})


@app.route("/api/ports/<int:port_id>/fill", methods=["POST"])
def api_port_fill(port_id):
    conn = get_db()
    port = conn.execute(
        "SELECT name, country, gps FROM ports WHERE id = ?", (port_id,)
    ).fetchone()
    if not port:
        conn.close()
        return jsonify({"error": "Port not found"}), 404

    prompt_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'PROMPT_FILLPORT'"
    ).fetchone()
    prompt_template = prompt_row["value"] if prompt_row else ""
    if not prompt_template.strip():
        conn.close()
        return jsonify({"error": "PROMPT_FILLPORT is empty. Configure it in Settings."}), 400

    port_name = port["name"]
    if port["country"]:
        port_name += ", " + port["country"]
    prompt = prompt_template.replace("{port_name}", port_name)

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16384,
            system="Return ONLY the JSON array. No markdown fences, no explanation. Keep each story under 150 words. Each item MUST include a \"gps\" field as a \"lat,lon\" string with real-world decimal GPS coordinates (e.g. \"65.7331,-23.1994\" for Dynjandi). Each item MUST also include an \"address\" field with the street address including house number if applicable (e.g. \"Laugavegur 28, 101 Reykjavik, Iceland\"). Be as accurate as possible. Only generate items about real physical places that exist on a map.",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        conn.close()
        return jsonify({"error": f"Claude API error: {e}"}), 502

    raw = message.content[0].text
    app.logger.info("Claude raw response: %s", raw)
    # Extract JSON array from markdown fences if present
    fence_match = re.search(r"```(?:json)?\s*(\[[\s\S]*\])\s*```", raw)
    json_str = fence_match.group(1) if fence_match else raw.strip()
    # If response still isn't a bare array, try to find one
    if not json_str.startswith("["):
        arr_match = re.search(r"\[[\s\S]*\]", json_str)
        if arr_match:
            json_str = arr_match.group(0)

    try:
        items = json.loads(json_str)
    except json.JSONDecodeError as e:
        conn.close()
        return jsonify({"error": f"Failed to parse Claude response as JSON: {e}", "raw": raw}), 502

    inserted = []
    for item in items:
        address = str(item.get("address", "")).strip()
        gps = ""
        geocoded = False

        # Try geocoding the address via Nominatim
        if address:
            result = geocode_address(address)
            if result:
                gps = f"{result[0]},{result[1]}"
                geocoded = True
                app.logger.info("Geocoded '%s' -> %s", address, gps)
            time.sleep(1.1)  # Nominatim rate limit

        # Fall back to gps field from Claude
        if not gps:
            raw_gps = str(item.get("gps", "")).strip()
            if raw_gps:
                parts = raw_gps.split(",")
                if len(parts) == 2:
                    try:
                        lat = float(parts[0])
                        lon = float(parts[1])
                        gps = f"{lat},{lon}"
                    except ValueError:
                        pass

        # Backward compat: fall back to lat/lon fields
        if not gps:
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
                gps = f"{lat},{lon}"
            except (KeyError, TypeError, ValueError):
                pass

        if not gps:
            app.logger.warning("Missing/invalid coordinates for '%s', item will be unplaced", item.get("name", ""))

        cur = conn.execute(
            "INSERT INTO items (name, story, tag, reference, source, gps, link, portid, address, geocoded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("name", ""),
                item.get("story", ""),
                item.get("tag", ""),
                item.get("reference", ""),
                item.get("source", ""),
                gps,
                item.get("link", ""),
                port_id,
                address,
                1 if geocoded else 0,
            ),
        )
        inserted.append({
            "id": cur.lastrowid,
            "name": item.get("name", ""),
            "story": item.get("story", ""),
            "tag": item.get("tag", ""),
            "gps": gps,
            "portid": port_id,
            "address": address,
            "geocoded": geocoded,
        })
    conn.commit()
    conn.close()
    return jsonify(inserted)


@app.route("/api/ports/<int:port_id>/unplaced", methods=["GET"])
def api_port_unplaced(port_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, tag FROM items WHERE portid = ? AND (gps IS NULL OR gps = '')",
        (port_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/items/<int:item_id>/gps", methods=["PATCH"])
def api_item_gps(item_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon must be numeric"}), 400
    gps = f"{lat},{lon}"
    conn = get_db()
    conn.execute("UPDATE items SET gps = ? WHERE id = ?", (gps, item_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "gps": gps})


@app.route("/api/ports/<int:port_id>/items", methods=["DELETE"])
def api_port_items_delete(port_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM items WHERE portid = ?", (port_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": deleted})


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
