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

TUNNELS = [
    {
        "name": "hermes",
        "cmd": [
            "ssh",
            "-i", r"C:\Users\richw\.ssh\id_hetzner",
            "-N",
            "-R", "5001:127.0.0.1:5000",   # Little Brother
            "-R", "5055:127.0.0.1:5055",   # Social Monitor
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "root@116.203.119.49",
        ],
    },
    {
        "name": "hermes2",
        "cmd": [
            "ssh",
            "-i", r"C:\Users\richw\.ssh\id_ed25519_hermes_normal",
            "-N",
            "-R", "5001:127.0.0.1:5000",   # Little Brother
            "-R", "5055:127.0.0.1:5055",   # Social Monitor
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "root@178.105.74.155",
        ],
    },
]

RECONNECT_DELAY = 5


def _keep_tunnel(tunnel: dict):
    """Reconnect loop for a single tunnel — runs in its own thread."""
    name = tunnel["name"]
    cmd  = tunnel["cmd"]
    log.info("[%s] Tunnel keeper started", name)
    while True:
        log.info("[%s] Opening SSH tunnel to %s", name, cmd[-1])
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            ret = proc.wait()
            log.warning("[%s] Tunnel exited (code=%s) — reconnecting in %ss", name, ret, RECONNECT_DELAY)
        except FileNotFoundError:
            log.error("[%s] ssh.exe not found — is OpenSSH installed? Retrying in %ss", name, RECONNECT_DELAY)
        except Exception as exc:
            log.error("[%s] Unexpected error: %s — retrying in %ss", name, exc, RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


def run():
    import threading
    threads = [
        threading.Thread(target=_keep_tunnel, args=(t,), daemon=True, name=t["name"])
        for t in TUNNELS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    run()
