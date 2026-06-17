# Little Brother v4

A local Windows desktop activity monitor that captures what you are doing on your machine and exposes it via a REST API, web dashboard, MCP server, and real-time event stream. Designed to give AI agents, security monitors, and personal analytics tools accurate context about recent user activity.

---

## What it monitors

| Monitor | What is captured |
|---|---|
| **Active Window** | Every focus change — foreground app, window title, process name, HWND |
| **Mouse Clicks** | Left/right/middle clicks, screen coordinates, which window was clicked |
| **Keyboard** | Keystroke chunks buffered per window context — key count, text (suppressed for password managers) |
| **Browser Tabs** | Tab open/close/activate events with URL and title (Chrome via DevTools Protocol) |
| **Filesystem** | File created/modified/deleted/moved across configured watch paths, tagged as human or agent activity |

All events are written asynchronously to a local SQLite database (`little_brother.db`) via a thread-safe batch-commit queue. WAL mode is enabled for concurrent read performance.

---

## Architecture

```
Windows logon (registry Run key)
  └── start.bat
        ├── LB App          pythonw.exe -m little_brother     (port 5000)
        ├── Watchdog        pythonw.exe tools/watchdog.py     (port 5001)
        └── Tray            pythonw.exe tools/tray.py

Remote access (SSH reverse tunnel)
  └── VPS port 5001 → local port 5000   (Hermes agent)
```

Three always-on processes, all windowless (`pythonw.exe`):

- **App** (`python -m little_brother`) — runs all five monitors, serves the dashboard and API.
- **Watchdog** (`tools/watchdog.py`) — independent process supervisor. Polls the app every 30 s and auto-restarts on crash. Exposes its own HTTP control API on port 5001. Betty Sentinel and NSM connect here.
- **Tray** (`tools/tray.py`) — system tray icon. Polls the watchdog for status; right-click menu for open dashboard / start / stop / restart.

The app runs in the **user session**, not as a Windows service, because its monitors require interactive-desktop access — active window detection, keyboard hooks, and Chrome DevTools all need the logged-in session.

A single-instance lock (socket bound to port 47923) prevents duplicate app processes. The watchdog discovers an already-running app on startup by scanning for the PID listening on the configured app port.

---

## Project structure

```
little_brother/
├── __main__.py              Entry point; single-instance lock; signal handlers
├── main.py                  LittleBrother orchestrator (start / stop / config)
├── config.json              All configuration
├── events.py                EventBus — pub/sub for real-time event delivery
├── betty.py                 Betty Sentinel telemetry agent (embedded)
├── mcp_server.py            MCP server — exposes monitoring data as AI tools
├── api/
│   ├── auth.py              @require_api_key decorator (X-API-Key header)
│   └── routes.py            /api/v1/* endpoints (status, events, digest, keystrokes, context, stream, control)
├── dashboard/
│   ├── server.py            Flask app; serves dashboard UI and /api/* summary endpoints
│   └── static/index.html   Surveillance-terminal dashboard (Bebas Neue + JetBrains Mono)
├── db/
│   ├── database.py          SQLite manager; WAL mode; async batch-commit writer thread
│   └── schema.sql           Table definitions (5 event tables)
└── monitors/
    ├── active_window.py     Win32 GetForegroundWindow polling
    ├── mouse_clicks.py      pynput global mouse listener
    ├── keyboard.py          pynput keyboard listener; buffered chunks; suppression list
    ├── browser_tabs.py      Chrome DevTools Protocol (CDP) over HTTP
    └── filesystem.py        watchdog ReadDirectoryChangesW; ActivityTagger (human vs agent)

tools/
├── watchdog.py              Process supervisor + crash-recovery loop + HTTP control API
├── tray.py                  System tray companion (pystray + Pillow)
├── export_for_analysis.py   Export a day's session as markdown for LLM analysis
├── install.py               One-shot Windows setup script
└── betty_agent.py           Standalone Betty agent for testing without the full app

tests/
└── test_watchdog.py         Watchdog unit tests (16 tests)

data/
└── reports/
    └── betty_seq.json       Betty sequence counter (persisted across restarts)
```

---

## Database schema

