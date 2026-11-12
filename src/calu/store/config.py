"""Configuration, rules and checker instructions, read from the database (PLAN.md D6).

Everything that steers the checkers lives in the database, out of reach of Claude Code and of
the commands it runs. A session loads it once at start and reloads it when the database
announces a change. The announcement is a hint, not the only path: `reload_loop` also re-reads
the rows every `RECONCILE_S` seconds and applies them if they differ from what it loaded last,
and it retries a reload that failed:

    config rows  (scope "global", then "project", key "settings")  ─┐
    checker_types rows (enabled)                                   ─┼─► ProjectSettings
    rules rows  (global, then project, by position)                ─┘      .config    (Config)
                                                                          .checkers  (by name)
    NOTIFY config_changed  ──►  ConfigStore.watch  ──► reload ──►         .rules      (Rules)

`ConfigStore.load` needs read access only; the role `calu_session` has it. Nothing in this
module writes.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import anyio
import psycopg
from pydantic import ValidationError

from calu.checkers.rules import Rules
from calu.config import (
    SETTINGS_KEY,
    CheckerConfig,
    Config,
    NetworkPolicy,
    build_config,
    default_model,
)
from calu.db import Db
from calu.db.notify import CONFIG_CHANGED, Dirty, Notifier

log = logging.getLogger(__name__)

POLICIES = ("none", "proxy", "full")
"""The values of `projects.network_policy` this store understands (D39)."""

RELOAD_DEBOUNCE_S = 0.25
"""Quiet time after a `config_changed` notification before the settings are reloaded, so that
one change of many rows (e.g. a replaced rule list) causes one reload."""

RECONCILE_S = 60.0
"""Seconds after which `reload_loop` re-reads the settings without a notification. It bounds how
long a session can run on settings the database no longer holds when a notification was lost
(a listening connection that was down, a change made before `LISTEN` took effect)."""

RELOAD_RETRY_FIRST_S = 2.0
"""Pause before the first retry of a failed reload; doubled per failure."""

RELOAD_RETRY_MAX_S = 60.0
"""Upper bound of the pause between two retries of a failed reload."""


class UnknownProject(ValueError):
    """The project a session was started for has no `projects` row.

    Raised instead of falling back to the global settings: a session that silently runs
    without its project's rules and settings applies less than the operator configured.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        """Create the error.

        Args:
            name: The project name that no row has.
            known: The names of the registered projects, for the message.
        """
        self.name = name
        self.known = known
        listed = ", ".join(known) if known else "none are registered"
        super().__init__(f"no project named {name!r} is registered (registered: {listed})")


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

    Every `load` reads the current rows; a session keeps the `ProjectSettings` it last loaded.
    Besides the connection, the workspace and the project name, the store remembers a
    fingerprint of the rows of its last successful `load`, which `reload_loop` uses to tell
    whether a periodic re-read found a change.
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
                apply, or None for the global settings alone. A name that no row matches makes
                `load` raise `UnknownProject`.
        """
        self._db = db
        self.workspace = workspace
        self.project = project
        self.fingerprint: str | None = None
        """SHA-256 over the rows of the last successful `load`; None before the first."""

    async def load(self) -> ProjectSettings:
        """Read configuration, checker definitions and rules from the database.

        Returns:
            The settings as the database holds them now. Global rows apply first, project rows
            override them; rules apply in the order (global before project, then `position`).
            Disabled checker types and disabled rules are left out.

        Side effects: on success, stores the fingerprint of the rows read in `fingerprint`.

        Raises:
            psycopg.Error: On database errors (connection lost, missing privileges, ...).
            TimeoutError: If the database does not answer within the connection's timeout.
            pydantic.ValidationError: If the stored `settings` values do not match `Config`.
            UnknownProject: If a project name was given and no `projects` row has it.
        """
        async with self._db.exclusive() as conn:
            # One lock for all four queries, so the settings cannot be read half-changed.
            project_id, policy = await self._project(conn)
            layers = await self._settings_layers(conn)
            checkers = await self._checker_definitions(conn)
            rule_rows = await self._rule_rows(conn, project_id)
        rules = Rules.from_rows(self.workspace, rule_rows)
        config = build_config(
            layers,
            [
                (c.name, c.model, c.request_limit, c.output_tokens_limit, c.max_tokens)
                for c in checkers.values()
            ],
        )
        if policy is not None:
            # The project's network policy shapes the sandbox the commands run in (D39). It
            # lives in its own column rather than in the settings, because it is a property of
            # the project, not a value someone sets per session.
            config = config.model_copy(
                update={
                    "claude": config.claude.model_copy(
                        update={"sandbox": config.claude.sandbox.with_policy(policy)}
                    )
                }
            )
        seen = repr((project_id, policy, layers, sorted(checkers.items()), rule_rows))
        # A definition without a model of its own (an extra checker such as `honesty`) uses
        # the default model, which follows Bedrock mode (D194).
        fallback = default_model(config.claude.use_bedrock)
        checkers = {
            name: definition if definition.model.strip() else replace(definition, model=fallback)
            for name, definition in checkers.items()
        }
        self.fingerprint = hashlib.sha256(seen.encode()).hexdigest()
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
        on_error: Callable[[Exception, int], Awaitable[None]] | None = None,
        reconcile_s: float = RECONCILE_S,
        on_unchanged: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Keep the caller's settings equal to the database's, until cancelled.

        The loop reloads in four cases:

        - `dirty.set()` (a `config_changed` notification): wait `debounce` seconds, `load`,
          await `on_change`.
        - `dirty.hint()` (a reconnect, a subscription that just took effect): the same, but
          await `on_change` only if the rows differ from the last load.
        - `reconcile_s` seconds passed without either: `load`; await `on_change` only if the
          rows differ from the last load.
        - The previous reload failed: retry after a pause (`RELOAD_RETRY_FIRST_S`, doubling up
          to `RELOAD_RETRY_MAX_S`); await `on_change` when it succeeds.

        A reload fails with a database error, a statement timeout, stored settings that do not
        match `Config`, or a project row that has disappeared. The failure is logged and
        reported to `on_error`; the caller keeps the settings it has until a retry succeeds,
        and decides in `on_error` whether that is acceptable. The loop never ends because of a
        failed reload. An exception from `on_change` or `on_error` propagates.

        Args:
            dirty: Set by the notification handler of `config_changed`.
            on_change: Awaited with the newly loaded settings.
            debounce: Quiet time in seconds after a notification before the reload, so that one
                change of many rows causes one reload.
            on_error: Awaited with the error and the number of reloads that have failed in a
                row (1 for the first), or None to only log.
            reconcile_s: Seconds without a notification after which the settings are re-read.
            on_unchanged: Awaited after a re-read that found the rows as they were, or None.
                A caller keeps further state from the database that the fingerprint does not
                cover (`calu run`: the chat summary) and re-reads it here, so that this state
                does not depend on a notification either. An exception from it propagates.
        """
        failures = 0
        while True:
            notified: bool | None = None
            with anyio.move_on_after(reconcile_s):
                notified = await dirty.wait()
            if notified is not None:
                await anyio.sleep(debounce)
            before = self.fingerprint
            try:
                settings = await self.load()
            except (psycopg.Error, TimeoutError, ValidationError, UnknownProject) as exc:
                failures += 1
                log.warning("reloading the configuration failed (%d in a row): %s", failures, exc)
                if on_error is not None:
                    await on_error(exc, failures)
                pause = RELOAD_RETRY_FIRST_S * 2 ** min(failures - 1, 10)
                await anyio.sleep(min(pause, RELOAD_RETRY_MAX_S))
                dirty.hint()
                continue
            recovered, failures = failures > 0, 0
            if notified or recovered or self.fingerprint != before:
                await on_change(settings)
            elif on_unchanged is not None:
                await on_unchanged()

    # ------------------------------------------------------------------ single queries

    async def _project(
        self, conn: psycopg.AsyncConnection
    ) -> tuple[int | None, NetworkPolicy | None]:
        """Look up the id and the network policy of the configured project.

        Args:
            conn: The locked connection.

        Returns:
            (`projects.id`, `projects.network_policy`), or (None, None) if no project is
            configured. A policy of None leaves the sandbox settings as they are configured,
            which is what a session without a project gets.

        Raises:
            psycopg.Error: On database errors.
            UnknownProject: If a project is configured and no row has that name.
        """
        if self.project is None:
            return None, None
        cur = await conn.execute(
            b"SELECT id, network_policy FROM projects WHERE name = %s", (self.project,)
        )
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute(b"SELECT name FROM projects ORDER BY name")
            raise UnknownProject(self.project, [str(r[0]) for r in await cur.fetchall()])
        policy = str(row[1])
        return row[0], cast("NetworkPolicy", policy) if policy in POLICIES else None

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

    async def _rule_rows(
        self, conn: psycopg.AsyncConnection, project_id: int | None
    ) -> list[tuple[str, str, int]]:
        """Read the enabled rules of the global scope and of one project.

        Args:
            conn: The locked connection.
            project_id: The project's row id, or None for the global rules alone.

        Returns:
            (list, pattern, row id) per rule, global rules before the project's, then by
            position: the form `Rules.from_rows` takes.

        Raises:
            psycopg.Error: On database errors.
        """
        cur = await conn.execute(
            b"SELECT list, pattern, id FROM rules"
            b" WHERE enabled AND (project_id IS NULL OR project_id = %s)"
            b" ORDER BY (project_id IS NOT NULL), position, id",
            (project_id,),
        )
        return await cur.fetchall()
