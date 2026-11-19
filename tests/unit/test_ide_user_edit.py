"""A file the user writes through `calu ide` is the user's, not Claude's.

Three places must agree on that: the session's request `files.user_edit`, the write sets
(`calu.write_sets`: the file is not part of a command's write set) and the code honesty state
(`calu.honesty`: the file is unreviewed, like any change made outside calu). The process
tests at the end cover `calu ide --cwd`.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest

from calu import write_sets
from calu.changes import SnapshotStore
from calu.config import Config, HonestyConfig, TracesConfig
from calu.credentials import CredentialStore
from calu.honesty import Baseline, HonestyState
from calu.protocol.client import RequestFailed
from calu.protocol.messages import REQUESTS, FilesUserEditParams
from calu.protocol.schema import protocol_schema
from calu.write_sets import WriteSets
from tests.unit.ide_helpers import bridged
from tests.unit.test_protocol_server import Events, client, served

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def own_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the token-free sockets at an empty directory, so no real session is found."""
    with tempfile.TemporaryDirectory(prefix="calu-ide-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        yield Path(directory)


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


# ---------------------------------------------------------------------- the write sets


def bare_write_sets(workspace: Path) -> WriteSets:
    """Return a `WriteSets` whose collaborators are never called by these tests.

    Args:
        workspace: The project directory.
    """

    async def unused(*_arguments: object, **_named: object) -> None:
        """Stand for a collaborator that must not be needed."""
        raise AssertionError("not expected in this test")

    return WriteSets(
        workspace,
        None,  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type,return-value]
        snapshot_root=lambda: workspace,
        changes=lambda: None,  # type: ignore[arg-type,return-value]
        emit=unused,
        store_call=unused,
        add_checker_run=unused,
        ask=unused,  # type: ignore[arg-type]
        run_checkers=unused,  # type: ignore[arg-type]
        check_context=lambda: None,  # type: ignore[arg-type,return-value]
    )


def test_a_reported_user_write_belongs_to_the_windows_open_then_or_soon_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now = [1000.0]
    monkeypatch.setattr(write_sets.time, "monotonic", lambda: now[0])
    sets = bare_write_sets(tmp_path)
    margin = write_sets.USER_EDIT_MARGIN_S
    sets.user_edited(["early.py"])
    now[0] += margin + 5
    sets.user_edited(["just-before.py"])
    now[0] += 1
    opened_at = now[0]  # a command's window opens here
    now[0] += 3
    sets.user_edited(["during.py", "during.py"])
    assert sets._user_edits_since(opened_at) == {"just-before.py", "during.py"}
    assert sets._user_edits_since(None) == frozenset(), "without a window nothing is known"
    # Reports no window can hold any more are forgotten at the next report.
    now[0] += 10 * margin
    sets.user_edited(["late.py"])
    assert set(sets._user_edits) == {"late.py"}
    # While a window is open, the reports it may hold are kept.
    sets._opened["call"] = now[0]
    now[0] += 10 * margin
    sets.user_edited(["much-later.py"])
    assert set(sets._user_edits) == {"late.py", "much-later.py"}


