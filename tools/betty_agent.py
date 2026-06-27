"""
Betty Sentinel agent for little-brother-v4.

Pushes signed heartbeat and service-state telemetry to Betty Sentinel
(http://localhost:8400) every 60 seconds, derived from the local API.

Run standalone: python tools/betty_agent.py
"""

import hashlib
import hmac
import json
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [betty] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("betty_agent")

ROOT = Path(__file__).resolve().parent.parent
SEQ_FILE = ROOT / "data" / "reports" / "betty_seq.json"
LB_API = "http://127.0.0.1:5000"
STALE_MINUTES = 10
LOOP_INTERVAL = 60


def _ts_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


def _canonical(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class BettyAgent:
    def __init__(self, config: dict):
        self._url = config["betty_url"].rstrip("/")
        self._agent_id = config["agent_id"]
        self._secret_bytes = bytes.fromhex(config["secret_hex"])
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"

    def _next_sequence(self) -> int:
        try:
            SEQ_FILE.parent.mkdir(parents=True, exist_ok=True)
            if SEQ_FILE.exists():
                data = json.loads(SEQ_FILE.read_text())
                seq = int(data["seq"]) + 1
            else:
                seq = int(time.time())
            tmp = SEQ_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seq": seq}))
            os.replace(tmp, SEQ_FILE)
            return seq
        except Exception as exc:
            log.warning("seq file error (%s), falling back to time()", exc)
            return int(time.time())

    def _sign(self, payload: dict) -> dict:
        sig = hmac.new(self._secret_bytes, _canonical(payload), hashlib.sha256).hexdigest()
        return {**payload, "signature": sig}

    def send_heartbeat(self) -> bool:
        payload = self._sign({
            "event_type": "agent_heartbeat",
            "schema_version": "1.0",
            "agent_id": self._agent_id,
            "host_id": socket.gethostname(),
            "environment": "production",
            "bridge_version": "1.0.0",
            "ts_utc": _ts_utc(),
            "sequence_number": self._next_sequence(),
            "services_summary": {},
            "system_summary": {},
        })
        return self._post("/ingest/heartbeat", payload)

    def send_service_state(self, last_data_utc: str, status: str, metrics: dict) -> bool:
        payload = self._sign({
            "event_type": "service_state",
            "schema_version": "1.0",
            "agent_id": self._agent_id,
            "service_name": "little-brother",
            "status": status,
            "last_data_utc": last_data_utc,
            "metrics_summary": metrics,
            "ts_utc": _ts_utc(),
            "sequence_number": self._next_sequence(),
        })
        return self._post("/ingest/service-state", payload)

    def _post(self, path: str, payload: dict) -> bool:
        url = self._url + path
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            if resp.status_code == 202:
                log.info("%s → 202", path)
                return True
            log.warning("%s → %s %s", path, resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            log.warning("%s failed: %s", path, exc)
            return False

    def close(self):
        self._session.close()


def _collect_lb_state() -> tuple[str, str, dict]:
    """
    Returns (last_data_utc, status, metrics_summary).
    Queries the little-brother local API to derive health and freshness.
    """
    try:
        status_resp = requests.get(f"{LB_API}/api/v1/status", timeout=5)
        summary_resp = requests.get(f"{LB_API}/api/summary", timeout=5)
        status_resp.raise_for_status()
        summary_resp.raise_for_status()
    except Exception as exc:
        log.warning("Cannot reach little-brother API: %s", exc)
        return (_ts_utc(), "error", {"active_monitors": 0, "total_monitors": 0,
                                     "queue_depth": 0, "uptime_seconds": 0})

    s = status_resp.json()
    summ = summary_resp.json()

    monitors = s.get("monitors", {})
    total = len(monitors)
    active = sum(1 for m in monitors.values() if m.get("running"))
    queue_depth = s.get("database", {}).get("queue_depth", 0)
    uptime_seconds = s.get("uptime_seconds", 0)

    last_ts_raw = (summ.get("active_window_events") or {}).get("last")
    if last_ts_raw:
        last_dt = datetime.fromisoformat(last_ts_raw).replace(tzinfo=timezone.utc)
        last_data_utc = last_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{last_dt.microsecond:06d}Z"
        age_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    else:
        last_data_utc = _ts_utc()
        age_minutes = 0

    if active < total:
        service_status = "degraded"
    elif age_minutes > STALE_MINUTES:
        service_status = "stale"
    else:
        service_status = "ok"

    metrics = {
        "active_monitors": active,
        "total_monitors": total,
        "queue_depth": queue_depth,
        "uptime_seconds": uptime_seconds,
    }
    return (last_data_utc, service_status, metrics)


def run_loop(config_path: str | None = None):
    if config_path is None:
        config_path = str(ROOT / "little_brother" / "config.json")

    with open(config_path) as f:
        cfg = json.load(f)

    betty_cfg = cfg.get("betty", {})
    if not betty_cfg.get("enabled", False):
        log.info("betty.enabled=false — exiting")
        return
    secret_hex = betty_cfg.get("secret_hex", "")
    if not secret_hex:
        log.info("betty.secret_hex is empty — exiting")
        return

    agent = BettyAgent({
        "betty_url": betty_cfg["url"],
        "agent_id": betty_cfg["agent_id"],
        "secret_hex": secret_hex,
    })

    _stop = threading.Event()

    def _handle_stop(signum, frame):
        log.info("signal %s received, stopping", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _handle_stop)

    log.info("Betty agent started (agent_id=%s, interval=%ss)", betty_cfg["agent_id"], LOOP_INTERVAL)

    try:
        while not _stop.is_set():
            last_data_utc, status, metrics = _collect_lb_state()
            agent.send_heartbeat()
            agent.send_service_state(last_data_utc, status, metrics)
            _stop.wait(timeout=LOOP_INTERVAL)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — stopping")
    finally:
        agent.close()
        log.info("Betty agent stopped")


if __name__ == "__main__":
    run_loop()
