"""Privacy exclusions — activity that must never be recorded by any monitor.

This is the single source of truth for "do not monitor this". Every monitor
(keyboard, mouse, active window, browser tabs, and the Firefox extension ingest
endpoint) consults the shared instance returned by ``get_exclusions()``.

Matched activity is dropped entirely — it is never written to the database, not
even as a ``[SUPPRESSED]`` placeholder. That distinction matters: suppression
still records *that* something happened (a keystroke count, a window switch);
exclusion leaves no trace at all.

Rules come from two places, merged together:

  * the built-in defaults below (sensible privacy defaults shipped with the app)
  * the ``privacy_exclusions`` block in ``config.json`` (user extensions)

Matching is case-insensitive substring matching. A monitor that can see a URL
(browser tabs, extension ingest) matches on both URL and window title; monitors
that only see a native window title (keyboard, mouse, active window) match on the
title alone. Because a browser puts the page/tab name into its window title, a
title rule like ``"whatsapp"`` reaches WhatsApp Web across every monitor even
though only the browser monitors ever see the ``web.whatsapp.com`` URL.

Note: matching never keys off process name, so excluding a web app does not
disable monitoring of its host browser — only the browser windows/tabs whose
title or URL matches are dropped.
"""

import json
import os
import threading


# Built-in defaults. Case-insensitive substrings. Keep this list conservative —
# anything here is invisible to the entire system by design.
_DEFAULT_TITLE_PATTERNS = [
    "whatsapp",          # WhatsApp Web tab title (browser window title) + desktop app
]
_DEFAULT_URL_PATTERNS = [
    "web.whatsapp.com",  # WhatsApp Web
]


class PrivacyExclusions:
    """Immutable set of title/URL substring rules used to drop activity."""

    def __init__(self, title_patterns=None, url_patterns=None):
        self.title_patterns = [p.lower() for p in (title_patterns or []) if p]
        self.url_patterns = [p.lower() for p in (url_patterns or []) if p]

    @classmethod
    def from_config(cls, config):
        """Build from a loaded config dict, merged with the built-in defaults."""
        block = (config or {}).get("privacy_exclusions", {}) or {}
        titles = list(_DEFAULT_TITLE_PATTERNS) + list(block.get("title_patterns", []))
        urls = list(_DEFAULT_URL_PATTERNS) + list(block.get("url_patterns", []))
        return cls(titles, urls)

    def match_title(self, title):
        t = (title or "").lower()
        return any(p in t for p in self.title_patterns)

    def match_url(self, url):
        u = (url or "").lower()
        return any(p in u for p in self.url_patterns)

    def is_excluded(self, title="", url=""):
        """True if this activity must not be recorded.

        A URL match or a window-title match is sufficient. Process name is
        deliberately ignored so excluding a web app never silences its browser.
        """
        return self.match_url(url) or self.match_title(title)


# --- Shared singleton -------------------------------------------------------

_lock = threading.Lock()
_instance = None


def _config_path():
    return os.path.join(os.path.dirname(__file__), "..", "config.json")


def _load_config():
    try:
        with open(_config_path(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


def get_exclusions():
    """Return the process-wide PrivacyExclusions, loading config on first use."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = PrivacyExclusions.from_config(_load_config())
    return _instance
