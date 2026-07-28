from __future__ import annotations

import json
from pathlib import Path

import pytest

from little_brother.api import console


def configure_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "drive"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "readme.txt").write_text("local bridge\n", encoding="utf-8")
    monkeypatch.setenv(
        "LB_CONSOLE_ROOTS",
        json.dumps({"drive_test": {"label": "Test drive", "path": str(root)}}),
    )
    return root


def test_local_console_lists_reads_and_changes_directory(tmp_path: Path, monkeypatch):
    root = configure_root(tmp_path, monkeypatch)
    (root / ".env.local").write_text("SECRET=value", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_ed25519").write_text("private", encoding="utf-8")

    roots = console._roots()
    listing = console._list("drive_test", "")
    preview = console._read("drive_test", "projects/readme.txt")
    changed = console._command("drive_test", "", "cd projects")

    assert roots["drive_test"][0] == "Test drive"
    assert listing["entries"][0]["name"] == "projects"
    assert {item["name"] for item in listing["entries"]}.isdisjoint({".env.local", ".ssh"})
    assert preview["content"].splitlines() == ["local bridge"]
    assert changed["cwd"] == "projects"
    with pytest.raises(console.ConsolePolicyError, match="Credential"):
        console._read("drive_test", ".env.local")


def test_local_console_denies_escape_and_mutation(tmp_path: Path, monkeypatch):
    configure_root(tmp_path, monkeypatch)

    with pytest.raises(console.ConsolePolicyError, match="escapes"):
        console._read("drive_test", "../secret.txt")
    with pytest.raises(console.ConsolePolicyError, match="Read-only commands only"):
        console._command("drive_test", "", "powershell Get-ChildItem")
    with pytest.raises(console.ConsolePolicyError, match="overrides"):
        console._command("drive_test", "", "git status -C C:/")
