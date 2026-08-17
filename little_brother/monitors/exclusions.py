"""Privacy exclusions — activity that monitors must not record in full.

Single source of truth for "do not monitor this", consulted by every capture
point. There are two tiers:

  * **Full exclusion** — the activity is invisible to the entire system. No
    window switch, no click, no browser tab, no keystroke is ever stored. Use
    for things you never want to know happened at all.

  * **Keystroke exclusion** — the app's *usage* is recorded normally (window
    focus, time spent, clicks, browser tabs) but keystrokes typed into it are
    never captured. Use for private-content apps you still want visibility of:
    you can see that WhatsApp Web was open for 20 minutes without recording a
    single character of the messages.

A full exclusion implies keystroke exclusion (if you can't see it happened, you
certainly can't see what was typed).

Rules come from built-in defaults below plus a ``privacy_exclusions`` block in
``config.json``, merged together. Matching is case-insensitive substring
matching on window title and/or URL. Monitors that only see a native window
title (keyboard, mouse, active window) match on the title alone; because a
browser puts the page/tab name into its window title, a title rule like
``"whatsapp"`` reaches WhatsApp Web even though only the browser monitors ever
see the ``web.whatsapp.com`` URL. Matching never keys off process name, so a
rule targeting a web app never disables monitoring of its host browser.
"""

import json
import os
import threading


# Built-in FULL exclusions — invisible to the whole system. Case-insensitive
# substrings. Conservative by design; anything here leaves no trace at all.
_DEFAULT_TITLE_PATTERNS = []
_DEFAULT_URL_PATTERNS = []

# Built-in KEYSTROKE exclusions — usage is recorded, keystrokes are not.
_DEFAULT_KEYSTROKE_TITLE_PATTERNS = [
    "whatsapp",          # WhatsApp Web tab title (browser window title) + desktop app
]
_DEFAULT_KEYSTROKE_URL_PATTERNS = [
    "web.whatsapp.com",  # WhatsApp Web
]


def _match(patterns, value):
    v = (value or "").lower()
    return any(p in v for p in patterns)


class PrivacyExclusions:
    """Immutable set of full and keystroke-only exclusion rules."""

    def __init__(self, title_patterns=None, url_patterns=None,
                 keystroke_title_patterns=None, keystroke_url_patterns=None):
        self.title_patterns = [p.lower() for p in (title_patterns or []) if p]
        self.url_patterns = [p.lower() for p in (url_patterns or []) if p]
        self.keystroke_title_patterns = [p.lower() for p in (keystroke_title_patterns or []) if p]
        self.keystroke_url_patterns = [p.lower() for p in (keystroke_url_patterns or []) if p]

    @classmethod
    def from_config(cls, config):
        """Build from a loaded config dict, merged with the built-in defaults."""
        block = (config or {}).get("privacy_exclusions", {}) or {}
        return cls(
            title_patterns=_DEFAULT_TITLE_PATTERNS + list(block.get("title_patterns", [])),
            url_patterns=_DEFAULT_URL_PATTERNS + list(block.get("url_patterns", [])),
            keystroke_title_patterns=(
                _DEFAULT_KEYSTROKE_TITLE_PATTERNS + list(block.get("keystroke_title_patterns", []))
            ),
            keystroke_url_patterns=(
                _DEFAULT_KEYSTROKE_URL_PATTERNS + list(block.get("keystroke_url_patterns", []))
            ),
        )

    def is_excluded(self, title="", url=""):
        """True if this activity must not be recorded by any monitor at all.

        Used by the usage monitors (active window, mouse, browser tabs, ingest).
        Process name is deliberately ignored so a web-app rule never silences
        its browser.
        """
        return _match(self.url_patterns, url) or _match(self.title_patterns, title)

    def exclude_keystrokes(self, title="", url=""):
        """True if keystrokes typed here must not be captured.

        Fully excluded targets qualify automatically; keystroke-only targets
        (e.g. WhatsApp) qualify while their usage is still recorded elsewhere.
        Used by the keyboard monitor.
        """
        if self.is_excluded(title=title, url=url):
            return True
        return (
            _match(self.keystroke_url_patterns, url)
            or _match(self.keystroke_title_patterns, title)
        )


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
