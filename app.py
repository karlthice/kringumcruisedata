import sqlite3
import os

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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/items")
def api_items():
    conn = get_db()
    rows = conn.execute("SELECT id, name, story, tag, gps, portid FROM items").fetchall()
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
