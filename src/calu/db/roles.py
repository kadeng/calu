"""calu's database roles, and narrowing a connection to the role an action needs.

The roles (migrations 0001, 0015, 0033; agent/records/PLAN.md D213, D249):

    role            logs in with                         may
    owner           the `admin` entry (root's store)     migrate, create roles, everything
    calu_admin      the admin password, or the           read and write every table: settings,
                    generated one of `--no-password`     checker instructions, rules, trust
    calu_operator   the `operator` entry                 read every table; resolve escalations;
                                                         mint and revoke client and server
                                                         tokens; name sessions; file and
                                                         withdraw session requests
    calu_session    the `session` entry                  the rows of its own session (RLS)

`calu_admin` is a member of `calu_operator`; the owner may take either role when it is a
superuser (the local container) or a member with the SET option. A client that holds one
login for several actions therefore runs each action after `SET ROLE` to the narrowest role
that can do it (`narrow`): listing sessions, minting tokens and requesting sessions as
`calu_operator`, changing settings as `calu_admin`. The database then refuses a write that
the action has no business making.
"""

import logging

import psycopg
from psycopg import sql

SESSION = "calu_session"
"""The role of a running session."""

OPERATOR = "calu_operator"
"""The role that watches the sessions and decides escalations."""

ADMIN = "calu_admin"
"""The role that changes what steers the checkers."""

_log = logging.getLogger("calu.db.roles")


async def narrow(conn: psycopg.AsyncConnection, role: str) -> bool:
    """Make `role` the current role of an open connection, for the rest of its life.

    `SET ROLE` keeps the login (`session_user`, which calu records as the actor) and takes
    the privileges, row-level security policies and `current_user` of `role`. A login that
    is not a member of `role` (an operator login asked to act as `calu_admin`), a role that
    does not exist (a schema before migration 0033), or an owner that may not take the role
    (a non-superuser owner on PostgreSQL 16 or later, whose membership lacks the SET option)
    leaves the connection as it was: the narrowing is a guard against mistakes of the
    caller, not the access control itself, so the action still runs with the login's own
    privileges, and the database decides as before.

    Side effects: one `SET ROLE` statement; on a refusal, a log line at info level and, for
    a connection that is not in autocommit mode, a rollback of its open transaction.

    Args:
        conn: An open connection, idle (no statement in flight).
        role: The role to act as, e.g. `OPERATOR`.

    Returns:
        True if the connection now acts as `role`; False if it was left as it was.

    Raises:
        psycopg.OperationalError: If the connection broke.
    """
    try:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        _log.info("the connection keeps its own role, %s refused: %s", role, exc)
        if not conn.autocommit:
            await conn.rollback()
        return False
    return True
