import io
import json
from pathlib import Path
from typing import Any

import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db import migrations
from calu.db.target import PasswordAuth, Target
from calu.preflight import LoginProtection
from calu.setup import RULE_LISTS, default_rules, parse_rule_file, recommended_rules


def test_parse_rule_file_attaches_adjacent_comments_only() -> None:
    text = "# Header about the file\n\n# No sudo\n^sudo( |$)\n^eval( |$)\n\n# dropped\n"
    rows = parse_rule_file("command-deny", text)
    assert [(r.pattern, r.comment, r.position) for r in rows] == [
        ("^sudo( |$)", "No sudo", 0),
        ("^eval( |$)", "", 1),
    ]


def test_no_rules_are_shipped_and_the_recommended_set_allows_no_command() -> None:
    """D179, D198: every list starts empty; the optional set has no command allow list."""
    assert default_rules() == []
    rows = recommended_rules()
    assert {r.list for r in rows} == set(RULE_LISTS) - {"command-allow"}
    assert any(r.pattern.startswith("^git( -[Cc] \\S+)* push") for r in rows)


def test_migrations_are_found_in_order() -> None:
    found = migrations()
    assert found[0].version == 1
    assert [m.version for m in found] == sorted(m.version for m in found)
    assert "CREATE TABLE sessions" in found[0].sql


DSN = "host=db.example.org dbname=calu user=u"
"""A connection string `calu credential set` accepts for a login entry."""


def run_cli(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, argv: list[str], stdin: str
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv, store_factory=lambda: store)


def test_credential_set_list_remove(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["credential", "set", "admin"], f"{DSN}\npw\npw\n") == 0
    assert store.load("admin", "pw") == bytearray(DSN.encode())
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "admin" in capsys.readouterr().out
    assert run_cli(monkeypatch, store, ["credential", "remove", "admin"], "") == 0
    assert run_cli(monkeypatch, store, ["credential", "remove", "admin"], "") == 1


def test_mismatched_confirmation_is_an_error(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["credential", "set", "admin"], f"{DSN}\npw\nother\n") == 1
    assert "do not match" in capsys.readouterr().err
    assert store.names() == []


def test_migrate_without_admin_credential_is_an_error(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["db", "migrate"], "") == 1
    assert "no credential 'admin'" in capsys.readouterr().err


def test_run_without_session_credential_is_an_error(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["run", "--batch"], "") == 1
    assert "no credential 'session'" in capsys.readouterr().err


def test_run_refuses_a_session_credential_stored_without_a_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """cs4: REQUIREMENTS 17 holds on the read path too, not only for `credential set`.

    An entry without a password is not authenticated, so whatever can write the credential
    file could swap it for a connection string to another database, and the session would
    start there without anybody being asked for anything.
    """
    store.save("session", "postgresql://attacker.example/calu", None)
    started: list[object] = []
    monkeypatch.setattr(cli, "run_session", lambda *args, **kwargs: started.append(args))
    assert run_cli(monkeypatch, store, ["run", "--batch", "-p", "go"], "") == 1
    assert "stored without a password" in capsys.readouterr().err
    assert started == []


def test_run_rejects_a_directory_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["run", "/no/such/place"], "") == 1
    assert "is not a directory" in capsys.readouterr().err


