"""Activity classification — map a browser tab (URL + title) to a usage category.

Applied at query/analysis time (not at collection). Turns raw dwell into
category buckets so reports and the AI analysis can reason about *what kind* of
time was spent, not just how much.

Categories
----------
  work     — productive tools and sites
  comms    — messaging and email
  research — documentation, reference, search
  ambient  — always-on background media (streaming) that runs concurrently with
             work. Deliberately its OWN bucket: never counted as work, never
             counted as leisure/wasted. This user always has something playing,
             so counting streaming hours against them would be wrong.
  leisure  — genuine downtime
  other    — unclassified

Matching
--------
Rules are ordered; the first match wins. Each rule matches case-insensitive
substrings against the full URL and/or the tab title. Title matching matters for
services that share a domain with something else — BBC iPlayer lives on
bbc.co.uk (also news) and Prime Video on amazon.co.uk (also shopping), so those
are matched by URL *path* and by title ("iplayer", "prime video"), never by the
bare domain.

Rules come from built-in defaults below plus an ``activity_classification``
block in ``config.json``, merged so config extends (and can override earlier)
the defaults.
"""

import json
import os
import threading

WORK = "work"
COMMS = "comms"
RESEARCH = "research"
AMBIENT = "ambient"
LEISURE = "leisure"
OTHER = "other"

CATEGORIES = (WORK, COMMS, RESEARCH, AMBIENT, LEISURE, OTHER)

# Categories that are neutral for productivity accounting: their time is neither
# "work" nor "wasted". Reports should reconcile totals with these set aside.
NEUTRAL_CATEGORIES = (AMBIENT,)


# Built-in rules. Each is (category, url_substrings, title_substrings).
# A rule matches if ANY url substring is in the URL OR ANY title substring is in
# the title. Order matters — earlier rules win.
_DEFAULT_RULES = [
    # --- Ambient / background streaming (concurrent with work) ---------------
    # YouTube is deliberately NOT here — it is genuinely dual-use (tutorials vs.
    # background music). Add "youtube.com/watch" via config if you want it here.
    (AMBIENT,
     ["channel4.com", "bbc.co.uk/iplayer", "itv.com", "itvx.com",
      "primevideo.com", "amazon.co.uk/gp/video", "amazon.co.uk/video",
      "vidmoly", "netflix.com", "disneyplus.com"],
     ["iplayer", "prime video", "channel 4", "all 4", "itvx"]),

    # --- Comms ---------------------------------------------------------------
    (COMMS,
     ["mail.google.com", "mail.yahoo.com", "outlook.", "web.whatsapp.com",
      "web.telegram.org", "slack.com", "discord.com"],
     []),

    # --- Work ----------------------------------------------------------------
    (WORK,
     ["claude.ai", "platform.claude.com", "chatgpt.com", "github.com",
      "gitlab.com", "localhost", "binance.com", "dataannotation.tech",
      "vercel.app", "solvx.uk"],
     []),

    # --- Research ------------------------------------------------------------
    (RESEARCH,
     ["stackoverflow.com", "docs.", "developer.mozilla.org", "readthedocs",
      "google.com/search", "wikipedia.org", "arxiv.org", "huggingface.co"],
     []),
]


def _match(substrings, value):
    v = (value or "").lower()
    return any(s in v for s in substrings)


class Classifier:
    """Ordered rule set mapping (url, title) to a category."""

    def __init__(self, rules=None):
        self.rules = rules if rules is not None else list(_DEFAULT_RULES)

    @classmethod
    def from_config(cls, config):
        """Built-in defaults, with config rules prepended so they win first."""
        block = (config or {}).get("activity_classification", {}) or {}
        extra = []
        # Custom ambient patterns are the common case; support them directly.
        amb_urls = block.get("ambient_url_patterns", [])
        amb_titles = block.get("ambient_title_patterns", [])
        if amb_urls or amb_titles:
            extra.append((AMBIENT, list(amb_urls), list(amb_titles)))
        # Generic per-category rule list: [{category, url_patterns, title_patterns}]
        for rule in block.get("rules", []):
            cat = rule.get("category")
            if cat in CATEGORIES:
                extra.append((cat, list(rule.get("url_patterns", [])),
                              list(rule.get("title_patterns", []))))
        return cls(extra + list(_DEFAULT_RULES))

    def classify(self, url="", title=""):
        for category, url_subs, title_subs in self.rules:
            if _match(url_subs, url) or _match(title_subs, title):
                return category
        return OTHER

    def is_neutral(self, url="", title=""):
        return self.classify(url=url, title=title) in NEUTRAL_CATEGORIES


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


def get_classifier():
    """Return the process-wide Classifier, loading config on first use."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = Classifier.from_config(_load_config())
    return _instance
