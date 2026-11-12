"""Finding ci1: the `hidden_env` setting reaches the runner that starts Claude Code."""

import json
from pathlib import Path

import pytest

from calu.claude import FakeRunner, FakeScript
from calu.config import PROVIDER_KEYS, SETTINGS_KEY
from calu.db import connect
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.testing import fake_models

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def test_the_runner_is_told_to_hide_the_configured_variables(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calu config add hidden_env X` had no effect: only the fixed provider keys were hidden."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-env-file"))
    owner = await connect(db_url)
    try:
        _, session_url = await setup(owner, db_url, "test:model")
        settings = {"claude": {"sandbox": {"enabled": False}}, "hidden_env": ["OPENROUTER_API_KEY"]}
        await owner.execute(
            "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
            (SETTINGS_KEY, json.dumps(settings)),
        )
    finally:
        await owner.close()
    hidden: list[set[str]] = []

    def runner_factory(workspace: Path, _config: object, names: set[str]) -> FakeRunner:
        """Record the names the runner is asked to hide.

        Args:
            workspace: The session's workspace.
            _config: The Claude configuration (unused).
            names: The hidden environment names.

        Returns:
            The fake runner.
        """
        hidden.append(set(names))
        return FakeRunner(FakeScript(turns=[[{"text": "Done."}]]), workspace)

    code = await run_session(
        session_url,
        RunOptions(workspace=tmp_path, prompt="have a look", interactive=False),
        runner_factory=runner_factory,
        checker_models=fake_models(),
    )
    assert code == 0
    assert hidden and hidden[0] == PROVIDER_KEYS | {"OPENROUTER_API_KEY"}
