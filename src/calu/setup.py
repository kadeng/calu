"""First-time setup of a calu database (`calu setup`).

Steps, all idempotent:

1. apply the migrations (`calu.db.migrate`);
2. load the built-in checker definitions and rule lists (`calu/defaults/`) into the database,
   without touching definitions or rules that already exist;
3. give the role `calu_admin`, the user login, its password (`set_user_password`; the CLI
   stores the login as the `user` entry, agent/records/PLAN.md D265).
"""

import os
import secrets
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg import sql

from calu.config import CHANGESET_REQUEST_LIMIT, CheckerConfig, changeset_limits
from calu.db import EarlierSchema, migrate, roles
from calu.db.connection import open_stored
from calu.db.target import PasswordAuth, Target
from calu.defaults.instructions import shipped_status
from calu.logins import refused_by_database

DEFAULT_CHECKER_MODEL = ""
"""The model new checker types are stored with: blank, which means the default model of
`calu.config.default_model` (Haiku on the Anthropic API, or through Bedrock in Bedrock mode)."""

CHECKER_DEFAULTS = (
    ("command", "command", "checkers/command.md", None, True),
    ("edit", "edit", "checkers/edit.md", None, True),
    ("subagent-result", "subagent", "checkers/subagent-result.md", None, True),
    # Far more than a step checker: judging a whole change means reading files around the diff and
    # following callers (agent/records/PLAN.md D197); its token limits are set below as well.
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

USER_ROLE = roles.ADMIN
"""The role of the user login (`calu.logins.USER`): every calu process of the person who set
calu up logs in as it, with the calu user password (migrations 0033, 0043)."""

MANAGED_ROLES = (roles.SESSION, roles.OPERATOR, roles.ADMIN)
"""The roles the migrations create or change, which the owner's login must manage."""

MIN_SERVER_VERSION = 140000
"""`server_version_num` of the oldest PostgreSQL `calu setup` accepts (14)."""

ADMIN_OPTION_VERSION = 160000
"""`server_version_num` from which a login with CREATEROLE may change only the roles it created
or was granted with the admin option (PostgreSQL 16)."""


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
    """Return the rules a new database starts with: none.

    See agent/records/PLAN.md D179, the owner's Q16.

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
a match skips the checker (agent/records/PLAN.md D198)."""


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
    conn: psycopg.AsyncConnection,
    model: str = DEFAULT_CHECKER_MODEL,
    update_outdated: bool = True,
) -> tuple[int, int]:
    """Insert the built-in checker types, and the default rules where no global rules exist.

    A checker type is inserted only if no type of that name exists. With `update_outdated`, a
    type that still holds an earlier shipped text unchanged (`shipped_status` "outdated", any
    version calu ever shipped, D248) gets the present one; a text somebody edited is left as
    it is. The default rules -- none
    since D179 -- would be inserted only if there are no global rules at all, so rules an
    administrator removed are not restored, and a database set up earlier keeps its rules.

    Args:
        conn: An open connection of the database owner, schema migrated.
        model: Pydantic AI model string for the new checker types.
        update_outdated: Whether to replace unedited older shipped texts; `calu setup` passes
            False and offers the update itself (`calu.cli`).

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
            # A row that still holds an earlier shipped text unchanged gets the present one
            # (D248: every version calu shipped is known by its hash). A text somebody edited
            # is left as it is.
            if update_outdated:
                cur = await conn.execute(
                    "SELECT instructions FROM checker_types WHERE name = %s", (name,)
                )
                found = await cur.fetchone()
                status = shipped_status(name, str(found[0])) if found else None
                if status is not None and status.kind == "outdated":
                    await conn.execute(
                        "UPDATE checker_types SET instructions = %s WHERE name = %s",
                        (status.present, name),
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


class SetupRefused(Exception):
    """The database is not one calu can set up as it is; the message lists what to change."""


async def preflight(conn: psycopg.AsyncConnection) -> list[str]:
    """Check that the connected login can set calu up in this database, before anything changes.

    calu needs a database of its own (it creates its tables in the schema `public`, grants
    its operator role read access to every table there, and `calu db reset` drops that
    schema) and a login that may create roles (`CREATEROLE`) and, from PostgreSQL 16 on, may
    manage the roles of `MANAGED_ROLES` if they exist already. It needs no superuser.

    Args:
        conn: An open connection of the login that `calu setup` was given.

    Returns:
        One sentence per problem, each with the SQL an administrator runs to remove it; empty
        if setup can go on.

    Raises:
        psycopg.Error: On database errors.
    """

    async def scalar(query: str) -> object:
        """Run a query that returns one value.

        Args:
            query: The SQL.

        Returns:
            The value.
        """
        cur = await conn.execute(query.encode())
        row = await cur.fetchone()
        return row[0] if row else None

    problems: list[str] = []
    user = str(await scalar("SELECT current_user"))
    database = str(await scalar("SELECT current_database()"))
    version = int(str(await scalar("SHOW server_version_num")))
    if version < MIN_SERVER_VERSION:
        problems.append(
            f"the server runs PostgreSQL {version // 10000}; calu needs "
            f"{MIN_SERVER_VERSION // 10000} or newer (it is tested with 18)"
        )
    superuser = bool(await scalar("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
    may_create = bool(
        await scalar("SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user")
    )
    if not superuser and not may_create:
        # Giving a role a login needs CREATEROLE on every supported version, whether or not
        # an administrator created the role beforehand.
        problems.append(
            f"{user} may not create or change roles, and calu creates its roles and gives "
            f"{USER_ROLE} its login. An administrator runs `ALTER ROLE {user} CREATEROLE;`"
        )
    for role in MANAGED_ROLES:
        if superuser or not may_create or version < ADMIN_OPTION_VERSION:
            continue
        # From PostgreSQL 16 on, CREATEROLE alone changes only roles the login created
        # itself or was given with the admin option.
        cur = await conn.execute(
            b"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s),"
            b" EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
            b" JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = %s AND"
            b" u.rolname = current_user AND m.admin_option)",
            (role, role),
        )
        row = await cur.fetchone()
        if row and row[0] and not row[1]:
            problems.append(
                f"the role {role} exists on this server (roles belong to the whole server, "
                f"so another calu database may have created it) and {user} may not manage "
                f"it. An administrator runs `GRANT {role} TO {user} WITH ADMIN OPTION;`"
            )
    if not await scalar("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"):
        problems.append(
            f"{user} may not create tables in the schema public of {database}. An "
            f"administrator runs `ALTER DATABASE {database} OWNER TO {user};` and, for a "
            f"database created before PostgreSQL 15, `ALTER SCHEMA public OWNER TO {user};`"
        )
    first = await scalar("SELECT to_regclass('public.schema_migrations') IS NULL")
    foreign = await scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
    if first and foreign:
        problems.append(
            f"the database {database} already holds {foreign} table(s) that are not calu's. "
            "calu needs a database of its own: an administrator runs `CREATE DATABASE calu "
            f"OWNER {user};` and setup is given that database"
        )
    return problems


async def _leave_rds_iam(conn: psycopg.AsyncConnection, role: str) -> None:
    """Take `role` out of the RDS role `rds_iam` if it is a member, so a password works.

    On RDS a member of `rds_iam` is asked for an IAM token and its password is not accepted.
    On a server without `rds_iam` (no RDS instance) nothing happens.

    Side effects: at most one `REVOKE rds_iam FROM <role>`.

    Args:
        conn: An open connection of the database owner.
        role: The role that is about to get a password.

    Raises:
        psycopg.Error: On database errors.
    """
    cur = await conn.execute(
        b"SELECT 1 FROM pg_roles r WHERE r.rolname = 'rds_iam'"
        b" AND pg_has_role(%s, r.oid, 'MEMBER')",
        (role,),
    )
    if await cur.fetchone():
        await conn.execute(sql.SQL("REVOKE rds_iam FROM {}").format(sql.Identifier(role)))


def user_login(place: Target, password: str) -> Target:
    """Describe the user login (the role `calu_admin`) at the database another login names.

    Args:
        place: Any login of the same database (the owner's, the place file's): where the
            database is and how its server is verified are taken from it, nothing else.
        password: The password of `calu_admin`: the user password, or the generated one of
            an empty user password.

    Returns:
        The login of `calu_admin`.

    Raises:
        TargetError: If `place` finds its server through a libpq service file.
    """
    return place.for_role(USER_ROLE, PasswordAuth(password=password))


async def user_can_log_in(conn: psycopg.AsyncConnection) -> bool:
    """Tell whether the role `calu_admin` exists and may log in.

    Args:
        conn: An open connection of the owner or of `calu_admin` itself.

    Returns:
        True if `calu setup` has given the role a password; False before that, and for a
        database without the role (schema older than version 33).

    Raises:
        psycopg.Error: On database errors.
    """
    cur = await conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (USER_ROLE,))
    row = await cur.fetchone()
    return bool(row and row[0])


async def is_user_password(place: Target, password: str) -> bool:
    """Ask the database whether `password` is the user password, by logging in with it.

    Side effect: one connection as `calu_admin`, opened and closed.

    Args:
        place: Any login of the database (`user_login` takes the place from it).
        password: The password to check; a blank one is sent like any other.

    Returns:
        True if the server accepted the login; False if it refused the password.

    Raises:
        psycopg.OperationalError: If the database cannot be reached or refuses the login
            for another reason than the password.
        TargetError: If `place` cannot be taken over (`Target.for_role`).
    """
    try:
        conn = await open_stored(user_login(place, password), False)
    except psycopg.OperationalError as exc:
        if refused_by_database(str(exc)):
            return False
        raise
    await conn.close()
    return True


SCRAM_ITERATIONS = 3_000_000
"""Rounds of the SCRAM verifier of `calu_admin` (D279, the owner: "Use 3 million rehashes")."""

ITERATIONS_ENV = "CALU_SCRAM_ITERATIONS"
"""Replaces `SCRAM_ITERATIONS`, at least 4096 (PostgreSQL's default). Calu's tests set it low:
they create the role hundreds of times and log in as it thousands of times."""


def scram_iterations() -> int:
    """Return the rounds of a new SCRAM verifier: `ITERATIONS_ENV`, else `SCRAM_ITERATIONS`.

    Returns:
        The rounds; a value under 4096 or that is no number gives `SCRAM_ITERATIONS`.
    """
    value = os.environ.get(ITERATIONS_ENV, "")
    return int(value) if value.isdigit() and int(value) >= 4096 else SCRAM_ITERATIONS


async def set_user_password(conn: psycopg.AsyncConnection, password: str) -> None:
    """Let `calu_admin` log in with `password`; an earlier password stops working.

    The server gets the SCRAM verifier, computed here, and never the password itself, so the
    password is in no statement the server could log. On RDS the role is first taken out of
    `rds_iam`, which would otherwise make the server ignore the password. From PostgreSQL 16
    on the verifier takes `SCRAM_ITERATIONS` rounds (D279): every guess at the password then
    costs the guesser that work, on the server's port as well as with a stolen verifier;
    every login of calu pays it once (about 0.3 s).

    Args:
        conn: An open connection of the database owner, schema migrated (version 33).
        password: The user password, or a generated one; not empty.

    Raises:
        ValueError: If the password is empty: PostgreSQL lets no role log in with one.
        psycopg.Error: On database errors.
    """
    if not password:
        raise ValueError("the password of calu_admin must not be empty")
    if conn.info.server_version >= 160000:
        # libpq computes the verifier with the connection's `scram_iterations`.
        await conn.execute(
            sql.SQL("SET scram_iterations = {}").format(sql.Literal(scram_iterations()))
        )
    verifier = conn.pgconn.encrypt_password(
        password.encode(), USER_ROLE.encode(), b"scram-sha-256"
    ).decode()
    await _leave_rds_iam(conn, USER_ROLE)
    await conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(USER_ROLE), sql.Literal(verifier)
        )
    )


def generated_password() -> str:
    """Make a password for `calu_admin` when the user password is empty.

    Returns:
        43 random URL-safe characters.
    """
    return secrets.token_urlsafe(32)


async def setup_database(
    conn: psycopg.AsyncConnection, model: str, update_outdated: bool = True
) -> str:
    """Run the preflight, the migrations and the seeding of the defaults (steps 1 and 2).

    The user login (step 3) is set by the caller (`set_user_password`), which knows the
    password.

    Args:
        conn: An open connection of the database owner in autocommit mode.
        model: Pydantic AI model string for new checker types.
        update_outdated: Whether `seed_defaults` replaces unedited older shipped checker
            instructions with the present ones.

    Returns:
        A human-readable report of what was done.

    Raises:
        SetupRefused: If `preflight` finds a problem, or the database was set up by an
            earlier calu whose migrations were replaced (`calu.db.EarlierSchema`); nothing
            was changed.
        psycopg.Error: On database errors.
    """
    problems = await preflight(conn)
    if problems:
        raise SetupRefused("\n".join(f"- {line}" for line in problems))
    try:
        applied = await migrate(conn)
    except EarlierSchema as exc:
        raise SetupRefused(f"- {exc}") from exc
    checkers, rules = await seed_defaults(conn, model, update_outdated)
    return (
        f"migrations applied: {len(applied)}; checker types added: {checkers}; rules added: {rules}"
    )


async def setup(conn: psycopg.AsyncConnection, admin_url: str, model: str) -> tuple[str, str]:
    """Run all setup steps with a generated user password (the form tests and spikes use).

    Args:
        conn: An open connection of the database owner in autocommit mode.
        admin_url: The owner's connection string (for host, port and database).
        model: Pydantic AI model string for new checker types.

    Returns:
        (a human-readable report of what was done, the connection string of the user login:
        `calu_admin` with its new generated password).

    Raises:
        SetupRefused: If `preflight` finds a problem; nothing was changed.
        psycopg.Error: On database errors.
    """
    report = await setup_database(conn, model)
    password = generated_password()
    await set_user_password(conn, password)
    return report, user_login(Target.parse(admin_url), password).libpq_string()
