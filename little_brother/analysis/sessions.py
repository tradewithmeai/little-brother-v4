"""Session stitching — turn the raw event stream into work sessions and
per-project effort.

The collectors record signals in separate tables with no shared notion of a
"session" or a "current project": key_events know the process and window but not
the workspace; file_events know the workspace but carry no keystrokes. This
module reconstructs both.

Approach
--------
1. **Sessions.** Merge the genuine activity signals (keystrokes, window
   *switches*, clicks) into one timeline and split it wherever there is a gap
   longer than ``gap_minutes``. Window *heartbeats* are excluded — they only
   mean a window kept focus, so counting them would bridge idle time into
   multi-day "sessions".

2. **Project attribution, per run.** Within a session, file_events mark which
   workspace was being touched and when. Each stretch of activity is attributed
   to the workspace that was most recently touched at that moment, so a session
   that moves between two projects is split between them rather than credited
   wholly to one. Workspace names are normalized (aliases + denylist) so repo
   and folder names line up and stray files drop out.

Activity with no known workspace (pure browsing, comms, media, or a project the
file monitor didn't see) is left unattributed rather than billed to a project.
"""

from datetime import datetime, timedelta

from .workspaces import get_workspaces


def _epoch(ts):
    """Parse an ISO timestamp (no tz) to epoch seconds; None on failure."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


# File classes that indicate real human project work (exclude build/data noise).
_WORK_FILE_CLASSES = ("source", "config", "document")


def _load_ticks(conn, since):
    """Genuine-activity ticks: (epoch, keystrokes, clicks). Sorted by time."""
    ticks = []
    for ts, kc in conn.execute(
        "SELECT timestamp, key_count FROM key_events WHERE timestamp >= ?", (since,)
    ):
        e = _epoch(ts)
        if e is not None:
            ticks.append((e, kc or 0, 0))
    # Only real window SWITCHES count — heartbeats are presence, not activity.
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
    ticks.sort(key=lambda t: t[0])
    return ticks


def _load_ws_marks(conn, since, ws_tool):
    """Normalized workspace marks over time: (epoch, workspace). Sorted."""
    marks = []
    placeholders = ",".join("?" for _ in _WORK_FILE_CLASSES)
    for ts, ws in conn.execute(
        f"""SELECT timestamp, workspace FROM file_events
            WHERE timestamp >= ? AND workspace IS NOT NULL
              AND source_tag = 'human' AND file_class IN ({placeholders})""",
        (since, *_WORK_FILE_CLASSES),
    ):
        norm = ws_tool.normalize(ws)
        if norm is None:
            continue
        e = _epoch(ts)
        if e is not None:
            marks.append((e, norm))
    marks.sort(key=lambda m: m[0])
    return marks


def build_sessions(conn, since_days=30, gap_minutes=15, config=None):
    """Reconstruct work sessions with a per-workspace effort breakdown.

    Each session dict carries the dominant workspace (for display) plus a
    ``by_workspace`` list splitting keystrokes/clicks/seconds across the projects
    actually worked on during that session.
    """
    since = (datetime.utcnow() - timedelta(days=since_days)).isoformat()
    gap = gap_minutes * 60
    ws_tool = get_workspaces(config)

    ticks = _load_ticks(conn, since)
    if not ticks:
        return []
    marks = _load_ws_marks(conn, since, ws_tool)

    # Split ticks into raw sessions (list of tick-index ranges).
    sessions = []
    cur = None
    for idx, (e, kc, clk) in enumerate(ticks):
        if cur is None or e - ticks[cur["last_i"]][0] > gap:
            cur = {"start_i": idx, "last_i": idx}
            sessions.append(cur)
        cur["last_i"] = idx

    out = []
    mi = 0  # pointer into marks, advances monotonically across sessions
    for s in sessions:
        si, ei = s["start_i"], s["last_i"]
        s_start = ticks[si][0]
        s_end = ticks[ei][0]

        # Establish the workspace in effect at session start: the most recent
        # mark at/before start, but only if it is within the gap window (older
        # marks belong to a previous session, before the splitting gap).
        while mi < len(marks) and marks[mi][0] <= s_start:
            mi += 1
        current_ws = None
        if mi > 0 and s_start - marks[mi - 1][0] <= gap:
            current_ws = marks[mi - 1][1]
        mj = mi  # marks that fall *inside* the session

        by_ws = {}   # workspace -> {keystrokes, clicks, seconds}
        tally = {}   # workspace -> mark count (for dominant)

        def _bucket(ws):
            return by_ws.setdefault(ws, {"keystrokes": 0, "clicks": 0, "seconds": 0.0})

        for k in range(si, ei + 1):
            t_time, t_keys, t_clk = ticks[k]
            # Apply any workspace marks up to this tick's time.
            while mj < len(marks) and marks[mj][0] <= t_time:
                current_ws = marks[mj][1]
                tally[current_ws] = tally.get(current_ws, 0) + 1
                mj += 1
            b = _bucket(current_ws)
            b["keystrokes"] += t_keys
            b["clicks"] += t_clk
            # Attribute the interval to the NEXT tick to the current workspace.
            if k < ei:
                b["seconds"] += ticks[k + 1][0] - t_time

        dominant = None
        if tally:
            dominant = max(tally, key=tally.get)
        elif current_ws is not None:
            dominant = current_ws

        out.append({
            "start": datetime.utcfromtimestamp(s_start).isoformat(),
            "end": datetime.utcfromtimestamp(s_end).isoformat(),
            "duration_seconds": int(s_end - s_start),
            "keystrokes": sum(b["keystrokes"] for b in by_ws.values()),
            "clicks": sum(b["clicks"] for b in by_ws.values()),
            "workspace": dominant,
            "by_workspace": [
                {"workspace": ws, "keystrokes": b["keystrokes"],
                 "clicks": b["clicks"], "seconds": int(b["seconds"])}
                for ws, b in sorted(by_ws.items(), key=lambda kv: -kv[1]["seconds"])
            ],
        })
    return out


def project_effort(conn, since_days=30, gap_minutes=15, config=None):
    """Roll the per-run session breakdown up per project.

    Returns per-workspace dicts, most active time first. Activity with no
    workspace is aggregated under the ``None`` key as unattributed time.
    """
    sessions = build_sessions(conn, since_days=since_days, gap_minutes=gap_minutes, config=config)
    agg = {}
    for s in sessions:
        for part in s["by_workspace"]:
            ws = part["workspace"]
            a = agg.setdefault(ws, {
                "workspace": ws, "active_seconds": 0, "keystrokes": 0,
                "clicks": 0, "sessions": 0, "last_active": None,
            })
            a["active_seconds"] += part["seconds"]
            a["keystrokes"] += part["keystrokes"]
            a["clicks"] += part["clicks"]
            a["sessions"] += 1
            if a["last_active"] is None or s["end"] > a["last_active"]:
                a["last_active"] = s["end"]
    rows = list(agg.values())
    for r in rows:
        r["active_hours"] = round(r["active_seconds"] / 3600.0, 2)
    rows.sort(key=lambda r: r["active_seconds"], reverse=True)
    return rows
