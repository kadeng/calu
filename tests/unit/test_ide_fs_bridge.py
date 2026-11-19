"""The file requests of `calu ide` through the in-memory editor: no stdio, no database.

The contract is `docs/notes/jetbrains-sync-spec.md`. The session, where one is attached, is a
served fake session (`tests.unit.test_protocol_server.served`).
"""

import base64
import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.config import ChangesConfig, Config
from calu.credentials import CredentialStore
from calu.ide import files
from calu.protocol.messages import Log
from calu.store import MemorySessionStore
from tests.unit.ide_helpers import Editor, Failed, bridged
from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio

FAST = {"poll_s": 0.1, "quiet_s": 0.05}
"""Bridge options that make the watcher and the batching quick enough for tests."""


@pytest.fixture(autouse=True)
def own_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the token-free sockets at an empty directory, so no real session is found."""
    with tempfile.TemporaryDirectory(prefix="calu-ide-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        yield Path(directory)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An empty project directory (the credential store lies beside it, not in it)."""
    root = tmp_path / "project"
    root.mkdir()
    return root


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


def b64(data: bytes) -> str:
    """Encode bytes as a request carries them.

    Args:
        data: The content.
    """
    return base64.b64encode(data).decode()


def git(root: Path, *arguments: str) -> str:
    """Run git in a directory with a fixed identity and return its output.

    Args:
        root: The directory.
        *arguments: git's arguments.
    """
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def changes(editor: Editor) -> list[dict[str, Any]]:
    """Return every change the editor was notified of, in order.

    Args:
        editor: The in-memory editor.
    """
    return [
        change
        for name, params in editor.notifications
        if name == "ide.fs.changed"
        for change in params["changes"]
    ]


async def wait_change(editor: Editor, path: str, count: int = 1, timeout: float = 10) -> dict:
    """Wait until the editor was notified of a path `count` times; return the last change.

    Args:
        editor: The in-memory editor.
        path: The changed file.
        count: How many changes of the path are awaited.
        timeout: Seconds before the wait fails.
    """
    with anyio.fail_after(timeout):
        while True:
            found = [change for change in changes(editor) if change["path"] == path]
            if len(found) >= count:
                return found[-1]
            await anyio.sleep(0.02)


async def refused(editor: Editor, method: str, params: dict[str, Any]) -> Failed:
    """Send a request that must fail and return the error.

    Args:
        editor: The in-memory editor.
        method: The request.
        params: Its params.
    """
    with pytest.raises(Failed) as error:
        await editor.request(method, params)
    return error.value


# ------------------------------------------------------------------ who the bridge is


async def test_the_bridge_names_its_capabilities_answers_pings_and_says_where_it_works(
    project: Path, store: CredentialStore
):
    git(project, "init", "-q", "-b", "trunk")
    (project / "a.py").write_bytes(b"a")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "first")
    async with bridged(project, store) as editor:
        hello = await editor.request("ide.initialize", {"client": "test"})
        assert hello["capabilities"] == ["cwd", "ping", "paths", "fs", "fs.watch"]
        pong = await editor.request("ide.ping", {"nonce": "n-1"})
        assert pong["nonce"] == "n-1" and abs(pong["time"] - time.time()) < 60
        assert (await editor.request("ide.ping"))["nonce"] is None
        where = await editor.request("ide.paths")
        assert where == {
            "root": str(project.resolve()),
            "home": str(Path.home()),
            "separator": "/",
            "git": True,
            "head": git(project, "rev-parse", "HEAD"),
            "branch": "trunk",
            "case_sensitive": where["case_sensitive"],
            "limits": {
                "file_bytes": 32 * 1024 * 1024,
                "request_bytes": 1024 * 1024,
                "page_entries": 2000,
                "uploads": 4,
            },
        }


# ---------------------------------------------------------------------------- manifest


