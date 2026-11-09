import io

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
