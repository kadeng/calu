"""File access of the editor's file synchronisation, on real directories (`calu.ide.filestore`).

Every refusal the confinement promises has a test here: links, special files, sizes, the
comparison with the base, and what is left behind after a failure.
"""

import hashlib
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from calu.acp.peer import RpcError
from calu.ide import files
from calu.ide.files import HashCache, clean_path
from calu.ide.filestore import MISSING, Tree, check_base


def details(error: object) -> dict[str, Any]:
    """Return the `data` object of an error, which these tests expect to be there.

    Args:
        error: An `RpcError` or the editor's `Failed`.
    """
    found = getattr(error, "data", None)
    assert isinstance(found, dict), found
    return found


def sha(data: bytes) -> str:
    """Hash bytes as the protocol does.

    Args:
        data: The content.
    """
    return hashlib.sha256(data).hexdigest()


def code_of(error: pytest.ExceptionInfo[RpcError]) -> int:
    """Return the JSON-RPC code of a caught error.

    Args:
        error: What `pytest.raises` caught.
    """
    return error.value.code


def leftovers(root: Path) -> list[str]:
    """List the temporary upload files below a directory.

    Args:
        root: The directory.
    """
    return sorted(str(p.relative_to(root)) for p in root.rglob(files.UPLOAD_PREFIX + "*"))


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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty project directory beside a directory that is outside of it."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret")
    project = tmp_path / "project"
    project.mkdir()
    return project


@pytest.fixture
def tree(root: Path) -> Iterator[Tree]:
    """The file access of the project directory."""
    opened = Tree(root)
    yield opened
    opened.close()


# ------------------------------------------------------------------------- confinement


def test_a_link_to_a_directory_outside_is_never_read_or_written_through(root: Path, tree: Tree):
    (root / "out").symlink_to(root.parent / "outside", target_is_directory=True)
    for action in (
        lambda: tree.read("out/secret.txt", 0, 100),
        lambda: tree.state("out/secret.txt"),
        lambda: tree.write("out/new.txt", b"x", None, None),
        lambda: tree.write("out/secret.txt", b"x", sha(b"secret"), None),
        lambda: tree.delete("out/secret.txt", sha(b"secret")),
        lambda: tree.rename("out/secret.txt", "mine.txt", sha(b"secret")),
        lambda: tree.temp("out/new.txt"),
    ):
        with pytest.raises(RpcError) as refused:
            action()
        assert code_of(refused) == files.PATH_REFUSED
        assert refused.value.data == {"reason": "outside"}
    assert (root.parent / "outside" / "secret.txt").read_text() == "secret"
    assert sorted(p.name for p in (root.parent / "outside").iterdir()) == ["secret.txt"]


def test_a_rename_into_a_linked_directory_is_refused(root: Path, tree: Tree):
    (root / "a.txt").write_bytes(b"a")
    (root / "out").symlink_to(root.parent / "outside", target_is_directory=True)
    with pytest.raises(RpcError) as refused:
        tree.rename("a.txt", "out/a.txt", sha(b"a"))
    assert code_of(refused) == files.PATH_REFUSED
    assert (root / "a.txt").exists() and not (root.parent / "outside" / "a.txt").exists()


def test_a_link_to_a_directory_inside_is_not_followed_either(root: Path, tree: Tree):
    (root / "real").mkdir()
    (root / "real" / "a.txt").write_bytes(b"a")
    (root / "alias").symlink_to("real", target_is_directory=True)
    for action in (
        lambda: tree.read("alias/a.txt", 0, 10),
        lambda: tree.write("alias/a.txt", b"b", sha(b"a"), None),
        lambda: tree.write("alias/new.txt", b"b", None, None),
    ):
        with pytest.raises(RpcError) as refused:
            action()
        assert code_of(refused) == files.NOT_REGULAR
        assert refused.value.data == {"kind": "symlink"}
    assert (root / "real" / "a.txt").read_bytes() == b"a"
    assert sorted(p.name for p in (root / "real").iterdir()) == ["a.txt"]