async def test_the_manifest_of_a_git_tree_leaves_out_ignored_files(
    project: Path, store: CredentialStore
):
    git(project, "init", "-q")
    (project / ".gitignore").write_text("*.log\nbuild/\n")
    (project / "src").mkdir()
    (project / "src" / "a.py").write_bytes(b"a = 1\n")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "first")
    (project / "new.txt").write_bytes(b"new")
    (project / "debug.log").write_bytes(b"ignored")
    (project / "build").mkdir()
    (project / "build" / "x.o").write_bytes(b"ignored")
    (project / "link").symlink_to("src/a.py")
    async with bridged(project, store) as editor:
        manifest = await editor.request("ide.fs.manifest")
        assert manifest["head"] == git(project, "rev-parse", "HEAD")
        assert manifest["files"] == [
            {"path": ".gitignore", "size": 13, "mode": 0o644, "sha256": sha(b"*.log\nbuild/\n")},
            {"path": "new.txt", "size": 3, "mode": 0o644, "sha256": sha(b"new")},
            {"path": "src/a.py", "size": 6, "mode": 0o644, "sha256": sha(b"a = 1\n")},
        ]
        assert manifest["skipped"] == [{"path": "link", "reason": "symlink"}]
        assert manifest["missing"] == [] and manifest["next_cursor"] is None

        dirty = await editor.request("ide.fs.manifest", {"scope": "dirty"})
        assert [entry["path"] for entry in dirty["files"]] == ["new.txt"]
        chosen = await editor.request("ide.fs.manifest", {"paths": ["src", "gone.txt"]})
        assert [entry["path"] for entry in chosen["files"]] == ["src/a.py"]
        assert chosen["missing"] == ["gone.txt"]
        nothing = await editor.request("ide.fs.manifest", {"paths": []})
        assert nothing["files"] == [] and nothing["head"] == manifest["head"]

        for bad in ([".git"], ["../x"], ["/etc"], [".agent/traces"]):
            error = await refused(editor, "ide.fs.manifest", {"paths": bad})
            assert error.code == files.PATH_REFUSED
        assert (await refused(editor, "ide.fs.manifest", {"scope": "some"})).code == -32602


async def test_a_long_manifest_comes_in_pages_and_notices_a_moved_head(
    project: Path, store: CredentialStore, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(files, "PAGE_ENTRIES", 3)
    git(project, "init", "-q")
    for index in range(8):
        (project / f"f{index}.txt").write_bytes(b"%d" % index)
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "first")
    async with bridged(project, store) as editor:
        seen: list[str] = []
        heads = set()
        cursor = None
        pages = 0
        while True:
            page = await editor.request("ide.fs.manifest", {"cursor": cursor} if cursor else {})
            seen += [entry["path"] for entry in page["files"]]
            heads.add(page["head"])
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert pages == 3 and seen == [f"f{index}.txt" for index in range(8)]
        assert len(heads) == 1

        first = await editor.request("ide.fs.manifest")
        git(project, "commit", "-q", "--allow-empty", "-m", "the agent committed")
        moved = await refused(editor, "ide.fs.manifest", {"cursor": first["next_cursor"]})
        assert (moved.code, moved.data) == (files.NO_MANIFEST, {"reason": "head-moved"})
        expired = await refused(editor, "ide.fs.manifest", {"cursor": first["next_cursor"]})
        assert (expired.code, expired.data) == (files.NO_MANIFEST, {"reason": "cursor-expired"})
        assert (await refused(editor, "ide.fs.manifest", {"cursor": "junk"})).code == -32602


# ------------------------------------------------------------------------------- reads


async def test_a_file_is_read_in_ranges(project: Path, store: CredentialStore):
    content = bytes(range(256)) * 20
    (project / "data.bin").write_bytes(content)
    async with bridged(project, store) as editor:
        whole = await editor.request("ide.fs.read", {"path": "data.bin"})
        assert base64.b64decode(whole["content_b64"]) == content
        assert whole == {
            "content_b64": whole["content_b64"],
            "sha256": sha(content),
            "size": len(content),
            "mode": 0o644,
            "eof": True,
        }
        pieces = b""
        offset = 0
        while True:
            piece = await editor.request(
                "ide.fs.read", {"path": "data.bin", "offset": offset, "length": 2000}
            )
            assert piece["sha256"] == sha(content)
            data = base64.b64decode(piece["content_b64"])
            pieces += data
            offset += len(data)
            if piece["eof"]:
                break
        assert pieces == content
        # A request never carries more than 1 MiB, whatever it asks for.
        (project / "big.bin").write_bytes(bytes(files.REQUEST_BYTES + 10))
        capped = await editor.request("ide.fs.read", {"path": "big.bin", "length": 10**9})
        assert len(base64.b64decode(capped["content_b64"])) == files.REQUEST_BYTES
        assert capped["eof"] is False

        assert (await refused(editor, "ide.fs.read", {"path": "none"})).code == files.NOT_FOUND
        assert (await refused(editor, "ide.fs.read", {"path": "a", "offset": -1})).code == -32602
        (project / "dir").mkdir()
        directory = await refused(editor, "ide.fs.read", {"path": "dir"})
        assert (directory.code, directory.data) == (files.NOT_REGULAR, {"kind": "directory"})


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../outside.txt", "outside"),
        ("/etc/passwd", "outside"),
        ("a/../../outside.txt", "outside"),
        ("C:/Users/x", "outside"),
        ("a\0b", "invalid"),
        ("a\nb", "invalid"),
        ("a\\b", "invalid"),
        ("", "invalid"),
        (".git/config", "excluded"),
        ("sub/.git/HEAD", "excluded"),
        (".agent/traces/s.md", "excluded"),
    ],
)
async def test_every_file_request_refuses_a_path_outside_the_project(
    project: Path, store: CredentialStore, path: str, reason: str
):
    (project.parent / "outside.txt").write_text("outside")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]")
    base = {"base_sha256": None}
    async with bridged(project, store) as editor:
        requests: list[tuple[str, dict[str, Any]]] = [
            ("ide.fs.read", {"path": path}),
            ("ide.fs.write", {"path": path, "content_b64": b64(b"x"), **base}),
            ("ide.fs.write.begin", {"path": path, "size": 1, "sha256": sha(b"x"), **base}),
            ("ide.fs.delete", {"path": path, "base_sha256": sha(b"x")}),
            ("ide.fs.rename", {"from": path, "to": "ok.txt", "base_sha256": sha(b"x")}),
            ("ide.fs.rename", {"from": "ok.txt", "to": path, "base_sha256": sha(b"x")}),
            ("ide.fs.manifest", {"paths": [path]}),
        ]
        for method, params in requests:
            error = await refused(editor, method, params)
            assert (error.code, error.data) == (files.PATH_REFUSED, {"reason": reason}), method
    assert (project.parent / "outside.txt").read_text() == "outside"
    assert (project / ".git" / "config").read_text() == "[core]"
    assert sorted(p.name for p in project.iterdir()) == [".git"]


