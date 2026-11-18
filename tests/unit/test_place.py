"""The place file of root's database (`calu.db.place`, D228), with the tests' own uid for root."""

import json
import os
from pathlib import Path

import pytest

from calu.db import place
from calu.db.target import Target
from calu.setup import ADMIN_ROLE

ME = os.getuid()
OWNER = Target.parse("postgresql://calu_owner:secret@127.0.0.1:5433/calu?sslmode=disable")


def test_the_place_is_written_without_any_secret_and_read_as_the_admin_login(tmp_path: Path):
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
    assert login.user == ADMIN_ROLE and login.place == "127.0.0.1:5433/calu"
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


def test_a_setup_without_passwords_shares_the_admin_login(tmp_path: Path, monkeypatch):
    """D228, the owner's ruling: without passwords, security is weak, and a client asks for
    nothing. The login is then in the file every user can read."""
    from calu import cli
    from calu.setup import admin_login

    path = tmp_path / "database.json"
    place.write(OWNER, path, admin_password="generated")
    login = place.read(path, ME)
    assert getattr(login.auth, "password", None) == "generated"

    # A client that knows the database without a password takes the shared one ...
    monkeypatch.setattr(cli.db_place, "read", lambda: login)
    asked = admin_login(OWNER, "")
    filled = cli._with_shared_password(asked)
    assert filled is not None and getattr(filled.auth, "password", None) == "generated"
    # ... but not for another database, and it keeps a password it already has.
    other = admin_login(Target.parse("postgresql://u:p@db.example:5432/calu"), "")
    assert cli._with_shared_password(other) is other
    typed = admin_login(OWNER, "typed")
    assert cli._with_shared_password(typed) is typed
    # After a setup with passwords the file holds no secret again.
    place.write(OWNER, path)
    assert "generated" not in path.read_text()
