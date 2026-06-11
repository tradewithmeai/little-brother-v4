import os
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import make_server

# Write connection for ingesting extension events (separate from the read-only get_db())
_WRITE_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "little_brother.db")
_write_lock = threading.Lock()


def _write_db():
    path = os.path.abspath(_WRITE_DB_PATH)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "little_brother.db")


def get_db():
    """Open a read-only connection to the database with timeout."""
    path = os.path.abspath(DB_PATH)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def hours_ago(hours):
    """Return ISO timestamp for N hours ago."""
    dt = datetime.utcnow() - timedelta(hours=hours)
    return dt.isoformat()


# --- Flask app ---

app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/browser-tab", methods=["POST"])
def api_browser_tab_ingest():
    """Receive a single tab event from the Firefox extension."""
    # Only accept from localhost
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    event_type = data.get("event_type", "").strip()
    title = (data.get("title") or "")[:500]
    url = (data.get("url") or "")[:2000]

    if not event_type:
        return jsonify({"error": "event_type required"}), 400

    raw_duration = data.get("duration_ms")
    try:
        duration_ms = int(raw_duration) if raw_duration is not None else None
    except (ValueError, TypeError):
        duration_ms = None

    raw_fg = data.get("is_foreground")
    is_foreground = (1 if raw_fg else 0) if raw_fg is not None else None

    ts = datetime.utcnow().isoformat()
    with _write_lock:
        conn = _write_db()
        try:
            conn.execute(
                "INSERT INTO browser_tab_events "
                "(timestamp, browser, event_type, title, url, duration_ms, is_foreground) "
                "VALUES (?, 'firefox', ?, ?, ?, ?, ?)",
                (ts, event_type, title, url, duration_ms, is_foreground),
            )
            conn.commit()
        finally:
            conn.close()

    return jsonify({"ok": True}), 201


@app.route("/api/summary")
def api_summary():
    conn = get_db()
    try:
        result = {}
        for table in ["active_window_events", "mouse_click_events", "browser_tab_events", "file_events", "key_events"]:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts FROM {table}"
            ).fetchone()
            result[table] = {
                "count": row["cnt"],
                "first": row["first_ts"],
                "last": row["last_ts"],
            }

        # Extra keystroke stat: total key count
        ks = conn.execute("SELECT SUM(key_count) as total FROM key_events").fetchone()
        result["key_events"]["total_keys"] = ks["total"] or 0

        db_path = os.path.abspath(DB_PATH)
        result["db_size_kb"] = round(os.path.getsize(db_path) / 1024, 1) if os.path.exists(db_path) else 0
        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/active-windows")
def api_active_windows():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        # Top apps by switch count
        top_apps = conn.execute("""
            SELECT process_name, COUNT(*) as switches,
                   MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
            FROM active_window_events
            WHERE timestamp >= ? AND process_name != ''
            GROUP BY process_name
            ORDER BY switches DESC
            LIMIT 20
        """, (since,)).fetchall()

        # Recent window switches
        recent = conn.execute("""
            SELECT timestamp, window_title, process_name, hwnd
            FROM active_window_events
            WHERE timestamp >= ?
            ORDER BY id DESC
            LIMIT 50
        """, (since,)).fetchall()

        return jsonify({
            "top_apps": [dict(r) for r in top_apps],
            "recent": [dict(r) for r in recent],
        })
    finally:
        conn.close()


@app.route("/api/mouse-clicks")
def api_mouse_clicks():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        # Clicks by button
        by_button = conn.execute("""
            SELECT button, COUNT(*) as cnt
            FROM mouse_click_events
            WHERE timestamp >= ?
            GROUP BY button
            ORDER BY cnt DESC
        """, (since,)).fetchall()

        # Clicks by window
        by_window = conn.execute("""
            SELECT window_title, COUNT(*) as cnt
            FROM mouse_click_events
            WHERE timestamp >= ? AND window_title != ''
            GROUP BY window_title
            ORDER BY cnt DESC
            LIMIT 15
        """, (since,)).fetchall()

        # Click positions for heatmap
        positions = conn.execute("""
            SELECT x, y FROM mouse_click_events
            WHERE timestamp >= ?
        """, (since,)).fetchall()

        return jsonify({
            "by_button": [dict(r) for r in by_button],
            "by_window": [{"title": r["window_title"][:80], "count": r["cnt"]} for r in by_window],
            "positions": [dict(r) for r in positions],
        })
    finally:
        conn.close()


