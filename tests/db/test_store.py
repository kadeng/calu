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
    # D179: no rules are shipped, so reading a .env file goes to the checker (not free, not
    # asked by a rule); calu's own folder stays protected by the built-in rule.
    outcome = settings.rules.evaluate(
        Step(tool_name="Read", tool_input={"file_path": "/w/.env"}, cwd="")
    )
    assert not outcome.ask and not outcome.allowed
    own = settings.rules.evaluate(
        Step(
            tool_name="Read",
            tool_input={"file_path": "/home/u/.config/calu/credentials.json"},
            cwd="",
        )
    )
    assert own.ask


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


# ------------------------------------------------------------------ unknown project (ga3)


async def test_a_project_name_that_no_row_has_is_an_error(db_url: str) -> None:
    """ga3: falling back to the global settings would silently drop the project's rules."""
    from calu.store import UnknownProject

    conn = await prepared(db_url)
    try:
        await add_project(conn, "payments")
        with pytest.raises(UnknownProject, match=r"payment'.*registered: payments") as caught:
            await ConfigStore(Db(conn), WORKSPACE, "payment").load()
        assert caught.value.known == ["payments"]
    finally:
        await conn.close()


# ------------------------------------------------------------------ reloading (sd2)


class Reloads:
    """Collects what `reload_loop` reports."""

    def __init__(self) -> None:
        self.seen: list[ProjectSettings] = []
        self.errors: list[tuple[Exception, int]] = []
        self.looked = 0

    async def unchanged(self) -> None:
        self.looked += 1

    async def on_change(self, settings: ProjectSettings) -> None:
        self.seen.append(settings)

    async def on_error(self, exc: Exception, failures: int) -> None:
        self.errors.append((exc, failures))

    async def until(self, done, seconds: float = 10) -> None:
        with anyio.fail_after(seconds):
            while not done():
                await anyio.sleep(0.01)


async def test_a_change_without_a_notification_is_found_by_the_periodic_reload(
    db_url: str,
) -> None:
    """sd2: a lost notification must not leave a session on the old rules for the night."""
    from calu.db import Dirty

    conn = await prepared(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    reloads = Reloads()
    try:
        await store.load()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                store.reload_loop, Dirty(), reloads.on_change, 0.01, None, 0.05, reloads.unchanged
            )
            # Nothing changed: the periodic re-read stays silent.
            await anyio.sleep(0.3)
            assert reloads.seen == []
            # But it says that it looked, so a caller can re-read what the rows do not cover.
            assert reloads.looked > 0
            await set_settings(conn, "global", "", {"auto_escalate": {"commands": True}})
            await reloads.until(lambda: reloads.seen)
            tg.cancel_scope.cancel()
    finally:
        await conn.close()
    assert reloads.seen[-1].config.auto_escalate.commands is True


async def test_a_change_made_before_the_subscription_is_found_once_listening(
    db_url: str,
) -> None:
    """sd2: the window between the first load and LISTEN, and every reconnect of the listener."""
    from calu.db import CONFIG_CHANGED, Dirty, Notifier

    conn = await prepared(db_url)
    listener = await connect(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    reloads = Reloads()
    try:
        await store.load()
        await set_settings(conn, "global", "", {"claude": {"model": "haiku"}})  # not heard
        dirty = Dirty()
        notifier = Notifier(listener)
        notifier.on(CONFIG_CHANGED, lambda _payload: dirty.set())
        notifier.on_listening(dirty.hint)
        async with anyio.create_task_group() as tg:
            tg.start_soon(notifier.run)
            tg.start_soon(store.reload_loop, dirty, reloads.on_change, 0.01, None, 3600)
            await reloads.until(lambda: reloads.seen)
            # A hint that finds nothing new does not make the session re-apply its settings.
            dirty.hint()
            await anyio.sleep(0.3)
            assert len(reloads.seen) == 1
            tg.cancel_scope.cancel()
    finally:
        await conn.close()
        await listener.close()
    assert reloads.seen[0].config.claude.model == "haiku"


async def test_a_failed_reload_is_reported_and_retried(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sd2: one failed attempt used to be the last one until the next notification."""
    from pydantic import ValidationError

    from calu.db import Dirty
    from calu.store import config as config_module

    monkeypatch.setattr(config_module, "RELOAD_RETRY_FIRST_S", 0.02)
    conn = await prepared(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    reloads = Reloads()
    try:
        await store.load()
        dirty = Dirty()
        async with anyio.create_task_group() as tg:
            tg.start_soon(store.reload_loop, dirty, reloads.on_change, 0.01, reloads.on_error, 3600)
            # Invalid stored settings: every attempt fails, and each is reported with its count.
            await set_settings(conn, "global", "", {"context": {"fold_every": 0}})
            dirty.set()
            await reloads.until(lambda: len(reloads.errors) >= 3)
            assert [count for _, count in reloads.errors[:3]] == [1, 2, 3]
            assert isinstance(reloads.errors[0][0], ValidationError)
            assert reloads.seen == []
            # Repaired, and applied by the retry alone: no further notification is sent.
            await set_settings(conn, "global", "", {"claude": {"model": "haiku"}})
            await reloads.until(lambda: reloads.seen)
            tg.cancel_scope.cancel()
    finally:
        await conn.close()
    assert reloads.seen[-1].config.claude.model == "haiku"


async def test_a_slow_database_does_not_end_the_reload_loop(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sd2: `TimeoutError` left the loop, and with it the session's task group."""
    from calu.db import Dirty
    from calu.store import config as config_module

    monkeypatch.setattr(config_module, "RELOAD_RETRY_FIRST_S", 0.02)
    conn = await prepared(db_url)
    store = ConfigStore(Db(conn), WORKSPACE)
    reloads = Reloads()
    real_load = store.load
    calls = 0

    async def slow_once() -> ProjectSettings:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("the database did not answer within 30 s")
        return await real_load()

    monkeypatch.setattr(store, "load", slow_once)
    try:
        dirty = Dirty()
        async with anyio.create_task_group() as tg:
            tg.start_soon(store.reload_loop, dirty, reloads.on_change, 0.01, reloads.on_error, 3600)
            dirty.set()
            await reloads.until(lambda: reloads.seen)
            tg.cancel_scope.cancel()
    finally:
        await conn.close()
    assert [type(exc) for exc, _ in reloads.errors] == [TimeoutError]
