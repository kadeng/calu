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


def test_an_existing_outer_directory_others_may_write_to_is_refused(short: Path):
    """An existing `/run/calu` is checked too, not only one that `bind` creates."""
    outer = short / "calu"
    outer.mkdir(mode=0o700)
    outer.chmod(0o777)
    path = localsocket.path_of("s", outer / "s")
    with pytest.raises(localsocket.NotTrusted, match="writable by its group or by others"):
        localsocket.bind(path, owner=ME)
    assert not path.exists(), "nothing is bound in a directory somebody else can change"
    outer.chmod(0o755)
    listener = localsocket.bind(path, owner=ME)
    try:
        assert localsocket.problems(path, ME) == []
        outer.chmod(0o775)
        assert any(str(outer) in problem for problem in localsocket.problems(path, ME))
        with pytest.raises(localsocket.NotTrusted):
            localsocket.connected(path, ME)
    finally:
        outer.chmod(0o700)
        listener.close()
    # Somebody else's outer directory: the real check asks for root.
    with pytest.raises(localsocket.NotTrusted, match=f"{outer} belongs to uid {ME}"):
        localsocket.bind(path)


def test_only_sockets_that_are_the_owners_alone_are_listed(short: Path):
    root = short / "calu" / "s"
    good = localsocket.bind(localsocket.path_of("good", root), owner=ME)
    open_one = localsocket.bind(localsocket.path_of("open", root), owner=ME)
    try:
        localsocket.path_of("open", root).chmod(0o660)
        (root / "plain.sock").write_text("")
        assert localsocket.listed(root, ME) == [localsocket.path_of("good", root)]
        assert localsocket.listed(short / "missing", ME) == []
    finally:
        good.close()
        open_one.close()


@pytest.mark.anyio
async def test_a_root_client_finds_its_session_through_the_sockets_alone(
    tmp_path: Path, short: Path, monkeypatch
):
    """D226: `calu attach --sudo` in the project needs neither the database nor a password."""
    from calu import cli

    workspace = tmp_path / "work"
    (workspace / "sub").mkdir(parents=True)
    root = short / "run" / "sessions"
    monkeypatch.setattr(localsocket, "ROOT", root)
    path = localsocket.path_of("11111111-2222-4333-8444-555555555555", root)
    really_connected, really_listed = localsocket.connected, localsocket.listed
    monkeypatch.setattr(localsocket, "connected", lambda found: really_connected(found, ME))
    monkeypatch.setattr(localsocket, "listed", lambda: really_listed(root, ME))
    monkeypatch.setattr(localsocket, "peer_uid", lambda connection: 0)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    async with served(workspace) as run, anyio.create_task_group() as tg:
        tg.start_soon(run.server.listen_local, localsocket.bind(path, owner=ME), path)
        monkeypatch.chdir(workspace / "sub")
        found = await anyio.to_thread.run_sync(cli._local_socket_endpoint, None)
        assert found is not None
        assert (found.address, found.token, found.token_id) == (f"unix:{path}", "", None)
        assert await anyio.to_thread.run_sync(cli._local_socket_endpoint, "nope") is None
        monkeypatch.chdir(tmp_path)
        assert await anyio.to_thread.run_sync(cli._local_socket_endpoint, None) is None
        tg.cancel_scope.cancel()


def test_a_root_client_found_by_socket_keeps_only_a_login_without_password(
    tmp_path: Path, monkeypatch
):
    """D226: nothing is asked; a password-free entry of root's store is kept, a protected one
    is not touched."""
    from calu import cli
    from calu.credentials import CredentialStore, KdfParams
    from calu.setup import ADMIN_ROLE

    store = CredentialStore(
        tmp_path / "store.json", KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    )
    store.save("admin", "postgresql://calu_owner:x@db.example:5432/calu", "secret")
    monkeypatch.setattr(cli, "_hold", [True])
    monkeypatch.setattr(cli, "_held", [])
    monkeypatch.setattr(cli, "_admin_place", [])
    cli._hold_password_free(store)
    assert cli._held == [] and cli._admin_place == []

    store.save("operator", "postgresql://calu_operator:y@db.example:5432/calu", None)
    cli._hold_password_free(store)
    assert [target.user for target in cli._held] == ["calu_operator"]
    assert [target.user for target in cli._admin_place] == [ADMIN_ROLE]


