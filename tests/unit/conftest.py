"""Fixtures of the unit layer."""

from pathlib import Path

import pytest

from calu.claude.sdk_runner import SdkRunner


@pytest.fixture(autouse=True)
def no_user_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """Keep the SDK runner from reading the `~/.claude/settings.json` of whoever runs the tests.

    The runner inspects that file for sandbox entries before it starts and before a command
    runs. A unit test must give the same result on every machine, so the runner is pointed at
    an empty directory; a test that wants a user settings file sets `runner.home` itself.

    Returns:
        The empty directory that stands for the home directory.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(SdkRunner, "home", home)
    return home
