"""`calu config models` and `calu setup` choosing a model (D129), against a real database."""

import io
from typing import cast

import anyio
import pytest

from calu import admin, cli
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.model_choice import ModelChoice
from calu.setup import setup

pytestmark = [pytest.mark.db, pytest.mark.anyio]


async def _run(store: CredentialStore, argv: list[str]) -> int:
    """Run the CLI in a worker thread, as it runs for real.

    Args:
        store: The credential store.
        argv: The arguments after the program name.

    Returns:
        The exit code.
    """
    return await anyio.to_thread.run_sync(
        lambda: cli.main(argv, store_factory=lambda: cast(CredentialStore, store))
    )


async def test_one_model_for_every_role_reaches_both_places_it_is_kept(
    db_url: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The configuration's role keys **and** every checker definition, or `honesty` lags behind.

    D51 makes the configuration win over a definition, but an extra checker on a route takes
    its model from its definition alone (D81), so a command that set only the keys would leave
    the honesty checker on the old model with nothing saying so.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "mistral:mistral-small-2603")
        store = CredentialStore(tmp_path / "credentials.json")
        store.save("admin", db_url, None)

        assert await _run(store, ["config", "models", "claude:haiku", "--effort", "low"]) == 0
        said = capsys.readouterr().out
        assert "every model role of the global scope now uses claude:haiku" in said

        db = Db(conn)
        stored = await admin.settings(db)
        for key in admin.MODEL_KEYS:
            section, field = key.split(".")
            assert stored[section][field] == "claude:haiku", key
        assert stored["command_checker"]["effort"] == "low"
        assert {row.model for row in await admin.checkers(db)} == {"claude:haiku"}, (
            "every definition, the honesty checker's included"
        )

        # Switching models removes an effort that was chosen for the old one.
        assert await _run(store, ["config", "models", "fireworks:accounts/x/models/y"]) == 0
        capsys.readouterr()
        stored = await admin.settings(db)
        assert "effort" not in stored.get("command_checker", {}), (
            "'low' chosen for Haiku must not reach another model as a thinking setting"
        )
    finally:
        await conn.close()


async def test_setup_chooses_the_model_on_a_fresh_database_only(
    db_url: str, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second `calu setup` must not overwrite the models someone chose since."""
    monkeypatch.setattr(
        cli,
        "detect_model",
        lambda _environ: ModelChoice(
            "Anthropic", "anthropic:claude-haiku-5-5", "medium", "ANTHROPIC_API_KEY is set"
        ),
    )
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("admin", db_url, None)

    monkeypatch.setattr("sys.stdin", io.StringIO("pw\npw\n"))
    assert await _run(store, ["setup"]) == 0
    said = capsys.readouterr().out
    assert "anthropic:claude-haiku-5-5, effort medium (Anthropic: ANTHROPIC_API_KEY" in said

    conn = await connect(db_url)
    try:
        db = Db(conn)
        assert (await admin.settings(db))["context"]["narrator_model"] == (
            "anthropic:claude-haiku-5-5"
        )
        # Someone chooses another model; setup runs again and leaves it alone.
        await admin.set_all_models(db, "claude:sonnet")
        monkeypatch.setattr("sys.stdin", io.StringIO("pw\npw\n"))
        assert await _run(store, ["setup"]) == 0
        capsys.readouterr()
        assert (await admin.settings(db))["context"]["narrator_model"] == "claude:sonnet"
    finally:
        await conn.close()
