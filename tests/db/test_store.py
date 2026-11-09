import json
import logging
from pathlib import Path

import anyio
import psycopg
import pytest
from pydantic import ValidationError

from calu.checkers import Step
from calu.db import Db, connect
from calu.setup import setup
from calu.store import ConfigStore, ProjectSettings

pytestmark = [pytest.mark.db, pytest.mark.anyio]

WORKSPACE = Path("/w")


async def prepared(db_url: str) -> psycopg.AsyncConnection:
    """Open an owner connection on a migrated database with the default checkers and rules."""
    conn = await connect(db_url)
    await setup(conn, db_url, "test:model")
    return conn


async def add_project(conn: psycopg.AsyncConnection, name: str) -> int:
    cur = await conn.execute("INSERT INTO projects (name) VALUES (%s) RETURNING id", (name,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def set_settings(
    conn: psycopg.AsyncConnection, scope: str, scope_id: str, value: object
) -> None:
    await conn.execute(
        "INSERT INTO config (scope, scope_id, key, value) VALUES (%s, %s, 'settings', %s)"
        " ON CONFLICT (scope, scope_id, key) DO UPDATE SET value = excluded.value",
        (scope, scope_id, json.dumps(value)),
    )


async def test_load_returns_defaults_of_a_fresh_database(db_url: str) -> None:
    conn = await prepared(db_url)
    try:
        settings = await ConfigStore(Db(conn), WORKSPACE).load()
    finally:
        await conn.close()
    assert sorted(settings.checkers) == [
        "changeset",
        "command",
        "edit",
        "narrator",
        "subagent-result",
    ]
    assert settings.config.command_checker.model == "test:model"
    assert settings.config.edit_checker.model == "test:model"
    assert settings.config.subagent_checker.model == "test:model"
    assert settings.config.context.narrator_model == "test:model"
    assert settings.instructions("command").startswith("#")
    assert settings.instructions("nonexistent", "fallback") == "fallback"
    assert settings.checkers["command"].version == 1
    # The default rules are in force: reading a .env file asks the human.
    outcome = settings.rules.evaluate(
        Step(tool_name="Read", tool_input={"file_path": "/w/.env"}, cwd="")
    )
    assert outcome.ask and not outcome.allowed


async def test_project_settings_override_global_ones(db_url: str) -> None:
    conn = await prepared(db_url)
    try:
        await add_project(conn, "demo")
        await set_settings(conn, "global", "", {"claude": {"model": "sonnet", "effort": "low"}})
        await set_settings(conn, "project", "demo", {"claude": {"model": "haiku"}})
        await set_settings(conn, "project", "other", {"claude": {"model": "opus"}})
        global_only = await ConfigStore(Db(conn), WORKSPACE).load()
        project = await ConfigStore(Db(conn), WORKSPACE, "demo").load()
    finally:
        await conn.close()
    assert global_only.config.claude.model == "sonnet"
    assert project.config.claude.model == "haiku"
    assert project.config.claude.effort == "low"  # kept from the global layer


async def test_rules_combine_global_and_project_and_skip_disabled(db_url: str) -> None:
    conn = await prepared(db_url)
    try:
        project = await add_project(conn, "demo")
        other = await add_project(conn, "other")
        await conn.execute("DELETE FROM rules")
        await conn.execute(
            "INSERT INTO rules (project_id, list, pattern, position, enabled)"
            " VALUES (NULL, 'command-deny', '^global-rule$', 0, true),"
            "        (NULL, 'command-deny', '^disabled$', 1, false),"
            "        (%s, 'command-deny', '^project-rule$', 0, true),"
            "        (%s, 'command-deny', '^other-rule$', 0, true)",
            (project, other),
        )
        settings = await ConfigStore(Db(conn), WORKSPACE, "demo").load()
        global_only = await ConfigStore(Db(conn), WORKSPACE).load()
    finally:
        await conn.close()
    patterns = [rule.pattern.pattern for rule in settings.rules.lists("command-deny")[0]]
    assert patterns == ["^global-rule$", "^project-rule$"]
    assert [r.pattern.pattern for r in global_only.rules.lists("command-deny")[0]] == [
        "^global-rule$"
    ]
    assert settings.rules.lists("command-deny")[0][0].source.startswith("command-deny#")


async def test_disabled_checker_type_is_left_out(db_url: str) -> None:
    conn = await prepared(db_url)
    try:
        await conn.execute("UPDATE checker_types SET enabled = false WHERE name = 'edit'")
        await conn.execute(
            "UPDATE checker_types SET model = 'other:model', max_tokens = 123"
            " WHERE name = 'command'"
        )
        settings = await ConfigStore(Db(conn), WORKSPACE).load()
    finally:
        await conn.close()
    assert "edit" not in settings.checkers
    assert settings.config.command_checker.model == "other:model"
    assert settings.config.command_checker.max_tokens == 123
    assert settings.config.edit_checker.model == "mistral:mistral-small-2603"  # the default
    assert settings.checkers["command"].version == 2  # the trigger bumped it
    assert settings.checkers["command"].limits().max_tokens == 123


async def test_the_session_role_can_load_the_settings(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        _, session_url = await setup(conn, db_url, "test:model")
    finally:
        await conn.close()
    session = await connect(session_url)
    try:
        settings = await ConfigStore(Db(session), WORKSPACE).load()
    finally:
        await session.close()
    assert settings.instructions("edit")


async def test_invalid_stored_settings_raise(db_url: str) -> None:
    conn = await prepared(db_url)
    try:
        await set_settings(conn, "global", "", {"context": {"fold_every": 0}})
        with pytest.raises(ValidationError):
            await ConfigStore(Db(conn), WORKSPACE).load()
        await set_settings(conn, "global", "", ["not", "an", "object"])
        settings = await ConfigStore(Db(conn), WORKSPACE).load()  # ignored, with a warning
    finally:
        await conn.close()
    assert settings.config.context.fold_every == 30


async def test_watch_reloads_after_a_change(db_url: str) -> None:
    conn = await prepared(db_url)
    listener = await connect(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    seen: list[ProjectSettings] = []
    reloaded = anyio.Event()

    async def on_change(settings: ProjectSettings) -> None:
        seen.append(settings)
        reloaded.set()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(store.watch, listener, on_change, 0.01)
            await anyio.sleep(0.2)  # let the listener subscribe before the change
            await set_settings(conn, "global", "", {"claude": {"model": "haiku"}})
            with anyio.move_on_after(10):
                await reloaded.wait()
            tg.cancel_scope.cancel()
    finally:
        await conn.close()
        await listener.close()
    assert seen and seen[-1].config.claude.model == "haiku"


async def test_watch_survives_a_broken_configuration(
    db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    conn = await prepared(db_url)
    listener = await connect(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    reloaded = anyio.Event()

    async def on_change(settings: ProjectSettings) -> None:
        reloaded.set()

    try:
        with caplog.at_level(logging.WARNING, logger="calu.store"):
            async with anyio.create_task_group() as tg:
                tg.start_soon(store.watch, listener, on_change, 0.01)
                await anyio.sleep(0.2)
                await set_settings(conn, "global", "", {"context": {"fold_every": 0}})
                await anyio.sleep(1.0)
                assert not reloaded.is_set()
                await set_settings(conn, "global", "", {"claude": {"model": "haiku"}})
                with anyio.move_on_after(10):
                    await reloaded.wait()
                tg.cancel_scope.cancel()
    finally:
        await conn.close()
        await listener.close()
    assert reloaded.is_set()
    assert any("reloading the configuration failed" in r.getMessage() for r in caplog.records)