def test_herd_commands_need_a_credential(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    # They take `operator` or `admin` (D48), so the message names both.
    for argv in (["sessions"], ["escalations"], ["resolve", "1", "allow"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert "no credential 'operator' or 'admin'" in capsys.readouterr().err


def test_evals_needs_a_credential(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading the cases and scoring them changes nothing, so an operator may run it (D48).
    assert run_cli(monkeypatch, store, ["evals"], "") == 1
    assert "no credential 'operator' or 'admin'" in capsys.readouterr().err


def test_the_configuration_commands_need_the_admin_credential(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (
        ["project", "list"],
        ["config", "get"],
        ["config", "set", "claude.model", "opus"],
        ["config", "add", "claude.sandbox.deny_read", "~/x"],
        ["config", "remove", "claude.sandbox.deny_read", "~/x"],
        ["checker", "list"],
        ["rules", "list"],
    ):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert "no credential 'admin'" in capsys.readouterr().err


def test_arguments_are_checked_before_the_password_is_asked(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["project", "add", "/no/such/dir"], "") == 1
    assert "is not a directory" in capsys.readouterr().err
    assert run_cli(monkeypatch, store, ["checker", "show"], "") == 1
    assert "needs a NAME" in capsys.readouterr().err
    assert (
        run_cli(monkeypatch, store, ["checker", "set", "edit", "--instructions-file", "/x"], "")
        == 1
    )
    assert "cannot read /x" in capsys.readouterr().err
    # `add` and `remove` change one entry, so they need the entry as well as the key.
    for action in ("add", "remove"):
        assert run_cli(monkeypatch, store, ["config", action, "claude.sandbox.deny_read"], "") == 1
        assert f"config {action} needs a KEY and a VALUE" in capsys.readouterr().err


def test_rules_arguments_are_interpreted_per_action(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["rules", "remove"], "") == 1
    assert "needs a rule id" in capsys.readouterr().err
    assert run_cli(monkeypatch, store, ["rules", "remove", "abc"], "") == 1
    assert "not a rule id" in capsys.readouterr().err
    assert run_cli(monkeypatch, store, ["rules", "add", "command-deny"], "") == 1
    assert "needs a list name and a pattern" in capsys.readouterr().err
    # A well-formed call gets as far as the credential.
    assert run_cli(monkeypatch, store, ["rules", "add", "command-deny", "^x$"], "") == 1
    assert "no credential 'admin'" in capsys.readouterr().err


def test_the_protocol_schema_is_written_without_a_credential(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(monkeypatch, store, ["protocol"], "") == 0
    document = json.loads(capsys.readouterr().out)
    assert "state.get" in document["requests"]
    assert "check.result" in document["events"]
    assert document["results"]["state.get"]
    out = tmp_path / "protocol.json"
    assert run_cli(monkeypatch, store, ["protocol", "--out", str(out)], "") == 0
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == 1
    assert run_cli(monkeypatch, store, ["protocol", "--out", "/no/such/dir/x.json"], "") == 1
    assert "cannot write" in capsys.readouterr().err


def test_resolve_takes_the_answer_as_one_argument(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """The hint `calu resolve <id> answer "..."` has to parse, or it is a lie."""
    parser = cli.build_parser()
    args = parser.parse_args(["resolve", "7", "answer", "Spaces, four of them"])
    assert (args.id, args.decision, args.text) == (7, "answer", "Spaces, four of them")
    assert args.note is None
    # --note is the same thing written the other way, and wins if both are given.
    other = parser.parse_args(["resolve", "7", "deny", "--note", "not today"])
    assert other.note == "not today" and other.text is None
    both = parser.parse_args(["resolve", "7", "deny", "positional", "--note", "flag"])
    assert (both.text, both.note) == ("positional", "flag")


def test_watching_a_herd_prefers_the_operator_credential(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read-and-resolve commands take the narrow credential when there is one.

    See agent/records/PLAN.md D48.
    """
    watching = (
        ["sessions"],
        ["escalations"],
        ["escalation", "1"],
        ["resolve", "1", "allow"],
        ["token", "list"],
        ["evals", "--from-db"],
    )
    # With neither credential the message names both and says how to get the narrow one.
    for argv in watching:
        assert run_cli(monkeypatch, store, argv, "") == 1
        said = capsys.readouterr().err
        assert "no credential 'operator' or 'admin'" in said
        assert "calu credential new operator" in said

    # With only an operator credential they use it; the password is wrong here, which proves
    # which credential was asked for without needing a database.
    store.save("operator", bytearray(b"postgresql://calu_operator@127.0.0.1/calu"), b"pw")
    for argv in watching:
        assert run_cli(monkeypatch, store, argv, "wrong-password") == 1
        assert "password" in capsys.readouterr().err.lower()

    # Commands that change the configuration have no stored `admin` entry to use here: they
    # find the database through the operator entry and ask for the admin password, to log in
    # as `calu_admin` (D213). A blank input is a password like any other: it is tried on the
    # operator entry, whose password it is not, and the store says so.
    for argv in (["config", "get"], ["checker", "list"], ["rules", "list"], ["project", "list"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        said = capsys.readouterr().err
        assert "empty password" not in said and "password" in said.lower()


def test_run_maps_every_flag_onto_the_session_options(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, tmp_path: Path
) -> None:
    """A flag that reaches the wrong field is silent, so every one of them is checked here."""
    recorded: list[Any] = []

    async def fake_run_session(url: bytearray, options: Any) -> int:
        """Record the options and the credential instead of starting a session.

        Args:
            url: The session credential as the command decrypted it.
            options: The `RunOptions` the command built.

        Returns:
            0, as a session that ended cleanly does.
        """
        recorded.append((bytes(url), options))
        return 0

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    monkeypatch.setattr(
        cli, "load_credential", lambda *_args, **_kwargs: bytearray(b"postgresql://session@db")
    )
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("  from a file  \n", encoding="utf-8")

    assert (
        run_cli(
            monkeypatch,
            store,
            [
                "run",
                str(tmp_path),
                "--prompt-file",
                str(prompt_file),
                "--project",
                "demo",
                "--resume",
                "9f0c1a52-6d1e-4c3b-8a57-2b1d0e4f7a11",
                "--fork-before-turn",
                "3",
                "--model",
                "opus",
                "--effort",
                "high",
                "--batch",
                "--secure",
            ],
            "",
        )
        == 0
    )
    assert len(recorded) == 1
    url, options = recorded[0]
    assert url == b"postgresql://session@db"
    assert options.workspace == tmp_path.resolve()
    assert options.prompt == "from a file"  # read from the file and stripped
    assert options.project == "demo"
    assert options.resume_id == "9f0c1a52-6d1e-4c3b-8a57-2b1d0e4f7a11"
    assert options.fork_before_turn == 3
    assert options.model == "opus"
    assert options.effort == "high"
    assert options.interactive is False  # --batch
    assert options.secure is True  # --secure (D226)
    assert options.socket_path is None  # no --socket: a TCP port
    # Requirement 42: the store's protection is read before any user switch; the fixture's
    # store is empty, so it says nothing about the session's login.
    assert options.login_protection == LoginProtection(False, None, False)


def test_run_refuses_a_prompt_file_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file is read before the password is asked, so a typo costs nothing."""
    assert run_cli(monkeypatch, store, ["run", "--prompt-file", "/no/such/file"], "") == 1
    said = capsys.readouterr().err
    assert "cannot read /no/such/file" in said


def test_a_credential_without_a_password_is_read_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D104: `--no-password` takes the prompt out of every command that uses the credential.

    The test does not mock the prompt away -- it leaves **nothing** on stdin after the
    connection string. A command that still asked for a password would read the empty line and
    fail, so the assertion that the credential comes back is also the assertion that nothing
    was asked.
    """
    assert (
        run_cli(monkeypatch, store, ["credential", "set", "operator", "--no-password"], f"{DSN}\n")
        == 0
    )
    printed = capsys.readouterr().out
    assert "only the file's mode protects it" in printed
    assert str(store.path) in printed, "the warning has to name the file that now holds it"

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.load_credential(store, "operator", "Operator password: ") == bytearray(DSN.encode())

    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "operator  (file mode only)" in capsys.readouterr().out

    # Setting it again without the switch puts a password back on it.
    assert run_cli(monkeypatch, store, ["credential", "set", "operator"], f"{DSN}\npw\npw\n") == 0
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "operator  (password)" in capsys.readouterr().out


def test_the_session_credential_cannot_be_stored_without_a_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D115: `--no-password` must not offer a one-flag way around REQUIREMENTS 17.

    Starting a session is password protected by design, and it asks once per session -- the
    complaint the switch answers was about the read-and-resolve commands, which ask several
    times an hour.
    """
    assert run_cli(monkeypatch, store, ["credential", "set", "session", "--no-password"], "") == 1
    said = capsys.readouterr().err
    assert "cannot be stored without a password" in said
    assert "REQUIREMENTS 17" in said and "'operator'" in said
    assert store.names() == [], "and nothing was stored"

    # The operator credential, which the switch is for, still works.
    assert (
        run_cli(monkeypatch, store, ["credential", "set", "operator", "--no-password"], f"{DSN}\n")
        == 0
    )
    assert store.needs_password("operator") is False


def test_every_command_sees_the_keys_in_calu_s_own_env_file(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D131: `calu doctor` said "none of the known providers has a key" with one in the file.

    `~/.config/calu/.env` is where the guide tells a user to put the checker keys, and only
    `calu run` read it -- so `calu doctor` contradicted itself (it listed the file as found two
    lines below) and `calu evals` ran its checkers without the key. The CLI now loads the file
    before any command.
    """
    for name in ("ANTHROPIC_API_KEY", "FIREWORKS_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)  # restored, or removed again, after the test
    (tmp_path / "calu").mkdir()
    (tmp_path / "calu" / ".env").write_text("FIREWORKS_API_KEY=fw-not-a-real-key\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    run_cli(monkeypatch, store, ["doctor"], "")  # the exit code depends on the machine
    said = capsys.readouterr().out
    assert "checker keys: fireworks" in said, said


def test_the_web_text_of_a_rule_list_reads_back_to_the_same_rules() -> None:
    """D137: the rules page edits a list as text, so text -> rules -> text must not drift."""
    from calu.admin import RuleRow, rule_list_text

    rows = [
        RuleRow(1, "global", "command-deny", "^sudo( |$)", "", 0, True),
        RuleRow(
            2, "global", "command-deny", "^git reset --hard( |$)", "Rewrites history.", 1, True
        ),
        RuleRow(3, "global", "command-deny", "^eval( |$)", "", 2, True),
    ]
    text = rule_list_text(rows)
    parsed = parse_rule_file("command-deny", text)
    assert [(r.pattern, r.comment) for r in parsed] == [(r.pattern, r.comment) for r in rows]
    assert rule_list_text([]) == ""


class _AdminConn:
    """A connection that records statements and says `calu_admin` is a member of rds_iam."""

    def __init__(self, member: bool) -> None:
        """Start with no statements.

        Args:
            member: What the membership query answers.
        """
        self.member = member
        self.statements: list[str] = []
        self.pgconn = self

    def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes) -> bytes:
        """Stand in for libpq's verifier computation.

        Args:
            password: The password.
            user: The role.
            algorithm: The algorithm.

        Returns:
            A fake verifier.
        """
        return b"SCRAM-SHA-256$fake"

    async def execute(self, query: Any, params: Any = None) -> _AdminConn:
        """Record the statement.

        Args:
            query: The statement (bytes, str or `psycopg.sql.Composed`).
            params: Its parameters (ignored).

        Returns:
            Itself, as the cursor.
        """
        self.statements.append(repr(query))
        return self

    async def fetchone(self) -> tuple | None:
        """Answer the membership query.

        Returns:
            A row if `calu_admin` is a member of rds_iam, else None.
        """
        return (1,) if self.member else None


@pytest.mark.parametrize("member", [True, False])
def test_the_admin_password_works_on_rds_after_leaving_rds_iam(member: bool) -> None:
    """On RDS a member of rds_iam is asked for a token and its password is ignored."""
    import asyncio

    from calu.setup import enable_admin_login

    conn = _AdminConn(member)
    asyncio.run(enable_admin_login(conn, "pw"))  # type: ignore[arg-type]
    revokes = [i for i, s in enumerate(conn.statements) if "REVOKE rds_iam" in s]
    alters = [i for i, s in enumerate(conn.statements) if "ALTER ROLE" in s]
    assert len(alters) == 1 and "calu_admin" in conn.statements[alters[0]]
    assert "'pw'" not in conn.statements[alters[0]], "only the verifier reaches the server"
    if member:
        assert len(revokes) == 1 and revokes[0] < alters[0]
        assert "calu_admin" in conn.statements[revokes[0]]
    else:
        assert revokes == []


def test_ctrl_c_at_a_password_prompt_is_one_line(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """`calu setup` and Ctrl-C at "Admin password:" printed a traceback (root box)."""
    store.save("admin", bytearray(DSN.encode()), b"pw")

    def interrupted(_prompt: str, confirm: bool = False) -> bytearray:
        """Stand in for the prompt at which the user presses Ctrl-C."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "read_password", interrupted)
    assert run_cli(monkeypatch, store, ["config", "get"], "") == 130
    assert capsys.readouterr().err.strip() == "calu: interrupted"


def test_the_end_of_input_at_a_password_prompt_is_an_error_in_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-D at a prompt on a terminal raised EOFError out of `calu`."""

    class Terminal(io.StringIO):
        """An input that says it is a terminal."""

        def isatty(self) -> bool:
            """Claim to be a terminal, so that the prompt uses `getpass`."""
            return True

    def ended(_prompt: str) -> str:
        """Stand in for `getpass.getpass` when the input ends."""
        raise EOFError

    monkeypatch.setattr("sys.stdin", Terminal(""))
    monkeypatch.setattr(cli.getpass, "getpass", ended)
    with pytest.raises(cli.CliError, match="no password was entered"):
        cli.read_password("Admin password: ")


def test_a_client_that_ends_asks_no_second_password_for_its_token(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """One calu password per command: the root box showed "Admin password:" again at /quit."""
    store.save("operator", bytearray(b"postgresql://calu_operator@127.0.0.1/calu"), b"pw")
    asked: list[str] = []
    monkeypatch.setattr(cli, "read_password", lambda prompt, confirm=False: asked.append(prompt))
    monkeypatch.setattr(cli, "_held", [])
    monkeypatch.setattr(cli, "_shared_admin", lambda: None)
    cli._revoke(store, 7, may_ask=False)
    said = capsys.readouterr().err
    assert asked == []
    assert "client token 7 was not revoked" in said and "calu token revoke 7" in said
    assert "editor" not in said, "that is the message of `calu acp`"


def test_a_password_the_database_refuses_is_said_once(capsys: pytest.CaptureFixture[str]) -> None:
    """libpq repeats the refusal per address it tried; one sentence is enough."""
    refusal = (
        'connection failed: connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
        'password authentication failed for user "calu_admin"\n'
        'connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
        'password authentication failed for user "calu_admin"'
    )
    assert cli._error_text(cli.LoginRefused(refusal, "28P01")) == (
        "the database refused the login, so the password is wrong: "
        'password authentication failed for user "calu_admin"'
    )
    other = cli.LoginRefused("FATAL:  database gone", "3D000")
    assert cli._error_text(other) == "LoginRefused: FATAL:  database gone"


# ------------------------------------------------------------------ which database is changed


OWN = b"postgresql://calu_owner:own@127.0.0.1:5433/calu"
SHARED = b"postgresql://calu_operator:op@db.example:5432/calu"


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[Target]:
    """Record the logins the settings commands would connect with, instead of connecting."""
    opened: list[Target] = []

    async def record(url: str, work: object) -> str:
        opened.append(Target.parse(url))
        return "done"

    monkeypatch.setattr(cli, "_with_admin", record)
    monkeypatch.setattr(cli, "_shared_admin", lambda: None)
    return opened


CHANGES = (
    ["config", "set", "claude.model", "haiku"],
    ["checker", "set", "command", "--disable"],
    ["rules", "add", "command_allow", "^ls$"],
)


def test_two_stored_databases_are_not_guessed_between(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    """Root box, E: `calu config` changed the user's own database, not the session's."""
    store.save("admin", bytearray(OWN), None)
    store.save("operator", bytearray(SHARED), None)
    for argv in CHANGES:
        assert run_cli(monkeypatch, store, argv, "") == 1
        said = capsys.readouterr().err
        assert "`admin` names 127.0.0.1:5433/calu" in said
        assert "`operator` names db.example:5432/calu" in said
        assert "--database admin" in said and "--database operator" in said
        assert "changing settings" not in said
    assert connections == []

    for argv in CHANGES:
        assert run_cli(monkeypatch, store, [*argv, "--database", "admin"], "") == 0
        assert (
            capsys.readouterr().err.strip()
            == "changing settings in 127.0.0.1:5433/calu as calu_owner"
        )
        assert connections.pop().place == "127.0.0.1:5433/calu"
        # The operator's database is changed as `calu_admin`, with the password typed now.
        assert run_cli(monkeypatch, store, [*argv, "--database", "operator"], "typed\n") == 0
        assert (
            capsys.readouterr().err.strip()
            == "changing settings in db.example:5432/calu as calu_admin"
        )
        used = connections.pop()
        assert (used.place, used.user) == ("db.example:5432/calu", "calu_admin")
        assert isinstance(used.auth, PasswordAuth) and used.auth.password == "typed"


def test_two_entries_of_one_database_need_no_choice(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    store.save("admin", bytearray(OWN), None)
    store.save("operator", bytearray(b"postgresql://calu_operator:op@127.0.0.1:5433/calu"), None)
    assert run_cli(monkeypatch, store, CHANGES[0], "") == 0
    assert (
        capsys.readouterr().err.strip() == "changing settings in 127.0.0.1:5433/calu as calu_owner"
    )
    assert connections.pop().user == "calu_owner"
    # A command that only reads says nothing on stderr.
    assert run_cli(monkeypatch, store, ["config", "list"], "") == 0
    assert capsys.readouterr().err == ""


def test_the_shared_login_and_the_password_free_entry_come_first(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    """(a) the place file's login, (b) a `calu_admin` entry without a password, then the rest."""
    store.save("admin", bytearray(OWN), None)
    store.save("operator", bytearray(SHARED), None)
    store.save(cli.ADMIN_ENTRY, bytearray(b"postgresql://calu_admin:gen@127.0.0.1:5440/calu"), None)
    assert run_cli(monkeypatch, store, CHANGES[0], "") == 0
    assert (
        capsys.readouterr().err.strip() == "changing settings in 127.0.0.1:5440/calu as calu_admin"
    )
    assert connections.pop().place == "127.0.0.1:5440/calu"

    place = Target.parse(b"postgresql://calu_admin:shared@127.0.0.1:5450/calu")
    monkeypatch.setattr(cli, "_shared_admin", lambda: place)
    assert run_cli(monkeypatch, store, CHANGES[1], "") == 0
    assert (
        capsys.readouterr().err.strip() == "changing settings in 127.0.0.1:5450/calu as calu_admin"
    )
    assert connections.pop().place == "127.0.0.1:5450/calu"
    # A choice on the command line is followed, whatever else is stored.
    assert run_cli(monkeypatch, store, [*CHANGES[1], "--database", "admin"], "") == 0
    assert "127.0.0.1:5433/calu as calu_owner" in capsys.readouterr().err
    assert connections.pop().place == "127.0.0.1:5433/calu"


def test_a_database_choice_without_its_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    store.save("admin", bytearray(OWN), None)
    assert run_cli(monkeypatch, store, [*CHANGES[0], "--database", "operator"], "") == 1
    assert "no credential 'operator'" in capsys.readouterr().err
    assert connections == []


def test_the_start_line_of_calu_web_names_the_admin_password_only_where_there_is_one() -> None:
    """After `calu setup --no-password` the line ended with advice that does not apply."""
    without = cli._web_token_line("calu.localhost", 8800, "tok", has_admin_password=False)
    assert without == (
        "calu web on http://calu.localhost:8800/?token=tok (open it once per browser; the "
        "token is your login, keep it to yourself)."
    )
    with_one = cli._web_token_line("calu.localhost", 8800, "tok", has_admin_password=True)
    assert with_one.startswith(without)
    assert with_one.endswith("`calu setup` makes the admin password the login instead.")
