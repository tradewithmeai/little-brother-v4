# Little Brother v4

A local desktop activity monitor that records what you are doing on your machine and exposes it via a REST API and web dashboard. Designed to be queried by AI agents and tools that need context about recent user activity.

## What it monitors

| Monitor | What is captured |
|---|---|
| Active Window | Which app/window is in focus, process name, every focus change |
| Mouse Clicks | Left/right/middle clicks, coordinates, which window was clicked |
| Browser Tabs | Tab open/close/activate events with URL and title (Chrome via DevTools Protocol) |
| Filesystem | File created/modified/deleted/moved in configured watch directories |

All events are stored in a local SQLite database (`little_brother.db`) via an async write queue.

## Architecture

```
Task Scheduler (at logon)
  └── watchdog  (port 5001 — always-on control plane)
        └── little_brother  (port 5000 — monitoring app)

User session autostart
  └── tray companion  (system tray icon, polls watchdog)
```

Three processes, three roles:
- **Watchdog** (`tools/watchdog.py`) — stable HTTP control surface. Manages LB lifecycle. Betty Sentinel talks to this.
- **LB app** (`python -m little_brother`) — the monitoring process, managed by watchdog.
- **Tray** (`tools/tray.py`) — user-session UI companion. Shows status, provides start/stop/restart via watchdog.

Note: LB runs in the user session (not as a Windows service) because its monitors require access to the interactive desktop — active window detection, mouse/keyboard hooks, and Chrome DevTools all require the user session.

## Project structure

```
little_brother/
├── api/
│   ├── auth.py            # Optional API key authentication
│   └── routes.py          # REST API endpoints
├── dashboard/
│   └── server.py          # Flask web dashboard
├── db/
│   └── database.py        # SQLite manager with async write queue
├── monitors/
│   ├── active_window.py   # Win32 foreground window polling
│   ├── browser_tabs.py    # Chrome DevTools Protocol monitor
│   ├── filesystem.py      # Watchdog-based filesystem events
│   └── mouse_clicks.py    # pynput global click listener
├── betty.py               # Betty Sentinel telemetry (embedded)
├── config.json            # All configuration
├── events.py              # EventBus for real-time pub/sub
└── main.py                # LittleBrother orchestrator
tools/
├── betty_agent.py         # Betty agent (standalone, for testing)
├── install.py             # One-shot Windows setup script
├── tray.py                # System tray companion
└── watchdog.py            # HTTP control plane / process supervisor
tests/
└── test_watchdog.py       # Watchdog unit tests (16 tests)
data/
└── reports/
    └── betty_seq.json     # Betty sequence counter (persisted)
```

## Requirements

- Python 3.10+
- Windows (monitors use Win32 APIs and pynput)
- Chrome with `--remote-debugging-port=9222` for browser tab monitoring

## Installation