Five tables in `little_brother.db`:

| Table | Key columns |
|---|---|
| `active_window_events` | `timestamp`, `window_title`, `process_name`, `hwnd` |
| `mouse_click_events` | `timestamp`, `x`, `y`, `button`, `window_title` |
| `key_events` | `timestamp`, `window_title`, `process_name`, `text_chunk`, `key_count`, `suppressed` |
| `browser_tab_events` | `timestamp`, `browser`, `event_type`, `title`, `url` |
| `file_events` | `timestamp`, `event_type`, `src_path`, `dest_path`, `source_tag` |

`source_tag` on `file_events` is `'human'` or `'agent_activity'` — the ActivityTagger classifies events by path patterns and write velocity.

---

## Requirements

- Python 3.10+
- Windows (monitors use Win32 APIs, pynput, and Chrome DevTools)
- Chrome launched with `--remote-debugging-port=9222` for browser tab monitoring (optional — all other monitors work without it)

---

## Installation

```bash
git clone https://github.com/tradewithmeai/little-brother-v4.git
cd little-brother-v4
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### Autostart setup

The registry `Run` key fires `start.bat` at every user login:

```powershell
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $regPath -Name "LittleBrother" -Value "`"D:\path\to\little-brother-v4\start.bat`""
```

Or run the one-shot installer:
```bash
python tools/install.py
```

---

## Running manually

```bash
# App only
venv\Scripts\python.exe -m little_brother

# App + watchdog + tray (matches what start.bat does)
start.bat
```

---

## Configuration (`little_brother/config.json`)

```json
{
  "active_window_poll_ms": 1000,
  "browser_debug_port": 9222,
  "dashboard_port": 5000,
  "folders_to_watch": [
    "C:/Users/%%USERNAME%%/Desktop",
    "D:/Documents/11Projects",
    "C:/Users/%%USERNAME%%/Downloads"
  ],
  "api_key": "<32-byte hex — required for all data endpoints>",
  "webhooks": [],
  "watchdog": {
    "port": 5001,
    "app_port": 5000,
    "app_start_command": ["venv/Scripts/pythonw.exe", "-m", "little_brother"],
    "start_timeout_seconds": 15,
    "stop_timeout_seconds": 10,
    "restart_timeout_seconds": 30,
    "restart_check_interval_seconds": 30,
    "auto_start_app": true
  },
  "betty": {
    "enabled": false,
    "url": "http://localhost:8400",
    "agent_id": "lb-desktop",
    "secret_hex": "<your-secret>"
  }
}
```

Generate an API key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## API reference

### Authentication

All data endpoints require `X-API-Key: <api_key>` header (or `?api_key=` query param). The `/api/v1/status` and watchdog `/health` endpoints are public — safe for health-checking without credentials.

If `api_key` is empty in config, authentication is disabled (development only).

---

### App API — port 5000

#### Public

| Endpoint | Description |
|---|---|
| `GET /api/v1/status` | System health: running state, uptime, per-monitor status, DB queue depth, DB size |

#### Data endpoints (require API key)

| Endpoint | Key params | Description |
|---|---|---|
| `GET /api/v1/digest` | `hours` | **Primary agent endpoint.** Single-call activity snapshot: summary counts, top apps, keystroke contexts, browser activity, top directories, hourly timeline. Text chunks excluded. |
| `GET /api/v1/keystrokes` | `hours` | Per-window typing stats, recent chunks (with text), hourly key counts, suppression stats |
| `GET /api/v1/events` | `hours`, `type`, `search`, `limit`, `offset` | Unified event query across all tables. `type` accepts: `active_window`, `mouse_click`, `browser_tab`, `file_event`, `key_events` |
| `GET /api/v1/context` | `ts` (ISO), `window` (minutes) | Activity at a point in time — active processes, window title, browser domains. Falls back to last-known state. |
| `GET /api/v1/events/stream` | — | Server-Sent Events live stream of all events as they are written |

#### Control endpoints (require API key)

