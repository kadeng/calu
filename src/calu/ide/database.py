"""What `calu ide` reads from and writes to the database, as JSON-ready values.

    bridge request            function here            builds on
    ide.settings.get      ──► settings_view        ──► calu.admin, calu.configdoc, calu.web.settings
    ide.models.presets    ──► model_roles          ──► calu.admin (role keys, checker definitions)
    ide.models.set_all    ──► set_all_models       ──► calu.admin.set_all_models
    ide.models.set        ──► set_role_model
    ide.checker.get/save  ──► checker_view, save_instructions
    ide.sessions          ──► sessions_view        ──► calu.herd, Claude Code's transcripts
    ide.attach            ──► pick_session         ──► calu.herd.sessions

Every function takes an open connection (`calu.ide.access.Access` opens it with the login
the request may use) and knows nothing about JSON-RPC framing; the errors an editor has to
tell apart are raised as `RpcError` with the codes below.
"""

import getpass
import socket
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ValidationError

from calu import admin, herd
from calu.acp.peer import RpcError
from calu.claude.transcripts import transcripts, user_inputs
from calu.config import Config, default_model, merge_settings
from calu.configdoc import setting_rows
from calu.db.connection import Db
from calu.defaults.instructions import honesty_rules
from calu.ide.access import INVALID_PARAMS
from calu.web.settings import MARKDOWN, ROUTE_CHECKERS, _default, _kind_of, _lookup, _model_of

NO_SESSION = -32011
"""`ide.attach`: no running session matches."""

SEVERAL_SESSIONS = -32012
"""`ide.attach`: several running sessions match; `data.sessions` lists them."""

VERSION_CONFLICT = -32016
"""`ide.checker.save`: the stored definition has another version than `base_version`."""

HONESTY_KEY = "honesty.reviewer_model"
"""The configuration key of the honesty reviewer's model, which `admin.MODEL_KEYS` leaves out."""

ROLE_LABELS = {
    "command_checker.model": "Command checker",
    "edit_checker.model": "Edit checker",
    "changeset_checker.model": "Changeset checker",
    "subagent_checker.model": "Subagent result checker",
    "context.narrator_model": "Narrator",
    "context.summarizer_model": "Chat summarizer",
    "traces.redactor_model": "Trace redaction agent",
    HONESTY_KEY: "Honesty reviewer",
}
"""Every configuration key that names a model, with the label an editor shows for it."""

CHECKER_ROLE = "checker:"
"""Prefix of the role key of an extra checker type, whose model is in its definition row."""

STORED_LIMIT = 20
"""How many stored conversations of the workspace `ide.sessions` returns at most."""


