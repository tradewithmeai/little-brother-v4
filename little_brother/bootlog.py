"""File-based boot/crash logging that survives pythonw (no console).

pythonw.exe discards stdout/stderr, so print()s during startup vanish. These
helpers write directly to little_brother/logs/ so a failing boot leaves evidence
on disk regardless of how the process was launched.
"""

import datetime
import os

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def _write(filename: str, msg: str) -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        line = f"{datetime.datetime.now().isoformat()} [pid {os.getpid()}] {msg}\n"
        with open(os.path.join(_LOG_DIR, filename), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Logging must never break startup.
        pass


def boot_log(msg: str) -> None:
    """Append a timestamped boot-phase marker to logs/boot.log."""
    _write("boot.log", msg)


def crash_log(msg: str) -> None:
    """Append a timestamped fatal-error record (with traceback) to logs/crash.log."""
    _write("crash.log", msg)
