import json
import os
import time
import threading
import signal
import sys

from .db.database import Database
from .monitors.active_window import ActiveWindowMonitor
from .monitors.mouse_clicks import MouseClickMonitor
from .monitors.browser_tabs import BrowserTabMonitor
from .monitors.filesystem import FileSystemMonitor
from .monitors.keyboard import KeyboardMonitor
from .events import EventBus
from .dashboard.server import DashboardServer
from .betty import BettySentinel


class LittleBrother:
    """Main orchestrator for the Little Brother monitoring system."""

    def __init__(self):
        """Initialize the Little Brother system."""
        self.db = None
        self.monitors = []
        self.monitor_map = {}
        self.dashboard = None
        self.event_bus = None
        self.config = {}
        self.running = False
        self._start_time = None
        self.shutdown_lock = threading.Lock()
        self.betty = BettySentinel()

    def load_config(self):
        """Load configuration from config.json."""
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)

    def start(self):
        """Start all monitors and the database."""
        print("[LB] Starting Little Brother monitoring system...")

        # Load configuration
        self.config = self.load_config()
        config = self.config
        print(f"[LB] Configuration loaded")

        # Initialize event bus
        self.event_bus = EventBus()

        # Initialize database
        self.db = Database(event_bus=self.event_bus)
        print("[LB] Database initialized")

        # Initialize monitors
        print("[LB] Initializing monitors...")
        active_win_mon = ActiveWindowMonitor(self.db, config)
        mouse_mon = MouseClickMonitor(self.db)
        browser_mon = BrowserTabMonitor(self.db, config)
        fs_mon = FileSystemMonitor(self.db, config)
        keyboard_mon = KeyboardMonitor(self.db)

        # Store monitors in startup order for later shutdown
        self.monitors = [active_win_mon, mouse_mon, browser_mon, fs_mon, keyboard_mon]
        self.monitor_map = {
            "active_window": active_win_mon,
            "mouse_clicks": mouse_mon,
            "browser_tabs": browser_mon,
            "filesystem": fs_mon,
            "keyboard": keyboard_mon,
        }

        # Start all monitors
        print("[LB] Starting monitors...")
        for monitor in self.monitors:
            monitor.start()
            print(f"[LB] - {monitor.__class__.__name__} started")

        # Start dashboard + API
        self.dashboard = DashboardServer(config, orchestrator=self, event_bus=self.event_bus)
        self.dashboard.start()

        # Register configured webhooks
        self._register_webhooks()

        self._start_time = time.time()
        self.running = True

        # Start Betty Sentinel telemetry
        self.betty.start(self)

        print("[LB] Monitors started. Press Ctrl+C to stop.")

    def stop(self):
        """Stop all monitors and the database in reverse order."""
        with self.shutdown_lock:
            if not self.running:
                return  # Already shutting down

            print("\n[LB] Shutting down...")
            self.running = False

            # Stop Betty Sentinel before monitors
            self.betty.stop()

            # Stop monitors in reverse order
            for monitor in reversed(self.monitors):
                try:
                    print(f"[LB] Stopping {monitor.__class__.__name__}...")
                    monitor.stop()
                except Exception as e:
                    print(f"[LB] Error stopping {monitor.__class__.__name__}: {e}")

            # Stop dashboard
            if self.dashboard:
                try:
                    self.dashboard.stop()
                except Exception as e:
                    print(f"[LB] Error stopping dashboard: {e}")

            # Stop database last
            if self.db:
                try:
                    self.db.stop()
                except Exception as e:
                    print(f"[LB] Error stopping database: {e}")

            print("[LB] Shutdown complete.")

    @property
    def uptime_seconds(self):
        if self._start_time:
            return int(time.time() - self._start_time)
        return 0

    def update_config(self, updates):
        """Update config fields and write back to config.json."""
        self.config.update(updates)
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "w") as f:
            json.dump(self.config, f, indent=2)
            f.write("\n")
        return dict(self.config)

    def _register_webhooks(self):
        """Register any webhook URLs from config with the event bus."""
        from .api.routes import _register_webhook
        for url in self.config.get("webhooks", []):
            _register_webhook(url, self.event_bus)
            print(f"[LB] Registered webhook: {url}")

    def run(self):
        """Main run loop."""
        try:
            self.start()

            # Main loop - just sleep and wait for signals
            while self.running:
                time.sleep(1)

        except KeyboardInterrupt:
            # Ctrl+C pressed
            pass
        except SystemExit:
            # System exit requested
            pass
        except Exception as e:
            print(f"[LB] Unexpected error: {e}")
        finally:
            self.stop()


def handle_exit(signum, frame):
    """Signal handler for clean exit."""
    print(f"\n[LB] Received signal {signum}")
    sys.exit(0)


if __name__ == "__main__":
    # When run directly (python main.py from inside little_brother/),
    # use the __main__.py entry point instead:
    #   python -m little_brother
    print("Use: python -m little_brother")
    print("  (run from the project root directory)")
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    lb = LittleBrother()
    lb.run()