def _json(value: Any) -> Any:
    """Turn a configuration value into what JSON can carry.

    Args:
        value: A default or stored value: a scalar, a list, a tuple, a dict or a model.

    Returns:
        The value with models dumped and tuples and sets as lists.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json(item) for item in value]
    return value


def _field_of(key: str) -> Any:
    """Find the Pydantic field of a dotted configuration key.

    Args:
        key: A key of `calu.configdoc.setting_rows`.

    Returns:
        The `FieldInfo`.

    Raises:
        KeyError: If the key names no field of `Config`.
    """
    model: type[BaseModel] = Config
    *parents, name = key.split(".")
    for part in parents:
        nested = _model_of(model.model_fields[part].annotation)
        if nested is None:
            raise KeyError(key)
        model = nested
    return model.model_fields[name]


def _kind(key: str, info: Any) -> tuple[str, list[str]]:
    """Decide which control an editor gives one setting.

    The same decision as the web settings form makes (`calu.web.settings._config_field`),
    plus two kinds for what that form does not render as one control.

    Args:
        key: The dotted key.
        info: Its `FieldInfo`.

    Returns:
        (kind, choices). The kinds: "bool", "choice" (with choices), "int", "number", "text",
        "model" (a Pydantic AI model string), "markdown", "lines" (a list of strings),
        "json" (any other value, edited as JSON), "section" (a nested group of settings,
        which has no control of its own).
    """
    if _model_of(info.annotation) is not None:
        return "section", []
    kind, choices = _kind_of(info.annotation)
    if kind is None:
        return "json", []
    if key in MARKDOWN:
        return "markdown", []
    if kind == "text" and key.endswith("model") and key != "claude.model":
        return "model", []
    return kind, list(choices)


async def settings_view(db: Db, project: str | None) -> dict[str, Any]:
    """Collect everything the settings dialog of an editor shows.

    Args:
        db: A connection that may read `config`, `checker_types` and `projects`.
        project: The project whose scope is shown, or None for the global scope.

    Returns:
        `schema` (the JSON Schema of `Config`), `rows` (one per setting of
        `calu.configdoc.setting_rows`: `key`, `type`, `default` (as text), `default_value`
        (as JSON), `description`, `value` (what is stored in this scope, else what the scope
        inherits: the global value in a project, else the default), `is_set` (stored in this
        scope), `inherits` ("global" or "default"), `kind` and `choices` (see `_kind`)),
        `checkers` (the definition rows) and `projects` (names).

    Raises:
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    stored = await admin.settings(db, project)
    inherited = await admin.settings(db, None) if project else {}
    rows: list[dict[str, Any]] = []
    for key, type_text, default_text, description in setting_rows():
        info = _field_of(key)
        kind, choices = _kind(key, info)
        default = honesty_rules() if key == "honesty.rules" else _default(info)
        own = None if kind == "section" else _lookup(stored, key)
        above = None if kind == "section" else _lookup(inherited, key)
        base = above if above is not None else default
        rows.append(
            {
                "key": key,
                "type": type_text,
                "default": default_text.strip("`"),
                "default_value": _json(default),
                "description": description,
                "value": _json(own if own is not None else base),
                "is_set": own is not None,
                "inherits": "global" if above is not None else "default",
                "kind": kind,
                "choices": choices,
            }
        )
    return {
        "schema": Config.model_json_schema(),
        "rows": rows,
        "checkers": [asdict(row) for row in await admin.checkers(db)],
        "projects": [row.name for row in await admin.projects(db)],
    }


