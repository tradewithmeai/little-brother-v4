import ctypes
import ctypes.wintypes
import threading
import datetime


# Win32 API setup
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GetForegroundWindow = user32.GetForegroundWindow
GetForegroundWindow.restype = ctypes.wintypes.HWND

GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
GetWindowTextW.restype = ctypes.c_int

GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
GetWindowTextLengthW.restype = ctypes.c_int

GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.POINTER(ctypes.wintypes.DWORD),
]
GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ActiveWindowMonitor:
    """Monitor active window changes by polling."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.poll_interval = config.get("active_window_poll_ms", 500) / 1000.0
        self._stop_event = threading.Event()
        self._thread = None
        self._last_hwnd = None
        self._last_title = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self):
        print("[ActiveWindow] Monitor running")
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as e:
                print(f"[ActiveWindow] Error: {e}")
            self._stop_event.wait(self.poll_interval)
        print("[ActiveWindow] Monitor stopped")

    def _check(self):
        hwnd = GetForegroundWindow()
        if not hwnd:
            return

        # Get window title
        title_len = GetWindowTextLengthW(hwnd)
        if title_len <= 0:
            title = ""
        else:
            buf = ctypes.create_unicode_buffer(title_len + 1)
            GetWindowTextW(hwnd, buf, title_len + 1)
            title = buf.value or ""

        # Deduplicate — only log when the window handle changes
        if hwnd == self._last_hwnd:
            return

        self._last_hwnd = hwnd
        self._last_title = title

        # Get process info
        pid = ctypes.wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        process_name, process_path = self._get_process_info(pid.value)

        timestamp = datetime.datetime.utcnow().isoformat()
        self.db.log_active_window(
            timestamp=timestamp,
            window_title=title,
            process_name=process_name,
            process_path=process_path,
            hwnd=int(hwnd),
        )

    def _get_process_info(self, pid):
        """Get process name and path using Win32 API."""
        if not pid:
            return "", ""

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "", ""

        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.wintypes.DWORD(512)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if ok and buf.value:
                exe_path = buf.value
                # Extract filename from path
                exe_name = exe_path.rsplit("\\", 1)[-1] if "\\" in exe_path else exe_path
                return exe_name, exe_path
            return "", ""
        finally:
            kernel32.CloseHandle(handle)
