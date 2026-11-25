"""The password of `calu web` where the calu user password is empty (D279): the stored hash,
`calu web --set-password`, and how `calu web` chooses its login."""

import io
import json
import stat
from pathlib import Path
from typing import cast

import anyio
import pytest

from calu import cli
from calu.db.target import Target
from calu.web import password as web_password


@pytest.fixture
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point calu's configuration directory at a temporary one.

    Returns:
        The directory `web-password.json` goes into.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "calu"


def test_the_file_holds_only_a_salted_hash_readable_by_its_owner(config_dir: Path) -> None:
    web_password.store("s3cret")
    path = config_dir / web_password.FILE_NAME
    assert web_password.default_path() == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    text = path.read_text()
    assert "s3cret" not in text
    data = json.loads(text)
    assert data["kdf"] == web_password.KDF
    assert data["rounds"] == 4096  # CALU_SCRAM_ITERATIONS of the tests (conftest.py)
    stored = web_password.load()
    assert stored is not None and not stored.empty
    assert stored.matches("s3cret")
    assert not stored.matches("s3cret ")
    assert not stored.matches("")
    # A second store of the same password gets a new salt.
    web_password.store("s3cret")
    assert json.loads(path.read_text())["salt"] != data["salt"]


def test_an_empty_choice_matches_anything_and_a_missing_file_is_no_choice(config_dir: Path) -> None:
    assert web_password.load() is None
    web_password.store("")
    assert json.loads((config_dir / web_password.FILE_NAME).read_text()) == {"empty": True}
    stored = web_password.load()
    assert stored is not None and stored.empty and stored.matches("whatever")


def test_a_file_of_another_format_is_refused(config_dir: Path) -> None:
    config_dir.mkdir()
    path = config_dir / web_password.FILE_NAME
    for text in ("[]", '{"kdf": "md5", "rounds": 1}', '{"kdf": "pbkdf2-sha256"}'):
        path.write_text(text)
        with pytest.raises(ValueError):
            web_password.load()


def test_store_replaces_a_symbolic_link_instead_of_writing_through_it(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    link = tmp_path / "calu" / web_password.FILE_NAME
    link.parent.mkdir()
    link.symlink_to(target)
    web_password.store("pw", link)
    assert target.read_text() == "keep"
    assert not link.is_symlink()
    stored = web_password.load(link)
    assert stored is not None and stored.matches("pw")


PLACE = cast(Target, object())
"""The user login; `_web_login` only binds it into the check of the user password."""


def test_calu_web_signs_in_with_the_user_password_where_there_is_one() -> None:
    check, how = cli._web_login(True, PLACE, (None, "would not be read"))
    assert check is not None
    assert how == "sign in with the calu user password"


def test_calu_web_without_a_user_password_needs_a_chosen_one(config_dir: Path) -> None:
    with pytest.raises(cli.CliError, match="calu web --set-password"):
        cli._web_login(False, PLACE, cli._load_web_password())
    config_dir.mkdir()
    (config_dir / web_password.FILE_NAME).write_text("not json")
    stored = cli._load_web_password()
    assert stored[0] is None and "cannot be used" in stored[1]
    with pytest.raises(cli.CliError, match="cannot be used"):
        cli._web_login(False, PLACE, stored)


def test_calu_web_checks_the_chosen_password(config_dir: Path) -> None:
    web_password.store("pw")
    check, how = cli._web_login(False, PLACE, cli._load_web_password())
    assert how == "sign in with the password of calu web"

    async def both() -> tuple[bool, bool]:
        return await check("pw"), await check("wrong")

    assert anyio.run(both) == (True, False)


def test_an_empty_web_password_means_no_login_and_a_warning(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    web_password.store("")
    assert cli._web_login(False, PLACE, cli._load_web_password()) == (None, "no password")
    assert "WARNING: calu web asks for no password" in capsys.readouterr().err


def test_set_password_asks_twice_and_stores_the_hash(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\npw\n"))
    cli._set_web_password()
    stored = web_password.load()
    assert stored is not None and stored.matches("pw")
    assert str(config_dir / web_password.FILE_NAME) in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\nother\n"))
    with pytest.raises(cli.CliError):
        cli._set_web_password()


def test_set_password_leaves_calu_web_open_only_after_a_yes(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_password.store("before")
    # Without a terminal the question takes its default, "no".
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n"))
    with pytest.raises(cli.CliError, match="nothing was changed"):
        cli._set_web_password()
    stored = web_password.load()
    assert stored is not None and stored.matches("before") and not stored.empty
    monkeypatch.setattr(cli, "ask", lambda prompt, default, flag: "yes")
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n"))
    cli._set_web_password()
    stored = web_password.load()
    assert stored is not None and stored.empty


def test_the_pane_line_carries_no_secret() -> None:
    line = cli._pane_line("calu.localhost", 8800, "https")
    assert line.startswith("chat pane: https://calu.localhost:8800/pane ")
    assert "token" not in line and "asks for the password" in line
