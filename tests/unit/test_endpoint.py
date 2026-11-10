"""Finding the session a client means when it is given no address (PLAN.md D105)."""

import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from calu import cli
from calu.cli import CliError, _pick_session
from calu.herd import STALE_AFTER_S, SessionRow


def row(
    directory: str,
    *,
    id: str = "11111111-1111-1111-1111-111111111111",
    host: str | None = None,
    address: str = "ws://127.0.0.1:8765",
    silent_for_s: float = 1.0,
    minute: int = 0,
) -> SessionRow:
    """Build a session row for the chooser.

    Args:
        directory: The project directory.
        id: The session id.
        host: The host name; None is this machine, which is the only one that counts.
        address: What the session published; "" means it serves no clients.
        silent_for_s: Seconds since the last heartbeat.
        minute: Minute of the start time, to order two sessions.

    Returns:
        The row.
    """
    when = datetime(2026, 11, 10, 12, minute, tzinfo=UTC)
    return SessionRow(
        id=id,
        hostname=host or socket.gethostname(),
        os_user="me",
        directory=directory,
        state="running",
        state_since=when,
        started_at=when,
        ended_at=None,
        claude_session_id=None,
        open_escalations=0,
        address=address,
        last_seen=when,
        silent_for_s=silent_for_s,
    )


def test_the_session_of_this_directory_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    monkeypatch.chdir(project / "src")
    # A subdirectory of the project is still the project: that is where one works.
    chosen = _pick_session([row(str(project))], None)
    assert chosen.directory == str(project)


def test_sessions_that_cannot_be_reached_are_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three rows the database shows that a client here cannot connect to."""
    monkeypatch.chdir(tmp_path)
    elsewhere = row(str(tmp_path), host="another-host")
    silent = row(str(tmp_path), silent_for_s=STALE_AFTER_S + 1)
    clientless = row(str(tmp_path), address="")
    for unusable in (elsewhere, silent, clientless):
        with pytest.raises(CliError) as raised:
            _pick_session([unusable], None)
        assert "no running session of" in str(raised.value)
        # The message has to say what to do instead, in both directions.
        assert "calu sessions" in str(raised.value) and "--token=" in str(raised.value)
    assert _pick_session([elsewhere, silent, clientless, row(str(tmp_path))], None).address


def test_the_nearest_project_directory_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session of a subproject is meant before the session of the tree above it."""
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    outer_row = row(str(tmp_path / "outer"), id="aaaaaaaa-0000-0000-0000-000000000000")
    inner_row = row(str(inner), id="bbbbbbbb-0000-0000-0000-000000000000")
    assert _pick_session([outer_row, inner_row], None).id == inner_row.id
    assert _pick_session([inner_row, outer_row], None).id == inner_row.id


def test_two_sessions_of_one_directory_have_to_be_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = row(str(tmp_path), id="aaaaaaaa-0000-0000-0000-000000000000", minute=1)
    second = row(str(tmp_path), id="bbbbbbbb-0000-0000-0000-000000000000", minute=2)
    with pytest.raises(CliError) as raised:
        _pick_session([first, second], None)
    message = str(raised.value)
    assert "2 sessions" in message
    assert first.id in message and second.id in message, "a choice needs the ids to choose from"
    assert "--session" in message

    # The beginning of an id is enough, and an ambiguous or unknown one is an error.
    assert _pick_session([first, second], "bbbb").id == second.id
    with pytest.raises(CliError) as unknown:
        _pick_session([first, second], "cccc")
    assert "no session on this host whose id starts with" in str(unknown.value)


def test_an_address_without_a_token_is_refused_before_connecting() -> None:
    """The remote path keeps both arguments; one of them alone is a mistake worth naming."""
    parser = cli.build_parser()
    store = None  # not reached

    only_address = parser.parse_args(["attach", "ws://127.0.0.1:8765"])
    with pytest.raises(CliError) as no_token:
        cli._endpoint_of(only_address, store, "calu attach")  # type: ignore[arg-type]
    assert "--token=<token>" in str(no_token.value)

    only_token = parser.parse_args(["attach", "--token", "x"])
    with pytest.raises(CliError) as no_address:
        cli._endpoint_of(only_token, store, "calu attach")  # type: ignore[arg-type]
    assert "address" in str(no_address.value)

    both = parser.parse_args(["attach", "ws://127.0.0.1:8765", "--token=x"])
    endpoint = cli._endpoint_of(both, store, "calu attach")  # type: ignore[arg-type]
    assert (endpoint.address, endpoint.token, endpoint.token_id) == (
        "ws://127.0.0.1:8765",
        "x",
        None,
    ), "an address given on the command line mints nothing and needs no database"
