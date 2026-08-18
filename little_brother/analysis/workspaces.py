"""Workspace normalization — canonical names and real-project filtering.

Local workspaces (top-level project folders) and GitHub repo names don't always
agree, and the watch roots collect stray files that masquerade as workspaces.
This module is the single place that:

  * **canonicalizes** a name via an alias map, so a repo and its differently
    named local folder collapse to one key (``mygov-hackathon`` -> ``mygov``);
  * **filters** names that aren't real projects — an explicit denylist
    (``desktop.ini``) plus anything that is obviously a loose file (ends in a
    code/doc/data extension), which happens when a file sits directly in a watch
    root and its own name becomes the "workspace".

Both the session effort layer and the GitHub correlation use these so their
per-project keys line up and the noise drops out.

Config (``workspaces`` block in config.json):
    "workspaces": {
        "aliases":  { "mygov-hackathon": "mygov" },
        "denylist": ["desktop.ini", "thumbs.db"]
    }
"""

import json
import os


_DEFAULT_DENYLIST = {"desktop.ini", "thumbs.db"}

# A workspace whose name ends in one of these is really a loose file, not a
# project folder. (Real project folders with dots like "localtaxpro.co.uk" or
# "solvx.uk" don't end in these, so they survive.)
_FILE_EXTENSIONS = {
    # code / config / data
    ".py", ".md", ".cs", ".html", ".htm", ".db", ".ini", ".json", ".txt",
    ".log", ".tmp", ".js", ".ts", ".sql", ".csv", ".yaml", ".yml", ".sh",
    ".bat", ".ps1", ".cfg", ".xml", ".lock", ".rs", ".go", ".java", ".c",
    ".cpp", ".h", ".rb", ".php", ".ipynb",
    # documents / office
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    ".ods", ".rtf",
    # media / archives / binaries
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4",
    ".mov", ".mp3", ".wav", ".zip", ".gz", ".tar", ".7z", ".exe", ".dll",
}


class Workspaces:
    def __init__(self, aliases=None, denylist=None):
        # Case-insensitive alias lookup.
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self.denylist = {d.lower() for d in (denylist or [])} | _DEFAULT_DENYLIST

    @classmethod
    def from_config(cls, config):
        block = (config or {}).get("workspaces", {}) or {}
        return cls(block.get("aliases"), block.get("denylist"))

    def canonical(self, name):
        if name is None:
            return None
        return self.aliases.get(name.lower(), name)

    def is_real(self, name):
        if not name:
            return False
        low = name.lower()
        if low in self.denylist:
            return False
        _, ext = os.path.splitext(low)
        if ext in _FILE_EXTENSIONS:
            return False
        return True

    def normalize(self, name):
        """Return the canonical name if it is a real project, else None."""
        c = self.canonical(name)
        return c if self.is_real(c) else None


def _load_config():
    path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


_instance = None


def get_workspaces(config=None):
    global _instance
    if config is not None:
        return Workspaces.from_config(config)
    if _instance is None:
        _instance = Workspaces.from_config(_load_config())
    return _instance
