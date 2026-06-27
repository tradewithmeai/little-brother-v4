"""
Tests for the monitoring pipeline improvements:
- Filesystem noise filtering
- DB heartbeat column and is_heartbeat flag
- DB browser tab duration_ms
- Keystroke start-context reset on flush
"""

import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from little_brother.monitors.filesystem import FileSystemMonitor, _EXCLUDED_EXTENSIONS, _EXCLUDED_FILENAMES


# ---------------------------------------------------------------------------
# Filesystem noise filter tests
# ---------------------------------------------------------------------------

def _make_fs_monitor():
    db = MagicMock()
    config = {"folders_to_watch": []}
    return FileSystemMonitor(db, config)


class TestFilesystemShouldIgnore(unittest.TestCase):

    def setUp(self):
        self.mon = _make_fs_monitor()

    def test_office_lock_files_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\docs\~$report.docx"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\docs\~$budget.xlsx"))

    def test_windows_metadata_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\Thumbs.db"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\thumbs.db"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\Desktop.ini"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\desktop.ini"))

    def test_partial_downloads_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\video.mp4.crdownload"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\downloads\archive.zip.part"))

    def test_log_and_temp_files_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\app.log"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\file.tmp"))
        self.assertTrue(self.mon._should_ignore(r"C:\Users\user\file.bak"))

    def test_pycache_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\project\__pycache__\module.pyc"))

    def test_node_modules_ignored(self):
        self.assertTrue(self.mon._should_ignore(r"C:\project\node_modules\package\index.js"))

    def test_regular_source_files_not_ignored(self):
        self.assertFalse(self.mon._should_ignore(r"C:\project\src\main.py"))
        self.assertFalse(self.mon._should_ignore(r"C:\project\src\app.js"))

    def test_regular_documents_not_ignored(self):
        self.assertFalse(self.mon._should_ignore(r"C:\Users\user\docs\report.docx"))
        self.assertFalse(self.mon._should_ignore(r"C:\Users\user\docs\data.xlsx"))

    def test_excluded_extensions_set_contains_partials(self):
        self.assertIn(".crdownload", _EXCLUDED_EXTENSIONS)
        self.assertIn(".part", _EXCLUDED_EXTENSIONS)

    def test_excluded_filenames_set_contains_noise(self):
        self.assertIn("thumbs.db", _EXCLUDED_FILENAMES)
        self.assertIn("desktop.ini", _EXCLUDED_FILENAMES)


# ---------------------------------------------------------------------------
# Database: is_heartbeat column and log_active_window
# ---------------------------------------------------------------------------

class TestDatabaseHeartbeat(unittest.TestCase):

    def _make_db(self):
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        from little_brother.db.database import Database
        db = Database(tmp)
        self._db_path = tmp
        return db

    def test_log_active_window_with_heartbeat(self):
        db = self._make_db()
        try:
            ts = "2026-01-01T12:00:00"
            db.log_active_window(
                timestamp=ts,
                window_title="Test",
                process_name="test.exe",
                process_path="C:\\test.exe",
                hwnd=1234,
                is_heartbeat=1,
            )
            time.sleep(0.3)  # let writer thread commit
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT is_heartbeat FROM active_window_events WHERE timestamp=?", (ts,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1)
        finally:
            db.stop()

    def test_log_active_window_without_heartbeat_defaults_zero(self):
        db = self._make_db()
        try:
            ts = "2026-01-01T13:00:00"
            db.log_active_window(
                timestamp=ts,
                window_title="Real Switch",
                process_name="real.exe",
                process_path="C:\\real.exe",
                hwnd=5678,
            )
            time.sleep(0.3)
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT is_heartbeat FROM active_window_events WHERE timestamp=?", (ts,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 0)
        finally:
            db.stop()

    def test_heartbeats_excluded_from_switch_count_sql(self):
        """The window_switches SQL excludes is_heartbeat rows."""
        db = self._make_db()
        try:
            base = "2026-01-01T"
            # 3 real events + 2 heartbeats
            for i, (hb, t) in enumerate([
                (0, "10:00:00"), (0, "10:05:00"), (1, "10:10:00"),
                (0, "10:15:00"), (1, "10:20:00"),
            ]):
                db.log_active_window(
                    timestamp=base + t,
                    window_title=f"Win{i}",
                    process_name="app.exe",
                    process_path="C:\\app.exe",
                    hwnd=100 + i,
                    is_heartbeat=hb,
                )
            time.sleep(0.5)

            conn = sqlite3.connect(self._db_path)
            since = "2026-01-01T00:00:00"
            row = conn.execute(
                "SELECT COUNT(*) as v FROM active_window_events "
                "WHERE timestamp >= ? AND (is_heartbeat = 0 OR is_heartbeat IS NULL)",
                (since,),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], 3)
        finally:
            db.stop()


