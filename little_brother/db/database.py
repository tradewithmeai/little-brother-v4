import sqlite3
import queue
import threading
import time
import os
import json
import datetime


def load_config():
    """Load configuration from config.json."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    with open(config_path, "r") as f:
        return json.load(f)


class Database:
    """Thread-safe database manager for Little Brother events."""

    def __init__(self, db_path="little_brother.db", event_bus=None):
        """Initialize database connection and start writer thread.

        Args:
            db_path: Path to SQLite database file
            event_bus: Optional EventBus instance for real-time event publishing
        """
        self.db_path = db_path
        self.event_bus = event_bus
        self.event_queue = queue.Queue()
        self._dropped_events = 0
        self._queue_cap = 500
        self.running = True

        # Create database connection
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        # Load schema
        self.load_schema()

        # Start writer thread
        self.start_writer_thread()

    def load_schema(self):
        """Load and execute schema.sql to create tables."""
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, "r") as f:
            schema_sql = f.read()

        cursor = self.conn.cursor()
        cursor.executescript(schema_sql)
        self._migrate(cursor)
        self.conn.commit()
        print(f"Schema loaded from {schema_path}")

    def _migrate(self, cursor):
        """Apply additive migrations for existing databases."""
        existing_file = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(file_events)").fetchall()
        }
        if "source_tag" not in existing_file:
            cursor.execute(
                "ALTER TABLE file_events ADD COLUMN source_tag TEXT DEFAULT 'human'"
            )
            print("[DB] Migrated: added source_tag column to file_events")

        existing_file = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(file_events)").fetchall()
        }
        if "workspace" not in existing_file:
            cursor.execute(
                "ALTER TABLE file_events ADD COLUMN workspace TEXT DEFAULT NULL"
            )
            print("[DB] Migrated: added workspace column to file_events")
        if "file_class" not in existing_file:
            cursor.execute(
                "ALTER TABLE file_events ADD COLUMN file_class TEXT DEFAULT NULL"
            )
            print("[DB] Migrated: added file_class column to file_events")

        existing_key = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(key_events)").fetchall()
        }
        if "input_method" not in existing_key:
            cursor.execute(
                "ALTER TABLE key_events ADD COLUMN input_method TEXT DEFAULT NULL"
            )
            print("[DB] Migrated: added input_method column to key_events")

        existing_browser = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(browser_tab_events)").fetchall()
        }
        if "duration_ms" not in existing_browser:
            cursor.execute(
                "ALTER TABLE browser_tab_events ADD COLUMN duration_ms INTEGER DEFAULT NULL"
            )
            print("[DB] Migrated: added duration_ms column to browser_tab_events")
        if "is_foreground" not in existing_browser:
            cursor.execute(
                "ALTER TABLE browser_tab_events ADD COLUMN is_foreground INTEGER DEFAULT NULL"
            )
            print("[DB] Migrated: added is_foreground column to browser_tab_events")

        existing_browser = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(browser_tab_events)").fetchall()
        }
        if "tab_id" not in existing_browser:
            cursor.execute(
                "ALTER TABLE browser_tab_events ADD COLUMN tab_id TEXT DEFAULT NULL"
            )
            print("[DB] Migrated: added tab_id column to browser_tab_events")

        existing_file2 = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(file_events)").fetchall()
        }
        if "file_size" not in existing_file2:
            cursor.execute(
                "ALTER TABLE file_events ADD COLUMN file_size INTEGER DEFAULT NULL"
            )
            print("[DB] Migrated: added file_size column to file_events")

        existing_mouse = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(mouse_click_events)").fetchall()
        }
        if "process_name" not in existing_mouse:
            cursor.execute(
                "ALTER TABLE mouse_click_events ADD COLUMN process_name TEXT DEFAULT NULL"
            )
            print("[DB] Migrated: added process_name column to mouse_click_events")

        # Performance indexes — safe to run repeatedly via IF NOT EXISTS
        cursor.executescript("""
            CREATE INDEX IF NOT EXISTS idx_file_events_ts        ON file_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_file_events_class     ON file_events(file_class, source_tag);
            CREATE INDEX IF NOT EXISTS idx_file_events_workspace ON file_events(workspace);
            CREATE INDEX IF NOT EXISTS idx_active_window_ts      ON active_window_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_mouse_click_ts        ON mouse_click_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_key_events_ts         ON key_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_browser_tab_ts        ON browser_tab_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_browser_tab_fg        ON browser_tab_events(is_foreground, timestamp);
            CREATE INDEX IF NOT EXISTS idx_file_events_class_ts  ON file_events(file_class, timestamp);
            CREATE INDEX IF NOT EXISTS idx_file_events_source_ts ON file_events(source_tag, timestamp);
        """)

        tables = {
            row[0]
            for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "key_events" not in tables:
            cursor.execute("""
                CREATE TABLE key_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    window_title TEXT,
                    process_name TEXT,
                    text_chunk TEXT,
                    key_count INTEGER,
                    suppressed INTEGER DEFAULT 0
                )
            """)
            print("[DB] Migrated: created key_events table")

    def write_event(self, table, data_dict):
        """Queue an event to be written to the database.

        Args:
            table: Table name (e.g., 'active_window_events')
            data_dict: Dictionary of column names to values
        """
        if self.event_queue.qsize() >= self._queue_cap:
            self._dropped_events += 1
            if self._dropped_events % 100 == 1:
                print(f"[DB] Queue full ({self._queue_cap}), dropping events (total dropped: {self._dropped_events})")
            return
        self.event_queue.put((table, data_dict))

        if self.event_bus:
            from ..events import Event, TABLE_TO_EVENT_TYPE
            evt = Event(
                event_type=TABLE_TO_EVENT_TYPE.get(table, table),
                table=table,
                data=dict(data_dict),
                timestamp=data_dict.get("timestamp", ""),
            )
            self.event_bus.publish(evt)

    def start_writer_thread(self):
        """Start the background writer thread."""
        self.writer_thread = threading.Thread(target=self.writer_loop, daemon=True)
        self.writer_thread.start()
        print("Database writer thread started")

    def writer_loop(self):
        """Main loop for writer thread - processes queued events."""
        while self.running:
            try:
                # Block until at least one event arrives
                try:
                    first = self.event_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                # Drain up to 49 more events that are already queued
                batch = [first]
                try:
                    while len(batch) < 50:
                        batch.append(self.event_queue.get_nowait())
                except queue.Empty:
                    pass

                # Execute all inserts and commit once
                cursor = self.conn.cursor()
                for table, data_dict in batch:
                    columns = ", ".join(data_dict.keys())
                    placeholders = ", ".join(["?" for _ in data_dict])
                    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
                    cursor.execute(sql, list(data_dict.values()))
                self.conn.commit()

                for _ in batch:
                    self.event_queue.task_done()

            except Exception as e:
                print(f"Error in writer loop: {e}")

    def stop(self):
        """Gracefully shut down the database writer."""
        print("Stopping database writer...")
        self.running = False

        # Drain up to 2 seconds worth of queued events, then give up.
        # Losing queued events on shutdown is acceptable for a monitoring app.
        try:
            self.writer_thread.join(timeout=2.0)
        except Exception:
            pass

        self.conn.close()
        remaining = self.event_queue.qsize()
        if remaining:
            print(f"Database stopped (discarded {remaining} queued events)")
        else:
            print("Database stopped cleanly")

    # Insert wrapper methods

    def log_active_window(self, timestamp, window_title, process_name, process_path, hwnd):
        """Log an active window event.

        Args:
            timestamp: ISO format timestamp string
            window_title: Title of the active window
            process_name: Name of the process
            process_path: Full path to the process executable
            hwnd: Windows handle identifier
        """
        self.write_event("active_window_events", {
            "timestamp": timestamp,
            "window_title": window_title,
            "process_name": process_name,
            "process_path": process_path,
            "hwnd": hwnd
        })

    def log_mouse_click(self, timestamp, button, x, y, window_title, process_name=None):
        self.write_event("mouse_click_events", {
            "timestamp": timestamp,
            "button": button,
            "x": x,
            "y": y,
            "window_title": window_title,
            "process_name": process_name,
        })

    def log_browser_tab(self, timestamp, browser, event_type, title, url):
        """Log a browser tab event.

        Args:
            timestamp: ISO format timestamp string
            browser: Browser name (e.g., 'chrome', 'firefox')
            event_type: Type of event ('created', 'updated', 'activated', 'removed')
            title: Page title
            url: Page URL
        """
        self.write_event("browser_tab_events", {
            "timestamp": timestamp,
            "browser": browser,
            "event_type": event_type,
            "title": title,
            "url": url
        })

    def log_key_event(self, timestamp, window_title, process_name, text_chunk, key_count, suppressed=0, input_method=None):
        self.write_event("key_events", {
            "timestamp": timestamp,
            "window_title": window_title,
            "process_name": process_name,
            "text_chunk": text_chunk,
            "key_count": key_count,
            "suppressed": suppressed,
            "input_method": input_method,
        })

    def log_file_event(self, timestamp, event_type, src_path, is_directory, source_tag="human", workspace=None, file_class=None, file_size=None):
        self.write_event("file_events", {
            "timestamp": timestamp,
            "event_type": event_type,
            "src_path": src_path,
            "is_directory": is_directory,
            "source_tag": source_tag,
            "workspace": workspace,
            "file_class": file_class,
            "file_size": file_size,
        })


if __name__ == "__main__":
    print("Testing database module...")

    # Create database instance
    db = Database("test_little_brother.db")

    # Insert a dummy active window event
    timestamp = datetime.datetime.utcnow().isoformat()
    db.log_active_window(
        timestamp=timestamp,
        window_title="Test Window - Notepad",
        process_name="notepad.exe",
        process_path="C:\\Windows\\System32\\notepad.exe",
        hwnd=123456
    )

    print(f"Logged dummy active window event at {timestamp}")

    # Wait for event to be processed
    time.sleep(0.5)

    # Stop database
    db.stop()

    print("Database test completed successfully!")
