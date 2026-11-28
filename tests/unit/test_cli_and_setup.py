import io
import json
import types
from pathlib import Path
from typing import Any

import pytest

from calu import cli, logins
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
    """D266: the shipped migrations begin with the baseline and are sorted by version."""
    found = migrations()
    assert found[0].name == "0001_baseline.sql"
    assert [m.version for m in found] == sorted(m.version for m in found)
    assert "CREATE TABLE public.sessions" in found[0].sql


def test_the_baseline_names_no_owner_and_leaves_the_search_path_as_it_was() -> None:
    """D266: the baseline is a cleaned-up schema dump. It must run as any owner (no role
    name of the database it was dumped from), and it must not change the connection's
    settings beyond its own transaction (`set_config(..., true)`, `SET LOCAL`)."""
    sql = migrations()[0].sql
    assert "calu_owner" not in sql
    assert "set_config('search_path', '', false)" not in sql
    statements = [line for line in sql.splitlines() if line.startswith("SET ")]
    assert statements and all(line.startswith("SET LOCAL ") for line in statements)


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
    assert run_cli(monkeypatch, store, ["credential", "set", "other"], f"{DSN}\npw\npw\n") == 0
    assert store.load("other", "pw") == bytearray(DSN.encode())
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "other" in capsys.readouterr().out
    assert run_cli(monkeypatch, store, ["credential", "remove", "other"], "") == 0
    assert run_cli(monkeypatch, store, ["credential", "remove", "other"], "") == 1


def test_mismatched_confirmation_is_an_error(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["credential", "set", "other"], f"{DSN}\npw\nnot pw\n") == 1
    assert logins.MISMATCH in capsys.readouterr().err
    assert store.names() == []


def test_migrate_without_the_db_admin_login_says_how_to_create_one(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["db", "migrate"], "") == 1
    assert logins.no_login() in capsys.readouterr().err


def test_run_without_a_user_login_says_how_to_create_one(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["run", "--batch"], "") == 1
    assert logins.no_login() in capsys.readouterr().err


def test_run_uses_a_user_login_stored_without_a_password_without_asking(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """D265 (2): an empty user password means calu never asks, `calu run` included.

    Nothing is left on stdin, so a prompt would end with `logins.NOT_ENTERED`; the session
    (a stand-in that records its login) starting is the proof that nothing was asked.
    """
    login = Target.parse(b"postgresql://calu_admin:gen@db.example.org/calu")
    store.save(logins.USER, bytearray(login.dump()), None)
    started: list[Target] = []

    async def fake_run_session(url: bytearray, options: Any) -> int:
        """Record the login instead of starting a session.

        Args:
            url: The login as the command decrypted it.
            options: The `RunOptions` (ignored).

        Returns:
            0, as a session that ended cleanly does.
        """
        started.append(Target.parse(url))
        return 0

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    monkeypatch.setattr(cli, "harden_process", lambda: None)
    assert run_cli(monkeypatch, store, ["run", "--batch", "-p", "go"], "") == 0
    assert started == [login]


def test_run_rejects_a_directory_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["run", "/no/such/place"], "") == 1
    assert "is not a directory" in capsys.readouterr().err