def test_a_link_as_the_file_itself_is_reported_and_never_opened(root: Path, tree: Tree):
    (root / "inside.txt").write_bytes(b"inside")
    (root / "to-inside").symlink_to("inside.txt")
    (root / "to-outside").symlink_to(root.parent / "outside" / "secret.txt")
    (root / "dangling").symlink_to("nowhere")
    for name in ("to-inside", "to-outside", "dangling"):
        assert tree.state(name).kind == "symlink" and tree.state(name).sha256 is None
        with pytest.raises(RpcError) as unread:
            tree.read(name, 0, 10)
        assert (code_of(unread), unread.value.data) == (files.NOT_REGULAR, {"kind": "symlink"})
        for base in (None, sha(b"inside"), sha(b"secret")):
            with pytest.raises(RpcError) as unwritten:
                tree.write(name, b"new", base, None)
            assert code_of(unwritten) == files.NOT_REGULAR
        with pytest.raises(RpcError) as kept:
            tree.delete(name, sha(b"secret"))
        assert code_of(kept) == files.NOT_REGULAR
        assert (root / name).is_symlink()
    assert (root.parent / "outside" / "secret.txt").read_text() == "secret"
    assert (root / "inside.txt").read_bytes() == b"inside"
    assert leftovers(root) == []


def test_a_directory_swapped_for_a_link_after_it_was_resolved_does_not_redirect(
    root: Path, tree: Tree
):
    (root / "d").mkdir()
    temp = tree.temp("d/a.txt")
    temp.put(b"content")
    # The directory is replaced by a link to the outside while the upload is open.
    (root / "d").rename(root / "moved")
    (root / "d").symlink_to(root.parent / "outside", target_is_directory=True)
    tree.commit(temp, None, None)
    assert (root / "moved" / "a.txt").read_bytes() == b"content"
    assert sorted(p.name for p in (root.parent / "outside").iterdir()) == ["secret.txt"]


def test_special_files_are_neither_read_nor_replaced(root: Path, tree: Tree):
    os.mkfifo(root / "pipe")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(root / "sock"))
    (root / "dir").mkdir()
    try:
        for name, kind in (("pipe", "fifo"), ("sock", "socket"), ("dir", "directory")):
            assert tree.state(name).kind == kind
            with pytest.raises(RpcError) as unread:
                tree.read(name, 0, 10)  # must not block on the FIFO
            assert (code_of(unread), unread.value.data) == (files.NOT_REGULAR, {"kind": kind})
            with pytest.raises(RpcError) as unwritten:
                tree.write(name, b"x", None, None)
            assert code_of(unwritten) == files.NOT_REGULAR
            with pytest.raises(RpcError) as moved:
                tree.rename(name, "elsewhere", sha(b""))
            assert code_of(moved) == files.NOT_REGULAR
        (root / "a.txt").write_bytes(b"a")
        with pytest.raises(RpcError) as under_a_file:
            tree.write("a.txt/b.txt", b"x", None, None)
        assert (code_of(under_a_file), under_a_file.value.data) == (
            files.NOT_REGULAR,
            {"kind": "file"},
        )
    finally:
        listener.close()
    assert leftovers(root) == []


def test_the_bridge_adds_no_privilege(root: Path, tree: Tree):
    """A directory the user cannot write stays unwritable: the error is the system's."""
    if os.geteuid() == 0:
        pytest.skip("root may write everywhere")
    locked = root / "locked"
    locked.mkdir()
    (locked / "a.txt").write_bytes(b"a")
    locked.chmod(0o555)
    try:
        for action in (
            lambda: tree.write("locked/new.txt", b"x", None, None),
            lambda: tree.write("locked/a.txt", b"x", sha(b"a"), None),
            lambda: tree.delete("locked/a.txt", sha(b"a")),
        ):
            with pytest.raises(RpcError) as refused:
                action()
            assert code_of(refused) == files.IO_ERROR
            assert details(refused.value)["errno"] in (13, 1, 30)
        assert (locked / "a.txt").read_bytes() == b"a"
    finally:
        locked.chmod(0o755)


