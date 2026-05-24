import ctypes
import ctypes.wintypes
import datetime
import threading
import time

# Processes and window title fragments that indicate credential entry.
# When matched, the keystroke buffer is suppressed (stored as a placeholder).
_SUPPRESSED_PROCESSES = {
    "keepass", "keepassxc", "1password", "bitwarden", "lastpass",
    "dashlane", "roboform", "nordpass", "enpass", "passwordsafe",
}

_SUPPRESSED_TITLE_FRAGMENTS = {
    "password", "sign in", "sign-in", "login", "log in",
    "credentials", "unlock", "vault", "authenticate",
    "two-factor", "2fa", "verification code",
}

# Special keys rendered as tokens in the text chunk
_SPECIAL_KEYS = {
    "enter": "[Enter]",
    "tab": "[Tab]",
    "backspace": "[Backspace]",
    "delete": "[Delete]",
    "escape": "[Esc]",
    "space": " ",
    "ctrl": "[Ctrl]",
    "alt": "[Alt]",
    "shift": None,   # modifier only — suppress token but allow next char
    "caps_lock": None,
    "cmd": "[Win]",
    "f1": "[F1]", "f2": "[F2]", "f3": "[F3]", "f4": "[F4]",
    "f5": "[F5]", "f6": "[F6]", "f7": "[F7]", "f8": "[F8]",
    "f9": "[F9]", "f10": "[F10]", "f11": "[F11]", "f12": "[F12]",
}

_FLUSH_IDLE_SECONDS = 5.0
_MAX_BUFFER_CHARS = 500


class KeyboardMonitor:
    """Capture keystrokes and store them as text chunks with window context.

    Keystrokes are buffered and flushed on Enter, on idle timeout, or when
    the buffer reaches _MAX_BUFFER_CHARS. The chunk is stored alongside the
    foreground window title and process name at flush time.

    Windows matching known password managers or containing credential-related
    title fragments are suppressed — the buffer is discarded and a
    [SUPPRESSED] placeholder is stored instead.
    """

    def __init__(self, db):
        self.db = db
        self._listener = None
        self._buffer = []
        self._lock = threading.Lock()
        self._last_key_time = None
        self._flush_timer = None
        self._last_chunk_sig = None  # (text_chunk, key_count) of last write, for dedup
        self._dedup_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self.is_running:
            print("[Keyboard] Already running, skipping start")
            return
        try:
            from pynput.keyboard import Listener
            self._listener = Listener(
                on_press=self._on_press,
                on_release=None,
            )
            self._listener.start()
            self._schedule_flush()
            print("[Keyboard] Monitor running")
        except ImportError:
            print("[Keyboard] pynput not available, keyboard monitoring disabled")
        except Exception as e:
            print(f"[Keyboard] Failed to start: {e}")

    def stop(self):
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        self._flush()
        if self._listener:
            try:
                self._listener.stop()
            except Exception as e:
                print(f"[Keyboard] Error stopping: {e}")
            self._listener = None
        print("[Keyboard] Monitor stopped")

    @property
    def is_running(self):
        return self._listener is not None and self._listener.is_alive()

    # ------------------------------------------------------------------
    # Key handler
    # ------------------------------------------------------------------

    def _on_press(self, key):
        try:
            from pynput.keyboard import Key
            with self._lock:
                self._last_key_time = time.monotonic()

                # Printable character
                if hasattr(key, "char") and key.char is not None:
                    self._buffer.append(key.char)
                else:
                    # Special / modifier key
                    name = getattr(key, "name", None) or str(key)
                    token = _SPECIAL_KEYS.get(name)
                    if token is not None:
                        self._buffer.append(token)
                    # None means suppress token (shift, caps_lock, etc.)

                # Flush on Enter
                if hasattr(key, "name") and key.name == "enter":
                    self._do_flush_locked()
                    return

                # Flush on buffer size limit
                if sum(len(c) for c in self._buffer) >= _MAX_BUFFER_CHARS:
                    self._do_flush_locked()

        except Exception as e:
            print(f"[Keyboard] Error in key handler: {e}")

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def _schedule_flush(self):
        """Schedule an idle-timeout flush check every second."""
        if self._listener is None:
            return
        self._flush_timer = threading.Timer(1.0, self._idle_check)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _idle_check(self):
        """Flush buffer if idle for _FLUSH_IDLE_SECONDS."""
        with self._lock:
            if (
                self._buffer
                and self._last_key_time is not None
                and time.monotonic() - self._last_key_time >= _FLUSH_IDLE_SECONDS
            ):
                self._do_flush_locked()
        self._schedule_flush()

    def _flush(self):
        with self._lock:
            self._do_flush_locked()

    def _do_flush_locked(self):
        """Must be called with self._lock held."""
        if not self._buffer:
            return

        text_chunk = "".join(self._buffer)
        key_count = len(self._buffer)
        self._buffer.clear()
        self._last_key_time = None

        # Fire the write outside the lock to avoid deadlock on DB operations
        threading.Thread(
            target=self._write_chunk,
            args=(text_chunk, key_count),
            daemon=True,
        ).start()

    def _write_chunk(self, text_chunk, key_count):
        try:
            # Deduplicate: drop if identical to the last written chunk
            # (catches two-instance race where both write the same buffer)
            sig = (text_chunk, key_count)
            with self._dedup_lock:
                if sig == self._last_chunk_sig:
                    return
                self._last_chunk_sig = sig

            timestamp = datetime.datetime.utcnow().isoformat()
            window_title, process_name = self._get_foreground_info()
            suppressed = self._is_suppressed(window_title, process_name)

            self.db.log_key_event(
                timestamp=timestamp,
                window_title=window_title,
                process_name=process_name,
                text_chunk="[SUPPRESSED]" if suppressed else text_chunk,
                key_count=key_count,
                suppressed=1 if suppressed else 0,
            )
        except Exception as e:
            print(f"[Keyboard] Error writing chunk: {e}")

    # ------------------------------------------------------------------
    # Suppression check
    # ------------------------------------------------------------------

    def _is_suppressed(self, window_title: str, process_name: str) -> bool:
        title_lower = window_title.lower()
        proc_lower = process_name.lower().replace(".exe", "")

        if proc_lower in _SUPPRESSED_PROCESSES:
            return True
        if any(frag in title_lower for frag in _SUPPRESSED_TITLE_FRAGMENTS):
            return True
        return False

    # ------------------------------------------------------------------
    # Win32 helpers
    # ------------------------------------------------------------------

    def _get_foreground_info(self):
        """Return (window_title, process_name) for the current foreground window."""
        try:
            import psutil
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return "", ""

            title = self._hwnd_title(hwnd)

            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                proc = psutil.Process(pid.value)
                process_name = proc.name()
            except Exception:
                process_name = ""

            return title, process_name
        except Exception:
            return "", ""

    def _hwnd_title(self, hwnd) -> str:
        try:
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or ""
        except Exception:
            return ""
