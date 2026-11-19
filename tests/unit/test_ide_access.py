"""Which stored login `calu ide` takes for a request (`calu.ide.access`), without a database.

Every test uses a credential store in `tmp_path` and no place file (`shared_login` is
patched); nothing connects anywhere, because only the choice of the login is checked.
"""

from pathlib import Path

import pytest

from calu.credentials import CredentialStore, wipe
from calu.db.target import Target
from calu.ide import access as access_module
from calu.ide.access import NEEDS_PASSWORD, NO_DATABASE, Access, NeedsPassword

pytestmark = pytest.mark.anyio

OPERATOR = "postgresql://calu_operator:op@db.example:5432/calu"
ADMIN = "postgresql://calu_owner:own@db.example:5432/calu"
CALU_ADMIN = "postgresql://calu_admin:gen@db.example:5432/calu"


@pytest.fixture(autouse=True)
def no_place_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's place file (`/var/lib/calu/database.json`) out of every choice."""
    monkeypatch.setattr(access_module, "shared_login", lambda: None)


def store_with(tmp_path: Path, **entries: str | None) -> CredentialStore:
    """Return a store in `tmp_path` holding OPERATOR, ADMIN or CALU_ADMIN under the given names.

    Args:
        tmp_path: The test's directory.
        **entries: Entry name ("operator", "admin", "calu_admin") to its password, or None
            for an entry without one.
    """
    store = CredentialStore(tmp_path / "credentials.json")
    secrets = {"operator": OPERATOR, "admin": ADMIN, "calu_admin": CALU_ADMIN}
    for name, password in entries.items():
        store.save(name, secrets[name], None if password is None else bytearray(password, "utf-8"))
    return store


def text(login: Target | bytearray) -> str:
    """Return a chosen login as its URL text, wiping a secret read from the store."""
    if isinstance(login, bytearray):
        value = login.decode()
        wipe(login)
        return value
    return str(login.conninfo)


def needs(error: NeedsPassword) -> str:
    """Return the credential name a `NeedsPassword` error asks for."""
    assert isinstance(error.data, dict)
    return error.data["needs"]


async def test_reading_takes_the_narrowest_password_free_entry(tmp_path: Path):
    access = Access(store_with(tmp_path, operator=None, admin=None))
    login, name = await access._reading_login(None)
    assert (name, text(login)) == ("operator", OPERATOR)


async def test_reading_prefers_a_password_free_admin_over_a_protected_operator(tmp_path: Path):
    access = Access(store_with(tmp_path, operator="pw", admin=None))
    _login, name = await access._reading_login(None)
    assert name == "admin"


async def test_a_protected_operator_alone_asks_for_its_password(tmp_path: Path):
    access = Access(store_with(tmp_path, operator="pw"))
    with pytest.raises(NeedsPassword) as asked:
        await access._reading_login(None)
    assert asked.value.code == NEEDS_PASSWORD and needs(asked.value) == "operator"
    with pytest.raises(NeedsPassword) as wrong:
        await access._reading_login("not it")
    assert "wrong password" in wrong.value.message
    login, name = await access._reading_login("pw")
    assert (name, text(login)) == ("operator", OPERATOR)


async def test_changing_never_uses_the_operator_entry_as_it_is(tmp_path: Path):
    """Settings change through calu_admin or admin; the operator login is never enough."""
    access = Access(store_with(tmp_path, operator=None, calu_admin=None, admin=None))
    login, name, typed = await access._changing_login(None)
    assert (name, text(login), typed) == ("calu_admin", CALU_ADMIN, False)

    access = Access(store_with(tmp_path / "b", operator=None, admin="pw"))
    with pytest.raises(NeedsPassword) as asked:
        await access._changing_login(None)
    assert needs(asked.value) == "admin"
    login, name, typed = await access._changing_login("pw")
    assert (name, text(login), typed) == ("admin", ADMIN, False)


async def test_with_only_an_operator_the_password_is_taken_as_calu_admins(tmp_path: Path):
    """The typed password logs in as `calu_admin` at the operator's database; nothing is stored."""
    store = store_with(tmp_path, operator=None)
    access = Access(store)
    with pytest.raises(NeedsPassword) as asked:
        await access._changing_login(None)
    assert needs(asked.value) == "admin"
    login, name, typed = await access._changing_login("the admin password")
    assert name == "admin" and typed is True
    assert isinstance(login, Target)
    assert login.conninfo.get("user") == "calu_admin"
    assert login.conninfo.get("host") == "db.example"
    assert "admin" not in store and "calu_admin" not in store


async def test_no_entry_at_all_is_no_database(tmp_path: Path):
    access = Access(CredentialStore(tmp_path / "credentials.json"))
    for choose in (access._reading_login, access._changing_login):
        with pytest.raises(access_module.RpcError) as refused:
            await choose(None)
        assert refused.value.code == NO_DATABASE