# ------------------------------------------------------------------------------ writes


async def test_a_write_needs_the_matching_base(project: Path, store: CredentialStore):
    async with bridged(project, store) as editor:
        created = await editor.request(
            "ide.fs.write", {"path": "src/a.py", "content_b64": b64(b"one"), "base_sha256": None}
        )
        assert created == {"sha256": sha(b"one"), "size": 3}
        assert (project / "src" / "a.py").read_bytes() == b"one"
        replaced = await editor.request(
            "ide.fs.write",
            {
                "path": "src/a.py",
                "content_b64": b64(b"two"),
                "base_sha256": sha(b"one"),
                "mode": 0o755,
            },
        )
        assert replaced["sha256"] == sha(b"two")
        assert (project / "src" / "a.py").stat().st_mode & 0o777 == 0o755

        for base in (sha(b"one"), None):
            stale = await refused(
                editor,
                "ide.fs.write",
                {"path": "src/a.py", "content_b64": b64(b"three"), "base_sha256": base},
            )
            assert stale.code == files.BASE_MISMATCH
            assert stale.data == {"sha256": sha(b"two"), "size": 3, "mode": 0o755}
        absent = await refused(
            editor,
            "ide.fs.write",
            {"path": "new/b.py", "content_b64": b64(b"x"), "base_sha256": sha(b"gone")},
        )
        assert absent.code == files.BASE_MISMATCH and details(absent)["sha256"] is None
        assert (project / "src" / "a.py").read_bytes() == b"two" and not (project / "new").exists()

        ok = {"path": "c.txt", "base_sha256": None}
        for params in (
            {"path": "c.txt", "content_b64": b64(b"x")},  # no base at all
            {**ok, "content_b64": "not base64!"},
            {**ok, "content_b64": b64(b"x"), "mode": 0o4755},
            {**ok, "content_b64": b64(b"x"), "during_turn": "yes"},
            {**ok, "content_b64": b64(b"x"), "base_sha256": "ABC"},
        ):
            assert (await refused(editor, "ide.fs.write", params)).code == -32602
        large = await refused(
            editor, "ide.fs.write", {**ok, "content_b64": "A" * (files.REQUEST_BYTES * 2)}
        )
        assert large.code == files.TOO_LARGE and details(large)["limit"] == files.REQUEST_BYTES
        assert not (project / "c.txt").exists()
    assert list(project.rglob(files.UPLOAD_PREFIX + "*")) == []


async def test_a_chunked_write_is_checked_and_committed_in_one_step(
    project: Path, store: CredentialStore
):
    content = bytes(range(256)) * 9000  # 2.3 MB: three chunks
    (project / "big.bin").write_bytes(b"old")
    async with bridged(project, store) as editor:
        begun = await editor.request(
            "ide.fs.write.begin",
            {
                "path": "big.bin",
                "base_sha256": sha(b"old"),
                "size": len(content),
                "sha256": sha(content),
            },
        )
        assert begun["chunk_size"] == files.REQUEST_BYTES
        upload = begun["upload_id"]
        wrong = await refused(
            editor,
            "ide.fs.write.chunk",
            {"upload_id": upload, "offset": 5, "content_b64": b64(b"x")},
        )
        assert (wrong.code, wrong.data) == (
            files.UPLOAD_FAILED,
            {"reason": "offset", "received": 0},
        )
        offset = 0
        while offset < len(content):
            piece = content[offset : offset + begun["chunk_size"]]
            answer = await editor.request(
                "ide.fs.write.chunk",
                {"upload_id": upload, "offset": offset, "content_b64": b64(piece)},
            )
            offset += len(piece)
            assert answer == {"received": offset}
            assert (project / "big.bin").read_bytes() == b"old", "the target is untouched"
        assert len(list(project.glob(files.UPLOAD_PREFIX + "*"))) == 1
        done = await editor.request("ide.fs.write.commit", {"upload_id": upload})
        assert done == {"sha256": sha(content), "size": len(content)}
        assert (project / "big.bin").read_bytes() == content
        again = await refused(editor, "ide.fs.write.commit", {"upload_id": upload})
        assert (again.code, details(again)["reason"]) == (files.UPLOAD_FAILED, "unknown-upload")
    assert list(project.rglob(files.UPLOAD_PREFIX + "*")) == []