@app.route("/api/file-events")
def api_file_events():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        noise_filter = "src_path NOT LIKE '%betty_seq.json%' AND src_path NOT LIKE '%health.json%'"

        # Events by type
        by_type = conn.execute(f"""
            SELECT event_type, COUNT(*) as cnt
            FROM file_events
            WHERE timestamp >= ? AND {noise_filter}
            GROUP BY event_type
            ORDER BY cnt DESC
        """, (since,)).fetchall()

        # Simpler approach - just get raw paths and group in Python
        raw = conn.execute(f"""
            SELECT src_path, COUNT(*) as cnt
            FROM file_events
            WHERE timestamp >= ? AND {noise_filter}
            GROUP BY src_path
            ORDER BY cnt DESC
        """, (since,)).fetchall()

        # Group by parent directory in Python
        dir_counts = {}
        for r in raw:
            path = r["src_path"].replace("\\", "/")
            parent = "/".join(path.split("/")[:-1]) if "/" in path else path
            dir_counts[parent] = dir_counts.get(parent, 0) + r["cnt"]

        top_dirs_clean = sorted(dir_counts.items(), key=lambda x: -x[1])[:15]

        return jsonify({
            "by_type": [dict(r) for r in by_type],
            "top_dirs": [{"path": d[0], "count": d[1]} for d in top_dirs_clean],
        })
    finally:
        conn.close()