# ------------------------------------------------------------------------------- reads


def test_a_read_returns_a_range_with_the_hash_of_the_whole_file(root: Path, tree: Tree):
    content = bytes(range(256)) * 10
    (root / "data.bin").write_bytes(content)
    (root / "data.bin").chmod(0o640)
    whole = tree.read("data.bin", 0, 10_000)
    assert whole == {
        "content": content,
        "sha256": sha(content),
        "size": 2560,
        "mode": 0o640,
        "eof": True,
    }
    piece = tree.read("data.bin", 100, 50)
    assert piece["content"] == content[100:150] and piece["eof"] is False
    assert piece["sha256"] == sha(content) and piece["size"] == 2560
    tail = tree.read("data.bin", 2500, 500)
    assert tail["content"] == content[2500:] and tail["eof"] is True
    beyond = tree.read("data.bin", 5000, 10)
    assert beyond["content"] == b"" and beyond["eof"] is True
    with pytest.raises(RpcError) as missing:
        tree.read("nothing.txt", 0, 10)
    assert code_of(missing) == files.NOT_FOUND
    with pytest.raises(RpcError) as missing_directory:
        tree.read("no/such/dir.txt", 0, 10)
    assert code_of(missing_directory) == files.NOT_FOUND


def test_a_file_above_the_limit_is_refused_without_being_read(root: Path, tree: Tree):
    """A sparse file of 33 MiB: the size is checked from the status, nothing is allocated."""
    with open(root / "huge.bin", "wb") as sparse:
        sparse.truncate(33 * 1024 * 1024)
    state = tree.state("huge.bin")
    assert state.kind == "file" and state.sha256 is None and state.size == 33 * 1024 * 1024
    with pytest.raises(RpcError) as unread:
        tree.read("huge.bin", 0, 10)
    assert code_of(unread) == files.TOO_LARGE
    assert unread.value.data == {"size": 33 * 1024 * 1024, "limit": files.MAX_FILE_BYTES}
    with pytest.raises(RpcError) as unwritten:
        tree.write("huge.bin", b"small", sha(b"whatever"), None)
    assert code_of(unwritten) == files.TOO_LARGE
    assert (root / "huge.bin").stat().st_size == 33 * 1024 * 1024
    _files, skipped, _signatures = tree.describe(["huge.bin"])
    assert skipped == [{"path": "huge.bin", "reason": "too-large"}]


def test_a_large_file_within_the_limit_is_hashed_in_blocks(root: Path, tree: Tree):
    with open(root / "big.bin", "wb") as sparse:
        sparse.truncate(5 * 1024 * 1024 + 7)
    assert tree.state("big.bin").sha256 == sha(bytes(5 * 1024 * 1024 + 7))


# ------------------------------------------------------------------------------ writes


def test_a_write_needs_the_matching_base_and_replaces_in_one_step(root: Path, tree: Tree):
    created = tree.write("src/deep/new.py", b"one", None, None)
    assert (root / "src" / "deep" / "new.py").read_bytes() == b"one"
    assert (created.sha256, created.size, created.mode) == (sha(b"one"), 3, 0o644)
    (root / "src" / "deep" / "new.py").chmod(0o750)
    before = (root / "src" / "deep" / "new.py").stat().st_ino
    replaced = tree.write("src/deep/new.py", b"two!", sha(b"one"), None)
    assert (replaced.sha256, replaced.mode) == (sha(b"two!"), 0o750), "the mode is kept"
    assert (root / "src" / "deep" / "new.py").stat().st_ino != before, "a new file was renamed in"
    explicit = tree.write("src/deep/new.py", b"three", sha(b"two!"), 0o600)
    assert explicit.mode == 0o600
    assert leftovers(root) == []


