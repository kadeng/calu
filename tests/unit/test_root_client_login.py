"""A root client asks the admin password once per run and keeps the narrowest login (D249).

Stand-ins replace the database (`is_admin_password`), the terminal prompt, the switch of
the file identity and the full-screen client; the real paths as root run in the root box
(`tests/root/`).
"""

from pathlib import Path
from typing import Any

import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db.target import PasswordAuth, Target
from calu.tui.start import TuiExit

OWNER = b"postgresql://calu_owner:generated@127.0.0.1:5433/calu"
OPERATOR = b"postgresql://calu_operator:op@127.0.0.1:5433/calu"
PASSWORD = "the admin password"


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer every password prompt with `PASSWORD` and record the prompts.

    Returns:
        The prompts, in the order they were shown.
    """
    asked: list[str] = []

    def answer(prompt: str, confirm: bool = False) -> bytearray:
        asked.append(prompt)
        return bytearray(PASSWORD.encode())

    monkeypatch.setattr(cli, "read_password", answer)
    monkeypatch.setattr(cli, "_place_or_none", lambda: None)
    return asked


def role_accepts(monkeypatch: pytest.MonkeyPatch, accepted: set[str]) -> list[str]:
    """Let `calu_admin` accept the passwords in `accepted`, without a database.

    Returns:
        The passwords that were checked.
    """
    checked: list[str] = []

    async def is_admin_password(place: Target, password: str) -> bool:
        checked.append(password)
        return password in accepted

    monkeypatch.setattr(cli, "is_admin_password", is_admin_password)
    return checked


def password_of(login: Target) -> str:
    """Return the password of a login."""
    assert isinstance(login.auth, PasswordAuth)
    return login.auth.password


def test_the_admin_password_is_asked_once_and_the_admin_login_is_kept(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    store.save("admin", bytearray(OWNER), PASSWORD.encode())
    role_accepts(monkeypatch, {PASSWORD})
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [cli.ADMIN_ONCE_PROMPT]
    held = cli._held[0]
    assert held.user == "calu_admin" and password_of(held) == PASSWORD
    assert cli._admin_place[0].user == "calu_admin"
    # Every later call keeps it and asks nothing.
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [cli.ADMIN_ONCE_PROMPT] and cli._held == [held]


def test_a_blank_admin_password_keeps_the_owner_login_it_opened(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    """PostgreSQL lets no role log in with a blank password; the owner entry opens with it."""
    store.save("admin", bytearray(OWNER), b"")
    monkeypatch.setattr(cli, "read_password", lambda prompt, confirm=False: bytearray())
    monkeypatch.setattr(cli, "_place_or_none", lambda: None)
    checked = role_accepts(monkeypatch, set())
    assert cli._hold_for_client(store, may_ask=True) is None
    assert checked == [""]
    assert cli._held[0].user == "calu_owner"


def test_wrong_passwords_are_asked_again_then_the_client_goes_without_the_database(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    prompts: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    store.save("admin", bytearray(OWNER), b"another one")
    role_accepts(monkeypatch, set())
    assert cli._hold_for_client(store, may_ask=True) == cli.REFUSED_ADMIN
    assert len(prompts) == cli.ADMIN_ATTEMPTS and cli._held == []
    assert capsys.readouterr().err.count("not accepted") == cli.ADMIN_ATTEMPTS


def test_ctrl_d_skips_the_database_features(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save("admin", bytearray(OWNER), PASSWORD.encode())

    def ended(prompt: str, confirm: bool = False) -> bytearray:
        raise cli.CliError("no password was entered (the input ended)")

    monkeypatch.setattr(cli, "read_password", ended)
    assert cli._hold_for_client(store, may_ask=True) == cli.SKIPPED_ADMIN
    assert cli._held == []


def test_an_owner_login_opened_at_the_start_is_narrowed_without_asking(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    """`calu attach --sudo` that found its session in the database asked the password then."""
    cli._held[:] = [Target.parse(OWNER)]
    cli._typed["admin"] = PASSWORD
    role_accepts(monkeypatch, {PASSWORD})
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == []
    assert cli._held[0].user == "calu_admin" and password_of(cli._held[0]) == PASSWORD


def test_an_owner_login_whose_password_the_role_refuses_is_kept(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    cli._held[:] = [Target.parse(OWNER)]
    cli._typed["admin"] = ""
    role_accepts(monkeypatch, set())
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [] and cli._held[0].user == "calu_owner"


def test_a_held_operator_login_is_replaced_by_the_admin_login_of_the_same_database(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    cli._held[:] = [Target.parse(OPERATOR)]
    role_accepts(monkeypatch, {PASSWORD})
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [cli.ADMIN_ONCE_PROMPT]
    assert cli._held[0].user == "calu_admin" and cli._held[0].place == "127.0.0.1:5433/calu"


def test_the_line_client_and_a_password_free_admin_login_ask_nothing(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    store.save("admin", bytearray(OWNER), PASSWORD.encode())
    assert cli._hold_for_client(store, may_ask=False) is None
    assert prompts == [] and cli._held == []
    admin = Target.parse(b"postgresql://calu_admin:generated@127.0.0.1:5433/calu")
    cli._held[:] = [admin]
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [] and cli._held == [admin]


def test_after_setup_no_password_a_held_operator_login_asks_nothing(
    store: CredentialStore, prompts: list[str]
) -> None:
    """`calu start --sudo` holds `calu_operator`; `--no-password` left `calu_admin` open."""
    store.save("admin", bytearray(OWNER), None)
    store.save(cli.ADMIN_ENTRY, bytearray(b"postgresql://calu_admin:gen@127.0.0.1:5433/calu"), None)
    cli._held[:] = [Target.parse(OPERATOR)]
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == []
    assert cli._held[0].user == "calu_admin" and password_of(cli._held[0]) == "gen"
    assert cli._admin_place[0].user == "calu_admin"


def test_without_a_database_to_open_nothing_is_asked(
    store: CredentialStore, prompts: list[str]
) -> None:
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == []


def test_one_password_serves_the_whole_run_across_a_switch(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str], tmp_path: Path
) -> None:
    """The owner's decision: one admin password per run of a root client, no second dialog.

    The client is given the held login complete, after `/switch` as before it; the token of
    the session it left is revoked with the same login, as `calu_operator`.
    """
    store.save("admin", bytearray(OWNER), PASSWORD.encode())
    role_accepts(monkeypatch, {PASSWORD})
    monkeypatch.setattr(cli, "_become_client_of", lambda run_as: None)
    monkeypatch.setattr(cli.tui_start, "wants_full_screen", lambda plain: True)
    given: list[Any] = []
    exits = [
        TuiExit(code=0, state="idle", claude_session_id=None, open_decisions=0, switch_to="b"),
        TuiExit(code=0, state="idle", claude_session_id=None, open_decisions=0),
    ]

    def run_tui(address: str, token: str, after_seq: int = 0, **kwargs: Any) -> TuiExit:
        given.append(kwargs["database"])
        return exits.pop(0)

    monkeypatch.setattr(cli.tui_start, "run_tui", run_tui)
    following = cli.Endpoint("unix:/run/calu/sessions/b.sock", "", None, "b")
    monkeypatch.setattr(cli, "_following", lambda store, endpoint, session_id: following)
    revoked: list[tuple[int, str | None]] = []

    def herd_value(store: Any, work: Any, may_ask: bool = True, role: Any = None) -> None:
        assert cli._held and cli._held[0].user == "calu_admin"
        revoked.append((7, role))

    monkeypatch.setattr(cli, "_herd_value", herd_value)
    run_as = cli.RunAs(uid=1000, gid=1000, home=str(tmp_path), user="ada")
    cli._attach(cli.Endpoint("ws://127.0.0.1:1", "t", 7, "a"), store, False, 0, False, run_as)
    assert prompts == [cli.ADMIN_ONCE_PROMPT]
    assert len(given) == 2 and given[0] == given[1]
    assert given[0].user == "calu_admin" and password_of(given[0]) == PASSWORD
    assert revoked == [(7, "calu_operator")]
