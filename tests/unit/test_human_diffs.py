"""The user's own edits in the IDE, recorded as diffs (agent/records/PLAN.md D253).

The module's parts (diff, file layout, `.gitignore`, cleanup), the session's request
`human_diffs.add` (setting, git-tracked files only, calu's own output left out) and the
guards (Claude's writes into the directory, write sets).
"""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from calu import human_diffs
from calu.checkers.model import Step
from calu.config import Config, HumanDiffsConfig, TracesConfig
from calu.protocol.client import RequestFailed
from calu.protocol.messages import REQUESTS, HumanDiffAddParams
from calu.protocol.schema import protocol_schema
from calu.web.settings import SECTION_PANES
from tests.unit.helpers import default_rules
from tests.unit.test_protocol_server import Events, client, served

WHEN = datetime(2026, 11, 20, 14, 3, 12, tzinfo=UTC)


def git(root: Path, *arguments: str) -> None:
    """Run git in a directory with a fixed identity.

    Args:
        root: The directory.
        *arguments: git's arguments.
    """
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


def repository(root: Path, files: dict[str, str]) -> Path:
    """Create a git repository with the files committed.

    Args:
        root: The directory.
        files: Relative path -> content.

    Returns:
        `root`.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "start")
    return root


def saved(path: str, before: str, after: str) -> human_diffs.Saved:
    """Build a save of user kai at `WHEN`.

    Args:
        path: The file.
        before: The text before.
        after: The text after.

    Returns:
        The save.
    """
    return human_diffs.Saved(path, before, after, WHEN, "kai", "s-1", "vscode")


# ------------------------------------------------------------------------------- the module


def test_the_diff_is_unified_with_prefixes_and_marks_a_missing_last_newline():
    diff = human_diffs.unified("doc/a.md", "one\ntwo\n", "one\nTWO")
    assert diff.splitlines() == [
        "--- a/doc/a.md",
        "+++ b/doc/a.md",
        "@@ -1,2 +1,2 @@",
        " one",
        "-two",
        "+TWO",
        "\\ No newline at end of file",
    ]
    assert human_diffs.unified("a", "same\n", "same\n") == ""


def test_a_save_is_appended_to_the_file_of_its_path_user_and_day(tmp_path: Path):
    first = human_diffs.record(tmp_path, "agent/human-diffs", saved("src/a.py", "x\n", "y\n"))
    second = human_diffs.record(tmp_path, "agent/human-diffs", saved("src/a.py", "y\n", "z\n"))
    assert first == second == tmp_path / "agent/human-diffs/2026-11-20/kai/src__a.py.tmp.diff"
    assert first is not None
    text = first.read_text()
    assert (
        text.count("# src/a.py: saved 2026-11-20 14:03:12 UTC in vscode, calu session s-1\n") == 2
    )
    assert "-x\n+y\n" in text and "-y\n+z\n" in text
    assert human_diffs.record(tmp_path, "agent/human-diffs", saved("b", "same", "same")) is None


def test_the_blocks_apply_with_git(tmp_path: Path):
    project = repository(tmp_path / "p", {"a.txt": "one\ntwo\n"})
    file = human_diffs.record(project, "d", saved("a.txt", "one\ntwo\n", "one\n2\n"))
    assert file is not None
    git(project, "apply", "--check", str(file))


def test_a_created_directory_gets_a_gitignore_that_keeps_renamed_files(tmp_path: Path):
    project = repository(tmp_path / "p", {"a.txt": "a\n"})
    file = human_diffs.record(project, "agent/human-diffs", saved("a.txt", "a\n", "b\n"))
    assert file is not None
    ignore = (project / "agent/human-diffs/.gitignore").read_text()
    assert "*.tmp.diff" in ignore.splitlines()
    kept = file.with_name("a.txt.diff")
    file.rename(kept)
    human_diffs.record(project, "agent/human-diffs", saved("a.txt", "b\n", "c\n"))
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.splitlines() == ["?? agent/human-diffs/2026-11-20/kai/a.txt.diff"]


def test_an_existing_directory_is_left_without_a_gitignore(tmp_path: Path):
    (tmp_path / "d").mkdir()
    human_diffs.record(tmp_path, "d", saved("a", "1\n", "2\n"))
    assert not (tmp_path / "d/.gitignore").exists()


def test_a_directory_that_leads_out_of_the_project_is_refused(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "p"
    project.mkdir()
    (project / "agent").symlink_to(outside)
    with pytest.raises(human_diffs.HumanDiffError):
        human_diffs.record(project, "agent/human-diffs", saved("a", "1\n", "2\n"))
    assert not list(outside.iterdir())


def test_only_tracked_files_count(tmp_path: Path):
    project = repository(tmp_path / "p", {"a.txt": "a\n", ".gitignore": "ignored.txt\n"})
    (project / "new.txt").write_text("n\n")
    (project / "ignored.txt").write_text("i\n")
    assert human_diffs.is_tracked(project / "a.txt")
    assert not human_diffs.is_tracked(project / "new.txt")
    assert not human_diffs.is_tracked(project / "ignored.txt")
    assert not human_diffs.is_tracked(tmp_path / "nowhere" / "x")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f").write_text("f")
    assert not human_diffs.is_tracked(plain / "f")


# ------------------------------------------------------------------------------ the cleanup


def aged(path: Path, text: str, days: float) -> Path:
    """Write a file whose last change lies `days` in the past.

    Args:
        path: The file.
        text: Its content.
        days: Its age.

    Returns:
        `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    then = time.time() - days * 86400
    os.utime(path, (then, then))
    return path


def test_the_cleanup_deletes_old_temporary_diffs_only(tmp_path: Path):
    base = tmp_path / "d"
    old = aged(base / "2026-10-01/kai/a.tmp.diff", "x", 20)
    young = aged(base / "2026-11-19/kai/b.tmp.diff", "x", 1)
    kept = aged(base / "2026-10-01/ann/c.diff", "x", 300)
    other = aged(base / "2026-10-01/ann/notes.md", "x", 300)
    removed = human_diffs.cleanup(tmp_path, "d", 14)
    assert removed == ["d/2026-10-01/kai/a.tmp.diff"]
    assert not old.exists() and young.exists() and kept.exists() and other.exists()
    assert not (base / "2026-10-01/kai").exists(), "the emptied folder is removed"
    assert base.exists()
    assert human_diffs.cleanup(tmp_path, "d", 0) == []
    assert young.exists()


def test_the_cleanup_follows_no_link(tmp_path: Path):
    outside = tmp_path / "outside"
    target = aged(outside / "victim.tmp.diff", "x", 100)
    folder = aged(outside / "inner/deep.tmp.diff", "x", 100)
    project = tmp_path / "p"
    base = project / "d"
    base.mkdir(parents=True)
    (base / "link.tmp.diff").symlink_to(target)
    (base / "dir.tmp.diff").symlink_to(outside / "inner")
    (base / "sub").symlink_to(outside)
    os.utime(base / "link.tmp.diff", (1, 1), follow_symlinks=False)
    assert human_diffs.cleanup(project, "d", 1) == []
    assert target.exists() and folder.exists()
    assert (base / "link.tmp.diff").is_symlink()
    # A directory setting that is itself a link is not walked.
    linked = tmp_path / "q"
    linked.mkdir()
    (linked / "d").symlink_to(outside)
    assert human_diffs.cleanup(linked, "d", 1) == []
    assert target.exists()


# ------------------------------------------------------------------------------ the setting


def test_the_setting_is_off_by_default_and_keeps_two_weeks():
    cfg = Config().human_diffs
    assert (cfg.enabled, cfg.directory, cfg.keep_days) == (False, "agent/human-diffs", 14)
    assert HumanDiffsConfig(directory="./x/y/").directory == "x/y"
    for bad in ("/abs", "../up", "", "~/x"):
        with pytest.raises(ValidationError):
            HumanDiffsConfig(directory=bad)
    with pytest.raises(ValidationError):
        HumanDiffsConfig(keep_days=-1)
    assert "human_diffs" in SECTION_PANES


# ------------------------------------------------------------------------------ the request


@pytest.mark.anyio
async def test_the_session_records_a_save_of_a_tracked_file(tmp_path: Path):
    assert REQUESTS["human_diffs.add"] is HumanDiffAddParams
    schema = protocol_schema()
    assert schema["results"]["human_diffs.add"] == {"$ref": "#/$defs/HumanDiffAddResult"}
    project = repository(tmp_path / "p", {"doc/a.md": "Old words.\n"})
    (project / "untracked.md").write_text("u\n")
    config = Config(human_diffs=HumanDiffsConfig(enabled=True), traces=TracesConfig(enabled=True))
    async with served(project, config=config) as run:
        async with client(run, Events(), name="vscode") as editor:
            await editor.request("session.start", {})
            answer = await editor.request(
                "human_diffs.add",
                {"path": "doc/a.md", "before": "Old words.\n", "after": "New words.\n"},
            )
            assert answer["enabled"] and answer["recorded"]
            assert answer["file"].startswith("agent/human-diffs/")
            assert answer["file"].endswith("/doc__a.md.tmp.diff")
            text = (project / answer["file"]).read_text()
            assert "in vscode, calu session" in text and "+New words.\n" in text

            async def reason(path: str, before: str = "a", after: str = "b") -> str:
                result = await editor.request(
                    "human_diffs.add", {"path": path, "before": before, "after": after}
                )
                assert not result["recorded"]
                return result["reason"]

            assert await reason("untracked.md") == "not tracked by git"
            assert await reason("doc/a.md", "same", "same") == "unchanged"
            assert await reason("doc/a.md", "a\x00", "b") == "binary"
            assert await reason("agent/human-diffs/x.tmp.diff") == "calu's own output"
            assert await reason("agent/traces/s.md") == "calu's own output"
            for bad in ("../x", "/etc/passwd", "a/../../b"):
                with pytest.raises(RequestFailed):
                    await editor.request(
                        "human_diffs.add", {"path": bad, "before": "", "after": "x"}
                    )
            # The directory is calu's own output: no command's write set holds it.
            assert set(run.session.write_sets.excluded) == {"agent/traces", "agent/human-diffs"}
            assert "agent/human-diffs" in run.session.honesty.excluded


@pytest.mark.anyio
async def test_the_session_records_nothing_while_the_feature_is_off(tmp_path: Path):
    project = repository(tmp_path / "p", {"a.md": "a\n"})
    async with served(project) as run:
        async with client(run, Events()) as editor:
            answer = await editor.request(
                "human_diffs.add", {"path": "a.md", "before": "a\n", "after": "b\n"}
            )
            assert answer == {"enabled": False, "recorded": False, "file": None, "reason": "off"}
            assert run.session.write_sets.excluded == ()
    assert not (project / "agent").exists()


# ------------------------------------------------------------------------------ the guards


def test_claude_writes_into_the_directory_meet_a_built_in_rule(tmp_path: Path):
    rules = default_rules(tmp_path)

    def asks(tool: str, tool_input: dict[str, str]) -> str:
        """Return the rules' reason for a tool call.

        Args:
            tool: The tool name.
            tool_input: Its input.
        """
        step = Step(tool_name=tool, tool_input=tool_input, cwd=str(tmp_path))
        return rules.evaluate(step).reason

    inside = {"file_path": "agent/human-diffs/2026-11-20/kai/a.tmp.diff", "content": "x"}
    command = {"command": "rm -rf agent/human-diffs"}
    marker = "calu's record of the user's own edits"
    assert marker not in asks("Write", inside)
    rules.human_diffs_directory = tmp_path / "agent/human-diffs"
    assert marker in asks("Write", inside)
    assert marker in asks("Bash", command)
    assert marker not in asks("Write", {"file_path": "agent/notes.md", "content": "x"})
    assert marker not in asks("Read", {"file_path": inside["file_path"]})
