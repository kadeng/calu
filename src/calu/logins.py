"""calu's two logins on a machine, their credential store entries, and what calu says about them.

See agent/records/PLAN.md D265 (the owner's decision of 2026-11-24: one "user" login).

    login      store entry   database role                    used for
    user       `user`        calu_admin                       everything: sessions, the fleet,
                                                              escalations, settings, the web
                                                              interface, the IDEs
    db admin   `db-admin`    the database owner (calu_owner   migrations, `calu setup`,
                             locally; the database user given `calu db reset`
                             at setup for a server)

One password protects both entries: the **user password**, chosen at `calu setup`. The role
`calu_admin` logs in with the same password, so a client that knows only where the database
is (root's place file, `calu.db.place`) logs in with what its user types. An **empty** user
password means that calu never asks for one: both entries are stored without encryption
(the file's mode protects them) and `calu_admin` gets a generated password, because
PostgreSQL lets no role log in with an empty one.

`OLD_ENTRIES` are the entries calu stored before D265 (`admin` the owner, `session`,
`operator`, `calu_admin`); `calu setup` converts them.

The texts below are what a person reads when calu needs the password: which password, why,
and what to do when it is wrong, missing or forgotten. Every front end uses them, and tests
hold their wording (the owner's requirement of 2026-11-24: helpful, explanatory, concise).
This module imports nothing from calu.
"""

USER = "user"
"""The store entry with the login of `calu_admin`, the one login a person uses."""

DB_ADMIN = "db-admin"
"""The store entry with the login of the database owner, for migrations and setup."""

OLD_ENTRIES = ("admin", "session", "operator", "calu_admin")
"""The entries of a store written before D265; `calu setup` replaces them by `USER` and
`DB_ADMIN`."""

MAX_ATTEMPTS = 3
"""How often a terminal command asks the user password before it gives up."""

USER_PROMPT = "calu user password: "
"""The prompt for the user password on a terminal."""

ONCE_PROMPT = (
    "calu user password (asked once, for /running, /switch, /new, /fork and saving settings; "
    "Ctrl-D skips them): "
)
"""The prompt of a client that runs as root for a user and asks the password once (D249)."""

NEW_PROMPT = "New calu user password (Enter for none): "
"""The prompt for a new user password at `calu setup`."""

REPEAT_PROMPT = "Repeat the new password: "
"""The second prompt for a new user password."""

OLD_ADMIN_PROMPT = "Admin password of your earlier calu setup: "
"""The prompt for the password that opens a store of before D265 (`calu setup` converts it)."""

FORGOT = 'Forgot it? DOCUMENTATION.md, "A forgotten user password", says how to set a new one.'
"""The hint after a wrong user password."""

NOT_ENTERED = "Not done: no password was entered. Run the command again to retry."
"""What a terminal command says after Ctrl-D (or Escape, or a closed dialog) at the prompt."""

MISMATCH = "Not changed: the two passwords differ. Run the command again to retry."
"""What `calu setup` says when the repeated new password differs."""

EMPTY_MEANS = (
    "An empty password means calu never asks for one; then every program that runs as you "
    "can use the login, Claude's commands outside the sandbox included (they could decide "
    "their own escalations)."
)
"""The line `calu setup` prints before it asks for a new user password."""


def choose_intro(local: bool) -> str:
    """Explain the new user password before `calu setup` asks for it.

    Args:
        local: Whether the database is calu's local one in Docker, whose owner login the
            same password protects.

    Returns:
        Two sentences: what the password is for, and what an empty one means.
    """
    also = (
        " It also protects the login of the local database's owner (migrations)."
        if local
        else " It also protects the database user's login calu keeps for migrations."
    )
    return (
        "Choose the calu user password: calu asks for it to start sessions, decide "
        f"escalations and change settings.{also}\n{EMPTY_MEANS}"
    )


def wrong(left: int, blank: bool = False) -> str:
    """Say that a typed user password was refused.

    Args:
        left: How many more tries the prompt gives; 0 for none.
        blank: Whether nothing was typed (Enter on an empty prompt).

    Returns:
        For example "That is not the calu user password (2 tries left)." Without tries left
        it ends with `FORGOT`. A blank answer is said to be one.
    """
    what = (
        "No password was typed, and this login has one"
        if blank
        else "That is not the calu user password"
    )
    if left > 0:
        return f"{what} ({left} {'try' if left == 1 else 'tries'} left)."
    return f"Not done: {what[0].lower()}{what[1:]}. {FORGOT}"


def refused(blank: bool = False) -> str:
    """Say that a password was refused, for a front end that asks again by itself.

    Args:
        blank: Whether nothing was typed.

    Returns:
        "That is not the calu user password." (or that none was typed), followed by `FORGOT`.
    """
    what = (
        "No password was typed, and this login has one"
        if blank
        else "That is not the calu user password"
    )
    return f"{what}. {FORGOT}"


def no_terminal(why: str) -> str:
    """Say that the user password is needed and cannot be asked for here.

    Args:
        why: Why there is no terminal, e.g. "its standard input carries the editor's
            protocol".

    Returns:
        The reason and the two ways out.
    """
    return (
        f"calu needs the calu user password and cannot ask for it here: {why}. Either give "
        "it where the front end asks for it, or remove the password (`calu setup "
        "--set-password` and an empty answer; calu then never asks)."
    )


def no_login(hint: str = "") -> str:
    """Say that this machine has no calu login.

    Args:
        hint: Text appended to the sentence, e.g. where root keeps its logins; "" for none.

    Returns:
        The sentence with the command that creates the login.
    """
    return (
        "There is no calu login on this machine: `calu setup` sets up the database and your "
        f"login (for a database another machine set up: `calu credential set user`){hint}"
    )


def old_logins(has_owner: bool) -> str:
    """Say that the store holds the logins of a calu from before D265.

    Args:
        has_owner: Whether the old store holds `admin` (the database owner), which `calu
            setup` needs for the conversion.

    Returns:
        What to run.
    """
    if has_owner:
        return (
            "This machine still has the logins of an earlier calu (admin, session, operator). "
            "Run `calu setup` once: it turns them into the one calu user login and asks for "
            "the earlier admin password."
        )
    return (
        "This machine still has logins of an earlier calu (operator or session) but not the "
        "database owner's, so they cannot be converted here. Remove them (`calu credential "
        "remove operator`, `calu credential remove session`) and store the user login of "
        "that database with `calu credential set user`."
    )


def outdated(place: str) -> str:
    """Say that the database refused a stored login whose password opened the entry.

    Args:
        place: Where the database is (`Target.place`).

    Returns:
        The sentence with the two ways to renew the login: `calu setup` on a machine that
        set the database up, `calu credential set user` on one that was given the login.
    """
    return (
        f"The database {place} refused the stored calu login: it is outdated (its password "
        "was changed by a later setup, or the database was set up again). `calu setup` "
        "renews it on the machine that set the database up; elsewhere, store the new login "
        "with `calu credential set user`."
    )


def refused_by_database(message: str) -> bool:
    """Tell whether a connection error is the server refusing a password.

    Args:
        message: The text of a `psycopg.OperationalError`.

    Returns:
        True for "password authentication failed" and "no password supplied".
    """
    return "password authentication failed" in message or "no password supplied" in message
