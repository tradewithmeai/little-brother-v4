import json
import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

from .auth import require_api_key

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "little_brother.db")


def get_db():
    path = os.path.abspath(DB_PATH)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def hours_ago(hours):
    dt = datetime.utcnow() - timedelta(hours=hours)
    return dt.isoformat()


def create_api_blueprint(orchestrator, event_bus):
    """Create the API Blueprint with references to the orchestrator and event bus."""

    api = Blueprint("api_v1", __name__)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @api.route("/api/v1/status")
    def api_status():
        monitors = {}
        for name, mon in orchestrator.monitor_map.items():
            monitors[name] = {
                "running": mon.is_running,
                "class": mon.__class__.__name__,
            }

        db_path = os.path.abspath(DB_PATH)
        db_size = round(os.path.getsize(db_path) / 1024, 1) if os.path.exists(db_path) else 0

        return jsonify({
            "running": orchestrator.running,
            "uptime_seconds": orchestrator.uptime_seconds,
            "monitors": monitors,
            "database": {
                "path": db_path,
                "size_kb": db_size,
                "queue_depth": orchestrator.db.event_queue.qsize() if orchestrator.db else 0,
            },
            "dashboard_port": orchestrator.config.get("dashboard_port", 5000),
        })

    # ------------------------------------------------------------------
    # Unified event query
    # ------------------------------------------------------------------

    @api.route("/api/v1/events")
    @require_api_key
    def api_events():
        hours = float(request.args.get("hours", 24))
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        search = request.args.get("search", "").strip()
        event_types = request.args.get("type", "").strip()

        since = hours_ago(hours)
        conn = get_db()
        try:
            results = []

            table_map = {
                "active_window": (
                    "active_window_events",
                    "timestamp, 'active_window' as event_type, window_title, process_name, '' as url, '' as src_path, '' as button",
                    "window_title LIKE ? OR process_name LIKE ?",
                ),
                "mouse_click": (
                    "mouse_click_events",
                    "timestamp, 'mouse_click' as event_type, window_title, '' as process_name, '' as url, '' as src_path, button",
                    "window_title LIKE ?",
                ),
                "browser_tab": (
                    "browser_tab_events",
                    "timestamp, 'browser_tab' as event_type, title as window_title, '' as process_name, url, '' as src_path, '' as button",
                    "title LIKE ? OR url LIKE ?",
                ),
                "file_event": (
                    "file_events",
                    "timestamp, 'file_event' as event_type, '' as window_title, '' as process_name, '' as url, src_path, '' as button",
                    "src_path LIKE ?",
                ),
                "key_events": (
                    "key_events",
                    "timestamp, 'key_events' as event_type, window_title, process_name, '' as url, text_chunk as src_path, '' as button",
                    "window_title LIKE ? OR process_name LIKE ?",
                ),
            }

            if event_types:
                selected = [t.strip() for t in event_types.split(",")]
            else:
                selected = list(table_map.keys())

            unions = []
            params = []
            for etype in selected:
                if etype not in table_map:
                    continue
                table, cols, search_clause = table_map[etype]
                if search:
                    pattern = f"%{search}%"
                    clause_params = [pattern] * search_clause.count("?")
                    unions.append(
                        f"SELECT {cols} FROM {table} WHERE timestamp >= ? AND ({search_clause})"
                    )
                    params.append(since)
                    params.extend(clause_params)
                else:
                    unions.append(f"SELECT {cols} FROM {table} WHERE timestamp >= ?")
                    params.append(since)

            if not unions:
                return jsonify({"events": [], "total": 0})

            sql = " UNION ALL ".join(unions) + f" ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}"
            rows = conn.execute(sql, params).fetchall()
            events = [dict(r) for r in rows]

            return jsonify({"events": events, "count": len(events)})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Context at timestamp (for NSM and AI correlation)
    # ------------------------------------------------------------------

    @api.route("/api/v1/context")
    @require_api_key
    def api_context():
        ts_param = request.args.get("ts", "").strip()
        window = int(request.args.get("window", 3))

        if ts_param:
            try:
                ts_dt = datetime.fromisoformat(ts_param.replace("Z", "+00:00"))
                ts_dt = ts_dt.replace(tzinfo=None)  # normalise to naive UTC (matches DB)
            except ValueError:
                return jsonify({"error": "invalid ts — use ISO format"}), 400
        else:
            ts_dt = datetime.utcnow()

        ts_str = ts_dt.isoformat()
        lo = (ts_dt - timedelta(minutes=window)).isoformat()
        hi = (ts_dt + timedelta(minutes=window)).isoformat()

        conn = get_db()
        try:
            # --- active window ---
            rows = conn.execute(
                "SELECT process_name, window_title, timestamp FROM active_window_events"
                " WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC",
                (lo, hi),
            ).fetchall()

            if rows:
                aw_source = "window"
                aw_processes = list(dict.fromkeys(r["process_name"] for r in rows if r["process_name"]))
                aw_title = rows[0]["window_title"]
                aw_last = rows[0]["timestamp"]
            else:
                # fall back to last known state before the alert
                row = conn.execute(
                    "SELECT process_name, window_title, timestamp FROM active_window_events"
                    " WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                    (hi,),
                ).fetchone()
                aw_source = "last_known"
                aw_processes = [row["process_name"]] if row and row["process_name"] else []
                aw_title = row["window_title"] if row else None
                aw_last = row["timestamp"] if row else None

            if aw_last:
                aw_last_dt = datetime.fromisoformat(aw_last)
                aw_seconds_ago = int((ts_dt - aw_last_dt).total_seconds())
            else:
                aw_seconds_ago = None

            # --- browser tabs ---
            tab_rows = conn.execute(
                "SELECT url, title, timestamp FROM browser_tab_events"
                " WHERE timestamp >= ? AND timestamp <= ? AND url IS NOT NULL ORDER BY timestamp DESC",
                (lo, hi),
            ).fetchall()

            if tab_rows:
                bt_source = "window"
                bt_domains = list(dict.fromkeys(
                    _domain(r["url"]) for r in tab_rows if _domain(r["url"])
                ))
                bt_last = tab_rows[0]["timestamp"]
            else:
                tab_row = conn.execute(
                    "SELECT url, title, timestamp FROM browser_tab_events"
                    " WHERE timestamp <= ? AND url IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
                    (hi,),
                ).fetchone()
                bt_source = "last_known"
                bt_domains = [_domain(tab_row["url"])] if tab_row and _domain(tab_row["url"]) else []
                bt_last = tab_row["timestamp"] if tab_row else None

            if bt_last:
                bt_last_dt = datetime.fromisoformat(bt_last)
                bt_seconds_ago = int((ts_dt - bt_last_dt).total_seconds())
            else:
                bt_seconds_ago = None

            result = {
                "ts_requested": ts_str,
                "window_minutes": window,
                "active_window": {
                    "source": aw_source,
                    "processes": aw_processes,
                    "window_title": aw_title,
                    "last_seen": aw_last,
                    "seconds_ago": aw_seconds_ago,
                },
                "browser_tabs": {
                    "source": bt_source,
                    "domains": bt_domains,
                    "last_seen": bt_last,
                    "seconds_ago": bt_seconds_ago,
                },
            }
            return jsonify(result)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # SSE event stream
    # ------------------------------------------------------------------

    @api.route("/api/v1/events/stream")
    @require_api_key
    def api_event_stream():
        def generate():
            q = queue.Queue()

            def on_event(event):
                q.put(event)

            event_bus.subscribe(on_event)
            try:
                while True:
                    try:
                        event = q.get(timeout=30)
                        yield f"data: {json.dumps(event.to_dict())}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            finally:
                event_bus.unsubscribe(on_event)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Monitor control
    # ------------------------------------------------------------------

    @api.route("/api/v1/monitors/<name>/start", methods=["POST"])
    @require_api_key
    def start_monitor(name):
        monitor = orchestrator.monitor_map.get(name)
        if not monitor:
            return jsonify({"error": f"Unknown monitor: {name}"}), 404
        if monitor.is_running:
            return jsonify({"status": "already_running", "monitor": name})
        monitor.start()
        return jsonify({"status": "started", "monitor": name})

    @api.route("/api/v1/monitors/<name>/stop", methods=["POST"])
    @require_api_key
    def stop_monitor(name):
        monitor = orchestrator.monitor_map.get(name)
        if not monitor:
            return jsonify({"error": f"Unknown monitor: {name}"}), 404
        if not monitor.is_running:
            return jsonify({"status": "already_stopped", "monitor": name})
        monitor.stop()
        return jsonify({"status": "stopped", "monitor": name})

    @api.route("/api/v1/monitors/start-all", methods=["POST"])
    @require_api_key
    def start_all_monitors():
        results = {}
        for name, mon in orchestrator.monitor_map.items():
            if not mon.is_running:
                mon.start()
                results[name] = "started"
            else:
                results[name] = "already_running"
        return jsonify({"status": "ok", "monitors": results})

    @api.route("/api/v1/monitors/stop-all", methods=["POST"])
    @require_api_key
    def stop_all_monitors():
        results = {}
        for name, mon in orchestrator.monitor_map.items():
            if mon.is_running:
                mon.stop()
                results[name] = "stopped"
            else:
                results[name] = "already_stopped"
        return jsonify({"status": "ok", "monitors": results})

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.route("/api/v1/config", methods=["GET"])
    @require_api_key
    def get_config():
        return jsonify(orchestrator.config)

    @api.route("/api/v1/config", methods=["PATCH"])
    @require_api_key
    def update_config():
        updates = request.get_json(silent=True) or {}
        if not updates:
            return jsonify({"error": "No JSON body provided"}), 400

        new_config = orchestrator.update_config(updates)
        restart_needed = []
        if "active_window_poll_ms" in updates:
            restart_needed.append("active_window")
        if "browser_debug_port" in updates:
            restart_needed.append("browser_tabs")
        if "folders_to_watch" in updates:
            restart_needed.append("filesystem")

        return jsonify({"config": new_config, "restart_required": restart_needed})

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    @api.route("/api/v1/webhooks", methods=["GET"])
    @require_api_key
    def list_webhooks():
        hooks = orchestrator.config.get("webhooks", [])
        return jsonify({"webhooks": [{"id": i, "url": url} for i, url in enumerate(hooks)]})

    @api.route("/api/v1/webhooks", methods=["POST"])
    @require_api_key
    def add_webhook():
        data = request.get_json(silent=True) or {}
        url = data.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        webhooks = orchestrator.config.get("webhooks", [])
        if url in webhooks:
            return jsonify({"error": "Webhook already registered"}), 409
        webhooks.append(url)
        orchestrator.update_config({"webhooks": webhooks})

        # Subscribe to event bus for this webhook
        _register_webhook(url, event_bus)

        return jsonify({"status": "registered", "url": url}), 201

    @api.route("/api/v1/webhooks/<int:hook_id>", methods=["DELETE"])
    @require_api_key
    def delete_webhook(hook_id):
        webhooks = orchestrator.config.get("webhooks", [])
        if hook_id < 0 or hook_id >= len(webhooks):
            return jsonify({"error": "Webhook not found"}), 404
        removed = webhooks.pop(hook_id)
        orchestrator.update_config({"webhooks": webhooks})
        return jsonify({"status": "removed", "url": removed})

    # ------------------------------------------------------------------
    # Keystrokes
    # ------------------------------------------------------------------

    @api.route("/api/v1/keystrokes")
    @require_api_key
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
                SELECT timestamp, window_title, process_name,
                       text_chunk, key_count, suppressed
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

    # ------------------------------------------------------------------
    # Digest — single-call activity snapshot for agent consumption
    # ------------------------------------------------------------------

    @api.route("/api/v1/digest")
    @require_api_key
    def api_digest():
        hours = float(request.args.get("hours", 24))
        since = hours_ago(hours)
        conn = get_db()
        try:
            # Summary counts
            counts = {}
            for key, table, col in [
                ("mouse_clicks",    "mouse_click_events",   "COUNT(*)"),
                ("file_events",     "file_events",          "COUNT(*)"),
                ("browser_tab_events", "browser_tab_events", "COUNT(*)"),
                ("keystroke_chunks", "key_events",          "COUNT(*)"),
                ("keystrokes",       "key_events",          "SUM(key_count)"),
            ]:
                row = conn.execute(
                    f"SELECT {col} as v FROM {table} WHERE timestamp >= ?", (since,)
                ).fetchone()
                counts[key] = row["v"] or 0
            # Exclude heartbeats from switch count so it reflects actual focus changes
            sw_row = conn.execute(
                "SELECT COUNT(*) as v FROM active_window_events "
                "WHERE timestamp >= ? AND (is_heartbeat = 0 OR is_heartbeat IS NULL)",
                (since,)
            ).fetchone()
            counts["window_switches"] = sw_row["v"] or 0

            # Top applications by switch count (real focus changes only)
            top_apps = conn.execute("""
                SELECT process_name, COUNT(*) as switches
                FROM active_window_events
                WHERE timestamp >= ? AND process_name != ''
                  AND (is_heartbeat = 0 OR is_heartbeat IS NULL)
                GROUP BY process_name ORDER BY switches DESC LIMIT 10
            """, (since,)).fetchall()

            # Top applications by actual dwell time (gap between consecutive events)
            top_apps_dwell = conn.execute("""
                SELECT process_name,
                       SUM(gap_ms) as total_focus_ms
                FROM (
                    SELECT process_name,
                           CAST(
                               (julianday(LEAD(timestamp) OVER (ORDER BY timestamp))
                                - julianday(timestamp)) * 86400000
                           AS INTEGER) as gap_ms
                    FROM active_window_events
                    WHERE timestamp >= ?
                      AND (is_heartbeat = 0 OR is_heartbeat IS NULL)
                      AND process_name != ''
                )
                WHERE gap_ms IS NOT NULL AND gap_ms > 0 AND gap_ms < 3600000
                GROUP BY process_name
                ORDER BY total_focus_ms DESC LIMIT 10
            """, (since,)).fetchall()

            # Keystroke contexts (top windows by keys typed)
            ks_contexts = conn.execute("""
                SELECT window_title, process_name, SUM(key_count) as keys
                FROM key_events
                WHERE timestamp >= ? AND suppressed = 0 AND window_title != ''
                GROUP BY window_title, process_name
                ORDER BY keys DESC LIMIT 10
            """, (since,)).fetchall()

            # Browser activity from active window events (all browsers)
            browser_activity = conn.execute("""
                SELECT window_title, process_name, COUNT(*) as focus_count
                FROM active_window_events
                WHERE timestamp >= ?
                  AND (process_name LIKE '%firefox%' OR process_name LIKE '%chrome%'
                       OR process_name LIKE '%msedge%')
                  AND window_title != ''
                GROUP BY window_title, process_name
                ORDER BY focus_count DESC LIMIT 15
            """, (since,)).fetchall()

            # Top directories — exclude raw_data and agent activity noise
            signal_paths = conn.execute("""
                SELECT src_path, COUNT(*) as cnt FROM file_events
                WHERE timestamp >= ?
                  AND (file_class IS NULL OR file_class NOT IN ('raw_data', 'directory'))
                  AND source_tag != 'agent_activity'
                GROUP BY src_path ORDER BY cnt DESC
            """, (since,)).fetchall()
            dir_counts = {}
            for r in signal_paths:
                path = r["src_path"].replace("\\", "/")
                parent = "/".join(path.split("/")[:-1]) if "/" in path else path
                dir_counts[parent] = dir_counts.get(parent, 0) + r["cnt"]
            top_dirs = sorted(dir_counts.items(), key=lambda x: -x[1])[:10]

            # Noise summary — raw_data events collapsed by workspace
            noise_rows = conn.execute("""
                SELECT COALESCE(workspace, 'unknown') as workspace,
                       COUNT(*) as event_count
                FROM file_events
                WHERE timestamp >= ? AND file_class = 'raw_data'
                GROUP BY workspace ORDER BY event_count DESC
            """, (since,)).fetchall()
            noise_file_summary = [dict(r) for r in noise_rows]

            # Hourly timeline (bucketed by hour)
            def hourly(table, value_col="COUNT(*)"):
                rows = conn.execute(f"""
                    SELECT SUBSTR(timestamp, 1, 13) as hour, {value_col} as v
                    FROM {table} WHERE timestamp >= ?
                    GROUP BY hour ORDER BY hour
                """, (since,)).fetchall()
                return [{"hour": r["hour"], "count": r["v"]} for r in rows]

            # Bridge status — per-source health for agent consumption
            bridge_sources = {
                "active_window":  "active_window_events",
                "mouse_clicks":   "mouse_click_events",
                "file_events":    "file_events",
                "key_events":     "key_events",
                "browser_tabs_cdp": "browser_tab_events",
            }
            bridge_status = {}
            now_dt = datetime.utcnow()
            for label, table in bridge_sources.items():
                row = conn.execute(
                    f"SELECT COUNT(*) as n, MAX(timestamp) as last_ts "
                    f"FROM {table} WHERE timestamp >= ?", (since,)
                ).fetchone()
                ever_row = conn.execute(
                    f"SELECT MAX(timestamp) as last_ever FROM {table}"
                ).fetchone()
                n = row["n"] or 0
                last_ts = row["last_ts"] or ever_row["last_ever"]
                if last_ts:
                    last_dt = datetime.fromisoformat(last_ts)
                    age_minutes = int((now_dt - last_dt).total_seconds() / 60)
                else:
                    age_minutes = None
                if n == 0 and age_minutes is None:
                    status = "dead"
                elif n == 0 or (age_minutes is not None and age_minutes > 120):
                    status = "stale"
                else:
                    status = "ok"
                bridge_status[label] = {
                    "events_in_period": n,
                    "last_event": last_ts,
                    "last_event_age_minutes": age_minutes,
                    "status": status,
                }

            # Browser source health — Firefox extension vs CDP vs active-window scan
            browser_sources = {}
            for label, where in [
                ("firefox_extension", "browser = 'firefox'"),
                ("cdp_chrome",        "browser = 'chrome'"),
            ]:
                brow = conn.execute(
                    f"SELECT COUNT(*) as n, MAX(timestamp) as last_ts "
                    f"FROM browser_tab_events WHERE timestamp >= ? AND {where}", (since,)
                ).fetchone()
                brow_ever = conn.execute(
                    f"SELECT MAX(timestamp) as last_ever FROM browser_tab_events WHERE {where}"
                ).fetchone()
                n = brow["n"] or 0
                last_ts = brow["last_ts"] or brow_ever["last_ever"]
                if last_ts:
                    age_min = int((now_dt - datetime.fromisoformat(last_ts)).total_seconds() / 60)
                else:
                    age_min = None
                if n == 0 and age_min is None:
                    bstatus = "unavailable"
                elif n == 0 or (age_min is not None and age_min > 60):
                    bstatus = "stale"
                else:
                    bstatus = "ok"
                browser_sources[label] = {
                    "events_in_period": n, "last_event": last_ts,
                    "age_minutes": age_min, "status": bstatus,
                }

            aw_brow = conn.execute("""
                SELECT COUNT(*) as n, MAX(timestamp) as last_ts
                FROM active_window_events
                WHERE timestamp >= ?
                  AND (process_name LIKE '%firefox%' OR process_name LIKE '%chrome%'
                       OR process_name LIKE '%msedge%')
            """, (since,)).fetchone()
            aw_n = aw_brow["n"] or 0
            aw_last = aw_brow["last_ts"]
            if aw_n > 0:
                aw_bstatus = "ok"
                aw_age = int((now_dt - datetime.fromisoformat(aw_last)).total_seconds() / 60)
            elif aw_last:
                aw_age = int((now_dt - datetime.fromisoformat(aw_last)).total_seconds() / 60)
                aw_bstatus = "stale" if aw_age < 120 else "unavailable"
            else:
                aw_age = None
                aw_bstatus = "unavailable"
            browser_sources["active_window_scan"] = {
                "events_in_period": aw_n, "last_event": aw_last,
                "age_minutes": aw_age, "status": aw_bstatus,
            }

            # File activity by workspace and class (human activity only)
            file_by_workspace = conn.execute("""
                SELECT workspace, file_class, COUNT(*) as cnt
                FROM file_events
                WHERE timestamp >= ? AND source_tag = 'human'
                  AND workspace IS NOT NULL AND file_class IS NOT NULL
                GROUP BY workspace, file_class
                ORDER BY cnt DESC LIMIT 30
            """, (since,)).fetchall()

            # Keystroke input method breakdown
            ks_by_method = conn.execute("""
                SELECT input_method, COUNT(*) as chunks, SUM(key_count) as keys
                FROM key_events
                WHERE timestamp >= ? AND suppressed = 0
                GROUP BY input_method
                ORDER BY keys DESC
            """, (since,)).fetchall()

            # Current foreground window (exact state at digest generation time)
            cur_win_row = conn.execute("""
                SELECT timestamp, window_title, process_name, process_path
                FROM active_window_events
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            if cur_win_row:
                cur_win_age = int((now_dt - datetime.fromisoformat(cur_win_row["timestamp"])).total_seconds())
                current_window = {
                    "window_title": cur_win_row["window_title"],
                    "process_name": cur_win_row["process_name"],
                    "last_seen": cur_win_row["timestamp"],
                    "age_seconds": cur_win_age,
                }
            else:
                current_window = None

            # Current active tab (most recent foreground event, ever)
            cur_row = conn.execute("""
                SELECT url, title, tab_id, timestamp
                FROM browser_tab_events
                WHERE is_foreground = 1
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            current_tab = dict(cur_row) if cur_row else None

            # Recent foreground tab visits in period (ordered newest first)
            recent_fg = conn.execute("""
                SELECT url, title, tab_id, timestamp
                FROM browser_tab_events
                WHERE is_foreground = 1
                  AND event_type IN ('activated', 'navigated')
                  AND timestamp >= ?
                ORDER BY timestamp DESC LIMIT 15
            """, (since,)).fetchall()

            # Top URLs by total dwell time in period
            top_dwell = conn.execute("""
                SELECT url, title, COUNT(*) as visits, SUM(duration_ms) as total_dwell_ms
                FROM browser_tab_events
                WHERE event_type = 'dwell' AND timestamp >= ?
                GROUP BY url
                ORDER BY total_dwell_ms DESC LIMIT 15
            """, (since,)).fetchall()

            # Domain-level dwell aggregation
            domain_dwell = {}
            for r in top_dwell:
                d = _domain(r["url"])
                if d:
                    domain_dwell[d] = domain_dwell.get(d, 0) + (r["total_dwell_ms"] or 0)
            top_domains = sorted(domain_dwell.items(), key=lambda x: -x[1])[:10]

            return jsonify({
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "period_hours": hours,
                "summary": counts,
                "current_window": current_window,
                "bridge_status": bridge_status,
                "browser_sources": browser_sources,
                "noise_file_summary": noise_file_summary,
                "file_by_workspace": [dict(r) for r in file_by_workspace],
                "keystroke_input_methods": [dict(r) for r in ks_by_method],
                "current_tab": current_tab,
                "recent_tabs": [dict(r) for r in recent_fg],
                "top_tabs_by_dwell": [
                    {
                        "url": r["url"],
                        "title": r["title"],
                        "visits": r["visits"],
                        "total_dwell_ms": r["total_dwell_ms"],
                    }
                    for r in top_dwell
                ],
                "top_domains_by_dwell": [
                    {"domain": d, "total_dwell_ms": ms} for d, ms in top_domains
                ],
                "top_applications": [
                    {"process": r["process_name"], "switches": r["switches"]}
                    for r in top_apps
                ],
                "top_applications_by_dwell": [
                    {"process": r["process_name"], "total_focus_ms": r["total_focus_ms"]}
                    for r in top_apps_dwell
                ],
                "keystroke_contexts": [
                    {"window": r["window_title"], "process": r["process_name"], "keys": r["keys"]}
                    for r in ks_contexts
                ],
                "browser_activity": [
                    {"title": r["window_title"], "process": r["process_name"], "focus_count": r["focus_count"]}
                    for r in browser_activity
                ],
                "top_directories": [
                    {"path": d[0], "count": d[1]} for d in top_dirs
                ],
                "timeline_hourly": {
                    "windows": hourly("active_window_events"),
                    "clicks":  hourly("mouse_click_events"),
                    "files":   hourly("file_events"),
                    "keys":    hourly("key_events", "SUM(key_count)"),
                },
            })
        finally:
            conn.close()

    return api


def _domain(url: str) -> str | None:
    """Extract hostname from a URL, stripping www. prefix."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") or None
    except Exception:
        return None


def _register_webhook(url, event_bus):
    """Register a webhook URL as an EventBus subscriber."""
    import requests as req_lib

    def send_webhook(event):
        def _post():
            try:
                req_lib.post(url, json=event.to_dict(), timeout=5)
            except Exception:
                pass

        threading.Thread(target=_post, daemon=True).start()

    event_bus.subscribe(send_webhook)