async def test_an_aborted_a_wrong_and_a_stale_upload_leave_nothing_behind(
    project: Path, store: CredentialStore
):
    (project / "a.txt").write_bytes(b"old")
    async with bridged(project, store) as editor:

        async def begin(path: str, content: bytes, base: str | None, size: int | None = None):
            """Open an upload and send its content as one chunk; return the upload's id.

            Args:
                path: The target.
                content: What is sent.
                base: The base the upload names.
                size: The announced size; None announces the content's.
            """
            begun = await editor.request(
                "ide.fs.write.begin",
                {
                    "path": path,
                    "base_sha256": base,
                    "size": len(content) if size is None else size,
                    "sha256": sha(content),
                },
            )
            await editor.request(
                "ide.fs.write.chunk",
                {"upload_id": begun["upload_id"], "offset": 0, "content_b64": b64(content)},
            )
            return begun["upload_id"]

        # Abort: the temporary file and the directory made for it are gone.
        aborted = await begin("made/for/it.txt", b"partial", None)
        assert (project / "made" / "for").is_dir()
        assert await editor.request("ide.fs.write.abort", {"upload_id": aborted}) == {}
        assert not (project / "made").exists()
        assert await editor.request("ide.fs.write.abort", {"upload_id": aborted}) == {}

        # Fewer bytes than announced, and another content than announced.
        short = await begin("a.txt", b"12345", sha(b"old"), size=9)
        error = await refused(editor, "ide.fs.write.commit", {"upload_id": short})
        assert (error.code, error.data) == (files.UPLOAD_FAILED, {"reason": "size", "received": 5})
        begun = await editor.request(
            "ide.fs.write.begin",
            {"path": "a.txt", "base_sha256": sha(b"old"), "size": 3, "sha256": sha(b"abc")},
        )
        await editor.request(
            "ide.fs.write.chunk",
            {"upload_id": begun["upload_id"], "offset": 0, "content_b64": b64(b"xyz")},
        )
        error = await refused(editor, "ide.fs.write.commit", {"upload_id": begun["upload_id"]})
        assert (error.code, details(error)["reason"]) == (files.UPLOAD_FAILED, "sha256")
        # More bytes than announced end the upload at the chunk.
        begun = await editor.request(
            "ide.fs.write.begin",
            {"path": "a.txt", "base_sha256": sha(b"old"), "size": 2, "sha256": sha(b"ab")},
        )
        error = await refused(
            editor,
            "ide.fs.write.chunk",
            {"upload_id": begun["upload_id"], "offset": 0, "content_b64": b64(b"abc")},
        )
        assert (error.code, details(error)["reason"]) == (files.UPLOAD_FAILED, "size")

        # The base moved between begin and commit: refused, the other content stays.
        stale = await begin("a.txt", b"mine", sha(b"old"))
        (project / "a.txt").write_bytes(b"theirs")
        error = await refused(editor, "ide.fs.write.commit", {"upload_id": stale})
        assert error.code == files.BASE_MISMATCH and details(error)["sha256"] == sha(b"theirs")
        assert (project / "a.txt").read_bytes() == b"theirs"

        # Refused before a byte is sent: the size, the base.
        huge = await refused(
            editor,
            "ide.fs.write.begin",
            {"path": "h.bin", "base_sha256": None, "size": 2**40, "sha256": sha(b"")},
        )
        assert huge.code == files.TOO_LARGE and details(huge)["limit"] == files.MAX_FILE_BYTES
        early = await refused(
            editor,
            "ide.fs.write.begin",
            {"path": "a.txt", "base_sha256": None, "size": 1, "sha256": sha(b"x")},
        )
        assert early.code == files.BASE_MISMATCH

        # An empty file needs no chunk.
        empty = await editor.request(
            "ide.fs.write.begin",
            {"path": "empty.txt", "base_sha256": None, "size": 0, "sha256": sha(b"")},
        )
        done = await editor.request("ide.fs.write.commit", {"upload_id": empty["upload_id"]})
        assert done == {"sha256": sha(b""), "size": 0}
        assert (project / "empty.txt").read_bytes() == b""
        assert list(project.rglob(files.UPLOAD_PREFIX + "*")) == []


