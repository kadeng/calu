"""The `$EDITOR` flow of `calu checker edit` and `calu rules edit`, and the rule list format."""

import stat
from pathlib import Path

import pytest

from calu import admin
from calu.cli import CliError, edit_in_editor, editor_command
from calu.setup import parse_rule_file


def fake_editor(tmp_path: Path, script: str) -> str:
    """Write an executable shell script that edits the file it is given, and return its path."""
    path = tmp_path / "fake-editor.sh"
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_the_editor_comes_from_visual_then_editor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VISUAL", "code -w")
    monkeypatch.setenv("EDITOR", "nano")
    assert editor_command() == ["code", "-w"]
    monkeypatch.delenv("VISUAL")
    assert editor_command() == ["nano"]
    monkeypatch.setenv("EDITOR", "   ")  # set but empty: fall through to the candidates
    monkeypatch.setattr(
        "calu.cli.shutil.which", lambda name: "/usr/bin/vi" if name == "vi" else None
    )
    assert editor_command() == ["vi"]
    monkeypatch.setattr("calu.cli.shutil.which", lambda name: None)
    with pytest.raises(CliError, match="no editor found"):
        editor_command()


def test_the_text_goes_through_the_editor_and_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("EDITOR", fake_editor(tmp_path, 'printf "edited\\n" > "$1"'))
    after, path = edit_in_editor("before\n", "command-instructions.md")
    assert after == "edited\n"
    assert path.name == "command-instructions.md" and path.is_file()
    assert "editing" in capsys.readouterr().out
    # The file keeps the edit, so nothing is lost if storing it fails.
    assert path.read_text(encoding="utf-8") == "edited\n"


def test_an_editor_that_fails_stores_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDITOR", fake_editor(tmp_path, "exit 3"))
    with pytest.raises(CliError, match="exited with 3"):
        edit_in_editor("before\n", "x.md")
    monkeypatch.setenv("EDITOR", str(tmp_path / "does-not-exist"))
    with pytest.raises(CliError, match="cannot start the editor"):
        edit_in_editor("before\n", "x.md")


def test_a_rule_list_is_rendered_so_that_parsing_it_back_keeps_everything():
    rows = [
        admin.RuleRow(
            id=1,
            scope="global",
            list="command-deny",
            pattern="^sudo( |$)",
            comment="no sudo",
            position=0,
            enabled=True,
        ),
        admin.RuleRow(
            id=2,
            scope="global",
            list="command-deny",
            pattern="^rm -rf /",
            comment="",
            position=1,
            enabled=True,
        ),
    ]
    text = admin.render_rule_list(rows, "command-deny", "global")
    assert "# Rules of the list command-deny (global scope)" in text
    parsed = parse_rule_file("command-deny", text)
    assert [(r.pattern, r.comment, r.position) for r in parsed] == [
        ("^sudo( |$)", "no sudo", 0),
        ("^rm -rf /", "", 1),
    ]
    # The header is dropped, so it never becomes a rule's comment.
    assert all("Rules of the list" not in r.comment for r in parsed)


def test_an_empty_rule_file_means_no_rules():
    empty = admin.render_rule_list([], "command-deny", "demo")
    assert parse_rule_file("command-deny", empty) == []
