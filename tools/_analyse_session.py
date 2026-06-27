"""Temporary analysis script — cross-midnight session 2026-05-25 16:00 to 2026-05-26 06:00."""
import sqlite3, sys, io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
DB_PATH = Path(__file__).resolve().parent.parent / "little_brother.db"

PROCESS_CATEGORIES = {
    "firefox.exe": "browser", "chrome.exe": "browser", "msedge.exe": "browser",
    "WindowsTerminal.exe": "dev", "cmd.exe": "dev", "powershell.exe": "dev",
    "Code.exe": "dev", "pycharm64.exe": "dev",
    "WINWORD.EXE": "writing", "EXCEL.EXE": "writing", "POWERPNT.EXE": "writing",
    "Discord.exe": "comms", "slack.exe": "comms", "Zoom.exe": "comms", "Teams.exe": "comms",
    "Spotify.exe": "entertainment", "vlc.exe": "entertainment", "GTA5_Enhanced.exe": "entertainment",
    "explorer.exe": "system", "SearchHost.exe": "system", "Taskmgr.exe": "system",
    "Notepad.exe": "writing", "notepad++.exe": "writing",
}
TITLE_KEYWORDS = {
    "youtube": "entertainment", "netflix": "entertainment", "prime video": "entertainment",
    "film4": "entertainment", "channel 4": "entertainment", "bbc iplayer": "entertainment",
    "e4 live": "entertainment", "twitch": "entertainment",
    "binance": "trading", "coinbase": "trading",
    "claude": "ai_work", "chatgpt": "ai_work",
    "github": "dev", "remotion": "dev", "stackoverflow": "dev",
    "gmail": "comms", "whatsapp": "comms", "telegram": "comms",
}

def categorise(p, t):
    c = PROCESS_CATEGORIES.get(p)
    if c:
        return c
    tl = (t or "").lower()
    for kw, cat in TITLE_KEYWORDS.items():
        if kw in tl:
            return cat
    return "other"

def parse_ts(ts):
    return datetime.fromisoformat(ts)

def fmt(dt):
    return dt.strftime("%H:%M")

# Hard-coded range: 4pm yesterday to 8am today
SESSION_START = "2026-05-25T16:00:00"
SESSION_END   = "2026-05-26T08:00:00"

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

all_windows = cur.execute(
    "SELECT timestamp, window_title, process_name, COALESCE(is_heartbeat, 0) as is_heartbeat "
    "FROM active_window_events "
    "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
    (SESSION_START, SESSION_END)
).fetchall()

if not all_windows:
    print("No window events found in this range.")
    sys.exit(0)

actual_start = all_windows[0]["timestamp"]
actual_end   = all_windows[-1]["timestamp"]

real_count = sum(1 for w in all_windows if not w["is_heartbeat"])
hb_count = len(all_windows) - real_count
print(f"# Little Brother Activity Report")
print(f"Session: {fmt(parse_ts(actual_start))} ({actual_start[:10]}) — {fmt(parse_ts(actual_end))} ({actual_end[:10]})")
print(f"({real_count} focus changes, {hb_count} heartbeats)\n")

# Compress: deduplicate consecutive same-window entries
compressed = []
prev_proc, prev_title, prev_ts = None, None, None
for w in all_windows:
    ts    = w["timestamp"]
    title = w["window_title"] or ""
    proc  = w["process_name"] or ""
    if proc != prev_proc or title != prev_title:
        if prev_ts:
            compressed.append((prev_ts, ts, prev_proc, prev_title))
        prev_proc, prev_title, prev_ts = proc, title, ts
if prev_ts:
    compressed.append((prev_ts, actual_end, prev_proc, prev_title))

cat_minutes = defaultdict(float)
timeline = []
for start_ts, end_ts, proc, title in compressed:
    m   = (parse_ts(end_ts) - parse_ts(start_ts)).total_seconds() / 60
    cat = categorise(proc, title)
    cat_minutes[cat] += m
    if m >= 1:
        timeline.append((start_ts, m, cat, proc, title))