async def model_roles(db: Db, project: str | None) -> dict[str, Any]:
    """Collect the model of every role and Claude Code's own model.

    Args:
        db: A connection that may read `config` and `checker_types`.
        project: The project whose effective settings are shown, or None for the global ones.

    Returns:
        `roles`: `{key, label, model}` per role -- the configuration keys of `ROLE_LABELS`
        with the model the scope effectively stores ("" means the default model), then one
        `checker:<name>` role per extra checker type with the model of its definition row.
        `claude`: `{model, effort, use_bedrock}`. `default_model`: the model a role without
        a stored model uses.

    Raises:
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    values = await admin.settings(db, None)
    if project:
        values = merge_settings(values, await admin.settings(db, project))
    roles = [
        {"key": key, "label": label, "model": str(_lookup(values, key) or "")}
        for key, label in ROLE_LABELS.items()
    ]
    for row in await admin.checkers(db):
        if row.name not in ROUTE_CHECKERS and row.route != "narrator":
            roles.append(
                {
                    "key": f"{CHECKER_ROLE}{row.name}",
                    "label": f"Extra checker: {row.name}",
                    "model": row.model,
                }
            )
    try:
        claude = Config.model_validate(values).claude
    except ValidationError:
        claude = Config().claude
    return {
        "roles": roles,
        "claude": {
            "model": claude.model,
            "effort": claude.effort,
            "use_bedrock": claude.use_bedrock,
        },
        "default_model": default_model(claude.use_bedrock),
    }


async def set_all_models(db: Db, model: str, effort: str | None, project: str | None) -> list[str]:
    """Point every model role at one model (`calu.admin.set_all_models`, and the reviewer).

    Side effects: writes the role keys of the scope, the checkers' effort keys (set to
    `effort`, or removed), and in the global scope the model of every checker definition.

    Args:
        db: A connection of the owner or of `calu_admin`.
        model: The Pydantic AI model string, or "" to store none (the default model).
        effort: The checkers' effort, or None to remove any that is stored.
        project: The project's scope, or None for the global one.

    Returns:
        The role keys that were written: the keys of `ROLE_LABELS`, then `checker:<name>`
        for every definition row whose model was changed.

    Raises:
        admin.AdminError: If the project does not exist or a value is refused.
        psycopg.Error: On database errors.
    """
    before = {row.name: row.model for row in await admin.checkers(db)}
    await admin.set_all_models(db, model, effort, project)
    if model:
        await admin.set_setting(db, HONESTY_KEY, model, project)
    else:
        await admin.unset_setting(db, HONESTY_KEY, project)
    changed = list(ROLE_LABELS)
    for row in await admin.checkers(db):
        if before.get(row.name) != row.model:
            changed.append(f"{CHECKER_ROLE}{row.name}")
    return changed


async def set_role_model(db: Db, role: str, model: str, project: str | None) -> None:
    """Store the model of one role.

    Args:
        db: A connection of the owner or of `calu_admin`.
        role: A key of `model_roles`: a configuration key of `ROLE_LABELS`, or
            `checker:<name>`.
        model: The Pydantic AI model string; "" stores none, so the default model is used.
        project: The project's scope for a configuration key, or None for the global one.
            A checker definition has no project scope.

    Raises:
        RpcError: `INVALID_PARAMS` if the role is unknown.
        admin.AdminError: If the project or the checker does not exist or the value is
            refused.
        psycopg.Error: On database errors.
    """
    if role.startswith(CHECKER_ROLE):
        name = role[len(CHECKER_ROLE) :]
        await admin.instructions(db, name)  # an unknown name must not create a checker
        await admin.set_checker(db, name, model=model)
        return
    if role not in ROLE_LABELS:
        raise RpcError(INVALID_PARAMS, f"no model role {role!r} (ide.models.presets lists them)")
    if model:
        await admin.set_setting(db, role, model, project)
    else:
        await admin.unset_setting(db, role, project)


async def checker_view(db: Db, name: str) -> dict[str, Any]:
    """Read one checker definition with its instructions.

    Args:
        db: A connection that may read `checker_types`.
        name: The checker type's name.

    Returns:
        `{name, route, model, version, enabled, instructions}`.

    Raises:
        admin.AdminError: If no checker type has that name.
        psycopg.Error: On database errors.
    """
    text = await admin.instructions(db, name)
    row = next(row for row in await admin.checkers(db) if row.name == name)
    return {
        "name": row.name,
        "route": row.route,
        "model": row.model,
        "version": row.version,
        "enabled": row.enabled,
        "instructions": text,
    }


async def save_instructions(
    db: Db, name: str, text: str, base_version: int | None
) -> dict[str, Any]:
    """Store new instructions for one checker, unless somebody changed them meanwhile.

    The same rule as the terminal client's save (`calu.tui.adminaccess.StoreAdmin`): the
    editor names the version it read, and a stored definition with another version is not
    overwritten.

    Args:
        db: A connection of the owner or of `calu_admin`.
        name: The checker type's name.
        text: The Markdown instructions; must not be empty.
        base_version: The version the edited text was read at; None stores whatever is
            there now.

    Returns:
        `{version}`: the version the database gave the change.

    Raises:
        RpcError: `VERSION_CONFLICT` if the stored version differs from `base_version`;
            `data` holds the stored `version` and `instructions`.
        admin.AdminError: If the checker is unknown or the text is empty.
        psycopg.Error: On database errors.
    """
    stored = await checker_view(db, name)
    if base_version is not None and stored["version"] != base_version:
        raise RpcError(
            VERSION_CONFLICT,
            f"the instructions of {name!r} were changed meanwhile: version "
            f"{stored['version']} is stored, the edited text was read at version {base_version}",
            {"version": stored["version"], "instructions": stored["instructions"]},
        )
    row = await admin.set_checker(db, name, instructions_text=text)
    return {"version": row.version}


def _within(path: Path, directory: str) -> bool:
    """Tell whether `path` is `directory` or below it.

    Args:
        path: An absolute, resolved path.
        directory: A session's project directory.

    Returns:
        True if it is; False also when `directory` is not a usable path.
    """
    try:
        return bool(directory) and path.is_relative_to(Path(directory))
    except OSError, ValueError:
        return False


def session_json(row: herd.SessionRow, workspace: Path) -> dict[str, Any]:
    """Render one running session for the editor.

    Args:
        row: The session.
        workspace: The editor's workspace folder, resolved.

    Returns:
        `{id, name, directory, hostname, state, claude_session_id, open_escalations,
        started_at, stale, here}`; `here` says that the session's directory is the workspace
        or an ancestor of it.
    """
    return {
        "id": row.id,
        "name": row.name,
        "directory": row.directory,
        "hostname": row.hostname,
        "state": row.state,
        "claude_session_id": row.claude_session_id,
        "open_escalations": row.open_escalations,
        "started_at": row.started_at.isoformat(),
        "stale": row.stale,
        "here": row.hostname == socket.gethostname() and _within(workspace, row.directory),
    }


async def sessions_view(db: Db, workspace: Path, every_host: bool = False) -> dict[str, Any]:
    """List the running sessions and the stored conversations of the workspace.

    Side effect: closes the open escalations of sessions that are gone (`herd.sessions`).

    Args:
        db: A connection that may read `sessions` and `turns` (operator, admin or owner).
        workspace: The editor's workspace folder, resolved.
        every_host: Whether the sessions of other hosts are listed too; they cannot be
            attached from here.

    Returns:
        `running`: the sessions that have not ended, of this host (or of every host), as
        `session_json`, those of the workspace first, then the newest first. `stored`: at
        most `STORED_LIMIT` conversations of the workspace, of this host and OS user, those
        that only Claude Code's own transcripts know included, the freshest first, each
        `{claude_session_id, last_input, turns, age_s, running, outside}`.

    Raises:
        psycopg.Error: On database errors.
    """
    host, user = socket.gethostname(), getpass.getuser()
    rows = [
        row
        for row in await herd.sessions(db, active_only=True)
        if every_host or row.hostname == host
    ]
    running = [session_json(row, workspace) for row in rows]
    running.sort(key=lambda entry: not entry["here"])  # stable: the newest stay first
    here = str(workspace)
    conversations = await herd.conversations(db, here, host, user, STORED_LIMIT)
    known = await herd.known_conversations(db, here, host, user)
    found = await anyio.to_thread.run_sync(transcripts, workspace)

    def last_input(session_id: str) -> str:
        """Return the last message the user typed in a transcript.

        Args:
            session_id: The Claude session id.

        Returns:
            The message, or "" if the transcript holds none.
        """
        inputs = user_inputs(session_id, workspace)
        return inputs[-1] if inputs else ""

    merged = await anyio.to_thread.run_sync(
        herd.with_transcripts, conversations, known, found, last_input, STORED_LIMIT
    )
    return {"running": running, "stored": [asdict(row) for row in merged]}


def pick_session(rows: list[herd.SessionRow], hint: str | None, workspace: Path) -> herd.SessionRow:
    """Choose the session `ide.attach` means, as `calu attach` chooses one.

    Only sessions of this host that serve clients and still say they are running count.
    With `hint`, the one whose id starts with it; without, the session whose directory is
    the workspace or its nearest ancestor.

    Args:
        rows: The sessions that have not ended (`herd.sessions`).
        hint: A session id or the beginning of one, or None for the workspace's session.
        workspace: The editor's workspace folder, resolved.

    Returns:
        The session.

    Raises:
        RpcError: `NO_SESSION` if none matches (the message says what to do);
            `SEVERAL_SESSIONS` if several do, with them in `data.sessions`.
    """
    host = socket.gethostname()
    local = [row for row in rows if row.hostname == host and row.address and not row.stale]
    if hint:
        chosen = [row for row in local if row.id.startswith(hint)]
        if not chosen:
            raise RpcError(
                NO_SESSION,
                f"no session on this machine whose id starts with {hint!r} is serving "
                "clients (ide.sessions lists the running ones)",
            )
    else:
        inside = [row for row in local if _within(workspace, row.directory)]
        if not inside:
            raise RpcError(
                NO_SESSION,
                f"no session of {workspace} is running on this machine: start one with "
                "ide.start, which needs a running `calu serve`, or with `calu run` in a "
                "terminal",
            )
        deepest = max(len(Path(row.directory).parts) for row in inside)
        chosen = [row for row in inside if len(Path(row.directory).parts) == deepest]
    if len(chosen) > 1:
        raise RpcError(
            SEVERAL_SESSIONS,
            f"{len(chosen)} sessions match; name one by its id",
            {"sessions": [session_json(row, workspace) for row in chosen]},
        )
    return chosen[0]
