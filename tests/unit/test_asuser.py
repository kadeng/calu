"""calu's own work as the unprivileged user (`calu.asuser`, D206): what needs no root.

The switch itself needs root and is measured in a container (`spikes/smoke_privileged.sh`,
`spikes/spike_fs_identity.py`). These tests touch no file outside `tmp_path` and print nothing.
"""

import argparse
import getpass
import os
from pathlib import Path

import pytest

from calu import asuser
from calu.cli import CliError, _run_as, build_parser
from calu.launch import RunAs


def test_a_number_is_a_bare_uid_with_a_home_from_calu(tmp_path: Path):
    assert asuser.resolve("1000", tmp_path) == RunAs(
        uid=1000, gid=1000, home=str(tmp_path / "claude" / "1000")
    )
    assert asuser.resolve("1000:100", tmp_path).gid == 100
    # Also for a uid that has a name here: the rule does not depend on the machine.
    assert asuser.resolve(str(os.getuid()), tmp_path).user is None
    with pytest.raises(asuser.UserError, match="numeric gid"):
        asuser.resolve("1000:staff", tmp_path)


def test_a_name_brings_its_own_home_and_groups():
    me = asuser.resolve(getpass.getuser())
    assert (me.uid, me.gid, me.user) == (os.getuid(), os.getgid(), getpass.getuser())
    assert me.home == str(Path.home())
    assert me.gid not in me.groups
    with pytest.raises(asuser.UserError, match="no such user"):
        asuser.resolve("no-such-user-on-this-machine")


def test_a_named_user_without_a_home_directory_is_refused(tmp_path: Path):
    ghost = RunAs(uid=os.getuid(), gid=os.getgid(), home=str(tmp_path / "gone"), user="ghost")
    with pytest.raises(asuser.UserError, match="does not exist"):
        asuser.prepare(ghost, tmp_path)


def test_the_directories_of_a_bare_uid_are_private(tmp_path: Path):
    """As the user the tests run as, so the change of owner is one to itself."""
    me = RunAs(uid=os.getuid(), gid=os.getgid(), home=str(tmp_path / "claude" / "me"))
    asuser.prepare(me, tmp_path)
    for directory in (Path(me.home), asuser.state_home(me, tmp_path)):
        assert directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700


def test_a_program_starts_as_before_while_nothing_was_switched():
    assert asuser.active() is None
    assert asuser.child() == {}
    assert asuser.child({"A": "1"}) == {"env": {"A": "1"}}


def test_after_the_switch_a_program_gets_the_user_and_a_built_environment(monkeypatch):
    user = RunAs(uid=1000, gid=1000, home="/var/lib/calu/claude/1000", groups=(44,))
    monkeypatch.setattr(asuser, "_active", user)
    given = {
        "PATH": "/usr/bin",
        "GIT_LITERAL_PATHSPECS": "1",
        "MISTRAL_API_KEY": "secret",
        "PGPASSWORD": "secret",
        "HOME": "/root",
        "CLAUDE_CONFIG_DIR": "/root/.claude",
    }
    assert asuser.child(given) == {
        "user": 1000,
        "group": 1000,
        "extra_groups": [44],
        "env": {
            "PATH": "/usr/bin",
            "GIT_LITERAL_PATHSPECS": "1",
            "HOME": "/var/lib/calu/claude/1000",
        },
    }


def options(*arguments: str) -> argparse.Namespace:
    """Parse a `calu run` command line.

    Args:
        *arguments: The arguments after `run`.

    Returns:
        The parsed arguments.
    """
    return build_parser().parse_args(["run", *arguments])


@pytest.mark.skipif(os.geteuid() == 0, reason="these are the rules for a calu that is not root")
def test_without_root_only_the_own_user_can_be_named():
    assert _run_as(options()) is None
    assert _run_as(options("--user", getpass.getuser())) is None
    with pytest.raises(CliError, match="needs calu to run as root"):
        _run_as(options("--user", "1"))


def test_as_root_the_user_must_be_named(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(CliError, match="say which user Claude Code runs as"):
        _run_as(options())
    # Root itself, when named: one user, as without the option.
    assert _run_as(options("--user", "0")) is None
    assert build_parser().parse_args(["serve", "--user", "1000:1000"]).user == "1000:1000"


def test_root_started_through_sudo_always_uses_the_system_directories():
    through_sudo = {"SUDO_UID": "1000", "XDG_CONFIG_HOME": "/home/ada/.config"}
    asuser.use_system_directories(through_sudo, 0)
    assert (
        through_sudo["XDG_CONFIG_HOME"] == "/etc" and through_sudo["XDG_STATE_HOME"] == "/var/lib"
    )
    chosen = {"XDG_CONFIG_HOME": "/srv/calu-config"}
    asuser.use_system_directories(chosen, 0)
    assert chosen == {"XDG_CONFIG_HOME": "/srv/calu-config", "XDG_STATE_HOME": "/var/lib"}
    ordinary = {"HOME": "/home/ada"}
    asuser.use_system_directories(ordinary, 1000)
    assert ordinary == {"HOME": "/home/ada"}


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the temporary files belong to root")
def test_an_installation_somebody_else_can_change_is_reported(tmp_path: Path):
    mine = tmp_path / "calu"
    mine.write_text("#!/bin/sh\n")
    problems = asuser.writable_by_others([mine, tmp_path / "missing"])
    assert any(f"{mine} belongs to uid {os.getuid()}" in problem for problem in problems)
    assert any("missing" in problem for problem in problems)
    # What the system installs for root passes.
    assert asuser.writable_by_others([Path("/usr/bin/env")]) == []


def test_the_sudo_command_names_the_invoking_user():
    command = asuser.sudo_command(["run", "--sudo", "-p", "hi"], "ada", Path("/usr/local/bin/calu"))
    assert command == [
        "sudo",
        "--",
        "/usr/local/bin/calu",
        "run",
        "-p",
        "hi",
        "--user",
        "ada",
        "--from-sudo",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="--sudo is for a calu that is not root")
def test_sudo_is_refused_for_an_installation_the_user_can_change():
    """The checkout the tests run from belongs to the user, so elevation must be refused."""
    with pytest.raises(CliError, match="can be changed without root"):
        _run_as(options("--sudo"))
    with pytest.raises(CliError, match="takes no --user"):
        _run_as(options("--sudo", "--user", "ada"))


def test_the_user_passed_through_sudo_must_be_the_one_who_ran_it(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "4242")
    with pytest.raises(CliError, match="not the user who ran sudo"):
        _run_as(options("--user", "1000", "--from-sudo"))


def test_without_an_admin_entry_settings_use_the_typed_admin_password(tmp_path, monkeypatch):
    """Only an `operator` entry is stored: the login is `calu_admin` at that database."""
    from calu import cli
    from calu.credentials import CredentialStore
    from calu.db.target import PasswordAuth, Target

    store = CredentialStore(tmp_path / "credentials.json")
    store.save("operator", "host=db.example port=5432 dbname=calu user=calu_operator", None)
    monkeypatch.setattr(cli, "read_password", lambda prompt, confirm=False: bytearray(b"typed"))
    login = Target.parse(cli._admin_secret(store))
    assert login.conninfo["user"] == "calu_admin" and login.conninfo["host"] == "db.example"
    assert login.auth == PasswordAuth(password="typed")
    assert "admin" not in store, "nothing was stored"
