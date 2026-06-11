import os
import datetime
import time
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------------------
# File classification by extension
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sql",
    ".sh", ".bat", ".ps1", ".html", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte", ".md", ".rst", ".r", ".m", ".lua", ".ex", ".exs",
}

_CONFIG_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".conf",
    ".xml", ".plist", ".lock", ".editorconfig", ".gitignore",
}

_DOCUMENT_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".txt", ".rtf", ".odt", ".ods", ".csv",
}

_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".mp4", ".mp3", ".wav", ".avi", ".mov", ".mkv", ".flac",
}


def _classify_file(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in _SOURCE_EXTENSIONS:
        return "source"
    if ext in _CONFIG_EXTENSIONS:
        return "config"
    if ext in _DOCUMENT_EXTENSIONS:
        return "document"
    if ext in _MEDIA_EXTENSIONS:
        return "media"
    return "other"


def _get_workspace(path: str, watched_roots: list) -> str | None:
    """Return workspace name derived from the watched root this path falls under."""
    try:
        p = Path(path)
        for root in watched_roots:
            root_p = Path(root)
            try:
                rel = p.relative_to(root_p)
                parts = rel.parts
                if not parts:
                    return root_p.name
                return parts[0]
            except ValueError:
                continue
    except Exception:
        pass
    return None


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

# Path fragments (lowercase) that indicate AI agent / tool activity
_AGENT_PATH_PATTERNS = {
    "\\.claude\\",
    "/.claude/",
    "\\.playwright-mcp\\",
    "/.playwright-mcp/",
    "\\appdata\\local\\temp\\claude",
    "/appdata/local/temp/claude",
}

# Velocity threshold: events per directory within the window
_VELOCITY_WINDOW_SECS = 5
_VELOCITY_THRESHOLD = 20


class ActivityTagger:
    """Tags filesystem events as 'human' or 'agent_activity'."""

    def __init__(self):
        self._dir_times: dict[str, deque] = {}
        self._cleanup_counter = 0

    def tag(self, path: str) -> str:
        path_lower = path.lower().replace("/", "\\")

        for pattern in _AGENT_PATH_PATTERNS:
            if pattern.replace("/", "\\") in path_lower:
                return "agent_activity"

        parent = str(Path(path).parent).lower()
        now = time.monotonic()
        times = self._dir_times.setdefault(parent, deque())

        cutoff = now - _VELOCITY_WINDOW_SECS
        while times and times[0] < cutoff:
            times.popleft()

        times.append(now)

        if len(times) >= _VELOCITY_THRESHOLD:
            return "agent_activity"

        # Periodically drop empty deques to prevent unbounded growth
        self._cleanup_counter += 1
        if self._cleanup_counter >= 1000:
            self._cleanup_counter = 0
            self._dir_times = {k: v for k, v in self._dir_times.items() if v}

        return "human"


class FileSystemMonitor:
    """Monitor filesystem changes using watchdog."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._observer = None
        self._watch_paths = self._resolve_paths(config.get("folders_to_watch", []))
        self._tagger = ActivityTagger()
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
            source_tag = self._tagger.tag(event.src_path)
            workspace = _get_workspace(event.src_path, self._watch_paths)
            file_class = _classify_file(event.src_path) if not event.is_directory else "directory"
            self.db.log_file_event(
                timestamp=timestamp,
                event_type=event_type,
                src_path=event.src_path,
                is_directory=1 if event.is_directory else 0,
                source_tag=source_tag,
                workspace=workspace,
                file_class=file_class,
            )
        except Exception as e:
            print(f"[Filesystem] Error logging event: {e}")