async def test_open_uploads_are_limited_expire_and_are_removed_when_the_bridge_ends(
    project: Path, store: CredentialStore
):
    async with bridged(project, store, upload_idle_s=0.4) as editor:
        uploads = []
        for index in range(files.MAX_UPLOADS):
            begun = await editor.request(
                "ide.fs.write.begin",
                {"path": f"u{index}.bin", "base_sha256": None, "size": 4, "sha256": sha(b"abcd")},
            )
            uploads.append(begun["upload_id"])
            await editor.request(
                "ide.fs.write.chunk",
                {"upload_id": begun["upload_id"], "offset": 0, "content_b64": b64(b"ab")},
            )
        full = await refused(
            editor,
            "ide.fs.write.begin",
            {"path": "more.bin", "base_sha256": None, "size": 1, "sha256": sha(b"x")},
        )
        assert (full.code, details(full)["reason"]) == (files.UPLOAD_FAILED, "too-many-uploads")
        assert len(list(project.glob(files.UPLOAD_PREFIX + "*"))) == files.MAX_UPLOADS
        # Nothing is asked for a while: the uploads are discarded.
        with anyio.fail_after(10):
            while list(project.glob(files.UPLOAD_PREFIX + "*")):
                await anyio.sleep(0.05)
        gone = await refused(
            editor,
            "ide.fs.write.chunk",
            {"upload_id": uploads[0], "offset": 2, "content_b64": b64(b"cd")},
        )
        assert (gone.code, details(gone)["reason"]) == (files.UPLOAD_FAILED, "unknown-upload")
        # One more, left open when the editor goes away.
        begun = await editor.request(
            "ide.fs.write.begin",
            {"path": "left/open.bin", "base_sha256": None, "size": 4, "sha256": sha(b"abcd")},
        )
        await editor.request(
            "ide.fs.write.chunk",
            {"upload_id": begun["upload_id"], "offset": 0, "content_b64": b64(b"ab")},
        )
        assert len(list(project.rglob(files.UPLOAD_PREFIX + "*"))) == 1
    assert list(project.iterdir()) == [], "the bridge's end removed the partial upload"


async def test_delete_and_rename_need_the_base(project: Path, store: CredentialStore):
    (project / "d").mkdir()
    (project / "d" / "a.txt").write_bytes(b"a")
    (project / "b.txt").write_bytes(b"b")
    async with bridged(project, store) as editor:
        stale = await refused(
            editor, "ide.fs.delete", {"path": "d/a.txt", "base_sha256": sha(b"z")}
        )
        assert stale.code == files.BASE_MISMATCH and details(stale)["sha256"] == sha(b"a")
        taken = await refused(
            editor, "ide.fs.rename", {"from": "d/a.txt", "to": "b.txt", "base_sha256": sha(b"a")}
        )
        assert (taken.code, taken.data) == (files.TARGET_EXISTS, {"sha256": sha(b"b")})
        missing = await refused(
            editor, "ide.fs.rename", {"from": "none.txt", "to": "x.txt", "base_sha256": sha(b"a")}
        )
        assert missing.code == files.NOT_FOUND
        assert (await refused(editor, "ide.fs.delete", {"path": "b.txt"})).code == -32602

        moved = {"from": "d/a.txt", "to": "e/f/a.txt", "base_sha256": sha(b"a")}
        assert await editor.request("ide.fs.rename", moved) == {}
        assert (project / "e" / "f" / "a.txt").read_bytes() == b"a" and not (project / "d").exists()
        assert (
            await editor.request("ide.fs.delete", {"path": "e/f/a.txt", "base_sha256": sha(b"a")})
            == {}
        )
        assert not (project / "e").exists() and (project / "b.txt").exists()
        gone = await refused(
            editor, "ide.fs.delete", {"path": "e/f/a.txt", "base_sha256": sha(b"a")}
        )
        assert gone.code == files.BASE_MISMATCH and details(gone)["sha256"] is None


# ----------------------------------------------------------------------- subscription