@pytest.mark.parametrize(
    ("existing", "base", "found"),
    [
        (b"current", sha(b"stale"), sha(b"current")),
        (b"current", None, sha(b"current")),
        (None, sha(b"stale"), None),
    ],
)
def test_a_mismatching_base_refuses_with_what_is_there_and_changes_nothing(
    root: Path, tree: Tree, existing: bytes | None, base: str | None, found: str | None
):
    target = root / "sub" / "a.txt"
    if existing is not None:
        target.parent.mkdir()
        target.write_bytes(existing)
    with pytest.raises(RpcError) as refused:
        tree.write("sub/a.txt", b"mine", base, None)
    assert code_of(refused) == files.BASE_MISMATCH
    assert details(refused.value)["sha256"] == found
    assert details(refused.value)["size"] == (len(existing) if existing is not None else None)
    if existing is None:
        assert not (root / "sub").exists(), "no directory is created for a refused write"
    else:
        assert target.read_bytes() == existing, "the target was never truncated"
    assert leftovers(root) == []


def test_the_base_is_compared_again_at_the_commit_and_a_failure_cleans_up(root: Path, tree: Tree):
    temp = tree.temp("new/dir/a.txt")
    temp.put(b"upload")
    assert leftovers(root) == [f"new/dir/{temp.name}"]
    (root / "new" / "dir" / "a.txt").write_bytes(b"somebody else")
    with pytest.raises(RpcError) as refused:
        tree.commit(temp, None, None)
    assert code_of(refused) == files.BASE_MISMATCH
    assert (root / "new" / "dir" / "a.txt").read_bytes() == b"somebody else"
    assert leftovers(root) == []


def test_a_discarded_upload_removes_its_file_and_the_directories_it_made(root: Path, tree: Tree):
    (root / "kept").mkdir()
    temp = tree.temp("kept/made/deeper/a.txt")
    temp.put(b"partial")
    temp.discard()
    temp.discard()  # a second time changes nothing
    assert leftovers(root) == []
    assert (root / "kept").is_dir() and not (root / "kept" / "made").exists()


def test_check_base_distinguishes_every_case():
    check_base("a", MISSING, None, 10)
    with pytest.raises(RpcError) as mismatch:
        check_base("a", MISSING, sha(b"x"), 10)
    assert mismatch.value.data == {"sha256": None, "size": None, "mode": None}


def test_a_delete_needs_the_base_and_prunes_empty_directories(root: Path, tree: Tree):
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "x.txt").write_bytes(b"x")
    (root / "a" / "keep.txt").write_bytes(b"k")
    with pytest.raises(RpcError) as stale:
        tree.delete("a/b/x.txt", sha(b"other"))
    assert code_of(stale) == files.BASE_MISMATCH and details(stale.value)["sha256"] == sha(b"x")
    assert (root / "a" / "b" / "x.txt").exists()
    tree.delete("a/b/x.txt", sha(b"x"))
    assert not (root / "a" / "b").exists() and (root / "a" / "keep.txt").exists()
    with pytest.raises(RpcError) as gone:
        tree.delete("a/b/x.txt", sha(b"x"))
    assert code_of(gone) == files.BASE_MISMATCH and details(gone.value)["sha256"] is None
    tree.delete("a/keep.txt", sha(b"k"))
    assert not (root / "a").exists() and root.is_dir(), "the root itself stays"


