"""Git snapshots of what a command wrote: write set, patch, revert (D31)."""

import subprocess
from pathlib import Path

import pytest

from calu.changes import SnapshotStore

pytestmark = pytest.mark.anyio


def store(tmp_path: Path, **kwargs: object) -> SnapshotStore:
    """A snapshot store for a project in `tmp_path/project`, with its git dir outside it."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return SnapshotStore(workspace=project, root=tmp_path / "snapshots", **kwargs)  # type: ignore[arg-type]


async def test_a_directory_that_is_no_git_repository_can_be_snapshotted(tmp_path: Path):
    snapshots = store(tmp_path)
    project = snapshots.workspace
    (project / "src").mkdir()
    (project / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (project / "src" / "b.py").write_text("keep\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()

    # A command edits, adds and deletes.
    (project / "src" / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (project / "src" / "c.py").write_text("new\n", encoding="utf-8")
    (project / "src" / "b.py").unlink()

    after = await snapshots.take()
    change = await snapshots.changes(before, after)
    assert {(c.status, c.path) for c in change.changes} == {
        ("changed", "src/a.py"),
        ("added", "src/c.py"),
        ("deleted", "src/b.py"),
    }
    assert "-    return 1" in change.patch and "+    return 2" in change.patch
    assert not change.cut
    assert "3 path(s)" in change.summary()

    # The revert undoes all three kinds, and the later state stays readable in the store.
    assert await snapshots.revert(before, after) is True
    assert (project / "src" / "a.py").read_text() == "def f():\n    return 1\n"
    assert (project / "src" / "b.py").read_text() == "keep\n"
    assert not (project / "src" / "c.py").exists()


async def test_nothing_changed_is_an_empty_write_set(tmp_path: Path):
    snapshots = store(tmp_path)
    (snapshots.workspace / "a.txt").write_text("x\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()
    after = await snapshots.take()
    change = await snapshots.changes(before, after)
    assert change.changes == () and change.summary() == "no files changed"
    # A rewrite with the same content is invisible, which is what we want.
    (snapshots.workspace / "a.txt").write_text("x\n", encoding="utf-8")
    assert (await snapshots.changes(before, await snapshots.take())).changes == ()


async def test_ignored_files_are_skipped_unless_they_are_named(tmp_path: Path):
    snapshots = store(tmp_path, always_capture=[".env"])
    project = snapshots.workspace
    (project / ".gitignore").write_text(".env\nbuild/\n", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "out.o").write_text("junk\n", encoding="utf-8")
    (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()
    (project / ".env").write_text("SECRET=1\nLEAK=2\n", encoding="utf-8")
    (project / "build" / "out.o").write_text("other junk\n", encoding="utf-8")
    change = await snapshots.changes(before, await snapshots.take())
    # The ignored build output is not hashed; the named secret file is.
    assert change.paths == (".env",)


async def test_a_big_and_a_binary_file_are_reported_but_not_shown(tmp_path: Path):
    snapshots = store(tmp_path, max_file_bytes=100)
    project = snapshots.workspace
    (project / "small.txt").write_text("a\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()
    (project / "small.txt").write_text("b\n", encoding="utf-8")
    (project / "big.txt").write_text("x" * 500, encoding="utf-8")
    (project / "data.bin").write_bytes(b"\x00\x01\x02" * 10)
    change = await snapshots.changes(before, await snapshots.take())
    by_path = {c.path: c for c in change.changes}
    assert set(by_path) == {"small.txt", "big.txt", "data.bin"}
    assert by_path["data.bin"].binary and by_path["big.txt"].size == 500
    assert "binary" in by_path["data.bin"].line()
    # Only the small text file is in the patch, and the set says that something was left out.
    assert "small.txt" in change.patch
    assert "big.txt" not in change.patch and "data.bin" not in change.patch
    assert change.cut


async def test_the_write_set_names_the_paths_a_summary_left_out(tmp_path: Path):
    snapshots = store(tmp_path)
    project = snapshots.workspace
    (project / "README.md").write_text("# p\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()
    (project / "README.md").write_text("# project\n", encoding="utf-8")
    (project / "secrets.txt").write_text("oops\n", encoding="utf-8")
    change = await snapshots.changes(before, await snapshots.take())
    assert change.undeclared("Fixes the heading in README.md.") == ("secrets.txt",)
    assert change.undeclared("Rewrites readme.md and secrets.txt.") == ()
    assert set(change.undeclared("Runs the formatter.")) == {"README.md", "secrets.txt"}


async def test_a_revert_that_does_not_apply_leaves_the_tree_alone(tmp_path: Path):
    snapshots = store(tmp_path)
    project = snapshots.workspace
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    await snapshots.prepare()
    before = await snapshots.take()
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    after = await snapshots.take()
    # Something else changed the file in the meantime, so the reverse patch does not apply.
    (project / "a.txt").write_text("three\n", encoding="utf-8")
    assert await snapshots.revert(before, after) is False
    assert (project / "a.txt").read_text() == "three\n"


async def test_the_projects_own_repository_is_untouched(tmp_path: Path):
    snapshots = store(tmp_path)
    project = snapshots.workspace
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=project, check=True)
    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, check=True
    ).stdout
    objects_before = len(list((project / ".git" / "objects").rglob("*")))

    await snapshots.prepare()
    before = await snapshots.take()
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    change = await snapshots.changes(before, await snapshots.take())

    assert change.paths == ("a.txt",)
    # The project's index and its object store are as they were; the snapshots live elsewhere.
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True, check=True
    ).stdout
    assert status_after == status_before.replace("A  a.txt", "AM a.txt")
    assert len(list((project / ".git" / "objects").rglob("*"))) == objects_before
    assert (tmp_path / "snapshots" / "HEAD").exists()


async def test_taking_a_snapshot_before_prepare_is_an_error(tmp_path: Path):
    snapshots = store(tmp_path)
    with pytest.raises(Exception, match="prepare"):
        await snapshots.take()
