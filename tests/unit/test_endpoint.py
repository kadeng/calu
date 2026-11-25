"""Finding the session a client means when it is given no address (agent/records/PLAN.md D105)."""

import dataclasses
import io
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from calu import cli, logins
from calu.cli import CliError, _pick_session
from calu.herd import STALE_AFTER_S, SessionRow
from calu.protocol.client import RequestFailed


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
        assert "calu sessions" in str(raised.value) and "CALU_TOKEN" in str(raised.value)
        assert "`calu start`" in str(raised.value), "the way to get one from a running server"
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
    # ... and something to tell them apart by: the state, the start and a name if it has one.
    named = dataclasses.replace(second, name="payment refactoring", state="escalated")
    with pytest.raises(CliError) as raised:
        _pick_session([first, named], None)
    assert f'{second.id}  escalated, started 2026-11-10 12:02:00, "payment refactoring"' in str(
        raised.value
    )
    assert f"{first.id}  running, started 2026-11-10 12:01:00\n" in str(raised.value)

    # The beginning of an id is enough, and an ambiguous or unknown one is an error.
    assert _pick_session([first, second], "bbbb").id == second.id
    with pytest.raises(CliError) as unknown:
        _pick_session([first, second], "cccc")
    assert "no session on this host whose id starts with" in str(unknown.value)


def accepting(tokens: list[str], good: str | None = None):
    """Return a stand-in for `probe` that records the tokens and accepts `good` (None: any)."""

    async def probe(address: str, token: str, client: str, timeout_s: float = 3.0) -> dict:
        tokens.append(token)
        if good is not None and token != good:
            raise RequestFailed({"code": -32001, "message": "invalid client token"})
        return {}

    return probe


