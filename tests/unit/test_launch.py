"""The launcher that starts Claude Code as another user (`calu.launch`, D207)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig
from calu.launch import (
    LAUNCH_ENV,
    REFUSED,
    SCRIPT,
    LaunchError,
    LaunchSpec,
    RunAs,
    become,
    environment,
    launcher_path,
    main,
)


def own(claude: str = "/usr/bin/env", **changes) -> LaunchSpec:
    """Return a record for the user the tests run as.

    Args:
        claude: The executable to start in place of Claude Code.
        **changes: Fields that differ.

    Returns:
        The record.
    """
    fields = {"uid": os.getuid(), "gid": os.getgid(), "home": "/state", "claude": claude}
    return LaunchSpec(**(fields | changes))


def test_the_environment_is_built_from_an_empty_one():
    given = {
        "PATH": "/usr/bin",
        "LC_ALL": "C.UTF-8",
        "ANTHROPIC_API_KEY": "key",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "SUDO_UID": "1000",
        "AWS_PROFILE": "calu-db",
        "PGPASSWORD": "secret",
        "MISTRAL_API_KEY": "",
        # Hidden by calu, which empties what it cannot remove: absent for Claude Code.
        "ANTHROPIC_BASE_URL": "",
        LAUNCH_ENV: "{}",
    }
    assert environment(own(user="ada"), given) == {
        "PATH": "/usr/bin",
        "LC_ALL": "C.UTF-8",
        "ANTHROPIC_API_KEY": "key",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "HOME": "/state",
        "USER": "ada",
        "LOGNAME": "ada",
    }


def test_a_uid_without_a_name_gets_no_user_variable():
    result = environment(own(), {"USER": "root", "LOGNAME": "root", "PATH": "/bin"})
    assert result == {"PATH": "/bin", "HOME": "/state"}


def test_a_record_with_an_unknown_field_is_refused():
    text = json.dumps({"uid": 1, "gid": 1, "home": "/h", "claude": "/c", "shell": "/bin/sh"})
    with pytest.raises(LaunchError, match="no valid launch record"):
        LaunchSpec.parse(text)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may change to any user")
def test_without_root_only_the_own_user_is_accepted():
    become(own())
    with pytest.raises(LaunchError, match="only root"):
        become(own(uid=os.getuid() + 1))
    with pytest.raises(LaunchError, match="only root"):
        become(own(gid=os.getgid() + 1))


def test_without_a_record_the_launcher_refuses(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["-v"], {"PATH": "/usr/bin"})
    assert stopped.value.code == REFUSED
    assert "started by calu, not by hand" in capsys.readouterr().err


def test_a_missing_executable_is_reported(capsys):
    with pytest.raises(SystemExit) as stopped:
        main([], {LAUNCH_ENV: own(claude="/nonexistent/claude").dump()})
    assert stopped.value.code == REFUSED
    assert "cannot execute Claude Code" in capsys.readouterr().err


def test_the_installed_launcher_replaces_itself_with_the_named_program():
    """The console script, as the Agent SDK would start it: arguments pass, variables do not."""
    given = {"PATH": os.environ["PATH"], "PGPASSWORD": "secret", LAUNCH_ENV: own().dump()}
    done = subprocess.run(
        [str(launcher_path()), "--", "printenv"],
        env=given,
        capture_output=True,
        text=True,
        check=True,
    )
    names = {line.split("=", 1)[0] for line in done.stdout.splitlines()}
    # Python itself may add LC_CTYPE at its start; nothing else comes through.
    assert names - {"LC_CTYPE"} == {"PATH", "HOME"}
    assert "HOME=/state" in done.stdout


def test_the_launcher_is_next_to_the_interpreter():
    assert launcher_path() == Path(sys.executable).parent / SCRIPT


def test_a_runner_for_another_user_starts_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CALU_CLAUDE_PATH", "/opt/claude")  # D279: this machine's, not the db's
    run_as = RunAs(uid=1000, gid=1000, home="/var/lib/calu/claude/1000")
    runner = SdkRunner(tmp_path, ClaudeConfig(), run_as=run_as)
    options = runner._options(None, None)  # type: ignore[arg-type]
    assert options.cli_path == str(launcher_path())
    spec = LaunchSpec.parse(options.env[LAUNCH_ENV])
    assert (spec.uid, spec.gid, spec.groups, spec.home, spec.user, spec.claude) == (
        1000,
        1000,
        (),
        "/var/lib/calu/claude/1000",
        None,
        "/opt/claude",
    )
    # The SDK's own `user` option changes the uid only (spikes/RESULTS.md): never used.
    assert options.user is None


def test_a_runner_for_the_same_user_starts_claude_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CALU_CLAUDE_PATH", raising=False)
    options = SdkRunner(tmp_path, ClaudeConfig())._options(None, None)  # type: ignore[arg-type]
    assert options.cli_path is None
    assert LAUNCH_ENV not in options.env


DSN = "postgresql://calu_owner@127.0.0.1:55490/calu"
"""A value for `CALU_TEST_DSN`, as `claude.pass_env` passes it (no secret in it)."""


def test_pass_env_reaches_claude_code_of_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`claude.pass_env`: the named variables join the kept ones, with calu's values."""
    monkeypatch.setenv("CALU_CLAUDE_PATH", "/usr/bin/env")
    run_as = RunAs(uid=os.getuid(), gid=os.getgid(), home="/state")
    config = ClaudeConfig(pass_env=["CALU_TEST_DSN"])
    runner = SdkRunner(tmp_path, config, run_as=run_as)
    env = runner._claude_env()
    spec = LaunchSpec.parse(env[LAUNCH_ENV])
    assert spec.keep[-1] == "CALU_TEST_DSN" and "ANTHROPIC_*" in spec.keep
    given = {"PATH": os.environ["PATH"], "CALU_TEST_DSN": DSN, "PGPASSWORD": "x"} | env
    built = environment(spec, given)
    assert built["CALU_TEST_DSN"] == DSN and "PGPASSWORD" not in built

    # The installed launcher, as the Agent SDK starts it with calu's environment.
    done = subprocess.run(
        [str(launcher_path()), "--", "printenv", "CALU_TEST_DSN"],
        env=given,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == DSN

    # Without the setting the variable stays behind.
    plain = SdkRunner(tmp_path, ClaudeConfig(), run_as=run_as)
    kept = LaunchSpec.parse(plain._claude_env()[LAUNCH_ENV])
    assert "CALU_TEST_DSN" not in environment(kept, given)


def test_the_claude_executable_is_this_machines_and_no_database_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D279: a `claude.cli_path` that a database login wrote no longer picks the program."""
    from calu.config import Config, claude_cli_path

    stored = Config.model_validate({"claude": {"cli_path": "/home/someone/not-claude"}})
    assert not hasattr(stored.claude, "cli_path")
    monkeypatch.delenv("CALU_CLAUDE_PATH", raising=False)
    assert claude_cli_path() is None
    monkeypatch.setenv("CALU_CLAUDE_PATH", " /opt/claude ")
    assert claude_cli_path() == "/opt/claude"
