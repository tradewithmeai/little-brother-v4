"""Authenticated, read-only desktop explorer and bounded terminal bridge."""

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

from flask import jsonify, request

from .auth import require_api_key


MAX_ENTRIES = 500
MAX_FILE_BYTES = 256_000
MAX_OUTPUT_CHARS = 100_000
GIT_SUBCOMMANDS = {"status", "log", "diff", "branch", "show", "rev-parse"}
RG_FLAGS = {"-n", "--line-number", "-i", "--ignore-case", "-l", "--files-with-matches",
            "-F", "--fixed-strings", "-w", "--word-regexp", "-S", "--smart-case", "--files"}
SENSITIVE_DIRECTORIES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", "secrets", "credentials",
}
SENSITIVE_FILENAMES = {
    "credentials", "credentials.json", "google_token.json", "token.json",
    "hosts.yml", "id_rsa", "id_ed25519",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
RG_EXCLUDES = (
    "!.ssh/**", "!.gnupg/**", "!.aws/**", "!.azure/**", "!.kube/**",
    "!secrets/**", "!credentials/**", "!.env*", "!*.key", "!*.pem", "!*.p12", "!*.pfx",
)


class ConsolePolicyError(ValueError):
    pass


def _roots():
    configured = os.getenv("LB_CONSOLE_ROOTS", "").strip()
    if configured:
        raw = json.loads(configured)
        roots = {}
        for root_id, value in raw.items():
            label = value.get("label", root_id) if isinstance(value, dict) else root_id
            path = Path(value.get("path", "")) if isinstance(value, dict) else Path(value)
            if path.exists():
                roots[str(root_id)] = (str(label), path.resolve())
        return roots
    if os.name == "nt":
        result = {}
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            path = Path(f"{letter}:\\")
            if path.exists():
                result[f"drive_{letter.lower()}"] = (f"{letter}: drive", path.resolve())
        return result
    home = Path.home().resolve()
    return {"home": ("Home", home)}


def _relative(cwd, requested):
    value = str(requested or "").replace("\\", "/").strip()
    parts = [] if value.startswith("/") else [
        part for part in PurePosixPath(cwd or ".").parts if part not in ("", ".")
    ]
    for part in PurePosixPath(value or ".").parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ConsolePolicyError("Path escapes the selected drive.")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _resolve(root, relative):
    _ensure_not_sensitive(relative)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ConsolePolicyError("Path escapes the selected drive.") from exc
    return candidate


def _is_sensitive(relative):
    parts = [
        part.lower()
        for part in PurePosixPath(str(relative).replace("\\", "/")).parts
        if part not in ("", ".")
    ]
    if any(part in SENSITIVE_DIRECTORIES for part in parts):
        return True
    if not parts:
        return False
    filename = parts[-1]
    return (
        filename in SENSITIVE_FILENAMES
        or filename.startswith(".env")
        or any(filename.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)
    )


def _ensure_not_sensitive(relative):
    if _is_sensitive(relative):
        raise ConsolePolicyError("Credential and secret paths are not exposed by the console.")


def _root(root_id):
    roots = _roots()
    if root_id not in roots:
        raise ConsolePolicyError("Unknown local drive.")
    return roots[root_id][1]


def _list(root_id, path=""):
    relative = _relative("", path)
    root = _root(root_id)
    directory = _resolve(root, relative)
    if not directory.is_dir():
        raise ConsolePolicyError("Directory does not exist.")
    entries = []
    for item in sorted(directory.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower()))[:MAX_ENTRIES]:
        item_relative = str(item.relative_to(root))
        if _is_sensitive(item_relative):
            continue
        try:
            stat = item.stat()
            navigable = item.is_dir() and _resolve(root, item_relative).is_dir()
            entries.append({
                "name": item.name,
                "kind": "directory" if item.is_dir() else "file",
                "size": stat.st_size if item.is_file() else None,
                "modified_at": stat.st_mtime,
                "navigable": navigable,
            })
        except (OSError, ConsolePolicyError):
            entries.append({"name": item.name, "kind": "unavailable", "size": None,
                            "modified_at": None, "navigable": False})
    return {"target": "local", "root": root_id, "path": relative, "entries": entries,
            "truncated": len(entries) >= MAX_ENTRIES, "read_only": True}


def _read(root_id, path):
    relative = _relative("", path)
    file_path = _resolve(_root(root_id), relative)
    if not file_path.is_file():
        raise ConsolePolicyError("File does not exist.")
    size = file_path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ConsolePolicyError(f"File exceeds the {MAX_FILE_BYTES}-byte preview limit.")
    raw = file_path.read_bytes()
    if b"\0" in raw:
        raise ConsolePolicyError("Binary files are not previewed.")
    content = raw.decode("utf-8", errors="replace")
    return {"target": "local", "root": root_id, "path": relative, "content": content,
            "bytes": size, "read_only": True}