@app.route("/api/browser-tabs")
def api_browser_tabs():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        by_type = conn.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM browser_tab_events
            WHERE timestamp >= ?
            GROUP BY event_type
            ORDER BY cnt DESC
        """, (since,)).fetchall()

        # CDP freshness check (Chrome only)
        cdp_ever = conn.execute(
            "SELECT MAX(timestamp) as last_ts FROM browser_tab_events WHERE browser = 'chrome'"
        ).fetchone()
        cdp_last_ts = cdp_ever["last_ts"] if cdp_ever else None
        if cdp_last_ts:
            cdp_age_min = int(
                (datetime.utcnow() - datetime.fromisoformat(cdp_last_ts)).total_seconds() / 60
            )
            cdp_status = "ok" if cdp_age_min < 30 else ("stale" if cdp_age_min < 240 else "unavailable")
        else:
            cdp_age_min = None
            cdp_status = "unavailable"

        # Only return CDP events when source is fresh; suppress stale/unavailable data
        if cdp_status == "ok":
            cdp_recent = conn.execute("""
                SELECT timestamp, browser, event_type, title, url
                FROM browser_tab_events
                WHERE timestamp >= ? AND browser = 'chrome'
                ORDER BY id DESC LIMIT 30
            """, (since,)).fetchall()
        else:
            cdp_recent = []

        # Firefox and Chrome activity via active window titles
        # Window title format: "Page Title — Mozilla Firefox" or "Page Title - Google Chrome"
        browser_windows = conn.execute("""
            SELECT timestamp, process_name, window_title
            FROM active_window_events
            WHERE timestamp >= ?
              AND (process_name LIKE '%firefox%' OR process_name LIKE '%chrome%'
                   OR process_name LIKE '%msedge%' OR process_name LIKE '%opera%')
              AND window_title != ''
            ORDER BY id DESC
            LIMIT 60
        """, (since,)).fetchall()

        # Top pages by time in focus (deduplicated by title)
        top_pages = conn.execute("""
            SELECT window_title, process_name, COUNT(*) as focus_count
            FROM active_window_events
            WHERE timestamp >= ?
              AND (process_name LIKE '%firefox%' OR process_name LIKE '%chrome%'
                   OR process_name LIKE '%msedge%' OR process_name LIKE '%opera%')
              AND window_title != ''
            GROUP BY window_title, process_name
            ORDER BY focus_count DESC
            LIMIT 20
        """, (since,)).fetchall()

        return jsonify({
            "by_type": [dict(r) for r in by_type],
            "cdp_status": {
                "status": cdp_status,
                "last_event": cdp_last_ts,
                "age_minutes": cdp_age_min,
            },
            "cdp_recent": [dict(r) for r in cdp_recent],
            "browser_windows": [dict(r) for r in browser_windows],
            "top_pages": [dict(r) for r in top_pages],
        })
    finally:
        conn.close()


@app.route("/api/timeline")
def api_timeline():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        # Bucket events by minute for each table
        tables = {
            "windows": ("active_window_events", "timestamp >= ?", (since,)),
            "clicks":  ("mouse_click_events",   "timestamp >= ?", (since,)),
            "tabs":    ("browser_tab_events",    "timestamp >= ?", (since,)),
            "files":   ("file_events",
                        "timestamp >= ? AND src_path NOT LIKE '%betty_seq.json%' AND src_path NOT LIKE '%health.json%'",
                        (since,)),
            "keys":    ("key_events",            "timestamp >= ?", (since,)),
        }
        result = {}
        for key, (table, where, params) in tables.items():
            rows = conn.execute(f"""
                SELECT SUBSTR(timestamp, 1, 16) as minute, COUNT(*) as cnt
                FROM {table}
                WHERE {where}
                GROUP BY minute
                ORDER BY minute
            """, params).fetchall()
            result[key] = [{"minute": r["minute"], "count": r["cnt"]} for r in rows]

        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/keystrokes")
def api_keystrokes():
    hours = float(request.args.get("hours", 24))
    since = hours_ago(hours)
    conn = get_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*) as chunks, SUM(key_count) as total_keys,
                   SUM(CASE WHEN suppressed=1 THEN 1 ELSE 0 END) as suppressed_chunks
            FROM key_events WHERE timestamp >= ?
        """, (since,)).fetchone()

        by_window = conn.execute("""
            SELECT window_title, process_name,
                   COUNT(*) as chunks, SUM(key_count) as keys
            FROM key_events
            WHERE timestamp >= ? AND suppressed = 0 AND window_title != ''
            GROUP BY window_title, process_name
            ORDER BY keys DESC
            LIMIT 15
        """, (since,)).fetchall()

        recent = conn.execute("""
            SELECT timestamp, window_title, process_name, text_chunk, key_count, suppressed
            FROM key_events
            WHERE timestamp >= ?
            ORDER BY id DESC
            LIMIT 40
        """, (since,)).fetchall()

        by_hour = conn.execute("""
            SELECT SUBSTR(timestamp, 1, 13) as hour, SUM(key_count) as keys
            FROM key_events WHERE timestamp >= ?
            GROUP BY hour ORDER BY hour
        """, (since,)).fetchall()

        return jsonify({
            "stats": dict(stats),
            "by_window": [dict(r) for r in by_window],
            "recent": [dict(r) for r in recent],
            "by_hour": [{"hour": r["hour"], "keys": r["keys"]} for r in by_hour],
        })
    finally:
        conn.close()


# --- Server wrapper ---

class DashboardServer:
    """Flask dashboard server that runs in a background thread."""

    def __init__(self, config, orchestrator=None, event_bus=None):
        self.port = config.get("dashboard_port", 5000)
        self._server = None
        self._thread = None

        # Register API blueprint if orchestrator is available
        if orchestrator and event_bus:
            from ..api.routes import create_api_blueprint
            api_bp = create_api_blueprint(orchestrator, event_bus)
            app.register_blueprint(api_bp)

        # Set API key in app config
        app.config["LB_API_KEY"] = config.get("api_key", "")

    def start(self):
        self._server = make_server("127.0.0.1", self.port, app)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[Dashboard] Running at http://localhost:{self.port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        print("[Dashboard] Stopped")