async def test_a_subscriber_is_told_about_changes_on_disk_and_about_its_own_writes(
    project: Path, store: CredentialStore
):
    (project / "a.txt").write_bytes(b"one")
    (project / "gone.txt").write_bytes(b"x")
    async with bridged(project, store, **FAST) as editor:
        await editor.request("ide.fs.manifest")
        subscribed = await editor.request("ide.fs.subscribe")
        assert subscribed["seq"] == 0 and subscribed["complete"] is True
        epoch = subscribed["epoch"]

        # Somebody else changes, creates and deletes files (a shell, another process).
        (project / "a.txt").write_bytes(b"two")
        (project / "new.txt").write_bytes(b"new")
        (project / "gone.txt").unlink()
        assert await wait_change(editor, "a.txt") == {
            "path": "a.txt",
            "before_sha256": sha(b"one"),
            "after_sha256": sha(b"two"),
            "deleted": False,
            "origin": "other",
        }
        created = await wait_change(editor, "new.txt")
        assert (created["before_sha256"], created["after_sha256"]) == (None, sha(b"new"))
        deleted = await wait_change(editor, "gone.txt")
        assert deleted["deleted"] is True and deleted["after_sha256"] is None

        # The editor's own write comes back once, marked as its echo.
        await editor.request(
            "ide.fs.write",
            {"path": "a.txt", "content_b64": b64(b"three"), "base_sha256": sha(b"two")},
        )
        echo = await wait_change(editor, "a.txt", 2)
        assert (echo["origin"], echo["before_sha256"], echo["after_sha256"]) == (
            "ide",
            sha(b"two"),
            sha(b"three"),
        )
        await editor.request("ide.fs.delete", {"path": "new.txt", "base_sha256": sha(b"new")})
        assert (await wait_change(editor, "new.txt", 2))["origin"] == "ide"
        await anyio.sleep(0.5)  # several scans later: nothing is reported twice
        assert [c["path"] for c in changes(editor)].count("a.txt") == 2
        assert [c["path"] for c in changes(editor)].count("new.txt") == 2

        # Touching a file without changing its content is no change.
        (project / "a.txt").write_bytes(b"three")
        await anyio.sleep(0.5)
        assert [c["path"] for c in changes(editor)].count("a.txt") == 2

        notes = [params for name, params in editor.notifications if name == "ide.fs.changed"]
        assert [note["seq"] for note in notes] == list(range(1, len(notes) + 1))
        assert {note["epoch"] for note in notes} == {epoch}
        first_note = next(
            i for i, m in enumerate(editor.wire) if m.get("method") == "ide.fs.changed"
        )
        answer = next(i for i, m in enumerate(editor.wire) if m.get("result") == subscribed)
        assert answer < first_note, "no notification before the answer to the subscription"

        # A renewed subscription replays what the editor says it missed.
        seen = len(notes)
        again = await editor.request("ide.fs.subscribe", {"after_seq": 1, "epoch": epoch})
        assert again == {"epoch": epoch, "seq": seen, "complete": True}
        with anyio.fail_after(5):
            while len([n for n, _ in editor.notifications if n == "ide.fs.changed"]) < 2 * seen - 1:
                await anyio.sleep(0.02)
        replayed = [p["seq"] for n, p in editor.notifications if n == "ide.fs.changed"][seen:]
        assert replayed == list(range(2, seen + 1))
        other = await editor.request("ide.fs.subscribe", {"after_seq": 1, "epoch": "another"})
        assert other["complete"] is False and other["epoch"] == epoch

        # Without a subscription nothing is watched, so a later one is not complete.
        assert await editor.request("ide.fs.unsubscribe") == {}
        (project / "unseen.txt").write_bytes(b"x")
        await anyio.sleep(0.3)
        late = await editor.request("ide.fs.subscribe", {"after_seq": seen, "epoch": epoch})
        assert late["complete"] is False
        (project / "seen.txt").write_bytes(b"y")
        assert (await wait_change(editor, "seen.txt"))["origin"] == "other"
        assert "unseen.txt" not in [c["path"] for c in changes(editor)]


def skips(editor: Editor) -> list[dict[str, Any]]:
    """Return every skipped path the editor was notified of, in order.

    Args:
        editor: The in-memory editor.
    """
    return [
        entry
        for name, params in editor.notifications
        if name == "ide.fs.changed"
        for entry in params.get("skipped", [])
    ]


async def test_a_path_the_bridge_would_skip_is_reported_as_skipped_at_once(
    project: Path, store: CredentialStore
):
    """The watcher said nothing about a file it would not list, so an editor listed a file
    above the size limit only at its next full comparison."""
    (project / "grows.bin").write_bytes(b"small")
    async with bridged(project, store, **FAST) as editor:
        await editor.request("ide.fs.subscribe")
        with (project / "huge.bin").open("wb") as huge:
            huge.truncate(files.MAX_FILE_BYTES + 1)  # sparse: nothing is written
        os.mkfifo(project / "pipe")
        with (project / "grows.bin").open("r+b") as grows:
            grows.truncate(files.MAX_FILE_BYTES + 1)
        (project / "plain.txt").write_bytes(b"plain")
        await wait_change(editor, "plain.txt")
        with anyio.fail_after(5):
            while len(skips(editor)) < 3:
                await anyio.sleep(0.02)
        await anyio.sleep(0.3)  # several scans later: each is reported once
        assert sorted((s["path"], s["reason"]) for s in skips(editor)) == [
            ("grows.bin", "too-large"),
            ("huge.bin", "too-large"),
            ("pipe", "not-regular"),
        ]
        assert {c["path"] for c in changes(editor)} == {"plain.txt"}
        notes = [params for name, params in editor.notifications if name == "ide.fs.changed"]
        assert all(set(note) == {"epoch", "seq", "changes", "skipped"} for note in notes)


