"""
Unit tests for tools/watchdog.py.
Mocks subprocess, psutil, and requests — no real processes spawned.
"""

import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watchdog import ProcessSupervisor, ActionResult, StatusResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CMD = ["python", "-m", "little_brother"]
CWD = "/fake/root"
APP_URL = "http://localhost:5000"
APP_PORT = 5000


def make_supervisor(api_reachable=False, process_running=False):
    """Create a ProcessSupervisor with discovery mocked out."""
    with patch("tools.watchdog.psutil.net_connections", return_value=[]):
        sup = ProcessSupervisor(
            cmd=CMD, cwd=CWD, app_url=APP_URL, app_port=APP_PORT,
            start_timeout=2, stop_timeout=2,
        )
    # Manually control state
    if process_running:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 9999
        sup._popen = mock_proc
        sup._proc_pid = 9999
    sup._api_reachable = MagicMock(return_value=api_reachable)
    return sup


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------

class TestGetStatus(unittest.TestCase):

    def test_status_running_api_ok(self):
        sup = make_supervisor(api_reachable=True, process_running=True)
        result = sup.get_status()
        self.assertEqual(result.process_state, "running")
        self.assertTrue(result.api_reachable)
        self.assertEqual(result.status, "ok")

    def test_status_running_api_down(self):
        sup = make_supervisor(api_reachable=False, process_running=True)
        result = sup.get_status()
        self.assertEqual(result.process_state, "running")
        self.assertFalse(result.api_reachable)
        self.assertEqual(result.status, "degraded")

    def test_status_stopped(self):
        sup = make_supervisor(api_reachable=False, process_running=False)
        result = sup.get_status()
        self.assertEqual(result.process_state, "stopped")
        self.assertEqual(result.status, "failed")


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthCheck(unittest.TestCase):

    def test_health_check_api_reachable(self):
        sup = make_supervisor(api_reachable=True, process_running=True)
        result = sup.run_health_check()
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.last_health_check_utc_ms)

    def test_health_check_api_unreachable(self):
        sup = make_supervisor(api_reachable=False, process_running=True)
        result = sup.run_health_check()
        self.assertEqual(result.status, "degraded")
        # Must not change process state
        self.assertEqual(result.process_state, "running")

    def test_health_check_does_not_mutate_state(self):
        sup = make_supervisor(api_reachable=True, process_running=True)
        before_pid = sup._proc_pid
        sup.run_health_check()
        self.assertEqual(sup._proc_pid, before_pid)


# ---------------------------------------------------------------------------
# Start tests
# ---------------------------------------------------------------------------

class TestStart(unittest.TestCase):

    def test_start_from_stopped(self):
        sup = make_supervisor(process_running=False)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1234
        sup._api_reachable = MagicMock(return_value=True)

        with patch("tools.watchdog.subprocess.Popen", return_value=mock_proc):
            result = sup.start()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result_code, "ok")
        self.assertIn("request_id", result.to_dict())
        self.assertEqual(result.detail["pid"], 1234)

    def test_start_already_running(self):
        sup = make_supervisor(process_running=True)
        result = sup.start()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.result_code, "already_running")

    def test_start_timeout(self):
        sup = make_supervisor(process_running=False)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 5555
        sup._api_reachable = MagicMock(return_value=False)  # never becomes reachable

        with patch("tools.watchdog.subprocess.Popen", return_value=mock_proc):
            result = sup.start()

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.result_code, "timeout")


# ---------------------------------------------------------------------------
# Stop tests
# ---------------------------------------------------------------------------

class TestStop(unittest.TestCase):

    def test_stop_when_running(self):
        sup = make_supervisor(process_running=True)
        sup._popen.wait = MagicMock(return_value=0)
        result = sup.stop()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result_code, "ok")
        self.assertIsNone(sup._popen)

    def test_stop_already_stopped(self):
        sup = make_supervisor(process_running=False)
        result = sup.stop()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.result_code, "already_stopped")


# ---------------------------------------------------------------------------
# Restart tests
# ---------------------------------------------------------------------------

class TestRestart(unittest.TestCase):

    def test_restart_when_running(self):
        sup = make_supervisor(process_running=True, api_reachable=True)
        sup._popen.wait = MagicMock(return_value=0)
        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_proc.pid = 2222
        sup._api_reachable = MagicMock(side_effect=[True, True])  # stop check + start check

        with patch("tools.watchdog.subprocess.Popen", return_value=new_proc):
            result = sup.restart()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result_code, "ok")

    def test_restart_when_stopped(self):
        sup = make_supervisor(process_running=False)
        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_proc.pid = 3333
        sup._api_reachable = MagicMock(return_value=True)

        with patch("tools.watchdog.subprocess.Popen", return_value=new_proc):
            result = sup.restart()

        self.assertEqual(result.status, "succeeded")


# ---------------------------------------------------------------------------
# Action lock test
# ---------------------------------------------------------------------------

class TestActionLock(unittest.TestCase):

    def test_action_lock_blocks_concurrent(self):
        sup = make_supervisor(process_running=False)
        results = []

        # Acquire the lock manually to simulate a long-running action
        sup._action_lock.acquire()
        try:
            result = sup.start()
            results.append(result)
        finally:
            sup._action_lock.release()

        self.assertEqual(results[0].status, "blocked")
        self.assertEqual(results[0].result_code, "blocked")


# ---------------------------------------------------------------------------
# Process discovery test
# ---------------------------------------------------------------------------

class TestDiscovery(unittest.TestCase):

    def test_discovery_finds_existing_process(self):
        import psutil as _psutil
        mock_conn = MagicMock()
        mock_conn.laddr.port = APP_PORT
        mock_conn.status = _psutil.CONN_LISTEN
        mock_conn.pid = 7777

        with patch("tools.watchdog.psutil.net_connections", return_value=[mock_conn]):
            sup = ProcessSupervisor(
                cmd=CMD, cwd=CWD, app_url=APP_URL, app_port=APP_PORT,
                start_timeout=2, stop_timeout=2,
            )

        self.assertTrue(sup._discovered)
        self.assertEqual(sup._proc_pid, 7777)

    def test_discovery_no_process(self):
        with patch("tools.watchdog.psutil.net_connections", return_value=[]):
            sup = ProcessSupervisor(
                cmd=CMD, cwd=CWD, app_url=APP_URL, app_port=APP_PORT,
                start_timeout=2, stop_timeout=2,
            )

        self.assertFalse(sup._discovered)
        self.assertIsNone(sup._proc_pid)


if __name__ == "__main__":
    unittest.main()
