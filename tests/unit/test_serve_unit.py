"""`calu serve` without a database: a session whose start cannot be reported is stopped.

`run_from` and `session_requests.finish` are replaced by fakes; no database, no Claude Code.
"""

from pathlib import Path
from typing import Any

import anyio
import psycopg
import pytest

from calu import serve, session_requests
from calu.serve import Server
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
    server = Server(source=None, hidden=set(), hostname="h", user="u")  # type: ignore[arg-type]
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
    from calu.serve import ServeOptions

    protection = LoginProtection(False, (), False)
    server = Server(
        source=None,  # type: ignore[arg-type]
        hidden=set(),
        hostname="h",
        user="u",
        options=ServeOptions(login_protection=protection),
    )
    request = SessionRequest(
        hostname="h", os_user="u", directory=str(tmp_path), id=7, state="claimed"
    )
    assert server._options(request).login_protection is protection
