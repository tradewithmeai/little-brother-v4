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
    is_directory INTEGER,
    source_tag TEXT DEFAULT 'human'
);

CREATE TABLE IF NOT EXISTS key_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    window_title TEXT,
    process_name TEXT,
    text_chunk TEXT,
    key_count INTEGER,
    suppressed INTEGER DEFAULT 0
);

-- Commits pulled from GitHub, correlated to local activity by workspace + time.
CREATE TABLE IF NOT EXISTS github_commits (
    sha TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    workspace TEXT,
    committed_at TEXT NOT NULL,
    author TEXT,
    message TEXT,
    additions INTEGER,
    deletions INTEGER,
    files_changed INTEGER,
    url TEXT,
    synced_at TEXT
);
