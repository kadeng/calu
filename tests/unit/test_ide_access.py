"""Which login `calu ide` takes for a request (`calu.ide.access`, D265), without a database.

Every test uses a credential store in `tmp_path` and no place file (`shared_login` and
`db_place.read` are patched; a test that needs root's place file patches its own). Nothing
connects anywhere: the choice of the login is checked, and where a test runs a request or an
unlock, the connection (`open_stored`) or the password check (`is_user_password`) is a stand-in.
"""

from pathlib import Path
from typing import Any

import psycopg
import pytest

from calu import logins
from calu.credentials import CredentialStore, KdfParams, wipe
from calu.db.target import Target
from calu.ide import access as access_module
from calu.ide.access import NEEDS_PASSWORD, NO_DATABASE, Access, NeedsPassword, NoCredential

pytestmark = pytest.mark.anyio

TEST_KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)
"""Cheap key derivation, so that the protected entries open fast."""

USER = "postgresql://calu_admin:gen@db.example:5432/calu"
"""The login the `user` entry holds in these tests."""

OLD = {
    "admin": "postgresql://calu_owner:own@db.example:5432/calu",
    "session": "postgresql://calu_session:s@db.example:5432/calu",
    "operator": "postgresql://calu_operator:op@db.example:5432/calu",
}
"""Entries of a store written before D265."""


@pytest.fixture(autouse=True)
def no_place_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's place file (`/var/lib/calu/database.json`) out of every choice."""
    monkeypatch.setattr(access_module, "shared_login", lambda: None)

    def no_file(path: Path | None = None, owner: int = 0) -> Target:
        raise access_module.db_place.PlaceError("no place file in this test")

    monkeypatch.setattr(access_module.db_place, "read", no_file)


def root_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let root's place file name db.example without a secret (`sudo calu setup` with a user
    password)."""
    place = Target.parse("postgresql://calu_admin@db.example:5432/calu")
    monkeypatch.setattr(access_module.db_place, "read", lambda path=None, owner=0: place)


def store_with(tmp_path: Path, **entries: str | None) -> CredentialStore:
    """Return a store in `tmp_path` holding the named entries.

    Args:
        tmp_path: The test's directory.
        **entries: Entry name ("user" or one of `OLD`) to its password, or None for an
            entry without one. "user" holds `USER`, the others their login in `OLD`.
    """
    store = CredentialStore(tmp_path / "credentials.json", kdf=TEST_KDF)
    for name, password in entries.items():
        secret = USER if name == logins.USER else OLD[name]
        store.save(name, secret, None if password is None else bytearray(password, "utf-8"))
    return store


def text(login: Target | bytearray) -> str:
    """Return a chosen login as its URL text, wiping a secret read from the store."""
    if isinstance(login, bytearray):
        value = login.decode()
        wipe(login)
        return value
    return str(login.conninfo)


def needs(error: NeedsPassword) -> str:
    """Return the login a `NeedsPassword` error asks the password of."""
    assert isinstance(error.data, dict)
    return error.data["needs"]


async def test_a_held_login_comes_first(tmp_path: Path):
    """After `ide.unlock` the held login serves every request, whatever the store holds."""
    access = Access(store_with(tmp_path, user="pw"))
    held = Target.parse(USER)
    access._held = held
    assert await access._login(None) == (held, None)
    assert access.unlocked() == [logins.USER] and not access.locked() and access.can_change()


async def test_a_place_file_with_a_password_comes_before_the_user_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty user password as root (`sudo calu setup --no-password`): the place file
    publishes the user login, and nothing is asked."""
    shared = Target.parse("postgresql://calu_admin:published@db.example:5432/calu")
    monkeypatch.setattr(access_module, "shared_login", lambda: shared)
    access = Access(store_with(tmp_path, user="pw"))
    assert await access._login(None) == (shared, None)
    assert not access.locked() and access.can_read()


async def test_a_user_entry_without_a_password_is_used_as_it_is(tmp_path: Path):
    access = Access(store_with(tmp_path, user=None))
    login, typed = await access._login(None)
    assert (text(login), typed) == (USER, None)
    assert not access.locked() and access.can_read() and access.can_change()
    assert access.place() == "db.example:5432/calu"


async def test_a_protected_user_entry_opens_with_the_user_password(tmp_path: Path):
    """A missing, a wrong and a blank password are each told apart (`calu.logins.refused`)."""
    access = Access(store_with(tmp_path, user="pw"))
    assert access.locked() and not access.can_read()
    with pytest.raises(NeedsPassword) as asked:
        await access._login(None)
    assert asked.value.code == NEEDS_PASSWORD and needs(asked.value) == logins.USER
    assert asked.value.data == {"needs": logins.USER, "unlocked": []}
    assert "calu user password" in asked.value.message
    with pytest.raises(NeedsPassword) as wrong:
        await access._login("not it")
    assert wrong.value.message == logins.refused(blank=False)
    with pytest.raises(NeedsPassword) as blank:
        await access._login("")
    assert blank.value.message == logins.refused(blank=True)
    login, typed = await access._login("pw")
    assert (text(login), typed) == (USER, None), "a stored login: the password opened the entry"


