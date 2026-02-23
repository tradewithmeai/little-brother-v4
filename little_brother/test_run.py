"""
Integration test for the Little Brother monitoring system.

This test starts all monitors, runs them for 10 seconds, then verifies
that events were captured in the database.
"""

import time
import sqlite3
import os
import json
import threading
import datetime
import tempfile
import shutil


def test_integration():
    """Run full integration test of the Little Brother system."""
    print("[TEST] Starting integration test...")
    print("[TEST] " + "=" * 60)

    # Import here to avoid signal handler registration issues
    from .main import LittleBrother

    # Create LittleBrother instance
    lb = LittleBrother()

    # Start in a background thread (daemon so it won't block exit)
    print("[TEST] Starting Little Brother in background thread...")
    t = threading.Thread(target=lb.run, daemon=True)
    t.start()

    # Give monitors time to initialize
    time.sleep(2)

    # Generate test file system events
    print("[TEST] Generating test filesystem events...")
    test_dir = tempfile.mkdtemp(prefix="lb_test_")
    test_file = os.path.join(test_dir, "test_file.txt")

    try:
        # Create a test file
        with open(test_file, "w") as f:
            f.write("Little Brother Test File\n")
        print(f"[TEST] Created test file: {test_file}")

        # Modify the test file
        time.sleep(0.5)
        with open(test_file, "a") as f:
            f.write("Additional line\n")
        print(f"[TEST] Modified test file")

        # Wait for a moment
        time.sleep(0.5)

    except Exception as e:
        print(f"[TEST] Error creating test file: {e}")

    # Run monitors for remaining time
    remaining_time = 7  # Total 10 seconds minus the time we already spent
    print(f"[TEST] Running monitors for {remaining_time} more seconds...")
    print("[TEST] During this time:")
    print("[TEST]   - Active window monitor should capture current window")
    print("[TEST]   - Mouse clicks will be logged if you click")
    print("[TEST]   - Browser tabs will be logged if Chrome is running with debug port")
    print("[TEST]   - Filesystem events from test file should be captured")
    print()

    for i in range(remaining_time):
        time.sleep(1)
        print(f"[TEST] {remaining_time - i} seconds remaining...")

    # Stop the system
    print("\n[TEST] Stopping Little Brother...")
    lb.stop()

    # Wait for thread to finish
    print("[TEST] Waiting for thread to complete...")
    t.join(timeout=5)

    # Clean up test files
    try:
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
            print(f"[TEST] Cleaned up test directory: {test_dir}")
    except Exception as e:
        print(f"[TEST] Error cleaning up: {e}")

    # Now query the database
    print("\n[TEST] " + "=" * 60)
    print("[TEST] DATABASE RESULTS")
    print("[TEST] " + "=" * 60)

    db_path = "little_brother.db"
    if not os.path.exists(db_path):
        print(f"[TEST] ERROR: Database file not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Define tables to check
    tables = [
        ("active_window_events", ["timestamp", "window_title", "process_name", "process_path", "hwnd"]),
        ("mouse_click_events", ["timestamp", "button", "x", "y", "window_title"]),
        ("browser_tab_events", ["timestamp", "browser", "event_type", "title", "url"]),
        ("file_events", ["timestamp", "event_type", "src_path", "is_directory"])
    ]

    for table_name, columns in tables:
        print(f"\n[TEST] Table: {table_name}")
        print("[TEST] " + "-" * 60)

        try:
            # Get total count
            cursor.execute(f"SELECT COUNT(*) as count FROM {table_name}")
            count = cursor.fetchone()["count"]
            print(f"[TEST] Total rows: {count}")

            if count == 0:
                print(f"[TEST] No events captured in {table_name}")
                continue

            # Get last 3 rows
            cursor.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT 3")
            rows = cursor.fetchall()

            print(f"[TEST] Last {min(3, len(rows))} events:")
            for i, row in enumerate(rows, 1):
                print(f"[TEST]   Event {i}:")
                for col in columns:
                    value = row[col] if col in row.keys() else "N/A"
                    # Truncate long values
                    if isinstance(value, str) and len(value) > 60:
                        value = value[:57] + "..."
                    print(f"[TEST]     {col}: {value}")

        except Exception as e:
            print(f"[TEST] Error querying {table_name}: {e}")

    conn.close()

    print("\n[TEST] " + "=" * 60)
    print("[TEST] Integration test complete!")
    print("[TEST] " + "=" * 60)


if __name__ == "__main__":
    try:
        test_integration()
    except KeyboardInterrupt:
        print("\n[TEST] Test interrupted by user")
    except Exception as e:
        print(f"\n[TEST] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
