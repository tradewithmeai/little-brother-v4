import threading
import datetime
import json
import time
import urllib.request
import urllib.error


class BrowserTabMonitor:
    """Monitor browser tabs via Chrome DevTools Protocol HTTP endpoint.

    Firefox is not supported via this method — use active_window_events
    for browser activity when running Firefox.
    """

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.debug_port = config.get("browser_debug_port", 9222)
        self._stop_event = threading.Event()
        self._thread = None
        self._last_tabs = {}  # id -> {title, url}
        self._tab_first_seen = {}  # id -> monotonic timestamp when tab was first seen
        self._poll_interval = 2.0
        self._connected = False

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self):
        print(f"[BrowserTabs] Monitor running (port {self.debug_port})")
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                if self._connected:
                    print(f"[BrowserTabs] Lost connection: {e}")
                    self._connected = False
            self._stop_event.wait(self._poll_interval)
        print("[BrowserTabs] Monitor stopped")

    def _poll(self):
        """Poll Chrome DevTools Protocol HTTP endpoint for open tabs."""
        url = f"http://localhost:{self.debug_port}/json"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            if self._connected:
                print("[BrowserTabs] Debug port not available")
                self._connected = False
            return

        if not self._connected:
            print("[BrowserTabs] Connected to browser DevTools")
            self._connected = True

        current_tabs = {}
        for entry in data:
            if entry.get("type") != "page":
                continue
            tab_id = entry.get("id", "")
            current_tabs[tab_id] = {
                "title": entry.get("title", ""),
                "url": entry.get("url", ""),
            }

        timestamp = datetime.datetime.utcnow().isoformat()
        now = time.monotonic()

        for tab_id, info in current_tabs.items():
            if tab_id not in self._last_tabs:
                self._tab_first_seen[tab_id] = now
                self.db.log_browser_tab(
                    timestamp=timestamp, browser="chrome",
                    event_type="created", title=info["title"], url=info["url"],
                )

        for tab_id, info in self._last_tabs.items():
            if tab_id not in current_tabs:
                # Emit a dwell event so the digest can compute time-in-tab
                first_seen = self._tab_first_seen.pop(tab_id, None)
                duration_ms = int((now - first_seen) * 1000) if first_seen is not None else None
                self.db.log_browser_tab(
                    timestamp=timestamp, browser="chrome",
                    event_type="dwell", title=info["title"], url=info["url"],
                    duration_ms=duration_ms,
                )
                self.db.log_browser_tab(
                    timestamp=timestamp, browser="chrome",
                    event_type="removed", title=info["title"], url=info["url"],
                )

        for tab_id, info in current_tabs.items():
            if tab_id in self._last_tabs:
                old = self._last_tabs[tab_id]
                if info["title"] != old["title"] or info["url"] != old["url"]:
                    self.db.log_browser_tab(
                        timestamp=timestamp, browser="chrome",
                        event_type="updated", title=info["title"], url=info["url"],
                    )

        self._last_tabs = current_tabs
