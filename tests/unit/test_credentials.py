import json
import stat

import pytest

from calu.credentials import (
    CorruptStore,
    CredentialStore,
    UnknownCredential,
    WrongPassword,
    default_path,
    wipe,
)


def test_save_and_load_round_trip(store: CredentialStore) -> None:
    store.save("admin", "postgresql://u:p@h/db", bytearray(b"secret"))
    assert store.load("admin", "secret") == bytearray(b"postgresql://u:p@h/db")
    assert store.names() == ["admin"]
    assert "admin" in store
    assert "session" not in store


def test_wrong_password_and_unknown_name(store: CredentialStore) -> None:
    store.save("admin", "x", "right")
    with pytest.raises(WrongPassword):
        store.load("admin", "wrong")
    with pytest.raises(UnknownCredential):
        store.load("session", "right")


def test_file_permissions_and_no_plaintext(store: CredentialStore) -> None:
    store.save("admin", "postgresql://owner:TOPSECRET@h/db", "pw")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert "TOPSECRET" not in store.path.read_text()


def test_entry_bound_to_its_name(store: CredentialStore) -> None:
    store.save("admin", "x", "pw")
    data = json.loads(store.path.read_text())
    data["entries"]["session"] = data["entries"]["admin"]
    store.path.write_text(json.dumps(data))
    with pytest.raises(WrongPassword):
        store.load("session", "pw")


def test_replace_and_remove_keep_other_entries(store: CredentialStore) -> None:
    store.save("admin", "a1", "pw")
    store.save("session", "s", "pw2")
    store.save("admin", "a2", "pw3")
    assert store.load("admin", "pw3") == bytearray(b"a2")
    assert store.load("session", "pw2") == bytearray(b"s")
    assert store.remove("admin")
    assert not store.remove("admin")
    assert store.names() == ["session"]


def test_corrupt_file(store: CredentialStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    with pytest.raises(CorruptStore):
        store.names()
    store.path.write_text('{"version": 99, "entries": {}}')
    with pytest.raises(CorruptStore):
        store.names()


def test_empty_name_or_password_rejected(store: CredentialStore) -> None:
    with pytest.raises(ValueError):
        store.save("", "x", "pw")
    with pytest.raises(ValueError):
        store.save("admin", "x", "")


def test_wipe_zeroes_in_place() -> None:
    buf = bytearray(b"secret")
    wipe(buf)
    assert buf == bytearray(6)


def test_default_path_uses_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert str(default_path()) == "/tmp/xdg/calu/credentials.json"
