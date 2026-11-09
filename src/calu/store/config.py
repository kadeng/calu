"""Configuration, rules and checker instructions, read from the database (PLAN.md D6).

Everything that steers the checkers lives in the database, out of reach of Claude Code and of
the commands it runs. A session loads it once at start and reloads it when the database
announces a change:

    config rows  (scope "global", then "project", key "settings")  ─┐
    checker_types rows (enabled)                                   ─┼─► ProjectSettings
    rules rows  (global, then project, by position)                ─┘      .config    (Config)
                                                                          .checkers  (by name)
    NOTIFY config_changed  ──►  ConfigStore.watch  ──► reload ──►         .rules      (Rules)

`ConfigStore.load` needs read access only; the role `calu_session` has it. Nothing in this
module writes.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import psycopg
from pydantic import ValidationError

from calu.checkers.rules import Rules
from calu.config import SETTINGS_KEY, CheckerConfig, Config, build_config
from calu.db import Db
from calu.db.notify import CONFIG_CHANGED, Dirty, Notifier

log = logging.getLogger(__name__)

RELOAD_DEBOUNCE_S = 0.25
"""Quiet time after a `config_changed` notification before the settings are reloaded, so that
one change of many rows (e.g. a replaced rule list) causes one reload."""

NARRATOR_CHECKER = "narrator"
"""Name of the checker type whose instructions the narrator uses."""


@dataclass(frozen=True)
class CheckerDefinition:
    """One row of `checker_types`: what a checker agent is and how it is run.

    `version` is raised by the database on every change of the definition (trigger
    `checker_types_version`), so a `checker_runs` row records which version decided.
    """

    name: str
    route: str
    instructions: str
    model: str
    request_limit: int
    output_tokens_limit: int
    max_tokens: int
    version: int

    def limits(self) -> CheckerConfig:
        """Return the model and limits of this definition as a `CheckerConfig`.

        Returns:
            A `CheckerConfig` with this definition's model and limits.
        """
        return CheckerConfig(
            model=self.model,
            request_limit=self.request_limit,
            output_tokens_limit=self.output_tokens_limit,
            max_tokens=self.max_tokens,
        )


@dataclass(frozen=True)
class ProjectSettings:
    """Everything a session reads from the database before and during a run.

    `config` holds the merged settings (see `calu.config.build_config`), `checkers` the enabled
    checker definitions by name, and `rules` the allow and deny lists of the project.
    """

    config: Config
    checkers: Mapping[str, CheckerDefinition] = field(default_factory=dict)
    rules: Rules = field(default_factory=lambda: Rules(Path.cwd()))

    def instructions(self, name: str, default: str = "") -> str:
        """Return the instructions of one checker type.

        Args:
            name: Checker type name, e.g. "command", "edit", "subagent-result", "narrator".
            default: Returned if no enabled checker type of that name exists.

        Returns:
            The Markdown instructions, or `default`.
        """
        definition = self.checkers.get(name)
        return definition.instructions if definition else default


class ConfigStore:
    """Reads the settings of one project from the database and watches them for changes.

    The store keeps no state of its own except the connection, the workspace and the project
    name: every `load` reads the current rows. A session keeps the `ProjectSettings` it last
    loaded.
    """

    def __init__(
        self,
        db: Db,
        workspace: Path,
        project: str | None = None,
    ) -> None:
        """Create a store on a shared connection.

        Args:
            db: The session's connection (`calu.db.Db`) with read access to `config`,
                `checker_types`, `rules` and `projects`. The store does not close it.
            workspace: The project root; `Rules` matches paths inside it in relative form.
            project: Name of the project row whose `project`-scope configuration and rules
                apply, or None for the global settings alone. A name that no row matches is
                treated like None.
        """
        self._db = db
        self.workspace = workspace
        self.project = project

    async def load(self) -> ProjectSettings:
        """Read configuration, checker definitions and rules from the database.

        Returns:
            The settings as the database holds them now. Global rows apply first, project rows
            override them; rules apply in the order (global before project, then `position`).
            Disabled checker types and disabled rules are left out.

        Raises:
            psycopg.Error: On database errors (connection lost, missing privileges, ...).
            pydantic.ValidationError: If the stored `settings` values do not match `Config`.
        """
        async with self._db.exclusive() as conn:
            # One lock for all four queries, so the settings cannot be read half-changed.
            project_id = await self._project_id(conn)
            layers = await self._settings_layers(conn)
            checkers = await self._checker_definitions(conn)
            rules = await self._rules(conn, project_id)
        config = build_config(
            layers,
            [
                (c.name, c.model, c.request_limit, c.output_tokens_limit, c.max_tokens)
                for c in checkers.values()
            ],
        )
        return ProjectSettings(config=config, checkers=checkers, rules=rules)

    async def watch(
        self,
        conn: psycopg.AsyncConnection,
        on_change: Callable[[ProjectSettings], Awaitable[None]],
        debounce: float = RELOAD_DEBOUNCE_S,
    ) -> None:
        """Reload the settings whenever the database announces a change, until cancelled.

        Listens on `config_changed` on `conn` and runs `reload_loop`. A session that also waits
        for `escalation_resolved` builds its own `Notifier` and runs `reload_loop` itself, so
        that one connection serves both channels.

        Side effects: runs `LISTEN config_changed` on `conn` and consumes its notifications, so
        `conn` must not be used for anything else while `watch` runs.

        Args:
            conn: A second open connection in autocommit mode, used only for listening.
            on_change: Awaited with the newly loaded settings after every change.
            debounce: Quiet time in seconds between the first notification of a burst and the
                reload.

        Raises:
            psycopg.Error: If `LISTEN` fails or the listening connection breaks.
        """
        dirty = Dirty()
        notifier = Notifier(conn)
        notifier.on(CONFIG_CHANGED, lambda _payload: dirty.set())
        async with anyio.create_task_group() as tg:
            tg.start_soon(notifier.run)
            tg.start_soon(self.reload_loop, dirty, on_change, debounce)

    async def reload_loop(
        self,
        dirty: Dirty,
        on_change: Callable[[ProjectSettings], Awaitable[None]],
        debounce: float = RELOAD_DEBOUNCE_S,
    ) -> None:
        """Reload the settings each time `dirty` is set, until cancelled.

        Waits `debounce` seconds after the first notification of a burst, so that one change of
        many rows causes one reload, then calls `load` and awaits `on_change` with the result. A
        failed reload (database error, invalid stored settings) is logged as a warning and
        otherwise ignored: the caller keeps the settings it has. An exception from `on_change`
        propagates.

        Args:
            dirty: Set by the notification handler of `config_changed`.
            on_change: Awaited with the newly loaded settings after every change.
            debounce: Quiet time in seconds before the reload.
        """
        while True:
            await dirty.wait()
            await anyio.sleep(debounce)
            try:
                settings = await self.load()
            except (psycopg.Error, ValidationError) as exc:
                log.warning("reloading the configuration failed: %s", exc)
                continue
            await on_change(settings)

    # ------------------------------------------------------------------ single queries

    async def _project_id(self, conn: psycopg.AsyncConnection) -> int | None:
        """Look up the id of the configured project.

        Args:
            conn: The locked connection.

        Returns:
            The `projects.id` of `self.project`, or None if no project is configured or no row
            has that name.

        Raises:
            psycopg.Error: On database errors.
        """
        if self.project is None:
            return None
        cur = await conn.execute(b"SELECT id FROM projects WHERE name = %s", (self.project,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def _settings_layers(self, conn: psycopg.AsyncConnection) -> list[Mapping[str, Any]]:
        """Read the partial configurations of the `config` table.

        Args:
            conn: The locked connection.

        Returns:
            The `settings` values, global first, then the project's; each a JSON object.
            Values that are not objects are skipped (a warning is logged).

        Raises:
            psycopg.Error: On database errors.
        """
        cur = await conn.execute(
            b"SELECT scope, value FROM config"
            b" WHERE key = %s AND ((scope = 'global' AND scope_id = '')"
            b"                     OR (scope = 'project' AND scope_id = %s))"
            b" ORDER BY scope = 'project'",
            (SETTINGS_KEY, self.project),
        )
        layers: list[Mapping[str, Any]] = []
        for scope, value in await cur.fetchall():
            if isinstance(value, Mapping):
                layers.append(value)
            else:
                log.warning("config row (%s, %s) is not an object; ignored", scope, SETTINGS_KEY)
        return layers

    async def _checker_definitions(
        self, conn: psycopg.AsyncConnection
    ) -> dict[str, CheckerDefinition]:
        """Read the enabled checker types.

        Args:
            conn: The locked connection.

        Returns:
            The definitions by name, in name order.

        Raises:
            psycopg.Error: On database errors.
        """
        cur = await conn.execute(
            b"SELECT name, route, instructions, model, request_limit, output_tokens_limit,"
            b" max_tokens, version FROM checker_types WHERE enabled ORDER BY name"
        )
        rows: Iterable[tuple[str, str, str, str, int, int, int, int]] = await cur.fetchall()
        return {row[0]: CheckerDefinition(*row) for row in rows}

    async def _rules(self, conn: psycopg.AsyncConnection, project_id: int | None) -> Rules:
        """Read the enabled rules of the global scope and of one project.

        Args:
            conn: The locked connection.
            project_id: The project's row id, or None for the global rules alone.

        Returns:
            The rules; sources are "<list>#<row id>" (see `Rules.from_rows`).

        Raises:
            psycopg.Error: On database errors.
        """
        cur = await conn.execute(
            b"SELECT list, pattern, id FROM rules"
            b" WHERE enabled AND (project_id IS NULL OR project_id = %s)"
            b" ORDER BY (project_id IS NOT NULL), position, id",
            (project_id,),
        )
        return Rules.from_rows(self.workspace, await cur.fetchall())