async def test_ignored_and_excluded_files_are_not_reported(project: Path, store: CredentialStore):
    git(project, "init", "-q")
    (project / ".gitignore").write_text("*.log\n")
    async with bridged(project, store, **FAST) as editor:
        await editor.request("ide.fs.subscribe")
        (project / "debug.log").write_bytes(b"noise")
        (project / ".agent" / "traces").mkdir(parents=True)
        (project / ".agent" / "traces" / "s.md").write_text("trace")
        (project / ".git" / "marker").write_text("x")
        (project / "real.txt").write_bytes(b"real")
        await wait_change(editor, "real.txt")
        await anyio.sleep(0.3)
        assert {change["path"] for change in changes(editor)} == {"real.txt"}


async def test_two_bridges_on_one_project_see_each_others_writes(
    project: Path, store: CredentialStore
):
    (project / "shared.txt").write_bytes(b"one")
    async with bridged(project, store, **FAST) as first, bridged(project, store, **FAST) as second:
        one = await first.request("ide.fs.subscribe")
        two = await second.request("ide.fs.subscribe")
        assert one["epoch"] != two["epoch"]
        await first.request(
            "ide.fs.write",
            {"path": "shared.txt", "content_b64": b64(b"two"), "base_sha256": sha(b"one")},
        )
        seen = await wait_change(second, "shared.txt")
        assert (seen["origin"], seen["after_sha256"]) == ("other", sha(b"two"))
        assert (await wait_change(first, "shared.txt"))["origin"] == "ide"
        # The second editor's stale upload is refused with the first one's content.
        stale = await refused(
            second,
            "ide.fs.write",
            {"path": "shared.txt", "content_b64": b64(b"mine"), "base_sha256": sha(b"one")},
        )
        assert stale.code == files.BASE_MISMATCH and details(stale)["sha256"] == sha(b"two")
        await second.request(
            "ide.fs.write",
            {"path": "from-two.txt", "content_b64": b64(b"2"), "base_sha256": None},
        )
        assert (await wait_change(first, "from-two.txt"))["origin"] == "other"


# ------------------------------------------------------------ with an attached session