print(f"## Window Timeline ({len(timeline)} entries >=1 min)\n")
for start_ts, m, cat, proc, title in timeline:
    print(f"- {fmt(parse_ts(start_ts))} ({m:.0f}m) [{cat}] {proc} | {title[:80]}")

print()
print("## Time by Category\n")
total = sum(cat_minutes.values())
for cat, mins in sorted(cat_minutes.items(), key=lambda x: -x[1]):
    pct = (mins / total * 100) if total else 0
    bar = "#" * int(pct / 5)
    print(f"- {cat:<15} {mins:>5.0f} min ({pct:>4.0f}%) {bar}")
print(f"\nTotal tracked: {total:.0f} min ({total/60:.1f} hrs)")

# Keystroke summary
key_rows = cur.execute(
    "SELECT timestamp, process_name, window_title, text_chunk, key_count, suppressed "
    "FROM key_events WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
    (actual_start, actual_end)
).fetchall()

if key_rows:
    total_keys = sum(r["key_count"] for r in key_rows)
    suppressed = sum(1 for r in key_rows if r["suppressed"])
    print(f"\n## Keystroke Summary ({total_keys} keys, {len(key_rows)} chunks, {suppressed} suppressed)\n")
    key_by_ctx = defaultdict(int)
    for r in key_rows:
        key_by_ctx[(r["process_name"] or "", r["window_title"] or "")] += r["key_count"]
    print("Top typing contexts:\n")
    for (proc, title), keys in sorted(key_by_ctx.items(), key=lambda x: -x[1])[:15]:
        print(f"  {keys:>5} keys | {proc} | {title[:60]}")
    print("\nKeystroke chunks (non-suppressed, >=10 keys):\n")
    shown = 0
    for r in key_rows:
        if r["suppressed"] or r["key_count"] < 10:
            continue
        chunk = (r["text_chunk"] or "").replace("\n", "<CR>")[:120]
        print(f"  {r['timestamp'][11:16]} [{r['key_count']:>3}k] {r['process_name']} | {chunk}")
        shown += 1
        if shown >= 80:
            print("  ...truncated")
            break

# Mouse clicks by hour
clicks = cur.execute(
    "SELECT substr(timestamp,12,2) as hr, COUNT(*) as n "
    "FROM mouse_click_events WHERE timestamp >= ? AND timestamp <= ? "
    "GROUP BY hr ORDER BY hr",
    (actual_start, actual_end)
).fetchall()
if clicks:
    print("\n## Mouse Clicks by Hour\n")
    for c in clicks:
        bar = "#" * (c["n"] // 10)
        print(f"  {c['hr']}:00  {bar} ({c['n']})")

# File activity
file_rows = cur.execute(
    "SELECT substr(timestamp,12,2) as hr, source_tag, COUNT(*) as n "
    "FROM file_events WHERE timestamp >= ? AND timestamp <= ? "
    "GROUP BY hr, source_tag ORDER BY hr",
    (actual_start, actual_end)
).fetchall()
if file_rows:
    print("\n## File Activity by Hour\n")
    hours = defaultdict(lambda: {"human": 0, "agent_activity": 0})
    for r in file_rows:
        hours[r["hr"]][r["source_tag"]] += r["n"]
    for hr in sorted(hours):
        h = hours[hr]["human"]
        a = hours[hr]["agent_activity"]
        print(f"  {hr}:00  human={h:<5}  agent={a}")

# Top active files
top_files = cur.execute(
    "SELECT src_path, COUNT(*) as n FROM file_events "
    "WHERE timestamp >= ? AND timestamp <= ? AND source_tag='human' AND is_directory=0 "
    "GROUP BY src_path ORDER BY n DESC LIMIT 20",
    (actual_start, actual_end)
).fetchall()
if top_files:
    print("\n## Most Active Files (human)\n")
    for r in top_files:
        parts = r["src_path"].replace("\\", "/").split("/")
        short = "/".join(parts[-3:]) if len(parts) >= 3 else r["src_path"]
        print(f"  {r['n']:>4}  {short}")

conn.close()
print("\n---")
print(f"Source: {DB_PATH}")
