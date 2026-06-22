"""
Little Brother tray companion.

Runs in the user session via pythonw.exe — no console window.
Polls the watchdog (localhost:5001) every 30s and shows status in the tray.
All control actions route through the watchdog, never directly to LB.

Start: pythonw tools/tray.py
"""

import os
import sys
import threading
import time
import traceback
import webbrowser
import winreg
from datetime import datetime
from pathlib import Path

import pystray
import requests
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = ROOT / "little_brother" / "logs"
WATCHDOG_URL = "http://localhost:5001"
DASHBOARD_URL = "http://localhost:5000"
POLL_INTERVAL = 30
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "LittleBrother"


def _tray_log(msg):
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat()} [pid {os.getpid()}] {msg}\n"
        with open(_LOG_DIR / "tray.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Icon rendering
# ---------------------------------------------------------------------------

def _make_icon(color: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    return img


ICON_GREEN = _make_icon("#22c55e")
ICON_YELLOW = _make_icon("#eab308")
ICON_RED = _make_icon("#ef4444")
ICON_GREY = _make_icon("#6b7280")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class TrayState:
    def __init__(self):
        self.status: str = "unknown"
        self.process_state: str = "unknown"
        self.api_reachable: bool = False
        self.uptime_seconds: int | None = None
        self.monitors_active: int | None = None
        self.monitors_total: int | None = None
        self.queue_depth: int | None = None
        self.last_check: str = "never"
        self.watchdog_reachable: bool = False
        self._lock = threading.Lock()

    def update(self, data: dict):
        with self._lock:
            self.status = data.get("status", "unknown")
            self.process_state = data.get("process_state", "unknown")
            self.api_reachable = data.get("api_reachable", False)
            self.uptime_seconds = data.get("uptime_seconds")
            self.watchdog_reachable = True
            self.last_check = datetime.now().strftime("%H:%M:%S")

            detail = data.get("detail", {})
            self.monitors_active = detail.get("monitors_active")
            self.monitors_total = detail.get("monitors_total")
            self.queue_depth = detail.get("queue_depth")

    def mark_watchdog_down(self):
        with self._lock:
            self.watchdog_reachable = False
            self.status = "unknown"
            self.process_state = "unknown"
            self.api_reachable = False
            self.last_check = datetime.now().strftime("%H:%M:%S")

    def icon_image(self) -> Image.Image:
        if not self.watchdog_reachable:
            return ICON_GREY
        if self.status == "ok":
            return ICON_GREEN
        if self.status in ("degraded", "starting"):
            return ICON_YELLOW
        return ICON_RED

    def tooltip(self) -> str:
        if not self.watchdog_reachable:
            return "Little Brother — watchdog unreachable"
        uptime = _fmt_uptime(self.uptime_seconds)
        return f"Little Brother — {self.status} | {uptime}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except FileNotFoundError:
        return False


def _set_autostart(enable: bool):
    start_bat = str(ROOT / "start.bat")
    value = f'"{start_bat}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                        access=winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Menu builder
# ---------------------------------------------------------------------------

def _build_menu(state: TrayState, icon: pystray.Icon):

    def _label(text):
        return pystray.MenuItem(text, None, enabled=False)

    def _separator():
        return pystray.Menu.SEPARATOR

    def open_dashboard(_icon, _item):
        webbrowser.open(DASHBOARD_URL)

    def toggle_autostart(_icon, _item):
        _set_autostart(not _is_autostart_enabled())

    def quit_tray(_icon, _item):
        _icon.stop()

    # Status lines
    uptime = _fmt_uptime(state.uptime_seconds)
    lb_line = f"LB: {state.process_state}  {uptime}" if state.process_state != "unknown" else "LB: unknown"
    wd_line = "Watchdog: reachable" if state.watchdog_reachable else "Watchdog: unreachable"
    check_line = f"Last check: {state.last_check}"

    mon_line = None
    if state.monitors_active is not None and state.monitors_total is not None:
        q = f"  Queue: {state.queue_depth}" if state.queue_depth is not None else ""
        mon_line = f"Monitors: {state.monitors_active}/{state.monitors_total}{q}"

    items = [
        _label(lb_line),
        _label(wd_line),
        _label(check_line),
    ]
    if mon_line:
        items.append(_label(mon_line))

    items += [
        _separator(),
        pystray.MenuItem("Open Dashboard", open_dashboard),
        _separator(),
        pystray.MenuItem(
            "Start with Windows",
            toggle_autostart,
            checked=lambda _: _is_autostart_enabled(),
        ),
        _separator(),
        pystray.MenuItem("Quit tray", quit_tray),
    ]
    return pystray.Menu(*items)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

STARTUP_POLL_INTERVAL = 3   # poll fast until watchdog first responds
STARTUP_TIMEOUT = 60        # give up fast-polling after this many seconds


def _poll_watchdog(state: TrayState):
    try:
        r = requests.get(f"{WATCHDOG_URL}/status", timeout=5)
        if r.status_code == 200:
            state.update(r.json())
            return True
    except Exception:
        pass
    state.mark_watchdog_down()
    return False


def _poll_loop(state: TrayState, icon: pystray.Icon):
    _tray_log("poll_loop started")
    # Fast-poll until watchdog first responds (it may not be ready yet at startup).
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            if _poll_watchdog(state):
                icon.icon = state.icon_image()
                icon.title = state.tooltip()
                icon.menu = _build_menu(state, icon)
                _tray_log(f"watchdog up (startup) status={state.status}")
                break
            icon.icon = state.icon_image()
            icon.menu = _build_menu(state, icon)
        except Exception:
            _tray_log("exception in startup poll:\n" + traceback.format_exc())
        time.sleep(STARTUP_POLL_INTERVAL)

    # Steady-state: poll every 30s — wrapped so one bad poll can't kill the thread.
    while True:
        try:
            _poll_watchdog(state)
            icon.icon = state.icon_image()
            icon.title = state.tooltip()
            icon.menu = _build_menu(state, icon)
        except Exception:
            _tray_log("exception in steady poll:\n" + traceback.format_exc())
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    state = TrayState()

    icon = pystray.Icon(
        name="little-brother",
        icon=ICON_GREY,
        title="Little Brother — starting…",
        menu=pystray.Menu(
            pystray.MenuItem("Loading…", None, enabled=False),
        ),
    )

    poll_thread = threading.Thread(
        target=_poll_loop, args=(state, icon), daemon=True
    )
    poll_thread.start()

    icon.run()


if __name__ == "__main__":
    _tray_log("tray process launched")
    try:
        main()
    except Exception:
        _tray_log("FATAL in tray main():\n" + traceback.format_exc())
        sys.exit(1)