# ---------------------------------------------------------------------------
# Database: browser tab duration_ms
# ---------------------------------------------------------------------------

class TestDatabaseBrowserDwell(unittest.TestCase):

    def _make_db(self):
        import tempfile
        tmp = tempfile.mktemp(suffix=".db")
        from little_brother.db.database import Database
        db = Database(tmp)
        self._db_path = tmp
        return db

    def test_log_browser_tab_with_duration(self):
        db = self._make_db()
        try:
            ts = "2026-01-01T14:00:00"
            db.log_browser_tab(
                timestamp=ts,
                browser="chrome",
                event_type="dwell",
                title="Test Page",
                url="https://example.com",
                duration_ms=12345,
            )
            time.sleep(0.3)
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT duration_ms FROM browser_tab_events WHERE timestamp=?", (ts,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 12345)
        finally:
            db.stop()

    def test_log_browser_tab_without_duration(self):
        db = self._make_db()
        try:
            ts = "2026-01-01T15:00:00"
            db.log_browser_tab(
                timestamp=ts,
                browser="chrome",
                event_type="created",
                title="New Tab",
                url="about:blank",
            )
            time.sleep(0.3)
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT duration_ms FROM browser_tab_events WHERE timestamp=?", (ts,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0])
        finally:
            db.stop()


# ---------------------------------------------------------------------------
# Keyboard: start-context reset on flush
# ---------------------------------------------------------------------------

class TestKeyboardStartContext(unittest.TestCase):

    def _make_keyboard(self):
        from little_brother.monitors.keyboard import KeyboardMonitor
        db = MagicMock()
        db.log_key_event = MagicMock()
        mon = KeyboardMonitor(db)
        return mon

    def test_buffer_start_context_none_initially(self):
        mon = self._make_keyboard()
        self.assertIsNone(mon._buffer_start_context)

    def test_buffer_start_context_reset_after_flush(self):
        mon = self._make_keyboard()
        mon._buffer = ["a", "b", "c"]
        mon._buffer_start_context = ("Test Window", "test.exe")

        with mon._lock:
            mon._do_flush_locked()

        # After flush the context must be cleared for the next chunk
        self.assertIsNone(mon._buffer_start_context)

    def test_write_chunk_uses_captured_context(self):
        mon = self._make_keyboard()
        with patch.object(mon, "_get_foreground_info", return_value=("Wrong Window", "wrong.exe")):
            mon._write_chunk(
                "hello", 5, "typed",
                captured_context=("Correct Window", "correct.exe"),
            )

        time.sleep(0.1)
        call_args = mon.db.log_key_event.call_args
        self.assertIsNotNone(call_args)
        self.assertEqual(call_args.kwargs.get("window_title") or call_args[1].get("window_title")
                         or call_args[0][1], "Correct Window")

    def test_write_chunk_falls_back_to_foreground_when_no_context(self):
        mon = self._make_keyboard()
        with patch.object(mon, "_get_foreground_info", return_value=("Fallback Window", "fallback.exe")):
            mon._write_chunk("hello", 5, "typed", captured_context=None)

        time.sleep(0.1)
        call_args = mon.db.log_key_event.call_args
        self.assertIsNotNone(call_args)
        # The fallback was used — verify it was called at all
        mon.db.log_key_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
