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

from calu.config import CHANGESET_REQUEST_LIMIT, CheckerConfig, changeset_limits
from calu.db import migrate
from calu.db.target import PasswordAuth, RdsIamAuth, Target
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

MIN_SERVER_VERSION = 140000
"""`server_version_num` of the oldest PostgreSQL `calu setup` accepts (14)."""

ADMIN_OPTION_VERSION = 160000
"""`server_version_num` from which a login with CREATEROLE may change only the roles it created
or was granted with the admin option (PostgreSQL 16)."""
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


class SetupRefused(Exception):
    """The database is not one calu can set up as it is; the message lists what to change."""


async def preflight(conn: psycopg.AsyncConnection) -> list[str]:
    """Check that the connected login can set calu up in this database, before anything changes.

    calu needs a database of its own (it creates its tables in the schema `public`, grants
    its operator role read access to every table there, and `calu db reset` drops that
    schema) and a login that may create roles (`CREATEROLE`) and, from PostgreSQL 16 on, may
    manage the two roles `calu_session` and `calu_operator` if they exist already. It needs
    no superuser.

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
            f"{user} may not create or change roles, and calu gives its two roles "
            f"({SESSION_ROLE}, {OPERATOR_ROLE}) their logins. An administrator runs "
            f"`ALTER ROLE {user} CREATEROLE;`"
        )
    for role in (SESSION_ROLE, OPERATOR_ROLE):
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


async def session_can_log_in(conn: psycopg.AsyncConnection) -> bool:
    """Tell whether the role `calu_session` has a login already.

    Args:
        conn: An open connection.

    Returns:
        True if the role exists and may log in: an earlier setup, on this machine or on
        another one, gave it a password.

    Raises:
        psycopg.Error: On database errors.
    """
    cur = await conn.execute(
        b"SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (SESSION_ROLE,)
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _enable_login(conn: psycopg.AsyncConnection, admin: Target, role: str) -> Target:
    """Give `role` a new random password and build its login record.

    Any earlier password of the role stops working, which is how a credential is taken back.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin: The owner's login; where the database is and how its server is verified are
            taken from it (`Target.for_role`), nothing else.
        role: The role to give a login, one of `SESSION_ROLE` and `OPERATOR_ROLE`.

    Returns:
        The login of that role, with a password, whatever the owner authenticates with.

    Raises:
        TargetError: If the owner's record cannot be taken over (`Target.for_role`); the role
            is not changed then.
        psycopg.Error: On database errors.
    """
    password = secrets.token_urlsafe(32)
    # Built first: an owner record that cannot be taken over must not cost the role its login.
    login = admin.for_role(role, PasswordAuth(password=password))
    cur = await conn.execute(
        b"SELECT 1 FROM pg_roles r WHERE r.rolname = 'rds_iam'"
        b" AND pg_has_role(%s, r.oid, 'MEMBER')",
        (role,),
    )
    if await cur.fetchone():
        # On RDS a member of `rds_iam` is asked for a token and its password is not accepted.
        await conn.execute(sql.SQL("REVOKE rds_iam FROM {}").format(sql.Identifier(role)))
    await conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    return login


async def enable_operator_login(conn: psycopg.AsyncConnection, admin: Target | str) -> str:
    """Give `calu_operator` a new random password and return its connection string (D48).

    An operator who watches a herd needs to read the fleet and resolve escalations and nothing
    else; the `admin` credential is the database owner and can migrate, create roles and change
    every setting, so sharing it with a person is more than the job needs. Calling this again
    renews the password, which takes every earlier copy out of use.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin: The owner's login, or its connection string.

    Returns:
        What `calu credential set operator` stores on the machine that will use it: a libpq
        connection string for `calu_operator`; for calu's own local database the form 2
        record instead, which also says where the container's files are. A string that names
        a CA file (`sslrootcert`) names the file on this machine.

    Raises:
        TargetError: If the owner's record cannot be taken over; the role is not changed.
        psycopg.Error: On database errors.
    """
    owner = admin if isinstance(admin, Target) else Target.parse(admin)
    login = await _enable_login(conn, owner, OPERATOR_ROLE)
    return login.dump().decode() if login.docker is not None else login.libpq_string()


async def enable_session_login(
    conn: psycopg.AsyncConnection, admin: Target | str, iam: RdsIamAuth | None = None
) -> Target:
    """Give `calu_session` a login and return it.

    With a password (the default): the role gets a new random one, and any earlier password
    stops working, so sessions that use it cannot reconnect and other machines that hold it
    cannot start sessions.

    With IAM authentication (an RDS instance): the role is allowed to log in and is made a
    member of `rds_iam`, which is what makes RDS ask it for a token instead of a password.
    Nothing secret is created; who may connect as `calu_session` is decided by the AWS policy
    (`rds-db:connect` for that database user) and by who can use the named AWS profile.

    Args:
        conn: An open connection of the database owner, schema migrated.
        admin: The owner's login, or its connection string.
        iam: Region and AWS profile for a login with IAM tokens; None for a password.

    Returns:
        The login of the role `calu_session`.

    Raises:
        psycopg.Error: On database errors, among them a server without the role `rds_iam`
            (one that is no RDS instance).
    """
    owner = admin if isinstance(admin, Target) else Target.parse(admin)
    if iam is None:
        return await _enable_login(conn, owner, SESSION_ROLE)
    login = owner.for_role(SESSION_ROLE, iam)
    await conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN").format(sql.Identifier(SESSION_ROLE)))
    await conn.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(SESSION_ROLE)))
    return login


async def setup_database(
    conn: psycopg.AsyncConnection,
    admin: Target,
    model: str,
    renew_session: bool,
    session_iam: RdsIamAuth | None = None,
) -> tuple[str, Target | None]:
    """Run all setup steps (see the module docstring), keeping a session login that exists.

    Args:
        conn: An open connection of the database owner in autocommit mode.
        admin: The owner's login.
        model: Pydantic AI model string for new checker types.
        renew_session: True gives `calu_session` a new login; False leaves the role as it
            is.
        session_iam: With `renew_session`: region and AWS profile if the session login uses
            IAM tokens (`enable_session_login`); None gives it a password.

    Returns:
        (a human-readable report of what was done, the new login of `calu_session` or None
        if it was left alone).

    Raises:
        SetupRefused: If `preflight` finds a problem; nothing was changed.
        psycopg.Error: On database errors.
    """
    problems = await preflight(conn)
    if problems:
        raise SetupRefused("\n".join(f"- {line}" for line in problems))
    applied = await migrate(conn)
    checkers, rules = await seed_defaults(conn, model)
    session = await enable_session_login(conn, admin, session_iam) if renew_session else None
    login = "renewed" if renew_session else "kept"
    report = (
        f"migrations applied: {len(applied)}; checker types added: {checkers}; "
        f"rules added: {rules}; login of {SESSION_ROLE} {login}"
    )
    return report, session


async def setup(conn: psycopg.AsyncConnection, admin_url: str, model: str) -> tuple[str, str]:
    """Run all setup steps and renew the session login (the form the tests and spikes use).

    Args:
        conn: An open connection of the database owner in autocommit mode.
        admin_url: The owner's connection string (for host, port and database).
        model: Pydantic AI model string for new checker types.

    Returns:
        (a human-readable report of what was done, the `calu_session` connection string).

    Raises:
        SetupRefused: If `preflight` finds a problem; nothing was changed.
        psycopg.Error: On database errors.
    """
    report, session = await setup_database(conn, Target.parse(admin_url), model, True)
    assert session is not None
    return report, session.libpq_string()
