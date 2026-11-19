"""`calu serve` without a database: a session whose start cannot be reported is stopped.

`run_from` and `session_requests.finish` are replaced by fakes; no database, no Claude Code.
"""

from pathlib import Path
from typing import Any

import anyio
import psycopg
import pytest

from calu import serve, session_requests
from calu.serve import ServeOptions, Server
from calu.session_requests import SessionRequest

pytestmark = pytest.mark.anyio


class Recorder:
    """Records the outcomes `_host` writes, and answers "started" as told.

    `outcomes` lists (request id, outcome, session id, message) per call.
    """

    def __init__(self, started: bool | Exception) -> None:
        """Prepare the fake.

        Args:
            started: What writing "started" does: True or False is returned, an exception
                is raised.
        """
        self.started = started
        self.outcomes: list[tuple[int, str, str | None, str]] = []

    async def finish(
        self, _db: Any, request_id: int, outcome: str, session_id: str | None, message: str = ""
    ) -> bool:
        """Stand in for `session_requests.finish`.

        Args:
            _db: Ignored.
            request_id: The request.
            outcome: "started" or "failed".
            session_id: The session's row id, or None.
            message: The reason of a failure.

        Returns:
            For "started" the configured answer; True for "failed".

        Raises:
            Exception: The configured exception, for "started".
        """
        self.outcomes.append((request_id, outcome, session_id, message))
        if outcome != "started":
            return True
        if isinstance(self.started, Exception):
            raise self.started
        return self.started


def _server_and_request(tmp_path: Path) -> tuple[Server, SessionRequest]:
    """Build a server with dummy connections and a request for an existing directory.

    Args:
        tmp_path: The project directory.

    Returns:
        The server and the claimed request, already listed in `server.sessions`.
    """
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        options=ServeOptions(projects=tmp_path.resolve()),
        hostname="h",
        user="u",
    )
    request = SessionRequest(
        hostname="h", os_user="u", directory=str(tmp_path), id=7, state="claimed"
    )
    server.sessions[request.id] = request
    return server, request


def _fake_run_from(session_id: str | None, stopped: list[bool]):
    """Make a `run_from` that reports ready and then runs until it is cancelled.

    Args:
        session_id: What it passes to `on_ready`.
        stopped: Gets True appended when the session is cancelled.

    Returns:
        The fake.
    """

    async def run_from(*_args: Any, on_ready: Any, **_kwargs: Any) -> None:
        """Report ready, then stay like a session with `stay=True`.

        Args:
            *_args: Ignored.
            on_ready: The server's callback.
            **_kwargs: Ignored.
        """
        await on_ready(session_id, "127.0.0.1:1")
        try:
            await anyio.sleep_forever()
        finally:
            stopped.append(True)

    return run_from


@pytest.mark.parametrize(
    ("session_id", "started", "reason"),
    [
        (None, True, "the session's row could not be written"),
        ("s-1", False, "the start of session s-1 could not be recorded"),
        ("s-1", psycopg.OperationalError("gone"), "the start of session s-1 could not be"),
        ("s-1", TimeoutError(), "the start of session s-1 could not be recorded"),
    ],
)
async def test_a_session_whose_start_is_not_recorded_is_stopped_and_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str | None,
    started: bool | Exception,
    reason: str,
):
    server, request = _server_and_request(tmp_path)
    recorder = Recorder(started)
    stopped: list[bool] = []
    monkeypatch.setattr(session_requests, "finish", recorder.finish)
    monkeypatch.setattr(serve, "run_from", _fake_run_from(session_id, stopped))
    with anyio.fail_after(5):
        await server._host(None, request)  # type: ignore[arg-type]
    assert stopped == [True]
    failed = [outcome for outcome in recorder.outcomes if outcome[1] == "failed"]
    assert len(failed) == 1
    assert failed[0][0] == request.id
    assert failed[0][3].startswith(reason)
    assert request.id not in server.sessions


async def test_a_session_whose_start_is_recorded_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    server, request = _server_and_request(tmp_path)
    recorder = Recorder(True)
    stopped: list[bool] = []
    monkeypatch.setattr(session_requests, "finish", recorder.finish)
    monkeypatch.setattr(serve, "run_from", _fake_run_from("s-1", stopped))
    with anyio.move_on_after(0.3) as scope:
        await server._host(None, request)  # type: ignore[arg-type]
    # Only the test's own timeout ends the session; no "failed" is written.
    assert scope.cancelled_caught
    assert stopped == [True]
    assert recorder.outcomes == [(request.id, "started", "s-1", "")]