| Endpoint | Description |
|---|---|
| `POST /api/v1/monitors/{name}/start` | Start a named monitor (`active_window`, `mouse_clicks`, `browser_tabs`, `filesystem`, `keyboard`) |
| `POST /api/v1/monitors/{name}/stop` | Stop a named monitor |
| `POST /api/v1/monitors/start-all` | Start all monitors |
| `POST /api/v1/monitors/stop-all` | Stop all monitors |
| `GET /api/v1/config` | Get current config |
| `PATCH /api/v1/config` | Update config at runtime (changes written to config.json) |
| `GET /api/v1/webhooks` | List registered webhook URLs |
| `POST /api/v1/webhooks` | Register a webhook URL (receives all events via POST) |
| `DELETE /api/v1/webhooks/{id}` | Remove a webhook |

#### Dashboard summary endpoints (no auth — dashboard use only)

| Endpoint | Description |
|---|---|
| `GET /api/summary` | Row counts + first/last timestamps for all 5 event tables |
| `GET /api/active-windows` | Top apps by switch count + recent window events |
| `GET /api/mouse-clicks` | Clicks by button, by window, and XY positions |
| `GET /api/file-events` | Events by type + top active directories |
| `GET /api/browser-tabs` | Tab events + browser window activity from active_window_events |
| `GET /api/keystrokes` | Keystroke contexts + recent chunks (dashboard panel) |
| `GET /api/timeline` | Per-minute event counts for all 5 monitors |

All endpoints accept `?hours=N` (default 24).

---

### Watchdog API — port 5001

The watchdog remains reachable even when the app is down.

| Endpoint | Description |
|---|---|
| `GET /health` | Always 200 — watchdog liveness check |
| `GET /status` | Full status: process state, API reachability, uptime, PID |
| `POST /control/start` | Start the app |
| `POST /control/stop` | Stop the app |
| `POST /control/restart` | Restart the app |
| `POST /control/run-health-check` | Trigger health check, return result |

Status response:
```json
{
  "service_name": "little_brother",
  "process_state": "running",
  "api_reachable": true,
  "status": "ok",
  "uptime_seconds": 3600,
  "detail": { "pid": 12345, "discovered": false }
}
```

`status` values: `ok` | `degraded` (process up, API unreachable) | `failed` (process down).

Control responses include a `request_id` (UUID) for audit correlation.

---

### MCP server

`little_brother/mcp_server.py` exposes monitoring data as MCP tools for AI assistants (Claude, Cursor, etc.).

```bash
python -m little_brother.mcp_server
```

Tools available:
- `get_activity_summary(hours)` — total counts per event type, DB size
- `get_active_windows(hours, limit)` — top apps + recent window switches
- `get_mouse_clicks(hours)` — click distribution
- `get_browser_activity(hours, limit)` — tab events + recent tabs
- `get_file_activity(hours)` — file events by type + top active dirs
- `search_events(query, hours, limit)` — full-text search across all event tables
- `get_system_status()` — health via watchdog API
- `control_monitor(name, action)` — start/stop monitors via API
- `get_config()` — current configuration
- `update_config(settings)` — update config at runtime

MCP resource: `lb://activity/summary` — human-readable text summary of the last hour.

---

## Dashboard

Web dashboard at `http://localhost:5000`.

Surveillance-terminal aesthetic — Bebas Neue headers, JetBrains Mono data, phosphor green on near-black, CRT scanline overlay.

Panels:
- **5 stat cards** — window switches, mouse clicks, file events, browser tabs, total keystrokes
- **Activity timeline** — per-minute counts for all 5 monitors overlaid as a line chart
- **Top Applications** — horizontal bar chart by window-switch count
- **Keystroke Contexts** — ranked bar meters showing which windows received the most typing
- **Recent Transmissions** — keystroke chunk feed; click any entry to reveal the text; suppressed entries shown as `[CLASSIFIED]`
- **File Activity** — event type breakdown (donut) + top active directories
- **Mouse Clicks** — button distribution (donut) + most-clicked windows
- **Browser Tabs** — top pages by focus time + recent browser window events (works with Firefox and Chrome; Chrome CDP events shown separately when available)

Time range selector (1h / 4h / 12h / 24h / 7d) with 30-second auto-refresh.

---

## Agent integrations

### Hermes (PA / digital-me agent on VPS)

Hermes connects via an SSH reverse tunnel:

