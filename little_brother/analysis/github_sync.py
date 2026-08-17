"""GitHub commit sync — pull commits and correlate them to local activity.

Transport is the ``gh`` CLI, which is already authenticated, so Little Brother
stores no token of its own. When ``gh`` is unavailable it falls back to a token
from ``config.json`` (``github.token``) or the ``GITHUB_TOKEN`` env var, talking
to the REST API directly.

Only repos whose name matches a known local workspace are synced by default:
the local monitor records a ``workspace`` per file event (the top-level project
folder), and that folder name equals the GitHub repo name (the ``crypto-lake-rs``
repo maps to the ``crypto-lake-rs`` workspace). Syncing the intersection keeps
us to the projects we actually have activity for.

The result is a ``github_commits`` table that the analysis layer joins against
``file_events``/``key_events`` by workspace and time — turning "I typed a lot in
this project" into "…and shipped 4 commits, +812/-190".

Run standalone to sync:  ``python -m little_brother.analysis.github_sync``
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta


def _config():
    path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _gh_available():
    return shutil.which("gh") is not None


def _gh(args, timeout=30):
    """Run `gh <args>` and return stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout,
            # gh emits UTF-8; Windows would otherwise decode as cp1252 and crash
            # on any non-ASCII commit message.
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def _token(config):
    """Resolve an API token: config, env, or `gh auth token`."""
    tok = (config.get("github", {}) or {}).get("token")
    if tok:
        return tok
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    if _gh_available():
        out = _gh(["auth", "token"])
        if out:
            return out.strip()
    return None


class GitHubClient:
    """Minimal GitHub REST reader over gh CLI (preferred) or urllib."""

    def __init__(self, config):
        self.config = config
        self.use_gh = _gh_available()
        self.token = None if self.use_gh else _token(config)

    def ok(self):
        return self.use_gh or bool(self.token)

    def api(self, path, params=None, paginate=False):
        """GET an API path, returning parsed JSON (list or dict), or None."""
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        if self.use_gh:
            args = ["api", path + query]
            if paginate:
                args.append("--paginate")
                # --slurp merges paginated arrays into one JSON array
                args.append("--slurp")
            out = _gh(args, timeout=60)
            if out is None:
                return None
            try:
                data = json.loads(out)
            except Exception:
                return None
            # With --slurp each page is an element; flatten one level for arrays.
            if paginate and isinstance(data, list) and data and isinstance(data[0], list):
                flat = []
                for page in data:
                    flat.extend(page)
                return flat
            return data
        # urllib fallback (single page only)
        url = "https://api.github.com/" + path.lstrip("/") + query
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "little-brother",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def viewer_login(self):
        data = self.api("user")
        return data.get("login") if isinstance(data, dict) else None

    def owned_repo_names(self):
        data = self.api("user/repos", params={"affiliation": "owner", "per_page": 100},
                        paginate=True)
        if not isinstance(data, list):
            return []
        return [r.get("name") for r in data if isinstance(r, dict) and r.get("name")]