def test_a_token_needs_an_address_and_an_address_is_tried_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token alone is a mistake worth naming; an address alone is tried without a token,
    which a session started with `--insecure` accepts from this machine (D227)."""
    parser = cli.build_parser()
    store = None  # not reached
    monkeypatch.delenv("CALU_TOKEN", raising=False)
    monkeypatch.setattr(cli, "probe", accepting([]))

    only_address = parser.parse_args(["attach", "ws://127.0.0.1:8765"])
    endpoint = cli._endpoint_of(only_address, store, "calu attach")  # type: ignore[arg-type]
    assert (endpoint.address, endpoint.token, endpoint.token_id) == (
        "ws://127.0.0.1:8765",
        "",
        None,
    )
    refused = cli._attach_failure(
        endpoint, RequestFailed({"code": -32001, "message": "invalid client token"})
    )
    assert "CALU_TOKEN=<token> calu attach" in refused and "--insecure" in refused
    assert "--token=" not in refused, "D279: no token on a command line"
    assert "calu token mint" in refused and "CALU_TOKEN" in refused

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


def test_a_given_address_takes_the_token_from_the_environment_or_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The remote way: a token need not stand on the command line, and a refused one is
    asked for again on a terminal; a blank answer gives up."""
    tokens: list[str] = []
    monkeypatch.setattr(cli, "probe", accepting(tokens, good="right"))
    monkeypatch.setenv("CALU_TOKEN", "right")
    assert cli._reach(cli.Endpoint("ws://127.0.0.1:1", "")).token == "right"
    monkeypatch.delenv("CALU_TOKEN")

    answers = iter(["wrong", "right"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    tokens.clear()
    assert cli._reach(cli.Endpoint("ws://127.0.0.1:1", "stale")).token == "right"
    assert tokens == ["stale", "wrong", "right"]
    assert "the session refused that token" in capsys.readouterr().err

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    with pytest.raises(CliError, match="no client token given"):
        cli._reach(cli.Endpoint("ws://127.0.0.1:1", ""))

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(CliError, match="refused the token") as refused:
        cli._reach(cli.Endpoint("ws://127.0.0.1:1", "stale"))
    assert "calu token mint" in str(refused.value)


def test_a_refused_server_token_is_said_once_without_the_protocols_error_code() -> None:
    # The relay's own sentence already named `calu token mint --server`, and the message
    # quoted it with "(code -32001)" before saying the same again.
    relay = (
        "invalid server token (`calu token mint --server` on the server's machine prints one; "
        "a token ends with its server)"
    )
    message = cli._attach_failure(
        cli.Endpoint("ws://127.0.0.1:8826", "t"),
        RequestFailed({"code": -32001, "message": relay}),
    )
    assert "code -32001" not in message
    assert message.count("calu token mint --server") == 1
    assert message.startswith("cannot attach to ws://127.0.0.1:8826: the relay refused the")
    session = cli._attach_failure(
        cli.Endpoint("ws://127.0.0.1:8765", "t"),
        RequestFailed({"code": -32001, "message": "invalid client token"}),
    )
    assert "code -32001" not in session and "calu token mint <session id>" in session


def test_an_address_nobody_answers_at_names_the_tunnel() -> None:
    message = cli._attach_failure(
        cli.Endpoint("ws://127.0.0.1:18765", "t"), ConnectionRefusedError(111, "refused")
    )
    assert "nothing listens at the address" in message
    assert "ssh -L <port>:127.0.0.1:<the session's port> <host>" in message


def test_a_session_behind_a_symbolic_link_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides are resolved paths, so a link on the way to the project hides nothing.

    `calu run` resolves the workspace before it writes the row and `Path.cwd()` is resolved,
    which is what makes the plain string comparison of the two sound. The test enters the
    project through a link to show that the invariant holds in the one case where it could
    break.
    """
    project = tmp_path / "project"
    project.mkdir()
    link = tmp_path / "link"
    link.symlink_to(project)
    monkeypatch.chdir(link)
    assert _pick_session([row(str(project.resolve()))], None).directory == str(project.resolve())


def test_the_acp_adapter_never_reads_a_password_from_the_editor_s_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An editor's protocol line must not be taken for a password.

    `calu acp` speaks ACP on its standard input, and `read_password` reads a line from standard
    input whenever it is not a terminal. Asking for the password of an encrypted credential
    there would therefore swallow the editor's `initialize` request, treat it as the password
    and fail with "wrong password" -- with the stream already broken. The adapter refuses
    instead, and the assertion that the line is still unread is the assertion that it did.
    """
    from calu.credentials import CredentialStore

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(logins.USER, "postgresql://calu_admin:pw@localhost/calu", "a password")
    monkeypatch.chdir(tmp_path)
    first_request = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    stdin = io.StringIO(first_request)
    monkeypatch.setattr("sys.stdin", stdin)

    assert cli.main(["acp"], store_factory=lambda: store) == 1
    printed = capsys.readouterr()
    assert printed.out == "", "stdout carries the protocol and nothing else"
    before, after = logins.no_terminal("\0").split("\0")
    assert before in printed.err and after in printed.err
    assert "CALU_TOKEN" in printed.err, "the way without a database is named"
    assert stdin.read() == first_request, "the editor's request was read as a password"


def test_the_session_list_and_a_minted_token_say_where_a_client_connects() -> None:
    """A user on another machine needs the session's port for the tunnel: `calu sessions`
    shows it, and `calu token mint` prints the two commands."""
    listening = row("/srv/project", host="buildbox", address="ws://127.0.0.1:41235")
    assert "  clients ws://127.0.0.1:41235" in listening.line()
    ended = dataclasses.replace(listening, ended_at=listening.started_at)
    assert "clients" not in ended.line() and cli._attach_recipe(ended) == ""
    recipe = cli._attach_recipe(listening)
    assert "    ssh -N -L 41235:127.0.0.1:41235 buildbox\n" in recipe
    assert recipe.endswith("    CALU_TOKEN=<the token above> calu attach ws://127.0.0.1:41235")
    socket_row = dataclasses.replace(listening, address="unix:/run/user/1000/calu/s.sock")
    assert "ssh -N -L 8765:/run/user/1000/calu/s.sock buildbox" in cli._attach_recipe(socket_row)


def test_a_token_can_come_from_a_file_instead_of_the_command_line(tmp_path: Path) -> None:
    """D279: `--token` stands in the process list, which every user of the machine reads."""
    parser = cli.build_parser()
    kept = tmp_path / "token"
    kept.write_text("secret-token\n")
    args = parser.parse_args(["attach", "ws://127.0.0.1:8765", "--token-file", str(kept)])
    cli._token_from_file(args)
    assert args.token == "secret-token"
    both = parser.parse_args(
        ["attach", "ws://127.0.0.1:8765", "--token=x", "--token-file", str(kept)]
    )
    with pytest.raises(CliError, match="not both"):
        cli._token_from_file(both)
    (tmp_path / "empty").write_text("\n")
    for missing in (tmp_path / "empty", tmp_path / "none"):
        args = parser.parse_args(["acp", "ws://127.0.0.1:8765", "--token-file", str(missing)])
        with pytest.raises(CliError, match="token-file"):
            cli._token_from_file(args)
