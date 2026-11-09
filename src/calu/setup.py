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

from calu.db import migrate

DEFAULT_CHECKER_MODEL = "mistral:mistral-small-2603"
"""Pydantic AI model string of the default checkers (Mistral Small 4)."""

CHECKER_DEFAULTS = (
    ("command", "command", "checkers/command.md"),
    ("edit", "edit", "checkers/edit.md"),
    ("subagent-result", "subagent", "checkers/subagent-result.md"),
    ("changeset", "changeset", "checkers/changeset.md"),
    ("narrator", "narrator", "checkers/narrator.md"),
)
"""(checker name, route, default instructions file) of the built-in checker types."""

RULE_LISTS = (
    "command-allow",
    "command-deny",
    "read-allow",
    "read-deny",
    "write-allow",
    "write-deny",
)

SESSION_ROLE = "calu_session"


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
    """Return the built-in rules of all six lists.

    Returns:
        The rows parsed from `calu/defaults/rules/<list>.txt`.
    """
    base = resources.files("calu.defaults").joinpath("rules")
    rows: list[RuleRow] = []
    for name in RULE_LISTS:
        rows += parse_rule_file(name, base.joinpath(f"{name}.txt").read_text(encoding="utf-8"))
    return rows


async def seed_defaults(
    conn: psycopg.AsyncConnection, model: str = DEFAULT_CHECKER_MODEL
) -> tuple[int, int]:
    """Insert the built-in checker types and global rules where none exist yet.

    A checker type is inserted only if no type of that name exists. The default rules are
    inserted only if there are no global rules at all, so rules an administrator removed are
    not restored.

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
        for name, route, path in CHECKER_DEFAULTS:
            cur = await conn.execute(
                "INSERT INTO checker_types (name, route, instructions, model)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (name, route, base.joinpath(path).read_text(encoding="utf-8"), model),
            )
            checkers += cur.rowcount
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
    password = secrets.token_urlsafe(32)
    await conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(SESSION_ROLE), sql.Literal(password)
        )
    )
    params = conninfo_to_dict(admin_url)
    params.update(user=SESSION_ROLE, password=password)
    return make_conninfo("", **params)


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