def test_every_session_of_the_server_gets_the_login_protection(tmp_path: Path) -> None:
    """Requirement 42: `calu serve` read the store once, before the switch to the user."""
    from calu.preflight import LoginProtection

    protection = LoginProtection(False, (), False)
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        hostname="h",
        user="u",
        options=ServeOptions(login_protection=protection, projects=tmp_path.resolve()),
    )
    request = SessionRequest(
        hostname="h", os_user="u", directory=str(tmp_path), id=7, state="claimed"
    )
    assert server._options(request).login_protection is protection


async def test_a_started_session_is_reachable_through_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_host` keeps the session's own token for the relay and forgets it at the end (D242)."""
    server, request = _server_and_request(tmp_path)
    monkeypatch.setattr(session_requests, "finish", Recorder(True).finish)
    seen: list[list[str]] = []

    async def run_from(*_args: Any, on_serving: Any, on_ready: Any, **_kwargs: Any) -> None:
        await on_serving("ws://127.0.0.1:9", "own-token")
        await on_ready("s-1", "ws://127.0.0.1:9")
        seen.append([one.token for one in server.hosted()])
        await anyio.sleep_forever()

    monkeypatch.setattr(serve, "run_from", run_from)
    with anyio.move_on_after(0.3):
        await server._host(None, request)  # type: ignore[arg-type]
    assert seen == [["own-token"]]
    assert server.hosted() == []


async def test_relay_start_files_through_the_token_and_waits_for_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from calu import servers
    from calu.protocol.messages import RelayStartParams
    from calu.relay import StartRefused

    projects = tmp_path.resolve()
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        options=ServeOptions(projects=projects),
        hostname="h",
        user="u",
    )
    server._db = object()  # type: ignore[assignment]
    server.server_id = "srv"
    filed: list[SessionRequest] = []

    async def file_request(_db: Any, server_id: str, sha: str, wanted: SessionRequest) -> int:
        assert (server_id, sha) == ("srv", "sha")
        filed.append(wanted)
        return 40 + len(filed)

    monkeypatch.setattr(servers, "file_request", file_request)
    async with anyio.create_task_group() as tg:

        async def finish_later(request_id: int, session_id: str | None, message: str) -> None:
            await anyio.sleep(0.05)
            server._record_outcome(request_id, session_id, message)

        tg.start_soon(finish_later, 41, "s-1", "")
        started = await server.relay_start(
            "sha", "laptop", RelayStartParams(directory=str(projects / "p"))
        )
        assert started == "s-1"
        tg.start_soon(finish_later, 42, None, "q is not a directory on h")
        with pytest.raises(StartRefused, match="q is not a directory"):
            await server.relay_start("sha", "", RelayStartParams(directory=str(projects / "q")))
        # Outside the projects directory: refused before anything is filed (D243).
        with pytest.raises(StartRefused, match=r"is not in .*, the projects directory"):
            await server.relay_start("sha", "", RelayStartParams(directory="/elsewhere"))
        with pytest.raises(StartRefused, match=r"hidden directory \.git"):
            await server.relay_start("sha", "", RelayStartParams(directory=str(projects / ".git")))
        (projects / "link").symlink_to(projects.parent)
        with pytest.raises(StartRefused, match="is not in"):
            await server.relay_start("sha", "", RelayStartParams(directory=str(projects / "link")))
    # The request names the server's own host and user, and says it came through the relay.
    assert [(w.hostname, w.os_user, w.label) for w in filed] == [
        ("h", "u", "relay: laptop"),
        ("h", "u", "relay"),
    ]


def test_a_claimed_request_outside_the_projects_directory_is_refused(tmp_path: Path) -> None:
    """D243: the same rule for every request the server claims, also one of `calu start`."""
    from calu.serve import Refused

    projects = (tmp_path / "projects").resolve()
    (projects / "app").mkdir(parents=True)
    (projects / ".cache").mkdir()
    (projects / "out").symlink_to(tmp_path)
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        options=ServeOptions(projects=projects),
        hostname="h",
        user="u",
    )

    def options(directory: Path | str):
        return server._options(
            SessionRequest(hostname="h", os_user="u", directory=str(directory), id=1)
        )

    assert options(projects / "app").workspace == projects / "app"
    assert options(projects).workspace == projects
    with pytest.raises(Refused, match=r"is not in .*projects, the projects directory .* on h"):
        options(tmp_path)
    with pytest.raises(Refused, match="calu serve --projects"):
        options("/etc")
    with pytest.raises(Refused, match="is not in"):
        options(projects / "out")
    with pytest.raises(Refused, match=r"hidden directory \.cache"):
        options(projects / ".cache")
    with pytest.raises(Refused, match="is not a directory on h"):
        options(projects / "missing")
    with pytest.raises(Refused, match="no absolute path"):
        options("app")


async def test_the_relay_takes_the_next_derived_port_when_its_own_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D243: the port derived from the directory, else the next one of the sequence."""
    import socket

    from calu import projects_dir
    from calu.serve import RelayPortTaken

    with socket.socket() as occupied, socket.socket() as probe:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        busy = occupied.getsockname()[1]
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()

        def server(**options: Any) -> Server:
            return Server(
                source=None,  # type: ignore[arg-type]
                hidden=set(),
                options=ServeOptions(projects=tmp_path.resolve(), **options),
                hostname="h",
                user="u",
            )

        monkeypatch.setattr(projects_dir, "relay_ports", lambda _projects: [busy, free])
        relay = await server()._bind_relay()
        try:
            assert relay.address == f"ws://127.0.0.1:{free}"
        finally:
            assert relay._ws is not None
            relay._ws.close()
            await relay._ws.wait_closed()
        shown = capsys.readouterr()
        text = shown.out + shown.err
        assert f"the relay port {busy} of {tmp_path.resolve()} is taken" in text
        assert f"takes port {free} instead of {busy}" in text
        monkeypatch.setattr(projects_dir, "relay_ports", lambda _projects: [busy])
        with pytest.raises(RelayPortTaken, match="every relay port from 8811 to 8999 is taken"):
            await server()._bind_relay()
        # A port named with --relay-port is not replaced.
        with pytest.raises(RelayPortTaken, match=f"the relay port {busy} cannot be bound"):
            await server(relay_port=busy)._bind_relay()


def test_a_server_row_counts_as_running_only_while_it_beats() -> None:
    """D244: stale after `herd.STALE_AFTER_S` of silence, at once when its process is gone."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from calu import herd
    from calu.servers import ServerRow

    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    row = ServerRow("5e7", "box", "ada", 42, None, "ws://127.0.0.1:8811", now, None, "/srv/p")
    assert row.live and not row.stale and "SILENT" not in row.line()
    silent = replace(row, silent_for_s=herd.STALE_AFTER_S + 1)
    assert silent.stale and not silent.live and "SILENT for 1 min" in silent.line()
    gone = replace(row, gone=True)
    assert gone.stale and "GONE" in gone.line()
    ended = replace(silent, ended_at=now, gone=True)
    assert not ended.stale and not ended.live and "ended 2026-11-19" in ended.line()


def test_a_server_tokens_line_says_when_its_server_does_not_run() -> None:
    """D244: the tokens of a stale or gone server are refused; `calu token list` says so."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from calu import herd
    from calu.servers import ServerTokenRow

    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    row = ServerTokenRow(2, "5e7", "laptop", now, None, False, "ws://127.0.0.1:8811")
    assert row.line() == "2  valid  server 5e7  ws://127.0.0.1:8811  never used  laptop"
    silent = replace(row, server_silent_for_s=herd.STALE_AFTER_S + 120)
    assert silent.state == "server silent for 3 min"
    assert replace(row, server_gone=True).line().startswith("2  server gone  server 5e7")
    assert replace(row, server_ended=True).state == "server ended"
    assert replace(silent, revoked=True, server_gone=True).state == "revoked"


def test_a_served_sessions_own_token_is_named_as_the_relays() -> None:
    """The token `calu.run` makes for a session under `calu serve` has no label of its own."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from calu.herd import TokenRow

    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    row = TokenRow(5, "abc", "", "ws://127.0.0.1:9000", now, None, False)
    assert row.line() == "5  valid  session abc  ws://127.0.0.1:9000  never used"
    relayed = replace(row, relay_of="5e7")
    assert relayed.line().endswith("never used  relay of server 5e7")
    assert replace(relayed, label="zed").line().endswith("  zed")


async def test_end_gone_peers_ends_only_rows_whose_process_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database names the peers; this process looks up their pids (D244)."""
    import os

    from calu import machine, servers

    here = machine.here().text()
    asked: list[tuple[str, tuple]] = []

    class FakeDb:
        """Answers `calu_server_peers` with a live and a dead peer, and records the ends."""

        async def all(self, sql: str, params: tuple) -> list[tuple]:
            """Return the peers."""
            return [("alive", os.getpid(), here), ("dead", 2147483646, here)]

        async def one(self, sql: str, params: tuple) -> tuple:
            """Record a `calu_server_gone` call."""
            asked.append((sql, params))
            return (True,)

    ended = await servers.end_gone_peers(FakeDb(), "me")  # type: ignore[arg-type]
    assert ended == ["dead"]
    assert [params for _sql, params in asked] == [("me", "dead")]