def _command(root_id, cwd, command):
    clean = str(command or "").strip()
    if not clean or len(clean) > 2_000:
        raise ConsolePolicyError("Command must contain 1-2000 characters.")
    root = _root(root_id)
    relative_cwd = _relative("", cwd)
    working = _resolve(root, relative_cwd)
    if not working.is_dir():
        raise ConsolePolicyError("Working directory does not exist.")
    try:
        tokens = shlex.split(clean, posix=True)
    except ValueError as exc:
        raise ConsolePolicyError(f"Invalid command quoting: {exc}") from exc
    name, args = tokens[0].lower(), tokens[1:]
    if name == "clear":
        return {"target": "local", "root": root_id, "cwd": relative_cwd, "output": "",
                "exit_code": 0, "clear": True, "read_only": True}
    if name == "pwd":
        if args:
            raise ConsolePolicyError("pwd does not accept arguments.")
        return {"target": "local", "root": root_id, "cwd": relative_cwd,
                "output": str(working), "exit_code": 0, "read_only": True}
    if name == "cd":
        if len(args) > 1:
            raise ConsolePolicyError("cd accepts one relative path.")
        new_cwd = _relative(relative_cwd, args[0] if args else "/")
        if not _resolve(root, new_cwd).is_dir():
            raise ConsolePolicyError("Directory does not exist.")
        return {"target": "local", "root": root_id, "cwd": new_cwd, "output": "",
                "exit_code": 0, "read_only": True}
    if name in {"ls", "dir"}:
        if len(args) > 1:
            raise ConsolePolicyError("ls accepts at most one relative path.")
        listing = _list(root_id, _relative(relative_cwd, args[0] if args else "."))
        output = "\n".join(
            f"{'d' if item['kind'] == 'directory' else '-'}  {item['name']}"
            for item in listing["entries"]
        )
        return {"target": "local", "root": root_id, "cwd": relative_cwd,
                "output": output, "exit_code": 0, "read_only": True}
    if name in {"cat", "head", "tail", "type"}:
        if len(args) != 1:
            raise ConsolePolicyError(f"{name} requires one relative file path.")
        preview = _read(root_id, _relative(relative_cwd, args[0]))
        lines = preview["content"].splitlines()
        text = "\n".join(lines[:40] if name == "head" else lines[-40:] if name == "tail" else lines)
        return {"target": "local", "root": root_id, "cwd": relative_cwd,
                "output": text[:MAX_OUTPUT_CHARS], "exit_code": 0, "read_only": True}
    if name == "git":
        if not args or args[0] not in GIT_SUBCOMMANDS:
            raise ConsolePolicyError("Allowed git commands: status, log, diff, branch, show, rev-parse.")
        if any(arg == "-C" or arg.startswith(("--git-dir", "--work-tree", "--exec-path", "-c")) for arg in args[1:]):
            raise ConsolePolicyError("Git path/config overrides are not allowed.")
        git = shutil.which("git")
        if not git:
            raise ConsolePolicyError("git is not installed.")
        subcommand, rest = args[0], args[1:]
        executable = [git, "-c", "core.fsmonitor=false", "-c", "diff.external=", subcommand]
        if subcommand == "diff":
            executable += ["--no-ext-diff", "--no-textconv"]
        executable += rest
    elif name == "rg":
        rg = shutil.which("rg")
        if not rg:
            raise ConsolePolicyError("rg is not installed.")
        flags = [arg for arg in args if arg.startswith("-")]
        if any(flag not in RG_FLAGS for flag in flags):
            raise ConsolePolicyError("Only basic read-only rg flags are allowed.")
        positional = [arg for arg in args if not arg.startswith("-")]
        if not positional and "--files" not in flags:
            raise ConsolePolicyError("rg requires a pattern.")
        if len(positional) > 2:
            raise ConsolePolicyError("rg accepts a pattern and one relative search path.")
        if len(positional) == 2:
            _resolve(root, _relative(relative_cwd, positional[1]))
        exclude_args = [part for pattern in RG_EXCLUDES for part in ("--glob", pattern)]
        executable = [rg, *exclude_args, *args]
    else:
        raise ConsolePolicyError(
            "Read-only commands only: pwd, cd, ls, cat, head, tail, type, rg, git, clear."
        )
    env = {**os.environ, "GIT_PAGER": "cat", "PAGER": "cat", "RIPGREP_CONFIG_PATH": ""}
    try:
        completed = subprocess.run(
            executable, cwd=working, capture_output=True, text=True,
            timeout=30, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ConsolePolicyError("Command timed out after 30 seconds.") from exc
    raw_output = completed.stdout + completed.stderr
    return {"target": "local", "root": root_id, "cwd": relative_cwd,
            "output": raw_output[:MAX_OUTPUT_CHARS], "exit_code": completed.returncode,
            "truncated": len(raw_output) > MAX_OUTPUT_CHARS, "read_only": True}


def register_console_routes(api):
    @api.route("/api/v1/console/roots")
    @require_api_key
    def console_roots():
        return jsonify({
            "target": "local", "read_only": True,
            "roots": [{"id": key, "label": label, "path": str(path)}
                      for key, (label, path) in _roots().items()],
        })

    @api.route("/api/v1/console/files")
    @require_api_key
    def console_files():
        try:
            return jsonify(_list(request.args.get("root", ""), request.args.get("path", "")))
        except (ConsolePolicyError, OSError) as exc:
            return jsonify({"error": str(exc), "status": "policy_denied"}), 403

    @api.route("/api/v1/console/file")
    @require_api_key
    def console_file():
        try:
            return jsonify(_read(request.args.get("root", ""), request.args.get("path", "")))
        except (ConsolePolicyError, OSError) as exc:
            return jsonify({"error": str(exc), "status": "policy_denied"}), 403

    @api.route("/api/v1/console/command", methods=["POST"])
    @require_api_key
    def console_command():
        started = time.monotonic()
        payload = request.get_json(silent=True) or {}
        try:
            result = _command(payload.get("root", ""), payload.get("cwd", ""), payload.get("command", ""))
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            return jsonify(result)
        except (ConsolePolicyError, OSError) as exc:
            return jsonify({
                "error": str(exc), "status": "policy_denied",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }), 403
