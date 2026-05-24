"""
Export a session's activity data as markdown for LLM analysis.

A "session" is all data from the first event of the given date until the next
gap of >2 hours (handles cross-midnight work sessions).

Usage:
    python tools/export_for_analysis.py              # most recent active day
    python tools/export_for_analysis.py 2026-05-14   # specific date
"""

import sqlite3
import sys
import io
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).resolve().parent.parent / "little_brother.db"

PROCESS_CATEGORIES = {
    "firefox.exe":          "browser",
    "chrome.exe":           "browser",
    "msedge.exe":           "browser",
    "WindowsTerminal.exe":  "dev",
    "cmd.exe":              "dev",
    "powershell.exe":       "dev",
    "Code.exe":             "dev",
    "pycharm64.exe":        "dev",
    "WINWORD.EXE":          "writing",
    "EXCEL.EXE":            "writing",
    "POWERPNT.EXE":         "writing",
    "Discord.exe":          "comms",
    "slack.exe":            "comms",
    "Zoom.exe":             "comms",
    "Teams.exe":            "comms",
    "Spotify.exe":          "entertainment",
    "vlc.exe":              "entertainment",
    "GTA5_Enhanced.exe":    "entertainment",
    "explorer.exe":         "system",
    "SearchHost.exe":       "system",
    "Taskmgr.exe":          "system",
    "Notepad.exe":          "writing",
    "notepad++.exe":        "writing",
}

TITLE_KEYWORDS = {
    "youtube":      "entertainment",
    "netflix":      "entertainment",
    "prime video":  "entertainment",
    "film4":        "entertainment",
    "channel 4":    "entertainment",
    "bbc iplayer":  "entertainment",
    "e4 live":      "entertainment",
    "twitch":       "entertainment",
    "binance":      "trading",
    "coinbase":     "trading",
    "claude":       "ai_work",
    "chatgpt":      "ai_work",
    "github":       "dev",
    "remotion":     "dev",
    "stackoverflow":"dev",
    "gmail":        "comms",
    "whatsapp":     "comms",
    "telegram":     "comms",
}


def categorise(process_name, window_title):
    cat = PROCESS_CATEGORIES.get(process_name)
    if cat:
        return cat
    title_lower = (window_title or "").lower()
    for kw, cat in TITLE_KEYWORDS.items():
        if kw in title_lower:
            return cat
    return "other"


def parse_ts(ts):
    return datetime.fromisoformat(ts)


def fmt(dt):
    return dt.strftime("%H:%M")