async def test_unlock_holds_the_user_login_after_the_right_password(tmp_path: Path):
    access = Access(store_with(tmp_path, user="pw"))
    with pytest.raises(NeedsPassword) as wrong:
        await access.unlock("not it")
    assert wrong.value.message == logins.refused(blank=False)
    with pytest.raises(NeedsPassword) as blank:
        await access.unlock("")
    assert blank.value.message == logins.refused(blank=True)
    assert access.unlocked() == []
    answer = await access.unlock("pw")
    assert answer == {"unlocked": [logins.USER], "reading": True, "changing": True}
    login, typed = await access._login(None)
    assert (str(login.conninfo) if isinstance(login, Target) else "", typed) == (
        str(Target.parse(USER).conninfo),
        None,
    )


@pytest.mark.parametrize(
    ("entries", "has_owner"), [(("admin", "session"), True), (("operator",), False)]
)
async def test_only_the_logins_of_an_earlier_calu_is_no_database_with_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: tuple[str, ...], has_owner: bool
):
    """D265 (5): the old entries are not used; the message says how to convert or remove
    them (`calu.logins.old_logins`), also where root's place file names a database."""
    root_place(monkeypatch)
    access = Access(store_with(tmp_path, **dict.fromkeys(entries)))
    for attempt in (access._login(None), access._login("pw"), access.unlock("pw")):
        with pytest.raises(NoCredential) as refused:
            await attempt
        assert refused.value.code == NO_DATABASE
        assert refused.value.message == logins.old_logins(has_owner)


async def test_with_only_roots_place_file_the_password_logs_in_as_calu_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A user of root's calu: no entry of their own, so the editor asks; the typed password
    is the password of `calu_admin` at the place file's database, and nothing is stored."""
    root_place(monkeypatch)
    store = CredentialStore(tmp_path / "credentials.json", kdf=TEST_KDF)
    access = Access(store)
    assert access.locked() and not access.can_read()
    with pytest.raises(NeedsPassword) as asked:
        await access._login(None)
    assert needs(asked.value) == logins.USER
    login, typed = await access._login("typed")
    assert isinstance(login, Target) and typed == "typed"
    assert login.conninfo.get("user") == "calu_admin" and login.conninfo.get("host") == "db.example"
    assert getattr(login.auth, "password", None) == "typed"
    assert logins.USER not in store

    async def is_user_password(place: Target, password: str) -> bool:
        """Accept "the user password" only."""
        return password == "the user password"

    monkeypatch.setattr(access_module, "is_user_password", is_user_password)
    with pytest.raises(NeedsPassword) as wrong:
        await access.unlock("wrong")
    assert wrong.value.message == logins.refused(blank=False)
    with pytest.raises(NeedsPassword) as blank:
        await access.unlock("")
    assert blank.value.message == logins.refused(blank=True)
    assert (await access.unlock("the user password"))["changing"] is True
    assert not access.locked() and access.unlocked() == [logins.USER]
    held, typed = await access._login(None)
    assert isinstance(held, Target) and typed is None
    assert held.conninfo.get("user") == "calu_admin"
    assert getattr(held.auth, "password", None) == "the user password"


async def test_no_login_at_all_is_no_database_with_the_setup_command(tmp_path: Path):
    access = Access(CredentialStore(tmp_path / "credentials.json", kdf=TEST_KDF))
    assert not access.locked() and not access.can_read() and access.place() == ""
    for attempt in (access._login(None), access._login("pw"), access.unlock("pw")):
        with pytest.raises(NoCredential) as refused:
            await attempt
        assert refused.value.code == NO_DATABASE
        assert refused.value.message == logins.no_login()


def refusing_database(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Make every connection fail as PostgreSQL refuses a password; return the logins tried."""
    tried: list[Any] = []

    async def refuse(login: Any, start_local: bool = True) -> Any:
        """Refuse the login as PostgreSQL does after its password was changed."""
        tried.append(login)
        raise psycopg.OperationalError('password authentication failed for user "calu_admin"')

    monkeypatch.setattr(access_module, "open_stored", refuse)
    return tried


async def nothing(db: Any) -> None:
    """The work of a request that never runs here."""
    raise AssertionError("the work ran although the connection was refused")


async def test_a_stored_login_the_database_refuses_is_called_outdated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The entry opened, so the refusal is no wrong password: `calu.logins.outdated`."""
    tried = refusing_database(monkeypatch)
    access = Access(store_with(tmp_path, user=None))
    for run in (access.read(nothing), access.change(nothing)):
        with pytest.raises(access_module.RpcError) as refused:
            await run
        assert not isinstance(refused.value, NeedsPassword)
        assert refused.value.code == NO_DATABASE
        assert refused.value.message == logins.outdated("db.example:5432/calu")
    assert len(tried) == 2


async def test_a_typed_password_the_database_refuses_is_a_wrong_or_blank_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only root's place file: the database checks the typed password itself."""
    root_place(monkeypatch)
    refusing_database(monkeypatch)
    access = Access(CredentialStore(tmp_path / "credentials.json", kdf=TEST_KDF))
    with pytest.raises(NeedsPassword) as wrong:
        await access.read(nothing, "wrong")
    assert wrong.value.message == logins.refused(blank=False)
    with pytest.raises(NeedsPassword) as blank:
        await access.change(nothing, "")
    assert blank.value.message == logins.refused(blank=True)
