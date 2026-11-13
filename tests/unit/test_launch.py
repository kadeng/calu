"""The launcher that starts Claude Code as another user (`calu.claude.launch`, D207)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from calu.claude.launch import (
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
from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig


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


def test_a_runner_for_another_user_starts_the_launcher(tmp_path: Path):
    run_as = RunAs(uid=1000, gid=1000, home="/var/lib/calu/claude/1000")
    runner = SdkRunner(tmp_path, ClaudeConfig(cli_path="/opt/claude"), run_as=run_as)
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


def test_a_runner_for_the_same_user_starts_claude_itself(tmp_path: Path):
    options = SdkRunner(tmp_path, ClaudeConfig())._options(None, None)  # type: ignore[arg-type]
    assert options.cli_path is None
    assert LAUNCH_ENV not in options.env