EDIT_THEN_ASK = [
    [
        {
            "tool": "Write",
            "input": {"file_path": "agent.py", "content": "x = 2\n"},
            "writes": {"agent.py": "x = 2\n"},
            "result": "written",
        },
        {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"},
    ]
]
"""One turn: the agent writes `agent.py`, then a command waits for a human."""


async def test_an_edit_of_the_session_is_announced_and_its_file_is_held_during_the_turn(
    project: Path, store: CredentialStore
):
    (project / "agent.py").write_bytes(b"x = 1\n")
    (project / "mine.py").write_bytes(b"m = 1\n")
    async with served(project, EDIT_THEN_ASK) as run:
        async with bridged(project, store, poll_s=30, quiet_s=0.05) as editor:
            attached = await editor.request(
                "ide.attach", {"address": run.address, "token": run.token}
            )
            await editor.request("ide.fs.subscribe")
            await editor.call("session.start")
            await editor.call("chat.send", {"text": "change it"})
            asked = (await editor.wait_event("escalation.request"))[0]
            assert [e["path"] for e in await editor.wait_event("edit.diff")] == ["agent.py"]

            # The watcher sleeps for 30 s here: the session's events made the bridge look.
            change = await wait_change(editor, "agent.py")
            assert change == {
                "path": "agent.py",
                "before_sha256": None,
                "after_sha256": sha(b"x = 2\n"),
                "deleted": False,
                "origin": "agent",
            }

            # The turn still runs (it waits for a human): the agent's file is held.
            upload = {
                "path": "agent.py",
                "content_b64": b64(b"x = 3\n"),
                "base_sha256": sha(b"x = 2\n"),
            }
            held = await refused(editor, "ide.fs.write", upload)
            assert held.code == files.HELD
            assert held.data == {
                "session_id": details(held)["session_id"],
                "sha256": sha(b"x = 2\n"),
            }
            # The fake session has no row in a database, so its id is empty here.
            assert details(held)["session_id"] == attached["session_id"]
            for method, params in (
                ("ide.fs.delete", {"path": "agent.py", "base_sha256": sha(b"x = 2\n")}),
                (
                    "ide.fs.rename",
                    {"from": "agent.py", "to": "b.py", "base_sha256": sha(b"x = 2\n")},
                ),
                (
                    "ide.fs.write.begin",
                    {**upload, "size": 6, "sha256": sha(b"x = 3\n")},
                ),
            ):
                assert (await refused(editor, method, params)).code == files.HELD, method
            assert (project / "agent.py").read_bytes() == b"x = 2\n"
            # Another file is not held, and "upload now" overrides the hold (not the base).
            await editor.request(
                "ide.fs.write",
                {"path": "mine.py", "content_b64": b64(b"m = 2\n"), "base_sha256": sha(b"m = 1\n")},
            )
            stale = await refused(
                editor,
                "ide.fs.write",
                {**upload, "base_sha256": sha(b"x = 1\n"), "during_turn": True},
            )
            assert stale.code == files.BASE_MISMATCH
            await editor.request("ide.fs.write", {**upload, "during_turn": True})
            assert (project / "agent.py").read_bytes() == b"x = 3\n"

            # The session was told that these are the user's writes.
            await anyio.sleep(0.2)
            told = [
                event["params"]["message"]
                for event in editor.events(Log.method)
                if "the user wrote" in event["params"]["message"]
            ]
            assert len(told) == 2 and "mine.py" in told[0] and "agent.py" in told[1]

            # The turn ends: nothing is held any more.
            await editor.call(
                "escalation.decide", {"escalation_id": asked["escalation_id"], "decision": "allow"}
            )
            await editor.wait_event("turn.result")
            with anyio.fail_after(10):
                while (await editor.call("state.get"))["state"] != "idle":
                    await anyio.sleep(0.02)
            await anyio.sleep(0.2)
            await editor.request(
                "ide.fs.write",
                {
                    "path": "agent.py",
                    "content_b64": b64(b"x = 4\n"),
                    "base_sha256": sha(b"x = 3\n"),
                },
            )


async def test_the_subscription_outlives_the_connection_to_the_session(
    project: Path, store: CredentialStore
):
    (project / "agent.py").write_bytes(b"x = 1\n")
    async with bridged(project, store, reconnect_wait_s=0.5, **FAST) as editor:
        async with served(project, EDIT_THEN_ASK) as run:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            subscribed = await editor.request("ide.fs.subscribe")
            await editor.call("session.start")
            await editor.call("chat.send", {"text": "change it"})
            await editor.wait_event("escalation.request")
            await wait_change(editor, "agent.py")
            held = await refused(
                editor,
                "ide.fs.write",
                {"path": "agent.py", "content_b64": b64(b"u\n"), "base_sha256": sha(b"x = 2\n")},
            )
            assert held.code == files.HELD
        # The session is gone. The bridge keeps watching, in the same epoch and sequence.
        await editor.wait_link("lost")
        (project / "later.txt").write_bytes(b"later")
        assert (await wait_change(editor, "later.txt"))["origin"] == "other"
        notes = [params for name, params in editor.notifications if name == "ide.fs.changed"]
        assert [note["seq"] for note in notes] == list(range(1, len(notes) + 1))
        assert {note["epoch"] for note in notes} == {subscribed["epoch"]}
        # A lost session holds nothing.
        await editor.request(
            "ide.fs.write",
            {"path": "agent.py", "content_b64": b64(b"u\n"), "base_sha256": sha(b"x = 2\n")},
        )
        again = await editor.request(
            "ide.fs.subscribe", {"after_seq": notes[-1]["seq"], "epoch": subscribed["epoch"]}
        )
        assert again["complete"] is True


async def test_an_upload_during_a_command_is_not_counted_as_the_commands_write(
    project: Path, store: CredentialStore
):
    """The command's write set is reviewed and undone; the user's upload is neither."""
    turns = [
        [
            {
                "tool": "Bash",
                "input": {"command": "generate"},
                "duration": 1.5,
                "writes": {"by-command.txt": "generated # calu:deny\n"},
                "result": "generated",
            }
        ]
    ]
    config = Config(changes=ChangesConfig(snapshot=True, review=True))
    memory = MemorySessionStore()
    async with served(project, turns, config=config, store=memory) as run:
        async with bridged(project, store, **FAST) as editor:
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            await editor.call("session.start")
            await editor.call("chat.send", {"text": "generate it"})
            await editor.wait_event("tool.use")
            with anyio.fail_after(10):
                while (await editor.call("state.get"))["state"] != "tool_call":
                    await anyio.sleep(0.02)
            await anyio.sleep(0.3)  # the command runs: its "before" snapshot is taken
            await editor.request(
                "ide.fs.write",
                {"path": "by-user.txt", "content_b64": b64(b"typed\n"), "base_sha256": None},
            )
            await editor.wait_event("turn.result", timeout=30)
    recorded = list(memory.write_sets.values())
    assert [set(entry["paths"]) for entry in recorded] == [{"by-command.txt"}]
    assert not recorded[0]["overlapping"]
    assert not (project / "by-command.txt").exists(), "the refused change was undone"
    assert (project / "by-user.txt").read_bytes() == b"typed\n", "the user's file was not"
