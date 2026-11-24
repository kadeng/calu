"""A root client asks the user password once per run and keeps the user login (D249, D265).

Stand-ins replace the database (`is_user_password`), the terminal prompt, the switch of
the file identity and the full-screen client; the real paths as root run in the root box
(`tests/root/`). The tests touch only a credential store in a temporary directory and print
nothing beyond what `calu.cli` prints on stderr.
"""

from pathlib import Path
from typing import Any

import pytest

from calu import cli, logins
from calu.credentials import CredentialStore
from calu.db.target import PasswordAuth, Target
from calu.tui.start import TuiExit

PASSWORD = "user-pw"
"""The calu user password of these tests."""

USER = b"postgresql://calu_admin:user-pw@127.0.0.1:5433/calu"
"""The `user` login root's store holds: `calu_admin` with the user password."""

PLACE = Target.parse(b"postgresql://calu_admin@127.0.0.1:5433/calu")
"""Root's place file without a secret: where the database is."""


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer every password prompt with `PASSWORD`, record the prompts; no place file.

    Returns:
        The prompts, in the order they were shown.
    """
    asked: list[str] = []

    def answer(prompt: str, confirm: bool = False) -> bytearray:
        asked.append(prompt)
        return bytearray(PASSWORD.encode())

    monkeypatch.setattr(cli, "read_password", answer)
    monkeypatch.setattr(cli, "_place_or_none", lambda: None)
    monkeypatch.setattr(cli, "_shared_user", lambda: None)
    return asked


def role_accepts(monkeypatch: pytest.MonkeyPatch, accepted: set[str]) -> list[str]:
    """Let `calu_admin` accept the passwords in `accepted`, without a database.

    Returns:
        The passwords that were checked.
    """
    checked: list[str] = []

    async def is_user_password(place: Target, password: str) -> bool:
        checked.append(password)
        return password in accepted

    monkeypatch.setattr(cli, "is_user_password", is_user_password)
    return checked


def password_of(login: Target) -> str:
    """Return the password of a login."""
    assert isinstance(login.auth, PasswordAuth)
    return login.auth.password


def test_the_user_password_is_asked_once_and_the_user_login_is_kept(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    store.save(logins.USER, bytearray(USER), PASSWORD.encode())
    checked = role_accepts(monkeypatch, set())
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [logins.ONCE_PROMPT]
    assert checked == [], "root's `user` entry opened; the database was not asked"
    held = cli._held[0]
    assert held == Target.parse(USER)
    assert cli._admin_place[0].user == "calu_admin" and password_of(cli._admin_place[0]) == ""
    # Every later call keeps it and asks nothing.
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [logins.ONCE_PROMPT] and cli._held == [held]


def test_without_a_user_entry_the_database_checks_the_typed_password(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str]
) -> None:
    monkeypatch.setattr(cli, "_place_or_none", lambda: PLACE)
    checked = role_accepts(monkeypatch, {PASSWORD})
    assert cli._hold_for_client(store, may_ask=True) is None
    assert checked == [PASSWORD]
    assert cli._held == [cli.user_login(PLACE, PASSWORD)]


def test_wrong_passwords_are_asked_again_then_the_client_goes_without_the_database(
    monkeypatch: pytest.MonkeyPatch,
    store: CredentialStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each refused answer but the last says the tries left; a blank one is called blank."""
    store.save(logins.USER, bytearray(USER), PASSWORD.encode())
    monkeypatch.setattr(cli, "_place_or_none", lambda: None)
    monkeypatch.setattr(cli, "_shared_user", lambda: None)
    answers = ["", *["wrong"] * (logins.MAX_ATTEMPTS - 1)]
    monkeypatch.setattr(
        cli, "read_password", lambda prompt, confirm=False: bytearray(answers.pop(0).encode())
    )
    assert cli._hold_for_client(store, may_ask=True) == cli.REFUSED_PASSWORD
    assert answers == [] and cli._held == []
    said = capsys.readouterr().err
    assert f"[client] {logins.wrong(logins.MAX_ATTEMPTS - 1, blank=True)}" in said
    for left in range(1, logins.MAX_ATTEMPTS - 1):
        assert f"[client] {logins.wrong(left)}" in said
    assert said.count("[client]") == logins.MAX_ATTEMPTS - 1
    assert logins.FORGOT in cli.REFUSED_PASSWORD


def test_ctrl_d_skips_the_database_features(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore
) -> None:
    store.save(logins.USER, bytearray(USER), PASSWORD.encode())
    monkeypatch.setattr(cli, "_shared_user", lambda: None)

    def ended(prompt: str, confirm: bool = False) -> bytearray:
        raise cli.CliError(logins.NOT_ENTERED)

    monkeypatch.setattr(cli, "read_password", ended)
    assert cli._hold_for_client(store, may_ask=True) == cli.SKIPPED_PASSWORD
    assert cli._held == []


def test_the_line_client_asks_nothing_and_a_password_free_user_login_is_kept(
    store: CredentialStore, prompts: list[str]
) -> None:
    store.save(logins.USER, bytearray(USER), PASSWORD.encode())
    assert cli._hold_for_client(store, may_ask=False) is None
    assert prompts == [] and cli._held == []
    # An empty user password: the entry is stored without one and kept, also by the line
    # client, without a prompt.
    free = b"postgresql://calu_admin:gen@127.0.0.1:5433/calu"
    store.save(logins.USER, bytearray(free), None)
    assert cli._hold_for_client(store, may_ask=False) is None
    assert prompts == [] and cli._held == [Target.parse(free)]
    assert cli._admin_place[0].user == "calu_admin"


def test_a_held_login_is_kept_and_nothing_is_asked(
    store: CredentialStore, prompts: list[str]
) -> None:
    held = Target.parse(b"postgresql://calu_admin:held@127.0.0.1:5433/calu")
    cli._held[:] = [held]
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == [] and cli._held == [held]


def test_without_a_database_to_open_nothing_is_asked(
    store: CredentialStore, prompts: list[str]
) -> None:
    assert cli._hold_for_client(store, may_ask=True) is None
    assert prompts == []


def test_one_password_serves_the_whole_run_across_a_switch(
    monkeypatch: pytest.MonkeyPatch, store: CredentialStore, prompts: list[str], tmp_path: Path
) -> None:
    """The owner's decision: one user password per run of a root client, no second dialog.

    The client is given the held login complete, after `/switch` as before it; the token of
    the session it left is revoked with the same login, without a prompt.
    """
    store.save(logins.USER, bytearray(USER), PASSWORD.encode())
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
    revoked: list[tuple[int, str, bool]] = []

    def herd_value(store: Any, work: Any, may_ask: bool = True) -> None:
        revoked.append((7, cli._held[0].user, may_ask))

    monkeypatch.setattr(cli, "_herd_value", herd_value)
    run_as = cli.RunAs(uid=1000, gid=1000, home=str(tmp_path), user="ada")
    cli._attach(cli.Endpoint("ws://127.0.0.1:1", "t", 7, "a"), store, False, 0, False, run_as)
    assert prompts == [logins.ONCE_PROMPT]
    assert len(given) == 2 and given[0] == given[1]
    assert given[0].user == "calu_admin" and password_of(given[0]) == PASSWORD
    assert revoked == [(7, "calu_admin", False)]
