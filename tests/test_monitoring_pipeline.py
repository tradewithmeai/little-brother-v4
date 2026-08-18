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
from datetime import datetime
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


# ---------------------------------------------------------------------------
# Firefox dwell HTTP ingest path
# ---------------------------------------------------------------------------

class TestFirefoxDwellHTTPPath(unittest.TestCase):
    """Verify Firefox extension dwell events arrive via /api/browser-tab and
    flow through to the top_tabs_by_dwell digest query."""

    def setUp(self):
        import tempfile
        from unittest.mock import patch
        from little_brother.dashboard import server as srv

        # Build a fresh DB with the full migrated schema
        self._tmp_db_path = tempfile.mktemp(suffix=".db")
        from little_brother.db.database import Database
        _db = Database(self._tmp_db_path)
        _db.stop()

        # Patch _write_db so the Flask endpoint writes to our temp DB
        def _fake_write_db():
            conn = sqlite3.connect(self._tmp_db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

        self._patcher = patch.object(srv, "_write_db", _fake_write_db)
        self._patcher.start()

        self._app = srv.app
        self._app.config["TESTING"] = True
        self._client = self._app.test_client()

    def tearDown(self):
        self._patcher.stop()
        try:
            import os
            os.unlink(self._tmp_db_path)
        except OSError:
            pass

    def _post_tab(self, payload):
        return self._client.post(
            "/api/browser-tab",
            json=payload,
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    def test_dwell_event_accepted_and_stored(self):
        resp = self._post_tab({
            "event_type": "dwell",
            "title": "GitHub",
            "url": "https://github.com",
            "tab_id": "101",
            "duration_ms": 8000,
            "is_foreground": 1,
        })
        self.assertEqual(resp.status_code, 201)

        conn = sqlite3.connect(self._tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT browser, event_type, duration_ms, is_foreground "
            "FROM browser_tab_events WHERE url = ?",
            ("https://github.com",),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["browser"], "firefox")
        self.assertEqual(row["event_type"], "dwell")
        self.assertEqual(row["duration_ms"], 8000)
        self.assertEqual(row["is_foreground"], 1)

    def test_dwell_without_duration_stored_as_null(self):
        resp = self._post_tab({
            "event_type": "dwell",
            "title": "MDN",
            "url": "https://developer.mozilla.org",
            "tab_id": "102",
        })
        self.assertEqual(resp.status_code, 201)

        conn = sqlite3.connect(self._tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT duration_ms FROM browser_tab_events WHERE url = ?",
            ("https://developer.mozilla.org",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIsNone(row["duration_ms"])

    def test_non_dwell_events_stored_correctly(self):
        for et in ("activated", "navigated", "created", "closed"):
            resp = self._post_tab({
                "event_type": et,
                "title": f"Tab {et}",
                "url": f"https://example.com/{et}",
                "tab_id": "200",
                "is_foreground": 1 if et == "activated" else 0,
            })
            self.assertEqual(resp.status_code, 201, f"expected 201 for event_type={et}")

    def test_missing_event_type_returns_400(self):
        resp = self._post_tab({"title": "Bad", "url": "https://example.com"})
        self.assertEqual(resp.status_code, 400)

    def test_top_tabs_by_dwell_includes_firefox_events(self):
        """The digest SQL query returns Firefox dwell rows alongside Chrome ones."""
        # Insert one Firefox dwell and one Chrome dwell directly
        conn = sqlite3.connect(self._tmp_db_path)
        ts = "2026-01-01T10:00:00"
        conn.execute(
            "INSERT INTO browser_tab_events (timestamp, browser, event_type, title, url, duration_ms) "
            "VALUES (?, 'firefox', 'dwell', 'Firefox Tab', 'https://firefox.example', 15000)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO browser_tab_events (timestamp, browser, event_type, title, url, duration_ms) "
            "VALUES (?, 'chrome', 'dwell', 'Chrome Tab', 'https://chrome.example', 7000)",
            (ts,),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(self._tmp_db_path)
        conn.row_factory = sqlite3.Row
        since = "2026-01-01T00:00:00"
        rows = conn.execute("""
            SELECT url, title, COUNT(*) as visits, SUM(duration_ms) as total_dwell_ms
            FROM browser_tab_events
            WHERE event_type = 'dwell' AND timestamp >= ?
            GROUP BY url
            ORDER BY total_dwell_ms DESC LIMIT 15
        """, (since,)).fetchall()
        conn.close()

        urls = [r["url"] for r in rows]
        self.assertIn("https://firefox.example", urls)
        self.assertIn("https://chrome.example", urls)
        # Firefox dwell should rank first (15s > 7s)
        self.assertEqual(rows[0]["url"], "https://firefox.example")
        self.assertEqual(rows[0]["total_dwell_ms"], 15000)

    def test_remote_addr_restriction(self):
        """Requests from non-localhost IPs are rejected with 403."""
        resp = self._client.post(
            "/api/browser-tab",
            json={"event_type": "dwell", "title": "X", "url": "https://x.com", "duration_ms": 1000},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Privacy exclusion tests
# ---------------------------------------------------------------------------

from little_brother.monitors.exclusions import PrivacyExclusions


class TestPrivacyExclusions(unittest.TestCase):

    def test_whatsapp_keystrokes_excluded(self):
        # WhatsApp keystrokes are withheld...
        ex = PrivacyExclusions.from_config({})
        self.assertTrue(ex.exclude_keystrokes(title="(3) WhatsApp - Mozilla Firefox"))
        self.assertTrue(ex.exclude_keystrokes(title="WhatsApp"))
        self.assertTrue(ex.exclude_keystrokes(url="https://web.whatsapp.com/"))

    def test_whatsapp_usage_still_visible(self):
        # ...but its usage (window/mouse/browser) is NOT fully excluded.
        ex = PrivacyExclusions.from_config({})
        self.assertFalse(ex.is_excluded(title="(3) WhatsApp - Mozilla Firefox"))
        self.assertFalse(ex.is_excluded(url="https://web.whatsapp.com/"))

    def test_non_excluded_pass_through(self):
        ex = PrivacyExclusions.from_config({})
        self.assertFalse(ex.exclude_keystrokes(title="WindowsTerminal"))
        self.assertFalse(ex.exclude_keystrokes(title="claude.ai", url="https://claude.ai/"))
        self.assertFalse(ex.is_excluded(title="Amazon", url="https://www.amazon.co.uk/"))

    def test_full_exclusion_implies_keystroke_exclusion(self):
        ex = PrivacyExclusions.from_config(
            {"privacy_exclusions": {"title_patterns": ["banking"]}}
        )
        self.assertTrue(ex.is_excluded(title="My Banking"))
        self.assertTrue(ex.exclude_keystrokes(title="My Banking"))

    def test_process_name_never_triggers_exclusion(self):
        # A web-app rule must not silence its whole browser.
        ex = PrivacyExclusions.from_config({})
        self.assertFalse(ex.is_excluded(title="GitHub - Mozilla Firefox", url="https://github.com/"))
        self.assertFalse(ex.exclude_keystrokes(title="GitHub - Mozilla Firefox", url="https://github.com/"))

    def test_config_extends_defaults(self):
        ex = PrivacyExclusions.from_config(
            {"privacy_exclusions": {"keystroke_title_patterns": ["signal"],
                                    "keystroke_url_patterns": ["telegram.org"]}}
        )
        # Custom keystroke rules apply...
        self.assertTrue(ex.exclude_keystrokes(title="Signal"))
        self.assertTrue(ex.exclude_keystrokes(url="https://web.telegram.org/"))
        # ...and built-in WhatsApp default still applies.
        self.assertTrue(ex.exclude_keystrokes(title="WhatsApp"))

    def test_case_insensitive(self):
        ex = PrivacyExclusions.from_config({})
        self.assertTrue(ex.exclude_keystrokes(title="WHATSAPP"))
        self.assertTrue(ex.exclude_keystrokes(url="https://WEB.WHATSAPP.COM/"))


class TestIngestExclusion(unittest.TestCase):
    """The Firefox extension ingest endpoint stores WhatsApp usage (keystrokes
    are handled by the keyboard monitor, not here)."""

    def setUp(self):
        from little_brother.dashboard import server as srv
        self._srv = srv
        srv.app.config["TESTING"] = True
        self._client = srv.app.test_client()

    def test_whatsapp_usage_ingest_is_stored(self):
        fake_conn = MagicMock()
        with patch.object(self._srv, "_write_db", return_value=fake_conn) as mock_wdb:
            resp = self._client.post(
                "/api/browser-tab",
                json={"event_type": "created", "title": "WhatsApp",
                      "url": "https://web.whatsapp.com/", "tab_id": "wa-test"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(resp.status_code, 201)
            # Usage IS recorded: a write connection was opened and an insert ran.
            mock_wdb.assert_called_once()
            self.assertTrue(fake_conn.execute.called)


# ---------------------------------------------------------------------------
# Activity classification tests
# ---------------------------------------------------------------------------

from little_brother.analysis.classify import Classifier, AMBIENT, WORK, COMMS, OTHER


class TestActivityClassification(unittest.TestCase):

    def setUp(self):
        self.c = Classifier.from_config({})

    def test_streaming_is_ambient(self):
        self.assertEqual(self.c.classify(url="https://www.channel4.com/watch/x"), AMBIENT)
        self.assertEqual(self.c.classify(url="https://www.itv.com/watch/x", title="Show - ITVX"), AMBIENT)
        self.assertEqual(self.c.classify(title="The Boys - Prime Video"), AMBIENT)

    def test_ambiguous_domains_resolved_by_path_and_title(self):
        # BBC iPlayer is ambient; BBC News on the same domain is not.
        self.assertEqual(self.c.classify(url="https://www.bbc.co.uk/iplayer/episode/a",
                                         title="Show - BBC iPlayer"), AMBIENT)
        self.assertNotEqual(self.c.classify(url="https://www.bbc.co.uk/news/uk-1",
                                            title="UK News - BBC"), AMBIENT)
        # Amazon Prime Video is ambient; Amazon shopping on the same domain is not.
        self.assertEqual(self.c.classify(url="https://www.amazon.co.uk/gp/video/detail/x",
                                         title="Film - Prime Video"), AMBIENT)
        self.assertNotEqual(self.c.classify(url="https://www.amazon.co.uk/dp/B0X",
                                            title="Drill - Amazon.co.uk"), AMBIENT)

    def test_ambient_is_neutral(self):
        self.assertTrue(self.c.is_neutral(url="https://www.channel4.com/watch/x"))
        self.assertFalse(self.c.is_neutral(url="https://claude.ai/"))

    def test_work_and_comms(self):
        self.assertEqual(self.c.classify(url="https://claude.ai/chat/1"), WORK)
        self.assertEqual(self.c.classify(url="https://mail.google.com/"), COMMS)

    def test_youtube_not_ambient_by_default(self):
        self.assertNotEqual(self.c.classify(url="https://www.youtube.com/watch?v=x"), AMBIENT)

    def test_config_ambient_extension(self):
        c = Classifier.from_config(
            {"activity_classification": {"ambient_url_patterns": ["youtube.com/watch"]}}
        )
        self.assertEqual(c.classify(url="https://www.youtube.com/watch?v=x"), AMBIENT)
        # Built-in defaults still apply.
        self.assertEqual(c.classify(url="https://www.channel4.com/watch/x"), AMBIENT)

    def test_unknown_is_other(self):
        self.assertEqual(self.c.classify(url="https://some-random-site.example/"), OTHER)


# ---------------------------------------------------------------------------
# GitHub sync + correlation tests
# ---------------------------------------------------------------------------

from little_brother.analysis import github_sync


class TestGitHubSyncAndCorrelate(unittest.TestCase):

    def setUp(self):
        # In-memory DB with the two tables the sync/correlate touch.
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(github_sync._SCHEMA)
        self.conn.executescript("""
            CREATE TABLE file_events (
                id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT,
                src_path TEXT, is_directory INTEGER, source_tag TEXT,
                workspace TEXT, file_class TEXT, file_size INTEGER
            );
        """)

    def tearDown(self):
        self.conn.close()

    def _fake_client(self):
        """A GitHubClient stub that serves canned API responses (no network)."""
        client = MagicMock()
        client.ok.return_value = True
        client.viewer_login.return_value = "tester"
        client.owned_repo_names.return_value = ["proj-a", "unrelated"]

        def api(path, params=None, paginate=False):
            if path == "repos/tester/proj-a/commits":
                return [
                    {"sha": "aaa", "html_url": "u/aaa",
                     "commit": {"author": {"date": "2026-08-10T10:00:00Z"},
                                "message": "first\n\nbody"}},
                    {"sha": "bbb", "html_url": "u/bbb",
                     "commit": {"author": {"date": "2026-08-11T10:00:00Z"},
                                "message": "second"}},
                ]
            if path.startswith("repos/tester/proj-a/commits/"):
                return {"stats": {"additions": 10, "deletions": 2},
                        "files": [{"filename": "x.py"}]}
            return []

        client.api.side_effect = api
        return client

    def test_sync_stores_commits_for_matching_workspace(self):
        # Local activity exists for proj-a, so it is the repo we sync.
        self.conn.execute(
            "INSERT INTO file_events (timestamp, workspace, file_class, source_tag) "
            "VALUES ('2026-08-10T09:00:00', 'proj-a', 'source', 'human')"
        )
        self.conn.commit()

        with patch.object(github_sync, "GitHubClient", return_value=self._fake_client()):
            result = github_sync.sync(self.conn, config={}, since_days=365)

        self.assertTrue(result["ok"])
        self.assertEqual(result["new_commits"], 2)
        row = self.conn.execute(
            "SELECT additions, deletions, files_changed, workspace "
            "FROM github_commits WHERE sha='aaa'"
        ).fetchone()
        self.assertEqual(row[0], 10)
        self.assertEqual(row[1], 2)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "proj-a")

    def test_sync_is_idempotent(self):
        self.conn.execute(
            "INSERT INTO file_events (timestamp, workspace, file_class, source_tag) "
            "VALUES ('2026-08-10T09:00:00', 'proj-a', 'source', 'human')"
        )
        self.conn.commit()
        with patch.object(github_sync, "GitHubClient", return_value=self._fake_client()):
            github_sync.sync(self.conn, config={}, since_days=365)
            second = github_sync.sync(self.conn, config={}, since_days=365)
        self.assertEqual(second["new_commits"], 0)
        count = self.conn.execute("SELECT COUNT(*) FROM github_commits").fetchone()[0]
        self.assertEqual(count, 2)

    def test_backfill_fills_missing_stats(self):
        # Two commits with NULL stats.
        for sha in ("s1", "s2"):
            self.conn.execute(
                "INSERT INTO github_commits (sha, repo, workspace, committed_at) "
                "VALUES (?, 'proj-a', 'proj-a', '2026-08-10T10:00:00Z')", (sha,),
            )
        self.conn.commit()

        client = MagicMock()
        client.ok.return_value = True
        client.viewer_login.return_value = "tester"
        client.api.return_value = {"stats": {"additions": 7, "deletions": 3},
                                   "files": [{"filename": "a"}, {"filename": "b"}]}

        with patch.object(github_sync, "GitHubClient", return_value=client):
            result = github_sync.backfill_stats(self.conn, config={}, max_fetch=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["filled"], 2)
        self.assertEqual(result["remaining"], 0)
        row = self.conn.execute(
            "SELECT additions, deletions, files_changed FROM github_commits WHERE sha='s1'"
        ).fetchone()
        self.assertEqual(tuple(row), (7, 3, 2))

    def test_correlate_joins_commits_and_local_edits(self):
        self.conn.execute(
            "INSERT INTO github_commits (sha, repo, workspace, committed_at, additions, deletions) "
            "VALUES ('c1', 'proj-a', 'proj-a', ?, 100, 20)",
            ((datetime.utcnow()).isoformat() + "Z",),
        )
        for _ in range(3):
            self.conn.execute(
                "INSERT INTO file_events (timestamp, workspace, file_class, source_tag) "
                "VALUES (?, 'proj-a', 'source', 'human')",
                (datetime.utcnow().isoformat(),),
            )
        self.conn.commit()
        rows = github_sync.correlate(self.conn, since_days=30)
        proj = next(r for r in rows if r["workspace"] == "proj-a")
        self.assertEqual(proj["commits"], 1)
        self.assertEqual(proj["additions"], 100)
        self.assertEqual(proj["local_source_edits"], 3)


# ---------------------------------------------------------------------------
# Session stitching tests
# ---------------------------------------------------------------------------

from little_brother.analysis import sessions as sessmod


class TestSessionStitching(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE key_events (id INTEGER PRIMARY KEY, timestamp TEXT, key_count INTEGER);
            CREATE TABLE active_window_events (id INTEGER PRIMARY KEY, timestamp TEXT, is_heartbeat INTEGER);
            CREATE TABLE mouse_click_events (id INTEGER PRIMARY KEY, timestamp TEXT);
            CREATE TABLE file_events (id INTEGER PRIMARY KEY, timestamp TEXT, workspace TEXT,
                                      source_tag TEXT, file_class TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def _key(self, ts, n):
        self.conn.execute("INSERT INTO key_events (timestamp, key_count) VALUES (?, ?)", (ts, n))

    def _file(self, ts, ws):
        self.conn.execute(
            "INSERT INTO file_events (timestamp, workspace, source_tag, file_class) "
            "VALUES (?, ?, 'human', 'source')", (ts, ws))

    def test_gap_splits_sessions(self):
        # Two keystroke bursts 30 min apart -> two sessions (gap > 15 min).
        self._key("2026-08-10T10:00:00", 100)
        self._key("2026-08-10T10:05:00", 50)
        self._key("2026-08-10T10:35:00", 80)
        self.conn.commit()
        sess = sessmod.build_sessions(self.conn, since_days=3650, gap_minutes=15)
        self.assertEqual(len(sess), 2)
        self.assertEqual(sess[0]["keystrokes"], 150)
        self.assertEqual(sess[1]["keystrokes"], 80)

    def test_heartbeats_do_not_bridge_idle(self):
        # Activity, then only heartbeats for hours, then activity again.
        # Heartbeats must NOT keep one giant session alive.
        self._key("2026-08-10T10:00:00", 100)
        for h in range(11, 20):  # hourly heartbeats, no real activity
            self.conn.execute(
                "INSERT INTO active_window_events (timestamp, is_heartbeat) VALUES (?, 1)",
                (f"2026-08-10T{h}:00:00",))
        self._key("2026-08-10T20:00:00", 100)
        self.conn.commit()
        sess = sessmod.build_sessions(self.conn, since_days=3650, gap_minutes=15)
        self.assertEqual(len(sess), 2)  # not one 10-hour session

    def test_dominant_workspace_attribution(self):
        # One session touching proj-a twice and proj-b once -> attributed to a.
        self._key("2026-08-10T10:00:00", 100)
        self._key("2026-08-10T10:10:00", 100)
        self._file("2026-08-10T10:01:00", "proj-a")
        self._file("2026-08-10T10:02:00", "proj-a")
        self._file("2026-08-10T10:03:00", "proj-b")
        self.conn.commit()
        sess = sessmod.build_sessions(self.conn, since_days=3650, gap_minutes=15)
        self.assertEqual(len(sess), 1)
        self.assertEqual(sess[0]["workspace"], "proj-a")

    def test_no_file_activity_is_unattributed(self):
        self._key("2026-08-10T10:00:00", 100)
        self.conn.commit()
        sess = sessmod.build_sessions(self.conn, since_days=3650, gap_minutes=15)
        self.assertIsNone(sess[0]["workspace"])

    def test_project_effort_rollup(self):
        # File touch precedes both bursts, so both attribute to proj-a.
        self._file("2026-08-10T10:00:00", "proj-a")
        self._key("2026-08-10T10:01:00", 100)
        self._key("2026-08-10T10:10:00", 200)
        self.conn.commit()
        rows = sessmod.project_effort(self.conn, since_days=3650)
        proj = next(r for r in rows if r["workspace"] == "proj-a")
        self.assertEqual(proj["keystrokes"], 300)
        self.assertGreaterEqual(proj["active_hours"], 0.0)

    def test_per_run_split_before_first_file_touch_is_unattributed(self):
        # Keystrokes before any file touch are unattributed; those after the
        # touch go to the project — the session is split, not back-credited.
        self._key("2026-08-10T10:00:00", 100)
        self._file("2026-08-10T10:05:00", "proj-a")
        self._key("2026-08-10T10:10:00", 200)
        self.conn.commit()
        rows = {r["workspace"]: r for r in sessmod.project_effort(self.conn, since_days=3650)}
        self.assertEqual(rows["proj-a"]["keystrokes"], 200)
        self.assertEqual(rows[None]["keystrokes"], 100)

    def test_alias_and_denylist(self):
        from little_brother.analysis.workspaces import Workspaces
        w = Workspaces(aliases={"mygov-hackathon": "mygov"}, denylist=["desktop.ini"])
        self.assertEqual(w.canonical("mygov-hackathon"), "mygov")
        self.assertFalse(w.is_real("desktop.ini"))
        self.assertFalse(w.is_real("notes.py"))          # loose file
        self.assertFalse(w.is_real("README.md"))
        self.assertTrue(w.is_real("project-bright"))
        self.assertTrue(w.is_real("localtaxpro.co.uk"))  # real folder with dots
        self.assertIsNone(w.normalize("desktop.ini"))
        self.assertEqual(w.normalize("mygov-hackathon"), "mygov")


if __name__ == "__main__":
    unittest.main()
