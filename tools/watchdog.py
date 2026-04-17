"""
Little Brother Watchdog — persistent control layer for Betty Sentinel.

Runs independently from the little-brother app. Stays alive when the app is
down and gives Betty canonical start/stop/restart/health-check actions.

Start: python tools/watchdog.py
API:   http://localhost:5001
"""

import json
import logging
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil
import requests
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("watchdog")

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------

@dataclass
class StatusResult:
    service_name: str = "little_brother"
    process_state: str = "unknown"
    api_reachable: bool = False
    status: str = "unknown"
    last_health_check_utc_ms: int | None = None
    uptime_seconds: int | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in (
            "api_reachable", "process_state", "status", "service_name"
        )}


@dataclass
class ActionResult:
    service_name: str = "little_brother"
    action_type: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = ""
    result_code: str = ""
    message: str = ""
    completed_utc_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# Process supervisor
# ---------------------------------------------------------------------------

class ProcessSupervisor:
    def __init__(self, cmd: list, cwd: str, app_url: str,
                 app_port: int, start_timeout: int, stop_timeout: int):
        self._cmd = cmd
        self._cwd = cwd
        self._app_url = app_url.rstrip("/")
        self._app_port = app_port
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout

        self._popen: subprocess.Popen | None = None
        self._proc_pid: int | None = None
        self._discovered: bool = False
        self._start_time: float | None = None
        self._action_lock = threading.Lock()
        self._last_health_check_ms: int | None = None

        self._discover_existing_process()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_existing_process(self):
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if (conn.laddr.port == self._app_port
                        and conn.status == psutil.CONN_LISTEN
                        and conn.pid):
                    self._proc_pid = conn.pid
                    self._discovered = True
                    log.info("Discovered existing little-brother process (pid=%s)", conn.pid)
                    return
        except Exception as exc:
            log.warning("Process discovery failed: %s", exc)
        log.info("No existing process found on port %s", self._app_port)

    # ------------------------------------------------------------------
    # Internal state helpers
    # ------------------------------------------------------------------

    def _process_state(self) -> str:
        if self._popen is not None:
            if self._popen.poll() is None:
                return "running"
            # Process exited — clean up
            self._popen = None
            self._proc_pid = None
            self._start_time = None
            return "stopped"
        if self._proc_pid is not None:
            if psutil.pid_exists(self._proc_pid):
                return "running"
            # Discovered process is gone
            self._proc_pid = None
            self._discovered = False
            return "stopped"
        return "stopped"

    def _api_reachable(self) -> bool:
        try:
            r = requests.get(f"{self._app_url}/api/v1/status", timeout=3)
            return r.status_code == 200
        except Exception as exc:
            log.debug("_api_reachable failed: %s", exc)
            return False

    def _uptime(self) -> int | None:
        if self._start_time:
            return int(time.time() - self._start_time)
        # Try to get uptime from the app API
        try:
            r = requests.get(f"{self._app_url}/api/v1/status", timeout=3)
            if r.status_code == 200:
                return r.json().get("uptime_seconds")
        except Exception:
            pass
        return None

    def _derive_status(self, proc_state: str, api_ok: bool) -> str:
        if proc_state == "running" and api_ok:
            return "ok"
        if proc_state == "running" and not api_ok:
            return "degraded"
        if proc_state == "stopped":
            return "failed"
        return "unknown"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> StatusResult:
        proc_state = self._process_state()
        api_ok = self._api_reachable() if proc_state == "running" else False
        return StatusResult(
            process_state=proc_state,
            api_reachable=api_ok,
            status=self._derive_status(proc_state, api_ok),
            last_health_check_utc_ms=self._last_health_check_ms,
            uptime_seconds=self._uptime() if proc_state == "running" else None,
            detail={"pid": self._proc_pid or (self._popen.pid if self._popen else None),
                    "discovered": self._discovered},
        )

    def run_health_check(self) -> StatusResult:
        proc_state = self._process_state()
        api_ok = self._api_reachable() if proc_state == "running" else False
        self._last_health_check_ms = int(time.time() * 1000)
        result = StatusResult(
            process_state=proc_state,
            api_reachable=api_ok,
            status=self._derive_status(proc_state, api_ok),
            last_health_check_utc_ms=self._last_health_check_ms,
            uptime_seconds=self._uptime() if proc_state == "running" else None,
            detail={"pid": self._proc_pid or (self._popen.pid if self._popen else None),
                    "discovered": self._discovered},
        )
        log.info("Health check: process_state=%s api_reachable=%s status=%s",
                 result.process_state, result.api_reachable, result.status)
        return result

    def start(self) -> ActionResult:
        if not self._action_lock.acquire(blocking=False):
            return ActionResult(
                action_type="start_service",
                status="blocked",
                result_code="blocked",
                message="Another control action is in progress",
            )
        try:
            if self._process_state() == "running":
                return ActionResult(
                    action_type="start_service",
                    status="blocked",
                    result_code="already_running",
                    message="little-brother is already running",
                )
            log.info("Starting little-brother: %s", self._cmd)
            try:
                self._popen = subprocess.Popen(
                    self._cmd,
                    cwd=self._cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._proc_pid = self._popen.pid
                self._discovered = False
                self._start_time = time.time()
            except Exception as exc:
                log.error("Failed to launch process: %s", exc)
                return ActionResult(
                    action_type="start_service",
                    status="failed",
                    result_code="internal_error",
                    message=str(exc),
                )

            # Wait for API to become reachable
            deadline = time.time() + self._start_timeout
            while time.time() < deadline:
                if self._popen.poll() is not None:
                    return ActionResult(
                        action_type="start_service",
                        status="failed",
                        result_code="internal_error",
                        message="Process exited immediately after launch",
                    )
                if self._api_reachable():
                    log.info("little-brother started (pid=%s)", self._popen.pid)
                    return ActionResult(
                        action_type="start_service",
                        status="succeeded",
                        result_code="ok",
                        message="little-brother started successfully",
                        detail={"pid": self._popen.pid},
                    )
                time.sleep(1)

            return ActionResult(
                action_type="start_service",
                status="timed_out",
                result_code="timeout",
                message=f"API not reachable after {self._start_timeout}s",
                detail={"pid": self._popen.pid},
            )
        finally:
            self._action_lock.release()

    def stop(self) -> ActionResult:
        if not self._action_lock.acquire(blocking=False):
            return ActionResult(
                action_type="stop_service",
                status="blocked",
                result_code="blocked",
                message="Another control action is in progress",
            )
        try:
            return self._do_stop()
        finally:
            self._action_lock.release()

    def _do_stop(self) -> ActionResult:
        """Stop without acquiring lock — for use inside restart."""
        if self._process_state() == "stopped":
            return ActionResult(
                action_type="stop_service",
                status="blocked",
                result_code="already_stopped",
                message="little-brother is not running",
            )

        pid = self._proc_pid or (self._popen.pid if self._popen else None)
        log.info("Stopping little-brother (pid=%s)", pid)

        try:
            if self._popen is not None:
                self._popen.send_signal(signal.SIGTERM)
                try:
                    self._popen.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    log.warning("SIGTERM timed out, sending SIGKILL")
                    self._popen.kill()
                    self._popen.wait(timeout=3)
            elif self._proc_pid is not None:
                proc = psutil.Process(self._proc_pid)
                proc.terminate()
                try:
                    proc.wait(timeout=self._stop_timeout)
                except psutil.TimeoutExpired:
                    log.warning("terminate() timed out, killing pid=%s", self._proc_pid)
                    proc.kill()
                    proc.wait(timeout=3)
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass  # Already gone
        except Exception as exc:
            log.error("Stop failed: %s", exc)
            return ActionResult(
                action_type="stop_service",
                status="failed",
                result_code="internal_error",
                message=str(exc),
            )
        finally:
            self._popen = None
            self._proc_pid = None
            self._discovered = False
            self._start_time = None

        log.info("little-brother stopped (pid=%s)", pid)
        return ActionResult(
            action_type="stop_service",
            status="succeeded",
            result_code="ok",
            message="little-brother stopped successfully",
            detail={"pid": pid},
        )

    def restart(self) -> ActionResult:
        if not self._action_lock.acquire(blocking=False):
            return ActionResult(
                action_type="restart_service",
                status="blocked",
                result_code="blocked",
                message="Another control action is in progress",
            )
        try:
            log.info("Restarting little-brother")
            if self._process_state() == "running":
                stop_result = self._do_stop()
                if stop_result.status not in ("succeeded", "blocked") or \
                        stop_result.result_code == "internal_error":
                    return ActionResult(
                        action_type="restart_service",
                        status="failed",
                        result_code=stop_result.result_code,
                        message=f"Stop phase failed: {stop_result.message}",
                    )
                time.sleep(1)

            # Start without lock (we hold it)
            log.info("Starting little-brother (restart phase): %s", self._cmd)
            try:
                self._popen = subprocess.Popen(
                    self._cmd,
                    cwd=self._cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._proc_pid = self._popen.pid
                self._discovered = False
                self._start_time = time.time()
            except Exception as exc:
                return ActionResult(
                    action_type="restart_service",
                    status="failed",
                    result_code="internal_error",
                    message=str(exc),
                )

            deadline = time.time() + self._start_timeout
            while time.time() < deadline:
                if self._popen.poll() is not None:
                    return ActionResult(
                        action_type="restart_service",
                        status="failed",
                        result_code="internal_error",
                        message="Process exited immediately after restart",
                    )
                if self._api_reachable():
                    log.info("little-brother restarted (pid=%s)", self._popen.pid)
                    return ActionResult(
                        action_type="restart_service",
                        status="succeeded",
                        result_code="ok",
                        message="little-brother restarted successfully",
                        detail={"pid": self._popen.pid},
                    )
                time.sleep(1)

            return ActionResult(
                action_type="restart_service",
                status="timed_out",
                result_code="timeout",
                message=f"API not reachable after {self._start_timeout}s",
                detail={"pid": self._popen.pid},
            )
        finally:
            self._action_lock.release()


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def _http_status(result: ActionResult) -> int:
    if result.status in ("succeeded", "blocked") and result.result_code not in ("internal_error",):
        return 200
    if result.status in ("failed", "timed_out") or result.result_code == "internal_error":
        return 500
    return 200


def create_app(supervisor: ProcessSupervisor) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "watchdog": "little-brother"})

    @app.route("/status")
    def status():
        return jsonify(supervisor.get_status().to_dict())

    @app.route("/control/run-health-check", methods=["POST"])
    def run_health_check():
        return jsonify(supervisor.run_health_check().to_dict())

    @app.route("/control/start", methods=["POST"])
    def start():
        result = supervisor.start()
        return jsonify(result.to_dict()), _http_status(result)

    @app.route("/control/stop", methods=["POST"])
    def stop():
        result = supervisor.stop()
        return jsonify(result.to_dict()), _http_status(result)

    @app.route("/control/restart", methods=["POST"])
    def restart():
        result = supervisor.restart()
        return jsonify(result.to_dict()), _http_status(result)

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "not found", "status": "invalid_request"}), 404

    @app.errorhandler(Exception)
    def handle_exception(e):
        log.error("Unhandled exception: %s", e, exc_info=True)
        return jsonify({"error": str(e), "status": "internal_error"}), 500

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config_path: str | None = None):
    if config_path is None:
        config_path = str(ROOT / "little_brother" / "config.json")

    with open(config_path) as f:
        cfg = json.load(f)

    wdog = cfg.get("watchdog", {})
    port = int(wdog.get("port", 5001))
    app_port = int(wdog.get("app_port", 5000))
    start_cmd = wdog.get("app_start_command", ["venv/Scripts/python.exe", "-m", "little_brother"])
    start_timeout = int(wdog.get("start_timeout_seconds", 15))
    stop_timeout = int(wdog.get("stop_timeout_seconds", 10))
    auto_start = bool(wdog.get("auto_start_app", False))

    if not isinstance(start_cmd, list):
        log.error("watchdog.app_start_command must be a list, not a string — refusing to start")
        return

    # Resolve command relative to project root
    cmd = [str(ROOT / start_cmd[0])] + start_cmd[1:]
    app_url = f"http://localhost:{app_port}"

    supervisor = ProcessSupervisor(
        cmd=cmd,
        cwd=str(ROOT),
        app_url=app_url,
        app_port=app_port,
        start_timeout=start_timeout,
        stop_timeout=stop_timeout,
    )

    if auto_start and supervisor.get_status().process_state != "running":
        log.info("auto_start_app=true — starting little-brother")
        supervisor.start()

    flask_app = create_app(supervisor)
    log.info("Watchdog listening on port %s", port)
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run()
