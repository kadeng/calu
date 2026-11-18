"""`calu login`: Claude Code's own login, run as the user Claude Code runs as in sessions.

The tests start nothing: they check the command line calu builds, and the refusals. As root
for another user the command goes through calu's launcher, which was measured as root in a
container (spikes/smoke_privileged.sh).
"""

import os

import pytest

from calu import cli
from calu.cli import CliError, build_parser, claude_login_command
from calu.launch import LAUNCH_ENV, LaunchSpec, RunAs, launcher_path


def test_as_calus_own_user_claude_auth_runs_directly(monkeypatch):
    monkeypatch.setattr(cli, "claude_path", lambda configured: "/opt/claude")
    command, env = claude_login_command("login", "console", None, {"PATH": "/bin"})
    assert command == ["/opt/claude", "auth", "login", "--console"]
    assert LAUNCH_ENV not in env
    status, _ = claude_login_command("status", None, None, {})
    assert status == ["/opt/claude", "auth", "status", "--text"]


def test_for_another_user_the_launcher_runs_it_with_that_users_home(monkeypatch):
    monkeypatch.setattr(cli, "claude_path", lambda configured: "/opt/claude")
    run_as = RunAs(uid=1000, gid=1000, home="/var/lib/calu/claude/1000")
    command, env = claude_login_command("login", None, run_as, {"PATH": "/bin"})
    assert command == [str(launcher_path()), "auth", "login"]
    spec = LaunchSpec.parse(env[LAUNCH_ENV])
    assert (spec.uid, spec.home, spec.claude) == (1000, "/var/lib/calu/claude/1000", "/opt/claude")


def test_contradictory_options_are_refused():
    parser = build_parser()
    for argv in (["login", "--status", "--logout"], ["login", "console", "--status"]):
        with pytest.raises(CliError):
            cli.cmd_login(parser.parse_args(argv), None)  # type: ignore[arg-type]


def test_as_root_the_user_must_be_named(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(CliError, match="say which user Claude Code runs as"):
        cli.cmd_login(build_parser().parse_args(["login"]), None)  # type: ignore[arg-type]


def test_a_failed_login_is_reported(monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("as root the user must be named")
    monkeypatch.setattr(cli, "claude_path", lambda configured: "/opt/claude")
    calls: list[list[str]] = []

    def fake_call(command, env=None):
        calls.append(command)
        return 3

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    with pytest.raises(CliError, match="not logged in for you; `calu login` logs it in"):
        cli.cmd_login(build_parser().parse_args(["login", "--status"]), None)  # type: ignore[arg-type]
    assert calls == [["/opt/claude", "auth", "status", "--text"]]
    with pytest.raises(CliError, match="claude auth login for you ended with exit code 3"):
        cli.cmd_login(build_parser().parse_args(["login"]), None)  # type: ignore[arg-type]
