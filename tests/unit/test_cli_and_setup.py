import io
import json
from pathlib import Path

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
