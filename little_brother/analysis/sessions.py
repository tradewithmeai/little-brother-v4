"""Session stitching — turn the raw event stream into work sessions and
per-project effort.

The collectors record signals in separate tables with no shared notion of a
"session" or a "current project": key_events know the process and window but not
the workspace; file_events know the workspace but carry no keystrokes. This
module reconstructs both.

Approach
--------
1. **Sessions.** Merge the sparse activity signals (keystrokes, window focus,
   clicks) into one timeline and split it wherever there is a gap longer than
   ``gap_minutes``. Each run of activity is one session with a start, end,
   keystroke total and click total.

2. **Project attribution.** Within a session, file_events tell us which
   workspace(s) were being touched. The workspace with the most file activity in
   the session's time span is the session's project. Its keystrokes, clicks and
   duration are attributed to that project.

Sessions with no file activity (pure browsing, comms, media) get no workspace —
that time is deliberately left unattributed rather than billed to a project.

This is an approximation: a session that context-switches between two projects
inside the gap window is credited wholly to its dominant one. Good enough for
per-project effort; a finer split could subdivide on workspace runs later.
"""

from datetime import datetime, timedelta


def _epoch(ts):
    """Parse an ISO timestamp (no tz) to epoch seconds; None on failure."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


# File classes that indicate real human project work (exclude build/data noise).
_WORK_FILE_CLASSES = ("source", "config", "document")


def build_sessions(conn, since_days=30, gap_minutes=15):
    """Reconstruct work sessions with per-session keystrokes, clicks and project.

    Returns a list of session dicts ordered by start time.
    """
    since = (datetime.utcnow() - timedelta(days=since_days)).isoformat()
    gap = gap_minutes * 60

    # --- 1. Sparse activity ticks (define session boundaries) ----------------
    ticks = []  # (epoch, keystrokes, is_click)
    for ts, kc in conn.execute(
        "SELECT timestamp, key_count FROM key_events WHERE timestamp >= ?", (since,)
    ):
        e = _epoch(ts)
        if e is not None:
            ticks.append((e, kc or 0, 0))
    # Only real window SWITCHES count as activity — heartbeats (is_heartbeat=1)
    # merely mean a window kept focus, which is presence, not activity. Counting
    # them would bridge idle gaps (screen left on) into multi-day "sessions".
    for (ts,) in conn.execute(
        "SELECT timestamp FROM active_window_events "
        "WHERE timestamp >= ? AND (is_heartbeat = 0 OR is_heartbeat IS NULL)", (since,)
    ):
        e = _epoch(ts)
        if e is not None:
            ticks.append((e, 0, 0))
    for (ts,) in conn.execute(
        "SELECT timestamp FROM mouse_click_events WHERE timestamp >= ?", (since,)
    ):
        e = _epoch(ts)
        if e is not None:
            ticks.append((e, 0, 1))

    if not ticks:
        return []
    ticks.sort(key=lambda t: t[0])

    # --- 2. Workspace signals over time (for attribution) --------------------
    ws_marks = []  # (epoch, workspace)
    placeholders = ",".join("?" for _ in _WORK_FILE_CLASSES)
    for ts, ws in conn.execute(
        f"""SELECT timestamp, workspace FROM file_events
            WHERE timestamp >= ? AND workspace IS NOT NULL
              AND source_tag = 'human' AND file_class IN ({placeholders})""",
        (since, *_WORK_FILE_CLASSES),
    ):
        e = _epoch(ts)
        if e is not None:
            ws_marks.append((e, ws))
    ws_marks.sort(key=lambda m: m[0])

    # --- 3. Split ticks into sessions ----------------------------------------
    sessions = []
    cur = None
    for e, kc, clk in ticks:
        if cur is None or e - cur["_last"] > gap:
            cur = {"start": e, "end": e, "_last": e,
                   "keystrokes": 0, "clicks": 0, "ticks": 0}
            sessions.append(cur)
        cur["end"] = e
        cur["_last"] = e
        cur["keystrokes"] += kc
        cur["clicks"] += clk
        cur["ticks"] += 1

    # --- 4. Attribute a dominant workspace to each session -------------------
    # Two-pointer walk: ws_marks and sessions are both time-sorted.
    mi = 0
    for s in sessions:
        tally = {}
        # advance to first mark at/after session start
        while mi < len(ws_marks) and ws_marks[mi][0] < s["start"]:
            mi += 1
        j = mi
        while j < len(ws_marks) and ws_marks[j][0] <= s["end"]:
            ws = ws_marks[j][1]
            tally[ws] = tally.get(ws, 0) + 1
            j += 1
        if tally:
            s["workspace"] = max(tally, key=tally.get)
            s["workspace_file_events"] = tally[s["workspace"]]
        else:
            s["workspace"] = None
            s["workspace_file_events"] = 0

    # --- 5. Finalize ---------------------------------------------------------
    out = []
    for s in sessions:
        out.append({
            "start": datetime.utcfromtimestamp(s["start"]).isoformat(),
            "end": datetime.utcfromtimestamp(s["end"]).isoformat(),
            "duration_seconds": int(s["end"] - s["start"]),
            "keystrokes": s["keystrokes"],
            "clicks": s["clicks"],
            "workspace": s["workspace"],
        })
    return out


def project_effort(conn, since_days=30, gap_minutes=15):
    """Roll sessions up per project: active time, keystrokes, clicks, sessions.

    Returns a list of per-workspace dicts, most active time first. Sessions with
    no workspace are aggregated under the ``None`` key as unattributed time.
    """
    sessions = build_sessions(conn, since_days=since_days, gap_minutes=gap_minutes)
    agg = {}
    for s in sessions:
        ws = s["workspace"]
        a = agg.setdefault(ws, {
            "workspace": ws, "active_seconds": 0, "keystrokes": 0,
            "clicks": 0, "sessions": 0, "last_active": None,
        })
        a["active_seconds"] += s["duration_seconds"]
        a["keystrokes"] += s["keystrokes"]
        a["clicks"] += s["clicks"]
        a["sessions"] += 1
        if a["last_active"] is None or s["end"] > a["last_active"]:
            a["last_active"] = s["end"]
    rows = list(agg.values())
    for r in rows:
        r["active_hours"] = round(r["active_seconds"] / 3600.0, 2)
    rows.sort(key=lambda r: r["active_seconds"], reverse=True)
    return rows
