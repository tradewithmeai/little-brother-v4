import ctypes
import ctypes.wintypes
import datetime

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MouseClickMonitor:
    """Monitor mouse clicks using pynput."""

    def __init__(self, db):
        self.db = db
        self._listener = None

    @property
    def is_running(self):
        return self._listener is not None and self._listener.is_alive()

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
            window_title, process_name = self._get_foreground_info()
            timestamp = datetime.datetime.utcnow().isoformat()

            self.db.log_mouse_click(
                timestamp=timestamp,
                button=button_name,
                x=int(x),
                y=int(y),
                window_title=window_title,
                process_name=process_name,
            )
        except Exception as e:
            print(f"[MouseClick] Error logging click: {e}")

    def _get_foreground_info(self):
        """Return (window_title, process_name) for the current foreground window."""
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return "", ""
            length = _user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value or ""
            else:
                title = ""
            pid = ctypes.wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process_name = ""
            if pid.value:
                handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                if handle:
                    try:
                        path_buf = ctypes.create_unicode_buffer(512)
                        size = ctypes.wintypes.DWORD(512)
                        if _kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                            exe = path_buf.value
                            process_name = exe.rsplit("\\", 1)[-1] if "\\" in exe else exe
                    finally:
                        _kernel32.CloseHandle(handle)
            return title, process_name
        except Exception:
            return "", ""