def test_loopback_addresses_are_told_from_others():
    from calu.protocol.server import _loopback

    assert _loopback(("127.0.0.1", 5000)) and _loopback(("127.3.4.5", 1))
    assert _loopback(("::1", 5000, 0, 0)) and _loopback(("::ffff:127.0.0.1", 5000, 0, 0))
    assert not _loopback(("192.168.1.2", 5000)) and not _loopback(("::ffff:10.0.0.1", 1, 0, 0))
    assert not _loopback(None) and not _loopback(("not an address", 1))


@pytest.mark.anyio
async def test_an_insecure_session_lets_local_clients_in_without_a_token(
    tmp_path: Path, short: Path, monkeypatch
):
    """D227: with `--insecure`, a loopback TCP client and a client on the session's own socket
    need no token; without it, both are refused."""
    from calu import cli

    workspace = tmp_path / "work"
    workspace.mkdir()
    async with served(workspace) as run:
        with pytest.raises(RequestFailed):
            async with connect(run.server.address, "", "test", Events()):
                pass
    (short / "runtime").mkdir()  # $XDG_RUNTIME_DIR exists; calu adds the two below it
    root = short / "runtime" / "calu" / "sessions"
    monkeypatch.setattr(localsocket, "user_root", lambda *args, **kwargs: root)
    path = localsocket.path_of("11111111-2222-4333-8444-555555555555", root)
    async with served(workspace, insecure=True) as run, anyio.create_task_group() as tg:
        async with connect(run.server.address, "", "tcp-client", Events()) as client:
            assert (await client.request("state.get"))["workspace"] == str(workspace)
        tg.start_soon(run.server.listen_local, localsocket.bind(path, owner=ME), path)
        # An ordinary user's `calu attach` finds the session through its socket alone.
        monkeypatch.chdir(workspace)
        found = await anyio.to_thread.run_sync(cli._local_socket_endpoint, None)
        assert found is not None and (found.address, found.token) == (f"unix:{path}", "")
        async with connect(found.address, "", "unix-client", Events()) as client:
            assert (await client.request("state.get"))["workspace"] == str(workspace)
        tg.cancel_scope.cancel()


def test_secure_and_insecure_together_are_refused(monkeypatch):
    from calu import cli

    for command in (["run", "."], ["serve"]):
        args = cli.build_parser().parse_args([*command, "--secure", "--insecure"])
        with pytest.raises(cli.CliError, match="contradict"):
            cli._check_security_flags(args)


def test_the_insecure_socket_directory_of_a_user(tmp_path: Path):
    """D227: a root session and that user's `calu attach` agree on the directory."""
    assert (
        localsocket.user_root({"XDG_RUNTIME_DIR": str(tmp_path)}) == tmp_path / "calu" / "sessions"
    )
    # A uid without /run/user/<uid>: the temporary directory, named by the uid.
    assert localsocket.user_root(uid=987654321) == Path(
        tempfile.gettempdir(), "calu-987654321", "sessions"
    )
    mine = Path("/run/user") / str(os.geteuid())
    if mine.is_dir():
        assert localsocket.user_root({}) == mine / "calu" / "sessions"
        assert localsocket.user_root(uid=os.geteuid()) == mine / "calu" / "sessions"


def test_the_kernel_names_the_user_that_listens_on_a_socket(short: Path):
    """D228: a client tells a session of root's calu from one of its own user by this."""
    from calu import cli
    from calu.tui.adminaccess import StoreAdmin

    path = localsocket.path_of("s", short / "calu" / "s")
    listener = localsocket.bind(path, owner=ME)
    try:
        assert cli._server_uid(path) == ME
    finally:
        listener.close()
    assert cli._server_uid(short / "nothing.sock") is None
    # Without a known database the client says why, and reads no store of this user.
    assert StoreAdmin(unavailable="no database known").problem() == "no database known"