_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_github_commits_ws_ts ON github_commits(workspace, committed_at);
CREATE INDEX IF NOT EXISTS idx_github_commits_ts    ON github_commits(committed_at);
"""


def _ensure_schema(conn):
    conn.executescript(_SCHEMA)


def _local_workspaces(conn):
    rows = conn.execute(
        "SELECT DISTINCT workspace FROM file_events WHERE workspace IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows if r[0]}


def sync(conn, config=None, since_days=90, max_stat_fetch=150, repos=None):
    """Pull commits for workspace-matching repos into github_commits.

    Args:
        conn: writable sqlite3 connection.
        config: loaded config dict (defaults to config.json).
        since_days: how far back to fetch commits.
        max_stat_fetch: cap on per-commit stat lookups per run (rate-limit guard).
        repos: explicit repo list; otherwise workspace ∩ owned repos.

    Returns a summary dict.
    """
    config = config if config is not None else _config()
    gh_cfg = config.get("github", {}) or {}
    _ensure_schema(conn)
    client = GitHubClient(config)
    if not client.ok():
        return {"ok": False, "error": "no GitHub auth (install/login gh, or set github.token)"}

    owner = gh_cfg.get("username") or client.viewer_login()
    if not owner:
        return {"ok": False, "error": "could not resolve GitHub username"}

    # Decide which repos to sync.
    if repos is None:
        repos = gh_cfg.get("repos")
    if not repos:
        workspaces = {w.lower(): w for w in _local_workspaces(conn)}
        owned = client.owned_repo_names()
        repos = [name for name in owned if name.lower() in workspaces]

    since_iso = (datetime.utcnow() - timedelta(days=since_days)).isoformat() + "Z"
    author = gh_cfg.get("author", owner)

    existing = {r[0] for r in conn.execute("SELECT sha FROM github_commits").fetchall()}
    synced_at = datetime.utcnow().isoformat()

    total_new = 0
    stats_fetched = 0
    per_repo = {}

    for repo in repos:
        commits = client.api(
            f"repos/{owner}/{repo}/commits",
            params={"author": author, "since": since_iso, "per_page": 100},
            paginate=True,
        )
        if not isinstance(commits, list):
            per_repo[repo] = {"new": 0, "error": True}
            continue

        new_here = 0
        for c in commits:
            sha = c.get("sha")
            if not sha or sha in existing:
                continue
            commit = c.get("commit", {}) or {}
            author_info = commit.get("author", {}) or {}
            committed_at = author_info.get("date") or ""
            message = (commit.get("message") or "").split("\n", 1)[0][:500]
            url = c.get("html_url") or ""

            additions = deletions = files_changed = None
            if stats_fetched < max_stat_fetch:
                detail = client.api(f"repos/{owner}/{repo}/commits/{sha}")
                stats_fetched += 1
                if isinstance(detail, dict):
                    st = detail.get("stats", {}) or {}
                    additions = st.get("additions")
                    deletions = st.get("deletions")
                    files = detail.get("files")
                    files_changed = len(files) if isinstance(files, list) else None

            conn.execute(
                "INSERT OR IGNORE INTO github_commits "
                "(sha, repo, workspace, committed_at, author, message, "
                " additions, deletions, files_changed, url, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, repo, repo, committed_at, author, message,
                 additions, deletions, files_changed, url, synced_at),
            )
            existing.add(sha)
            new_here += 1
            total_new += 1

        per_repo[repo] = {"new": new_here}

    conn.commit()
    return {
        "ok": True,
        "owner": owner,
        "repos_synced": len(repos),
        "new_commits": total_new,
        "stats_fetched": stats_fetched,
        "per_repo": per_repo,
    }


def correlate(conn, since_days=30):
    """Join shipped commits against local file activity, per workspace.

    Commits carry a workspace (== repo name); file_events carry the same
    workspace (the project folder). Keystrokes are per-process, not per-project,
    so they are not attributed here — the honest per-project signal is source
    edits locally vs. commits shipped to GitHub.

    Returns a list of per-workspace rows, most commits first.
    """
    since = (datetime.utcnow() - timedelta(days=since_days)).isoformat()

    commits = {}
    for r in conn.execute(
        """SELECT workspace,
                  COUNT(*)              AS commits,
                  SUM(COALESCE(additions,0)) AS additions,
                  SUM(COALESCE(deletions,0)) AS deletions,
                  COUNT(DISTINCT substr(committed_at,1,10)) AS commit_days,
                  MAX(committed_at)     AS last_commit
           FROM github_commits
           WHERE committed_at >= ?
           GROUP BY workspace""",
        (since,),
    ).fetchall():
        commits[r[0]] = {
            "commits": r[1], "additions": r[2] or 0, "deletions": r[3] or 0,
            "commit_days": r[4], "last_commit": r[5],
        }

    edits = {}
    for r in conn.execute(
        """SELECT workspace,
                  COUNT(*) AS source_edits,
                  COUNT(DISTINCT substr(timestamp,1,10)) AS active_days,
                  MAX(timestamp) AS last_edit
           FROM file_events
           WHERE timestamp >= ? AND file_class = 'source' AND source_tag = 'human'
           GROUP BY workspace""",
        (since,),
    ).fetchall():
        edits[r[0]] = {"source_edits": r[1], "active_days": r[2], "last_edit": r[3]}

    rows = []
    for ws in set(commits) | set(edits):
        c = commits.get(ws, {})
        e = edits.get(ws, {})
        rows.append({
            "workspace": ws,
            "commits": c.get("commits", 0),
            "additions": c.get("additions", 0),
            "deletions": c.get("deletions", 0),
            "commit_days": c.get("commit_days", 0),
            "last_commit": c.get("last_commit"),
            "local_source_edits": e.get("source_edits", 0),
            "local_active_days": e.get("active_days", 0),
            "last_local_edit": e.get("last_edit"),
        })
    rows.sort(key=lambda r: (r["commits"], r["local_source_edits"]), reverse=True)
    return rows


def _main():
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), "..", "..", "little_brother.db")
    conn = sqlite3.connect(os.path.abspath(db_path), timeout=10)
    try:
        result = sync(conn)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_main())
