"""First-time setup of a calu database (`calu setup`).

Steps, all idempotent:

1. apply the migrations (`calu.db.migrate`);
2. load the built-in checker definitions and rule lists (`calu/defaults/`) into the database,
   without touching definitions or rules that already exist;
3. give the role `calu_session` a new random login password and return its connection string,
   which the CLI stores encrypted as the `session` credential.
"""

import secrets
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu.config import CHANGESET_REQUEST_LIMIT, CheckerConfig, changeset_limits
from calu.db import migrate
from calu.defaults.instructions import legacy_instructions

DEFAULT_CHECKER_MODEL = ""
"""The model new checker types are stored with: blank, which means the default model of
`calu.config.default_model` (Haiku on the Anthropic API, or through Bedrock in Bedrock mode)."""

CHECKER_DEFAULTS = (
    ("command", "command", "checkers/command.md", None, True),
    ("edit", "edit", "checkers/edit.md", None, True),
    ("subagent-result", "subagent", "checkers/subagent-result.md", None, True),
    # Far more than a step checker: judging a whole change means reading files around the
    # diff and following callers (PLAN.md D197); its token limits are set below as well.
    ("changeset", "changeset", "checkers/changeset.md", CHANGESET_REQUEST_LIMIT, True),
    ("narrator", "narrator", "checkers/narrator.md", None, True),
    # Seeded but **off**: honesty enforcement is a feature a user switches on deliberately, its
    # judgement needs a capable model (Q17), and it is a second checker on the edit route (D81).
    # `calu checker set honesty --enable` and `calu config set honesty.mode track` start it.
    ("honesty", "edit", "checkers/honesty.md", None, False),
)
"""(checker name, route, default instructions file, request limit, enabled) of the built-in
checker types. A request limit of None takes the default of `CheckerConfig`; the token limits
are `CheckerConfig`'s too, except for the changeset checker (`changeset_limits`)."""

RULE_LISTS = (
    "command-allow",
    "command-deny",
    "read-allow",
    "read-deny",
    "write-allow",
    "write-deny",
)

SESSION_ROLE = "calu_session"
OPERATOR_ROLE = "calu_operator"
"""Role of a human who watches a herd: reads every table and resolves an escalation, and may
change nothing else (migration 0001). It is created `NOLOGIN`; `enable_operator_login` gives it
a password when an operator needs one (PLAN.md D48)."""


@dataclass(frozen=True)
class RuleRow:
    """One rule as stored in the `rules` table."""

    list: str
    pattern: str
    comment: str
    position: int


def parse_rule_file(list_name: str, text: str) -> list[RuleRow]:
    r"""Convert a rules file of the sitl format into rule rows.

    Format: one regular expression per line; lines starting with `#` are comments; blank lines
    are ignored. A block of comment lines directly above a pattern (no blank line between)
    becomes that pattern's comment; comment blocks followed by a blank line (file headers) are
    dropped.

    Args:
        list_name: The list the rules belong to, e.g. "command-deny".
        text: The file content.

    Returns:
        The rules in file order, with positions 0, 1, 2, ...

    Example:
        "# Header\\n\\n# No sudo\\n^sudo( |$)\\n" gives one row with pattern "^sudo( |$)" and
        comment "No sudo".
    """
    rows: list[RuleRow] = []
    comment: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            comment = []
        elif stripped.startswith("#"):
            comment.append(stripped.lstrip("#").strip())
        else:
            rows.append(RuleRow(list_name, stripped, " ".join(comment), len(rows)))
            comment = []
    return rows


def default_rules() -> list[RuleRow]:
    """Return the rules a new database starts with: none (PLAN.md D179, the owner's Q16).

    calu ships no allow or deny rules; every list starts empty, and every step goes to a
    checker until someone adds rules. `recommended_rules` holds the former defaults, which
    `calu rules add-set recommended` adds. calu's own protections (`calu.checkers.rules`
    `BUILT_IN_*`) are not rules of a list and apply regardless.

    Returns:
        An empty list.
    """
    return []


RECOMMENDED_LISTS = tuple(name for name in RULE_LISTS if name != "command-allow")
"""The lists the `recommended` set has rules for. There is no shipped command allow list: an
allow rule matches a command by its text, so it also matches another program of that name, and
a match skips the checker (PLAN.md D198)."""


def recommended_rules() -> list[RuleRow]:
    """Return the rules of the optional `recommended` set (D179, D198).

    Returns:
        The rows parsed from `calu/defaults/rules/<list>.txt` for `RECOMMENDED_LISTS`.
    """
    base = resources.files("calu.defaults").joinpath("rules")
    rows: list[RuleRow] = []
    for name in RECOMMENDED_LISTS:
        rows += parse_rule_file(name, base.joinpath(f"{name}.txt").read_text(encoding="utf-8"))
    return rows


