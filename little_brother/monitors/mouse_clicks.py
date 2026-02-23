import ctypes
import ctypes.wintypes
import datetime


class MouseClickMonitor:
    """Monitor mouse clicks using pynput."""

    def __init__(self, db):
        self.db = db
        self._listener = None

    def start(self):
        try:
            from pynput.mouse import Listener
            self._listener = Listener(on_click=self._on_click)
            self._listener.start()
            print("[MouseClick] Monitor running")
        except ImportError:
            print("[MouseClick] pynput not available, mouse monitoring disabled")
        except Exception as e:
            print(f"[MouseClick] Failed to start: {e}")

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception as e:
                print(f"[MouseClick] Error stopping: {e}")
            self._listener = None
        print("[MouseClick] Monitor stopped")

    def _on_click(self, x, y, button, pressed):
        """Handle mouse click events. Only log presses, not releases."""
        if not pressed:
            return

        try:
            button_name = getattr(button, "name", str(button))
            window_title = self._get_foreground_title()
            timestamp = datetime.datetime.utcnow().isoformat()

            self.db.log_mouse_click(
                timestamp=timestamp,
                button=button_name,
                x=int(x),
                y=int(y),
                window_title=window_title,
            )
        except Exception as e:
            print(f"[MouseClick] Error logging click: {e}")

    def _get_foreground_title(self):
        """Get the title of the current foreground window."""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or ""
        except Exception:
            return ""
