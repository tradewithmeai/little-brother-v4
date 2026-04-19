import os
import datetime
from pathlib import Path

# File extensions that are never worth recording
_EXCLUDED_EXTENSIONS = {
    ".db", ".db-journal", ".db-wal", ".db-shm",
    ".log", ".tmp", ".temp", ".bak",
    ".pyc", ".pyo",
    ".parquet", ".arrow", ".zst", ".gz", ".bz2",  # data pipeline output files
}

# Directory names that are never worth recording
_EXCLUDED_DIR_NAMES = {
    "__pycache__", ".git", "node_modules", ".pytest_cache",
    "venv", ".venv", "env",
    "target",  # Rust/Java build output
}


class FileSystemMonitor:
    """Monitor filesystem changes using watchdog."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._observer = None
        self._watch_paths = self._resolve_paths(config.get("folders_to_watch", []))
        # Always exclude the app's own directory to prevent feedback loops
        # Normalize to lowercase for case-insensitive Windows comparison
        self._excluded_paths = {
            str(Path(__file__).resolve().parent.parent.parent).lower()  # project root
        }

    @property
    def is_running(self):
        return self._observer is not None and self._observer.is_alive()

    def start(self):
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            handler = self._make_handler(FileSystemEventHandler)
            self._observer = Observer()

            scheduled = 0
            for path in self._watch_paths:
                if os.path.isdir(path):
                    self._observer.schedule(handler, path, recursive=True)
                    scheduled += 1
                    print(f"[Filesystem] Watching: {path}")
                else:
                    print(f"[Filesystem] Skipping (not found): {path}")

            if scheduled > 0:
                self._observer.start()
                print(f"[Filesystem] Monitor running ({scheduled} paths)")
            else:
                print("[Filesystem] No valid paths to watch")
                self._observer = None

        except ImportError:
            print("[Filesystem] watchdog not available, filesystem monitoring disabled")
        except Exception as e:
            print(f"[Filesystem] Failed to start: {e}")

    def stop(self):
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as e:
                print(f"[Filesystem] Error stopping: {e}")
            self._observer = None
        print("[Filesystem] Monitor stopped")

    def _resolve_paths(self, raw_paths):
        """Replace %%USERNAME%% placeholder with actual user home path."""
        home = os.path.expanduser("~")
        # Extract the actual username from the home directory path
        username = os.path.basename(home)

        resolved = []
        for p in raw_paths:
            resolved.append(p.replace("%%USERNAME%%", username))
        return resolved

    def _make_handler(self, base_class):
        """Create a watchdog event handler that logs to the database."""
        monitor = self

        class Handler(base_class):
            def on_created(self, event):
                monitor._log(event, "created")

            def on_modified(self, event):
                monitor._log(event, "modified")

            def on_deleted(self, event):
                monitor._log(event, "deleted")

            def on_moved(self, event):
                monitor._log(event, "moved")

        return Handler()

    def _should_ignore(self, path: str) -> bool:
        p = Path(path)
        # Ignore excluded extensions
        if p.suffix.lower() in _EXCLUDED_EXTENSIONS:
            return True
        # Ignore excluded directory names anywhere in the path
        if any(part in _EXCLUDED_DIR_NAMES for part in p.parts):
            return True
        # Ignore anything inside the app's own directory tree
        # Normalize to lowercase for case-insensitive Windows path comparison
        try:
            resolved = str(p.resolve()).lower()
            for excl in self._excluded_paths:
                if resolved.startswith(excl):
                    return True
        except Exception:
            pass
        return False

    def _log(self, event, event_type):
        if self._should_ignore(event.src_path):
            return
        try:
            timestamp = datetime.datetime.utcnow().isoformat()
            self.db.log_file_event(
                timestamp=timestamp,
                event_type=event_type,
                src_path=event.src_path,
                is_directory=1 if event.is_directory else 0,
            )
        except Exception as e:
            print(f"[Filesystem] Error logging event: {e}")
