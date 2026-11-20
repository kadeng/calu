"""Command line entry point `calu` (agent/records/PLAN.md §8).

    calu db up                     start the local PostgreSQL (Docker) by hand; commands that
                                   need it start it themselves
    calu db down                   stop the local PostgreSQL (data is kept)
    calu db migrate                apply schema migrations (asks for the admin password)
    calu db reset                  drop and recreate everything in the database; asks for the
                                   admin password and a typed confirmation
    calu setup [--database docker|server|rds-iam] [--checker-model M]
                                   on the first run choose and set up the database (a local
                                   one in Docker, an existing server, RDS with IAM); then
                                   migrate, load the default checkers, create the `session`
                                   credential (asks for the passwords)
    calu credential list|set NAME|remove NAME
                                   manage the encrypted credential store
    calu run [DIR]                 start a checked Claude Code session in DIR (asks for the
                                   session password; see `calu.run`)
    calu attach [ADDRESS --token T]
                                   terminal client for the session of this directory, or of
                                   the address given
    calu acp [ADDRESS --token T]   serve that session to an ACP editor (stdin/stdout)
    calu escalation ID             everything needed to decide it, from the database
    calu summary ID [--edit]       the conversation the checkers read, folded
    calu doctor [--db]             what is in place, and what a session would miss
    calu token mint SESSION        client token for an editor or a remote interface
    calu web                       the herd overview in a browser (127.0.0.1 only)
    calu protocol [--out FILE]     JSON Schema of the client protocol, to generate client types
    calu sessions [--all]          list the sessions of all hosts
    calu list-sessions [N]         the last N (5) Claude conversations of this directory, to
                                   resume (`calu run --resume ID`) or fork (`calu run --fork ID`)
    calu escalations [SESSION]     list the open escalations
    calu resolve ID allow|deny [--note TEXT]
                                   resolve an escalation; the waiting session continues
    calu evals [FILE ...]          measure how well the configured checkers decide
    calu project add DIR|list      register a project in the database
    calu config list [--changed]   every setting: effective value, where it comes from, default
    calu config models MODEL|auto  one model for every checker, the narrator and the summarizer
    calu config get|set|unset KEY  configuration values (`claude.model`, `context.narrate`, ...)
    calu checker list|show|set|edit|diff|update
                                   checker definitions; `edit NAME` opens the instructions in
                                   $EDITOR and stores them on save
    calu rules list [LIST] | add LIST PATTERN | edit LIST | remove|enable|disable ID
                                   the allow and deny lists (`edit` opens one in $EDITOR)
    calu honesty show|edit|reset   the honesty rules the change checkers read
    calu honesty trust REV [--note TEXT] | trust --file PATH | trusted | untrust COMMIT
    calu honesty prune --days N    remove the trust calu itself stored longer than N days ago
                                   certify a commit (or a file's content) as honest, list
                                   and remove certifications

Passwords are read from the terminal without echo. If stdin is not a terminal, each password
is read as one line from stdin (for scripts and tests).
"""

import argparse
import asyncio
import contextlib
import dataclasses
import functools
import getpass
import json
import logging
import math
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import psycopg

from calu import (
    __version__,
    admin,
    asuser,
    doctor,
    herd,
    localsocket,
    logfile,
    machine,
    projects_dir,
    servers,
    session_requests,
)
from calu.acp import serve_acp
from calu.attach import attach, unreachable
from calu.claude.transcripts import transcripts, user_inputs
from calu.config import (
    ANTHROPIC_HAIKU,
    BEDROCK_EU_HAIKU,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_NETWORK_POLICY,
    EMPTY_CHAT_SUMMARY,
    PROVIDER_KEYS,
    Config,
    load_secrets,
    merge_settings,
    user_env_file,
)
from calu.configdoc import setting_rows
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db import Db, LoginRefused, migrate
from calu.db import local as local_db
from calu.db import place as db_place
from calu.db import rds as rds_auth
from calu.db import roles as db_roles
from calu.db.connection import open_stored
from calu.db.target import (
    PasswordAuth,
    RdsIamAuth,
    Target,
    TargetError,
    from_fields,
)
from calu.defaults import library
from calu.defaults.instructions import (
    SHIPPED_TEXTS,
    default_redaction_instructions,
    honesty_rules,
    shipped_status,
)
from calu.evals.cases import case_line, cases_to_yaml
from calu.honesty import GitError, content_ids, resolve_commit
from calu.ide import serve_ide
from calu.launch import LAUNCH_ENV, LaunchError, RunAs, claude_path, launcher_path
from calu.localsocket import check_socket_path
from calu.model_choice import detect_model
from calu.paths import INSIDE_SESSION_ENV
from calu.preflight import (
    SANDBOX_PACKAGES,
    login_protection,
    missing_sandbox_tools,
    sandbox_off_warnings,
)
from calu.protocol.client import (
    RelayTarget,
    RequestFailed,
    probe,
    relay_base,
    relay_call,
    relay_session_id,
)
from calu.protocol.messages import MAX_FRAME_BYTES
from calu.protocol.schema import protocol_schema
from calu.run import RunOptions, default_socket_path, harden_process, run_session
from calu.serve import ServeOptions
from calu.serve import os_user as serve_os_user
from calu.serve import serve as serve_sessions
from calu.session_requests import SessionRequest
from calu.setup import (
    ADMIN_ROLE,
    SetupRefused,
    admin_login,
    enable_admin_login,
    enable_operator_login,
    is_admin_password,
    preflight,
    seed_defaults,
    session_can_log_in,
    setup_database,
)
from calu.store import trust
from calu.store.projects import register_directory
from calu.tui import start as tui_start

_log = logging.getLogger("calu.cli")

SECURE_HELP = (
    "every client shows a client token, also a local one: without it, a session that runs as "
    "root lets root clients (`calu attach --sudo`, `calu web --sudo`) in through a root-owned "
    "Unix socket without a token or a password"
)
"""Help text of `--secure` on `calu run` and `calu serve` (D226)."""

INSECURE_HELP = (
    "clients on this machine need no token or password: every connection on a Unix socket "
    "and every TCP connection from a loopback address. Any local process that can reach the "
    "session, Claude Code's own commands included, can then act as a client"
)
"""Help text of `--insecure` on `calu run` and `calu serve` (D227)."""


def _check_security_flags(args: argparse.Namespace) -> None:
    """Refuse `--secure` together with `--insecure`.

    Args:
        args: Parsed arguments (`secure`, `insecure`).

    Raises:
        CliError: If both are given.
    """
    if args.secure and args.insecure:
        raise CliError("--secure and --insecure contradict each other; give one of them")


class CliError(Exception):
    """An error to report to the user without a traceback (exit code 1)."""


class NeedsPassword(CliError):
    """A stored credential needs its password, and the caller said not to ask for one."""


def read_password(prompt: str, confirm: bool = False) -> bytearray:
    """Read a password from the terminal (no echo) or, if stdin is no terminal, from a line.

    Args:
        prompt: The prompt, e.g. "Admin password: ".
        confirm: Ask a second time and require both to match (for new passwords).

    Returns:
        The password; the caller should `wipe` it.

    Raises:
        CliError: If the confirmation does not match, or the terminal's input ended at the
            prompt (Ctrl-D). A blank answer is returned as it is.
    """

    def ask(text: str) -> str:
        """Read one password.

        Args:
            text: The prompt.

        Returns:
            The password without the line end.

        Raises:
            CliError: If the terminal's input ended at the prompt.
        """
        if sys.stdin.isatty():
            try:
                return getpass.getpass(text)
            except EOFError:
                # Ctrl-D: end the line the prompt is on, then say what happened.
                print(file=sys.stderr)
                raise CliError("no password was entered (the input ended)") from None
        return sys.stdin.readline().rstrip("\n")

    # A blank answer is passed on like any other: whether it is right is for the store or
    # the server to say, not for the prompt (the owner, 2026-11-18).
    first = ask(prompt)
    if confirm and ask("Repeat: ") != first:
        raise CliError("passwords do not match")
    return bytearray(first.encode())


_typed: dict[str, str] = {}
"""`admin`: the admin password as it was typed in this command, kept until the command ends
so that `calu setup` can give the role `calu_admin` the same one (agent/records/PLAN.md D213)."""

_admin_place: list[Target] = []
"""The database the last herd command of this process connected to, without any secret: the
login of `calu_admin` there with an empty password. The full-screen client is given it, so
that it can change settings in the database of the session it is attached to."""


_held: list[Target] = []
"""The database login a client that runs as root keeps in memory (`calu attach --sudo`): read
once from root's store, then used for every later step (minting and revoking tokens,
switching sessions, changing settings), because after the switch to the user's file identity
the store cannot be read again, and so that nothing is asked twice (agent/records/PLAN.md D214).
`_hold_for_client` makes it the narrowest one that serves all of them, `calu_admin` where the
database accepts the admin password (D249); each step then narrows the connection further to
the role it needs (`calu.db.roles.narrow`). Never written to disk or passed on."""

_hold: list[bool] = []
"""Non-empty while the next credential a herd command loads is to be kept in `_held`."""


def _client_user(args: argparse.Namespace) -> RunAs | None:
    """Decide whether a client (`calu attach`, `calu start`) runs as root for a user (D214).

        not root, --sudo      ──► calu runs again as root through sudo, with --user <you>
        root, --user X        ──► X: the client keeps its login in memory and does its file
                                  access and starts its programs as X
        root, no --user       ──► None: an ordinary client of root
        not root, no --sudo   ──► None: an ordinary client

    Side effect: with a user, the next credential a herd command loads is kept in memory.

    Args:
        args: Parsed arguments (`user`, `sudo`, `from_sudo`).

    Returns:
        The user the client works for, or None.

    Raises:
        CliError: As `_run_as`.
    """
    if not getattr(args, "sudo", False) and getattr(args, "user", None) is None:
        return None
    run_as = _run_as(args)
    if run_as is not None:
        _hold[:] = [True]
    return run_as


def _become_client_of(run_as: RunAs) -> None:
    """Switch a client that runs as root to the file identity of its user, once.

    Side effects: `calu.asuser.enter`; points `XDG_CONFIG_HOME` at the user's own
    configuration, where the client's `tui.toml` is.

    Args:
        run_as: The user.

    Raises:
        CliError: If the switch is not possible.
    """
    if asuser.active() is not None:
        return
    try:
        asuser.enter(run_as)
    except asuser.UserError as exc:
        raise CliError(str(exc)) from exc
    os.environ["XDG_CONFIG_HOME"] = str(Path(run_as.home) / ".config")


def _remember_admin_password(password: bytes | bytearray) -> None:
    """Keep the admin password that was just typed, for `calu setup` (see `_typed`).

    Args:
        password: The password; it is copied, the caller still wipes its buffer.
    """
    _typed["admin"] = bytes(password).decode()


_READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "config": frozenset({"list", "get"}),
    "checker": frozenset({"list", "show", "diff", "stats", "misses"}),
    "rules": frozenset({"list"}),
    "project": frozenset({"list"}),
    "honesty": frozenset({"show", "trusted"}),
    "traces": frozenset({"show"}),
}
"""Per command that works with `_admin_secret`: its actions that only read. Every other action
of these commands, and every other command, counts as a change and is announced on stderr
(`_announce_change`)."""


def _only_reads(args: argparse.Namespace | None) -> bool:
    """Tell whether a settings command only reads the database.

    Args:
        args: The parsed arguments (`command`, `action`, `edit`), or None for a caller that
            has none.

    Returns:
        True for the actions in `_READ_ONLY_ACTIONS` and for `calu summary` without
        `--edit`; False for everything else, also where it is not known.
    """
    if args is None:
        return False
    command = getattr(args, "command", "")
    if command == "summary":
        return not getattr(args, "edit", False)
    return getattr(args, "action", None) in _READ_ONLY_ACTIONS.get(command, frozenset())


def _announce_change(login: Target) -> None:
    """Say on stderr which database a settings command is about to change, and as which role.

    A person can hold logins of several databases (an `admin` entry of an own setup, an
    `operator` entry of a shared one, the place file of root's database). The line is what
    tells them which one a change went to.

    Args:
        login: The login the command uses.
    """
    print(f"changing settings in {login.place} as {login.user}", file=sys.stderr)


def _admin_secret(store: CredentialStore, args: argparse.Namespace | None = None) -> bytearray:
    """Return a login that may change settings, asking for what it needs.

    See agent/records/PLAN.md D213.

    Without `--database` (`args.database` is None):

        the place file holds a login        ──► that login (`sudo calu setup --no-password`,
                                                D228)
        `calu_admin` stored here
        without a password                  ──► that entry (`calu setup --no-password`)
        only `admin` stored                 ──► that entry (the owner login), with its password
        only `operator` stored              ──► the login of `calu_admin` at the operator's
                                                database, with the admin password typed now;
                                                nothing is stored
        `admin` and `operator` stored,
          the same database (`Target.place`)──► the `admin` entry
          different databases               ──► CliError that names both databases and asks
                                                for `--database admin` or `--database operator`

    With `--database admin` or `--database operator` the named entry is used as in the rows
    "only ... stored", whatever else is stored.

    Side effects: asks for the passwords of the entries it reads (both entries where both
    are stored and none was chosen); an `admin` password typed here stays in `_typed` until
    the command ends (`load_credential`), and so does nothing else; prints the line of
    `_announce_change` to stderr unless the command only reads (`_only_reads`).

    Args:
        store: The credential store.
        args: The command's parsed arguments (`database`, and what `_only_reads` reads), or
            None: no choice, and the command counts as a change.

    Returns:
        The login as the credential store would hold it; the caller wipes it.

    Raises:
        CliError: If no usable credential is stored, the entry `--database` names is not
            stored, or `admin` and `operator` name different databases and none was chosen.
        CredentialError: On a wrong password of a stored entry.
        TargetError: If an entry that had to be read holds no database login this version
            can read.
    """
    login = _admin_login_for(store, getattr(args, "database", None))
    if not _only_reads(args):
        _announce_change(login)
    return bytearray(login.dump())


def _admin_login_for(store: CredentialStore, database: str | None) -> Target:
    """Choose the login `_admin_secret` returns (the table there).

    Args:
        store: The credential store.
        database: "admin", "operator" or None, as `--database` gave it.

    Returns:
        The login.

    Raises:
        CliError: As `_admin_secret`.
        CredentialError: On a wrong password of a stored entry.
        TargetError: If an entry holds no database login this version can read.
    """

    def through_operator() -> Target:
        """Build the login of `calu_admin` at the operator's database.

        Returns:
            The login, with the admin password that was typed now.

        Raises:
            CliError: If the admin password is left blank: PostgreSQL accepts no blank
                password, and a database set up with `--no-password` has a generated one
                that only its own machine holds (test plan J, O5).
        """
        place = load_target(store, "operator", "Operator password: ")
        password = read_password(f"Admin password of {place.place} (the role calu_admin): ")
        try:
            if not password.strip():
                raise CliError(
                    f"no admin password was typed: changing settings in {place.place} needs "
                    "the password of its role calu_admin, which only that database's own "
                    "setup gives. After `calu setup --no-password` it is a generated one that "
                    "only the machine of that setup holds; there `calu setup --set-passwords` "
                    "sets one a person can type, or run the command on that machine"
                )
            return admin_login(place, password.decode())
        finally:
            wipe(password)

    if database is not None:
        if database not in store:
            raise CliError(
                f"--database {database}: no credential {database!r} in {store.path} "
                f"(`calu credential list` shows what is stored)"
            )
        if database == "operator":
            return through_operator()
        return load_target(store, "admin", "Admin password: ")
    shared = _shared_admin()
    if shared is not None:
        # `sudo calu setup --no-password` (D228): the login of the place file, before any
        # entry of this user's own store, as the fleet commands choose (`_herd_value`).
        return shared
    if ADMIN_ENTRY in store and not store.needs_password(ADMIN_ENTRY):
        # A generated password that only the store holds: nobody knows one to type.
        return load_target(store, ADMIN_ENTRY, "")
    if "operator" not in store:
        return load_target(store, "admin", "Admin password: ")
    if "admin" not in store:
        return through_operator()
    # Both are stored. Which database is meant is known only if both name the same one; the
    # entries are encrypted, so both are read before anything is changed.
    owner = load_target(store, "admin", "Admin password: ")
    operator = load_target(store, "operator", "Operator password: ")
    if owner.place != operator.place:
        raise CliError(
            "two databases are stored here and the command does not say which one to work "
            f"on: the credential `admin` names {owner.place}, the credential `operator` "
            f"names {operator.place}. Repeat the command with `--database admin` or "
            "`--database operator` (with `operator`, the admin password of that database "
            "is asked)"
        )
    return owner


def _root_store_hint(store: CredentialStore) -> str:
    """Say how a calu that was started through sudo gets the invoking user's credentials.

    A root calu keeps its credentials in `/etc/calu` (agent/records/PLAN.md D212), so what the user
    set up in the own home directory is not seen after `--sudo`.

    Args:
        store: The credential store that lacks the credential.

    Returns:
        "" unless calu runs as root through sudo for a named user; else a sentence with the
        two commands that copy that user's credential store, and the alternative.
    """
    user = os.environ.get("SUDO_USER")
    if os.geteuid() != 0 or not user:
        return ""
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        return ""
    return (
        f". calu as root keeps its credentials in {store.path.parent}, not in your home "
        f"directory. To use the ones you have: `sudo install -d -m 700 {store.path.parent}` "
        f"and `sudo install -m 600 {home}/.config/calu/credentials.json {store.path}` (the "
        "entries stay encrypted with their passwords; copy `.env` with the checker keys the "
        "same way). Or set up as root: `sudo calu setup`"
    )


def load_credential(store: CredentialStore, name: str, prompt: str) -> bytearray:
    """Return the secret of credential `name`, asking for its password if it has one.

    An entry stored with `calu credential set <name> --no-password` is read without asking: it is
    protected by the file's mode rather than by a password, and prompting for a password that is
    then thrown away would teach the user that the prompt means nothing (agent/records/PLAN.md
    D104).

    Args:
        store: The credential store.
        name: The credential name.
        prompt: The password prompt, used only for an encrypted entry.

    Returns:
        The decrypted secret; the caller should `wipe` it.

    Raises:
        CliError: If the credential does not exist.
        CredentialError: If the password is wrong or the store is corrupt.
    """
    if name not in store:
        raise CliError(
            f"no credential {name!r} in {store.path}; run `calu setup` or `calu credential "
            f"set {name}`{_root_store_hint(store)}"
        )
    if not store.needs_password(name):
        return store.load(name)
    password = read_password(prompt)
    try:
        secret = store.load(name, password)
        if name == "admin":
            _remember_admin_password(password)
        return secret
    finally:
        wipe(password)


def ask(prompt: str, default: str | None, flag: str) -> str:
    """Ask one setup question that is no secret.

    Args:
        prompt: The question without the default, e.g. "Port on 127.0.0.1".
        default: What an empty answer means; None if an answer is required.
        flag: The command-line option that gives the answer, for the error message.

    Returns:
        The answer. Without a terminal nothing is asked: the default is returned.

    Raises:
        CliError: If an answer is required and none was given, or there is no terminal to ask
            on and no default (the message names the option to pass).
    """
    if not sys.stdin.isatty():
        if default is None:
            raise CliError(f"{flag} is needed (there is no terminal to ask on)")
        return default
    shown = f"{prompt} ({default}): " if default else f"{prompt}: "
    answer = input(shown).strip() or (default or "")
    if not answer:
        raise CliError(f"{prompt.lower()} must not be empty")
    return answer


def _store_new_admin(store: CredentialStore, owner: Target, password: bytearray | None) -> None:
    """Store the owner login of a database that was just chosen, as `admin`.

    Side effects: saves the `admin` entry; removes a `session` entry, which belongs to the
    database set up before (there was no `admin` entry, so this is another database or one
    set up again). Removing it now, and not only when the setup succeeds, makes a setup
    that stops later (docker fails, the role `calu_session` has a login, a wrong password)
    renew the session login when it runs again, instead of keeping the old one.

    Args:
        store: The credential store; it holds no `admin` entry.
        owner: The owner login.
        password: The admin password, which encrypts the entry (the caller wipes it); None
            stores the entry protected by the file's mode alone (`calu setup
            --no-password`).
    """
    store.save("admin", owner.dump(), password)
    if password is not None:
        _remember_admin_password(password)
    if store.remove("session"):
        print(
            "removed the stored 'session' credential, which belongs to the database set up before"
        )


def _new_admin_password(no_password: bool, protects: str) -> bytearray | None:
    """Ask for the new admin password, unless the setup is to have none.

    Args:
        no_password: `calu setup --no-password`: nothing is asked.
        protects: What the password protects, for the line printed before the question.

    Returns:
        The password (the caller wipes it), or None with `no_password`.

    Raises:
        CliError: If the passwords do not match or the input ended (Ctrl-D); a blank password
            is accepted.
    """
    if no_password:
        return None
    print(f"Choose the admin password (it protects {protects}).")
    return read_password("New admin password: ", confirm=True)


def _first_start_local(
    store: CredentialStore, port: int, data: str, no_password: bool = False
) -> Target:
    """Create the local database: store the `admin` credential, then start the container.

    The credential is saved before the container starts, so that a start that fails can be
    repeated with `calu db up`: the generated owner password is not lost. A stored `session`
    entry is removed then (`_store_new_admin`).

    Args:
        store: The credential store; it must hold no `admin` entry.
        port: The port on 127.0.0.1.
        data: An absolute directory prepared with `local_db.prepare_data_dir`, or
            `local_db.VOLUME_DATA`.
        no_password: Ask for no admin password; the entry is protected by the file's mode.

    Returns:
        The owner login.

    Raises:
        CliError: If the passwords do not match or the input ended (Ctrl-D); a blank password
            is accepted.
        local_db.DockerError: If docker fails.
    """
    password = _new_admin_password(no_password, "the database owner login")
    owner = local_db.owner_target(local_db.new_owner_password(), port, data)
    try:
        _store_new_admin(store, owner, password)
    finally:
        if password is not None:
            wipe(password)
    assert isinstance(owner.auth, PasswordAuth)
    local_db.up(port, owner.auth.password, data)
    where = "the Docker volume calu-db_data" if data == local_db.VOLUME_DATA else data
    print(f"local database initialized in {where}, running on 127.0.0.1:{port}")
    return owner


def load_target(store: CredentialStore, name: str, prompt: str) -> Target:
    """Read a stored database login, asking for the entry's password if it has one.

    Args:
        store: The credential store.
        name: The credential name.
        prompt: The password prompt.

    Returns:
        The login. It holds the password of a password login; it is not wiped, because the
        command that asked for it uses it until it ends.

    Raises:
        CliError: If the credential does not exist.
        CredentialError: If the password is wrong or the store is corrupt.
        TargetError: If the entry holds no database login this version can read.
    """
    secret = load_credential(store, name, prompt)
    try:
        return Target.parse(secret)
    finally:
        wipe(secret)


