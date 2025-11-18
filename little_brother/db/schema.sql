CREATE TABLE IF NOT EXISTS active_window_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    window_title TEXT,
    process_name TEXT,
    process_path TEXT,
    hwnd INTEGER
);

CREATE TABLE IF NOT EXISTS mouse_click_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    button TEXT,
    x INTEGER,
    y INTEGER,
    window_title TEXT
);

CREATE TABLE IF NOT EXISTS browser_tab_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    browser TEXT,
    event_type TEXT,
    title TEXT,
    url TEXT
);

CREATE TABLE IF NOT EXISTS file_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT,
    src_path TEXT,
    is_directory INTEGER
);