async def test_a_skipped_path_is_in_neither_the_write_set_nor_its_patch(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("one\n")
    snapshots = SnapshotStore(workspace=project, root=tmp_path / "snapshots")
    await snapshots.prepare()
    before = await snapshots.take()
    (project / "a.txt").write_text("the command\n")
    (project / "user.txt").write_text("the user\n")
    after = await snapshots.take()
    everything = await snapshots.changes(before, after)
    assert set(everything.paths) == {"a.txt", "user.txt"}
    without = await snapshots.changes(before, after, {"user.txt"})
    assert without.paths == ("a.txt",)
    assert "the command" in without.patch and "the user" not in without.patch
    # The undo of the command's write set leaves the user's file alone.
    assert await snapshots.revert(before, after, without.paths)
    assert (project / "a.txt").read_text() == "one\n"
    assert (project / "user.txt").read_text() == "the user\n"
    assert (await snapshots.changes(before, after, {"a.txt", "user.txt"})).paths == ()


# -------------------------------------------------------------------------- the request


async def test_the_session_takes_note_of_a_user_edit_and_names_its_own_directories(
    tmp_path: Path,
):
    assert REQUESTS["files.user_edit"] is FilesUserEditParams
    schema = protocol_schema()
    assert schema["requests"]["files.user_edit"] == {"$ref": "#/$defs/FilesUserEditParams"}
    assert schema["results"]["files.user_edit"] == {"$ref": "#/$defs/FilesUserEditResult"}
    config = Config(honesty=HonestyConfig(mode="track"), traces=TracesConfig(enabled=True))
    async with served(tmp_path, config=config) as run:
        events = Events()
        async with client(run, events, name="an editor") as editor:
            await editor.request("session.start", {})
            assert await editor.request("files.user_edit", {"paths": []}) == {
                "excluded": ["agent/traces"]
            }
            assert not events.of("log") or all(
                "the user wrote" not in e["message"] for e in events.of("log")
            )
            await editor.request("files.user_edit", {"paths": ["src/a.py", "b.py"]})
            assert set(run.session.write_sets._user_edits) == {"src/a.py", "b.py"}
            # The honesty state watches the files from the next look on.
            assert {"src/a.py", "b.py"} <= run.session._written
            with anyio.fail_after(5):
                while not [e for e in events.of("log") if "the user wrote" in e["message"]]:
                    await anyio.sleep(0.02)
            told = [e["message"] for e in events.of("log") if "the user wrote" in e["message"]]
            assert told == ["an editor: the user wrote src/a.py, b.py in the editor"]
            for bad in (["../x"], ["/etc/passwd"], ["a/../../b"], [""]):
                with pytest.raises(RequestFailed):
                    await editor.request("files.user_edit", {"paths": bad})
            with pytest.raises(RequestFailed):
                await editor.request("files.user_edit", {"paths": ["x"] * 1001})
    async with served(tmp_path) as run:
        async with client(run, Events()) as editor:
            # Without traces the session has no directory of its own in the project.
            assert await editor.request("files.user_edit", {"paths": ["a.py"]}) == {"excluded": []}


async def test_the_bridge_excludes_the_directory_the_attached_session_names(
    tmp_path: Path, store: CredentialStore
):
    project = tmp_path / "project"
    (project / "notes" / "traces").mkdir(parents=True)
    (project / "notes" / "traces" / "s.md").write_text("trace")
    (project / "a.py").write_text("a")
    config = Config(traces=TracesConfig(enabled=True, directory="notes/traces"))
    async with served(project, config=config) as run:
        async with bridged(project, store) as editor:
            before = await editor.request("ide.fs.manifest")
            assert [e["path"] for e in before["files"]] == ["a.py", "notes/traces/s.md"]
            await editor.request("ide.attach", {"address": run.address, "token": run.token})
            await editor.call("session.start")
            # The bridge asked at the attach; the session answers from its settings. If the
            # session read them only at its start, the next write asks again.
            await editor.request(
                "ide.fs.write",
                {
                    "path": "b.py",
                    "content_b64": base64.b64encode(b"b").decode(),
                    "base_sha256": None,
                },
            )
            after = await editor.request("ide.fs.manifest")
            assert [e["path"] for e in after["files"]] == ["a.py", "b.py"]
            with pytest.raises(Exception, match="does not synchronise"):
                await editor.request("ide.fs.read", {"path": "notes/traces/s.md"})
            await editor.request("ide.detach")
            again = await editor.request("ide.fs.manifest")
            assert "notes/traces/s.md" in [e["path"] for e in again["files"]]


# -------------------------------------------------------------------- the honesty state


async def test_a_file_the_user_wrote_through_the_bridge_is_unreviewed(
    tmp_path: Path, store: CredentialStore
):
    """The honesty state shows an editor's upload like any change made outside calu."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    (project / "src" / "parser.py").write_text("X = 1\n")
    (project / "README.md").write_text("# project\n")
    (project / ".gitignore").write_text("build/\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "first")
    state = HonestyState(project)
    state.baseline = Baseline(
        git(project, "rev-parse", "HEAD"), git(project, "rev-parse", "HEAD^{tree}"), "user"
    )
    assert state.snapshot().clean

    def upload(path: str, content: bytes, base: bytes | None) -> dict:
        """Build the params of an `ide.fs.write`.

        Args:
            path: The file.
            content: Its new content.
            base: Its present content, or None if it does not exist.
        """
        return {
            "path": path,
            "content_b64": base64.b64encode(content).decode(),
            "base_sha256": None if base is None else hashlib.sha256(base).hexdigest(),
        }

    async with bridged(project, store) as editor:
        await editor.request("ide.fs.write", upload("src/parser.py", b"X = 2\n", b"X = 1\n"))
        await editor.request("ide.fs.write", upload("src/new.py", b"Y = 1\n", None))
        await editor.request("ide.fs.write", upload("build/out.py", b"Z = 1\n", None))
        snapshot = await anyio.to_thread.run_sync(state.snapshot)
        assert snapshot.unreviewed.paths == ("src/new.py", "src/parser.py")
        assert not snapshot.clean and not state.is_reviewed("src/parser.py")
        # The session watches what the bridge reported (`files.user_edit`), so a file git
        # ignores counts as well.
        state.watch_all(["src/parser.py", "src/new.py", "build/out.py"])
        watched = await anyio.to_thread.run_sync(state.snapshot)
        assert watched.unreviewed.paths == ("build/out.py", "src/new.py", "src/parser.py")
        # Putting the baseline's content back through the bridge is reviewed content again.
        await editor.request("ide.fs.write", upload("src/parser.py", b"X = 1\n", b"X = 2\n"))
        assert state.is_reviewed("src/parser.py")


# --------------------------------------------------------------------- calu ide --cwd


def bridge_environment(tmp_path: Path, runtime: Path) -> dict[str, str]:
    """Return the tester's environment with calu's own directories moved into the test's.

    calu's configuration, state and runtime directories point below `tmp_path` and
    `runtime`, so `calu ide` finds no credential, log or session of the tester's. `HOME`,
    `PATH` and the checker keys that `tests/conftest.py` loaded stay, so git still reads the
    tester's global configuration.

    Args:
        tmp_path: The test's temporary directory.
        runtime: The runtime directory of this test.
    """
    return {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(runtime),
    }


async def test_calu_ide_refuses_a_working_directory_that_does_not_exist(
    tmp_path: Path, own_runtime_directory: Path
):
    with anyio.fail_after(60):
        done = await anyio.run_process(
            [sys.executable, "-m", "calu", "ide", "--cwd", str(tmp_path / "missing")],
            cwd=tmp_path,
            env=bridge_environment(tmp_path, own_runtime_directory),
            check=False,
            input=b"",
        )
    assert done.returncode == 2
    assert done.stdout == b""
    lines = done.stderr.decode().splitlines()
    assert len(lines) == 1 and "--cwd" in lines[0] and "missing" in lines[0]


async def test_calu_ide_works_in_the_directory_it_is_given(
    tmp_path: Path, own_runtime_directory: Path
):
    project = tmp_path / "remote project"
    project.mkdir()
    (project / "a.txt").write_bytes(b"hello")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "ide.initialize", "params": {"client": "test"}},
        {"jsonrpc": "2.0", "id": 2, "method": "ide.paths", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "ide.fs.read", "params": {"path": "a.txt"}},
        {"jsonrpc": "2.0", "id": 4, "method": "ide.shutdown", "params": {}},
    ]
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    with anyio.fail_after(60):
        async with await anyio.open_process(
            [sys.executable, "-m", "calu", "ide", "--cwd", str(project)],
            cwd=elsewhere,
            env=bridge_environment(tmp_path, own_runtime_directory),
        ) as process:
            assert process.stdin is not None and process.stdout is not None
            for request in requests:
                await process.stdin.send((json.dumps(request) + "\n").encode())
            # stdin stays open, as an SSH connection does: the process ends when asked.
            written = b""
            async for chunk in process.stdout:
                written += chunk
            assert await process.wait() == 0
    answers = [json.loads(line) for line in written.decode().splitlines()]
    by_id = {answer["id"]: answer for answer in answers}
    assert by_id[1]["result"]["workspace"] == str(project.resolve())
    assert "cwd" in by_id[1]["result"]["capabilities"]
    assert by_id[2]["result"]["root"] == str(project.resolve())
    assert base64.b64decode(by_id[3]["result"]["content_b64"]) == b"hello"