def test_a_rename_never_replaces_an_existing_target(root: Path, tree: Tree):
    (root / "old").mkdir()
    (root / "old" / "a.txt").write_bytes(b"a")
    (root / "taken.txt").write_bytes(b"t")
    with pytest.raises(RpcError) as exists:
        tree.rename("old/a.txt", "taken.txt", sha(b"a"))
    assert code_of(exists) == files.TARGET_EXISTS and exists.value.data == {"sha256": sha(b"t")}
    with pytest.raises(RpcError) as stale:
        tree.rename("old/a.txt", "new/a.txt", sha(b"zzz"))
    assert code_of(stale) == files.BASE_MISMATCH and not (root / "new").exists()
    with pytest.raises(RpcError) as missing:
        tree.rename("old/none.txt", "new/a.txt", sha(b"a"))
    assert code_of(missing) == files.NOT_FOUND
    moved = tree.rename("old/a.txt", "new/deep/b.txt", sha(b"a"))
    assert moved.sha256 == sha(b"a")
    assert (root / "new" / "deep" / "b.txt").read_bytes() == b"a"
    assert not (root / "old").exists(), "the emptied directory is removed"
    assert (root / "taken.txt").read_bytes() == b"t"


# ---------------------------------------------------------------------------- manifest


def test_a_git_tree_is_listed_without_ignored_files_and_without_dot_git(root: Path, tree: Tree):
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("build/\n*.log\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"a = 1\n")
    (root / "src" / "run.sh").write_bytes(b"#!/bin/sh\n")
    (root / "src" / "run.sh").chmod(0o755)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    (root / "untracked.txt").write_bytes(b"new")
    (root / "debug.log").write_bytes(b"ignored")
    (root / "build").mkdir()
    (root / "build" / "out.o").write_bytes(b"ignored")
    (root / ".agent" / "traces").mkdir(parents=True)
    (root / ".agent" / "traces" / "session_001.md").write_text("trace")
    (root / "link").symlink_to("src/a.py")
    os.mkfifo(root / "pipe")  # git lists no special files

    listing = tree.listing()
    assert listing.git and listing.head is not None and len(listing.head) == 40
    assert listing.paths == [".gitignore", "link", "src/a.py", "src/run.sh", "untracked.txt"]
    described, skipped, _signatures = tree.describe(listing.paths)
    assert described == [
        {"path": ".gitignore", "size": 13, "mode": 0o644, "sha256": sha(b"build/\n*.log\n")},
        {"path": "src/a.py", "size": 6, "mode": 0o644, "sha256": sha(b"a = 1\n")},
        {"path": "src/run.sh", "size": 10, "mode": 0o755, "sha256": sha(b"#!/bin/sh\n")},
        {"path": "untracked.txt", "size": 3, "mode": 0o644, "sha256": sha(b"new")},
    ]
    assert skipped == [{"path": "link", "reason": "symlink"}]
    assert tree.git_info() == (True, listing.head, "main")

    # The dirty scope: what differs from HEAD or is untracked; a deleted file is missing.
    (root / "src" / "a.py").write_bytes(b"a = 2\n")
    (root / "src" / "run.sh").unlink()
    dirty = tree.listing("dirty")
    assert dirty.paths == ["link", "src/a.py", "untracked.txt"]
    assert dirty.missing == ["src/run.sh"]
    # A selection: a directory, a file and a path at which nothing is.
    chosen = tree.listing("all", ["src", "untracked.txt", "nothing/here.txt"])
    assert chosen.paths == ["src/a.py", "src/run.sh", "untracked.txt"]
    assert chosen.missing == ["nothing/here.txt"]


def test_a_nested_repository_is_skipped_as_a_submodule(root: Path, tree: Tree):
    git(root, "init", "-q")
    (root / "vendor").mkdir()
    git(root / "vendor", "init", "-q")
    (root / "vendor" / "lib.py").write_bytes(b"x")
    git(root / "vendor", "add", ".")
    git(root / "vendor", "commit", "-q", "-m", "inner")
    (root / "a.py").write_bytes(b"a")
    listing = tree.listing()
    described, skipped, _signatures = tree.describe(listing.paths, listing.git)
    assert [entry["path"] for entry in described] == ["a.py"]
    assert skipped == [{"path": "vendor", "reason": "submodule"}]


def test_without_git_the_tree_is_walked_with_the_built_in_ignore_list(root: Path, tree: Tree):
    for path in (
        "a.py",
        "pkg/b.py",
        "node_modules/x/index.js",
        ".venv/bin/python",
        "__pycache__/a.pyc",
    ):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(b"x")
    (root / ".git").mkdir()  # not a repository git accepts, and never listed
    (root / ".git" / "config").write_text("[core]")
    (root / "pkg" / "out").symlink_to(root.parent / "outside", target_is_directory=True)
    os.mkfifo(root / "pipe")
    listing = tree.listing()
    assert not listing.git and listing.head is None
    assert listing.paths == ["a.py", "pipe", "pkg/b.py", "pkg/out"]
    described, skipped, _signatures = tree.describe(listing.paths, listing.git)
    assert [entry["path"] for entry in described] == ["a.py", "pkg/b.py"]
    assert skipped == [
        {"path": "pipe", "reason": "not-regular"},
        {"path": "pkg/out", "reason": "symlink"},
    ]
    with pytest.raises(RpcError) as dirty:
        tree.listing("dirty")
    assert (code_of(dirty), dirty.value.data) == (files.NO_MANIFEST, {"reason": "not-git"})


def test_names_the_protocol_cannot_carry_are_skipped(root: Path, tree: Tree):
    (root / "fine.txt").write_bytes(b"x")
    (root / "new\nline.txt").write_bytes(b"x")
    (root / "back\\slash.txt").write_bytes(b"x")
    os.close(os.open(os.fsencode(root) + b"/latin-\xe9.txt", os.O_CREAT | os.O_WRONLY))
    listing = tree.listing()
    described, skipped, _signatures = tree.describe(listing.paths, listing.git)
    assert [entry["path"] for entry in described] == ["fine.txt"]
    assert sorted(entry["reason"] for entry in skipped) == ["unsupported-name"] * 3
    for entry in skipped:
        entry["path"].encode("utf-8")  # every reported name is valid text


def test_a_second_manifest_of_an_unchanged_tree_reads_no_content(
    root: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(20):
        (root / f"f{index}.txt").write_bytes(b"content %d" % index)
    old = 1_600_000_000
    for path in root.iterdir():
        os.utime(path, (old, old))
    tree = Tree(root, cache=HashCache())
    try:
        first, _skipped, _signatures = tree.describe(tree.listing().paths, False)
        reads: list[int] = []
        real = os.pread
        monkeypatch.setattr(os, "pread", lambda *a: reads.append(1) or real(*a))
        second, _skipped, _signatures = tree.describe(tree.listing().paths, False)
        assert second == first and reads == []
        (root / "f3.txt").write_bytes(b"changed")
        third, _skipped, _signatures = tree.describe(tree.listing().paths, False)
        assert reads, "the changed file is read again"
        assert {entry["path"]: entry["sha256"] for entry in third}["f3.txt"] == sha(b"changed")
    finally:
        tree.close()


def test_calus_own_directory_inside_the_project_is_excluded(
    root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "cfg"))
    (root / "cfg" / "calu").mkdir(parents=True)
    (root / "cfg" / "calu" / "credentials.json").write_text("{}")
    (root / "a.py").write_bytes(b"a")
    tree = Tree(root)
    try:
        assert "cfg/calu" in tree.excluded and files.TRACE_DIRECTORY in tree.excluded
        assert tree.listing().paths == ["a.py"]
        with pytest.raises(RpcError) as refused:
            clean_path("cfg/calu/credentials.json", tree.excluded)
        assert refused.value.data == {"reason": "excluded"}
    finally:
        tree.close()


def test_the_case_rule_of_the_file_system_is_probed_without_writing(root: Path, tree: Tree):
    assert tree.case_sensitive() is True
    (root / "Readme.md").write_bytes(b"x")
    before = sorted(p.name for p in root.iterdir())
    sensitive = tree.case_sensitive()
    assert sorted(p.name for p in root.iterdir()) == before
    assert sensitive is not (root / "rEADME.MD").exists()
