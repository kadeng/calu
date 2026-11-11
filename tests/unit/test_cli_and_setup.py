import io
import json
from pathlib import Path
from typing import Any

import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db import migrations
from calu.setup import RULE_LISTS, default_rules, parse_rule_file


def test_parse_rule_file_attaches_adjacent_comments_only() -> None:
    text = "# Header about the file\n\n# No sudo\n^sudo( |$)\n^eval( |$)\n\n# dropped\n"
    rows = parse_rule_file("command-deny", text)
    assert [(r.pattern, r.comment, r.position) for r in rows] == [
        ("^sudo( |$)", "No sudo", 0),
        ("^eval( |$)", "", 1),
    ]


def test_default_rules_cover_all_lists() -> None:
    rows = default_rules()
    assert {r.list for r in rows} == set(RULE_LISTS)
    assert any(r.pattern.startswith("^git( -[Cc] \\S+)* push") for r in rows)


def test_migrations_are_found_in_order() -> None:
    found = migrations()
    assert found[0].version == 1
    assert [m.version for m in found] == sorted(m.version for m in found)
    assert "CREATE TABLE sessions" in found[0].sql


def run_cli(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, argv: list[str], stdin: str
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(argv, store_factory=lambda: store)


def test_credential_set_list_remove(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["credential", "set", "admin"], "dsn\npw\npw\n") == 0
    assert store.load("admin", "pw") == bytearray(b"dsn")
    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "admin" in capsys.readouterr().out
    assert run_cli(monkeypatch, store, ["credential", "remove", "admin"], "") == 0
    assert run_cli(monkeypatch, store, ["credential", "remove", "admin"], "") == 1


def test_mismatched_confirmation_is_an_error(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, store, ["credential", "set", "admin"], "dsn\npw\nother\n") == 1
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
    """Read-and-resolve commands take the narrow credential when there is one (PLAN.md D48)."""
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

    # Commands that change the configuration keep asking for `admin` only.
    for argv in (["config", "get"], ["checker", "list"], ["rules", "list"], ["project", "list"]):
        assert run_cli(monkeypatch, store, argv, "") == 1
        assert "no credential 'admin'" in capsys.readouterr().err


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
                "claude-9",
                "--fork-before-turn",
                "3",
                "--model",
                "opus",
                "--effort",
                "high",
                "--batch",
                "--thinking",
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
    assert options.resume_id == "claude-9"
    assert options.fork_before_turn == 3
    assert options.model == "opus"
    assert options.effort == "high"
    assert options.interactive is False  # --batch
    assert options.show_thinking is True  # --thinking
    assert options.socket_path is None  # no --socket: a TCP port


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
        run_cli(monkeypatch, store, ["credential", "set", "operator", "--no-password"], "dsn\n")
        == 0
    )
    printed = capsys.readouterr().out
    assert "only the file's mode protects it" in printed
    assert str(store.path) in printed, "the warning has to name the file that now holds it"

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.load_credential(store, "operator", "Operator password: ") == bytearray(b"dsn")

    assert run_cli(monkeypatch, store, ["credential", "list"], "") == 0
    assert "operator  (file mode only)" in capsys.readouterr().out

    # Setting it again without the switch puts a password back on it.
    assert run_cli(monkeypatch, store, ["credential", "set", "operator"], "dsn\npw\npw\n") == 0
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
        run_cli(monkeypatch, store, ["credential", "set", "operator", "--no-password"], "dsn\n")
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