```bash
git clone https://github.com/tradewithmeai/little-brother-v4.git
cd little-brother-v4
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### One-shot Windows setup (autostart + tray)

```bash
python tools/install.py
```

This registers the watchdog as a Task Scheduler logon task (auto-restarts on crash) and adds the tray companion to Windows autostart. After this, everything starts automatically on login with no terminal windows.

To uninstall:
```bash
python tools/install.py --uninstall
```

## Running manually

Start the app:
```bash
python -m little_brother
```

Start the watchdog (manages app lifecycle, required for Betty):
```bash
python tools/watchdog.py
```

Start the tray companion:
```bash
pythonw tools/tray.py
```

## Configuration

All configuration lives in `little_brother/config.json`:

```json
{
  "active_window_poll_ms": 500,
  "browser_debug_port": 9222,
  "dashboard_port": 5000,
  "folders_to_watch": [
    "C:/Users/%%USERNAME%%/Desktop",
    "D:/Documents/11Projects",
    "C:/Users/%%USERNAME%%/Downloads"
  ],
  "api_key": "",
  "webhooks": [],
  "watchdog": {
    "port": 5001,
    "app_port": 5000,
    "app_start_command": ["venv/Scripts/python.exe", "-m", "little_brother"],
    "start_timeout_seconds": 15,
    "stop_timeout_seconds": 10,
    "restart_timeout_seconds": 30,
    "auto_start_app": false
  },
  "betty": {
    "enabled": true,
    "url": "http://localhost:8400",
    "agent_id": "lb-desktop",
    "secret_hex": "<your-secret>"
  }
}
```

Set `auto_start_app: true` to have the watchdog automatically launch LB when it starts.

## API reference

### App API (port 5000)

| Endpoint | Description |
|---|---|
| `GET /api/v1/status` | Health, uptime, monitor states, DB queue depth |
| `GET /api/v1/events` | Query events with `hours`, `type`, `search`, `limit` filters |
| `GET /api/v1/context?ts=<ISO>&window=<minutes>` | Activity context at a point in time — returns active processes, window title, browser domains. Falls back to last-known state if no events in window. |
| `GET /api/v1/events/stream` | Server-Sent Events live stream |
| `GET /api/summary` | Row counts and first/last timestamps per event table |
| `POST /api/v1/monitors/{name}/start` | Start a monitor (requires API key) |
| `POST /api/v1/monitors/{name}/stop` | Stop a monitor (requires API key) |
| `POST /api/v1/monitors/start-all` | Start all monitors (requires API key) |
| `POST /api/v1/monitors/stop-all` | Stop all monitors (requires API key) |
| `GET /api/v1/config` | Get current config (requires API key) |
| `PATCH /api/v1/config` | Update config at runtime (requires API key) |

The `/api/v1/context` endpoint is the primary integration point for AI agents correlating network or security events with user activity.

### Watchdog API (port 5001)

The watchdog remains reachable even when LB is down.

| Endpoint | Description |
|---|---|
| `GET /health` | Watchdog liveness check — always 200 |
| `GET /status` | Full status: process state, API reachability, uptime, PID |
| `POST /control/start` | Start LB |
| `POST /control/stop` | Stop LB |
| `POST /control/restart` | Restart LB |
| `POST /control/run-health-check` | Health check without mutating state |

All control responses include a `request_id` for audit correlation and use structured JSON on all paths including errors.

Example status response:
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

On watchdog restart, it discovers an already-running LB process by scanning for the PID listening on the configured app port (using psutil).

## Betty Sentinel integration

Betty Sentinel is a local monitoring server that receives signed telemetry and alerts via Telegram when services go stale.

The Betty agent is embedded in the app and starts automatically with it. Every 60 seconds it:
1. Reads monitor state and last activity directly from the orchestrator
2. Posts a signed heartbeat to `POST http://localhost:8400/ingest/heartbeat`
3. Posts a signed service-state to `POST http://localhost:8400/ingest/service-state`

Status mapping:
- `ok` — all monitors running, activity within 10 minutes
- `degraded` — some monitors not running
- `stale` — all running but no activity for 10+ minutes
- `error` — cannot reach local API

To configure Betty, generate a secret and add it to Betty's `.env`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Add to Betty's .env:
# BETTY_AGENT_SECRET_LB_DESKTOP=<the hex string>
```

Betty should connect to the **watchdog** (`localhost:5001`), not the app API directly. The watchdog exposes the canonical control actions Betty needs (`start_service`, `stop_service`, `restart_service`, `run_health_check`).

A standalone agent for testing without the full app:
```bash
python tools/betty_agent.py
```

## NSM integration

The network-security-monitor queries LB to provide user activity context for AI alert analysis. Use the `/api/v1/context` endpoint:

```
GET http://localhost:5000/api/v1/context?ts=2026-04-17T10:00:00&window=3
```

Returns:
```json
{
  "ts_requested": "2026-04-17T10:00:00",
  "window_minutes": 3,
  "active_window": {
    "source": "window",
    "processes": ["firefox.exe", "WindowsTerminal.exe"],
    "window_title": "little-brother-v4 - VS Code",
    "last_seen": "2026-04-17T09:59:43",
    "seconds_ago": 17
  },
  "browser_tabs": {
    "source": "last_known",
    "domains": ["github.com", "chatgpt.com"],
    "last_seen": "2026-04-17T09:52:11",
    "seconds_ago": 469
  }
}
```

`source: "window"` means events were found in the time window. `source: "last_known"` means LB fell back to the most recent state before the requested timestamp — useful when nothing changed for a long period.

## Dashboard

Web dashboard at `http://localhost:5000`:
- Event counts and activity timeline
- Top applications (bar chart)
- Mouse click distribution (donut chart)
- File activity by type
- Most clicked windows
- Most active directories
- Recent window switches
- Browser tab activity (requires Chrome with `--remote-debugging-port=9222`)

## Running tests

```bash
python -m pytest tests/ -v
```

## Privacy

This tool records detailed activity on the local machine. All data stays local — nothing is sent externally except the Betty Sentinel telemetry (to a local server on the same machine). Ensure you have appropriate permissions before deploying on shared or managed machines.
