"""The place file of root's database (`calu.db.place`, D228), with the tests' own uid for root."""

import json
import os
from pathlib import Path

import pytest

from calu.db import place
from calu.db.target import Target
from calu.setup import USER_ROLE

ME = os.getuid()
OWNER = Target.parse("postgresql://calu_owner:secret@127.0.0.1:5433/calu?sslmode=disable")


def test_the_place_is_written_without_any_secret_and_read_as_the_user_login(tmp_path: Path):
    path = tmp_path / "state" / "database.json"
    place.write(OWNER, path)
    text = path.read_text()
    assert "secret" not in text and "calu_owner" not in text
    assert json.loads(text)["conninfo"] == {
        "dbname": "calu",
        "host": "127.0.0.1",
        "port": "5433",
        "sslmode": "disable",
    }
    assert path.stat().st_mode & 0o777 == 0o644
    login = place.read(path, ME)
    assert login.user == USER_ROLE and login.place == "127.0.0.1:5433/calu"
    assert getattr(login.auth, "password", None) == ""


def test_a_place_file_others_could_have_written_is_refused(tmp_path: Path):
    path = tmp_path / "database.json"
    place.write(OWNER, path)
    # The real check asks for root, and this file belongs to the tests.
    with pytest.raises(place.PlaceError, match="not to uid 0"):
        place.read(path)
    path.chmod(0o664)
    with pytest.raises(place.PlaceError, match="writable by its group"):
        place.read(path, ME)
    path.chmod(0o644)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(place.PlaceError, match="writable"):
            place.read(path, ME)
    finally:
        tmp_path.chmod(0o755)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(place.PlaceError, match="no regular file"):
        place.read(link, ME)
    with pytest.raises(place.PlaceError):
        place.read(tmp_path / "missing.json", ME)
    path.write_text('{"calu_place": 99}')
    with pytest.raises(place.PlaceError, match="unknown format"):
        place.read(path, ME)


def test_a_setup_without_a_user_password_shares_the_user_login(tmp_path: Path, monkeypatch):
    """D228, the owner's ruling: without passwords, security is weak, and a client asks for
    nothing (D265: an empty user password).

    `place.write` with an `admin_password` stands for such a setup. Since D279 the login is not
    in the place file that every user reads. It is in a file of mode 600 next to it, which the
    test checks. A client that knows the database and has no password takes that shared login.
    A setup with passwords leaves no file that holds the secret."""
    from calu import cli
    from calu.setup import user_login

    path = tmp_path / "database.json"
    place.write(OWNER, path, admin_password="generated")
    login = place.read(path, ME)
    assert getattr(login.auth, "password", None) == "generated"
    # D279: not in the place file, which every user reads, but in a file of mode 600.
    assert "generated" not in path.read_text()
    secret = place.login_path(path)
    assert secret.stat().st_mode & 0o777 == 0o600 and "generated" in secret.read_text()
    assert not place.holds_password(path)

    # A client that knows the database without a password takes the shared one ...
    monkeypatch.setattr(cli.db_place, "read", lambda: login)
    asked = user_login(OWNER, "")
    filled = cli._with_shared_password(asked)
    assert filled is not None and getattr(filled.auth, "password", None) == "generated"
    # ... but not for another database, and it keeps a password it already has.
    other = user_login(Target.parse("postgresql://u:p@db.example:5432/calu"), "")
    assert cli._with_shared_password(other) is other
    typed = user_login(OWNER, "typed")
    assert cli._with_shared_password(typed) is typed
    # After a setup with passwords no file holds the secret.
    place.write(OWNER, path)
    assert "generated" not in path.read_text() and not secret.exists()
    # A place file of before D279 is still read, and reported as one that holds it.
    import json

    monkeypatch.undo()

    path.write_text(json.dumps(json.loads(path.read_text()) | {"admin_password": "old"}))
    assert getattr(place.read(path, ME).auth, "password", None) == "old"
    assert place.holds_password(path)


def test_commands_use_the_shared_login_and_ask_nothing(tmp_path: Path, monkeypatch):
    """D228: after a setup without passwords, also the commands of a user who cannot read
    root's store find the database in the place file and its login in the login file next to
    it (D279), and they ask for nothing. A place file written without that login shares none."""
    from calu import cli
    from calu.credentials import CredentialStore

    assert cli._shared_user() is None, "the tests never read this machine's own place file"
    path = tmp_path / "shared" / "database.json"
    place.write(OWNER, path, admin_password="generated")
    really = place.read
    monkeypatch.setattr(place, "read", lambda: really(path, ME))
    shared = cli._shared_user()
    assert shared is not None and shared.user == USER_ROLE
    store = CredentialStore(tmp_path / "empty-store.json")
    login = cli._user_target(store, may_ask=False)
    assert login.user == USER_ROLE and getattr(login.auth, "password", "") == "generated"
    # Without a password in the file, nothing is shared.
    place.write(OWNER, path)
    assert cli._shared_user() is None