```
VPS port 5001 ←→ local port 5000
ssh -N -R 5001:127.0.0.1:5000 root@<vps-ip>
```

`tools/tunnel_keeper.py` maintains the tunnel with auto-reconnect (runs via `start.bat`). Forwards port 5001 (LB) and 5055 (Social Monitor) to all configured Hermes VPS instances.

Primary endpoint for Hermes: `GET /api/v1/digest?hours=N` — returns the full activity picture in a single authenticated call.

Required headers: `X-API-Key: <api_key>`

### Betty Sentinel

Betty is a local monitoring server that receives signed telemetry and sends Telegram alerts when services go stale.

The Betty agent is embedded in the app (`little_brother/betty.py`) and runs automatically. Every 60 seconds it:
1. Reads monitor state and last-activity timestamp from the orchestrator
2. Posts a signed HMAC-SHA256 heartbeat to `POST http://localhost:8400/ingest/heartbeat`
3. Posts a signed service-state to `POST http://localhost:8400/ingest/service-state`

Status mapping:

| Status | Condition |
|---|---|
| `ok` | All monitors running, activity within 10 minutes |
| `degraded` | Some monitors not running |
| `stale` | All running but no activity for 10+ minutes |
| `error` | Cannot reach local API |

Betty connects to the **watchdog** (`localhost:5001`) for control actions, not the app directly.

To configure: generate a secret and add it to Betty's `.env`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# BETTY_AGENT_SECRET_LB_DESKTOP=<hex>
```

Standalone test agent (no full app required):
```bash
python tools/betty_agent.py
```

### NSM (Network Security Monitor)

The NSM queries `/api/v1/context` to correlate network alerts with user activity:

```
GET http://localhost:5000/api/v1/context?ts=2026-05-24T10:00:00&window=3
X-API-Key: <api_key>
```

Response:
```json
{
  "ts_requested": "2026-05-24T10:00:00",
  "window_minutes": 3,
  "active_window": {
    "source": "window",
    "processes": ["firefox.exe", "WindowsTerminal.exe"],
    "window_title": "little-brother-v4 — VS Code",
    "last_seen": "2026-05-24T09:59:43",
    "seconds_ago": 17
  },
  "browser_tabs": {
    "source": "last_known",
    "domains": ["github.com"],
    "last_seen": "2026-05-24T09:52:11",
    "seconds_ago": 469
  }
}
```

`source: "window"` — events found in the time window.  
`source: "last_known"` — fallback to most recent state before the timestamp.

---

## Daily analysis export

`tools/export_for_analysis.py` exports a day's longest work session as markdown for pasting into an LLM:

```bash
python tools/export_for_analysis.py              # most recent active day
python tools/export_for_analysis.py 2026-05-24   # specific date
```

Output includes: compressed window timeline, category breakdown, keystroke summary (top contexts + chunks ≥10 keys), mouse clicks by hour, filesystem activity by hour, most active files.

Session detection uses a 4-hour gap algorithm — cross-midnight sessions are handled correctly.

---

## Keyboard monitor — privacy

The keyboard monitor buffers keystrokes per window context and writes chunks on Enter, idle timeout (5 s), or 500-character limit.

Suppression: any window whose process name matches a known password manager (KeePass, 1Password, Bitwarden, etc.) or whose title contains credential-related fragments (`password`, `sign in`, `2fa`, etc.) is suppressed — the text chunk is stored as `[SUPPRESSED]` and the `suppressed` column is set to 1.

All keystroke data is local only. The `/api/v1/digest` endpoint intentionally excludes `text_chunk` values — raw text is only available via `/api/v1/keystrokes` or `/api/v1/events?type=key_events`, both of which require the API key.

---

## Security

- Dashboard and API bind to `127.0.0.1` only — not reachable from the LAN.
- All data endpoints require `X-API-Key` header. Only `/api/v1/status` and `/health` are public.
- Dashboard HTML escapes all dynamic content — keystroke text containing `<script>` tags cannot inject.
- Remote access via SSH reverse tunnel (not an open port).

---

## Running tests

```bash
venv\Scripts\python.exe -m pytest tests/ -v
```
