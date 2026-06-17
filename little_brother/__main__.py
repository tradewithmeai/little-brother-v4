import signal
import sys
import socket
import os
import traceback

from .main import LittleBrother
from .bootlog import boot_log, crash_log

# Single-instance lock: bind a local socket on a fixed port.
# If it's already bound, another instance is running — exit immediately.
_LOCK_PORT = 47923
_lock_socket = None


def acquire_instance_lock():
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        _lock_socket.bind(("127.0.0.1", _LOCK_PORT))
    except OSError:
        print("[LB] Another instance is already running. Exiting.")
        sys.exit(0)


def handle_exit(signum, frame):
    print(f"\n[LB] Received signal {signum}")
    sys.exit(0)


if __name__ == "__main__":
    boot_log("app process launched (-m little_brother)")
    try:
        acquire_instance_lock()

        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)

        lb = LittleBrother()
        lb.run()
    except SystemExit:
        # Clean exit (e.g. single-instance lock already held) — not a crash.
        raise
    except Exception:
        crash_log("FATAL in app startup:\n" + traceback.format_exc())
        raise
