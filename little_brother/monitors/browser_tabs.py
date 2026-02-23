import threading
import datetime
import json


class BrowserTabMonitor:
    """Monitor browser tabs via Chrome DevTools Protocol."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.debug_port = config.get("browser_debug_port", 9222)
        self._stop_event = threading.Event()
        self._thread = None
        self._last_tabs = {}  # id -> {title, url}
        self._poll_interval = 2.0
        self._connected = False

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
        """Poll Chrome DevTools Protocol for open tabs."""
        import urllib.request
        import urllib.error

        url = f"http://localhost:{self.debug_port}/json"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            if self._connected:
                print("[BrowserTabs] Chrome debug port not available")
                self._connected = False
            return

        if not self._connected:
            print("[BrowserTabs] Connected to Chrome DevTools")
            self._connected = True

        # Build current tab snapshot (only page targets)
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

        # Detect new tabs
        for tab_id, info in current_tabs.items():
            if tab_id not in self._last_tabs:
                self.db.log_browser_tab(
                    timestamp=timestamp,
                    browser="chrome",
                    event_type="created",
                    title=info["title"],
                    url=info["url"],
                )

        # Detect removed tabs
        for tab_id, info in self._last_tabs.items():
            if tab_id not in current_tabs:
                self.db.log_browser_tab(
                    timestamp=timestamp,
                    browser="chrome",
                    event_type="removed",
                    title=info["title"],
                    url=info["url"],
                )

        # Detect updated tabs (title or URL changed)
        for tab_id, info in current_tabs.items():
            if tab_id in self._last_tabs:
                old = self._last_tabs[tab_id]
                if info["title"] != old["title"] or info["url"] != old["url"]:
                    self.db.log_browser_tab(
                        timestamp=timestamp,
                        browser="chrome",
                        event_type="updated",
                        title=info["title"],
                        url=info["url"],
                    )

        self._last_tabs = current_tabs