def cmd_db_up(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start the local database; without an `admin` credential create the database first.

    With an `admin` credential the port and the data location come from it, so the command
    asks for the admin password. `calu run` and the other commands start a stopped local
    database themselves; this command is for starting it by hand.

    Args:
        args: Parsed arguments (`port`, `data_dir`).
        store: The credential store.

    Raises:
        CliError: If the stored database is not the local one, or an option contradicts what
            is stored.
        CredentialError: On a wrong password.
        local_db.DockerError: If docker fails.
    """
    if "admin" not in store:
        data = str(local_db.prepare_data_dir(Path(args.data_dir or local_db.default_data_dir())))
        _first_start_local(store, args.port or local_db.DEFAULT_PORT, data)
        print("next: calu setup")
        return
    owner = load_target(store, "admin", "Admin password: ")
    block = owner.docker
    if block is None:
        raise CliError(
            f"the stored database is {owner.place}, not a local one that calu starts; "
            "`calu credential remove admin` first if a local database is wanted instead"
        )
    if args.port not in (None, block.port):
        raise CliError(f"the local database was set up on port {block.port}, not {args.port}")
    chosen = (
        os.path.normpath(os.path.abspath(Path(args.data_dir).expanduser()))
        if args.data_dir
        else None
    )
    if chosen is not None and chosen != os.path.normpath(block.data):
        raise CliError(f"the local database was set up in {block.data}")
    first = not local_db.initialized(block.data)
    password = owner.auth.password if isinstance(owner.auth, PasswordAuth) and first else None
    local_db.up(block.port, password, block.data)
    state = "initialized and running" if first else "running"
    print(f"local database {state} on 127.0.0.1:{block.port}")


def cmd_db_down(args: argparse.Namespace, store: CredentialStore) -> None:
    """Stop the local database.

    Args:
        args: Parsed arguments (unused).
        store: The credential store (unused).

    Raises:
        local_db.DockerError: If docker fails.
    """
    local_db.down()
    print("local database stopped (data kept); the next calu command that needs it starts it")


async def _migrate(url: bytearray) -> int:
    """Apply migrations with the given owner connection string.

    Args:
        url: The owner connection string.

    Returns:
        The number of migrations applied.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await open_stored(url)
    try:
        return len(await migrate(conn))
    finally:
        await conn.close()


def cmd_db_migrate(args: argparse.Namespace, store: CredentialStore) -> None:
    """Apply schema migrations as the database owner.

    Args:
        args: Parsed arguments (unused).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    url = load_credential(store, "admin", "Admin password: ")
    try:
        count = asyncio.run(_migrate(url))
    finally:
        wipe(url)
    print(f"migrations applied: {count}")


RESET_PHRASE = "yes delete the db"
"""What `calu db reset` asks the user to type before it deletes anything."""

RESET_MIN_SECONDS = 2.0
"""The least time typing `RESET_PHRASE` may take. A phrase that arrives faster was not typed
by a person looking at the prompt (piped input, a pasted line, an automatic "yes")."""

RESET_COUNTS = (
    ("sessions", "session(s)"),
    ("steps", "step(s)"),
    ("escalations", "escalation(s)"),
    ("projects", "project(s)"),
    ("rules", "rule(s)"),
    ("checker_types", "checker definition(s)"),
    ("trusted_commits", "trusted commit(s)"),
    ("trusted_files", "trusted file content(s)"),
)
"""The tables whose row counts `calu db reset` shows before it asks: (table, what a row is)."""


async def _reset_summary(url: bytearray) -> list[str]:
    """Describe what a reset of the database would delete.

    Args:
        url: The owner connection string.

    Returns:
        One line per table of `RESET_COUNTS` that exists, with its row count, and one line
        about sessions that have not ended; [] for a database without calu's tables.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await open_stored(url)
    try:
        db = Db(conn)
        lines: list[str] = []
        for table, what in RESET_COUNTS:
            exists = await db.one("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
            if not exists or not exists[0]:
                continue
            # `table` is one of the fixed names of `RESET_COUNTS`, never input.
            count = await db.scalar(f"SELECT count(*) FROM {table}")
            lines.append(f"  {count} {what}")
            if table == "sessions":
                running = await db.scalar("SELECT count(*) FROM sessions WHERE ended_at IS NULL")
                if running:
                    lines.append(
                        f"  {running} of them have not ended: a session that still runs loses "
                        "its rows and stops"
                    )
        return lines
    finally:
        await conn.close()


async def _reset(url: bytearray) -> str:
    """Drop everything calu keeps in the database and create it again, empty.

    The schema `public` is dropped with all its tables, functions and types and created anew;
    then the migrations run and the default checker definitions are loaded, as on a fresh
    database. The roles and their passwords are not part of the schema and stay, so the
    stored `session` and `operator` credentials keep working.

    Args:
        url: The owner connection string.

    Returns:
        A report of what was created.

    Raises:
        psycopg.Error: On database errors. The drop and the re-creation of the schema are one
            transaction; a failure there leaves the database as it was.
    """
    conn = await open_stored(url)
    try:
        async with conn.transaction():
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            # A new schema has no privileges for other roles; PostgreSQL's own `public` lets
            # every role look names up in it, and the migrations' grants build on that.
            await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        applied = await migrate(conn)
        checkers, _rules = await seed_defaults(conn)
        return (
            f"the database is empty again: {len(applied)} migration(s) applied, {checkers} "
            "default checker definition(s) loaded, no rules, no projects, no settings.\n"
            "The stored credentials still work. Register your projects again with "
            "`calu project add`."
        )
    finally:
        await conn.close()


def confirm_reset(
    read: Callable[[str], str] = input, clock: Callable[[], float] = time.monotonic
) -> None:
    """Ask the user to type `RESET_PHRASE`, and refuse an answer that came too fast.

    Args:
        read: Shows a prompt and returns the line typed; `input` by default.
        clock: A monotonic clock in seconds.

    Raises:
        CliError: If the line is not exactly `RESET_PHRASE`, if it arrived in less than
            `RESET_MIN_SECONDS` after the prompt, or if the input ended.
    """
    started = clock()
    try:
        answer = read(f'Type "{RESET_PHRASE}" to delete everything: ')
    except EOFError as exc:
        raise CliError("no confirmation was given; nothing was deleted") from exc
    elapsed = clock() - started
    if answer.strip() != RESET_PHRASE:
        raise CliError("the confirmation does not match; nothing was deleted")
    if elapsed < RESET_MIN_SECONDS:
        raise CliError(
            f"the confirmation arrived after {elapsed:.1f} s, faster than a person types it; "
            "nothing was deleted. Run the command again and type the phrase by hand"
        )


def cmd_db_reset(args: argparse.Namespace, store: CredentialStore) -> None:
    """Delete everything in calu's database and set it up again, empty.

    Every table is dropped and created anew, not emptied: sessions, steps, escalations,
    projects, rules, checker instructions and all settings are gone, and the defaults of a
    fresh database are loaded. Two things stand in front of that: the `admin` credential's
    password, which must be typed (a credential stored without a password is refused here),
    and the phrase `RESET_PHRASE`, typed in not less than `RESET_MIN_SECONDS`.

    Args:
        args: Parsed arguments (unused).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, it has no password, or the confirmation
            is missing, wrong or too fast.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if "admin" in store and not store.needs_password("admin"):
        raise CliError(
            "the `admin` credential is stored without a password, and a reset must be confirmed "
            "with one. Store it with a password first: `calu credential set admin`"
        )
    url = load_credential(store, "admin", "Admin password: ")
    try:
        lines = asyncio.run(_reset_summary(url))
        print("This deletes the whole calu database and creates it again, empty:")
        print("\n".join(lines) if lines else "  (the database holds no calu tables yet)")
        print("Checker instructions, rules and every setting are lost. This cannot be undone.")
        confirm_reset()
        print(asyncio.run(_reset(url)))
    finally:
        wipe(url)


def _chosen_model(requested: str | None, effort: str | None) -> tuple[str, str | None, str]:
    """Turn `MODEL` or `auto` into a model, an effort and a line saying how it was chosen.

    Args:
        requested: A Pydantic AI model string, "auto", None (the same as "auto"), or "default"
            for no stored model, which makes every role use the default model.
        effort: An effort the user asked for, which wins over the detected one.

    Returns:
        (model string, "" for "default"; checker effort or None; an explanation for the
        report).

    Raises:
        CliError: If "auto" finds no credentials at all; the message says what to add, and that
            `claude:haiku` needs none.
    """
    if requested == "default":
        return (
            "",
            effort,
            "the default model: " + ANTHROPIC_HAIKU + ", or in Bedrock mode " + (BEDROCK_EU_HAIKU),
        )
    if requested and requested != "auto":
        return requested, effort, "as given"
    choice = detect_model(os.environ)  # `main` has loaded calu's env file into it
    if choice is None:
        raise CliError(
            "no model provider found: no AWS credentials that STS accepts, and none of "
            "ANTHROPIC_API_KEY, FIREWORKS_API_KEY, MISTRAL_API_KEY in the environment or in "
            f"{user_env_file()}. Add one, or give a model explicitly -- `claude:haiku` runs the "
            "checks on this machine's own Claude Code account and needs no key"
        )
    return choice.model, effort or choice.effort, f"{choice.provider}: {choice.reason}"


async def _has_checker_types(db: Db) -> bool:
    """Tell whether the database already holds checker definitions.

    Args:
        db: A connection of the database owner.

    Returns:
        False on a fresh database -- before the first migration the table does not even exist.
    """
    exists = await db.one("SELECT to_regclass('checker_types') IS NOT NULL")
    if not exists or not exists[0]:
        return False
    counted = await db.one("SELECT count(*) FROM checker_types")
    return bool(counted and counted[0])


VARIANTS = ("docker", "server", "rds-iam")
"""The values of `calu setup --database`, in the order the question lists them."""


def _ask_variant(chosen: str | None) -> str:
    """Decide which kind of database `calu setup` sets up.

    Args:
        chosen: The value of `--database`, or None.

    Returns:
        One of `VARIANTS`; without a terminal and without the option, "docker".

    Raises:
        CliError: If the answer is none of the three.
    """
    if chosen:
        return chosen
    print(
        "Which database should calu use?\n"
        "  1  a local PostgreSQL in Docker, created and started by calu\n"
        "  2  a PostgreSQL server that exists already (host, port, user, password, database)\n"
        "  3  an AWS RDS instance with IAM authentication"
    )
    answer = ask("Database", "1", "--database")
    by_number = {"1": "docker", "2": "server", "3": "rds-iam"}
    variant = by_number.get(answer, answer)
    if variant not in VARIANTS:
        raise CliError(f"{answer!r} is none of 1, 2, 3 (or docker, server, rds-iam)")
    return variant


def _port(text: str) -> int:
    """Read a port number from an answer.

    Args:
        text: The answer.

    Returns:
        The port.

    Raises:
        CliError: If it is no number between 1 and 65535.
    """
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise CliError(f"{text!r} is no port number")
    return int(text)


def _keep_ca_file(store: CredentialStore, source: str) -> str:
    """Copy a certificate authority file next to the credential store.

    The connection then does not depend on a file in a download directory, and the copy is
    in a directory only this user can read.

    Args:
        store: The credential store, for its directory.
        source: The file the user named.

    Returns:
        The absolute path of the copy (`db-ca.pem` in the store's directory, mode 0600).

    Raises:
        CliError: If the file cannot be read or holds no certificate.
    """
    try:
        content = Path(source).expanduser().read_bytes()
    except OSError as exc:
        raise CliError(f"cannot read the CA file {source}: {exc}") from exc
    if b"-----BEGIN CERTIFICATE-----" not in content:
        raise CliError(f"{source} holds no PEM certificate")
    target = store.path.parent / "db-ca.pem"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(0o600)
    return str(target)


async def _check_owner_login(owner: Target) -> None:
    """Connect with a new owner login and check that calu can be set up there.

    Args:
        owner: The login `calu setup` was given.

    Raises:
        CliError: If the connection fails (the message holds the server's answer) or the
            preflight finds a problem (the message lists them with the SQL that fixes each).
    """
    try:
        conn = await open_stored(owner)
    except psycopg.OperationalError as exc:
        raise CliError(f"cannot connect to {owner.place} as {owner.user}: {exc}") from exc
    try:
        problems = await preflight(conn)
    finally:
        await conn.close()
    if problems:
        lines = "\n".join(f"- {line}" for line in problems)
        raise CliError(f"calu cannot be set up in {owner.place} as it is:\n{lines}")


def _setup_server(args: argparse.Namespace, store: CredentialStore) -> Target:
    """Ask for a PostgreSQL server that exists already and check the login.

    Nothing is stored here: the caller saves the login after it has been checked.

    Args:
        args: Parsed arguments (`host`, `port`, `dbname`, `user`, `sslmode`, `sslrootcert`).
        store: The credential store, for the directory a CA file is copied into.

    Returns:
        The owner login, checked with a connection and the preflight.

    Raises:
        CliError: If an answer is missing or invalid, the connection fails, or the preflight
            finds a problem.
    """
    print(
        "The user below becomes the owner login of calu's database: it creates the tables and "
        "the two roles calu's sessions and operators log in as. It needs a database of its "
        "own and the right to create roles; it does not need to be a superuser."
    )
    host = args.host or ask("Host", None, "--host")
    port = args.port or _port(ask("Port", "5432", "--port"))
    dbname = args.dbname or ask("Database", "calu", "--dbname")
    user = args.user or ask("User", None, "--user")
    password = read_password("Password of that user (not echoed): ")
    try:
        ca = _keep_ca_file(store, args.sslrootcert) if args.sslrootcert else None
        owner = from_fields(
            host, port, dbname, user, PasswordAuth(password=password.decode()), args.sslmode, ca
        )
    finally:
        wipe(password)
    if owner.conninfo.get("sslmode") == "disable":
        print("warning: sslmode disable sends the password and all data unencrypted.")
    asyncio.run(_check_owner_login(owner))
    return owner


def _setup_rds(args: argparse.Namespace, store: CredentialStore) -> Target:
    """Ask for an AWS RDS instance with IAM authentication and check the login.

    No database password is asked: the login authenticates with a token that AWS signs for
    the named profile at every connection.

    Args:
        args: Parsed arguments (`host`, `port`, `dbname`, `user`, `region`, `aws_profile`,
            `sslrootcert`).
        store: The credential store, for the directory the CA file is copied into.

    Returns:
        The owner login, checked with a connection and the preflight.

    Raises:
        CliError: If an answer is missing or invalid, no token can be made, the connection
            fails, or the preflight finds a problem.
    """
    print(
        "The user below becomes the owner login of calu's database. An administrator prepares "
        "it once: CREATE ROLE <user> LOGIN CREATEROLE; GRANT rds_iam TO <user> WITH ADMIN "
        "OPTION; CREATE DATABASE calu OWNER <user>; and an IAM policy that allows "
        "rds-db:connect for that database user (and for calu_session, if sessions use IAM "
        "as well)."
    )
    host = args.host or ask("Endpoint host", None, "--host")
    port = args.port or _port(ask("Port", "5432", "--port"))
    dbname = args.dbname or ask("Database", "calu", "--dbname")
    user = args.user or ask("User (a database role with rds_iam)", None, "--user")
    region = args.region or ask("AWS region", rds_auth.region_of(host), "--region")
    profile = args.aws_profile or ask("AWS profile that may connect", None, "--aws-profile")
    source = args.sslrootcert or ask(
        "CA bundle file (global-bundle.pem from AWS)", None, "--sslrootcert"
    )
    ca = _keep_ca_file(store, source)
    owner = from_fields(
        host, port, dbname, user, RdsIamAuth(region=region, profile=profile), "verify-full", ca
    )
    asyncio.run(_check_owner_login(owner))
    return owner


_SETUP_OPTIONS = (
    ("data_dir", "--data-dir", ("docker",)),
    ("port", "--port", ("docker", "server", "rds-iam")),
    ("host", "--host", ("server", "rds-iam")),
    ("dbname", "--dbname", ("server", "rds-iam")),
    ("user", "--user", ("server", "rds-iam")),
    ("sslmode", "--sslmode", ("server",)),
    ("sslrootcert", "--sslrootcert", ("server", "rds-iam")),
    ("region", "--region", ("rds-iam",)),
    ("aws_profile", "--aws-profile", ("rds-iam",)),
)
"""(attribute, option, variants it applies to) of the `calu setup` options that describe a new
database. `--sslmode` is not for rds-iam: IAM authentication always uses `verify-full`."""


def _check_setup_options(args: argparse.Namespace, variant: str | None) -> None:
    """Refuse `calu setup` options that would be ignored, before anything is asked or stored.

    Args:
        args: Parsed arguments of `calu setup`.
        variant: The variant of the new database; None if a database is stored already
            (the `admin` entry), whose options are then all fixed.

    Raises:
        CliError: If an option of `_SETUP_OPTIONS` was given for a stored database or for a
            variant it does not apply to, or a session option (`--session-auth`,
            `--session-aws-profile`) for a variant other than rds-iam,
            `--session-aws-profile` together with `--session-auth password`, or
            `--set-passwords` together with `--no-password`.
    """
    for attribute, option, variants in _SETUP_OPTIONS:
        if getattr(args, attribute) is None:
            continue
        if variant is None:
            raise CliError(
                f"{option} describes a new database, and one is set up already (the `admin` "
                "credential); leave it out, or `calu credential remove admin` first to set up "
                "another one"
            )
        if variant not in variants:
            raise CliError(
                f"{option} does not apply to the database {variant}; it is for "
                f"{' and '.join(variants)}"
            )
    if args.set_passwords and args.no_password:
        raise CliError("--set-passwords and --no-password contradict each other; give one")
    if variant not in (None, "rds-iam") and (args.session_auth or args.session_aws_profile):
        raise CliError("--session-auth and --session-aws-profile are for the database rds-iam")
    if args.session_auth == "password" and args.session_aws_profile:
        raise CliError("--session-aws-profile is for --session-auth iam, not password")


SANDBOX_KEY = "claude.sandbox.enabled"
"""The setting `calu setup` asks about (requirement 42)."""

_TOOL_PACKAGES = {"bwrap": "bubblewrap (bwrap)", "socat": "socat"}
"""How the setup question names a program of `calu.preflight.SANDBOX_TOOLS`."""


def sandbox_intro(missing: Sequence[str]) -> str:
    """Explain the sandbox question of `calu setup` in two lines.

    Args:
        missing: The programs of `calu.preflight.SANDBOX_TOOLS` that are not installed.

    Returns:
        What the sandbox does, and whether bubblewrap and socat are installed; if not, the
        packages to install and what a session with the sandbox on then does.
    """
    what = (
        "Claude Code's sandbox runs Claude's Bash commands with calu's own files hidden and "
        "with network access only through a filtering proxy. It needs bubblewrap and socat"
    )
    if not missing:
        return f"{what}: both are installed."
    named = " and ".join(_TOOL_PACKAGES.get(tool, tool) for tool in missing)
    return (
        f"{what}: {named} {'is' if len(missing) == 1 else 'are'} not installed (Debian/Ubuntu: "
        f"`sudo apt install {SANDBOX_PACKAGES}`).\nWith the sandbox on, a session refuses "
        "to start until they are installed."
    )


def choose_sandbox(flag: bool | None, stored: bool | None, missing: Sequence[str]) -> bool | None:
    """Decide what `calu setup` stores as `claude.sandbox.enabled` (requirement 42).

        --sandbox / --no-sandbox    ──► that value
        a terminal                  ──► the question; default: the stored value, else on
                                        when bubblewrap and socat are installed, else off
        no terminal, no option      ──► None: the stored value (or the default, on) stays

    Side effects: on a terminal without the option, prints `sandbox_intro` and the stored
    value, and reads one answer.

    Args:
        flag: The option (`--sandbox` True, `--no-sandbox` False), or None.
        stored: The value the global settings hold, or None if they hold none.
        missing: The sandbox programs that are not installed (`missing_sandbox_tools`).

    Returns:
        The value to store, or None to leave the settings as they are.

    Raises:
        CliError: If the answer is none of on, off, yes, no (y, n).
    """
    if flag is not None:
        return flag
    if not sys.stdin.isatty():
        return None
    print(sandbox_intro(missing))
    if stored is not None:
        print(f"Stored now: {'on' if stored else 'off'}.")
    default = stored if stored is not None else not missing
    answer = ask(
        "Switch Claude Code's sandbox on? on/off",
        "on" if default else "off",
        "--sandbox or --no-sandbox",
    ).lower()
    if answer in ("on", "yes", "y"):
        return True
    if answer in ("off", "no", "n"):
        return False
    raise CliError(f"{answer!r} is neither on nor off")


async def _offer_instruction_updates(db: Db, flag: bool | None) -> list[str]:
    """Say which stored instruction texts are older shipped ones, and offer to update them.

    Side effects: on a terminal without the option, prints the outdated checker texts and
    asks once whether to replace them with the present shipped ones; replaces them on a yes
    or with `--update-instructions`. Without a terminal and without the option, and with
    `--no-update-instructions`, it keeps them and reports each with the commands that update
    it (the owner's decision of 2026-11-20, agent/records/PLAN.md D250: a script changes no
    instructions unasked). A text the user edited is never changed, and a stored setting
    (`honesty.rules`, `traces.redactor_instructions`) is only reported, with the command that
    resets it (D248).

    Args:
        db: A connection of the database owner.
        flag: `--update-instructions` (True), `--no-update-instructions` (False), or None.

    Returns:
        Lines for the setup report: one per updated, kept or edited text; none when every
        stored text is the present shipped one.

    Raises:
        AdminError: If an update is refused.
        psycopg.Error: On database errors.
    """
    states = await admin.instruction_states(db)
    outdated = [s for s in states if s.status.kind == "outdated" and s.checker]
    lines: list[str] = []
    if outdated:
        update = flag
        if update is None and sys.stdin.isatty():
            print("Stored checker instructions that are an older version calu shipped, unchanged:")
            for state in outdated:
                print(f"  {state.checker}")
            answer = ask(
                "Replace them with the present shipped versions? yes/no",
                "yes",
                "--update-instructions or --no-update-instructions",
            ).lower()
            update = answer in ("yes", "y")
        for state in outdated:
            if update:
                row = await admin.update_instructions(db, state.checker)
                lines.append(
                    f"{state.what}: updated to the present shipped version (v{row.version})"
                )
            else:
                lines.append(state.sentence())
        if not update:
            lines.append(
                f"{len(outdated)} outdated shipped text(s) kept; `calu setup "
                "--update-instructions` replaces them all"
            )
    for state in states:
        if state.status.kind == "edited" or (state.status.kind == "outdated" and not state.checker):
            lines.append(state.sentence())
    return lines


async def _apply_sandbox_choice(db: Db, flag: bool | None) -> tuple[bool, str]:
    """Ask about the sandbox (`choose_sandbox`) and store the answer in the global settings.

    Side effects: what `choose_sandbox` prints and reads; writes `claude.sandbox.enabled`
    into the global settings when the chosen value differs from the stored one, or none is
    stored. A setting that is not asked about (no terminal, no option) is never changed.

    Args:
        db: A connection of the database owner.
        flag: `--sandbox` (True), `--no-sandbox` (False), or None.

    Returns:
        (whether the sandbox is on for sessions without a project setting of their own,
        one line for the setup report).

    Raises:
        CliError: On an answer that is neither on nor off.
        AdminError: If the settings cannot be validated.
        psycopg.Error: On database errors.
    """
    current = await admin.settings(db)
    claude_part = current.get("claude")
    sandbox_part = claude_part.get("sandbox") if isinstance(claude_part, dict) else None
    value = sandbox_part.get("enabled") if isinstance(sandbox_part, dict) else None
    stored = value if isinstance(value, bool) else None
    chosen = choose_sandbox(flag, stored, missing_sandbox_tools())
    if chosen is None:
        enabled = True if stored is None else stored
        how = "the default" if stored is None else "as stored"
        return enabled, (
            f"Claude Code's sandbox: {'on' if enabled else 'off'} ({how}; `calu setup "
            "--sandbox` or `--no-sandbox` sets it)"
        )
    if chosen != stored:
        await admin.set_setting(db, SANDBOX_KEY, chosen)
        return chosen, f"Claude Code's sandbox: {'on' if chosen else 'off'} (stored)"
    return chosen, f"Claude Code's sandbox: {'on' if chosen else 'off'} (as stored)"


def sandbox_setup_notes(enabled: bool, store: CredentialStore) -> list[str]:
    """Return the warnings `calu setup` prints about the sandbox after everything is stored.

    Args:
        enabled: Whether the sandbox is on (`_apply_sandbox_choice`).
        store: The credential store, as the setup left it.

    Returns:
        With the sandbox on: one warning if bubblewrap or socat is missing (sessions refuse
        to start then), else []. With it off: the recommendation of requirement 42
        (`calu.preflight.sandbox_off_warnings`), unless it is met: every stored login has a
        password, or the setup runs as root (the store is root's alone and sessions run
        with --sudo), and the place file publishes no password of `calu_admin`.
    """
    if enabled:
        missing = missing_sandbox_tools()
        if not missing:
            return []
        return [
            f"the sandbox is on and {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'}"
            f" not installed, so sessions refuse to start: `sudo apt install {SANDBOX_PACKAGES}`"
            " (Debian/Ubuntu), or `calu setup --no-sandbox`"
        ]
    off = Config.model_validate({"claude": {"sandbox": {"enabled": False}}})
    return sandbox_off_warnings(off, login_protection(store, os.geteuid() == 0))


def _check_session_options(args: argparse.Namespace, owner: Target, renew: bool) -> None:
    """Refuse session options that the stored database or the kept session login ignore.

    Args:
        args: Parsed arguments of `calu setup` (`session_auth`, `session_aws_profile`).
        owner: The owner login of the database.
        renew: Whether this setup gives `calu_session` a new login.

    Raises:
        CliError: If a session option is given for a database without IAM authentication,
            or while the session login is kept (no `--renew-session`).
    """
    if not (args.session_auth or args.session_aws_profile):
        return
    if not isinstance(owner.auth, RdsIamAuth):
        raise CliError(
            "--session-auth and --session-aws-profile are for a database with IAM "
            f"authentication; {owner.place} is not set up with it"
        )
    if not renew:
        raise CliError(
            "--session-auth and --session-aws-profile change the session login, which this "
            "setup keeps; add --renew-session to replace it"
        )


def _new_database(args: argparse.Namespace, store: CredentialStore) -> Target:
    """Set up the database of the chosen variant and store its owner login as `admin`.

    Args:
        args: Parsed arguments of `calu setup`.
        store: The credential store; it holds no `admin` entry.

    Returns:
        The owner login.

    Side effects: stores `admin` and removes a stored `session` entry (`_store_new_admin`).

    Raises:
        CliError: On an option that does not apply to the chosen variant
            (`_check_setup_options`), an invalid answer, a failed connection or a failed
            preflight; nothing is stored then, except for the local database, whose
            credential is stored before its container starts.
        local_db.DockerError: If docker fails.
    """
    variant = _ask_variant(args.database)
    _check_setup_options(args, variant)
    if variant == "docker":
        chosen = args.data_dir or ask(
            "Data directory", str(local_db.default_data_dir()), "--data-dir"
        )
        port = args.port or _port(ask("Port on 127.0.0.1", str(local_db.DEFAULT_PORT), "--port"))
        data = str(local_db.prepare_data_dir(Path(chosen)))
        return _first_start_local(store, port, data, args.no_password)
    owner = _setup_server(args, store) if variant == "server" else _setup_rds(args, store)
    password = _new_admin_password(args.no_password, "the stored owner login on this machine")
    try:
        _store_new_admin(store, owner, password)
    finally:
        if password is not None:
            wipe(password)
    return owner


def cmd_setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run `_setup`, then forget the admin password that was typed for it (`_typed`).

    Args:
        args: Parsed arguments of `calu setup`.
        store: The credential store.

    Raises:
        CliError: As `_setup`.
        CredentialError: On a wrong password.
        local_db.DockerError: If docker fails.
        psycopg.Error: On database errors.
    """
    try:
        _setup(args, store)
    finally:
        _typed.clear()


def _setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Set calu's database up: choose it on the first run, then migrate, seed and log in.

    Without an `admin` credential the command asks which database to use (`--database`:
    a local one in Docker, a server that exists, or an RDS instance with IAM authentication),
    checks the login and stores it as `admin`. Then, and on every later run: it applies the
    migrations, adds the default checkers, and makes sure this machine has the `session`
    credential that `calu run` uses. A session login that exists is kept, so a second run
    does not cut off running sessions or other machines; `--renew-session` replaces it.

    Without `--checker-model` no model is stored: every checker, the narrator and the
    summarizer then use the default model (`calu.config.default_model`: Haiku on the Anthropic
    API, or Haiku through AWS Bedrock in an EU region when `claude.use_bedrock` is on), and a
    database that is already set up keeps its models. `--checker-model M` sets M for every
    role; `--checker-model auto` picks one from the credentials this machine has
    (`calu.model_choice`).

    Every run then asks whether Claude Code's sandbox is on (`claude.sandbox.enabled` of
    the global settings; `--sandbox`/`--no-sandbox` answer without the question, and without
    a terminal and without the option the stored value stays: `choose_sandbox`). With the
    sandbox off it prints the recommendation of requirement 42 unless it is met
    (`sandbox_setup_notes`).

    `--set-passwords` encrypts the stored `admin` and `session` logins with passwords typed
    now (the old ones are asked first where the entries have any) and gives the role
    `calu_admin` the new admin password: the way back from `--no-password`, and the way to
    change a password. The logins themselves stay as they are, so running sessions go on.

    Every run may change the password of the role `calu_admin`: with `--no-password` to a new
    generated one (kept in the store; as root also in the place file, which every user can
    read, `_after_setup`); otherwise to the admin password typed in this run, or, if an
    earlier `--no-password` setup had published one, to a new generated one kept in the store
    only. A blank or untyped admin password leaves the role as it is. A long-running client
    that logged in as `calu_admin` notices the change when its connection drops.

    Args:
        args: Parsed arguments (`checker_model`, `database` and the options of its variants,
            `renew_session`, `session_auth`, `no_password`, `set_passwords`, `sandbox`).
        store: The credential store.

    Raises:
        CliError: If an answer or password is missing or invalid, the database cannot be
            reached or set up as it is, `--database` is given although a database is stored,
            the role `calu_session` has a login this machine does not hold (the message
            names `--renew-session`), an option does not apply (`_check_setup_options`,
            `_check_session_options`), `--checker-model auto` finds no provider's
            credentials, or the sandbox answer is neither on nor off.
        CredentialError: On a wrong password.
        local_db.DockerError: If docker fails.
        psycopg.Error: On database errors.
    """
    explicit = args.checker_model is not None
    model, effort, how = _chosen_model(args.checker_model, None) if explicit else ("", None, "")
    if "admin" in store:
        if args.database:
            raise CliError(
                "a database is set up already (the `admin` credential); `calu credential "
                "remove admin` first to set up another one"
            )
        _check_setup_options(args, None)
        owner = load_target(store, "admin", "Admin password: ")
        if args.no_password and store.needs_password("admin"):
            store.save("admin", owner.dump(), None)
            print("the 'admin' credential is now stored without a password")
        if args.set_passwords:
            # The way back from --no-password, and the way to change the password: the
            # stored owner login is encrypted with a password typed now, which further
            # down also becomes the password of the role `calu_admin` (`_typed`).
            chosen = _new_admin_password(False, "the stored owner login on this machine")
            assert chosen is not None
            try:
                store.save("admin", owner.dump(), chosen)
                _remember_admin_password(chosen)
            finally:
                wipe(chosen)
            print("the 'admin' credential is now protected by the new admin password")
        block = owner.docker
        if (
            block is not None
            and isinstance(owner.auth, PasswordAuth)
            and local_db.mount_of(block.data) is not None
            and not local_db.initialized(block.data)
        ):
            # An earlier setup stored the owner login and then failed to start the container
            # (docker missing, the port taken): finish that first start now.
            local_db.up(block.port, owner.auth.password, block.data)
            print(f"local database initialized in {block.data}")
        new_database = False
    else:
        owner = _new_database(args, store)
        new_database = True
    # A database that was just chosen never shares a session login stored for another one.
    renew = args.renew_session or new_database or "session" not in store
    _check_session_options(args, owner, renew)
    session_iam = None
    if isinstance(owner.auth, RdsIamAuth) and args.session_auth != "password":
        session_iam = RdsIamAuth(
            region=owner.auth.region, profile=args.session_aws_profile or owner.auth.profile
        )

    admin_password: list[str] = []
    """The password `run` gave the role `calu_admin`, if it gave it one."""
    typed_admin: list[bool] = []
    """Non-empty if that password is one a person typed, not a generated one."""
    sandbox_on: list[bool] = []
    """Whether Claude Code's sandbox is on after `run` (`_apply_sandbox_choice`)."""

    async def run() -> tuple[str, Target | None]:
        """Connect and run the setup steps.

        Returns:
            The report and the new session login, or None if the login was kept.

        Raises:
            CliError: If `calu_session` has a login this machine does not hold, or the
                database cannot be set up as it is.
        """
        conn = await open_stored(owner)
        try:
            db = Db(conn)
            if renew and not args.renew_session and await session_can_log_in(conn):
                raise CliError(
                    "the role calu_session has a login already, and this machine does not "
                    "hold it (another machine, or an earlier setup). `calu setup "
                    "--renew-session` gives it a new one, which stops the sessions and the "
                    "machines that use the old one."
                )
            # Models are stored only when asked for; otherwise every role uses the default
            # model (D194), and a second `calu setup` leaves chosen models alone (D129).
            fresh = not await _has_checker_types(db)
            try:
                report, session = await setup_database(
                    conn, owner, model, renew, session_iam, update_outdated=False
                )
            except SetupRefused as exc:
                raise CliError(f"calu cannot be set up in {owner.place} as it is:\n{exc}") from exc
            if args.no_password:
                # Always a generated password, also when an admin password was typed in
                # this run to convert the setup: this one is written where every user of
                # the machine can read it, and a password a person chose must not be.
                admin_password.append(secrets.token_urlsafe(32))
                await enable_admin_login(conn, admin_password[0])
                report += "\nlogin of calu_admin: a generated password, kept in the store"
            elif _typed.get("admin"):
                # The password a person knows also opens the role that changes settings,
                # from any client and without a stored login (D213).
                await enable_admin_login(conn, _typed["admin"])
                admin_password.append(_typed["admin"])
                typed_admin.append(True)
                report += "\nlogin of calu_admin: set to the admin password"
            elif ADMIN_ENTRY in store or _shared_admin() is not None:
                # An earlier `--no-password` gave the role a password that the store, and
                # perhaps the place file, hold. Without the option that is taken back: the
                # role gets a new one, which whoever copied the old one does not have.
                admin_password.append(secrets.token_urlsafe(32))
                await enable_admin_login(conn, admin_password[0])
                report += (
                    "\nlogin of calu_admin: a new generated password, kept in the store "
                    "only; the one a setup with --no-password had published stops working. "
                    "The stored logins still have no password: `calu setup --set-passwords` "
                    "puts typed ones back"
                )
            elif "admin" in _typed:
                # A blank admin password is accepted for the stored entry (the owner's
                # ruling, D230); PostgreSQL lets no role log in with one.
                report += (
                    "\nlogin of calu_admin: left as it is, because the admin password is "
                    "blank and PostgreSQL accepts no blank password for a login. A client "
                    "without the `admin` credential cannot change settings then, and `calu "
                    "web` gives out an address with a token"
                )
            else:
                report += (
                    "\nlogin of calu_admin: left as it is, because the `admin` credential "
                    "has no password and none was typed"
                )
            enabled, line = await _apply_sandbox_choice(db, args.sandbox)
            sandbox_on.append(enabled)
            report += f"\n{line}"
            for said in await _offer_instruction_updates(db, args.update_instructions):
                report += f"\n{said}"
            if explicit:
                await admin.set_all_models(db, model, effort)
                report += "\nmodels of every checker, the narrator and the summarizer: "
                report += f"{model}" + (f", effort {effort}" if effort else "") + f" ({how})"
            elif fresh:
                report += (
                    "\nmodels: the default for every role -- "
                    f"{ANTHROPIC_HAIKU} (needs ANTHROPIC_API_KEY in {user_env_file()}), or "
                    f"{BEDROCK_EU_HAIKU} once `calu config set claude.use_bedrock true` is"
                    " set. `calu config models M` chooses another one."
                )
            return report, session
        finally:
            await conn.close()

    # A session entry without a password is accepted by `calu run` only in a store that root
    # alone can write (`_session_login`); elsewhere the session password stays.
    bare_session = args.no_password and _only_root_writes(store.path)
    if args.no_password and not bare_session:
        print(
            "the session password stays: without one, whatever can write "
            f"{store.path} could point sessions at another database. As root "
            "(`sudo calu setup --no-password`) it is not needed."
        )
    if not renew:
        if bare_session and store.needs_password("session"):
            # Before the database is touched, as below: a mistyped password changes nothing.
            kept = load_credential(store, "session", "Session password: ")
            try:
                store.save("session", kept, None)
            finally:
                wipe(kept)
            print("the 'session' credential is now stored without a password")
        if args.set_passwords:
            # The same login, encrypted with a new password: running sessions keep it.
            kept = load_credential(store, "session", "Session password: ")
            try:
                print("Choose the session password (needed to start sessions with `calu run`).")
                fresh = read_password("New session password: ", confirm=True)
                try:
                    store.save("session", kept, fresh)
                finally:
                    wipe(fresh)
            finally:
                wipe(kept)
            print("the 'session' credential is now protected by the new session password")
        report, _ = asyncio.run(run())
        print(report)
        print(
            "session login kept (`calu setup --renew-session` replaces it and stops the "
            "sessions that use it)"
        )
        _after_setup(args, store, owner, admin_password, bool(typed_admin))
        _print_sandbox_notes(sandbox_on, store)
        return
    # Asked before the database is touched: a mistyped password must not leave the role with
    # a new login that nothing holds.
    session_password: bytearray | None = None
    if not bare_session:
        print("Choose the session password (needed to start sessions with `calu run`).")
        session_password = read_password("New session password: ", confirm=True)
    try:
        report, session = asyncio.run(run())
        assert session is not None
        store.save("session", session.dump(), session_password)
    finally:
        if session_password is not None:
            wipe(session_password)
    print(report)
    print("stored credential 'session'")
    _after_setup(args, store, owner, admin_password, bool(typed_admin))
    _print_sandbox_notes(sandbox_on, store)
    print("next: `calu project add . --name NAME` in a project directory, then `calu run`")


def _print_sandbox_notes(sandbox_on: list[bool], store: CredentialStore) -> None:
    """Print `sandbox_setup_notes` to stderr, each as a WARNING line.

    Args:
        sandbox_on: Holds whether the sandbox is on; empty if setup did not get that far.
        store: The credential store, as the setup left it.
    """
    for note in sandbox_setup_notes(sandbox_on[0], store) if sandbox_on else []:
        print(f"WARNING: {note}", file=sys.stderr)


ADMIN_ENTRY = "calu_admin"
"""The store entry `calu setup --no-password` keeps the login of the role `calu_admin` in,
protected by the file's mode alone (D228)."""


def _after_setup(
    args: argparse.Namespace,
    store: CredentialStore,
    owner: Target,
    admin_password: list[str],
    typed: bool = False,
) -> None:
    """Finish a setup: the password-free admin login and the place file (D228).

    Side effects: saves a generated password of `calu_admin` as the entry `ADMIN_ENTRY`
    without a password, and with `--no-password` says who can read the store and the place
    file; removes that entry when the role got a typed password, so that no stale login is
    kept. As root, writes where the database is
    to `calu.db.place.PATH`: without a secret, or with `--no-password` together with the
    password of `calu_admin`, readable by every user of the machine. A failure of that is
    reported and does not fail the setup.

    Args:
        args: Parsed arguments (`no_password`).
        store: The credential store.
        owner: The owner login, for the place.
        admin_password: The password the role `calu_admin` was given in this run; empty if
            it was left as it is.
        typed: Whether that password is one a person typed. A generated one is stored as
            `ADMIN_ENTRY`; it is written to the place file only with `--no-password`.
    """
    if admin_password and not typed:
        try:
            store.save(ADMIN_ENTRY, admin_login(owner, admin_password[0]).dump(), None)
        except ValueError as exc:  # TargetError: the owner login names a libpq service
            print(f"note: the login of calu_admin could not be stored ({exc})", file=sys.stderr)
        else:
            print(f"stored credential {ADMIN_ENTRY!r} (no password)")
    elif typed and ADMIN_ENTRY in store:
        store.remove(ADMIN_ENTRY)
        print(f"removed the stored {ADMIN_ENTRY!r} credential: the role has a typed password now")
    if args.no_password:
        if os.geteuid() == 0:
            print(
                f"no password protects the logins in {store.path}: only root can read them, "
                "so `calu run --sudo`, `calu serve --sudo`, `calu attach --sudo` and `calu "
                "web --sudo` ask for none"
            )
            if admin_password:
                print(
                    f"WARNING: the login that changes settings is also in {db_place.PATH}, "
                    "which every user and every program of this machine can read (Claude "
                    "Code's commands included): a client asks for no password, and nothing "
                    "but the checkers keeps a program from changing the checkers. `sudo calu "
                    "setup` without --no-password gives the role a new password and "
                    "removes it from that file.",
                    file=sys.stderr,
                )
        else:
            print(
                f"WARNING: no password protects the logins in {store.path}. Every program "
                "that runs as you can read them, the commands of Claude Code included, and "
                "can then change settings and decide escalations. `calu setup` without "
                "--no-password, or calu installed for root (--sudo), avoids that.",
                file=sys.stderr,
            )
    if os.geteuid() == 0:
        try:
            # With --no-password the admin login goes in as well: the owner's ruling is that
            # such a setup has weak security and that this is allowed (D228).
            shared = admin_password[0] if args.no_password and admin_password else None
            db_place.write(owner, admin_password=shared)
        except (OSError, ValueError) as exc:
            print(f"note: {db_place.PATH} was not written ({exc})", file=sys.stderr)


def _new_operator(store: CredentialStore) -> None:
    """Renew the `calu_operator` login and print its connection string once.

    The password is random and replaces any earlier one, so every copy handed out before stops
    working -- that is how an operator's access is taken back. calu does not store the string:
    the person who gets it stores it on their own machine with `calu credential set operator`.

    Args:
        store: The credential store; its `admin` credential is used to reach the database.

    Raises:
        CliError: If there is no `admin` credential.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    url = load_credential(store, "admin", "Admin password: ")

    async def work() -> str:
        """Give the operator role a new password.

        Returns:
            Its connection string.
        """
        owner = Target.parse(url)
        conn = await open_stored(owner)
        try:
            return await enable_operator_login(conn, owner)
        finally:
            await conn.close()

    try:
        operator_url = asyncio.run(work())
    finally:
        wipe(url)
    ca_note = ""
    if "sslrootcert=" in operator_url:
        ca_note = (
            "It names a CA file of this machine (sslrootcert): copy that file to the other "
            "machine as well and change the path in the string to where it is there. "
        )
    print(
        "login of the operator role (this is the only copy):\n\n"
        f"    {operator_url}\n\n"
        f"{ca_note}"
        "Store it on the machine that will use it with `calu credential set operator`. It reads "
        "the fleet and resolves escalations (`calu sessions`, `calu escalations`, `calu "
        "escalation`, `calu resolve`, `calu evals`) and can change nothing else. Running "
        "`calu credential new operator` again replaces it and stops every earlier copy."
    )


def cmd_credential(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, set, remove or newly create credentials.

    `set` stores a connection string the user has; `new operator` is the other direction -- it
    needs the `admin` credential, gives `calu_operator` a fresh password in the database and
    prints the connection string once, for a person who should watch the herd without owning it
    (agent/records/PLAN.md D48).

    `set --no-password` stores the connection string protected by the file's mode alone, so that the
    commands that use it ask nothing; it prints what that costs (agent/records/PLAN.md D104). It is
    refused for the `session` credential, because starting a session is password protected
    (REQUIREMENTS 17, D115).

    Args:
        args: Parsed arguments (`action`, `name`, `no_password`).
        store: The credential store.

    Raises:
        CliError: If `name` is missing or unknown, a password confirmation does not match or
            the input ended, `new` was asked for something other than the operator, or `set`
            of a login entry (`LOGIN_ENTRIES`) was given a secret that is no database login
            (`Target.parse`).
        CredentialError: On a wrong password.
        psycopg.Error: On database errors (for `new`).
    """
    if args.action == "list":
        for name in store.names():
            protection = "password" if store.needs_password(name) else "file mode only"
            print(f"{name}  ({protection})")
        return
    if not args.name:
        raise CliError(f"credential {args.action} needs a NAME")
    if args.action == "remove":
        if not store.remove(args.name):
            raise CliError(f"no credential {args.name!r}")
        print(f"removed credential {args.name!r}")
        return
    if args.action == "new":
        if args.name != "operator":
            raise CliError(
                "`calu credential new` only makes an operator connection string: "
                "`calu credential new operator`"
            )
        _new_operator(store)
        return
    if args.no_password and args.name == "session":
        # REQUIREMENTS 17: starting a session is password protected. The ergonomics complaint
        # that `--no-password` answers was about the read-and-resolve commands, which ask for a
        # password several times an hour; starting a session asks once (D115).
        raise CliError(
            "the 'session' credential cannot be stored without a password: starting a session "
            "is password protected by design (REQUIREMENTS 17), and this credential is what a "
            "session writes its steps and escalations with. `--no-password` is meant for the "
            "'operator' credential, which reads the fleet and resolves escalations"
        )
    secret = read_password("Connection string (not echoed): ")
    if args.name in LOGIN_ENTRIES:
        # Checked before the entry's password is asked: a typo in the string would otherwise
        # only show at the next command that uses the entry.
        try:
            Target.parse(secret)
        except TargetError as exc:
            wipe(secret)
            raise CliError(f"credential {args.name!r} not stored: {exc}") from None
    password = None
    if not args.no_password:
        password = read_password(f"New password for {args.name!r}: ", confirm=True)
    try:
        store.save(args.name, secret, password)
    finally:
        wipe(secret)
        if password is not None:
            wipe(password)
    if args.no_password:
        print(
            f"stored credential {args.name!r} without a password: every command that uses it "
            "runs without a prompt, and only the file's mode protects it. Anyone who can read "
            f"{store.path} -- another process of your user, a backup, a synchronised dotfile "
            "directory -- has this credential. `calu credential set "
            f"{args.name}` again (without --no-password) puts a password back on it."
        )
        return
    print(f"stored credential {args.name!r}")


LOGIN_ENTRIES = ("admin", "session", "operator")
"""The credential entries that hold a database login (`calu.db.target.Target`)."""


def _workspace_and_prompt(args: argparse.Namespace) -> tuple[Path, str | None]:
    """Read the project directory and the first message of `calu run` and `calu start`.

    Args:
        args: Parsed arguments (`directory`, `prompt`, `prompt_file`).

    Returns:
        The resolved directory and the first message, or None for none.

    Raises:
        CliError: If the directory does not exist, `--prompt` and `--prompt-file` are both
            given, or the prompt file cannot be read or holds nothing but white space.
    """
    workspace = Path(args.directory).resolve()
    if not workspace.is_dir():
        raise CliError(f"{workspace} is not a directory")
    if args.prompt is not None and args.prompt_file:
        raise CliError("--prompt and --prompt-file exclude each other; give one of them")
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CliError(f"cannot read {args.prompt_file}: {exc}") from exc
        if not prompt:
            # An empty first message would start a session that waits, which is not what a
            # file of instructions was meant for (a wrong file, or one not written yet).
            raise CliError(f"{args.prompt_file}: the file is empty")
    return workspace, prompt


def _check_conversation_choice(args: argparse.Namespace) -> None:
    """Check `--resume`, `--fork` and `--fork-before-turn` of `calu run` and `calu start`.

    Args:
        args: Parsed arguments (`resume`, `fork`, `fork_before_turn`).

    Raises:
        CliError: If `--fork` comes with `--resume` or with `--fork-before-turn`, if
            `--fork-before-turn` comes without `--resume` or is below 2, or if `--resume` or
            `--fork` name no UUID.
    """
    if args.fork and args.resume:
        raise CliError(
            "--fork and --resume exclude each other: --resume ID continues that conversation, "
            "--fork ID starts a copy of it under a new session id"
        )
    if args.fork and args.fork_before_turn is not None:
        raise CliError(
            "--fork keeps the whole conversation; to branch off before a turn use "
            "--resume ID --fork-before-turn N"
        )
    if args.fork_before_turn is not None and args.fork_before_turn < 2:
        raise CliError("--fork-before-turn takes a turn number of 2 or more")
    for option, value in (("--resume", args.resume), ("--fork", args.fork)):
        if value is None:
            continue
        try:
            uuid.UUID(value)
        except ValueError:
            # calu reads a conversation back by this id, and prints it as the session's id.
            raise CliError(
                f"{option} takes a Claude session id (a UUID); `calu list-sessions` shows "
                "those of this directory"
            ) from None


_ARGUMENTS: list[str] = []
"""calu's arguments as they were given, for the command `--sudo` runs again."""


def _run_as(args: argparse.Namespace, prepare: bool = True) -> RunAs | None:
    """Decide which user Claude Code runs as, from `--user` and who calu runs as (D206).

        calu is root, no --user     ──► refused: Claude Code never runs as root by accident
        calu is root, --user root   ──► None: one user, as without the option
        calu is root, --user X      ──► X; its directories are prepared (`calu.asuser.prepare`)
        calu is not root, --user X  ──► refused, unless X is the user calu runs as (None)
        calu is not root, --sudo    ──► calu runs again as root through sudo, with --user
                                        <the invoking user> (`_run_again_through_sudo`)

    Args:
        args: Parsed arguments (`user`, `sudo`, `from_sudo`).
        prepare: Create the user's directories; False for a command that only reports
            (`calu doctor`).

    Returns:
        The user to switch to, or None to run everything as calu's own user.

    Raises:
        CliError: In the refused cases, if the user or its directories cannot be used, and
            if `--sudo` cannot run calu again.
    """
    root = os.geteuid() == 0
    if args.sudo and not root:
        _run_again_through_sudo(args)
    if args.user is None:
        if root:
            raise CliError(
                "calu runs as root, so say which user Claude Code runs as: --user NAME, or "
                "--user UID[:GID] for a user without a name or home directory on this "
                "machine (as in a container); root itself only as --user root"
            )
        return None
    try:
        run_as = asuser.resolve(args.user)
        if run_as.uid == os.geteuid():
            return None
        if args.from_sudo and str(run_as.uid) != os.environ.get("SUDO_UID"):
            # `--sudo` passes the invoking user's own name; anything else is not that path.
            raise CliError(
                f"--user {args.user} is not the user who ran sudo (uid "
                f"{os.environ.get('SUDO_UID', 'unknown')})"
            )
        if not root:
            raise CliError(
                "--user needs calu to run as root: only root can start Claude Code as another user"
            )
        if prepare:
            asuser.prepare(run_as)
    except asuser.UserError as exc:
        raise CliError(str(exc)) from exc
    return run_as


def _run_again_through_sudo(args: argparse.Namespace) -> None:
    """Replace this process by `sudo calu ... --user <the invoking user>` (requirement 26).

    calu then runs as root and Claude Code as the user who typed the command. sudo asks for
    its password on the terminal. Refused when somebody other than root can change the
    `calu` script, the Python executable or calu's package directory, or one of their
    parents: elevation would hand that user root. (Other installed packages inside a
    root-owned directory are not checked one by one.)

    Side effects: prints a note to stderr naming the checker-key variables sudo will not
    pass on; on success the process image is replaced and this function does not return.

    Args:
        args: Parsed arguments (`user`).

    Raises:
        CliError: If `--user` is given as well, the installation is not root's alone, or
            sudo cannot be executed.
    """
    if args.user is not None:
        raise CliError("--sudo runs Claude Code as you; it takes no --user")
    executable = Path(sys.executable).parent / "calu"
    problems = asuser.writable_by_others(
        [executable, Path(sys.executable), Path(asuser.__file__).parent]
    )
    if problems:
        raise CliError(
            "--sudo would run this installation of calu as root, and it can be changed "
            "without root: " + "; ".join(problems[:3]) + ". Install calu for root, for "
            f"example: {asuser.INSTALL_HINT}"
        )
    carried = sorted(
        name for name in os.environ if name in PROVIDER_KEYS or name.startswith("ANTHROPIC_")
    )
    if carried:
        # sudo starts calu with a fresh environment: what the checkers need has to be in
        # root's own file, or they fail at the first check with the provider's error.
        print(
            f"note: sudo does not pass your environment ({', '.join(carried)}). The checkers "
            "read their keys from /etc/calu/.env; Claude Code uses your own login.",
            file=sys.stderr,
            flush=True,
        )
    command = asuser.sudo_command(_ARGUMENTS, getpass.getuser(), executable)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        raise CliError(f"cannot run sudo: {exc}") from exc


def _only_root_writes(path: Path, owner: int = 0) -> bool:
    """Tell whether a credential store can be read and changed by root alone.

    Args:
        path: The store file.
        owner: The uid that stands for root (a test passes its own).

    Returns:
        True if this process runs as `owner`, the file belongs to `owner` and grants nothing
        to its group or to others, and its directory belongs to `owner` and cannot be written
        by its group or by others. False otherwise, also when the file does not exist.
    """
    return asuser.only_root_writes(path, owner)


def _session_login(store: CredentialStore) -> bytearray:
    """Ask for the session password and return the decrypted `session` credential.

    Side effects: clears the process's "dumpable" flag before the password is read, so that
    neither the password nor the credential is ever in a process another process of this
    user can read; starts the local database when that variant is configured and its port
    does not answer (the docker command gets no secret).

    Args:
        store: The credential store.

    Returns:
        The credential; the caller wipes it.

    Raises:
        CliError: If there is no `session` credential, or it is stored without a password in
            a store that somebody other than root can write.
        CredentialError: On a wrong password.
    """
    if (
        "session" in store
        and not store.needs_password("session")
        and not _only_root_writes(store.path)
    ):
        # REQUIREMENTS 17 on the read path as well (D115 guards the write path): an entry
        # without a password is unauthenticated, so whatever can write the credential file
        # could point the session at another database without anybody being asked anything.
        # A store that only root can write (`sudo calu setup --no-password`, D228) is the
        # exception: nothing but root could have put the entry there.
        raise CliError(
            "the 'session' credential is stored without a password, and starting a session is "
            "password protected by design (REQUIREMENTS 17); store it again with "
            "`calu credential set session`"
        )
    harden_process()
    url = load_credential(store, "session", "Session password: ")
    try:
        # After the password, because where the local database is comes out of the decrypted
        # entry; before anything connects.
        block = Target.parse(url).docker
        if block is not None and local_db.ensure_running(block):
            print(f"started the local database on 127.0.0.1:{block.port}", flush=True)
    except BaseException:
        wipe(url)
        raise
    return url


def cmd_serve(args: argparse.Namespace, store: CredentialStore) -> None:
    """Answer session requests in the foreground until interrupted (agent/records/PLAN.md D208).

    Asks for the session password once; every session that `calu start` asks for afterwards
    runs in this process without a further password.

    Args:
        args: Parsed arguments (`max_sessions`, `idle_timeout`, `user`, `sudo`, `secure`,
            `insecure`, `relay_port`, `no_relay`, `projects`).
        store: The credential store.

    Raises:
        CliError: If there is no usable `session` credential, `--max-sessions` is below 1,
            `--idle-timeout` is not above 0, `--relay-port` is no port, the user cannot be
            switched (`_run_as`), the projects directory is refused
            (`calu.projects_dir.check_projects_dir`, before the password is asked), the
            database refused the session login, or no relay port can be bound.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors at the start.
    """
    _check_security_flags(args)
    if args.max_sessions < 1:
        raise CliError("--max-sessions takes a number of 1 or more")
    if args.idle_timeout is not None and not (
        math.isfinite(args.idle_timeout) and args.idle_timeout > 0
    ):
        raise CliError("--idle-timeout takes a number of minutes above 0")
    if args.relay_port is not None and not 0 <= args.relay_port <= 65535:
        raise CliError("--relay-port takes a port number from 0 to 65535")
    run_as = _run_as(args)
    try:
        projects = projects_dir.check_projects_dir(
            args.projects or ".", claude_as_root=os.geteuid() == 0 and run_as is None
        )
    except projects_dir.ProjectsDirRefused as exc:
        raise CliError(str(exc)) from None
    url = _session_login(store)
    try:
        options = ServeOptions(
            projects=projects,
            relay=not args.no_relay,
            relay_port=args.relay_port,
            max_sessions=args.max_sessions,
            run_as=run_as,
            idle_timeout_s=args.idle_timeout * 60 if args.idle_timeout is not None else None,
            secure=args.secure,
            insecure=args.insecure,
            login_protection=login_protection(store, run_as is not None),
        )
        code = asyncio.run(serve_sessions(url, options, user=run_as.user if run_as else None))
    finally:
        wipe(url)
    if code:
        raise CliError("the server ended with an error (see the lines above and the log file)")


def cmd_start(args: argparse.Namespace, store: CredentialStore) -> None:
    """Ask the running `calu serve` for a session and attach to it (agent/records/PLAN.md D208).

    Side effects: writes a `session_requests` row and, unless `--no-attach`, a client token
    that is revoked when the client ends; prints the session's ids.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `prompt_file`, `resume`,
            `fork`, `fork_before_turn`, `model`, `effort`, `no_attach`, `plain`, `thinking`).
        store: The credential store; the `operator` credential is enough.

    Raises:
        CliError: If the options contradict each other (also `--no-attach` with `--plain` or
            `--thinking`), no server answered, the server refused or failed to start the
            session, or the client ended with an error.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
        KeyboardInterrupt: On Ctrl-C while waiting for the server; the request was withdrawn
            first where no server had claimed it yet.
    """
    if args.token and not args.relay:
        raise CliError("--token goes with --relay, the address of a `calu serve` relay")
    if args.relay:
        _start_through_relay(args, store)
        return
    workspace, prompt = _workspace_and_prompt(args)
    _check_conversation_choice(args)
    if args.fork_before_turn is not None and not args.resume:
        raise CliError("--fork-before-turn needs --resume <session id>")
    if args.no_attach and (args.plain or args.thinking):
        raise CliError(
            "--plain and --thinking choose how the client shows the session, and --no-attach "
            "starts no client; give them to `calu attach` later"
        )
    run_as = _client_user(args)
    user, host = serve_os_user(), socket.gethostname()
    if run_as is not None:
        user = run_as.user or str(run_as.uid)
    wanted = SessionRequest(
        hostname=host,
        os_user=user,
        directory=str(workspace),
        kind="resume" if args.resume else "fork" if args.fork else "new",
        claude_session_id=args.resume or args.fork,
        fork_before_turn=args.fork_before_turn,
        prompt=prompt,
        model=args.model,
        effort=args.effort,
        project=args.project,
        label=f"calu start ({user})",
    )

    async def work(db: Db) -> tuple[Endpoint, str]:
        """File the request, wait for the session and mint a token for it.

        Args:
            db: The connection.

        Returns:
            The endpoint (without a token for `--no-attach`) and the Claude session id.

        Raises:
            CliError: If no server answered or the session was not started.
            asyncio.CancelledError: On Ctrl-C while waiting; the request was withdrawn
                first where that was still possible.
        """
        request_id = await session_requests.file(db, wanted)
        try:
            done = await session_requests.wait_for(db, request_id)
        except asyncio.CancelledError:
            # Ctrl-C: `asyncio.run` cancels this task. Take the request back, so that a
            # server does not start a session for a client that has gone.
            withdrawn = False
            with contextlib.suppress(Exception):
                withdrawn = await session_requests.withdraw(db, request_id)
            print(
                "withdrew the session request"
                if withdrawn
                else "the session request could not be withdrawn (a server may have taken it "
                "already; `calu sessions` shows the session if it starts)",
                file=sys.stderr,
                flush=True,
            )
            raise
        except session_requests.NoServer:
            raise CliError(
                f"no `calu serve` answered for {user} on {host}: start it in a terminal with "
                "`calu serve`, or run this one session with `calu run`"
            ) from None
        except TimeoutError as exc:
            raise CliError(str(exc)) from None
        if done.state != "started" or done.session_id is None:
            raise CliError(f"the server did not start the session: {done.message}")
        row = await db.one(
            "SELECT coalesce(address, ''), coalesce(claude_session_id, '') "
            "FROM sessions WHERE id = %s::uuid",
            (done.session_id,),
        )
        address, claude_id = row if row is not None else ("", "")
        if args.no_attach:
            return Endpoint(address, "", None, done.session_id), claude_id
        local = _local_endpoint(done.session_id)
        if local is not None:
            return local, claude_id
        token_id, token = await herd.mint_token(db, done.session_id, "calu start")
        return Endpoint(address, token, token_id, done.session_id), claude_id

    endpoint, claude_id = _herd_value(store, work, role=db_roles.OPERATOR)
    if claude_id:
        print(f"[session] {claude_id}", flush=True)
    else:
        print("[session] the Claude session id is not known yet", flush=True)
    if args.no_attach:
        print(f"[clients] attach with: calu attach --session {endpoint.session_id}", flush=True)
        return
    _attach(endpoint, store, args.plain, 0, args.thinking, run_as)


def _start_through_relay(args: argparse.Namespace, store: CredentialStore) -> None:
    """`calu start --relay ADDRESS DIR`: ask a `calu serve` through its relay (D242).

    The directory is one on the server's machine and is not looked at here; the server
    refuses one that does not exist. The server token comes from `--token`, `CALU_TOKEN` or
    the terminal (`_reach`).

    Side effects: the server files a `session_requests` row through the token and starts the
    session; the client then attaches to it through the relay (unless `--no-attach`).

    Args:
        args: Parsed arguments (`relay`, `token`, `directory`, `prompt`, `prompt_file`,
            `resume`, `fork`, `fork_before_turn`, `model`, `effort`, `project`, `no_attach`,
            `plain`, `thinking`).
        store: The credential store (only for the client that attaches).

    Raises:
        CliError: If the directory is not absolute, the options contradict each other, the
            relay cannot be reached or refuses the token, or the server did not start the
            session.
    """
    _check_conversation_choice(args)
    if args.fork_before_turn is not None and not args.resume:
        raise CliError("--fork-before-turn needs --resume <session id>")
    if not args.directory.startswith("/"):
        raise CliError(
            "with --relay the directory is one on the server's machine: give its absolute path"
        )
    if args.prompt is not None and args.prompt_file:
        raise CliError("--prompt and --prompt-file exclude each other; give one of them")
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CliError(f"cannot read {args.prompt_file}: {exc}") from exc
    checked = _reach(Endpoint(relay_base(args.relay), args.token or ""), pick=False)
    relay = checked.relay
    if relay is None:
        raise CliError(f"{args.relay} is a session's address, not the relay of a `calu serve`")
    params = {
        "kind": "resume" if args.resume else "fork" if args.fork else "new",
        "directory": args.directory,
        "claude_session_id": args.resume or args.fork,
        "fork_before_turn": args.fork_before_turn,
        "prompt": prompt or None,
        "model": args.model,
        "effort": args.effort,
        "project": args.project,
    }
    print("[session] asked the server through its relay …", flush=True)
    try:
        _hello, started = asyncio.run(
            relay_call(relay.address, relay.token, "relay.start", params, 150.0)
        )
    except RequestFailed as exc:
        raise CliError(
            f"the server did not start the session: {exc.error.get('message') or exc}"
        ) from exc
    except (OSError, ConnectionError) as exc:
        raise CliError(f"the relay {relay.address} cannot be reached: {exc}") from exc
    session_id = str((started or {}).get("calu_session_id") or "")
    print(f"[session] started: {session_id}", flush=True)
    if args.no_attach:
        print(
            f"[clients] attach with: calu attach {relay.address} --session {session_id}",
            flush=True,
        )
        return
    endpoint = Endpoint(
        relay.session_address(session_id), relay.token, None, session_id, relay=relay
    )
    _attach(endpoint, store, args.plain, 0, args.thinking, None, True)


def cmd_run(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start a checked Claude Code session in the foreground.

    Asks for the session password, decrypts the `session` credential, and hands it to
    `calu.run.run_session`, which wipes it before Claude Code starts.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `resume`, `fork`,
            `fork_before_turn`, `model`, `effort`, `batch`, `secure`, `insecure`).
        store: The credential store.

    Raises:
        CliError: If there is no `session` credential, the directory does not exist, the
            options contradict each other (`--fork` with `--resume` or with
            `--fork-before-turn`), `--resume` or `--fork` name no UUID, `--fork-before-turn`
            is below 2, or the session could not be started.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
        KeyboardInterrupt: On Ctrl-C or SIGTERM, after the session was stopped in order.
    """
    workspace, prompt = _workspace_and_prompt(args)
    if args.background_timeout is not None and (
        not args.batch or not math.isfinite(args.background_timeout) or args.background_timeout <= 0
    ):
        raise CliError("--background-timeout needs --batch and a number of seconds above 0")
    _check_conversation_choice(args)
    _check_security_flags(args)
    socket_path = _socket_path(args.socket)
    # Last: `--sudo` replaces this process by sudo, which asks for a password, so every
    # mistake that can be found without it is reported first.
    run_as = _run_as(args)
    options = RunOptions(
        workspace=workspace,
        project=args.project,
        prompt=prompt,
        resume_id=args.resume,
        fork_id=args.fork,
        fork_before_turn=args.fork_before_turn,
        socket_path=socket_path,
        model=args.model,
        effort=args.effort,
        interactive=not args.batch,
        background_timeout_s=args.background_timeout,
        run_as=run_as,
        secure=args.secure,
        insecure=args.insecure,
        # Read now: after the switch to the user, root's store cannot be read (D206).
        login_protection=login_protection(store, run_as is not None),
    )
    if options.interactive and not options.prompt and sys.stdin.isatty():
        print(
            "Type a message for Claude; /quit ends the session. This terminal shows the "
            "session's status only: `calu attach` in another terminal shows the conversation."
        )
    url = _session_login(store)
    # SIGTERM (a service manager, `docker stop`, `kill`) stops the session in order, as
    # Ctrl-C does; a handler can only be installed in the main thread.
    in_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _terminate_as_interrupt) if in_main else None
    try:
        code = asyncio.run(run_session(url, options))
    finally:
        if in_main:
            signal.signal(signal.SIGTERM, previous)
        wipe(url)
    if code:
        raise CliError("the session ended with an error (see the lines above and the log file)")


def _terminate_as_interrupt(_number: int, _frame: object) -> None:
    """Handle SIGTERM of `calu run` as Ctrl-C: the session stops in order.

    Without it the process ended at once: the session's row kept its last state, its
    sockets stayed behind and nothing was said (found in the root box, 2026-11-18).

    Side effect: sends this process SIGINT, which the running event loop turns into a
    cancellation of the session; `calu run` then ends with "calu: interrupted" and 130.

    Args:
        _number: The signal's number (unused).
        _frame: The interrupted frame (unused).
    """
    signal.raise_signal(signal.SIGINT)


async def _with_admin(url: str, work: Callable[[Db], Awaitable[str]]) -> str:
    """Connect as the database owner, run `work` and close the connection.

    Args:
        url: The owner connection string.
        work: Called with the wrapped connection; its return value is printed.

    Returns:
        What `work` returned.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await open_stored(url)
    try:
        return await work(Db(conn))
    finally:
        await conn.close()


OPERATOR_FIRST = ("operator", "admin")
"""Credentials a read-and-resolve command tries, in order (agent/records/PLAN.md D48).

`operator` is the `calu_operator` login: it reads every table and resolves an escalation and can
change nothing else, which is exactly what watching a herd needs. `admin` is the database owner
and works too, so a setup that never made an operator credential keeps working unchanged.
"""


def _herd_command(store: CredentialStore, work: Callable[[Db], Awaitable[str]]) -> None:
    """Run a read-and-resolve command and print its report.

    The login, in this order (not the narrowest one: the first row is `calu_admin`):

        the place file holds a login (D228)   ──► that login (`calu_admin`), nothing is asked
        `operator` stored                     ──► that entry (`calu_operator`)
        `admin` stored                        ──► that entry (the database owner)

    Unlike `_herd_value` it neither uses a login kept in `_held` nor prefers a password-free
    `calu_admin` entry of the store, and it records nothing in `_admin_place`.

    Side effects: asks for the password of the entry it reads; an `admin` password typed
    here stays in `_typed` until the command ends (`load_credential`).

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    shared = _shared_admin()
    if shared is not None:
        # `sudo calu setup --no-password` (D228): nothing is asked.
        print(asyncio.run(_with_admin(shared.dump().decode(), work)))
        return
    for name in OPERATOR_FIRST:
        if name in store:
            url = load_credential(store, name, f"{name.capitalize()} password: ")
            try:
                print(asyncio.run(_with_admin(url.decode(), work)))
            finally:
                wipe(url)
            return
    raise CliError(
        "no credential 'operator' or 'admin'; `calu credential new operator` makes an operator "
        "connection string on the database machine, `calu credential set operator` stores it here"
    )


def _admin_command(
    store: CredentialStore,
    work: Callable[[Db], Awaitable[str]],
    args: argparse.Namespace | None = None,
) -> None:
    """Run a herd command with a login that may change settings and print its report.

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.
        args: The command's parsed arguments, for `_admin_secret` (which database, and
            whether the command only reads).

    Raises:
        CliError: As `_admin_secret`: no usable credential is stored, the entry `--database`
            names is not stored, or `admin` and `operator` name different databases and
            none was chosen.
        CredentialError: On a wrong password.
        TargetError: If an entry that had to be read holds no database login this version
            can read.
        psycopg.Error: On database errors.
    """
    url = _admin_secret(store, args)
    try:
        print(asyncio.run(_with_admin(url.decode(), work)))
    finally:
        wipe(url)


@dataclass(frozen=True)
class Endpoint:
    """Where a client should connect, and with which token (agent/records/PLAN.md D105)."""

    address: str
    """`ws://host:port` or `unix:<path>`, as `sessions.address` holds it."""
    token: str
    """A client token for that session."""
    token_id: int | None = None
    """The id of a token minted for this one client, to be revoked when it disconnects; None
    for a token the user passed on the command line."""
    session_id: str = ""
    """Which session it belongs to, for messages."""
    server_uid: int | None = None
    """For a session found through a local socket: the uid of the process that listens on
    it, as the kernel reports it; None otherwise. 0 is a session of root's calu, whose
    database a client that is not root learns from `calu.db.place` (D228)."""
    relay: RelayTarget | None = None
    """The `calu serve` relay the address goes through (D242); None for a session's own
    address. A client then follows `/new`, `/fork` and `/switch` through it."""
    gone: Callable[[], bool] | None = None
    """Tells whether the session's process is known to be gone (a session of this machine
    found in the database or through a local socket); None where that cannot be judged (an
    address given on the command line). A client whose connection breaks ends at once when
    it returns True, instead of trying again for minutes."""


def _pick_session(rows: list[herd.SessionRow], hint: str | None) -> herd.SessionRow:
    """Choose the session a client with no address means.

    Only sessions of **this** host are considered, because the address a session publishes is a
    loopback port or a Unix socket path: an address from another machine would not be reachable
    even though the database shows it. Of those, the session whose directory is the current
    directory or the nearest ancestor of it wins, so that `calu attach` works from a
    subdirectory of the project. A session that has stopped saying it is running is left out
    (`SessionRow.stale`), and so is one that serves no clients (empty `address`).

    Both sides of the directory comparison are resolved paths: `calu run` resolves the
    workspace before it writes the row (`cmd_run`), and `Path.cwd()` is resolved, so a symbolic
    link on the way to the project does not hide its session.

    Args:
        rows: The active sessions, as `herd.sessions` returns them.
        hint: A session id or the beginning of one, from `--session`; None means "the one
            for this directory".

    Returns:
        The session to connect to.

    Raises:
        CliError: If no session matches, or if several do and `hint` does not single one out.
            The message says what to do: which sessions there are, or how to name one.
    """
    here = Path.cwd().resolve()
    local = [
        row
        for row in rows
        if row.hostname == socket.gethostname() and row.address and not row.stale
    ]
    if hint:
        chosen = [row for row in local if row.id.startswith(hint)]
        if not chosen:
            raise CliError(
                f"no session on this host whose id starts with {hint!r} is serving clients "
                "(`calu sessions` lists them)"
            )
        if len(chosen) > 1:
            ids = ", ".join(row.id for row in chosen)
            raise CliError(f"{hint!r} matches several sessions: {ids}")
        return chosen[0]
    matching = [row for row in local if here == Path(row.directory) or _inside(here, row.directory)]
    if not matching:
        raise CliError(
            f"no running session of {here} on this host\n"
            "`calu start` asks a running `calu serve` for one and attaches, and `calu run` "
            "starts one in this terminal. `calu sessions` lists the sessions of every host; "
            "a session elsewhere is reached with its address and `--token=<token>` through "
            "an SSH tunnel"
        )
    # The nearest project directory wins: a session of a subproject is meant before the
    # session of the directory above it. Every candidate contains `here`, so of two candidate
    # directories one contains the other and the deepest is unique -- what can be left over is
    # several sessions of that one directory, which is a question for the user.
    deepest = max(len(Path(row.directory).parts) for row in matching)
    nearest = [row for row in matching if len(Path(row.directory).parts) == deepest]
    if len(nearest) > 1:
        lines = "\n".join(
            f"    {row.id}  {row.state}, started {row.started_at:%Y-%m-%d %H:%M:%S}"
            + (f', "{row.name}"' if row.name else "")
            for row in nearest
        )
        raise CliError(
            f"{len(nearest)} sessions of {nearest[0].directory} are running; "
            f"name one with --session <id>:\n{lines}"
        )
    return nearest[0]


def _inside(path: Path, directory: str) -> bool:
    """Tell whether `path` is inside `directory`.

    Args:
        path: An absolute, resolved path.
        directory: The session's project directory.

    Returns:
        True if `path` is `directory` or below it; False also when `directory` is not a
        usable path.
    """
    try:
        return path.is_relative_to(Path(directory))
    except OSError, ValueError:  # pragma: no cover - a directory that is not a path
        return False


def _local_endpoint(session_id: str) -> Endpoint | None:
    """Return a session's root-owned socket as the endpoint, for a client that is root (D215).

    Args:
        session_id: The id of the session's row.

    Returns:
        The endpoint, without a token, if this process is root and the session's socket
        exists and is root's alone (`calu.localsocket.problems`); else None, and the caller
        uses the session's address with a token. The socket is checked again when the
        client connects.
    """
    if os.geteuid() != 0:
        return None
    path = localsocket.path_of(session_id)
    if asuser.as_root(localsocket.problems, path):
        return None
    return Endpoint(f"unix:{path}", "", None, session_id)


def _local_socket_endpoint(hint: str | None) -> Endpoint | None:
    """Find the session a client means through the local sockets, without a token.

    Every client looks here first, root or not; only where it looks differs:

        root      ──► /run/calu/sessions/*.sock: root's own sockets (D215, D226)
        not root  ──► $XDG_RUNTIME_DIR/calu/sessions/*.sock: those of `--insecure`
                      sessions of this user (D227)

    No database and no password: each socket that is its owner's alone
    (`calu.localsocket.listed`) is asked who it is (`calu.protocol.client.probe` without a
    token; a session that wants one refuses and is skipped), and the session is picked as
    `_pick_session` picks one: by `hint`, else by the nearest project directory that holds
    the current directory.

    Side effects: one short connection to each listed socket; each session logs it. A socket
    that does not answer (its process died) is skipped.

    Args:
        hint: `--session`, a session id or its beginning, or None for "this directory".

    Returns:
        The endpoint without a token, or None if no socket answers or none matches; the
        caller then asks the database.

    Raises:
        CliError: If several sessions match and `hint` does not single one out.
    """
    if os.geteuid() == 0:
        paths = asuser.as_root(localsocket.listed)
    else:
        # Both places a session may have chosen: this user's runtime directory as this
        # process sees it, and the one a root session derives from the uid alone.
        roots = {localsocket.user_root(), localsocket.user_root(uid=os.geteuid())}
        paths = [path for root in sorted(roots) for path in localsocket.listed(root, os.geteuid())]
    if not paths:
        return None

    async def ask_all() -> list[tuple[Path, str, str]]:
        """Ask each socket for its session id and directory.

        Returns:
            (socket, session id, directory) of each socket that answered.
        """
        found: list[tuple[Path, str, str]] = []
        for path in paths:
            try:
                answer = await probe(f"unix:{path}", "", "calu (finding sessions)")
            except OSError, RequestFailed, ConnectionError, TimeoutError:
                continue
            session_id = str(answer.get("calu_session_id") or path.stem)
            found.append((path, session_id, str(answer.get("workspace") or "")))
        return found

    found = asyncio.run(ask_all())
    if hint:
        chosen = [entry for entry in found if entry[1].startswith(hint)]
    else:
        here = Path.cwd().resolve()
        inside = [entry for entry in found if entry[2] and _inside(here, entry[2])]
        deepest = max((len(Path(entry[2]).parts) for entry in inside), default=0)
        chosen = [entry for entry in inside if len(Path(entry[2]).parts) == deepest]
    if not chosen:
        return None
    if len(chosen) > 1:
        lines = "\n".join(f"    {entry[1]}  {entry[2]}" for entry in chosen)
        raise CliError(f"{len(chosen)} sessions match; name one with --session <id>:\n{lines}")
    path, session_id, _directory = chosen[0]
    pid, uid = _server_peer(path)
    return Endpoint(f"unix:{path}", "", None, session_id, uid, gone=_pid_gone(pid))


def _server_peer(path: Path) -> tuple[int | None, int | None]:
    """Ask the kernel which process and user listen on a Unix socket.

    Side effect: one connection, opened and closed.

    Args:
        path: The socket.

    Returns:
        The pid and the uid of the process that listens; None for either if the socket
        cannot be connected to or the kernel does not say.
    """
    probe_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe_socket.settimeout(2.0)
        probe_socket.connect(str(path))
        return localsocket.peer_pid(probe_socket), localsocket.peer_uid(probe_socket)
    except OSError:
        return None, None
    finally:
        probe_socket.close()


def _pid_gone(pid: int | None) -> Callable[[], bool] | None:
    """Make the check a client runs when its connection to a local socket breaks.

    Args:
        pid: The pid of the process that listened on the socket, as the kernel reported it.

    Returns:
        A function that returns True once no process has that pid; None without a pid.
    """
    if pid is None:
        return None
    return lambda: not herd.process_exists(pid)


def _row_gone(row: herd.SessionRow) -> Callable[[], bool] | None:
    """Make the check a client runs when its connection to a session of the database breaks.

    Args:
        row: The session, with the pid and the process table it recorded.

    Returns:
        A function that returns True once the session's process is known to be gone
        (`herd.process_gone`: same process table and no such pid, or this machine rebooted
        since); None if the row recorded neither.
    """
    if row.pid is None or not row.machine:
        return None
    pid, recorded, this = row.pid, row.machine, machine.here()
    return lambda: herd.process_gone(pid, recorded, this)


def _hold_password_free(store: CredentialStore) -> None:
    """Keep a database login for a root client that found its session through a socket.

    A client that runs as root for a user (`_hold`) cannot read root's credential store after
    it switched to the user's file identity, so it keeps a login in memory (`_held`). Found
    through a root-owned socket it needed none to attach (D226); it takes one here only if
    root's store has an entry that needs no password (`calu_admin`, `admin`, else
    `operator`), and asks
    for nothing. Without one, the client attaches anyway and its database features
    (`/running`, `/switch`, `/new`, `/fork`, saving settings) say that they are not available.

    Side effects: may set `_held` and `_admin_place`; reads root's credential store.

    Args:
        store: Root's credential store.
    """
    if not _hold or _held:
        return
    target = _password_free_login(store, (ADMIN_ENTRY, "admin", "operator"))
    if target is not None:
        _held[:] = [target]
        with contextlib.suppress(ValueError):
            _admin_place[:] = [admin_login(target, "")]


def _password_free_login(store: CredentialStore, names: tuple[str, ...]) -> Target | None:
    """Return the login of the first entry in `names` that root's store keeps without a password.

    `sudo calu setup --no-password` stores `calu_admin` (`ADMIN_ENTRY`) and `admin` that way.
    An entry that needs a password, cannot be read or holds no usable login is skipped (and
    logged); the decrypted connection string is wiped.

    Args:
        store: Root's credential store.
        names: The entry names, most preferred first.

    Returns:
        The login, or None if no entry in `names` qualifies.
    """
    for name in names:
        try:
            if name not in store or store.needs_password(name):
                continue
            url = store.load(name)
        except (CredentialError, OSError) as exc:
            _log.info("the %r credential could not be read: %s", name, exc)
            continue
        try:
            return Target.parse(url)
        except ValueError as exc:
            _log.info("the %r credential holds no usable login: %s", name, exc)
            continue
        finally:
            wipe(url)
    return None


def _changes_settings(login: Target) -> bool:
    """Tell whether a held login may change settings: `calu_admin` or the owner.

    Args:
        login: A login `_held` keeps.

    Returns:
        False for the logins of `calu_operator` and `calu_session`, True for every other
        role (`calu_admin`, and the owner, whose name `calu setup` does not fix).
    """
    return login.user not in (db_roles.OPERATOR, db_roles.SESSION)


ADMIN_ONCE_PROMPT = (
    "Admin password (asked once; this client keeps the login in memory for /running, "
    "/switch, /new, /fork and saving settings; Ctrl-D skips): "
)
"""The prompt of a root client that holds no login that changes settings yet (D249)."""

ADMIN_ATTEMPTS = 3
"""How often a root client asks the admin password before it attaches without the database."""


def _hold_for_client(store: CredentialStore, may_ask: bool) -> str | None:
    """Give a root client the one login it keeps for its database features (D214, D249).

    A client that runs as root for a user (`calu attach --sudo`, `calu start --sudo`) asks
    the admin password at most once per run, before it switches to the user's file
    identity, and keeps the narrowest login that serves every later step in `_held`:

        `_held` is `calu_admin`                      ──► kept, nothing asked
        `_held` is the owner, and the admin password
          was typed to open it (`_typed`)            ──► `calu_admin` with that password,
                                                         if the database accepts it; the
                                                         owner login is dropped
        `_held` is the owner without a password      ──► kept (each action narrows it,
                                                         `calu.db.roles.narrow`)
        nothing held, or `calu_operator`, and root's
          store keeps `calu_admin` or `admin`
          without a password (`--no-password`)       ──► that login, nothing asked
        nothing held, or `calu_operator` (may_ask)   ──► the admin password is asked here,
                                                         up to `ADMIN_ATTEMPTS` times:
                                                         `calu_admin` with it, or, where
                                                         the role refuses it (a blank admin
                                                         password, a schema before migration
                                                         0033), the owner login of root's
                                                         `admin` entry that it opened

    The login stays in this process's memory: it is not written anywhere, not put in an
    environment variable, and not given to the session or to a program the client starts.

    Side effects: may ask for the password on the terminal, open root's `admin` entry with
    it and log in once as `calu_admin` to check it; sets `_held` and `_admin_place`; prints
    a line on stderr after a password that opens nothing.

    Args:
        store: Root's credential store, still readable (before `_become_client_of`).
        may_ask: Whether the password may be asked: True for the full-screen client, whose
            database features need it; False for the line client.

    Returns:
        None if the client holds a login that changes settings, or holds none and asks
        none; otherwise why its database features are not available (skipped with
        Ctrl-D, or no password was accepted).
    """
    if _held and _changes_settings(_held[0]):
        typed = _typed.get("admin")
        if _held[0].user != db_roles.ADMIN and typed is not None:
            narrower = _admin_if_accepted(_held[0], typed)
            if narrower is not None:
                _held[:] = [narrower]
        return None
    free = _password_free_login(store, (ADMIN_ENTRY, "admin"))
    if free is not None:
        _held[:] = [free]
        with contextlib.suppress(ValueError):
            _admin_place[:] = [admin_login(free, "")]
        return None
    if not may_ask or not ("admin" in store or _held or _place_or_none() is not None):
        return None
    for _ in range(ADMIN_ATTEMPTS):
        try:
            password = read_password(ADMIN_ONCE_PROMPT)
        except CliError:
            return SKIPPED_ADMIN
        try:
            login = _login_for_password(store, password)
        finally:
            wipe(password)
        if login is not None:
            _held[:] = [login]
            with contextlib.suppress(ValueError):
                _admin_place[:] = [admin_login(login, "")]
            return None
        print("[client] the admin password was not accepted", file=sys.stderr, flush=True)
    return REFUSED_ADMIN


SKIPPED_ADMIN = (
    "the admin password was skipped: /running, /switch, /new, /fork and saving settings "
    "need it; `calu attach --sudo` again asks it once"
)
"""Why a root client offers no database features after Ctrl-D at the admin password."""

REFUSED_ADMIN = (
    f"the admin password was not accepted {ADMIN_ATTEMPTS} times: /running, /switch, /new, "
    "/fork and saving settings need it; `calu attach --sudo` again asks it once"
)
"""Why a root client offers no database features after wrong admin passwords."""


def _place_or_none() -> Target | None:
    """Read root's place file (`calu.db.place`) without raising.

    Returns:
        The login it describes, or None if there is no trustworthy file.
    """
    try:
        return db_place.read()
    except db_place.PlaceError:
        return None


def _admin_if_accepted(place: Target, password: str) -> Target | None:
    """Return the login of `calu_admin` with `password` if the database accepts it.

    Side effect: one login as `calu_admin`, opened and closed (`is_admin_password`).

    Args:
        place: Any login of the database.
        password: The admin password.

    Returns:
        The login, or None if the role refused the password, the database cannot be
        reached, or `place` cannot be taken over (a libpq service name).
    """
    try:
        if asyncio.run(is_admin_password(place, password)):
            return admin_login(place, password)
    except (psycopg.Error, ValueError) as exc:
        _log.info("the login of calu_admin could not be checked: %s", exc)
    return None


def _login_for_password(store: CredentialStore, password: bytearray) -> Target | None:
    """Turn the admin password a root client was given into the login it keeps.

    The password opens root's `admin` entry (the owner login) where it is stored with a
    password, and is the password of the role `calu_admin` after every `calu setup` that
    was given it (D213). The narrower login wins:

        `calu_admin` accepts it            ──► that login
        it opens the `admin` entry only    ──► the owner login (blank admin password, a
                                               schema before migration 0033)
        neither                            ──► None

    Args:
        store: Root's credential store.
        password: The typed password; not wiped here.

    Returns:
        The login, or None for a password that opens nothing.
    """
    owner: Target | None = None
    try:
        if "admin" in store and store.needs_password("admin"):
            secret = store.load("admin", password)
            try:
                owner = Target.parse(secret)
            finally:
                wipe(secret)
    except (CredentialError, OSError, ValueError) as exc:
        _log.info("the 'admin' credential was not opened: %s", exc)
    place = owner or (_held[0] if _held else None) or _place_or_none()
    if place is None:
        return None
    admin = _admin_if_accepted(place, bytes(password).decode())
    return admin if admin is not None else owner


def _find_endpoint(
    store: CredentialStore, hint: str | None, label: str, may_ask: bool = True
) -> Endpoint:
    """Find the local session a client should connect to and mint a token for it.

    This is the zero-argument path of `calu attach` and `calu acp` (agent/records/PLAN.md D105):
    instead of the address and the 43-character token that `calu run` printed, the client asks the
    database which session runs in this directory on this host and mints its own token. The token is
    minted rather than read because only the hash of a token is stored, so a token printed once
    cannot be recovered; `calu attach` revokes the one it minted when it ends. Every client first
    tries the local sockets (`_local_socket_endpoint`: root's own for a root client, the user's
    `--insecure` ones otherwise), which need neither the database nor a token (D226, D227); a
    session found in the database that has such a socket is reached through it too
    (`_local_endpoint`).

    Side effects: inserts a `client_tokens` row, unless the session was reached through a
    local socket; with a local socket, keeps a password-free login in memory
    (`_hold_password_free`); otherwise those of `_herd_value` (`_admin_place`, `_held`).

    Args:
        store: The credential store; the `operator` credential is enough, and with
            `calu credential set operator --no-password` (D104) nothing is asked.
        hint: `--session`, a session id or its beginning, or None for "this directory".
        label: What to write in the token's label, e.g. "calu attach".
        may_ask: Whether a password may be asked for; False for `calu acp`, whose standard
            input is the editor's protocol stream (see `_herd_value`).

    Returns:
        The endpoint. On the database path it carries the minted token and its id in
        `token_id`; for a session reached through a local socket the token is "" and
        `token_id` is None (nothing to revoke).

    Raises:
        CliError: If there is no database credential, or no single session matches.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    # Every client first looks at its local sockets: no database, no password (D226, D227).
    local = _local_socket_endpoint(hint)
    if local is not None:
        _hold_password_free(store)
        return local

    async def work(db: Db) -> Endpoint:
        """Pick the session and mint a token.

        Args:
            db: The connection.

        Returns:
            The endpoint.
        """
        row = _pick_session(await herd.sessions(db, active_only=True), hint)
        local = _local_endpoint(row.id)
        if local is not None:
            return dataclasses.replace(local, gone=_row_gone(row))
        token_id, token = await herd.mint_token(db, row.id, label)
        return Endpoint(row.address, token, token_id, row.id, gone=_row_gone(row))

    return _herd_value(store, work, may_ask, db_roles.OPERATOR)


def _revoke(store: CredentialStore, token_id: int, may_ask: bool = True) -> None:
    """Revoke a token that `_find_endpoint` minted. Never raises.

    A client that ends should not leave a usable token behind, and `calu token list` should not
    fill up with one row per attach. A failure here is reported and ignored: the client's work
    is done, and the token stays visible in `calu token list` either way.

    Side effects: sets `revoked` on the `client_tokens` row; prints a line if that fails or
    is left out.

    Args:
        store: The credential store.
        token_id: The token to revoke.
        may_ask: Whether a password may be asked for. False for `calu acp`, where a prompt
            would read from the editor's protocol stream (see `_herd_value`), and for a
            client that ends: the password of a protected credential was asked once to find
            the session, and a command asks for no calu password twice. The token is then
            left as it is, with a line that says so: it was known to this process only and
            ends with its session.
    """

    async def work(db: Db) -> bool:
        """Revoke it.

        Args:
            db: The connection.

        Returns:
            Whether the row was there.
        """
        return await herd.revoke_token(db, token_id)

    try:
        _herd_value(store, work, may_ask, db_roles.OPERATOR)
    except NeedsPassword:
        print(
            f"note: the client token {token_id} was not revoked, because that would ask the "
            "password a second time. Only this client knew it, and it ends with its "
            f"session; `calu token revoke {token_id}` revokes it now",
            file=sys.stderr,
        )
    except (CliError, CredentialError, OSError, psycopg.Error) as exc:
        print(
            f"note: the client token {token_id} could not be revoked ({exc}); "
            f"`calu token revoke {token_id}` does it",
            file=sys.stderr,
        )


def _shared_admin() -> Target | None:
    """Return the login of `calu_admin` that `calu setup --no-password` made readable.

    Returns:
        The login with its password, from the place file (`calu.db.place`), or None if
        there is no such file, it cannot be trusted, or it holds no password.
    """
    try:
        shared = db_place.read()
    except db_place.PlaceError:
        return None
    if isinstance(shared.auth, PasswordAuth) and shared.auth.password:
        return shared
    return None


def _herd_value[T](
    store: CredentialStore,
    work: Callable[[Db], Awaitable[T]],
    may_ask: bool = True,
    role: str | None = None,
) -> T:
    """Run `work` with a database login and return its value.

    `_herd_command` prints what `work` returns, which is right for a report and useless for a
    caller that needs the value. The login, in this order (it is not the narrowest one):

        a login kept in `_held`                ──► that login
        the place file holds a login (D228)    ──► that login (`calu_admin`)
        `calu_admin` stored without password   ──► that entry
        `operator`, then `admin` stored        ──► the first of them (`OPERATOR_FIRST`);
                                                   `admin` first while `_hold` is set

    Side effects: records the database it connects to, without a secret, in `_admin_place`
    (for the place file's login and for a stored entry); keeps a stored entry's login, with
    its secret, in `_held` while `_hold` is set or the process runs as root, so that later
    steps ask nothing; an `admin` password typed here stays in `_typed` until the command
    ends (`load_credential`).

    Args:
        store: The credential store.
        work: Called with the connection.
        may_ask: Whether a password may be asked for. False for a process whose stdin carries
            something else -- `calu acp` speaks the editor's protocol on it, and
            `read_password` reads a line from stdin when it is not a terminal, so a prompt
            there would eat the editor's first request and call it the password. An encrypted
            credential is then an error that says what to do instead.
        role: The narrowest role that can do `work` (`_with_admin_value`), e.g.
            `calu_operator` for finding a session and minting or revoking its token; None
            runs it with the login's own role.

    Returns:
        What `work` returned.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored.
        NeedsPassword: If the one that is stored needs a password and `may_ask` is False.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if _held:
        return asyncio.run(_with_admin_value(_held[0], work, role))
    shared = _shared_admin()
    if shared is not None:
        # `sudo calu setup --no-password`: the login every user of the machine may read
        # (D228). Nothing is asked, and this user's own store, which may name another
        # database, is not consulted.
        _admin_place[:] = [shared]
        return asyncio.run(_with_admin_value(shared, work, role))
    names = ("admin", "operator") if _hold else OPERATOR_FIRST
    if ADMIN_ENTRY in store and not store.needs_password(ADMIN_ENTRY):
        # `calu setup --no-password`: the login that asks nothing and may do everything a
        # client needs comes first (D228).
        names = (ADMIN_ENTRY, *names)
    for name in names:
        if name in store:
            if not may_ask and store.needs_password(name):
                raise NeedsPassword(
                    f"the {name!r} credential is protected by a password, and this command has "
                    "no terminal to ask for one on -- its standard input carries the editor's "
                    "protocol. Either store it without a password (`calu credential set "
                    f"{name} --no-password`, which leaves the file's mode as its protection), "
                    "or give this command the session's address and `--token=<token>`, which "
                    "needs no database at all"
                )
            url = load_credential(store, name, f"{name.capitalize()} password: ")
            try:
                with contextlib.suppress(ValueError):  # also TargetError: no usable place
                    _admin_place[:] = [admin_login(Target.parse(url), "")]
                    if _hold or os.geteuid() == 0:
                        # A root process is out of reach of what Claude Code starts, so it
                        # keeps the login for its later steps (revoking its token at the
                        # end) instead of asking the password a second time.
                        _held[:] = [Target.parse(url)]
                return asyncio.run(_with_admin_value(url.decode(), work, role))
            finally:
                wipe(url)
    raise CliError(
        "no credential 'operator' or 'admin'; `calu credential new operator` makes an operator "
        "connection string on the database machine, `calu credential set operator` stores it "
        "here. A client can also be given the address and `--token=<token>` that `calu run` "
        "printed, which needs no database at all"
    )


async def _with_admin_value[T](
    url: str | Target, work: Callable[[Db], Awaitable[T]], role: str | None = None
) -> T:
    """Connect, run `work`, close the connection and return its value.

    Args:
        url: The connection string.
        work: Called with the wrapped connection.
        role: The narrowest role that can do `work`; the connection takes it before `work`
            runs (`calu.db.roles.narrow`, which leaves a login that may not take it as it
            is). None runs `work` with the login's own role.

    Returns:
        What `work` returned.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await open_stored(url)
    try:
        if role is not None:
            await db_roles.narrow(conn, role)
        return await work(Db(conn))
    finally:
        await conn.close()


def _socket_path(chosen: str | None) -> Path | None:
    """Turn the `--socket` argument into a path.

    Args:
        chosen: None if the flag was absent, "" if it was given without a path, else the path.

    Returns:
        The socket path, or None to bind a TCP port as before.

    Raises:
        CliError: If the path is too long for a Unix socket; better to say so now than to let
            the session fail when it binds.
    """
    if chosen is None:
        return None
    path = Path(chosen) if chosen else default_socket_path()
    try:
        check_socket_path(path)
    except OSError as exc:
        raise CliError(str(exc)) from exc
    return path


def cmd_attach(args: argparse.Namespace, store: CredentialStore) -> None:
    """Attach a terminal client to a running session.

    With an ADDRESS it needs no database credential: the session process authenticates the client
    with the token it printed at its start. Without one it finds the session running in this
    directory and mints its own token, which it revokes when it ends (agent/records/PLAN.md D105).

    On a terminal this is the full-screen client (`calu.tui`); with `--plain`,
    through a pipe, or without Textual installed it is the line client (`calu.attach`). The
    choice is made by `calu.tui.start.wants_full_screen`.

    Args:
        args: Parsed arguments (`address`, `token`, `session`, `after_seq`, `thinking`,
            `plain`).
        store: The credential store; used only when no address is given.

    Raises:
        CliError: If the address cannot be reached, the token is refused, or no single session
            of this directory is running.
    """
    run_as = _client_user(args)
    endpoint = _endpoint_of(args, store, "calu attach")
    _attach(endpoint, store, args.plain, args.after_seq, args.thinking, run_as, bool(args.address))


def _attach(
    endpoint: Endpoint,
    store: CredentialStore,
    plain: bool,
    after_seq: int,
    thinking: bool,
    run_as: RunAs | None = None,
    given: bool = False,
) -> None:
    """Run the terminal client on an endpoint until it ends, then revoke a minted token.

    Args:
        endpoint: Where to connect, and the token.
        store: The credential store, for revoking a token that was minted for this client.
        plain: The line client also on a terminal.
        after_seq: Continue after this event sequence number.
        thinking: The line client also prints Claude's thinking.
        run_as: The user a client that runs as root works for (`_client_user`): the client
            then asks the admin password at most once (`_hold_for_client`), does its file
            access and starts its programs as that user, and uses the login it holds in
            memory for settings and for switching sessions.
        given: Whether the address was given on the command line (the remote way): the line
            printed after the client names it, so that attaching again needs no database.

    Raises:
        CliError: If the address cannot be reached, the token is refused, the user cannot be
            switched to, or the client ended with an error.
    """
    full_screen = tui_start.wants_full_screen(plain)
    admin_problem: str | None = None
    if run_as is not None and endpoint.relay is None:
        # Before the switch: root's store is readable now, and the password is asked at
        # most once for the whole run (D214, D249).
        admin_problem = _hold_for_client(store, full_screen)
    if run_as is not None:
        _become_client_of(run_as)
    if admin_problem is not None:
        database: Target | None = None
        print(f"[client] {admin_problem}", file=sys.stderr, flush=True)
    elif _held and _changes_settings(_held[0]):
        # A root client's own login, complete: no dialog asks a password again (D249).
        database = _held[0]
    elif _admin_place:
        # The admin password is asked when a change needs it, and not kept (D213).
        database = _admin_place[0]
    elif run_as is not None or endpoint.server_uid == 0:
        # A session of root's calu, found through a socket: nothing was asked of a database,
        # so which database it is comes from the file only root writes (D228). This user's
        # own store would name another database.
        try:
            database = db_place.read()
        except db_place.PlaceError as exc:
            database = None
            admin_problem = (
                "this client does not know the database of root's calu "
                f"({exc}); `sudo calu setup` writes {db_place.PATH}"
            )
            print(f"[client] {admin_problem}", file=sys.stderr, flush=True)
    else:
        database = None
    database = _with_shared_password(database)
    if given and database is None and admin_problem is None and not _admin_here(store):
        admin_problem = REMOTE_CLIENT
    if endpoint.relay is not None:
        # Through a relay the server answers for its sessions (D242); settings changes still
        # need the database, on the server's machine.
        admin_problem = admin_problem or RELAY_CLIENT
    try:
        if full_screen:
            # On a terminal: the full-screen client (`calu.tui`); it shows thinking by itself.
            while True:
                ended = tui_start.run_tui(
                    endpoint.address,
                    endpoint.token,
                    after_seq,
                    database=database,
                    admin_problem=admin_problem,
                    relay=endpoint.relay,
                    gone=endpoint.gone,
                )
                if ended.switch_to is None:
                    break
                # `/new`, `/fork` or `/switch` in the client: the session it named is attached
                # next, with a token of its own; the one of the session that was left is
                # revoked. Through a relay the server token covers every session.
                print(f"[session] switching to {ended.switch_to}", flush=True)
                following = _following(store, endpoint, ended.switch_to)
                if endpoint.token_id is not None:
                    _revoke(store, endpoint.token_id, may_ask=False)
                endpoint, after_seq = following, 0
                given = given and endpoint.relay is not None
            print("\n".join(tui_start.exit_lines(ended, _again(endpoint, given))))
            code = ended.code
        else:
            while True:
                switched: list[str] = []
                code = asyncio.run(
                    attach(
                        endpoint.address,
                        endpoint.token,
                        after_seq,
                        thinking,
                        relay=endpoint.relay,
                        gone=endpoint.gone,
                        switch_to=switched,
                    )
                )
                if not switched:
                    break
                print(f"[session] switching to {switched[0]}", flush=True)
                following = _following(store, endpoint, switched[0])
                if endpoint.token_id is not None:
                    _revoke(store, endpoint.token_id, may_ask=False)
                endpoint, after_seq = following, 0
    except (OSError, RequestFailed) as exc:
        raise CliError(_attach_failure(endpoint, exc)) from exc
    finally:
        if endpoint.token_id is not None:
            # Never a second password: one was asked, at most, to find the session.
            _revoke(store, endpoint.token_id, may_ask=False)
    if code:
        raise CliError("the client ended with an error")


def _following(store: CredentialStore, endpoint: Endpoint, session_id: str) -> Endpoint:
    """Return the endpoint of the session a client switches to.

    Args:
        store: The credential store, for a session found in the database.
        endpoint: The endpoint of the session that is left.
        session_id: The session to attach to next.

    Returns:
        Through a relay: that session on the same relay, with the same server token (D242).
        Otherwise the session as `_find_endpoint` finds it, with a token minted for it.

    Raises:
        CliError: If the session cannot be found or no database login is there.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if endpoint.relay is not None:
        relay = endpoint.relay
        return Endpoint(
            relay.session_address(session_id), relay.token, None, session_id, relay=relay
        )
    return _find_endpoint(store, session_id, "calu attach")


def _again(endpoint: Endpoint, given: bool) -> str:
    """Say which command attaches to the same session again, after the client ended.

    Args:
        endpoint: The session the client showed last.
        given: Whether the address came from the command line.

    Returns:
        `calu attach` for a session of this directory; the address (the relay's with
        `--session`) and a token placeholder for one given on the command line.
    """
    if endpoint.relay is not None:
        again = f"calu attach {endpoint.relay.address} --session {endpoint.session_id}"
        return again + (" --token=<the same token>" if endpoint.token else "")
    if not given:
        return "calu attach"
    return f"calu attach {endpoint.address}" + (
        " --token=<the same token>" if endpoint.token else ""
    )


RELAY_CLIENT = (
    "this client reaches the session through the relay of `calu serve`: /new, /fork, /rewind, "
    "/running and /switch go through the relay; /rename and saving checker instructions work "
    "in a client on the server's machine (ssh -t <that host> calu attach)"
)
"""Why the full-screen client cannot change settings when it is attached through a relay."""


REMOTE_CLIENT = (
    "this client was given the session's address and this machine has no 'admin' "
    "credential: /new, /fork, /rewind, /running, /switch, /rename and saving checker "
    "instructions work in a client on the session's machine (ssh -t <that host> calu attach)"
)
"""Why the full-screen client cannot reach the database, when it was given a session's
address on a machine without calu's `admin` credential: the remote way of attaching."""


def _admin_here(store: CredentialStore | None) -> bool:
    """Tell whether this machine's store holds an `admin` credential.

    Args:
        store: The credential store; None counts as one without it.

    Returns:
        True if the entry `admin` (or the login of `calu_admin` that `calu setup
        --no-password` stores) is there; False without either or if the store cannot be read.
    """
    if store is None:
        return False
    try:
        return "admin" in store or ADMIN_ENTRY in store
    except CredentialError:
        return False


def _attach_failure(endpoint: Endpoint, exc: BaseException) -> str:
    """Say why a client could not attach, and what to do about it.

    Args:
        endpoint: Where it tried to connect.
        exc: What the attempt raised.

    Returns:
        The message: for a refused token, that a token belongs to one session and how to get
        one; for an address nobody answers at, that a session on another machine needs its
        port forwarded; else the error as it is.
    """
    address = endpoint.address
    if isinstance(exc, RequestFailed):
        if not endpoint.token:
            return (
                f"cannot attach to {address}: this session wants a client token. On the "
                "session's machine `calu token mint <session id>` prints one (`calu sessions` "
                f"shows the id); then `calu attach {address} --token=<token>`, or set "
                "CALU_TOKEN. A session started with --insecure lets local clients in "
                "without one"
            )
        # The peer's sentence without the JSON-RPC code (which `str(exc)` appends).
        said = str(exc.error.get("message") or exc)
        if "server token" in said:
            # The relay's sentence repeats the advice below, so it is left out.
            return (
                f"cannot attach to {address}: the relay refused the server token. A server "
                "token belongs to one `calu serve` and ends with it or with `calu token "
                "revoke --server`: mint a new one on the server's machine with `calu token "
                "mint --server`"
            )
        return (
            f"cannot attach to {address}: the session refused the token ({said}). A token "
            "belongs to one session and ends with it or with `calu token revoke`: mint a new "
            "one on the session's machine with `calu token mint <session id>`"
        )
    if isinstance(exc, OSError) and address.startswith("ws://"):
        reason = unreachable(exc)
        return (
            f"cannot attach to {address}: {reason}. A session on another machine is reached "
            "through a forwarded port: `ssh -L <port>:127.0.0.1:<the session's port> <host>`, "
            "with the session's port from `calu sessions` there; check that the tunnel is "
            "open and that the session still runs"
        )
    return f"cannot attach to {address}: {exc}"


TOKEN_TRIES = 3
"""How often `calu attach` asks for a client token on a terminal before it gives up."""


def _reach(endpoint: Endpoint, hint: str | None = None, pick: bool = True) -> Endpoint:
    """Check an address given on the command line and its token before a client starts.

    The token comes from `--token`, else from the environment variable `CALU_TOKEN` (so that
    it need not stand on a command line, where other users of the machine can read it). If
    the session refuses it and standard input is a terminal, the token is asked for, at most
    `TOKEN_TRIES` times; a blank answer gives up. A session started with `--insecure` lets a
    local client in without a token, so no token is tried first.

    The address may be a `calu serve` relay (D242): its own address, whose session is then
    chosen from the server's sessions (`_through_relay`), or the address of one session on it
    (`ws://host:port/session/<id>`). Either way the endpoint names the relay, so that the
    client can follow `/new`, `/fork` and `/switch`.

    Side effects: one short connection to the session (`initialize`, then closed), which the
    session logs; a prompt on the terminal; on a relay, the requests of `_through_relay`.

    Args:
        endpoint: The address and the token of the command line.
        hint: `--session`: on a relay, the id (or its beginning) of the session to attach to.
        pick: Whether a session is chosen on a relay's own address; False returns the relay
            itself (`calu start --relay`).

    Returns:
        The endpoint with the token the session accepted.

    Raises:
        CliError: If the address cannot be reached or does not answer within 15 seconds, the
            session refuses every token given, or no single session of a relay is named.
    """
    token = endpoint.token or os.environ.get("CALU_TOKEN", "")
    attempt = 0
    while True:
        attempt += 1
        tried = Endpoint(endpoint.address, token, endpoint.token_id, endpoint.session_id)
        try:
            answer = asyncio.run(
                probe(endpoint.address, token, "calu attach (checking the token)", 15.0)
            )
            if "relay" in answer:
                if not pick:
                    return dataclasses.replace(tried, relay=_relay_target(tried, answer))
                return _through_relay(tried, answer, hint)
            if relay_session_id(endpoint.address) is not None:
                hello, _ = asyncio.run(relay_call(endpoint.address, token, "initialize"))
                server = hello.get("relay") or {}
                target = RelayTarget(
                    relay_base(endpoint.address),
                    token,
                    str(server.get("hostname") or ""),
                    str(server.get("os_user") or ""),
                )
                found = relay_session_id(endpoint.address) or ""
                return dataclasses.replace(tried, session_id=found, relay=target)
            return tried
        except RequestFailed as exc:
            if attempt > TOKEN_TRIES or not sys.stdin.isatty():
                raise CliError(_attach_failure(tried, exc)) from exc
            said = "the session refused that token" if token else "this session wants a token"
            print(
                f"calu: {said}; on the session's machine `calu token mint <session id>` prints "
                "one for that session, `calu token mint --server` one for every session of "
                "its `calu serve` (through its relay port)",
                file=sys.stderr,
            )
            try:
                token = getpass.getpass("Client token (Enter gives up): ").strip()
            except EOFError:
                print(file=sys.stderr)
                token = ""
            if not token:
                raise CliError("no client token given: nothing was attached") from exc
        except OSError as exc:  # TimeoutError is one
            if isinstance(exc, TimeoutError):
                raise CliError(
                    f"cannot attach to {endpoint.address}: no answer within 15 seconds"
                ) from exc
            raise CliError(_attach_failure(tried, exc)) from exc


def _relay_target(tried: Endpoint, answer: dict[str, Any]) -> RelayTarget:
    """Describe a relay from its `initialize` result.

    Args:
        tried: An address on the relay and the server token it accepted.
        answer: The relay's `initialize` result (`RelayInitializeResult`).

    Returns:
        The relay's own address, the token, and the server's host, user and projects directory.
    """
    server = answer.get("relay") or {}
    return RelayTarget(
        relay_base(tried.address),
        tried.token,
        str(server.get("hostname") or ""),
        str(server.get("os_user") or ""),
        str(server.get("projects_directory") or ""),
    )


def _through_relay(tried: Endpoint, answer: dict[str, Any], hint: str | None) -> Endpoint:
    """Choose the session a client attaches to through a `calu serve` relay (D242).

        --session given     ──► the one session whose id begins with it
        one session runs    ──► that one
        several run         ──► on a terminal: the user picks one from a numbered list;
                                otherwise an error that lists them
        none runs           ──► an error that says how to start one

    Side effects: one request to the relay (`relay.sessions`); a prompt on the terminal.

    Args:
        tried: The relay's address and the server token it accepted.
        answer: The relay's `initialize` result (`RelayInitializeResult`).
        hint: `--session`, or None.

    Returns:
        The endpoint of the chosen session on the relay.

    Raises:
        CliError: If no single session is chosen, or the relay cannot be asked.
    """
    target = _relay_target(tried, answer)
    who = f"{target.os_user}@{target.hostname}" if target.hostname else "the server"
    if target.projects_directory:
        who += f" (projects directory {target.projects_directory})"
    try:
        _hello, listed = asyncio.run(relay_call(target.address, target.token, "relay.sessions"))
    except (OSError, RequestFailed, ConnectionError) as exc:
        raise CliError(f"cannot list the sessions of {target.address}: {exc}") from exc
    sessions = [entry for entry in (listed or {}).get("sessions") or [] if isinstance(entry, dict)]
    lines = [
        f"    {index}. {entry['calu_session_id']}  {entry.get('state')}"
        + (f" · {entry['open_escalations']} open" if entry.get("open_escalations") else "")
        + f"  {entry.get('directory')}"
        for index, entry in enumerate(sessions, 1)
    ]
    if hint:
        chosen = [e for e in sessions if str(e["calu_session_id"]).startswith(hint)]
        if len(chosen) != 1:
            what = "no session" if not chosen else f"{len(chosen)} sessions"
            raise CliError(
                f"{what} of {who} start with {hint!r}:\n" + ("\n".join(lines) or "    (none)")
            )
    elif len(sessions) == 1:
        chosen = sessions
    elif not sessions:
        raise CliError(
            f"no session runs in the `calu serve` of {who} behind {target.address}. Start "
            "one through the relay: calu start "
            f"<directory in {target.projects_directory or 'its projects directory'}> "
            f"--relay {target.address}"
        )
    elif sys.stdin.isatty():
        print(f"{len(sessions)} sessions run in the `calu serve` of {who}:", file=sys.stderr)
        print("\n".join(lines), file=sys.stderr)
        while True:
            try:
                picked = input("Session (number or beginning of its id, Enter gives up): ")
            except EOFError:
                picked = ""
            picked = picked.strip()
            if not picked:
                raise CliError("no session chosen: nothing was attached")
            if picked.isdigit() and 1 <= int(picked) <= len(sessions):
                chosen = [sessions[int(picked) - 1]]
                break
            chosen = [e for e in sessions if str(e["calu_session_id"]).startswith(picked)]
            if len(chosen) == 1:
                break
            print(f"calu: {picked!r} names {len(chosen)} sessions", file=sys.stderr)
    else:
        raise CliError(
            f"{len(sessions)} sessions run in the `calu serve` of {who}; name one with "
            "--session <id>:\n" + "\n".join(lines)
        )
    session_id = str(chosen[0]["calu_session_id"])
    return Endpoint(
        target.session_address(session_id), target.token, None, session_id, relay=target
    )


def _with_shared_password(database: Target | None) -> Target | None:
    """Fill in the admin password that `calu setup --no-password` made readable (D228).

    Args:
        database: The login of `calu_admin` a client would use, or None.

    Returns:
        `database` with the password of the place file (`calu.db.place`), if it has none of
        its own and the file names the same database and holds one; else `database` as it
        is.
    """
    if database is None or not isinstance(database.auth, PasswordAuth) or database.auth.password:
        return database
    try:
        shared = db_place.read()
    except db_place.PlaceError:
        return database
    same = shared.place == database.place and shared.user == database.user
    if not same or not isinstance(shared.auth, PasswordAuth) or not shared.auth.password:
        return database
    return database.model_copy(update={"auth": shared.auth})


def _endpoint_of(
    args: argparse.Namespace, store: CredentialStore, label: str, may_ask: bool = True
) -> Endpoint:
    """Return where a client should connect, from its arguments or from the database.

    An address on the command line is used as it is, with the token next to it; that is the
    remote path and it touches no database. Without an address the session of this directory is
    looked up and a token minted for this client (agent/records/PLAN.md D105).

    Args:
        args: Parsed arguments (`address`, `token`, `session`).
        store: The credential store, used only for the lookup.
        label: The label of a minted token, e.g. "calu attach".
        may_ask: Whether a password may be asked for; False for `calu acp` (see
            `_herd_value`).

    Returns:
        The endpoint; `token_id` is set only when a token was minted here.

    Raises:
        CliError: If a token was given without an address, or if no single session of this
            directory is running. An address without a token is tried without one (D227).
    """
    if args.token and not args.address:
        raise CliError("--token names the session's address too, e.g. ws://127.0.0.1:8765")
    if args.address:
        # Without `--token` the client tries `CALU_TOKEN`, then none: a session started with
        # `--insecure` lets a local client in (D227); any other refuses, and on a terminal
        # the token is asked for (`_reach`).
        return _reach(Endpoint(args.address, args.token or ""), getattr(args, "session", None))
    return _find_endpoint(store, args.session, label, may_ask)


def claude_login_command(
    action: str,
    method: str | None,
    run_as: RunAs | None,
    environ: Mapping[str, str],
    hidden: Collection[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build the command line and environment of Claude Code's own login for `calu login`.

    Claude Code keeps its login below the home directory of the user it runs as, so the login
    has to run as that user: as calu's own user directly, as another user through calu's
    launcher (`calu.launch`), which sets that user's ids and home -- the user's own home for a
    name, `/var/lib/calu/claude/<uid>` for a bare uid -- the same way a session starts Claude
    Code.

    Args:
        action: "login", "status" or "logout": the `claude auth` subcommand.
        method: For a login: "claudeai", "console" or "sso" (`claude auth login --<method>`),
            or None for Claude Code's default (a Claude subscription).
        run_as: The user Claude Code runs as, or None for calu's own user.
        environ: calu's environment.
        hidden: Names of variables Claude Code must not see; None takes `PROVIDER_KEYS` and
            the names defined in calu's env file (`user_env_file`), which `main` loaded.

    Returns:
        The command line and the environment to start it with: `environ` without the
        `hidden` variables (the checker keys), as a session starts Claude Code.

    Raises:
        LaunchError: If no Claude Code executable is found, or (with `run_as`) this
            installation has no launcher.
    """
    claude = claude_path(None)
    arguments = ["auth", action]
    if action == "login" and method:
        arguments.append(f"--{method}")
    if action == "status":
        arguments.append("--text")
    if hidden is None:
        from dotenv import dotenv_values

        from calu.config import PROVIDER_KEYS, user_env_file

        hidden = set(PROVIDER_KEYS)
        if user_env_file().is_file():
            hidden |= set(dotenv_values(user_env_file()))
    environment = {name: value for name, value in environ.items() if name not in hidden}
    if run_as is None:
        return [claude, *arguments], environment
    environment[LAUNCH_ENV] = run_as.spec(claude).dump()
    return [str(launcher_path()), *arguments], environment


def cmd_login(args: argparse.Namespace, store: CredentialStore) -> None:
    """Log Claude Code in, out, or show its login, for the user it runs as in sessions.

    Without options: as calu's own user. With `--sudo`: calu runs itself again as root
    through sudo for you (`_run_again_through_sudo`), and the login is stored where Claude
    Code finds it when a session runs it as you. As root, `--user` names the user, as for
    `calu run`; for a bare uid the login is stored in the state directory calu gives it.
    The login itself is Claude Code's (`claude auth login`): it opens or prints an address
    to sign in with, in this terminal.

    Args:
        args: Parsed arguments (`method`, `status`, `logout`, `user`, `sudo`, `from_sudo`).
        store: Unused; the login is Claude Code's, not a calu credential.

    Raises:
        CliError: If `--status` and `--logout` are both given, the user cannot be used
            (`_run_as`), Claude Code or the launcher cannot be found, or the login ended with
            an error.
    """
    if args.status and args.logout:
        raise CliError("--status and --logout exclude each other")
    if (args.status or args.logout) and args.method:
        raise CliError("a login method goes with a login, not with --status or --logout")
    run_as = _run_as(args)
    action = "status" if args.status else "logout" if args.logout else "login"
    try:
        command, environment = claude_login_command(action, args.method, run_as, os.environ)
    except LaunchError as exc:
        raise CliError(str(exc)) from exc
    who = "you" if run_as is None else (run_as.user or f"uid {run_as.uid}")
    if action == "login":
        home = "your home directory" if run_as is None else run_as.home
        print(f"Claude Code's login for {who}, kept in {home}:", flush=True)
    code = subprocess.call(command, env=environment)
    if code and action == "status":
        # `claude auth status` ends with 1 when nobody is logged in: say what to do.
        again = " --sudo" if args.from_sudo else f" --user {args.user}" if args.user else ""
        raise CliError(f"Claude Code is not logged in for {who}; `calu login{again}` logs it in")
    if code:
        raise CliError(f"claude auth {action} for {who} ended with exit code {code}")


def cmd_acp(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve a running session to an editor that speaks the Agent Client Protocol.

    The editor spawns this command and talks ACP on its stdin and stdout; stdout therefore carries
    protocol lines only and every message goes to stderr. With an ADDRESS it needs no database
    credential: the session authenticates the client with the token it printed. Without one -- which
    is what an editor is configured with, because it has neither an address nor a token to put in
    its arguments -- the session running in the editor's project directory is looked up and a token
    is minted for this adapter (agent/records/PLAN.md D105). That lookup must not ask for a
    password: an editor gives this process no terminal, so the credential has to be stored with
    `calu credential set operator --no-password` (D104).

    Args:
        args: Parsed arguments (`address`, `token`, `session`).
        store: The credential store; used only when no address is given.

    Raises:
        CliError: If the address cannot be reached, the token is refused, or no single session
            of this directory is running.
    """
    endpoint = _endpoint_of(args, store, "calu acp", may_ask=False)
    try:
        code = asyncio.run(serve_acp(endpoint.address, endpoint.token))
    except (OSError, RequestFailed) as exc:
        raise CliError(_attach_failure(endpoint, exc)) from exc
    finally:
        if endpoint.token_id is not None:
            _revoke(store, endpoint.token_id, may_ask=False)
    if code:
        raise CliError("the adapter ended with an error")


def cmd_ide(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve an editor extension: JSON-RPC on stdin and stdout, one JSON object per line.

    The extension starts this command in its workspace folder (`calu.ide`). stdout carries
    protocol lines only; every message goes to stderr and to the log file
    (`$XDG_STATE_HOME/calu/log/ide.log`). Nothing is asked on the terminal: a credential that
    needs a password is reported to the extension as an error, and the extension repeats the
    request with the password its user typed.

    Side effect: with `--cwd DIR` the process changes to DIR first (an editor that starts
    the bridge over SSH starts it in the home directory).

    Args:
        args: Parsed arguments: `cwd`, the directory to work in, or None for the current
            one.
        store: The credential store.

    Raises:
        SystemExit: With the code 2 and one line on stderr if `--cwd` names no directory.
    """
    if args.cwd:
        try:
            os.chdir(Path(args.cwd).expanduser())
        except OSError as exc:
            print(f"calu ide: --cwd {args.cwd}: {exc.strerror or exc}", file=sys.stderr)
            raise SystemExit(2) from None
    asyncio.run(serve_ide(Path.cwd(), store))


def _web_token_line(host: str, port: int, token: str, has_admin_password: bool) -> str:
    """Build the start line of `calu web` when the address carries the login token.

    Args:
        host: The host name the interface is opened under.
        port: Its port.
        token: The login token.
        has_admin_password: Whether this installation has an admin password a person types.
            Then the database is older than the role `calu_admin`, and `calu setup` would
            make that password the browser's login. After `calu setup --no-password` there
            is no such password, and the token is the only login there is.

    Returns:
        One line: the address with the token, what the token is, and, only with an admin
        password, how the password becomes the login.
    """
    line = (
        f"calu web on http://{host}:{port}/?token={token}"
        " (open it once per browser; the token is your login, keep it to yourself)."
    )
    if has_admin_password:
        line += " `calu setup` makes the admin password the login instead."
    return line


def cmd_web(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve the web interface: every session of every host, their history, the checkers.

    It needs the `admin` credential, because it also changes what steers the checkers, and it serves
    the loopback interface only: its login is the token in the address it prints, there are no user
    accounts (agent/records/PLAN.md Q4), so reach it from another machine through an SSH tunnel.

    The address it prints names the host `calu.localhost` (`calu.web.app.WEB_HOST_NAME`), not
    the bound address: the server answers to no other name, so that the browser keeps the
    login apart from every other server on the loopback interface. The decrypted connection
    string stays in memory until the server stops, because a lost database connection is
    opened again with it (`calu.web.database.ReopeningDb`).

    Args:
        args: Parsed arguments (`host`, `port`).
        store: The credential store.

    Raises:
        CliError: If the web dependencies are missing, the address is not local, the port is
            not between 1 and 65535, or there is no `admin` credential.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    try:
        from calu.web.app import WEB_HOST_NAME, NotLocal, check_host, create_app
        from calu.web.database import ReopeningDb
    except ImportError as exc:  # a damaged installation: these are ordinary dependencies
        raise CliError(
            "a package of the web interface is missing; install calu again: "
            f"`uv tool install --reinstall calu` (or `uv sync` in a clone). {exc}"
        ) from exc
    try:
        check_host(args.host)
    except NotLocal as exc:
        raise CliError(str(exc)) from exc
    if not 1 <= args.port <= 65535:
        # Port 0 would bind a random port, and the address printed below would name port 0.
        raise CliError(f"--port takes a number from 1 to 65535, not {args.port}")
    run_as = _client_user(args)
    # After `calu setup --no-password` nobody knows the password of `calu_admin`, so a
    # browser cannot be asked for it: the address carries the token instead (D228).
    # The same holds for a user who gets that login from the place file (`_shared_admin`).
    typed_password = (
        ADMIN_ENTRY not in store or store.needs_password(ADMIN_ENTRY)
    ) and _shared_admin() is None
    url = _admin_secret(store, args)
    if run_as is not None:
        # As root for a user (`calu web --sudo`): the login was read from root's store and
        # stays in this process's memory; from here on the process touches files as the user.
        _become_client_of(run_as)

    async def serve() -> None:
        """Open the connection, build the app and serve until interrupted."""
        import uvicorn

        # A connection the database closed (a restart, a failover, an idle timeout) is opened
        # again, so the server outlives the database's restarts.
        db = ReopeningDb(await open_stored(url.decode()), lambda: open_stored(url.decode()))
        try:
            values = await admin.settings(db)
            template = str((values.get("web") or {}).get("git_url_template") or "")
            # The login token (D161): any local process could otherwise decide escalations,
            # Claude's own `curl` included. New on every start, never stored.
            token = secrets.token_urlsafe(24)
            # Where `calu setup` gave `calu_admin` its password, a browser logs in with the
            # admin password, which the database checks (D216); the token is then not shown.
            place = Target.parse(url)
            by_password = typed_password and bool(
                await db.scalar(
                    "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (ADMIN_ROLE,)
                )
            )
            app = create_app(
                db,
                git_url_template=template,
                token=token,
                host_name=WEB_HOST_NAME,
                check_password=functools.partial(is_admin_password, place) if by_password else None,
            )
            # Flushed: this is the one line a user needs, and the process then serves for
            # hours. Without it the address sits in a buffer whenever stdout is a pipe -- a
            # log file, a systemd unit, `tmux pipe-pane` -- and the user sees nothing (D125).
            if by_password:
                print(
                    f"calu web on http://{WEB_HOST_NAME}:{args.port}/ (sign in with the "
                    "admin password)",
                    flush=True,
                )
            else:
                print(_web_token_line(WEB_HOST_NAME, args.port, token, typed_password), flush=True)
            config = uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level="warning",
                access_log=False,
                # A chat message may carry images (D220).
                ws_max_size=MAX_FRAME_BYTES,
            )
            await uvicorn.Server(config).serve()
        finally:
            await db.close()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:  # pragma: no cover - the usual way to stop a server
        print("stopped")
    finally:
        wipe(url)


def cmd_protocol(args: argparse.Namespace, store: CredentialStore) -> None:
    """Write the JSON Schema of the client protocol.

    Needs no database and no credential: the schema is generated from the Pydantic models, so a
    client in another language can generate its types from it.

    Args:
        args: Parsed arguments (`out`).
        store: The credential store (unused).

    Raises:
        CliError: If the file cannot be written.
    """
    document = json.dumps(protocol_schema(), indent=2, sort_keys=True)
    if not args.out:
        print(document)
        return
    try:
        Path(args.out).write_text(document + "\n", encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot write {args.out}: {exc}") from exc
    print(f"wrote the protocol schema to {args.out}")


def cmd_sessions(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the sessions of all hosts.

    Args:
        args: Parsed arguments (`all`).
        store: The credential store.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """List the sessions.

        Args:
            db: The connection.

        Returns:
            One line per session.
        """
        rows = await herd.sessions(db, active_only=not args.all)
        return "\n".join(row.line() for row in rows) or "no sessions"

    _herd_command(store, work)


def cmd_list_sessions(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the last Claude conversations of the current directory, the freshest first.

    The directory is the resolved current directory, which is what `calu run` records for a
    session started here; only sessions of this host and of this OS user are listed, because
    Claude Code keeps their transcripts in that user's home directory. Each line names the
    age of the last activity, the Claude session id and the last message of the user; the
    lines after the list say how to continue one.

    Args:
        args: Parsed arguments (`count`).
        store: The credential store.

    Raises:
        CliError: If `count` is below 1, or if neither an `operator` nor an `admin` credential
            is stored.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if args.count < 1:
        raise CliError("the number of sessions to list must be 1 or more")
    here = str(Path.cwd().resolve())
    width = shutil.get_terminal_size((100, 24)).columns

    async def work(db: Db) -> str:
        """List the conversations.

        Args:
            db: The connection.

        Returns:
            One line per conversation and how to resume or fork one, or a sentence that
            says there is none.
        """
        host, user = socket.gethostname(), getpass.getuser()
        rows = await herd.conversations(db, here, host, user, args.count)
        known = await herd.known_conversations(db, here, host, user)
        # Conversations that were started with `claude` itself, from its own transcripts
        # (D209); read in a worker thread, because it reads files.
        found = await anyio.to_thread.run_sync(transcripts, Path(here))

        def last_input(session_id: str) -> str:
            """Return the last message the user typed in a transcript.

            Args:
                session_id: The Claude session id.

            Returns:
                The message, or "" if the transcript holds none.
            """
            inputs = user_inputs(session_id, Path(here))
            return inputs[-1] if inputs else ""

        rows = await anyio.to_thread.run_sync(
            herd.with_transcripts, rows, known, found, last_input, args.count
        )
        if not rows:
            return (
                f"no Claude session of {here} on this host for this user\n"
                "(a session of calu is listed from its first message on; `calu sessions "
                "--all` lists the sessions of every directory, host and user)"
            )
        lines = [row.line(width) for row in rows]
        lines.append("")
        lines.append("continue one:  calu run --resume <session id>")
        lines.append("copy one:      calu run --fork <session id>   (the original stays as it is)")
        lines.append("with a running `calu serve`: calu start --resume | --fork <session id>")
        if any(row.running for row in rows):
            lines.append("running:       a process still has that conversation; use calu attach")
        if any(row.outside for row in rows):
            lines.append(
                "outside calu:  started with `claude` itself, so none of its steps was "
                "checked; if a `claude` still has it open, fork it"
            )
        return "\n".join(lines)

    _herd_command(store, work)


def cmd_escalations(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the open escalations.

    Args:
        args: Parsed arguments (`session`, `rendered`).
        store: The credential store.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """List the open escalations.

        Args:
            db: The connection.

        Returns:
            One entry per escalation.
        """
        rows = await herd.escalations(db, args.session)
        if not rows:
            return "no open escalations"
        parts = [row.line() + (f"\n{row.rendered}" if args.rendered else "") for row in rows]
        return "\n".join(parts)

    _herd_command(store, work)


def cmd_doctor(args: argparse.Namespace, store: CredentialStore) -> None:
    """Report what is in place and what a session would miss.

    Everything that needs no password is checked first: the programs calu starts, the credential
    store, the checker keys, the runtime directories and, if a directory was named, the
    workspace. With `--keys` each checker key that is set is shown to its own provider's free
    model list, so an expired or revoked key is found before it escalates every step (D131).
    With `--db` the `admin` credential is used to ask whether the database answers and whether
    its schema is current, whether a stored checker text is an older shipped one or the
    user's own (`doctor.instruction_findings`), and whether the global settings switch Claude
    Code's sandbox off, in which case the recommendation of requirement 42 is a note unless it
    is met (`doctor.sandbox_off_findings`). Nothing is changed.

    As root with `--user` (or as a user with `--sudo`, which runs calu again through sudo)
    the report is about the arrangement of requirement 26: calu's own files are read as
    root, then the process switches its file access and the programs it starts to that user
    (`calu.asuser.enter`), so that Claude Code's version, its settings files, the sandbox and
    the workspace are judged as that user finds them. No directory is created for the user.
    As root without `--user` the report says that it looked at root's own account.

    Args:
        args: Parsed arguments (`db`, `keys`, `directory`, `user`, `sudo`, `from_sudo`).
        store: The credential store.

    Raises:
        CliError: If something a session cannot run without is missing, after the whole report
            was printed — so the reader sees everything, not only the first problem; or if
            the user cannot be used or `--sudo` cannot run calu again (`_run_as`).
        CredentialError: On a wrong password (only with `--db`).
    """
    run_as = _run_as(args, prepare=False) if args.sudo or args.user is not None else None
    # Root's own files first: after the switch to the user they cannot be read.
    stored = doctor.store_findings(store)
    protection = login_protection(store, run_as is not None)
    database: list[doctor.Finding] = []
    if args.db:
        url = load_credential(store, "admin", "Admin password: ")
        try:
            database = asyncio.run(doctor.database_findings(url.decode()))
            database += asyncio.run(doctor.instruction_findings(url.decode()))
            sandbox = asyncio.run(doctor.stored_sandbox(url.decode()))
        finally:
            wipe(url)
        database += doctor.sandbox_off_findings(sandbox, protection)
    about_user: list[doctor.Finding] = []
    if run_as is not None:
        about_user = doctor.user_findings(run_as)
        try:
            asuser.enter(run_as)
        except asuser.UserError as exc:
            raise CliError(str(exc)) from exc
    elif os.geteuid() == 0 and args.user is None:
        about_user = doctor.root_without_user_findings()
    findings = [*about_user, *doctor.machine_findings(), *stored]
    if args.directory:
        findings += doctor.workspace_findings(Path(args.directory).resolve())
    if args.keys:
        findings += doctor.key_validity_findings()
        findings += doctor.aws_findings()
    findings += database
    # Flushed, so the report is on screen before the error line that may follow on stderr.
    print(doctor.report(findings), flush=True)
    if doctor.essential_missing(findings):
        raise CliError("something a session cannot run without is missing (see above)")


def cmd_summary(args: argparse.Namespace, store: CredentialStore) -> None:
    """Show or correct the chat summary a session's checkers read (agent/records/PLAN.md D40).

    The summary is the conversation the checkers see for everything older than the unfolded
    turns, written by the summarizer model. A wrong one misleads every later check, so it is
    worth correcting by hand; a running session applies the change at its next check, because
    the same `config_changed` notification that reloads the rules re-reads this.

    With `--edit` the password is asked once: the summary is read, the editor runs outside the
    database connection, and the result is stored with a second one. If storing fails, the
    temporary file stays and its path is printed, so no work is lost.

    Args:
        args: Parsed arguments (`session`, `edit`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential or no editor is available.
        CredentialError: On a wrong password.
        admin.AdminError: If the session is unknown or the edited summary is empty; `main`
            reports it as one line, and the temporary file keeps the text.
        psycopg.Error: On database errors.
    """
    if not args.edit:

        async def show(db: Db) -> str:
            """Read the summary.

            Args:
                db: The connection.

            Returns:
                The summary, or a line saying there is none yet.
            """
            found = await admin.chat_summary(db, args.session)
            return found or "no chat summary yet: none of this session's turns are folded"

        _admin_command(store, show, args)
        return

    url = _admin_secret(store, args)
    try:
        admin_url = url.decode()

        async def read(db: Db) -> str:
            """Read the summary to edit.

            Args:
                db: The connection.

            Returns:
                The stored summary, or the empty-summary placeholder.
            """
            return await admin.chat_summary(db, args.session) or EMPTY_CHAT_SUMMARY

        before = asyncio.run(_with_admin(admin_url, read))
        after, path = edit_in_editor(before, "chat-summary.md")
        if after == before:
            print("unchanged; nothing was stored")
            return

        async def write(db: Db) -> str:
            """Store the edited summary and notify the session.

            Args:
                db: The connection.

            Returns:
                The report.
            """
            await admin.set_chat_summary(db, args.session, after)
            cleared = not after.strip()
            what = "cleared" if cleared else "replaced"
            return f"the chat summary was {what}; the session applies it at its next check" + (
                ", and the checkers then read no folded turns" if cleared else ""
            )

        try:
            print(asyncio.run(_with_admin(admin_url, write)))
        except admin.AdminError, psycopg.Error:
            print(f"the edited summary is kept in {path}", file=sys.stderr)
            raise
    finally:
        wipe(url)


def cmd_escalation(args: argparse.Namespace, store: CredentialStore) -> None:
    """Show everything needed to decide one escalation, from the database alone.

    See agent/records/PLAN.md D30.

    No session process is involved: an operator who can reach the database sees why the step
    was escalated, what each checker decided about it and why, what it would do, what Claude
    asked if it was a question, and the conversation as the checkers saw it — also for a
    session on another host, or one that has already ended. The live stream of a running
    session is **not** in the database (D3); `calu attach` shows that.

    Args:
        args: Parsed arguments (`id`, `full`).
        store: The credential store.

    Raises:
        CliError: If neither credential is stored (`operator` or `admin`), or no
            escalation has that id.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """Read the escalation and its context.

        Args:
            db: The connection.

        Returns:
            The rendering.

        Raises:
            CliError: If no escalation has that id.
        """
        row = await herd.escalation(db, args.id)
        if row is None:
            raise CliError(f"no escalation {args.id}")
        context = await herd.escalation_context(db, args.id)
        runs = await herd.escalation_runs(db, args.id)
        return herd.render_escalation(row, context, full=args.full, runs=runs)

    _herd_command(store, work)


def cmd_resolve(args: argparse.Namespace, store: CredentialStore) -> None:
    """Allow, deny, answer or cancel an open escalation; the waiting session continues.

    "answer" belongs to a question Claude asked (`AskUserQuestion`, agent/records/PLAN.md D35): the
    note is the answer it receives, and the call itself does not run.

    "cancel" is for an escalation nobody can act on any more -- the session's process is gone
    (D56), which `calu escalations` marks. It records a cancellation rather than a decision, so
    the history does not claim that somebody allowed or denied a step that never ran (D57).

    Args:
        args: Parsed arguments (`id`, `decision`, `text`, `note`). `text` and `--note` are the
            same thing written two ways, so `calu resolve 7 answer "Spaces"` works as well as
            `calu resolve 7 answer --note Spaces`; `--note` wins if both are given.
        store: The credential store.

    Raises:
        CliError: If neither credential is stored (`operator` or `admin`), or the
            escalation is not open.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """Resolve the escalation.

        Args:
            db: The connection.

        Returns:
            The report.

        Raises:
            CliError: If the escalation is not open any more.
        """
        note = args.note or args.text or ""
        if not await herd.resolve(db, args.id, args.decision, note, actor=herd.caller()):
            raise CliError(f"escalation {args.id} is not open (resolved, cancelled or unknown)")
        shown = f": {note}" if note else ""
        if args.decision == "cancel":
            return f"escalation {args.id}: cancelled{shown} (no decision was recorded)"
        return f"escalation {args.id}: {args.decision}{shown}"

    _herd_command(store, work)


def _attach_recipe(row: herd.SessionRow) -> str:
    """Say how a client attaches with a token just minted, here and from another machine.

    Args:
        row: The session the token is for.

    Returns:
        Lines to append to the minting report; "" for a session that has ended or serves no
        clients. A TCP address is a port on the session host's loopback interface, so a
        client elsewhere forwards that port first (`ssh -L`); a Unix socket is forwarded to a
        local port the same way.
    """
    if row.ended_at is not None or not row.address:
        return ""
    if row.address.startswith("ws://"):
        port = row.address.rsplit(":", 1)[-1]
        forward, local = f"{port}:127.0.0.1:{port}", row.address
    else:
        forward = f"8765:{row.address.removeprefix('unix:')}"
        local = "ws://127.0.0.1:8765"
    return (
        f"\n\nThe session listens at {row.address} on {row.hostname}. From another machine, "
        "forward it and attach there (the token can also come from the environment variable "
        "CALU_TOKEN, or `calu attach` asks for it):\n\n"
        f"    ssh -N -L {forward} {row.hostname}\n"
        f"    calu attach {local} --token=<the token above>"
    )


def cmd_token(args: argparse.Namespace, store: CredentialStore) -> None:
    """Mint, list or revoke the client tokens of a session (agent/records/PLAN.md D30).

    A minted token lets a client attach to that session without the terminal that started it:
    an editor's ACP adapter, a local command or a remote interface keeps its token in its own
    configuration, and revoking it ends that access. The session accepts a new token at the
    next `initialize`, without being restarted.

    With `--server` the tokens are those of a `calu serve`'s relay (`_server_token`, D242).

    Args:
        args: Parsed arguments (`action`, `session`, `label`, `server`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the arguments do not fit the action, or the
            token or session does not exist.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """Run the chosen action.

        Args:
            db: The connection.

        Returns:
            The report; for `mint` it holds the token, which is printed only this once.

        Raises:
            CliError: If an argument is missing or the token or session is unknown.
        """
        if args.server:
            return await _server_token(db, args)
        if args.action == "mint":
            if not args.session:
                raise CliError("calu token mint needs the id of a session (`calu sessions`)")
            try:
                token_id, token = await herd.mint_token(db, args.session, args.label or "")
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            row = await herd.session(db, args.session)
            return (
                f"token {token_id} for session {args.session}:\n\n    {token}\n\n"
                "This is the only copy; calu stores just its hash. Give it to one client and "
                f"end its access with `calu token revoke {token_id}`."
                + (_attach_recipe(row) if row is not None else "")
            )
        if args.action == "revoke":
            if args.session is None:
                raise CliError("calu token revoke needs the id of a token (`calu token list`)")
            try:
                token_id = int(args.session)
            except ValueError as exc:
                raise CliError(f"{args.session!r} is not a token id") from exc
            if not await herd.revoke_token(db, token_id):
                raise CliError(f"token {token_id} is unknown or already revoked")
            return f"token {token_id} revoked; a client that is still connected keeps its session"
        rows = await herd.list_tokens(db, args.session)
        report = "\n".join(row.line() for row in rows) if rows else "no client tokens"
        if args.session is None:
            try:
                server_rows = await servers.tokens(db)
            except psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn:
                server_rows = []  # a schema before migration 0040
            if server_rows:
                report += "\n\nserver tokens (`calu token revoke --server <id>`):\n"
                report += "\n".join(row.line() for row in server_rows)
        return report

    _herd_command(store, work)


async def _server_token(db: Db, args: argparse.Namespace) -> str:
    """Mint, list or revoke the tokens of a `calu serve`'s relay (D242).

    Args:
        db: The connection (operator or owner).
        args: Parsed arguments (`action`, `session`: a server id or a token id, `label`).

    Returns:
        The report; for `mint` it holds the token, printed this once, and the commands that
        reach the relay from another machine.

    Raises:
        CliError: If no running server or several match (a server that died without ending
            its row does not run: `calu.servers.running`), the token is unknown, or the
            database has no `servers` table or heartbeat yet (`calu setup` adds them).
    """
    try:
        if args.action == "list":
            rows = await servers.tokens(db)
            return "\n".join(row.line() for row in rows) if rows else "no server tokens"
        if args.action == "revoke":
            try:
                token_id = int(args.session or "")
            except ValueError as exc:
                raise CliError(
                    "calu token revoke --server needs the id of a server token "
                    "(`calu token list --server`)"
                ) from exc
            if not await servers.revoke(db, token_id):
                raise CliError(f"server token {token_id} is unknown or already revoked")
            return (
                f"server token {token_id} revoked; the relay closes its connections within seconds"
            )
        if args.session:
            row = await servers.server(db, args.session)
            if row is None or not row.live:
                stopped = ""
                if row is not None and row.stale:
                    why = "its process is gone" if row.gone else "it has not reported for a minute"
                    stopped = f"; server {row.id} did not end its row, but {why}"
                raise CliError(
                    f"no running server has an id that starts with {args.session!r}{stopped} "
                    "(`calu token list --server` names the servers of the tokens)"
                )
        else:
            here, user = socket.gethostname(), serve_os_user()
            running = await servers.running(db, here, user)
            if not running:
                raise CliError(
                    f"no `calu serve` runs for {user} on {here}; start one, or name a server's "
                    "id (`calu token mint --server <id>`)"
                )
            if len(running) > 1:
                lines = "\n".join(f"    {row.line()}" for row in running)
                raise CliError(f"{len(running)} servers run; name one by its id:\n{lines}")
            row = running[0]
        token_id, token = await servers.mint(db, row.id, args.label or "")
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        raise CliError(
            "this database has no server tokens yet, or an older form of them; `calu setup` "
            "brings it up to date"
        ) from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    serves = f", projects directory {row.projects_directory}" if row.projects_directory else ""
    report = (
        f"server token {token_id} for the `calu serve` of {row.os_user}@{row.hostname} "
        f"(server {row.id}{serves}):\n\n    {token}\n\n"
        "This is the only copy; calu stores just its hash. It reaches every session of that "
        "server and lets its holder decide their steps and ask the server for new sessions "
        "in its projects directory. "
        f"It ends with the server or with `calu token revoke --server {token_id}`."
    )
    if row.relay_address.startswith("ws://"):
        port = row.relay_address.rsplit(":", 1)[-1]
        report += (
            f"\n\nThe relay listens at {row.relay_address} on {row.hostname}. From another "
            "machine, forward it once and attach there (the token can also come from the "
            "environment variable CALU_TOKEN, or `calu attach` asks for it):\n\n"
            f"    ssh -N -L {port}:127.0.0.1:{port} {row.hostname}\n"
            f"    calu attach ws://127.0.0.1:{port} --token=<the token above>"
        )
    else:
        report += "\n\nThat server has no relay (it was started with --no-relay)."
    return report


def cmd_evals(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the checker evaluation and print its report.

    Args:
        args: Parsed arguments (`files`, `from_db`, `no_builtin`, `model`, `repeat`,
            `directory`, `project`).
        store: The credential store.

    With `--list` or `--export` the cases are only collected: they are printed or written as
    YAML and no model is called, which is how the labels a herd produced are read, kept and
    later re-run as a fixed set (agent/records/PLAN.md D55).

    Raises:
        CliError: If neither credential is stored (`operator` or `admin`), a case file cannot
            be read or written, or the run found a critical miss (a checker let a step through
            that it should not have).
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    # Imported here: the evaluation runs checkers, and their import (Pydantic AI) takes over a
    # second that no other command should pay.
    from calu.evals.cli import collect_cases, run_evals

    workspace = Path(args.directory).resolve()
    critical = 0

    async def collect(db: Db) -> str:
        """Collect the cases and print or write them, without calling a model.

        Args:
            db: The connection.

        Returns:
            The report: the case lines for `--list`, or where the file was written.

        Raises:
            CliError: If a case file cannot be read or the export cannot be written.
        """
        try:
            cases = await collect_cases(db, args.files, args.from_db, not args.no_builtin)
        except OSError as exc:
            raise CliError(f"cannot read a case file: {exc}") from exc
        if args.export:
            document = cases_to_yaml(cases)
            if args.export == "-":
                return document.rstrip("\n")
            target = Path(args.export)
            try:
                # In a thread: writing a file is blocking, and this runs inside the event loop.
                await anyio.to_thread.run_sync(
                    lambda: target.write_text(document, encoding="utf-8")
                )
            except OSError as exc:
                raise CliError(f"cannot write {args.export}: {exc}") from exc
            return f"wrote {len(cases)} case(s) to {args.export}"
        lines = [case_line(case) for case in cases]
        return "\n".join([*lines, f"{len(cases)} case(s)"]) if lines else "no cases"

    async def work(db: Db) -> str:
        """Run the evaluation.

        Args:
            db: The connection.

        Returns:
            The report text.

        Raises:
            CliError: If a case file cannot be read.
        """
        nonlocal critical
        try:
            report, critical = await run_evals(
                db,
                workspace,
                args.project,
                args.files,
                from_db=args.from_db,
                builtin=not args.no_builtin,
                model=args.model,
                repeat=args.repeat,
            )
        except OSError as exc:
            raise CliError(f"cannot read a case file: {exc}") from exc
        return report

    if args.list_cases or args.export:
        # Looking at the labels, or keeping them, calls no model and costs nothing: this is the
        # path that replaces a training mode (agent/records/PLAN.md D55, Q9).
        _herd_command(store, collect)
        return
    _herd_command(store, work)
    if critical:
        raise CliError(f"{critical} critical miss(es): a checker let a step through")


EDITOR_FALLBACKS = ("nano", "vim", "vi")
"""Editors `calu ... edit` tries when neither VISUAL nor EDITOR is set."""


def editor_command() -> list[str]:
    """Return the command that opens a file in the user's editor.

    Args:
        None.

    Returns:
        The command and its arguments, e.g. `["nano"]` or `["code", "-w"]`; the file name is
        appended by the caller. `$VISUAL` wins over `$EDITOR`, and both may carry arguments.

    Raises:
        CliError: If neither variable is set and none of `EDITOR_FALLBACKS` is installed.
    """
    for variable in ("VISUAL", "EDITOR"):
        value = os.environ.get(variable, "").strip()
        if value:
            return shlex.split(value)
    for candidate in EDITOR_FALLBACKS:
        if shutil.which(candidate):
            return [candidate]
    raise CliError(
        "no editor found: set EDITOR (or VISUAL), or pass the text with --instructions-file"
    )


def edit_in_editor(text: str, name: str) -> tuple[str, Path]:
    """Write `text` to a temporary file, open it in the editor and read it back.

    The file is kept, so that nothing is lost if storing the result fails; the caller tells the
    user where it is in that case.

    Args:
        text: The text to edit.
        name: File name inside the temporary directory, e.g. "command-instructions.md"; the
            suffix decides the editor's syntax highlighting.

    Returns:
        The text after the edit and the path of the file.

    Raises:
        CliError: If no editor is available, the editor cannot be started, or it exits with an
            error.
        OSError: If the temporary file cannot be written or read.
    """
    command = editor_command()
    path = Path(tempfile.mkdtemp(prefix="calu-edit-")) / name
    path.write_text(text, encoding="utf-8")
    print(f"editing {path} with {' '.join(command)}")
    try:
        completed = subprocess.run([*command, str(path)], check=False)
    except OSError as exc:
        raise CliError(f"cannot start the editor {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        raise CliError(f"the editor exited with {completed.returncode}; nothing was stored")
    return path.read_text(encoding="utf-8"), path


def cmd_project(args: argparse.Namespace, store: CredentialStore) -> None:
    """Register a project or list the registered ones.

    Args:
        args: Parsed arguments (`action`, `directory`, `name`, `git_remote`, `network`).
            `network` changes the policy of an existing project (D39); `add` sets it at once.
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the directory does not exist, the project
            exists already, or `network` was asked for without a name.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    name = args.name
    directory: Path | None = None
    if args.action == "add" and (args.directory or not name):
        directory = Path(args.directory or ".").resolve()
        if not directory.is_dir():
            raise CliError(f"{directory} is not a directory")
        name = name or directory.name

    async def work(db: Db) -> str:
        """Add a project, list them, or change one's network policy.

        Args:
            db: The connection.

        Returns:
            The report.

        Raises:
            CliError: If `network` was asked for without a project name.
        """
        if args.action == "list":
            rows = await admin.projects(db)
            return "\n".join(row.line() for row in rows) or "no projects"
        if args.action == "network":
            if not name:
                raise CliError("calu project network needs --name and a --network policy")
            changed = await admin.set_network_policy(db, name, args.network)
            return (
                f"project {changed.name!r} now has network {changed.network_policy}; a running "
                "session applies it when it next starts"
            )
        row = await admin.add_project(db, name or "", args.git_remote, args.network)
        if directory is not None:
            await register_directory(db, row.name, directory, socket.gethostname())
        report = [
            f"added project {row.name!r}; "
            + (
                f"`calu run` in {directory} or below uses it, elsewhere `--project {row.name}`"
                if directory is not None
                else f"use it with `calu run --project {row.name}`"
            ),
            f"network policy {row.network_policy}: "
            + {
                "none": "its commands reach nothing",
                "proxy": "its commands reach the domains in claude.sandbox.allowed_domains",
                "full": "its commands reach every domain through the sandbox's proxy",
            }[row.network_policy],
        ]
        if row.network_policy == "proxy":
            report.append(
                f"wrote a starter allow list of {len(DEFAULT_ALLOWED_DOMAINS)} domains "
                "(package registries, git over HTTPS, OS packages); widen it with "
                f"`calu config add claude.sandbox.allowed_domains <domain> --project {row.name}`"
            )
        return "\n".join(report)

    _admin_command(store, work, args)


def _value_at(values: dict[str, Any], key: str) -> str:
    """Render the value a dotted key holds in stored settings, for a report line.

    Args:
        values: Stored settings, as `calu.admin.settings` returns them.
        key: A dotted path into them.

    Returns:
        The value as compact JSON, or "nothing" if the key is not in them.
    """
    node: Any = values
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return "nothing"
        node = node[part]
    return json.dumps(node, sort_keys=True)


def _configuration_table(
    global_values: dict[str, Any],
    project_values: dict[str, Any],
    project: str | None,
    changed_only: bool = False,
) -> str:
    """Render every configuration key with its effective value, its origin and its default.

    `calu config get` prints the **stored** values of one scope, which is `{}` on a fresh
    installation and says nothing about what a session will actually use. This answers the other
    question -- "what is set, what could be set, and where does each value come from"
    (agent/records/PLAN.md D128). The keys and defaults come from `calu.configdoc`, the same walk
    the reference document is generated from.

    Args:
        global_values: The stored global settings (`calu.admin.settings`).
        project_values: The stored settings of `project`, or {} when no project was named.
        project: The project whose scope is shown, or None for the global one.
        changed_only: Leave out the keys that still hold their default.

    Returns:
        One line per key: the key, the effective value, where it comes from, and the default
        when it differs. Nested models are headings rather than rows.
    """
    effective = Config.model_validate(merge_settings(global_values, project_values))
    scope = f"project {project}" if project else "global"
    lines: list[str] = []
    for key, kind, default, _description in setting_rows():
        # A row for a nested model is a heading in the reference and has no value of its own.
        if kind == "object" or default == "see below":
            continue
        shown = _shown(_node_at(effective.model_dump(mode="json"), key))
        where = (
            f"{scope} (set)"
            if _is_set(project_values if project else global_values, key)
            else "global (set)"
            if project and _is_set(global_values, key)
            else "default"
        )
        if changed_only and where == "default":
            continue
        tail = "" if where == "default" else f"   default: {default.strip('`')}"
        lines.append(f"{key:<42}  {shown:<30}  {where}{tail}")
    if not lines:
        return (
            f"every setting of the {scope} scope holds its default "
            "(`calu config list` without --changed shows them all)"
        )
    head = f"{len(lines)} setting(s), scope: {scope}"
    return "\n".join(
        [head, *lines, "", "What each setting means: docs/CONFIGURATION.md, or the web interface."]
    )


def _node_at(values: dict[str, Any], key: str) -> Any:
    """Return the value a dotted key holds, or `KeyError`'s stand-in `None`.

    Args:
        values: A (partial) configuration as a dict.
        key: The dotted path.

    Returns:
        The value, or None when the path is not there.
    """
    node: Any = values
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _shown(value: Any) -> str:
    """Render a configuration value for one line of the table.

    A reader compares these with the defaults in the same table, so the rendering has to be
    the same shape for both: a plain string without JSON quotes, an empty one and an unset
    optional said in words rather than as `""` and `null`, everything else compact JSON.

    Args:
        value: The effective value.

    Returns:
        The text for the line.
    """
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value or "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, sort_keys=True)


def _is_set(values: dict[str, Any], key: str) -> bool:
    """Tell whether a dotted key is present in a stored settings dict.

    Args:
        values: A stored (partial) configuration.
        key: The dotted key.

    Returns:
        True if every part of the path exists, so the scope really stores this value.
    """
    current: Any = values
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def cmd_config(args: argparse.Namespace, store: CredentialStore) -> None:
    """Read or change configuration values of the global or a project scope.

    `list` prints every key with its effective value, where that value comes from and its
    default; `get`, `set` and `unset` work on whole values; `add` and `remove` change one entry
    of a list setting and keep the others, starting from the list the scope effectively uses
    (`calu.admin.add_to_setting`).

    Args:
        args: Parsed arguments (`action`, `key`, `value`, `project`, `changed`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, or the key or value is refused.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if args.action in ("set", "add", "remove") and args.value is None:
        raise CliError(f"config {args.action} needs a KEY and a VALUE")

    async def work(db: Db) -> str:
        """Read or change one value.

        Args:
            db: The connection.

        Returns:
            The report.
        """
        if args.action == "models":
            model, effort, how = _chosen_model(args.key or None, args.effort)
            changes = await admin.set_all_models(db, model, effort, args.project)
            scope = f"the {args.project} project" if args.project else "the global scope"
            return "\n".join(
                [f"every model role of {scope} now uses {model or 'the default model'} ({how}):"]
                + [f"  {line}" for line in changes]
                + ["A running session picks the new models up at its next check."]
            )
        if args.action == "list":
            return _configuration_table(
                await admin.settings(db, None),
                await admin.settings(db, args.project) if args.project else {},
                args.project,
                changed_only=args.changed,
            )
        if args.action == "get":
            values = await admin.settings(db, args.project)
            if args.key:
                for part in args.key.split("."):
                    values = values.get(part, {}) if isinstance(values, dict) else {}
            return json.dumps(values, indent=2, sort_keys=True)
        scope = f"the {args.project or 'global'} scope"
        if args.action == "set":
            value = admin.parse_value(args.value)
            await admin.set_setting(db, args.key, value, args.project)
            done = f"set {args.key} in {scope}"
            offer = await admin.enable_offer(db, args.key, value, args.project)
            if offer is not None:
                where = f" --project {shlex.quote(args.project)}" if args.project else ""
                done += (
                    f"\nhint: {offer['text']} Switch them on with:\n"
                    f"  calu config set {offer['key']} true{where}"
                )
            return done
        if args.action in ("add", "remove"):
            entry = admin.parse_value(args.value)
            if args.action == "add":
                merged, added = await admin.add_to_setting(db, args.key, entry, args.project)
                where = _value_at(merged, args.key)
                if not added:
                    return f"{args.key} already has {entry!r} in {scope}; now {where}"
                return f"added {entry!r} to {args.key} in {scope}; now {where}"
            merged = await admin.remove_from_setting(db, args.key, entry, args.project)
            where = _value_at(merged, args.key)
            return f"removed {entry!r} from {args.key} in {scope}; now {where}"
        await admin.unset_setting(db, args.key, args.project)
        return f"unset {args.key} in {scope}"

    _admin_command(store, work, args)


def cmd_checker(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, show or change the checker definitions, or report how they have decided.

    `stats` reads what the checkers already decided and what humans did with it, so it costs nothing
    and describes this project's real steps rather than the eval set (agent/records/PLAN.md D61).
    `misses` lists the rows those counts point at -- a step a checker approved that a human then
    denied -- with the escalation id, so `calu escalation <id>` shows the whole context and the
    instructions can be fixed against a real case (D62).

    `set --instructions-file` reads the new instructions from a file, or from stdin for "-";
    `edit` opens them in `$EDITOR` and stores them when the editor closes. `show` and `edit`
    say on stderr whether the text is the present shipped one, an older shipped one (which
    `update` replaces with the present one) or the user's own (which `diff` compares with the
    shipped one and nothing replaces; agent/records/PLAN.md D248).

    Args:
        args: Parsed arguments (`action`, `name`, `instructions_file`, `model`, `enable`,
            `disable`, `route`, `request_limit`, `output_tokens_limit`, `max_tokens`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the checker is unknown, or the change is
            refused.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    if args.action not in ("list", "stats", "misses") and not args.name:
        raise CliError(f"checker {args.action} needs a NAME")
    if args.action == "edit":
        _edit_checker(args, store)
        return
    if args.action == "template":
        _checker_template(args, store)
        return
    text: str | None = None
    if args.instructions_file:
        try:
            text = (
                sys.stdin.read()
                if args.instructions_file == "-"
                else Path(args.instructions_file).read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise CliError(f"cannot read {args.instructions_file}: {exc}") from exc

    async def work(db: Db) -> str:
        """List, show or change a checker.

        Args:
            db: The connection.

        Returns:
            The report.
        """
        if args.action == "list":
            rows = await admin.checkers(db)
            return "\n".join(row.line() for row in rows) or "no checkers"
        if args.action == "misses":
            # The rows D61's counts point at: what a checker approved and a human refused.
            found = await herd.checker_misses(db, args.name, args.days)
            if not found:
                scope = f" of {args.name}" if args.name else ""
                return (
                    f"no misses{scope}: no step a checker approved was denied by a human. "
                    "They only appear when an auto-escalation category makes somebody look at "
                    "an approved step (`calu config set auto_escalate.edits true`)."
                )
            return "\n\n".join(row.line() for row in found)
        if args.action == "stats":
            # From the record, not by running anything: free, and about this project's real
            # steps rather than the eval set (agent/records/PLAN.md D61).
            stats = await herd.checker_stats(db, args.days)
            if not stats:
                return "no checker has run yet"
            window = f" in the last {args.days} day(s)" if args.days else ""
            # How many steps reached a checker at all, and what the gate costs in time: the
            # rules settle a step without a model call, so that share is the free one (D64).
            steps = await herd.step_stats(db, args.days)
            return "\n".join(
                [
                    f"what the checkers decided{window}:",
                    *(row.line() for row in stats),
                    *([""] if steps.lines() else []),
                    *steps.lines(),
                ]
            )
        if args.action == "show":
            stored = await admin.instructions(db, args.name)
            note = _shipped_note(args.name, stored)
            if note:
                # On stderr: `calu checker show NAME > file` keeps the text alone.
                print(note, file=sys.stderr)
            return stored
        if args.action == "diff":
            stored = await admin.instructions(db, args.name)
            if args.name not in SHIPPED_TEXTS:
                return f"calu ships no instructions for a checker named {args.name!r}"
            status = shipped_status(args.name, stored)
            note = _shipped_note(args.name, stored)
            return note if status.kind == "current" else f"{note}\n{status.diff(stored)}"
        if args.action == "update":
            row = await admin.update_instructions(db, args.name)
            return f"updated to the present shipped version: {row.line()}"
        enabled = True if args.enable else (False if args.disable else None)
        row = await admin.set_checker(
            db,
            args.name,
            instructions_text=text,
            model=args.model,
            enabled=enabled,
            request_limit=args.request_limit,
            output_tokens_limit=args.output_tokens_limit,
            max_tokens=args.max_tokens,
            route=args.route,
        )
        return row.line()

    _admin_command(store, work, args)


def _checker_template(args: argparse.Namespace, store: CredentialStore) -> None:
    """List or show the shipped templates of checker instructions, or load one (D250).

        calu checker template list                         name, checker type, summary
        calu checker template show NAME                    the text `use` would store
        calu checker template use NAME [--checker C] [--append | --replace]

    `list` and `show` read only the files shipped with calu and need no database. `use`
    stores the template in the checker type it is written for, or in `--checker`
    (`admin.use_template`: a text calu shipped or an unchanged template is replaced; the
    user's own text only with `--replace`, or the template goes after it with `--append`;
    nothing is stored when the instructions changed between the read and the write).

    Args:
        args: Parsed arguments (`name`: "list", "show" or "use"; `target`: the template;
            `checker`, `append`, `replace`).
        store: The credential store.

    Raises:
        CliError: If the sub-action is unknown, a template name is missing or unknown, or
            there is no `admin` credential.
        CredentialError: On a wrong password.
        admin.AdminError: If the template cannot be loaded (see `admin.use_template`).
        psycopg.Error: On database errors.
    """
    what = args.name
    if what not in ("list", "show", "use"):
        raise CliError("checker template needs list, show NAME or use NAME")
    try:
        if what == "list":
            for found in library.templates():
                print(f"{found.name:<24} {found.checker:<16} {found.summary}")
            return
        if not args.target:
            raise CliError(f"checker template {what} needs the template's NAME")
        chosen = library.template(args.target)
    except library.TemplateError as exc:
        raise CliError(str(exc)) from exc
    if what == "show":
        print(f"# template {chosen.name} for the {chosen.checker} checker", file=sys.stderr)
        print(chosen.text, end="")
        return
    checker = args.checker or chosen.checker

    async def work(db: Db) -> str:
        """Load the template into the checker.

        Args:
            db: The connection.

        Returns:
            The report.
        """
        row = await admin.use_template(
            db, checker, chosen, append=args.append, replace=args.replace
        )
        how = "added to" if args.append else "loaded into"
        return f"template {chosen.name} {how} the {checker} checker: {row.line()}"

    _admin_command(store, work, args)


def _shipped_note(name: str, stored: str) -> str:
    """Say how a checker's stored instructions relate to the texts calu shipped (D248).

    Args:
        name: The checker type.
        stored: Its stored instructions.

    Returns:
        One sentence (`ShippedStatus.sentence`), with `calu checker update NAME` for an older
        shipped text and `calu checker diff NAME` for the user's own; "" for a checker calu
        ships no text for.
    """
    if name not in SHIPPED_TEXTS:
        return ""
    said = shipped_status(name, stored).sentence(
        f"the {name} checker's instructions", f"`calu checker update {name}`"
    )
    if shipped_status(name, stored).kind == "edited":
        said += f" (`calu checker diff {name}` shows the difference)"
    return said


def _edit_checker(args: argparse.Namespace, store: CredentialStore) -> None:
    """Open a checker's instructions in the editor and store what comes back.

    The password is asked once: the instructions are read, the editor runs outside the database
    connection, and the result is stored with a second connection. If storing fails, the
    temporary file stays and its path is printed, so no editing work is lost.

    Args:
        args: Parsed arguments (`name`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the checker is unknown, or no editor is
            available.
        CredentialError: On a wrong password.
        admin.AdminError: If the edited instructions are refused (e.g. empty).
        psycopg.Error: On database errors.
    """
    url = _admin_secret(store, args)
    try:
        admin_url = url.decode()

        async def read(db: Db) -> str:
            """Read the instructions of the checker.

            Args:
                db: The connection.

            Returns:
                The stored instructions.
            """
            return await admin.instructions(db, args.name)

        before = asyncio.run(_with_admin(admin_url, read))
        note = _shipped_note(args.name, before)
        if note:
            print(note, file=sys.stderr)
        after, path = edit_in_editor(before, f"{args.name}-instructions.md")
        if after == before:
            print("unchanged; nothing was stored")
            return

        async def write(db: Db) -> str:
            """Store the edited instructions.

            Args:
                db: The connection.

            Returns:
                The definition after the change.
            """
            row = await admin.set_checker(db, args.name, instructions_text=after)
            return row.line()

        try:
            print(asyncio.run(_with_admin(admin_url, write)))
        except admin.AdminError, psycopg.Error:
            print(f"the edited instructions are kept in {path}", file=sys.stderr)
            raise
    finally:
        wipe(url)


def _edit_rule_list(args: argparse.Namespace, store: CredentialStore) -> None:
    """Open one allow or deny list in the editor and store what comes back.

    The file has one regular expression per line, with a comment block above a pattern becoming
    that pattern's comment — the format `calu setup` loads the built-in lists from. Disabled
    rules are left untouched and are not in the file.

    Args:
        args: Parsed arguments (`target` as the list name, `project`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the list name is missing, or no editor is
            available.
        CredentialError: On a wrong password.
        admin.AdminError: If the list name is unknown or a pattern does not compile.
        psycopg.Error: On database errors.
    """
    if not args.target:
        raise CliError(f"rules edit needs a list name ({', '.join(admin.RULE_LISTS)})")
    scope = args.project or "global"
    url = _admin_secret(store, args)
    try:
        admin_url = url.decode()

        async def read(db: Db) -> str:
            """Render the list as the text to edit.

            Args:
                db: The connection.

            Returns:
                The rules of the list in file form.
            """
            rows = [
                row
                for row in await admin.rules(db, args.project, args.target)
                if row.scope == scope and row.enabled
            ]
            return admin.render_rule_list(rows, args.target, scope)

        before = asyncio.run(_with_admin(admin_url, read))
        after, path = edit_in_editor(before, f"{args.target}.txt")
        if after == before:
            print("unchanged; nothing was stored")
            return

        async def write(db: Db) -> str:
            """Replace the list with the edited rules.

            Args:
                db: The connection.

            Returns:
                The report.
            """
            removed, stored = await admin.replace_rule_list(db, args.target, after, args.project)
            return f"{args.target} ({scope}): {removed} rule(s) replaced by {stored}"

        try:
            print(asyncio.run(_with_admin(admin_url, write)))
        except admin.AdminError, psycopg.Error:
            print(f"the edited list is kept in {path}", file=sys.stderr)
            raise
    finally:
        wipe(url)


TRUSTED_SHOWN = 50
"""How many trusted commits `calu honesty trusted` lists without `--all`."""


def _os_user() -> str:
    """Name the person at the terminal, for the record of a certification.

    Returns:
        The OS user name, or "unknown" if the system does not tell it.
    """
    try:
        return getpass.getuser() or "unknown"
    except OSError, KeyError:
        return "unknown"


def _honesty_trust(args: argparse.Namespace, store: CredentialStore) -> None:
    """Certify a commit or a file content as honest, list the certifications, or remove some.

        calu honesty trust REV [--note TEXT]     REV is a commit, a branch or a tag; a branch
                                                 is resolved to the commit it points at now
        calu honesty trust --file PATH [--note TEXT]
                                                 the SHA-256 of the file's present content
        calu honesty trusted [--all]             the trusted commits and file contents, each
                                                 with the source of its trust
        calu honesty untrust COMMIT              a commit id, its prefix, or a name git
                                                 resolves in the current directory
        calu honesty untrust --file HASH_OR_PATH
        calu honesty prune --days N              remove what calu itself stored (sources
                                                 `check` and `review`) longer than N days ago

    `trust` records the source `user` and the OS user name; it replaces a weaker record
    (`check`, `review`) of the same commit or content. `prune` never removes a `user` record.
    Names are resolved with git in the current directory; the database is not asked whether
    the commit exists anywhere.

    The commands use the `operator` credential, or `admin` if there is none: a `user`
    certification made here rests on the protection of the credential store. `trust`,
    `untrust` and `prune` do not run inside a calu session (`calu.paths.INSIDE_SESSION_ENV`
    is set there): a person runs them in a terminal of their own, or uses `/trust` in a
    client of the session.

    Args:
        args: Parsed arguments (`action`: "trust", "trusted", "untrust" or "prune"; `target`,
            `file`, `note`, `all`, `days`).
        store: The credential store.

    Raises:
        CliError: If a changing action is started from inside a session, neither credential is
            stored, the commit or file is not named or cannot be resolved or read, `--days`
            is missing or negative, or there is nothing to remove.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    here = Path.cwd()
    if args.action != "trusted" and os.environ.get(INSIDE_SESSION_ENV):
        raise CliError(
            f"calu honesty {args.action} does not run inside a calu session; a person runs it "
            "in a terminal of their own, or certifies with /trust in a client of the session"
        )
    if args.action == "trusted":

        async def listing(db: Db) -> str:
            """Render both tables.

            Args:
                db: The connection.

            Returns:
                The trusted commits, newest first, then the trusted file contents (all with
                `--all`, otherwise the newest `TRUSTED_SHOWN` of each).
            """
            commits = await trust.trusted_commits(db, None if args.all else TRUSTED_SHOWN + 1)
            files = await trust.trusted_files(db, None if args.all else TRUSTED_SHOWN + 1)
            shown = None if args.all else TRUSTED_SHOWN
            lines = ["trusted commits (id, certified, source, by, note):"]
            lines += [f"  {row.line()}" for row in commits[:shown]]
            if not commits:
                lines.append("  none; `calu honesty trust <commit-or-branch>` certifies one")
            elif not args.all and len(commits) > TRUSTED_SHOWN:
                lines.append("  ... older ones are left out; --all lists every one")
            if files:
                lines.append("trusted file contents (sha256, certified, source, by, path, note):")
                lines += [f"  {row.line()}" for row in files[:shown]]
                if not args.all and len(files) > TRUSTED_SHOWN:
                    lines.append("  ... older ones are left out; --all lists every one")
            return "\n".join(lines)

        _herd_command(store, listing)
        return
    if args.action == "prune":
        days = args.days
        if days is None or days < 0:
            raise CliError("calu honesty prune needs --days N with N of 0 or more")

        async def pruning(db: Db) -> str:
            """Remove the old `check` and `review` records.

            Args:
                db: The connection.

            Returns:
                The report.
            """
            removed = await trust.prune(db, days)
            return (
                f"{removed} record(s) of the sources check and review older than {days} day(s) "
                "removed; user records are kept"
            )

        _herd_command(store, pruning)
        return
    note = args.note or ""
    if args.file:
        if args.target:
            raise CliError("name a commit or --file, not both")
        named = Path(os.path.abspath(args.file))
        if args.action == "trust":
            ids = content_ids(named)
            if ids is None:
                raise CliError(f"{args.file} is not a file that can be read")
            sha256 = ids.sha256

            async def trust_file(db: Db) -> str:
                """Record the file's content hash.

                Args:
                    db: The connection.

                Returns:
                    The report.
                """
                added = await trust.trust_file(db, sha256, str(named), "user", _os_user(), note)
                state = "trusted" if added else "was trusted by a user already"
                return f"{named} with its present content (sha256 {sha256[:12]}): {state}"

            _herd_command(store, trust_file)
            return

        async def untrust_file(db: Db) -> str:
            """Remove the rows with that hash or path.

            Args:
                db: The connection.

            Returns:
                The report.

            Raises:
                CliError: If no row has that hash or path.
            """
            removed = await trust.untrust_file(db, args.file) or await trust.untrust_file(
                db, str(named)
            )
            if not removed:
                raise CliError(f"no trusted file content has the hash or the path {args.file}")
            return f"{removed} trusted file content(s) removed"

        _herd_command(store, untrust_file)
        return
    if not args.target:
        raise CliError(f"calu honesty {args.action} needs a commit or a branch (or --file PATH)")
    if args.action == "trust":
        try:
            commit = resolve_commit(here, args.target)
        except GitError as exc:
            raise CliError(str(exc)) from exc

        async def certify(db: Db) -> str:
            """Record the commit as trusted.

            Args:
                db: The connection.

            Returns:
                The report.
            """
            added = await trust.certify_commit(
                db, commit.commit_id, commit.tree_id, "user", _os_user(), note
            )
            state = "trusted" if added else "was trusted by a user already"
            return f"commit {commit.short} ({args.target}): {state}"

        _herd_command(store, certify)
        return

    async def untrust(db: Db) -> str:
        """Remove the commit from the trusted ones.

        Args:
            db: The connection.

        Returns:
            The report.

        Raises:
            CliError: If the name fits no trusted commit, or more than one.
        """
        try:
            ids = [resolve_commit(here, args.target).commit_id]
        except GitError:
            ids = await trust.commits_with_prefix(db, args.target)
        if len(ids) > 1:
            raise CliError(f"{args.target} fits {len(ids)} trusted commits; write more of the id")
        if not ids or not await trust.untrust_commit(db, ids[0]):
            raise CliError(f"{args.target} is not a trusted commit")
        return f"commit {ids[0][:12]}: no longer trusted"

    _herd_command(store, untrust)


def cmd_honesty(args: argparse.Namespace, store: CredentialStore) -> None:
    """Show, edit or reset the honesty rules, or manage the trusted commits.

    `show`, `edit` and `reset` are about the honesty rules the change checkers read
    (`honesty.rules`, D181). The rules in force for a scope are the project's `honesty.rules`
    if set, else the global one, else the text shipped with calu. `show` prints them; `edit`
    opens them in `$EDITOR` and stores the result in the scope (`--project` or global);
    `reset` removes the scope's value, so the next scope down (or the shipped text) applies
    again. `trust`, `trusted`, `untrust` and `prune` are described at `_honesty_trust`.

    Args:
        args: Parsed arguments (`action`: "show", "edit", "reset", "trust", "trusted",
            "untrust" or "prune"; `project`; and what `_honesty_trust` reads).
        store: The credential store.

    Raises:
        CliError: If the credential the action needs is missing, no editor is available, or
            `_honesty_trust` reports one.
        CredentialError: On a wrong password.
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    if args.action in ("trust", "trusted", "untrust", "prune"):
        _honesty_trust(args, store)
        return
    _text_setting(args, store, "honesty.rules", honesty_rules, "honesty-rules.md", "rules")


def _stored_at(values: Mapping[str, Any], key: str) -> Any:
    """Return what a scope's stored settings hold at a dotted key, without defaults.

    Args:
        values: The stored settings of one scope (`admin.settings`).
        key: A dotted key, e.g. "honesty.rules".

    Returns:
        The stored value, or None if the scope stores none there.
    """
    node: Any = values
    for part in key.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _text_setting(
    args: argparse.Namespace,
    store: CredentialStore,
    key: str,
    shipped: Callable[[], str],
    file_name: str,
    noun: str,
) -> None:
    """Show, edit or reset a Markdown setting that has a shipped default (D181, D245).

    The text in force for a scope is the project's value if set, else the global one, else
    `shipped()`. `show` prints it; `edit` opens it in `$EDITOR` and stores the result in the
    scope (`--project` or global); `reset` removes the scope's value, so the next scope down
    (or the shipped text) applies again.

    A save is conflict-safe: after the editor closes, the scope's stored value is read again,
    and if somebody changed it meanwhile (another terminal, the web interface, an editor
    extension) nothing is stored and the edited text stays in its file.

    Args:
        args: Parsed arguments (`action`: "show", "edit" or "reset"; `project`).
        store: The credential store.
        key: The dotted configuration key, e.g. "traces.redactor_instructions".
        shipped: Returns the text calu ships.
        file_name: The name of the file the editor opens, e.g. "honesty-rules.md".
        noun: What the text is called in the reports, e.g. "rules" or "instructions".

    Raises:
        CliError: If the credential the action needs is missing, no editor is available, or
            the stored value changed while the editor was open.
        CredentialError: On a wrong password.
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    url = _admin_secret(store, args)
    scope = args.project or "global"
    try:
        admin_url = url.decode()

        async def read(db: Db) -> tuple[str, Any]:
            """Return the text in force for the scope and the value the scope stores itself.

            Args:
                db: The connection.

            Returns:
                (the configured text of the most specific scope or the shipped text, the
                scope's own stored value or None).
            """
            own = _stored_at(await admin.settings(db, args.project), key)
            if own:
                return str(own), own
            if args.project:
                above = _stored_at(await admin.settings(db, None), key)
                if above:
                    return str(above), own
            return shipped(), own

        if args.action == "reset":

            async def reset(db: Db) -> str:
                """Remove the scope's value.

                Args:
                    db: The connection.

                Returns:
                    The report.
                """
                await admin.unset_setting(db, key, args.project)
                return f"{key} ({scope}) removed"

            print(asyncio.run(_with_admin(admin_url, reset)))
            return
        before, own_before = asyncio.run(_with_admin_value(admin_url, read))
        if own_before:
            # The scope stores its own copy: say how it relates to the shipped texts (D248),
            # on stderr, so that `show > file` keeps the text alone.
            state = shipped_status(admin.SHIPPED_SETTINGS[key], str(own_before))
            command = "calu honesty" if key == "honesty.rules" else "calu traces instructions"
            project = f" --project {args.project}" if args.project else ""
            said = state.sentence(f"{key} ({scope})", f"`{command} reset{project}`")
            if state.kind != "current":
                said += f"\n{state.diff(str(own_before))}"
            print(said, file=sys.stderr)
        if args.action == "show":
            print(before, end="" if before.endswith("\n") else "\n")
            return
        after, path = edit_in_editor(before, file_name)
        if after == before:
            print("unchanged; nothing was stored")
            return

        async def write(db: Db) -> str:
            """Store the edited text in the scope unless the scope changed meanwhile.

            Args:
                db: The connection.

            Returns:
                The report.

            Raises:
                CliError: If the scope's stored value is no longer the one the edit started
                    from.
            """
            _, own_now = await read(db)
            if own_now != own_before:
                raise CliError(
                    f"{key} ({scope}) was changed by someone else while the editor was open; "
                    f"nothing was stored. Compare your text with the stored {noun} and edit "
                    "again"
                )
            await admin.set_setting(db, key, after, args.project)
            return f"{key} ({scope}) stored"

        try:
            print(asyncio.run(_with_admin(admin_url, write)))
        except CliError, admin.AdminError, psycopg.Error:
            print(f"the edited {noun} are kept in {path}", file=sys.stderr)
            raise
    finally:
        wipe(url)


def cmd_traces(args: argparse.Namespace, store: CredentialStore) -> None:
    """Session traces: the redaction agent's instructions, the final pass and its hook.

    `calu traces finalize [--staged]` runs the final redaction pass (`_traces_finalize`,
    D246); `calu traces hook install|remove` the pre-commit hook (`_traces_hook`). For the
    instructions (D245):

    `calu traces instructions show|edit|reset [--project NAME]` works on
    `traces.redactor_instructions` as `calu honesty show|edit|reset` works on the honesty
    rules (`_text_setting`): the text in force is the project's, else the global one, else
    the shipped `trace-redaction.md`. calu puts its fixed part in front of it; `show` prints
    only the editable text.

    Args:
        args: Parsed arguments (`subject`: "instructions", "finalize" or "hook"; `action`:
            "show", "edit" or "reset" for the instructions, "install" or "remove" for the
            hook, none for finalize; `project`, `staged`, `directory`, `chain`).
        store: The credential store.

    Raises:
        CliError: If the action word does not fit the subject, the credential the action
            needs is missing, no editor is available, the stored value changed while the
            editor was open, or the final pass or the hook could not be done.
        CredentialError: On a wrong password.
        admin.AdminError: If the project does not exist.
        psycopg.Error: On database errors.
    """
    if args.subject == "finalize":
        if args.action is not None:
            raise CliError("`calu traces finalize` takes no action word")
        _traces_finalize(args, store)
        return
    if args.subject == "hook":
        if args.action not in ("install", "remove"):
            raise CliError("`calu traces hook` needs `install` or `remove`")
        _traces_hook(args)
        return
    if args.action not in ("show", "edit", "reset"):
        raise CliError("`calu traces instructions` needs `show`, `edit` or `reset`")
    _text_setting(
        args,
        store,
        "traces.redactor_instructions",
        default_redaction_instructions,
        "trace-redaction.md",
        "instructions",
    )


FINALIZE_REFUSED = (
    "the final redaction pass of the session traces could not run ({problem}), so the trace "
    "files staged for this commit are not finalized; the commit is refused. Run `calu traces "
    "finalize --staged` in a terminal (it asks for a password where one is needed) and commit "
    "again. `git commit --no-verify` commits the trace files as the per-block redaction wrote "
    "them, without the final pass."
)
"""What `calu traces finalize --staged` (the pre-commit hook) says when the pass cannot run
(D246)."""

FINALIZE_FAILED = (
    "the final redaction pass of the session traces could not run ({problem}); the trace "
    "files that failed are left as they are"
)
"""What `calu traces finalize` without `--staged` says when the pass cannot run."""


def _traces_finalize(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the final redaction pass of the session traces (`calu traces finalize`, D246).

    Without `--staged`: the trace files below `traces.directory` that differ from HEAD are
    finalized on disk (not staged). With `--staged` (the pre-commit hook): the trace files
    the index adds or changes are finalized in the index, and on disk where the file holds
    the staged text. Settings and credentials are read only if a file has something to
    redact, so a commit without a new trace part needs neither. The settings are those of the
    project of the directory; in a git worktree outside the project, those of the project of
    its main checkout (`settings_directory`).

        nothing to finalize                 -> nothing printed, exit 0
        `traces.final_redaction` off        -> one line, exit 0, files unchanged
        no settings (no login, no password
        without a terminal, no database)    -> CliError: `FINALIZE_REFUSED` (`--staged`) or
                                               `FINALIZE_FAILED`
        the agent fails for a file          -> CliError, as above; the other files are
                                               finalized

    Args:
        args: Parsed arguments (`staged`, `directory`, `project`).
        store: The credential store.

    Raises:
        CliError: If the directory is in no git work tree, or the pass could not finish (the
            message says what to do; as the pre-commit hook this refuses the commit).
    """
    from dotenv import dotenv_values

    from calu.config import PROVIDER_KEYS_BY_PREFIX, default_model
    from calu.defaults.instructions import final_redaction_instructions
    from calu.models import resolve_model
    from calu.store.config import ConfigStore
    from calu.store.projects import project_for_directory
    from calu.traces.finalize import (
        FinalizeError,
        final_redactor,
        finalize,
        plans,
        repository_of,
        settings_directory,
    )
    from calu.traces.redact import Secrets

    workspace = Path(args.directory).resolve()
    repo = repository_of(workspace)
    if repo is None:
        raise CliError(f"{workspace} is in no git work tree")
    refused = FINALIZE_REFUSED if args.staged else FINALIZE_FAILED
    found = None
    if args.staged:
        try:
            found = plans(repo, "staged")
        except FinalizeError as exc:
            raise CliError(refused.format(problem=exc)) from exc
        if not found:
            return

    async def read(db: Db) -> Config:
        """Load the settings that apply to the project directory.

        Args:
            db: The connection.

        Returns:
            The configuration.
        """
        # A worktree outside the project takes its main checkout's settings (D251).
        home = settings_directory(workspace, repo)
        project = args.project or await project_for_directory(db, home, socket.gethostname())
        return (await ConfigStore(db, home, project).load()).config

    try:
        config = _herd_value(store, read, may_ask=sys.stdin.isatty())
    except (CliError, CredentialError, psycopg.Error, OSError, ValueError) as exc:
        problem = (
            "a password is needed and there is no terminal to ask for it"
            if isinstance(exc, NeedsPassword)
            else f"the settings could not be read: {exc}"
        )
        raise CliError(refused.format(problem=problem)) from exc
    cfg = config.traces
    if not cfg.final_redaction:
        print("traces.final_redaction is off: the trace files are left as they are")
        return
    try:
        if found is None:
            relative = (workspace / cfg.directory).resolve().relative_to(repo.resolve())
            found = plans(repo, "worktree", relative.as_posix())
    except (FinalizeError, ValueError) as exc:
        raise CliError(refused.format(problem=exc)) from exc
    if not found:
        print("nothing to finalize: no trace file has a part that is not committed")
        return
    redactor = None
    if cfg.redact:
        model = cfg.redactor_model.strip() or default_model(config.claude.use_bedrock)
        redactor = final_redactor(
            final_redaction_instructions(cfg.redactor_instructions),
            resolve_model(model),
            cfg.final_timeout_s,
        )
    names = {
        *PROVIDER_KEYS,
        *(name for names in PROVIDER_KEYS_BY_PREFIX.values() for name in names),
        *config.hidden_env,
    }
    if user_env_file().is_file():
        names |= set(dotenv_values(user_env_file()))
    secrets = Secrets.from_environment(names)
    mode = "staged" if args.staged else "worktree"
    outcome = asyncio.run(finalize(repo, mode, redactor, secrets, found=found))
    summary = outcome.summary()
    if summary:
        print(summary, file=sys.stderr if outcome.failures else sys.stdout)
    if outcome.failures:
        problem = "; ".join(why for _path, why in outcome.failures)
        raise CliError(refused.format(problem=problem))


def _traces_hook(args: argparse.Namespace) -> None:
    """Install or remove the pre-commit hook of the session traces (`calu traces hook`, D246).

    Args:
        args: Parsed arguments (`action`: "install" or "remove"; `directory`; `chain`).

    Raises:
        CliError: If the directory is in no git work tree, or the hook cannot be installed or
            removed (another hook is there, a file cannot be written).
    """
    from calu.traces.finalize import repository_of
    from calu.traces.hook import HookError, install, remove

    workspace = Path(args.directory).resolve()
    repo = repository_of(workspace)
    if repo is None:
        raise CliError(f"{workspace} is in no git work tree")
    calu = shutil.which("calu") or str(Path(sys.argv[0]).resolve())
    try:
        print(install(repo, calu, args.chain) if args.action == "install" else remove(repo))
    except HookError as exc:
        raise CliError(str(exc)) from exc


def cmd_rules(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the allow and deny lists, add, remove, enable or disable a rule, or add a rule set.

    `target` is the list name for `list` and `add`, the rule id for `remove`, `enable` and
    `disable`, and the set name for `add-set` (`admin.RULE_SETS`, D143).

    Args:
        args: Parsed arguments (`action`, `target`, `pattern`, `comment`, `project`,
            `position`).
        store: The credential store.

    Raises:
        CliError: If an argument is missing or malformed, if there is no `admin` credential, or
            if the rule does not exist.
        CredentialError: On a wrong password.
        admin.AdminError: If the list name or the pattern is refused.
        psycopg.Error: On database errors.
    """
    if args.action == "edit":
        _edit_rule_list(args, store)
        return
    rule_id: int | None = None
    if args.action in ("remove", "enable", "disable"):
        if not args.target:
            raise CliError(f"rules {args.action} needs a rule id (see `calu rules list`)")
        try:
            rule_id = int(args.target)
        except ValueError as exc:
            raise CliError(f"not a rule id: {args.target!r}") from exc
    if args.action == "add" and not (args.target and args.pattern):
        raise CliError("rules add needs a list name and a pattern")
    if args.action == "add-set" and not args.target:
        raise CliError(f"rules add-set needs a set name: {', '.join(admin.RULE_SETS)}")

    async def work(db: Db) -> str:
        """Run the requested rules action.

        Args:
            db: The connection.

        Returns:
            The report.

        Raises:
            CliError: If the rule does not exist.
        """
        if args.action == "list":
            rows = await admin.rules(db, args.project, args.target)
            return "\n".join(row.line() for row in rows) or "no rules"
        if args.action == "add":
            row = await admin.add_rule(
                db, args.target, args.pattern, args.comment or "", args.project, args.position
            )
            return f"added rule {row.id}: {row.line()}"
        if args.action == "add-set":
            added, skipped = await admin.add_rule_set(db, args.target, args.project)
            lines = [f"added rule {row.id}: {row.line()}" for row in added]
            if skipped:
                lines.append(f"{len(skipped)} pattern(s) were already in the list")
            lines.append("remove the ones you do not want with `calu rules remove ID`")
            return "\n".join(lines)
        assert rule_id is not None
        if args.action == "remove":
            if not await admin.remove_rule(db, rule_id):
                raise CliError(f"no rule {rule_id}")
            return f"removed rule {rule_id}"
        if not await admin.set_rule(db, rule_id, args.action == "enable"):
            raise CliError(f"no rule {rule_id}")
        return f"{args.action}d rule {rule_id}"

    _admin_command(store, work, args)


def cmd_lightcycles(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve MetaTron for the browser, an easter egg (`calu.lightcycles`).

    Needs no database, no credential and no configuration. The server and the game are
    imported here and not at the top of this module, so no other command pays for them.

    Side effects: binds a port on 127.0.0.1, prints the address, opens the default browser
    unless `--no-browser` is given, and serves until Ctrl-C.

    Args:
        args: Parsed arguments (`port`, `no_browser`, `players`, `bots`, `speed`, `seed`).
        store: The credential store (unused).

    Raises:
        CliError: If an option is out of range, or the players and computer players
            together are more than ten.
        OSError: If the asked port cannot be bound.
    """
    from calu.lightcycles.server import serve

    limits = {"players": (0, 10), "bots": (0, 10), "speed": (1, 9), "seed": (0, 2**32 - 1)}
    options: dict[str, int] = {}
    for name, (low, high) in limits.items():
        value = getattr(args, name)
        if value is None:
            continue
        if not low <= value <= high:
            raise CliError(f"--{name} takes a number from {low} to {high}, not {value}")
        options[name] = value
    if options.get("players", 1 if "bots" in options else 0) + options.get("bots", 0) > 10:
        raise CliError("MetaTron has ten players: --players and --bots together at most 10")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise CliError(f"--port takes a number from 1 to 65535, not {args.port}")
    serve(port=args.port, browser=not args.no_browser, options=options)


def _database_option(parser: argparse.ArgumentParser) -> None:
    """Add `--database admin|operator` to a command that changes settings.

    Args:
        parser: The command's parser.
    """
    parser.add_argument(
        "--database",
        choices=["admin", "operator"],
        help="which stored credential names the database to work on, for a store that "
        "holds both `admin` and `operator` and they name different databases (with "
        "`operator`, the admin password of that database is asked)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser of `calu`.

    Returns:
        The parser; the selected command's handler is in the `handler` attribute.
    """
    parser = argparse.ArgumentParser(prog="calu", description="calu: Claude Code under checks")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="local database and migrations").add_subparsers(
        dest="db_command", required=True
    )
    p = db.add_parser("up", help="start the local PostgreSQL in Docker")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"host port of a new local database (default: {local_db.DEFAULT_PORT}); an "
        "existing one keeps the port it was set up with",
    )
    p.add_argument(
        "--data-dir",
        metavar="DIR",
        help="directory for the files of a new local database (default: ~/.local/state/calu/db)",
    )
    p.set_defaults(handler=cmd_db_up)
    db.add_parser("down", help="stop the local PostgreSQL").set_defaults(handler=cmd_db_down)
    db.add_parser("migrate", help="apply schema migrations").set_defaults(handler=cmd_db_migrate)
    db.add_parser(
        "reset",
        help="delete the whole database (sessions, rules, instructions, settings) and create "
        "it again, empty; asks for the admin password and a typed confirmation",
    ).set_defaults(handler=cmd_db_reset)

    p = sub.add_parser(
        "setup",
        help="set the database up: choose it on the first run (a local one in Docker, an "
        "existing server, or RDS with IAM authentication), then migrate and create the "
        "session login",
    )
    p.add_argument(
        "--database",
        choices=VARIANTS,
        help="which database to set up, instead of the question: docker (created and started "
        "by calu), server (an existing PostgreSQL) or rds-iam (AWS RDS, IAM authentication)",
    )
    p.add_argument("--data-dir", metavar="DIR", help="docker: directory for the database's files")
    p.add_argument("--port", type=int, help="docker: port on 127.0.0.1; else the server's port")
    p.add_argument("--host", help="server, rds-iam: host name of the server")
    p.add_argument("--dbname", help="server, rds-iam: the database (default: calu)")
    p.add_argument("--user", help="server, rds-iam: the role that owns calu's database")
    p.add_argument(
        "--sslmode",
        choices=("disable", "allow", "prefer", "require", "verify-ca", "verify-full"),
        help="server: libpq sslmode; left out, a remote host gets `require`",
    )
    p.add_argument(
        "--sslrootcert",
        metavar="FILE",
        help="server, rds-iam: the CA file the server's certificate is verified with; it is "
        "copied next to the credential store",
    )
    p.add_argument("--region", help="rds-iam: AWS region, if the host name does not show it")
    p.add_argument("--aws-profile", help="rds-iam: the AWS profile that may connect as --user")
    p.add_argument(
        "--session-auth",
        choices=("iam", "password"),
        default=None,
        help="rds-iam: how sessions log in as calu_session: with IAM tokens (the default; "
        "calu keeps no database password then) or with a generated password",
    )
    p.add_argument(
        "--session-aws-profile",
        help="rds-iam: the AWS profile of the session login, if it differs from --aws-profile",
    )
    p.add_argument(
        "--renew-session",
        action="store_true",
        help="give calu_session a new login although one exists; sessions and machines that "
        "use the old one stop working",
    )
    p.add_argument(
        "--no-password",
        action="store_true",
        help="ask for no admin and no session password: the logins are stored protected by "
        "the store file's mode alone, and the login that changes settings is written to "
        "/var/lib/calu/database.json, which every user and program of the machine can read "
        "(Claude Code's commands included), so that no client asks for a password. Weak "
        "security by choice; `calu setup` without the option takes it back. An existing "
        "setup is converted",
    )
    p.add_argument(
        "--set-passwords",
        action="store_true",
        help="choose a new admin and a new session password for the logins that are stored "
        "(their old passwords are asked first, if they have any): the way back from "
        "--no-password, and the way to change a password. The role that changes settings "
        "gets the new admin password",
    )
    p.add_argument(
        "--update-instructions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="replace stored checker instructions that are an older shipped version, "
        "unchanged, with the present one (or keep them), instead of the question; without a "
        "terminal they are kept and listed unless this option is given. A text you edited is "
        "never replaced",
    )
    p.add_argument(
        "--sandbox",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="switch Claude Code's sandbox for Claude's commands on (needs bubblewrap and "
        "socat) or off (claude.sandbox.enabled), instead of the question; left out without a "
        "terminal, the stored value stays",
    )
    p.add_argument(
        "--checker-model",
        default=None,
        help="model of every checker, the narrator and the summarizer; left out, they use the "
        "default (Haiku on the Anthropic API, or through AWS Bedrock in Bedrock mode); 'auto' "
        "chooses from the credentials found: AWS Bedrock, then ANTHROPIC_API_KEY, "
        "FIREWORKS_API_KEY, MISTRAL_API_KEY",
    )
    p.set_defaults(handler=cmd_setup)

    p = sub.add_parser("run", help="start a checked Claude Code session")
    p.add_argument("directory", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--project", help="name of the project row whose settings and rules apply")
    p.add_argument("-p", "--prompt", help="first message for Claude")
    p.add_argument("--prompt-file", help="read the first message from this file")
    p.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Claude session id to resume (`calu list-sessions` shows those of this directory)",
    )
    p.add_argument(
        "--fork",
        metavar="SESSION_ID",
        help="start a copy of that conversation under a new session id and continue the copy;"
        " the original stays as it is. Not together with --resume",
    )
    p.add_argument(
        "--fork-before-turn",
        type=int,
        metavar="N",
        help="with --resume: branch the conversation off before turn N instead of continuing it;"
        " the fork gets its own session id (`calu sessions` and the web interface show the"
        " turns). The working tree is left alone: the files keep what the dropped turn wrote"
        " (D46)",
    )
    p.add_argument(
        "--socket",
        nargs="?",
        const="",
        metavar="PATH",
        help="serve the clients on a Unix socket instead of a local TCP port; without a PATH,"
        " $XDG_RUNTIME_DIR/calu/<pid>.sock. The file system then decides who may connect",
    )
    p.add_argument("--model", help="Claude model alias or id (overrides the configuration)")
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max", "default"],
        help="reasoning effort (overrides the configuration)",
    )
    p.add_argument(
        "--batch",
        action="store_true",
        help="end the session when Claude is done, instead of reading messages from stdin",
    )
    p.add_argument(
        "--background-timeout",
        type=float,
        metavar="SECONDS",
        help="with --batch: end the run after Claude has been idle this long while background "
        "tasks (a dev server, a Monitor, a workflow) still run; default: wait for them",
    )
    p.add_argument(
        "--user",
        metavar="NAME|UID[:GID]",
        help="as root: the user Claude Code runs as, and whose rights calu's own work in the "
        "project has. A number is a uid without a name or home directory on this machine "
        "(calu gives Claude Code a state directory); root itself only when named",
    )
    p.add_argument(
        "--sudo",
        action="store_true",
        help="started as an ordinary user who has sudo: run calu again as root through sudo, "
        "with Claude Code as you. Needs an installation only root can change",
    )
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--secure", action="store_true", help=SECURE_HELP)
    p.add_argument("--insecure", action="store_true", help=INSECURE_HELP)
    p.set_defaults(handler=cmd_run, log_name="run")

    p = sub.add_parser(
        "serve",
        help="one process, authenticated once, that starts sessions when `calu start` asks",
    )
    p.add_argument(
        "--projects",
        metavar="DIR",
        help="the projects directory: sessions start only in it or below it (default: the "
        "current directory). Refused: /, hidden directories and system directories",
    )
    p.add_argument(
        "--max-sessions",
        type=int,
        default=10,
        metavar="N",
        help="how many sessions run at once; a request beyond that is refused (default: 10)",
    )
    p.add_argument(
        "--idle-timeout",
        type=float,
        metavar="MINUTES",
        help="end a session that has been idle this long with no client attached (default: "
        "never); `calu start --resume` continues its conversation",
    )
    p.add_argument(
        "--user",
        metavar="NAME|UID[:GID]",
        help="as root: the user Claude Code runs as, and whose rights calu's own work in the "
        "project has. A number is a uid without a name or home directory on this machine "
        "(calu gives Claude Code a state directory); root itself only when named",
    )
    p.add_argument(
        "--sudo",
        action="store_true",
        help="started as an ordinary user who has sudo: run calu again as root through sudo, "
        "with Claude Code as you. Needs an installation only root can change",
    )
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--secure", action="store_true", help=SECURE_HELP)
    p.add_argument("--insecure", action="store_true", help=INSECURE_HELP)
    relay_choice = p.add_mutually_exclusive_group()
    relay_choice.add_argument(
        "--relay-port",
        type=int,
        metavar="N",
        help="the port on 127.0.0.1 of the relay for every session of this server (default: "
        f"one of {projects_dir.RELAY_PORT_FIRST}-{projects_dir.RELAY_PORT_LAST} derived from the "
        "projects directory, the same after every restart, or the next free one if that is "
        "taken; 0: any free port). A client on another machine needs one SSH forward of it and "
        "a token of `calu token mint --server`",
    )
    relay_choice.add_argument(
        "--no-relay", action="store_true", help="no relay: each session only on its own port"
    )
    p.set_defaults(handler=cmd_serve, log_name="serve")

    p = sub.add_parser("start", help="ask the running `calu serve` for a session and attach to it")
    p.add_argument("directory", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--project", help="name of the project row whose settings and rules apply")
    p.add_argument("-p", "--prompt", help="first message for Claude")
    p.add_argument("--prompt-file", help="read the first message from this file")
    p.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Claude session id to resume (`calu list-sessions` shows those of this directory)",
    )
    p.add_argument(
        "--fork",
        metavar="SESSION_ID",
        help="start a copy of that conversation under a new session id and continue the copy;"
        " the original stays as it is. Not together with --resume",
    )
    p.add_argument(
        "--fork-before-turn",
        type=int,
        metavar="N",
        help="with --resume: branch the conversation off before turn N instead of continuing it",
    )
    p.add_argument("--model", help="Claude model alias or id (overrides the configuration)")
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max", "default"],
        help="reasoning effort (overrides the configuration)",
    )
    p.add_argument(
        "--no-attach",
        action="store_true",
        help="only start the session and print its ids; attach later with `calu attach`",
    )
    p.add_argument("--thinking", action="store_true", help="also print Claude's thinking")
    p.add_argument(
        "--plain",
        action="store_true",
        help="the line client instead of the full-screen one (a pipe always gets the line client)",
    )
    p.add_argument(
        "--sudo",
        action="store_true",
        help="run this client as root through sudo, for you: it reads calu's credentials in "
        "/etc/calu (it asks for a calu password only if they need one), and nothing Claude "
        "Code runs can reach it",
    )
    p.add_argument("--user", metavar="NAME|UID[:GID]", help="as root: the user to work for")
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--relay",
        metavar="ADDRESS",
        help="ask the `calu serve` behind this relay address (e.g. a forwarded "
        "ws://127.0.0.1:<port>) instead of the database; DIRECTORY is then an absolute path "
        "on the server's machine, inside the server's projects directory",
    )
    p.add_argument(
        "--token",
        help="with --relay: the server token (`calu token mint --server`); else CALU_TOKEN, or "
        "the client asks for it",
    )
    p.set_defaults(handler=cmd_start)

    p = sub.add_parser("attach", help="terminal client for a running session")
    p.add_argument(
        "address",
        nargs="?",
        help="WebSocket address that `calu run` printed; left out, the session running in this "
        "directory is used",
    )
    p.add_argument(
        "--token",
        help="client token that `calu run` printed, for an address; without it the client "
        "tries without one, which a session started with --insecure accepts",
    )
    p.add_argument("--session", help="session id, when several run in this directory")
    p.add_argument(
        "--after-seq", type=int, default=0, help="continue after this event sequence number"
    )
    p.add_argument("--thinking", action="store_true", help="also print Claude's thinking")
    p.add_argument(
        "--plain",
        action="store_true",
        help="the line client instead of the full-screen one (a pipe always gets the line client)",
    )
    p.add_argument(
        "--sudo",
        action="store_true",
        help="run this client as root through sudo, for you: it reads calu's credentials in "
        "/etc/calu (it asks for a calu password only if they need one), and nothing Claude "
        "Code runs can reach it",
    )
    p.add_argument("--user", metavar="NAME|UID[:GID]", help="as root: the user to work for")
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_attach)

    p = sub.add_parser(
        "login",
        help="log Claude Code in for the user it runs as in sessions (Claude Code's own login)",
    )
    p.add_argument(
        "method",
        nargs="?",
        choices=["claudeai", "console", "sso"],
        help="claudeai: a Claude subscription (Claude Code's default); console: Anthropic "
        "Console, billed per use; sso: your organisation's single sign-on",
    )
    p.add_argument("--status", action="store_true", help="only show whether it is logged in")
    p.add_argument("--logout", action="store_true", help="log it out")
    p.add_argument(
        "--sudo",
        action="store_true",
        help="for the sessions calu runs as root for you (`calu run --sudo`): the login is "
        "stored where Claude Code finds it then",
    )
    p.add_argument(
        "--user",
        metavar="NAME|UID[:GID]",
        help="as root: the user to log in for; a number is a uid whose login calu keeps in "
        "/var/lib/calu/claude/<uid>",
    )
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_login)

    p = sub.add_parser("acp", help="serve a running session to an ACP editor (stdin/stdout)")
    p.add_argument(
        "address",
        nargs="?",
        help="address of the session, e.g. ws://127.0.0.1:8765; left out, the session running "
        "in this directory is used, which is what an editor is configured with",
    )
    p.add_argument(
        "--token",
        help="client token that `calu run` printed, for an address; without it the client "
        "tries without one, which a session started with --insecure accepts",
    )
    p.add_argument("--session", help="session id, when several run in this directory")
    p.set_defaults(handler=cmd_acp)

    p = sub.add_parser(
        "ide", help="serve an editor extension, e.g. VSCode's (JSON-RPC on stdin/stdout)"
    )
    p.add_argument(
        "--cwd",
        metavar="DIR",
        help="work in DIR instead of the current directory (for an editor that starts "
        "the bridge over SSH); a DIR that does not exist ends the command with exit code 2",
    )
    p.set_defaults(handler=cmd_ide, log_name="ide")

    p = sub.add_parser("web", help="the herd overview in a browser (127.0.0.1 only)")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (loopback only)")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument(
        "--sudo",
        action="store_true",
        help="run the web interface as root through sudo, for you: it reads calu's "
        "credentials in /etc/calu, and nothing Claude Code runs can reach it",
    )
    p.add_argument("--user", metavar="NAME|UID[:GID]", help="as root: the user to work for")
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    _database_option(p)
    p.set_defaults(handler=cmd_web)

    p = sub.add_parser("protocol", help="JSON Schema of the client protocol")
    p.add_argument("--out", help="write it to this file instead of stdout")
    p.set_defaults(handler=cmd_protocol)

    p = sub.add_parser("sessions", help="list the sessions of all hosts")
    p.add_argument("--all", action="store_true", help="also show sessions that have ended")
    p.set_defaults(handler=cmd_sessions)

    p = sub.add_parser(
        "list-sessions",
        help="the last Claude sessions of this directory, to resume or fork",
    )
    p.add_argument(
        "count",
        nargs="?",
        type=int,
        default=5,
        metavar="N",
        help="how many sessions to list, the freshest first (default: 5)",
    )
    p.set_defaults(handler=cmd_list_sessions)

    p = sub.add_parser("escalations", help="list the open escalations")
    p.add_argument("session", nargs="?", help="only escalations of this session id")
    p.add_argument("--rendered", action="store_true", help="also print the rendered step")
    p.set_defaults(handler=cmd_escalations)

    p = sub.add_parser("doctor", help="report what is in place and what a session would miss")
    p.add_argument("directory", nargs="?", help="also check this workspace")
    p.add_argument("--db", action="store_true", help="also ask the database (needs the password)")
    p.add_argument(
        "--keys",
        action="store_true",
        help="also ask each provider whether its key is accepted (a free model-list request)",
    )
    p.add_argument(
        "--user",
        metavar="NAME|UID[:GID]",
        help="as root: also check what Claude Code would find as this user (its home "
        "directory, its settings, the sandbox, calu's installation)",
    )
    p.add_argument(
        "--sudo",
        action="store_true",
        help="run the report as root through sudo, for you: what `calu run --sudo` would find",
    )
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_doctor)

    p = sub.add_parser("summary", help="show or correct the chat summary the checkers read")
    p.add_argument("session", help="id of the session (`calu sessions`)")
    p.add_argument("--edit", action="store_true", help="edit it in $EDITOR and store it")
    _database_option(p)
    p.set_defaults(handler=cmd_summary)

    p = sub.add_parser("escalation", help="show one escalation with everything needed to decide it")
    p.add_argument("id", type=int)
    p.add_argument("--full", action="store_true", help="do not cut the long sections")
    p.set_defaults(handler=cmd_escalation)

    p = sub.add_parser("resolve", help="allow, deny or answer an open escalation")
    p.add_argument("id", type=int, help="escalation id")
    p.add_argument("decision", choices=["allow", "deny", "answer", "cancel"])
    p.add_argument(
        "text",
        nargs="?",
        help="the note, or the answer for `answer` (the same as --note, as one argument)",
    )
    p.add_argument("--note", help="text for Claude and for the record")
    p.set_defaults(handler=cmd_resolve)

    token = sub.add_parser(
        "token", help="client tokens of a session or of a `calu serve`: mint, list, revoke"
    )
    token.add_argument("action", choices=["mint", "list", "revoke"])
    token.add_argument(
        "session",
        nargs="?",
        help="session id for mint and list (`calu sessions`), token id for revoke; with "
        "--server: the server's id for mint (default: the running server of this user here), "
        "the server token's id for revoke",
    )
    token.add_argument("--label", default="", help='what the token is for, e.g. "zed on laptop"')
    token.add_argument(
        "--server",
        action="store_true",
        help="a server token: every session of one `calu serve`, through its relay port",
    )
    token.set_defaults(handler=cmd_token)

    p = sub.add_parser("evals", help="measure how well the configured checkers decide")
    p.add_argument("files", nargs="*", help="further YAML case files")
    p.add_argument("--directory", default=".", help="project directory whose settings apply")
    p.add_argument("--project", help="name of the project row whose settings apply")
    p.add_argument(
        "--from-db", action="store_true", help="also use the resolved escalations as cases"
    )
    p.add_argument("--no-builtin", action="store_true", help="leave out the built-in cases")
    p.add_argument("--model", help="one model for every checker, instead of the configured ones")
    p.add_argument("--repeat", type=int, default=1, help="how often each case runs")
    p.add_argument(
        "--list",
        action="store_true",
        dest="list_cases",
        help="print the collected cases instead of running them (no model is called)",
    )
    p.add_argument(
        "--export",
        metavar="FILE",
        help="write the collected cases as YAML instead of running them ('-' for stdout);"
        " `calu evals FILE` runs the result later as a fixed set",
    )
    p.set_defaults(handler=cmd_evals)

    project = sub.add_parser("project", help="register a project in the database")
    project.add_argument("action", choices=["add", "list", "network"])
    project.add_argument("directory", nargs="?", help="project directory (for add)")
    project.add_argument("--name", help="project name (default: the directory's name)")
    project.add_argument("--git-remote", help="git remote of the project")
    project.add_argument(
        "--network",
        choices=list(admin.NETWORK_POLICIES),
        default=DEFAULT_NETWORK_POLICY,
        help=(
            f"network policy of the sandbox (default: {DEFAULT_NETWORK_POLICY}; 'none' reaches "
            "nothing, 'proxy' the project's allowed_domains, 'full' every domain)"
        ),
    )
    _database_option(project)
    project.set_defaults(handler=cmd_project)

    config = sub.add_parser("config", help="configuration values in the database")
    config.add_argument(
        "action", choices=["list", "get", "set", "unset", "add", "remove", "models"]
    )
    config.add_argument("key", nargs="?", default="", help="dotted key, e.g. claude.model")
    config.add_argument("value", nargs="?", help="value (JSON, or plain text)")
    config.add_argument(
        "--changed",
        action="store_true",
        help="with list: only the settings that do not hold their default",
    )
    config.add_argument(
        "--effort",
        choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="with models: the checkers' thinking effort (left out: the chosen model's own)",
    )
    config.epilog = (
        "list prints every setting with its effective value, where that value comes from and "
        "its default; get prints what a scope stores, which is `{}` until something is set. "
        "`models MODEL` points every checker, the narrator and the summarizer at one model; "
        "`models auto` chooses it from the credentials found, `models default` stores none, so "
        "that Haiku on the Anthropic API (or through Bedrock in Bedrock mode) is used. "
        "add and remove change one entry of a list setting and keep the others, e.g. "
        "`calu config add claude.sandbox.deny_read ~/.ssh`; set replaces the whole list."
    )
    config.add_argument("--project", help="the project's scope instead of the global one")
    _database_option(config)
    config.set_defaults(handler=cmd_config)

    checker = sub.add_parser(
        "checker",
        help="checker definitions: instructions, model, limits",
        description=(
            "calu checker list | show | set | edit | diff | update | stats | misses [NAME]; "
            "calu checker template list | show NAME | use NAME [--checker C] "
            "[--append | --replace]: the example instructions shipped with calu"
        ),
    )
    checker.add_argument(
        "action",
        choices=["list", "show", "set", "edit", "diff", "update", "stats", "misses", "template"],
    )
    checker.add_argument(
        "name", nargs="?", help="checker name, e.g. command; for template: list, show or use"
    )
    checker.add_argument("target", nargs="?", help="for template show and use: the template")
    checker.add_argument(
        "--checker",
        dest="checker",
        help="for template use: the checker to load it into (default: the one it is written for)",
    )
    checker.add_argument(
        "--append",
        action="store_true",
        help="for template use: add the template after the stored instructions",
    )
    checker.add_argument(
        "--replace",
        action="store_true",
        help="for template use: replace instructions that are your own text",
    )
    checker.add_argument(
        "--instructions-file", help="read new instructions from this file ('-' for stdin)"
    )
    checker.add_argument("--model", help="Pydantic AI model string")
    checker.add_argument("--route", choices=list(admin.ROUTES), help="route (for a new checker)")
    checker.add_argument("--enable", action="store_true", help="use this checker")
    checker.add_argument("--disable", action="store_true", help="stop using this checker")
    checker.add_argument("--request-limit", type=int)
    checker.add_argument("--output-tokens-limit", type=int)
    checker.add_argument("--max-tokens", type=int)
    checker.add_argument(
        "--days",
        type=int,
        help="for `stats` and `misses`: only the last N days (default: all of them)",
    )
    _database_option(checker)
    checker.set_defaults(handler=cmd_checker)

    honesty = sub.add_parser(
        "honesty",
        help="the honesty rules the change checkers read, and the trusted commits",
        description=(
            "calu honesty show | edit | reset [--project NAME] (the honesty rules); "
            "calu honesty trust COMMIT-OR-BRANCH [--note TEXT] | trust --file PATH | "
            "trusted [--all] | untrust COMMIT | untrust --file HASH-OR-PATH | prune --days N "
            "(what is certified as honest)"
        ),
    )
    honesty.add_argument(
        "action", choices=["show", "edit", "reset", "trust", "trusted", "untrust", "prune"]
    )
    honesty.add_argument(
        "target",
        nargs="?",
        help="for trust: a commit, branch or tag, resolved with git in the current directory "
        "(a branch means the commit it points at now); for untrust: a commit or its id",
    )
    honesty.add_argument("--project", help="the project's rules instead of the global ones")
    honesty.add_argument(
        "--file",
        help="for trust: certify this file's present content by its SHA-256, for a file git "
        "does not hold; for untrust: the hash or the path of a trusted file content",
    )
    honesty.add_argument("--note", help="for trust: why, kept with the certification")
    honesty.add_argument(
        "--all", action="store_true", help="for trusted: every entry, not only the newest"
    )
    honesty.add_argument(
        "--days",
        type=int,
        help="for prune: remove the records of the sources check and review that are older "
        "than this many days; user records are never removed",
    )
    _database_option(honesty)
    honesty.set_defaults(handler=cmd_honesty)

    traces = sub.add_parser(
        "traces",
        help="session traces: the redaction agent's instructions, the final pass, the hook",
        description=(
            "calu traces instructions show | edit | reset [--project NAME]: what the "
            "redaction agent keeps, removes and replaces in a session trace "
            "(traces.redactor_instructions; empty: the text calu ships). "
            "calu traces finalize [--staged]: the final redaction pass over what the next "
            "commit adds to the trace files. calu traces hook install [--chain] | remove: the "
            "git pre-commit hook that runs `calu traces finalize --staged`"
        ),
    )
    traces.add_argument("subject", choices=["instructions", "finalize", "hook"])
    traces.add_argument(
        "action",
        nargs="?",
        choices=["show", "edit", "reset", "install", "remove"],
        help="instructions: show, edit or reset; hook: install or remove; finalize: none",
    )
    traces.add_argument(
        "--project",
        help="instructions: the project's instead of the global ones; finalize: the project "
        "whose settings apply (default: the one registered for the directory)",
    )
    traces.add_argument(
        "--staged",
        action="store_true",
        help="finalize: the trace files staged for the next commit (what the pre-commit hook "
        "runs), instead of the files on disk",
    )
    traces.add_argument(
        "--directory",
        default=".",
        help="finalize, hook: the project directory (default: the current directory)",
    )
    traces.add_argument(
        "--chain",
        action="store_true",
        help="hook install: keep a pre-commit hook calu did not write and run it after calu's",
    )
    _database_option(traces)
    traces.set_defaults(handler=cmd_traces)

    rules = sub.add_parser(
        "rules",
        help="the allow and deny lists",
        description=(
            "calu rules list [LIST] | add LIST PATTERN | edit LIST | remove|enable|disable ID"
            " | add-set NAME (a ready-made set: "
            + "; ".join(f"{name}: {what}" for name, (_list, what) in admin.RULE_SETS.items())
            + ")"
        ),
    )
    rules.add_argument(
        "action", choices=["list", "add", "edit", "remove", "enable", "disable", "add-set"]
    )
    rules.add_argument(
        "target",
        nargs="?",
        help="list name (list, add), rule id (remove, enable, disable) or set name (add-set)",
    )
    rules.add_argument("pattern", nargs="?", help="regular expression (for add)")
    rules.add_argument("--comment", help="why the rule exists")
    rules.add_argument("--project", help="the project's rules instead of the global ones")
    rules.add_argument("--position", type=int, help="position in the list (default: append)")
    _database_option(rules)
    rules.set_defaults(handler=cmd_rules)

    p = sub.add_parser("credential", help="manage the encrypted credential store")
    p.add_argument("action", choices=["list", "set", "remove", "new"])
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--no-password",
        action="store_true",
        help="store it protected by the file mode only, so no command asks for a password "
        "(for the narrow 'operator' credential; refused for 'session'; prints what it costs)",
    )
    p.set_defaults(handler=cmd_credential)

    # An easter egg, hidden from `calu --help`: the list of commands is fixed before it is
    # added, and it gets no `help`, so argparse prints no row for it.
    sub.metavar = "{" + ",".join(sub.choices) + "}"
    p = sub.add_parser(
        "lightcycles",
        description="MetaTron for the browser \N{EM DASH} after MetaTron by Kai Londenberg. "
        "Serves the game on 127.0.0.1 and opens it in the browser; Ctrl-C stops it. "
        "Without --players and --bots the game starts with the original's preset: "
        "two people. Alone: --players 1 --bots 3.",
    )
    p.add_argument("--port", type=int, help="the port (default: 8817, or a free one)")
    p.add_argument("--no-browser", action="store_true", help="only print the address")
    p.add_argument("--players", type=int, help="people, 0 to 10 (players 1, 2, ...)")
    p.add_argument("--bots", type=int, help="computer players (KI) after the people")
    p.add_argument("--speed", type=int, help="pixels per frame, 1 to 9 (default: 2)")
    p.add_argument("--seed", type=int, help="seed of the game's random numbers")
    p.set_defaults(handler=cmd_lightcycles)
    return parser


def main(
    argv: list[str] | None = None, store_factory: Callable[[], CredentialStore] = CredentialStore
) -> int:
    """Run `calu` with the given arguments.

    Args:
        argv: Arguments without the program name; None uses `sys.argv[1:]`.
        store_factory: Creates the credential store (tests pass a store in a temporary dir).

    Side effects: as root, points calu at `/etc/calu` and `/var/lib/calu`
    (`calu.asuser.use_system_directories`); remembers the arguments for `--sudo`; loads calu's own
    env file (`~/.config/calu/.env`) into the process environment before any command runs, without
    overwriting what is already set. Every command then sees the same checker keys a session sees:
    only `calu run` used to read the file, so `calu doctor` reported "none of the known providers
    has a key" with a key in it, and `calu evals` ran its checkers without one
    (agent/records/PLAN.md D131). The keys stay hidden from Claude
    Code: a session (`calu run`, `calu serve`) removes them (`SdkRunner.hidden_env`), and
    `calu login` starts `claude auth` without them (`claude_login_command`). Other programs
    calu starts may inherit them (the browser of `calu lightcycles`, for one).

    `calu run` and `calu serve` run through `_logged`: tracebacks go to their log file and
    stderr gets one line per error (D225).

    Returns:
        The exit code: 0 on success, 1 on a reported error, 130 when the command was
        interrupted with Ctrl-C (one line on stderr, no traceback).
    """
    global _ARGUMENTS
    _ARGUMENTS = list(sys.argv[1:] if argv is None else argv)
    # Before anything reads a path: as root, calu's credentials and state are in the system
    # directories, never in a home directory (requirement 26).
    asuser.use_system_directories(os.environ, os.geteuid())
    # A password typed in an earlier call of `main` in the same process (tests, an embedding
    # program) must not reach this command's `calu setup`.
    _typed.clear()
    args = build_parser().parse_args(argv)
    log_name = getattr(args, "log_name", None)
    if log_name is not None:
        return _logged(log_name, args, store_factory)
    try:
        load_secrets()
        args.handler(args, store_factory())
    except KeyboardInterrupt:
        # Ctrl-C, also at a password prompt: one line, as `calu run` and `calu serve` end.
        print("\ncalu: interrupted", file=sys.stderr)
        return 130
    except (
        CliError,
        CredentialError,
        admin.AdminError,
        local_db.DockerError,
        psycopg.Error,
        ValueError,
        # A file that cannot be read or written, a port that is taken, a server that does not
        # answer: reported as one line, not as a traceback.
        OSError,
        TimeoutError,
    ) as exc:
        print(f"calu: {_error_text(exc)}", file=sys.stderr)
        return 1
    return 0


def _error_text(exc: Exception) -> str:
    """Put an error a command ended with into the text after "calu: ".

    Args:
        exc: The error.

    Returns:
        For a wrong password of a stored credential, a sentence that names the credential
        (the exception itself carries only the credential's name); for a password the
        database refused, the server's own sentence once; for a `CliError` its message; else
        "<type name>: <message>".
    """
    if isinstance(exc, CliError):
        return str(exc)  # written for the user; the type name would only be noise
    if isinstance(exc, WrongPassword):
        return f"wrong password for the credential {str(exc)!r}"
    if isinstance(exc, LoginRefused) and exc.code == "28P01":
        # PostgreSQL's "password authentication failed": libpq repeats it once per address
        # it tried, with the host and the port in front; the first statement is enough.
        found = re.search(r"FATAL:\s*(.+?)(?=\s+connection to server|\n|$)", str(exc))
        reason = found.group(1).strip() if found else str(exc)
        return f"the database refused the login, so the password is wrong: {reason}"
    return f"{type(exc).__name__}: {exc}"


def _logged(
    name: str, args: argparse.Namespace, store_factory: Callable[[], CredentialStore]
) -> int:
    """Run `calu run` or `calu serve` with the log file and one-line errors on stderr (D225).

    Side effects: sets up the log (`calu.logfile.setup`: the log file at
    `$XDG_STATE_HOME/calu/log/<name>.log` with every record and its traceback, stderr with one
    line per warning or error), unless the command was given `--sudo` and does not run as
    root yet: it then replaces itself by `sudo calu ...`, which opens root's log; then
    everything the command's handler does. An error the
    command ends with is written to the log file with its traceback and to stderr as one line;
    Ctrl-C ends the command with one line, too.

    Args:
        name: The command's name, which names the log file.
        args: The parsed arguments; `args.handler` is the command.
        store_factory: Creates the credential store.

    Returns:
        The exit code: 0 on success, 1 on an error, 130 on Ctrl-C.
    """
    # A command that is about to replace itself by `sudo calu ...` opens no log file: the
    # log is root's (`/var/lib/calu/log`), and the invoking user's home gets nothing of calu's.
    elevates = getattr(args, "sudo", False) and os.geteuid() != 0
    path = None if elevates else logfile.setup(name)
    where = f"; details in {path}" if path is not None else ""
    try:
        load_secrets()
        args.handler(args, store_factory())
    except KeyboardInterrupt:
        _log.info("calu %s: interrupted", name)
        print("calu: interrupted", file=sys.stderr)
        return 130
    except (CliError, CredentialError) as exc:
        # Errors calu reports on purpose: their text is the whole message, and it is not
        # cut, because it may end with the command to run next (the install hint of --sudo).
        text = _error_text(exc) if isinstance(exc, WrongPassword) else str(exc)
        _log.info("calu %s: %s", name, text)
        print(f"calu: {logfile.one_line(text, limit=None)}", file=sys.stderr)
        return 1
    except Exception as exc:
        # At INFO: the traceback goes to the file only; stderr gets the line below.
        _log.info("calu %s ended with an error", name, exc_info=True)
        print(f"calu: {logfile.describe(exc)}{where}", file=sys.stderr)
        return 1
    return 0
