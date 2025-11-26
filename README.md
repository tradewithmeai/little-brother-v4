# Little Brother v4

A comprehensive system monitoring tool that tracks various aspects of computer activity including active windows, browser tabs, filesystem changes, and mouse interactions.

## Features

- **Active Window Monitoring**: Track which applications and windows are in focus
- **Browser Tab Tracking**: Monitor browsing activity and tab usage
- **Filesystem Monitoring**: Detect and log file system changes
- **Mouse Click Detection**: Record mouse interaction patterns
- **SQLite Database Storage**: All monitoring data stored in a local database for analysis

## Project Structure

```
little_brother/
├── db/
│   ├── database.py      # Database management
│   └── __init__.py
├── monitors/
│   ├── active_window.py  # Window activity tracking
│   ├── browser_tabs.py   # Browser monitoring
│   ├── filesystem.py     # File system changes
│   ├── mouse_clicks.py   # Mouse interaction tracking
│   └── __init__.py
├── main.py              # Main application entry point
└── test_run.py          # Testing utilities
```

## Requirements

- Python 3.7+
- SQLite3

## Installation

1. Clone the repository:
```bash
git clone https://github.com/tradewithmeai/little-brother-v4.git
cd little-brother-v4
```

2. Install required dependencies:
```bash
pip install -r requirements.txt
```

## Usage

Run the main application:
```bash
python -m little_brother.main
```

For testing:
```bash
python -m little_brother.test_run
```

## Configuration

Configuration options can be adjusted in the respective monitor modules to customize:
- Monitoring intervals
- Data retention periods
- Logging verbosity
- Database location

## Privacy Notice

This tool is designed for personal productivity tracking and system monitoring. Ensure you have appropriate permissions and comply with local privacy laws when using monitoring software.

## License

See LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
