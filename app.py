import json
import math
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


def _trim_to_sentence(text, limit=4000):
    """Trim text to limit, cutting back to the last complete sentence."""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # Find the last sentence-ending punctuation
    for i in range(len(truncated) - 1, -1, -1):
        if truncated[i] in ".!?":
            return truncated[:i + 1]
    return truncated.rsplit(" ", 1)[0] + "..."


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in kilometres."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_last_nominatim_ts = 0


def _nominatim_search(query, viewbox=None):
    """Single Nominatim API call with automatic rate-limiting.
    Returns (lat, lon) or None."""
    global _last_nominatim_ts
    elapsed = time.time() - _last_nominatim_ts
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_nominatim_ts = time.time()

    try:
        params = {"q": query, "format": "json", "limit": "1"}
        if viewbox:
            params["viewbox"] = viewbox
            params["bounded"] = "0"          # prefer, don't restrict
        url = ("https://nominatim.openstreetmap.org/search?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(
            url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        app.logger.warning("Nominatim search failed for '%s': %s", query, e)
    return None


def _wikipedia_coords(query):
    """Search Wikipedia and return (lat, lon) from the top article, or None."""
    try:
        url = ("https://en.wikipedia.org/w/api.php?"
               + urllib.parse.urlencode({
                   "action": "query",
                   "generator": "search",
                   "gsrsearch": query,
                   "gsrlimit": "1",
                   "prop": "coordinates",
                   "format": "json",
               }))
        req = urllib.request.Request(
            url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            coords = page.get("coordinates")
            if coords:
                return float(coords[0]["lat"]), float(coords[0]["lon"])
    except Exception as e:
        app.logger.warning("Wikipedia lookup failed for '%s': %s", query, e)
    return None


def _claude_geocode(names, port_name, port_country):
    """Ask Claude for GPS coordinates of places it couldn't geocode.
    Returns dict mapping name -> (lat, lon)."""
    if not names:
        return {}
    places_list = "\n".join(f"- {n}" for n in names)
    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=(
                "You are a geocoding assistant. Return ONLY a JSON object mapping "
                "each place name to its GPS coordinates as a [lat, lon] array. "
                "Use coordinates from Wikipedia or other authoritative sources. "
                "If you are not confident about a location, omit it from the result. "
                "No markdown fences, no explanation."
            ),
            messages=[{"role": "user", "content":
                f"Find the GPS coordinates for these places near {port_name}, {port_country}:\n{places_list}"}],
        )
    except Exception as e:
        app.logger.warning("Claude geocode failed: %s", e)
        return {}

    raw = message.content[0].text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    json_str = fence_match.group(1) if fence_match else raw

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError:
        app.logger.warning("Claude geocode returned unparseable JSON")
        return {}

    coords = {}
    for name, val in result.items():
        try:
            if isinstance(val, list) and len(val) == 2:
                coords[name] = (float(val[0]), float(val[1]))
            elif isinstance(val, dict):
                coords[name] = (float(val["lat"]), float(val["lon"]))
        except (KeyError, TypeError, ValueError):
            pass
    return coords


def geocode_place(name, port_name, port_country, port_lat, port_lon):
    """Geocode a named place near a port using cascading Nominatim queries.

    Tries progressively broader queries, each biased toward the port area
    via a Nominatim viewbox.  Rejects results more than 300 km from the port.
    Returns (lat, lon) or None.
    """
    delta = 1.5   # ~150 km padding
    viewbox = (f"{port_lon - delta},{port_lat + delta},"
               f"{port_lon + delta},{port_lat - delta}")

    queries = [
        f"{name}, {port_name}, {port_country}",
        f"{name}, {port_country}",
        name,
    ]

    for query in queries:
        result = _nominatim_search(query, viewbox=viewbox)
        if result:
            dist = haversine_km(port_lat, port_lon, result[0], result[1])
            if dist < 300:
                return result
            app.logger.info(
                "Rejected '%s' result (%.0f km from port)", query, dist)

    return None


def geocode_wikipedia(name, port_name, port_country, port_lat, port_lon):
    """Try Wikipedia API to find coordinates for a named place.
    Returns (lat, lon) or None."""
    queries = [
        f"{name} {port_name} {port_country}",
        f"{name} {port_country}",
        name,
    ]
    for query in queries:
        result = _wikipedia_coords(query)
        if result:
            dist = haversine_km(port_lat, port_lon, result[0], result[1])
            if dist < 300:
                app.logger.info("Wikipedia found '%s' -> %s,%s", name, result[0], result[1])
                return result
            app.logger.info(
                "Wikipedia rejected '%s' result (%.0f km from port)", query, dist)
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
            system="Return ONLY the JSON array. No markdown fences, no explanation. Keep each story under 150 words. Each item's \"name\" MUST be the real, official name of the place exactly as it appears on maps and in travel guides (e.g. \"Hallgrímskirkja\", \"Gullfoss\", \"Museo del Prado\"). Only generate items about real, existing physical places. Do NOT include gps, lat, lon, coordinates, or address fields — locations will be resolved automatically.",
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

    # Parse port coordinates for geocoding context
    port_lat, port_lon = None, None
    if port["gps"]:
        parts = port["gps"].split(",")
        if len(parts) == 2:
            try:
                port_lat, port_lon = float(parts[0]), float(parts[1])
            except ValueError:
                pass

    # --- Tier 1: Nominatim geocoding ---
    resolved = {}   # name -> (lat, lon)
    unresolved = []  # names that need further lookup
    for item in items:
        name = str(item.get("name", "")).strip()
        if name and port_lat is not None:
            result = geocode_place(
                name, port["name"], port["country"] or "", port_lat, port_lon)
            if result:
                resolved[name] = result
                app.logger.info("Tier 1 (Nominatim) '%s' -> %s,%s", name, result[0], result[1])
                continue
        unresolved.append(name)

    # --- Tier 2: Wikipedia API for items Nominatim missed ---
    still_unresolved = []
    for name in unresolved:
        if name and port_lat is not None:
            result = geocode_wikipedia(
                name, port["name"], port["country"] or "", port_lat, port_lon)
            if result:
                resolved[name] = result
                app.logger.info("Tier 2 (Wikipedia) '%s' -> %s,%s", name, result[0], result[1])
                continue
        still_unresolved.append(name)

    # --- Tier 3: Ask Claude for remaining items in one batch ---
    if still_unresolved and port_lat is not None:
        claude_results = _claude_geocode(
            still_unresolved, port["name"], port["country"] or "")
        for name in still_unresolved:
            if name in claude_results:
                lat, lon = claude_results[name]
                dist = haversine_km(port_lat, port_lon, lat, lon)
                if dist < 300:
                    resolved[name] = (lat, lon)
                    app.logger.info("Tier 3 (Claude) '%s' -> %s,%s", name, lat, lon)
                else:
                    app.logger.info(
                        "Tier 3 rejected '%s' (%.0f km from port)", name, dist)

    # --- Insert items, circle-place anything still unresolved ---
    inserted = []
    unplaced_count = 0
    total_unresolved = sum(1 for item in items
                          if str(item.get("name", "")).strip() not in resolved)
    for item in items:
        name = str(item.get("name", "")).strip()

        if name in resolved:
            lat, lon = resolved[name]
            gps = f"{lat},{lon}"
            geocoded = True
        elif port_lat is not None:
            # Arrange unresolved items in a circle around the port
            angle = 2 * math.pi * unplaced_count / max(1, total_unresolved)
            offset = 0.008  # ~800 m radius
            gps = (f"{port_lat + offset * math.cos(angle)},"
                   f"{port_lon + offset * math.sin(angle)}")
            geocoded = False
            unplaced_count += 1
            app.logger.info("Placed '%s' near port (all tiers failed)", name)
        else:
            gps = ""
            geocoded = False

        cur = conn.execute(
            "INSERT INTO items (name, story, tag, reference, source, gps, link, portid, address, geocoded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                item.get("story", ""),
                item.get("tag", ""),
                item.get("reference", ""),
                item.get("source", ""),
                gps,
                item.get("link", ""),
                port_id,
                "",
                1 if geocoded else 0,
            ),
        )
        inserted.append({
            "id": cur.lastrowid,
            "name": name,
            "story": item.get("story", ""),
            "tag": item.get("tag", ""),
            "gps": gps,
            "portid": port_id,
            "address": "",
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


def _wikipedia_extract(title):
    """Fetch the introductory plain-text extract of a Wikipedia article."""
    try:
        url = ("https://en.wikipedia.org/w/api.php?"
               + urllib.parse.urlencode({
                   "action": "query",
                   "titles": title,
                   "prop": "extracts",
                   "exintro": "1",
                   "explaintext": "1",
                   "format": "json",
               }))
        req = urllib.request.Request(
            url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract", "")
            if extract:
                return extract
    except Exception as e:
        app.logger.warning("Wikipedia extract failed for '%s': %s", title, e)
    return ""


def _find_nearest_port(lat, lon):
    """Return the port id of the nearest port to the given coordinates."""
    conn = get_db()
    ports = conn.execute("SELECT id, gps FROM ports").fetchall()
    conn.close()
    best_id = None
    best_dist = float("inf")
    for port in ports:
        coords = port["gps"]
        if not coords:
            continue
        parts = coords.split(",")
        if len(parts) != 2:
            continue
        try:
            plat, plon = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        d = haversine_km(lat, lon, plat, plon)
        if d < best_dist:
            best_dist = d
            best_id = port["id"]
    return best_id


@app.route("/api/wiki/geosearch")
def api_wiki_geosearch():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    try:
        url = ("https://en.wikipedia.org/w/api.php?"
               + urllib.parse.urlencode({
                   "action": "query",
                   "list": "geosearch",
                   "gscoord": f"{lat}|{lon}",
                   "gsradius": "10000",
                   "gslimit": "8",
                   "format": "json",
               }))
        req = urllib.request.Request(
            url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return jsonify({"error": f"Wikipedia API error: {e}"}), 502
    results = data.get("query", {}).get("geosearch", [])
    return jsonify([{
        "pageid": r["pageid"],
        "title": r["title"],
        "lat": r["lat"],
        "lon": r["lon"],
        "dist": r["dist"],
    } for r in results])


@app.route("/api/geo/reverse")
def api_geo_reverse():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    try:
        url = ("https://nominatim.openstreetmap.org/reverse?"
               + urllib.parse.urlencode({
                   "lat": lat, "lon": lon,
                   "format": "json", "zoom": "18",
                   "namedetails": "1",
               }))
        req = urllib.request.Request(
            url, headers={"User-Agent": "KringumCruiseData/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        nd = data.get("namedetails", {})
        name = (nd.get("name")
                or data.get("name")
                or data.get("display_name", "").split(",")[0])
        return jsonify({"name": name.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/claude/import", methods=["POST"])
def api_claude_import():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    try:
        name = str(data["name"]).strip()
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "name, lat, lon required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM items WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"'{name}' already exists"}), 409

    story = ""
    tag = "Culture"
    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "Return ONLY a JSON object with two fields: \"story\" and \"tag\". "
                "\"story\": write a factual, engaging description of the given place "
                "or subject for tourists. Be informative and clear. "
                "Write 4-5 paragraphs, maximum 4000 characters."
                "\"tag\": one of Nature, History, Culture, Photo, Guide — pick "
                "whichever best fits the subject. "
                "No markdown fences, no explanation."
            ),
            messages=[{"role": "user", "content":
                f"Write about: {name} (located at {lat},{lon})"}],
        )
        raw_resp = message.content[0].text.strip()
        fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_resp)
        parsed = json.loads(fence.group(1) if fence else raw_resp)
        story = str(parsed.get("story", ""))[:800]
        raw_tag = str(parsed.get("tag", ""))
        if raw_tag in ("Nature", "History", "Culture", "Photo", "Guide"):
            tag = raw_tag
    except Exception as e:
        app.logger.warning("Claude import failed: %s", e)

    port_id = _find_nearest_port(lat, lon)
    gps = f"{lat},{lon}"
    cur = conn.execute(
        "INSERT INTO items (name, story, tag, reference, source, gps, link, portid, address, geocoded) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, story, tag, "", "Claude", gps, "", port_id, "", 1),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({
        "id": new_id, "name": name, "story": story, "tag": tag,
        "gps": gps, "portid": port_id, "address": "", "geocoded": 1,
    })


@app.route("/api/wiki/import", methods=["POST"])
def api_wiki_import():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    try:
        title = str(data["title"])
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "title, lat, lon required"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM items WHERE name = ?", (title,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"'{title}' already exists"}), 409

    extract = _wikipedia_extract(title)

    story = ""
    tag = "Culture"
    if extract:
        try:
            client = anthropic.Anthropic()
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=(
                    "Return ONLY a JSON object with two fields: \"story\" and \"tag\". "
                    "\"story\": a description of the subject based on the Wikipedia text "
                    "below. Write as an engaging factual writer — informative, clear, "
                    "and interesting without being flowery. Write 4-5 paragraphs, maximum 4000 characters."
                    "\"tag\": one of Nature, History, Culture, Photo, Guide — pick "
                    "whichever best fits the subject. "
                    "No markdown fences, no explanation."
                ),
                messages=[{"role": "user", "content": extract[:6000]}],
            )
            raw_resp = message.content[0].text.strip()
            fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_resp)
            parsed = json.loads(fence.group(1) if fence else raw_resp)
            story = _trim_to_sentence(str(parsed.get("story", "")))
            raw_tag = str(parsed.get("tag", ""))
            if raw_tag in ("Nature", "History", "Culture", "Photo", "Guide"):
                tag = raw_tag
        except Exception as e:
            app.logger.warning("Claude summary failed: %s", e)
            story = _trim_to_sentence(extract)

    port_id = _find_nearest_port(lat, lon)
    gps = f"{lat},{lon}"
    wiki_link = ("https://en.wikipedia.org/wiki/"
                 + urllib.parse.quote(title.replace(" ", "_")))

    cur = conn.execute(
        "INSERT INTO items (name, story, tag, reference, source, gps, link, portid, address, geocoded) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, story, tag, "", "Wikipedia", gps, wiki_link, port_id, "", 1),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({
        "id": new_id, "name": title, "story": story, "tag": tag,
        "gps": gps, "portid": port_id, "address": "", "geocoded": 1,
    })


@app.route("/api/items", methods=["POST"])
def api_item_create():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    tag = str(data.get("tag", "")).strip() or "Culture"
    story = str(data.get("story", "")).strip()
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    gps = f"{lat},{lon}"
    port_id = _find_nearest_port(lat, lon)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO items (name, story, tag, reference, source, gps, link, portid, address, geocoded) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, story, tag, "", "", gps, "", port_id, "", 1),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({
        "id": new_id, "name": name, "story": story, "tag": tag,
        "gps": gps, "portid": port_id, "address": "", "geocoded": 1,
    })


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def api_item_delete(item_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/items/<int:item_id>", methods=["PATCH"])
def api_item_update(item_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    conn = get_db()
    row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Item not found"}), 404
    for field in ("name", "tag", "story"):
        if field in data:
            conn.execute(f"UPDATE items SET {field} = ? WHERE id = ?",
                         (data[field], item_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ports", methods=["POST"])
def api_port_create():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    country = str(data.get("country", "")).strip()
    gps = str(data.get("gps", "")).strip()
    description = str(data.get("description", "")).strip()
    tag = str(data.get("tag", "")).strip()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO ports (name, country, gps, description, tag) VALUES (?, ?, ?, ?, ?)",
        (name, country, gps, description, tag),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({
        "id": new_id, "name": name, "country": country,
        "gps": gps, "description": description, "tag": tag,
    })


@app.route("/api/ports/<int:port_id>", methods=["DELETE"])
def api_port_delete(port_id):
    conn = get_db()
    conn.execute("DELETE FROM items WHERE portid = ?", (port_id,))
    cur = conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Port not found"}), 404
    return jsonify({"ok": True})


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
