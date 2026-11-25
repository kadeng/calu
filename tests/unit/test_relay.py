"""The relay of `calu serve` with a scripted session behind it (agent/records/PLAN.md D242).

No database: the server's token check is a dictionary the tests change, and the server's
sessions are the `served` session of the protocol tests.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from calu.protocol.client import (
    RelayTarget,
    RequestFailed,
    connect,
    relay_base,
    relay_call,
    relay_session_id,
)
from calu.protocol.messages import CLOSE_SERVER_TOKEN_REVOKED, Log, RelayServer, RelayStartParams
from calu.relay import Hosted, Relay, StartRefused
from calu.tokens import token_hash
from tests.unit.test_protocol_server import Events, Served, served

pytestmark = pytest.mark.anyio

SESSION_ID = "5e551011-0000-4000-8000-000000000001"
TOKEN = "the-server-token"


@dataclass
class FakeServer:
    """The relay's view of `calu serve`: its sessions and its answer to `relay.start`."""

    running: list[Hosted] = field(default_factory=list)
    started: list[RelayStartParams] = field(default_factory=list)
    refuse: str = ""
    details: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    """What the database would say about the sessions' names and state times."""
    asked: list[str | None] = field(default_factory=list)
    """The token hashes `session_details` was called with."""

    def hosted(self) -> list[Hosted]:
        """Return the sessions."""
        return list(self.running)

    async def session_details(
        self, sha: str | None, session_ids: list[str]
    ) -> dict[str, tuple[str, datetime]]:
        """Return the scripted names and state times, only for a client with a token."""
        self.asked.append(sha)
        if sha is None:
            return {}
        return {key: value for key, value in self.details.items() if key in session_ids}

    async def relay_start(self, sha: str, label: str, params: RelayStartParams) -> str:
        """Record the request and name the first session, or refuse."""
        assert sha == token_hash(TOKEN) and label == "laptop"
        if self.refuse:
            raise StartRefused(self.refuse)
        self.started.append(params)
        return self.running[0].calu_session_id


@dataclass
class Relayed:
    """A relay in front of one scripted session."""

    relay: Relay
    run: Served
    server: FakeServer
    valid: dict[str, str]

    @property
    def target(self) -> RelayTarget:
        """The relay as a client names it, with the valid token."""
        return RelayTarget(self.relay.address, TOKEN)