def test_herd_commands_need_a_user_login(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["sessions"], ["escalations"], ["resolve", "1", "allow"], ["evals"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert logins.no_login() in capsys.readouterr().err


def test_the_configuration_commands_need_a_user_login(
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
        assert logins.no_login() in capsys.readouterr().err


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
    # A well-formed call gets as far as the login.
    assert run_cli(monkeypatch, store, ["rules", "add", "command-deny", "^x$"], "") == 1
    assert logins.no_login() in capsys.readouterr().err


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


WATCHING = (
    ["sessions"],
    ["escalations"],
    ["escalation", "1"],
    ["resolve", "1", "allow"],
    ["token", "list"],
    ["evals", "--from-db"],
)
"""Commands that read the fleet or decide an escalation."""

SETTINGS = (["config", "get"], ["checker", "list"], ["rules", "list"], ["project", "list"])
"""Commands that read or change settings."""


def test_the_fleet_and_the_settings_commands_use_the_user_login(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (1): every command takes the `user` entry; there is no other login to choose.

    The stored entry has a password that is never typed here, so each command ends after
    `logins.MAX_ATTEMPTS` wrong answers without a database: the wording proves which
    password was asked for.
    """
    store.save(logins.USER, bytearray(b"postgresql://calu_admin:pw@127.0.0.1/calu"), b"pw")
    wrong = "wrong\n" * logins.MAX_ATTEMPTS
    for argv in (*WATCHING, *SETTINGS):
        assert run_cli(monkeypatch, store, argv, wrong) == 1
        said = capsys.readouterr().err
        for left in range(logins.MAX_ATTEMPTS):
            assert logins.wrong(left) in said


def test_a_store_with_only_old_logins_is_told_to_run_setup(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (5): the entries of before D265 are not used; the message says what to run."""
    store.save("operator", bytearray(b"postgresql://calu_operator:op@127.0.0.1/calu"), b"pw")
    for argv in (*WATCHING, *SETTINGS, ["db", "migrate"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert logins.old_logins(False) in capsys.readouterr().err
    store.save("admin", bytearray(b"postgresql://calu_owner:o@127.0.0.1/calu"), b"pw")
    for argv in (*WATCHING, *SETTINGS, ["db", "migrate"], ["run", "--batch"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert logins.old_logins(True) in capsys.readouterr().err


def test_run_maps_every_flag_onto_the_session_options(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, tmp_path: Path
) -> None:
    """A flag that reaches the wrong field is silent, so every one of them is checked here."""
    recorded: list[Any] = []

    async def fake_run_session(url: bytearray, options: Any) -> int:
        """Record the options and the credential instead of starting a session.

        Args:
            url: The user login as the command decrypted it.
            options: The `RunOptions` the command built.

        Returns:
            0, as a session that ended cleanly does.
        """
        recorded.append((bytes(url), options))
        return 0

    login = Target.parse(b"postgresql://calu_admin:pw@db/calu")
    monkeypatch.setattr(cli, "run_session", fake_run_session)
    monkeypatch.setattr(cli, "harden_process", lambda: None)
    monkeypatch.setattr(cli, "_user_target", lambda *_args, **_kwargs: login)
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
    assert Target.parse(url) == login
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

    The test does not mock the prompt. It leaves nothing on stdin after the connection string.
    A command that still asked for a password would read the end of the input and fail, so a
    credential that comes back shows that nothing was asked.
    """
    argv = ["credential", "set", logins.USER, "--no-password"]
    assert run_cli(monkeypatch, store, argv, f"{DSN}\n") == 0
    printed = capsys.readouterr().out
    assert "only the file's mode protects it" in printed
    assert str(store.path) in printed, "the warning has to name the file that now holds it"

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.load_credential(store, logins.USER, logins.USER_PROMPT) == bytearray(DSN.encode())

    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert f"{logins.USER}  (file mode only)" in capsys.readouterr().out

    # Setting it again without the switch puts a password back on it.
    argv = ["credential", "set", logins.USER]
    assert run_cli(monkeypatch, store, argv, f"{DSN}\npw\npw\n") == 0
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert f"{logins.USER}  (password)" in capsys.readouterr().out


def test_an_empty_password_at_credential_set_user_stores_the_login_without_encryption(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """D265 (2): Enter at the new-password prompt is the empty user password, not an error."""
    assert run_cli(monkeypatch, store, ["credential", "set", logins.USER], f"{DSN}\n\n\n") == 0
    printed = capsys.readouterr().out
    assert logins.EMPTY_MEANS in printed, "what an empty password means is said before"
    assert "without a password" in printed
    assert store.needs_password(logins.USER) is False
    assert store.load(logins.USER) == bytearray(DSN.encode())


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

    The test runs the real `calu doctor`, which probes this machine's tools and environment, and
    reads the process environment. The four provider variables are set first and then removed,
    so that monkeypatch restores them (and removes what the CLI loads) after the test.
    """
    for name in ("ANTHROPIC_API_KEY", "FIREWORKS_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
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
        self.info = types.SimpleNamespace(server_version=180000)

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
def test_the_user_password_works_on_rds_after_leaving_rds_iam(
    member: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the user password sends the verifier, not the password, in one `ALTER ROLE`.

    The verifier is computed with 300000 rounds before that statement. A member of rds_iam is
    revoked from it first, because RDS asks such a member for a token and ignores its password.
    The statements go to a fake connection; nothing connects to a database.
    """
    import asyncio

    monkeypatch.delenv("CALU_SCRAM_ITERATIONS")  # the rounds of a real setup (D279)

    from calu.setup import set_user_password

    conn = _AdminConn(member)
    asyncio.run(set_user_password(conn, "pw"))  # type: ignore[arg-type]
    revokes = [i for i, s in enumerate(conn.statements) if "REVOKE rds_iam" in s]
    alters = [i for i, s in enumerate(conn.statements) if "ALTER ROLE" in s]
    assert len(alters) == 1 and "calu_admin" in conn.statements[alters[0]]
    assert "'pw'" not in conn.statements[alters[0]], "only the verifier reaches the server"
    # D279: the verifier is computed with 300,000 rounds (the connection's setting).
    rounds = [i for i, s in enumerate(conn.statements) if "scram_iterations" in s and "300000" in s]
    assert len(rounds) == 1 and rounds[0] < alters[0]
    if member:
        assert len(revokes) == 1 and revokes[0] < alters[0]
        assert "calu_admin" in conn.statements[revokes[0]]
    else:
        assert revokes == []


def test_ctrl_c_at_a_password_prompt_is_one_line(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """`calu setup` and Ctrl-C at the password prompt printed a traceback (root box)."""
    store.save(logins.USER, bytearray(DSN.encode()), b"pw")

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
    with pytest.raises(cli.CliError) as raised:
        cli.read_password(logins.USER_PROMPT)
    assert str(raised.value) == logins.NOT_ENTERED


def test_a_client_that_ends_asks_no_second_password_for_its_token(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """One calu password per command: the root box showed "Admin password:" again at /quit."""
    store.save(logins.USER, bytearray(b"postgresql://calu_admin@127.0.0.1/calu"), b"pw")
    asked: list[str] = []
    monkeypatch.setattr(cli, "read_password", lambda prompt, confirm=False: asked.append(prompt))
    monkeypatch.setattr(cli, "_held", [])
    monkeypatch.setattr(cli, "_shared_user", lambda: None)
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


USER_LOGIN = b"postgresql://calu_admin:pw@127.0.0.1:5433/calu"
"""A stored `user` login: the role `calu_admin` of a local database."""


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[Target]:
    """Record the logins the settings commands would connect with, instead of connecting.

    Side effects: replaces `cli._with_admin` by the recorder and `cli._shared_user` by "no
    place file publishes a login" for the test.

    Returns:
        The logins, in the order the commands used them.
    """
    opened: list[Target] = []

    async def record(url: str, work: object) -> str:
        opened.append(Target.parse(url))
        return "done"

    monkeypatch.setattr(cli, "_with_admin", record)
    monkeypatch.setattr(cli, "_shared_user", lambda: None)
    return opened


CHANGES = (
    ["config", "set", "claude.model", "haiku"],
    ["checker", "set", "command", "--disable"],
    ["rules", "add", "command_allow", "^ls$"],
)


def test_a_settings_change_names_the_database_and_a_read_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    """Root box, E: a person who reaches two databases is told which one a change went to."""
    store.save(logins.USER, bytearray(USER_LOGIN), None)
    for argv in CHANGES:
        assert run_cli(monkeypatch, store, argv, "") == 0
        assert (
            capsys.readouterr().err.strip()
            == "changing settings in 127.0.0.1:5433/calu as calu_admin"
        )
        assert connections.pop() == Target.parse(USER_LOGIN)
    assert run_cli(monkeypatch, store, ["config", "list"], "") == 0
    assert capsys.readouterr().err == ""
    assert connections.pop() == Target.parse(USER_LOGIN)


def test_the_login_the_place_file_publishes_comes_before_the_user_entry(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
    connections: list[Target],
) -> None:
    """D228: with root's empty user password the place file's login is used, nothing asked."""
    store.save(logins.USER, bytearray(USER_LOGIN), b"never typed")
    place = Target.parse(b"postgresql://calu_admin:shared@127.0.0.1:5450/calu")
    monkeypatch.setattr(cli, "_shared_user", lambda: place)
    assert run_cli(monkeypatch, store, CHANGES[0], "") == 0
    assert (
        capsys.readouterr().err.strip() == "changing settings in 127.0.0.1:5450/calu as calu_admin"
    )
    assert connections.pop() == place


# ------------------------------------------------------------------ the user login (D265)


def _no_prompt(prompt: str, confirm: bool = False) -> bytearray:
    """Stand in for `read_password` where nothing may be asked.

    Args:
        prompt: The prompt.
        confirm: Ignored.

    Raises:
        AssertionError: Always, naming the prompt.
    """
    raise AssertionError(f"asked {prompt!r}")


def _place_file(monkeypatch: pytest.MonkeyPatch, place: Target | None) -> None:
    """Stand in for root's place file (`calu.db.place`), as `cli` reads it.

    Side effects: replaces `cli._place_or_none` and `cli._shared_user` for the test.

    Args:
        monkeypatch: Used for the replacements.
        place: The login the file describes; with a password it is the published login of
            an empty user password. None: there is no file.
    """
    published = (
        place
        if place is not None and isinstance(place.auth, PasswordAuth) and place.auth.password
        else None
    )
    monkeypatch.setattr(cli, "_place_or_none", lambda: place)
    monkeypatch.setattr(cli, "_shared_user", lambda: published)


def _accepts(monkeypatch: pytest.MonkeyPatch, accepted: set[str]) -> list[str]:
    """Let the database accept the user passwords in `accepted`, without a database.

    Side effects: replaces `cli.is_user_password` for the test.

    Args:
        monkeypatch: Used for the replacement.
        accepted: The passwords `calu_admin` logs in with.

    Returns:
        The passwords that were checked, in order.
    """
    checked: list[str] = []

    async def is_user_password(place: Target, password: str) -> bool:
        checked.append(password)
        return password in accepted

    monkeypatch.setattr(cli, "is_user_password", is_user_password)
    return checked


PLACE = Target.parse(b"postgresql://calu_admin@db.example:5432/calu")
"""Root's place file without a secret: where the database is."""

FREE = Target.parse(b"postgresql://calu_admin:gen@127.0.0.1:5433/calu")
"""A `user` login stored without a password (an empty user password)."""


def test_a_held_login_comes_first_and_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    held = Target.parse(b"postgresql://calu_admin:held@10.0.0.1/calu")
    cli._held[:] = [held]
    store.save(logins.USER, bytearray(FREE.dump()), None)
    _place_file(monkeypatch, Target.parse(b"postgresql://calu_admin:shared@10.0.0.2/calu"))
    monkeypatch.setattr(cli, "read_password", _no_prompt)
    assert cli._user_target(store) is held


def test_the_published_login_comes_before_a_protected_user_entry(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    shared = Target.parse(b"postgresql://calu_admin:shared@10.0.0.2/calu")
    store.save(logins.USER, bytearray(FREE.dump()), b"pw")
    _place_file(monkeypatch, shared)
    monkeypatch.setattr(cli, "read_password", _no_prompt)
    assert cli._user_target(store) == shared
    assert cli._admin_place == [shared]


def test_a_free_user_entry_is_used_without_asking_and_before_the_place_file(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save(logins.USER, bytearray(FREE.dump()), None)
    _place_file(monkeypatch, PLACE)
    monkeypatch.setattr(cli, "read_password", _no_prompt)
    assert cli._user_target(store) == FREE
    (place,) = cli._admin_place
    assert (place.place, place.user) == (FREE.place, "calu_admin")
    assert place.auth == PasswordAuth(password=""), "where the database is, without a secret"
    assert cli._held == [], "only a root client keeps the login"


def test_a_protected_user_entry_asks_once_for_both_entries(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """One user password protects `user` and `db-admin`: a command that needs both asks once."""
    owner = Target.parse(b"postgresql://calu_owner:own@127.0.0.1:5433/calu")
    store.save(logins.USER, bytearray(FREE.dump()), b"pw")
    store.save(logins.DB_ADMIN, bytearray(owner.dump()), b"pw")
    _place_file(monkeypatch, PLACE)
    asked: list[str] = []

    def answer(prompt: str, confirm: bool = False) -> bytearray:
        asked.append(prompt)
        return bytearray(b"pw")

    monkeypatch.setattr(cli, "read_password", answer)
    assert cli._user_target(store) == FREE
    assert cli._db_admin_target(store) == owner
    assert asked == [logins.USER_PROMPT]


def test_a_protected_user_entry_without_a_terminal_needs_the_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save(logins.USER, bytearray(FREE.dump()), b"pw")
    _place_file(monkeypatch, None)
    monkeypatch.setattr(cli, "read_password", _no_prompt)
    with pytest.raises(cli.NeedsPassword) as raised:
        cli._user_target(store, may_ask=False, why="no terminal here")
    assert str(raised.value) == logins.no_terminal("no terminal here")


def test_a_user_entry_is_used_although_old_entries_are_stored(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save("admin", bytearray(b"postgresql://calu_owner:o@127.0.0.1/calu"), b"old")
    store.save(logins.USER, bytearray(FREE.dump()), None)
    _place_file(monkeypatch, None)
    assert cli._user_target(store) == FREE


def test_old_entries_are_refused_before_the_place_file_is_tried(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save("operator", bytearray(b"postgresql://calu_operator:op@127.0.0.1/calu"), None)
    _place_file(monkeypatch, PLACE)
    monkeypatch.setattr(cli, "read_password", _no_prompt)
    with pytest.raises(cli.CliError) as raised:
        cli._user_target(store)
    assert str(raised.value) == logins.old_logins(False)


def test_without_a_user_entry_the_place_file_takes_the_typed_user_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused answer is asked again with the tries left; a blank one is called blank."""
    _place_file(monkeypatch, PLACE)
    checked = _accepts(monkeypatch, {"right"})
    monkeypatch.setattr("sys.stdin", io.StringIO("nope\n\nright\n"))
    login = cli._user_target(store)
    assert login == cli.user_login(PLACE, "right")
    assert checked == ["nope", "", "right"]
    assert cli._typed["user"] == "right"
    said = capsys.readouterr().err
    assert logins.wrong(2) in said and logins.wrong(1, blank=True) in said
    # Without a terminal nothing is asked.
    cli._admin_place.clear()
    with pytest.raises(cli.NeedsPassword):
        cli._user_target(store, may_ask=False)


def test_the_last_refused_try_at_the_place_file_says_how_to_recover(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    _place_file(monkeypatch, PLACE)
    _accepts(monkeypatch, set())
    monkeypatch.setattr("sys.stdin", io.StringIO("a\nb\n\n"))
    with pytest.raises(cli.CliError) as raised:
        cli._user_target(store)
    assert str(raised.value) == logins.wrong(0, blank=True)
    assert logins.FORGOT in str(raised.value)


def test_without_any_login_the_message_names_setup(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    _place_file(monkeypatch, None)
    with pytest.raises(cli.CliError) as raised:
        cli._user_target(store)
    assert str(raised.value) == logins.no_login()


def test_a_wrong_user_password_is_asked_again_with_the_tries_left(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.save(logins.USER, bytearray(DSN.encode()), b"pw")
    monkeypatch.setattr("sys.stdin", io.StringIO("a\n\npw\n"))
    assert cli.load_credential(store, logins.USER, logins.USER_PROMPT) == bytearray(DSN.encode())
    said = capsys.readouterr().err
    assert logins.wrong(logins.MAX_ATTEMPTS - 1) in said
    assert logins.wrong(logins.MAX_ATTEMPTS - 2, blank=True) in said, "Enter is called blank"

    monkeypatch.setattr("sys.stdin", io.StringIO("a\n" * logins.MAX_ATTEMPTS))
    cli._typed.clear()
    with pytest.raises(cli.CliError) as raised:
        cli.load_credential(store, logins.USER, logins.USER_PROMPT)
    assert str(raised.value) == logins.wrong(0)
    said = capsys.readouterr().err
    assert [logins.wrong(left) in said for left in range(1, logins.MAX_ATTEMPTS)] == [True] * (
        logins.MAX_ATTEMPTS - 1
    )


def test_ctrl_d_at_the_user_password_says_what_was_not_done(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.save(logins.USER, bytearray(DSN.encode()), b"pw")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(cli.CliError) as raised:
        cli.load_credential(store, logins.USER, logins.USER_PROMPT)
    assert str(raised.value) == logins.NOT_ENTERED
    assert run_cli(monkeypatch, store, ["config", "get"], "") == 1
    assert logins.NOT_ENTERED in capsys.readouterr().err
