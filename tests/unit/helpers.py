"""Shared helpers of the unit tests."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio

from calu.checkers.rules import Rules
from calu.claude import FakeRunner, FakeScript
from calu.config import CheckerConfig, Config
from calu.defaults import default_instructions
from calu.protocol.messages import Event
from calu.session import Session
from calu.setup import CHECKER_DEFAULTS
from calu.setup import default_rules as default_rule_rows
from calu.store import CheckerDefinition, MemorySessionStore, ProjectSettings
from calu.testing import fake_models


def default_rules(workspace: Path, replace: dict[str, str] | None = None) -> Rules:
    """Return the built-in rules for `workspace`, as `calu setup` loads them.

    Args:
        workspace: The workspace root.
        replace: List texts (line format of `parse_rules`) that replace whole default lists.
    """
    rules = Rules.from_rows(
        workspace, ((r.list, r.pattern, r.position) for r in default_rule_rows())
    )
    if replace:
        lists = {name: rules.lists(name) for name in rules._lists}
        lists.update(Rules.from_texts(workspace, replace)._lists)
        rules.replace(lists)
    return rules


def default_settings(
    workspace: Path,
    config: Config | None = None,
    rules: Rules | None = None,
    model: str = "test:model",
) -> ProjectSettings:
    """Return the settings that `calu setup` loads into an empty database.

    Args:
        workspace: The workspace root (for the rules).
        config: The configuration; None uses the defaults.
        rules: The rules; None uses the built-in ones.
        model: Model string of the checker definitions.

    Returns:
        Settings with the four built-in checker definitions (version 1), the built-in rules and
        `config`.
    """
    checkers = {
        name: CheckerDefinition(
            name=name,
            route=route,
            instructions=default_instructions(name),
            model=model,
            # The budgets a fresh database holds (`calu.setup`, migration 0017/0018): the
            # changeset checker gets more requests than a step checker (D75).
            request_limit=request_limit or CheckerConfig.model_fields["request_limit"].default,
            output_tokens_limit=CheckerConfig.model_fields["output_tokens_limit"].default,
            max_tokens=CheckerConfig.model_fields["max_tokens"].default,
            version=1,
        )
        for name, route, _, request_limit in CHECKER_DEFAULTS
    }
    return ProjectSettings(
        config=config or Config(),
        checkers=checkers,
        rules=rules if rules is not None else default_rules(workspace),
    )


class Recorder:
    """Collects events and lets tests wait for one of a given type."""

    def __init__(self) -> None:
        """Create an empty recorder."""
        self.events: list[Event] = []
        self._changed = anyio.Event()

    async def __call__(self, event: Event) -> None:
        """Record `event` and wake every `wait_for`.

        Args:
            event: The event.
        """
        self.events.append(event)
        self._changed.set()
        self._changed = anyio.Event()

    def of(self, cls: type) -> list[Any]:
        """Return the recorded events of one type.

        Args:
            cls: The event class.
        """
        return [e for e in self.events if isinstance(e, cls)]

    async def wait_for(self, cls: type, count: int = 1, timeout: float = 5) -> list[Any]:
        """Wait until `count` events of type `cls` were recorded.

        Args:
            cls: The event class.
            count: How many are awaited.
            timeout: Seconds before the wait fails.
        """
        with anyio.fail_after(timeout):
            while len(self.of(cls)) < count:
                await self._changed.wait()
        return self.of(cls)


def make_session(
    workspace: Path,
    turns: Sequence[Sequence[dict[str, Any]]],
    config: Config | None = None,
    settings: ProjectSettings | None = None,
) -> tuple[Session, Recorder, MemorySessionStore]:
    """Build a session with a scripted fake runner, fake models and an in-memory store.

    Args:
        workspace: The workspace root (usually `tmp_path`).
        turns: The fake runner's script (see `calu.claude.fake_runner`).
        config: The configuration; None uses the defaults.
        settings: Complete settings; None builds them from `config` and the built-in defaults.
            The write-set snapshots (D31) are kept next to the workspace, not in the user's
            state directory, so a test never writes outside its temporary directory.

    Returns:
        The session, the event recorder and the store.
    """
    rec = Recorder()
    store = MemorySessionStore()
    script = FakeScript(turns=[list(t) for t in turns])
    session = Session(
        workspace,
        settings or default_settings(workspace, config),
        store,
        lambda: FakeRunner(script, workspace),
        rec,
        checker_models=fake_models(),
        snapshot_root=workspace.parent / f"{workspace.name}-snapshots",
    )
    return session, rec, store