@asynccontextmanager
async def relayed(tmp_path: Path, insecure: bool = False) -> AsyncGenerator[Relayed]:
    """Run a scripted session and a relay to it on free ports."""
    valid = {token_hash(TOKEN): "laptop"}

    async def check(sha: str) -> str | None:
        return valid.get(sha)

    async with served(tmp_path) as run:
        fake = FakeServer(
            [Hosted(SESSION_ID, run.address, run.token, str(tmp_path), datetime.now(UTC))]
        )
        relay = Relay(
            fake,
            check,
            RelayServer(
                server_id="srv", hostname="box", os_user="dev", projects_directory="/srv/p"
            ),
            insecure=insecure,
            revalidate_s=0.2,
        )
        await relay.open("127.0.0.1", 0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(relay.run)
            try:
                yield Relayed(relay, run, fake, valid)
            finally:
                tg.cancel_scope.cancel()


async def test_the_relay_lists_the_servers_sessions(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        hello, listed = await relay_call(it.relay.address, TOKEN, "relay.sessions")
        assert hello["relay"] == {
            "server_id": "srv",
            "hostname": "box",
            "os_user": "dev",
            "projects_directory": "/srv/p",
        }
        [one] = listed["sessions"]
        assert one["calu_session_id"] == SESSION_ID
        assert one["directory"] == str(tmp_path)
        assert one["state"] in {"idle", "starting", "stopped"}


async def test_a_wrong_or_missing_server_token_gets_nowhere(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        for token in ("wrong", ""):
            with pytest.raises(RequestFailed) as refused:
                await relay_call(it.relay.address, token, "relay.sessions")
            assert refused.value.code == -32001
            with pytest.raises(RequestFailed):
                async with connect(it.target.session_address(SESSION_ID), token, "t", Events()):
                    pass
        # The session's own token is not a server token either.
        with pytest.raises(RequestFailed):
            await relay_call(it.relay.address, it.run.token, "relay.sessions")


async def test_a_client_attaches_to_a_session_through_the_relay(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        events = Events()
        address = it.target.session_address(SESSION_ID)
        async with connect(address, TOKEN, "calu attach", events) as client:
            assert client.initialize["workspace"] == str(tmp_path)
            state = await client.request("state.get")
            assert state["workspace"] == str(tmp_path)
            await it.run.server.bus.publish(Log(level="info", message="through the relay"))
            logs = await events.wait_for("log")
            assert any(params["message"] == "through the relay" for params in logs)
            # The session sees the relay's connection under the client's name and the label
            # of its server token, authenticated with the session's own token.
            names = {c.name for c in it.run.server._clients}
            assert "calu attach via relay (laptop)" in names


async def test_a_session_that_does_not_run_is_refused(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        with pytest.raises(RequestFailed) as refused:
            async with connect(it.target.session_address("0000dead"), TOKEN, "t", Events()):
                pass
        assert "no session 0000dead runs on this server" in str(refused.value)


async def test_a_revoked_server_token_closes_the_connection(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        events = Events()
        async with connect(it.target.session_address(SESSION_ID), TOKEN, "t", events) as client:
            await client.request("state.get")
            it.valid.clear()
            with pytest.raises((RequestFailed, ConnectionError)):
                await client.request("chat.send", {"text": "after the revocation"})
            with anyio.fail_after(5):
                await client.closed.wait()
            assert client.close_code == CLOSE_SERVER_TOKEN_REVOKED
        # Revoked stays revoked, also while the database would say nothing.
        with pytest.raises(RequestFailed):
            await relay_call(it.relay.address, TOKEN, "relay.sessions")


async def test_the_sweep_closes_an_idle_connection_of_a_revoked_token(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        async with connect(it.target.session_address(SESSION_ID), TOKEN, "t", Events()) as client:
            it.valid.clear()
            with anyio.fail_after(5):
                await client.closed.wait()
            assert client.close_code == CLOSE_SERVER_TOKEN_REVOKED


async def test_the_session_closing_the_connection_reaches_the_client(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        async with connect(it.target.session_address(SESSION_ID), TOKEN, "t", Events()) as client:
            await client.request("shutdown")
            with anyio.fail_after(5):
                await client.closed.wait()
            assert client.close_code not in (None, 1006)


async def test_relay_start_asks_the_server_and_names_the_session(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        params = {"kind": "fork", "directory": "/srv/project", "claude_session_id": "abc"}
        _hello, result = await relay_call(it.relay.address, TOKEN, "relay.start", params)
        assert result == {"calu_session_id": SESSION_ID}
        assert it.server.started[0].directory == "/srv/project"
        it.server.refuse = "/nowhere is not a directory on box"
        with pytest.raises(RequestFailed) as refused:
            await relay_call(it.relay.address, TOKEN, "relay.start", {"directory": "/nowhere"})
        assert "/nowhere is not a directory on box" in str(refused.value)


async def test_the_relay_refuses_a_session_outside_the_projects_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D243: `relay.start` of a real `calu serve` for a directory it does not serve."""
    from calu import servers
    from calu.serve import ServeOptions, Server

    projects = (tmp_path / "projects").resolve()
    projects.mkdir()
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        options=ServeOptions(projects=projects),
        hostname="box",
        user="dev",
    )
    server._db = object()  # type: ignore[assignment]
    server.server_id = "srv"
    filed: list[str] = []

    async def file_request(*_args: object) -> int:
        filed.append("filed")
        return 1

    monkeypatch.setattr(servers, "file_request", file_request)

    async def check(sha: str) -> str | None:
        return "laptop" if sha == token_hash(TOKEN) else None

    relay = Relay(server, check, server._relay_server())
    await relay.open("127.0.0.1", 0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(relay.run)
        hello, _ = await relay_call(relay.address, TOKEN, "relay.sessions")
        assert hello["relay"]["projects_directory"] == str(projects)
        for outside, why in (
            (str(tmp_path), "the projects directory this `calu serve` on box serves"),
            ("/etc", "calu serve --projects"),
            (str(projects / ".ssh"), "hidden directory .ssh"),
        ):
            with pytest.raises(RequestFailed) as refused:
                await relay_call(relay.address, TOKEN, "relay.start", {"directory": outside})
            assert refused.value.code == -32000
            assert why in str(refused.value)
        tg.cancel_scope.cancel()
    assert filed == []


async def test_insecure_lets_loopback_attach_without_a_token_but_not_ask_for_sessions(
    tmp_path: Path,
) -> None:
    async with relayed(tmp_path, insecure=True) as it:
        async with connect(it.target.session_address(SESSION_ID), "", "t", Events()) as client:
            await client.request("state.get")
        with pytest.raises(RequestFailed) as refused:
            await relay_call(it.relay.address, "", "relay.start", {"directory": str(tmp_path)})
        assert "needs a server token" in str(refused.value)
        assert it.server.started == []
        # Listing works without a token, but the database is not asked about the names.
        it.server.details[SESSION_ID] = ("secret plans", datetime.now(UTC))
        _, listed = await relay_call(it.relay.address, "", "relay.sessions")
        assert listed["sessions"][0]["name"] == "" and it.server.asked[-1] is None


async def test_insecure_still_takes_a_valid_server_token_for_asking_for_sessions(
    tmp_path: Path,
) -> None:
    """A loopback client with a server token may ask an insecure relay for a session.

    Seen in the desktop (test plan K): `calu attach --token` through the relay of `calu serve
    --insecure` was refused `/new` as if it had no token.
    """
    async with relayed(tmp_path, insecure=True) as it:
        params = {"directory": str(tmp_path)}
        _hello, result = await relay_call(it.relay.address, TOKEN, "relay.start", params)
        assert result["calu_session_id"] == SESSION_ID
        assert len(it.server.started) == 1
        # A wrong token on loopback attaches as without one, and still asks for nothing.
        _, listed = await relay_call(it.relay.address, "wrong", "relay.sessions")
        assert listed["sessions"]
        with pytest.raises(RequestFailed) as refused:
            await relay_call(it.relay.address, "wrong", "relay.start", params)
        assert "needs a server token" in str(refused.value)
        assert len(it.server.started) == 1


async def test_an_unknown_path_is_closed(tmp_path: Path) -> None:
    async with relayed(tmp_path) as it:
        with pytest.raises((RequestFailed, ConnectionError, OSError)):
            async with connect(it.relay.address + "/other", TOKEN, "t", Events()):
                pass


def test_relay_addresses() -> None:
    assert relay_base("ws://127.0.0.1:8811/session/abc") == "ws://127.0.0.1:8811"
    assert relay_base("ws://127.0.0.1:8811/") == "ws://127.0.0.1:8811"
    assert relay_session_id("ws://127.0.0.1:8811/session/abc") == "abc"
    assert relay_session_id("ws://127.0.0.1:8811") is None
    target = RelayTarget("ws://127.0.0.1:8811/", "t")
    assert target.session_address("abc") == "ws://127.0.0.1:8811/session/abc"


async def test_the_full_screen_clients_relay_access_lists_and_starts(tmp_path: Path) -> None:
    """`/running`, `/switch`, `/new` and `/fork` of the full-screen client through the relay."""
    from calu.session_requests import SessionRequest
    from calu.tui.adminaccess import AdminFailed, RelayAdmin
    from calu.tui.app import switch_refusal

    async with relayed(tmp_path) as it:
        admin = RelayAdmin(RelayTarget(it.relay.address, TOKEN, "box", "dev", "/srv"), "")
        assert admin.sessions_problem() is None and admin.problem() is not None
        assert not admin.needs_password() and admin.session_host() == "box"
        [row] = await admin.sessions(None)
        assert row.id == SESSION_ID and row.hostname == "box"
        # Without the database's answer: no name, and the state counts from the start.
        assert row.name == "" and row.state_since == row.started_at
        since = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        it.server.details[SESSION_ID] = ("payment refactoring", since)
        [row] = await admin.sessions(None)
        assert row.name == "payment refactoring" and row.state_since == since
        assert it.server.asked[-1] == token_hash(TOKEN)
        [served] = await admin.served(None)
        assert served.line() == "calu serve of dev@box serves /srv"
        assert row.address.endswith(f"/session/{SESSION_ID}")
        # Through the relay the server's host counts, not this machine's.
        assert switch_refusal(row, None, admin.session_host()) == ""
        wanted = SessionRequest(hostname="here", os_user="me", directory="/srv/p", kind="new")
        assert await admin.request_session(wanted, None) == SESSION_ID
        assert it.server.started[-1].directory == "/srv/p"
        it.server.refuse = "this server already runs 10 sessions"
        with pytest.raises(AdminFailed, match="already runs 10 sessions"):
            await admin.request_session(wanted, None)
        with pytest.raises(AdminFailed):
            await admin.save_instructions("command", "text", None)
        bad = RelayAdmin(RelayTarget(it.relay.address, "wrong", "box", "dev"), "")
        with pytest.raises(AdminFailed, match="invalid server token"):
            await bad.sessions(None)


def _listing(*ids: str) -> dict:
    """A `relay.sessions` result with one idle session per id."""
    return {
        "sessions": [
            {"calu_session_id": one, "directory": f"/srv/{one}", "state": "idle"} for one in ids
        ]
    }


def test_calu_attach_on_a_relay_picks_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from calu import cli

    answers = {"listed": _listing("aaaa1111", "bbbb2222")}

    async def relay_call(address, token, method, params=None, timeout_s=15.0):
        assert method == "relay.sessions"
        return {}, answers["listed"]

    monkeypatch.setattr(cli, "relay_call", relay_call)
    hello = {
        "relay": {
            "server_id": "s",
            "hostname": "box",
            "os_user": "dev",
            "projects_directory": "/srv/p",
        }
    }
    tried = cli.Endpoint("ws://127.0.0.1:8811", "tok")
    chosen = cli._through_relay(tried, hello, "bbbb")
    assert chosen.address == "ws://127.0.0.1:8811/session/bbbb2222"
    assert chosen.relay is not None and chosen.relay.hostname == "box"
    assert chosen.token == "tok" and chosen.session_id == "bbbb2222"
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(cli.CliError, match="name one with --session"):
        cli._through_relay(tried, hello, None)
    with pytest.raises(
        cli.CliError, match=r"no session of dev@box \(projects directory /srv/p\) start with 'cccc'"
    ):
        cli._through_relay(tried, hello, "cccc")
    answers["listed"] = _listing("aaaa1111")
    assert cli._through_relay(tried, hello, None).session_id == "aaaa1111"
    answers["listed"] = _listing()
    with pytest.raises(cli.CliError, match=r"--relay ws://127\.0\.0\.1:8811") as none:
        cli._through_relay(tried, hello, None)
    # D243: the server's projects directory is named.
    assert "(projects directory /srv/p)" in str(none.value)
    assert "calu start <directory in /srv/p>" in str(none.value)
    # After the client: the command that attaches to the same session again.
    assert cli._again(chosen, True) == (
        "CALU_TOKEN=<the same token> calu attach ws://127.0.0.1:8811 --session bbbb2222"
    )
    following = cli._following(None, chosen, "aaaa1111")  # type: ignore[arg-type]
    assert following.address == "ws://127.0.0.1:8811/session/aaaa1111"
    assert following.relay == chosen.relay


def test_calu_serve_takes_the_relay_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from calu import cli

    parser = cli.build_parser()
    assert parser.parse_args(["serve"]).relay_port is None
    assert parser.parse_args(["serve", "--relay-port", "0"]).relay_port == 0
    assert parser.parse_args(["serve", "--no-relay"]).no_relay
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--no-relay", "--relay-port", "9"])
    args = parser.parse_args(["token", "mint", "--server", "--label", "laptop"])
    assert args.server and args.label == "laptop"


async def test_the_line_client_says_the_relay_closed_it_for_a_revoked_server_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4003 comes from the relay, never from a session: the client names the relay and why."""
    import calu.attach as attach_module
    from calu.attach import attach

    async def read_lines(send: object) -> None:
        await anyio.sleep_forever()

    monkeypatch.setattr(attach_module, "read_lines", read_lines)
    async with relayed(tmp_path) as it:
        codes: list[int] = []
        async with anyio.create_task_group() as tg:

            async def attached() -> None:
                codes.append(await attach(it.target.session_address(SESSION_ID), TOKEN))

            tg.start_soon(attached)
            with anyio.fail_after(10):
                while "[attached]" not in capsys.readouterr().out:
                    await anyio.sleep(0.02)
            it.valid.clear()
        assert codes == [0]
    out = capsys.readouterr().out
    assert (
        "[detached] the relay of the `calu serve` closed the connection: the server token was "
        "revoked, or its server ended"
    ) in out
    assert "the session closed" not in out


async def test_a_browser_cannot_open_the_relay_or_a_session(tmp_path: Path) -> None:
    """D279: a web page's WebSocket carries an `Origin`; with `--insecure` it would otherwise
    act as a loopback client of every session. Calu's own clients send no `Origin`."""
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import InvalidStatus

    async with relayed(tmp_path, insecure=True) as it:
        for address in (it.relay.address, it.run.address):
            with pytest.raises(InvalidStatus) as refused:
                async with ws_connect(address, origin="https://evil.example"):  # type: ignore[arg-type]
                    pass
            assert refused.value.response.status_code == 403
            async with ws_connect(address):
                pass  # no Origin: accepted