async def seed_defaults(
    conn: psycopg.AsyncConnection, model: str = DEFAULT_CHECKER_MODEL
) -> tuple[int, int]:
    """Insert the built-in checker types, and the default rules where no global rules exist.

    A checker type is inserted only if no type of that name exists. The default rules -- none
    since D179 -- would be inserted only if there are no global rules at all, so rules an
    administrator removed are not restored, and a database set up earlier keeps its rules.

    Args:
        conn: An open connection of the database owner, schema migrated.
        model: Pydantic AI model string for the new checker types.

    Returns:
        (number of checker types inserted, number of rules inserted).

    Raises:
        psycopg.Error: On database errors.
    """
    base = resources.files("calu.defaults")
    checkers = 0
    async with conn.transaction():
        for name, route, path, request_limit, enabled in CHECKER_DEFAULTS:
            limits = changeset_limits() if name == "changeset" else CheckerConfig()
            cur = await conn.execute(
                "INSERT INTO checker_types (name, route, instructions, model, request_limit,"
                " output_tokens_limit, max_tokens, enabled)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (
                    name,
                    route,
                    base.joinpath(path).read_text(encoding="utf-8"),
                    model,
                    request_limit or limits.request_limit,
                    limits.output_tokens_limit,
                    limits.max_tokens,
                    enabled,
                ),
            )
            checkers += cur.rowcount
            # D181: the shipped instructions were split into calu's fixed system part and a
            # compact user part. A row that still holds the old shipped text unchanged gets
            # the new user part -- otherwise the session would put the system part in front of
            # the old text, which repeats it. A text somebody edited is left as it is.
            legacy = legacy_instructions(name)
            if legacy is not None:
                await conn.execute(
                    "UPDATE checker_types SET instructions = %s"
                    " WHERE name = %s AND instructions = %s",
                    (base.joinpath(path).read_text(encoding="utf-8"), name, legacy),
                )
        cur = await conn.execute("SELECT count(*) FROM rules WHERE project_id IS NULL")
        row = await cur.fetchone()
        rules = 0
        if row and row[0] == 0:
            for rule in default_rules():
                await conn.execute(
                    "INSERT INTO rules (list, pattern, comment, position) VALUES (%s, %s, %s, %s)",
                    (rule.list, rule.pattern, rule.comment, rule.position),
                )
                rules += 1
    return checkers, rules


async def _enable_login(conn: psycopg.AsyncConnection, admin_url: str, role: str) -> str:
    """Give `role` a new random password and build its connection string.

    Any earlier password of the role stops working, which is how a credential is taken back.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin_url: The owner's connection string; host, port and database are taken from it.
        role: The role to give a login, one of `SESSION_ROLE` and `OPERATOR_ROLE`.

    Returns:
        A libpq connection string (key=value form) for that role.

    Raises:
        psycopg.Error: On database errors.
    """
    password = secrets.token_urlsafe(32)
    await conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    params = conninfo_to_dict(admin_url)
    params.update(user=role, password=password)
    return make_conninfo("", **params)


async def enable_operator_login(conn: psycopg.AsyncConnection, admin_url: str) -> str:
    """Give `calu_operator` a new random password and return its connection string (D48).

    An operator who watches a herd needs to read the fleet and resolve escalations and nothing
    else; the `admin` credential is the database owner and can migrate, create roles and change
    every setting, so sharing it with a person is more than the job needs. Calling this again
    renews the password, which takes every earlier copy out of use.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin_url: The owner's connection string; host, port and database are taken from it.

    Returns:
        A libpq connection string for `calu_operator`, to be stored with
        `calu credential set operator` on the machine that will use it.

    Raises:
        psycopg.Error: On database errors.
    """
    return await _enable_login(conn, admin_url, OPERATOR_ROLE)


async def enable_session_login(conn: psycopg.AsyncConnection, admin_url: str) -> str:
    """Give `calu_session` a new random password and return its connection string.

    Any earlier password of the role stops working.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin_url: The owner's connection string; host, port and database are taken from it.

    Returns:
        A libpq connection string (key=value form) for the role `calu_session`.

    Raises:
        psycopg.Error: On database errors.
    """
    return await _enable_login(conn, admin_url, SESSION_ROLE)


async def setup(conn: psycopg.AsyncConnection, admin_url: str, model: str) -> tuple[str, str]:
    """Run all setup steps (see the module docstring).

    Args:
        conn: An open connection of the database owner in autocommit mode.
        admin_url: The owner's connection string (for host, port and database).
        model: Pydantic AI model string for new checker types.

    Returns:
        (a human-readable report of what was done, the `calu_session` connection string).

    Raises:
        psycopg.Error: On database errors.
    """
    applied = await migrate(conn)
    checkers, rules = await seed_defaults(conn, model)
    session_url = await enable_session_login(conn, admin_url)
    report = (
        f"migrations applied: {len(applied)}; checker types added: {checkers}; "
        f"rules added: {rules}; login of {SESSION_ROLE} renewed"
    )
    return report, session_url