def run(date=None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if not date:
        row = cur.execute(
            "SELECT substr(timestamp,1,10) as d, COUNT(*) as n "
            "FROM active_window_events GROUP BY d ORDER BY n DESC LIMIT 1"
        ).fetchone()
        date = row["d"] if row else None

    if not date:
        print("No data found.")
        return

    # Fetch all window events for the full calendar date (and up to 4am next day)
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    all_windows = cur.execute(
        "SELECT timestamp, window_title, process_name FROM active_window_events "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (date + "T00:00:00", next_day + "T04:00:00")
    ).fetchall()

    # Split into sessions separated by gaps > 4 hours, take the longest session
    sessions = []
    current = []
    for i, w in enumerate(all_windows):
        if i == 0:
            current.append(w)
            continue
        gap = (parse_ts(w["timestamp"]) - parse_ts(all_windows[i-1]["timestamp"])).total_seconds()
        if gap > 14400:  # 4 hour gap = new session
            sessions.append(current)
            current = []
        current.append(w)
    if current:
        sessions.append(current)

    # Use the longest session (by event count)
    session_windows = max(sessions, key=len) if sessions else []

    if not session_windows:
        print("No window data for this date.")
        return

    session_start = session_windows[0]["timestamp"]
    session_end = session_windows[-1]["timestamp"]
    date_range = f"{session_start[:10]}"
    if session_end[:10] != session_start[:10]:
        date_range += f" → {session_end[:10]}"

    print(f"# Little Brother Activity Report — {date_range}")
    print(f"Session: {fmt(parse_ts(session_start))} – {fmt(parse_ts(session_end))}\n")
    print("*Paste into an LLM for analysis.*\n")

    # ── Compress timeline: deduplicate, drop <1min entries ────────────────────
    compressed = []
    prev_proc, prev_title, prev_ts = None, None, None
    for w in session_windows:
        ts = w["timestamp"]
        title = w["window_title"] or ""
        proc = w["process_name"] or ""
        if proc != prev_proc or title != prev_title:
            if prev_ts:
                compressed.append((prev_ts, ts, prev_proc, prev_title))
            prev_proc, prev_title, prev_ts = proc, title, ts
    if prev_ts:
        compressed.append((prev_ts, session_end, prev_proc, prev_title))

    # Filter to >=1 min and accumulate category time
    cat_minutes = defaultdict(float)
    timeline = []
    for start_ts, end_ts, proc, title in compressed:
        m = (parse_ts(end_ts) - parse_ts(start_ts)).total_seconds() / 60
        cat = categorise(proc, title)
        if m < 60:
            cat_minutes[cat] += m
        if m >= 1:
            timeline.append((start_ts, m, cat, proc, title))

    print(f"## Window Timeline ({len(timeline)} entries ≥1 min)\n")
    for start_ts, m, cat, proc, title in timeline:
        print(f"- `{fmt(parse_ts(start_ts))}` ({m:.0f}m) **{cat}** | {proc} | {title[:70]}")

    print()

    # ── Category breakdown ────────────────────────────────────────────────────
    print("## Time by Category\n")
    total = sum(cat_minutes.values())
    for cat, mins in sorted(cat_minutes.items(), key=lambda x: -x[1]):
        pct = (mins / total * 100) if total else 0
        print(f"- **{cat}**: {mins:.0f} min ({pct:.0f}%)")
    print(f"\n*Total tracked: {total:.0f} min*\n")

    # ── Keystroke summary ─────────────────────────────────────────────────────
    key_rows = cur.execute(
        "SELECT timestamp, process_name, window_title, text_chunk, key_count, suppressed "
        "FROM key_events WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        (session_start, session_end)
    ).fetchall()

    if key_rows:
        total_keys = sum(r["key_count"] for r in key_rows)
        suppressed_count = sum(1 for r in key_rows if r["suppressed"])
        print(f"## Keystroke Summary ({total_keys} total keys, {len(key_rows)} chunks, {suppressed_count} suppressed)\n")
        # Group by process/title, show top contexts by key count
        key_by_ctx = defaultdict(int)
        for r in key_rows:
            ctx = (r["process_name"] or "", r["window_title"] or "")
            key_by_ctx[ctx] += r["key_count"]
        top = sorted(key_by_ctx.items(), key=lambda x: -x[1])[:15]
        print("Top typing contexts:\n")
        for (proc, title), keys in top:
            print(f"- {keys} keys | {proc} | {title[:60]}")
        print()

        # Show non-suppressed chunks with meaningful content
        print("Keystroke chunks (non-suppressed, ≥10 keys):\n")
        shown = 0
        for r in key_rows:
            if r["suppressed"] or r["key_count"] < 10:
                continue
            chunk = (r["text_chunk"] or "").replace("\n", "↵")[:120]
            print(f"- `{r['timestamp'][11:16]}` [{r['key_count']}k] {r['process_name']} | {chunk}")
            shown += 1
            if shown >= 40:
                print("*...truncated*")
                break
        print()

    # ── Mouse clicks by hour ──────────────────────────────────────────────────
    clicks = cur.execute(
        "SELECT substr(timestamp,12,2) as hr, COUNT(*) as n "
        "FROM mouse_click_events WHERE timestamp >= ? AND timestamp <= ? "
        "GROUP BY hr ORDER BY hr",
        (session_start, session_end)
    ).fetchall()

    if clicks:
        print("## Mouse Clicks by Hour\n")
        for c in clicks:
            bar = "█" * (c["n"] // 10)
            print(f"- {c['hr']}:00  {bar} ({c['n']})")
        print()

    # ── File events ───────────────────────────────────────────────────────────
    file_rows = cur.execute(
        "SELECT substr(timestamp,12,2) as hr, source_tag, COUNT(*) as n "
        "FROM file_events WHERE timestamp >= ? AND timestamp <= ? "
        "GROUP BY hr, source_tag ORDER BY hr",
        (session_start, session_end)
    ).fetchall()

    if file_rows:
        print("## File System Activity by Hour\n")
        print("| Hour | Human | Agent |")
        print("|------|-------|-------|")
        hours = defaultdict(lambda: {"human": 0, "agent_activity": 0})
        for r in file_rows:
            hours[r["hr"]][r["source_tag"]] += r["n"]
        for hr in sorted(hours):
            print(f"| {hr}:00 | {hours[hr]['human']} | {hours[hr]['agent_activity']} |")
        print()

    # ── Top active files ──────────────────────────────────────────────────────
    top_files = cur.execute(
        "SELECT src_path, COUNT(*) as n FROM file_events "
        "WHERE timestamp >= ? AND timestamp <= ? AND source_tag='human' AND is_directory=0 "
        "GROUP BY src_path ORDER BY n DESC LIMIT 15",
        (session_start, session_end)
    ).fetchall()

    if top_files:
        print("## Most Active Files (human)\n")
        for r in top_files:
            parts = r["src_path"].replace("\\", "/").split("/")
            short = "/".join(parts[-3:]) if len(parts) >= 3 else r["src_path"]
            print(f"- `{short}` ({r['n']})")
        print()

    conn.close()
    print("---")
    print(f"*Source: {DB_PATH}*")


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(date_arg)
