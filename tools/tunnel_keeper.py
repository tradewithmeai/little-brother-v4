"""
SSH reverse tunnel keeper.

Runs via pythonw.exe — no console window.
Maintains the SSH reverse tunnel with automatic reconnect on failure.

Start: pythonw tools/tunnel_keeper.py
"""

import logging
import subprocess
import time
from pathlib import Path

logging.basicConfig(
    filename=Path(__file__).resolve().parent.parent / "little_brother" / "logs" / "tunnel.log",
    level=logging.INFO,
    format="%(asctime)s [tunnel] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tunnel")

SSH_CMD = [
    "ssh",
    "-i", r"C:\Users\richw\.ssh\id_hetzner",
    "-N",
    "-R", "5001:127.0.0.1:5000",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=no",
    "root@116.203.119.49",
]

RECONNECT_DELAY = 5


def run():
    log.info("Tunnel keeper started")
    while True:
        log.info("Opening SSH tunnel: %s", " ".join(SSH_CMD[1:]))
        try:
            proc = subprocess.Popen(
                SSH_CMD,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            ret = proc.wait()
            log.warning("SSH tunnel exited (code=%s) — reconnecting in %ss", ret, RECONNECT_DELAY)
        except FileNotFoundError:
            log.error("ssh.exe not found — is OpenSSH installed? Retrying in %ss", RECONNECT_DELAY)
        except Exception as exc:
            log.error("Unexpected error: %s — retrying in %ss", exc, RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    run()
