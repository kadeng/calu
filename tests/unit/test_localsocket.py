"""The session's root-owned socket (`calu.localsocket`, D215), as far as it runs without root.

The tests create sockets in a short temporary directory (a Unix socket path is limited to
about 100 characters) owned by the user they run as, and pass that uid where the real code
expects root; they print nothing.
"""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import anyio
import pytest

from calu import asuser, localsocket
from calu.protocol.client import RequestFailed, connect
from tests.unit.test_protocol_server import Events, served

ME = os.getuid()


@pytest.fixture
def short() -> Generator[Path]:
    """A temporary directory with a short path, removed afterwards.

    Yields:
        The directory.
    """
    with tempfile.TemporaryDirectory(prefix="calu-ls-") as name:
        yield Path(name)


def test_the_socket_and_its_directory_are_private(short: Path):
    path = localsocket.path_of("11111111-2222-4333-8444-555555555555", short / "calu" / "s")
    listener = localsocket.bind(path, owner=ME)
    try:
        assert path.is_socket() and path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert localsocket.problems(path, ME) == []
        # A socket left by an earlier process is replaced, not an error.
        listener.close()
        listener = localsocket.bind(path, owner=ME)
        peer = localsocket.connected(path, ME)
        accepted, _ = listener.accept()
        assert localsocket.peer_uid(accepted) == ME
        peer.close()
        accepted.close()
    finally:
        listener.close()


def test_what_is_not_the_owners_alone_is_refused(short: Path):
    path = localsocket.path_of("s", short / "calu" / "s")
    listener = localsocket.bind(path, owner=ME)
    try:
        # Somebody else's: the real check asks for root, and this one belongs to the tests.
        assert any("not to uid 0" in problem for problem in localsocket.problems(path))
        with pytest.raises(localsocket.NotTrusted):
            localsocket.connected(path)
        path.chmod(0o660)
        assert any("open to its group" in p for p in localsocket.problems(path, ME))
        path.chmod(0o600)
        path.parent.chmod(0o755)
        assert any(str(path.parent) in p for p in localsocket.problems(path, ME))
        with pytest.raises(localsocket.NotTrusted):
            localsocket.connected(path, ME)
    finally:
        listener.close()
    plain = short / "calu" / "s" / "file.sock"
    plain.write_text("")
    assert any("is no socket" in problem for problem in localsocket.problems(plain, ME))
    link = short / "calu" / "s" / "link.sock"
    link.symlink_to(path)
    assert any("is no socket" in problem for problem in localsocket.problems(link, ME))
    assert localsocket.problems(short / "nothing.sock", ME) != []


def test_before_a_switch_root_access_is_a_plain_call():
    assert asuser.active() is None
    assert asuser.as_root(lambda a, b: a + b, 1, 2) == 3


@pytest.mark.anyio
async def test_a_root_peer_on_the_local_socket_needs_no_token(
    tmp_path: Path, short: Path, monkeypatch
):
    """The session trusts the kernel's word about the peer, and only that."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    root = short / "run" / "sessions"
    monkeypatch.setattr(localsocket, "ROOT", root)
    path = localsocket.path_of("session", root)
    really = localsocket.connected
    # The client asks for root's socket; here the tests' own uid stands for root.
    monkeypatch.setattr(localsocket, "connected", lambda found: really(found, ME))
    async with served(workspace) as run, anyio.create_task_group() as tg:
        tg.start_soon(run.server.listen_local, localsocket.bind(path, owner=ME), path)
        # The peer is not root (the tests are not): the socket alone opens nothing.
        with pytest.raises(RequestFailed) as refused:
            async with connect(f"unix:{path}", "", "test", Events()):
                pass
        assert refused.value.code == -32001
        # ... but its token still works there, as on the TCP port.
        async with connect(f"unix:{path}", run.token, "test", Events()) as client:
            assert (await client.request("state.get"))["workspace"] == str(workspace)
        # A peer the kernel reports as root is in without a token.
        monkeypatch.setattr(localsocket, "peer_uid", lambda connection: 0)
        async with connect(f"unix:{path}", "", "root-client", Events()) as client:
            assert (await client.request("state.get"))["workspace"] == str(workspace)
        tg.cancel_scope.cancel()
    assert not path.exists(), "the socket is removed with the session"
