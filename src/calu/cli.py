"""Command line entry point `calu` (agent/records/PLAN.md §8).

    calu db up                     start the local PostgreSQL (Docker) by hand; commands that
                                   need it start it themselves
    calu db down                   stop the local PostgreSQL (data is kept)
    calu db migrate                apply schema migrations (asks for the user password)
    calu db reset                  drop and recreate everything in the database; asks for the
                                   user password and a typed confirmation
    calu setup [--database docker|server|rds-iam] [--checker-model M]
                                   on the first run choose and set up the database (a local
                                   one in Docker, an existing server, RDS with IAM); then
                                   migrate, load the default checkers, store the `user` and
                                   `db-admin` logins (asks for the user password)
    calu credential list|set NAME|remove NAME
                                   manage the encrypted credential store
    calu run [DIR]                 start a checked Claude Code session in DIR (asks for the
                                   user password; see `calu.run`)
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
    logins,
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
from calu.db import Db, EarlierSchema, LoginRefused, migrate
from calu.db import local as local_db
from calu.db import place as db_place
from calu.db import rds as rds_auth
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
    USER_ROLE,
    SetupRefused,
    generated_password,
    is_user_password,
    preflight,
    seed_defaults,
    set_user_password,
    setup_database,
    user_login,
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
        prompt: The prompt, e.g. `logins.USER_PROMPT`.
        confirm: Ask a second time (`logins.REPEAT_PROMPT`) and require both to match (for
            new passwords).

    Returns:
        The password; the caller should `wipe` it. A blank answer is returned as it is:
        whether it is right is for the store or the server to say (the owner, 2026-11-18).

    Raises:
        CliError: `logins.MISMATCH` if the confirmation does not match; `logins.NOT_ENTERED`
            if the input ended at the prompt (Ctrl-D on a terminal, the end of piped input).
    """

    def ask(text: str) -> str:
        """Read one password.

        Args:
            text: The prompt.

        Returns:
            The password without the line end.

        Raises:
            CliError: If the input ended at the prompt.
        """
        if sys.stdin.isatty():
            try:
                return getpass.getpass(text)
            except EOFError:
                # Ctrl-D: end the line the prompt is on, then say what happened.
                print(file=sys.stderr)
                raise CliError(logins.NOT_ENTERED) from None
        line = sys.stdin.readline()
        if not line:
            raise CliError(logins.NOT_ENTERED)
        return line.rstrip("\n")

    first = ask(prompt)
    if confirm and ask(logins.REPEAT_PROMPT) != first:
        raise CliError(logins.MISMATCH)
    return bytearray(first.encode())


_typed: dict[str, str] = {}
"""`user`: the user password as it was typed in this command, kept until the command ends, so
that a command that opens both entries (`calu setup`, `calu db migrate`) asks once."""

_admin_place: list[Target] = []
"""The database the last herd command of this process connected to, without any secret: the
login of `calu_admin` there with an empty password. The full-screen client is given it, so
that it can change settings in the database of the session it is attached to."""


_held: list[Target] = []
"""The database login a client that runs as root keeps in memory (`calu attach --sudo`): read
once from root's store, then used for every later step (minting and revoking tokens,
switching sessions, changing settings), because after the switch to the user's file identity
the store cannot be read again, and so that nothing is asked twice (agent/records/PLAN.md D214).
It is the user login (`calu_admin`, D249, D265). Never written to disk or passed on."""

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


_READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "config": frozenset({"list", "get"}),
    "checker": frozenset({"list", "show", "diff", "stats", "misses"}),
    "rules": frozenset({"list"}),
    "project": frozenset({"list"}),
    "honesty": frozenset({"show", "trusted"}),
    "traces": frozenset({"show"}),
}
"""Per command that works with `_settings_login`: its actions that only read. Every other action
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

    A person can reach more than one database (the `user` entry of an own setup, the place
    file of root's database). The line is what tells them which one a change went to.

    Args:
        login: The login the command uses.
    """
    print(f"changing settings in {login.place} as {login.user}", file=sys.stderr)


def _settings_login(store: CredentialStore, args: argparse.Namespace | None = None) -> bytearray:
    """Return the user login for a command that may change settings, and say where it goes.

    Side effects: those of `_user_target` (asks the user password where the entry has one);
    prints the line of `_announce_change` to stderr unless the command only reads
    (`_only_reads`).

    Args:
        store: The credential store.
        args: The command's parsed arguments (what `_only_reads` reads), or None: the
            command counts as a change.

    Returns:
        The login as the credential store would hold it; the caller wipes it.

    Raises:
        CliError: As `_user_target`.
    """
    login = _user_target(store)
    if not _only_reads(args):
        _announce_change(login)
    return bytearray(login.dump())


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

    An entry stored without a password is read without asking. A protected entry is asked
    for up to `logins.MAX_ATTEMPTS` times; each wrong answer says so and how many tries are
    left (`logins.wrong`). The user password typed for `user` or `db-admin` is kept in
    `_typed` until the command ends and tried first for the other of the two, so that a
    command that needs both asks once.

    Args:
        store: The credential store.
        name: The credential name.
        prompt: The password prompt, used only for a protected entry.

    Returns:
        The decrypted secret; the caller should `wipe` it.

    Raises:
        CliError: If the credential does not exist (the message says how to create it), the
            input ended at the prompt (`logins.NOT_ENTERED`), or every try was wrong.
        CredentialError: If the store is corrupt.
    """
    if name not in store:
        if name in (logins.USER, logins.DB_ADMIN):
            _refuse_old_logins(store)
            raise CliError(logins.no_login(_root_store_hint(store)))
        raise CliError(f"no credential {name!r} in {store.path}{_root_store_hint(store)}")
    if not store.needs_password(name):
        return store.load(name)
    shared = name in (logins.USER, logins.DB_ADMIN)
    if shared and "user" in _typed:
        with contextlib.suppress(WrongPassword):
            return store.load(name, _typed["user"])
    attempt = 0
    while True:
        attempt += 1
        password = read_password(prompt)
        try:
            secret = store.load(name, password)
            if shared or name == "admin":
                # "admin": the old owner entry `calu setup` converts (`_convert_old_store`).
                _typed["user" if shared else "admin"] = bytes(password).decode()
            return secret
        except WrongPassword:
            left = logins.MAX_ATTEMPTS - attempt
            message = logins.wrong(left, blank=not password)
            if not shared:
                message = message.replace("the calu user password", f"the password of {name!r}")
            if not left:
                raise CliError(message) from None
            print(message, file=sys.stderr)
        finally:
            wipe(password)


def _refuse_old_logins(store: CredentialStore) -> None:
    """Stop a command on a store that holds only the logins of before D265.

    Args:
        store: The credential store; it holds no `user` entry.

    Raises:
        CliError: With `logins.old_logins` if any of `logins.OLD_ENTRIES` is stored.
    """
    old = [name for name in logins.OLD_ENTRIES if name in store]
    if old:
        raise CliError(logins.old_logins("admin" in old))


def ask(prompt: str, default: str | None, flag: str) -> str:
    """Ask one setup question that is no secret.

    Args:
        prompt: The question without the default, e.g. "Port on 127.0.0.1".
        default: What an empty answer means; None if an answer is required.
        flag: The command-line option that gives the answer, for the error message.

    Returns:
        The answer. Without a terminal nothing is asked: the default is returned.

    Raises:
        CliError: If an answer is required and none was given, there is no terminal to ask
            on and no default (the message names the option to pass), or the input ended at
            the question (Ctrl-D).
    """
    if not sys.stdin.isatty():
        if default is None:
            raise CliError(f"{flag} is needed (there is no terminal to ask on)")
        return default
    shown = f"{prompt} ({default}): " if default else f"{prompt}: "
    try:
        answer = input(shown).strip() or (default or "")
    except EOFError:
        # Ctrl-D: end the line the question is on, then say what happened.
        print(file=sys.stderr)
        raise CliError(
            f"Not done: the input ended at the question {prompt!r}. Run the command again "
            f"to answer it, or give {flag}"
        ) from None
    if not answer:
        raise CliError(f"{prompt.lower()} must not be empty")
    return answer


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
        CliError: As `load_credential`.
        CredentialError: If the store is corrupt.
        TargetError: If the entry holds no database login this version can read.
    """
    secret = load_credential(store, name, prompt)
    try:
        return Target.parse(secret)
    finally:
        wipe(secret)


def _user_target(store: CredentialStore, may_ask: bool = True, why: str = "") -> Target:
    """Return the user login (the role `calu_admin`) for this command (D265).

        a login kept in `_held` (a root client)         ──► that login, nothing asked
        root's place file holds a password (an empty
          user password, `sudo calu setup`, D228)       ──► that login, nothing asked
        the `user` entry without a password             ──► that entry, nothing asked
        the `user` entry with a password                ──► that entry, the user password
                                                            asked (`load_credential`)
        only entries of before D265                     ──► CliError: `calu setup` converts
        root's place file without a password            ──► `calu_admin` there, with the user
                                                            password typed now (checked by
                                                            one login; asked up to
                                                            `logins.MAX_ATTEMPTS` times)
        nothing                                         ──► CliError: `calu setup`

    Side effects: may ask for the user password and keep it in `_typed` until the command
    ends; may log in once to check a typed password; records where the database is, without
    a secret, in `_admin_place`, and keeps the login in `_held` while `_hold` is set or the
    process runs as root, so that later steps of the same command ask nothing.

    Args:
        store: The credential store.
        may_ask: Whether a password may be asked for. False for a process whose stdin
            carries something else (`calu acp` speaks the editor's protocol on it, and
            `read_password` reads a line from a stdin that is not a terminal).
        why: Why nothing may be asked, for the message of `NeedsPassword`.

    Returns:
        The login, with its password.

    Raises:
        NeedsPassword: If the password is needed and `may_ask` is False
            (`logins.no_terminal`).
        CliError: If there is no login, only logins of before D265, the input ended at the
            prompt, or every try was wrong.
        CredentialError: If the store is corrupt.
        TargetError: If the entry holds no database login this version can read.
    """
    if _held:
        return _held[0]
    shared = _shared_user()
    if shared is not None:
        _admin_place[:] = [shared]
        return shared
    if logins.USER in store:
        if not may_ask and store.needs_password(logins.USER):
            raise NeedsPassword(logins.no_terminal(why or "this command has no terminal"))
        login = load_target(store, logins.USER, logins.USER_PROMPT)
    else:
        _refuse_old_logins(store)
        place = _place_or_none()
        if place is None:
            raise CliError(logins.no_login(_root_store_hint(store)))
        if not may_ask:
            raise NeedsPassword(logins.no_terminal(why or "this command has no terminal"))
        login = _typed_user_login(place)
    with contextlib.suppress(ValueError):  # TargetError: a login found through a service file
        _admin_place[:] = [user_login(login, "")]
    if _hold or os.geteuid() == 0:
        # A root process is out of reach of what Claude Code starts, so it keeps the login
        # for its later steps (revoking its token at the end) instead of asking again.
        _held[:] = [login]
    return login


def _typed_user_login(place: Target) -> Target:
    """Ask the user password and log in with it as `calu_admin` where the place file says.

    Side effects: asks up to `logins.MAX_ATTEMPTS` times on the terminal; one login per
    answer to check it (`is_user_password`); keeps the accepted password in `_typed`.

    Args:
        place: Root's place file's record: where the database is, without a secret.

    Returns:
        The login of `calu_admin` with the accepted password.

    Raises:
        CliError: If the input ended at the prompt, or every try was refused.
        psycopg.OperationalError: If the database cannot be reached.
    """
    attempt = 0
    while True:
        attempt += 1
        password = read_password(logins.USER_PROMPT)
        try:
            text = bytes(password).decode()
        finally:
            wipe(password)
        if asyncio.run(is_user_password(place, text)):
            _typed["user"] = text
            return user_login(place, text)
        left = logins.MAX_ATTEMPTS - attempt
        if not left:
            raise CliError(logins.wrong(0, blank=not text))
        print(logins.wrong(left, blank=not text), file=sys.stderr)


def _db_admin_target(store: CredentialStore) -> Target:
    """Return the database owner's login (`db-admin`), for migrations and setup.

    Side effects: asks for the user password if the entry has one and none was typed in
    this command (`load_credential`).

    Args:
        store: The credential store.

    Returns:
        The owner login.

    Raises:
        CliError: If there is no `db-admin` entry (only entries of before D265: `calu setup`
            converts them), the input ended at the prompt, or every try was wrong.
        CredentialError: If the store is corrupt.
        TargetError: If the entry holds no database login this version can read.
    """
    return load_target(store, logins.DB_ADMIN, logins.USER_PROMPT)


def _shared_user() -> Target | None:
    """Return the user login that root's place file publishes (an empty user password, D228).

    Returns:
        The login with its password, from the place file (`calu.db.place`), or None if there
        is no such file, it cannot be trusted, or it holds no password.
    """
    try:
        shared = db_place.read()
    except db_place.PlaceError:
        return None
    if isinstance(shared.auth, PasswordAuth) and shared.auth.password:
        return shared
    return None


def _place_or_none() -> Target | None:
    """Read root's place file (`calu.db.place`) without raising.

    Returns:
        The login it describes, or None if there is no trustworthy file.
    """
    try:
        return db_place.read()
    except db_place.PlaceError:
        return None


def cmd_db_up(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start the local database; without a stored database create the local one first.

    With a `db-admin` credential the port and the data location come from it, so the command
    asks for the user password. `calu run` and the other commands start a stopped local
    database themselves; this command is for starting it by hand.

    Args:
        args: Parsed arguments (`port`, `data_dir`).
        store: The credential store.

    Raises:
        CliError: If the stored database is not the local one, an option contradicts what is
            stored, the store holds only logins of before D265, or a new password is not
            confirmed.
        local_db.DockerError: If docker fails.
    """
    if logins.DB_ADMIN not in store:
        _refuse_old_logins(store)
        data = str(local_db.prepare_data_dir(Path(args.data_dir or local_db.default_data_dir())))
        password = _choose_user_password(False, local=True)
        try:
            _first_start_local(store, args.port or local_db.DEFAULT_PORT, data, password)
        finally:
            wipe(password)
        print("next: calu setup")
        return
    owner = _db_admin_target(store)
    block = owner.docker
    if block is None:
        raise CliError(
            f"the stored database is {owner.place}, not a local one that calu starts; "
            "`calu credential remove db-admin` and `calu credential remove user` first if a "
            "local database is wanted instead"
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

    Side effect: asks for the user password if the `db-admin` entry has one.

    Raises:
        CliError: If there is no `db-admin` credential (only logins of before D265: `calu
            setup` converts them and migrates), the password is not given, or the database
            was set up by an earlier calu whose migrations were replaced (`EarlierSchema`:
            the message names `calu db reset`).
        psycopg.Error: On database errors.
    """
    url = load_credential(store, logins.DB_ADMIN, logins.USER_PROMPT)
    try:
        count = asyncio.run(_migrate(url))
    except EarlierSchema as exc:
        raise CliError(str(exc)) from exc
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
    stored `user` and `db-admin` credentials keep working.

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
    fresh database are loaded. Two things stand in front of that: the user password that
    opens the `db-admin` credential (none with an empty user password), and the phrase
    `RESET_PHRASE`, typed in not less than `RESET_MIN_SECONDS`.

    Args:
        args: Parsed arguments (unused).
        store: The credential store.

    Raises:
        CliError: If there is no `db-admin` credential, the password is not given, or the
            confirmation is missing, wrong or too fast.
        psycopg.Error: On database errors.
    """
    url = load_credential(store, logins.DB_ADMIN, logins.USER_PROMPT)
    try:
        lines = asyncio.run(_reset_summary(url))
        print("This deletes the whole calu database and creates it again, empty:")
        print("\n".join(lines) if lines else "  (the database holds no calu tables yet)")
        print("Checker instructions, rules and every setting are lost. This cannot be undone.")
        confirm_reset()
        print(asyncio.run(_reset(url)))
    finally:
        wipe(url)


def cmd_uninstall(args: argparse.Namespace, store: CredentialStore) -> None:
    """Remove calu from this machine: logins, state, the local database, the installation.

    See `calu.uninstall` (agent/records/PLAN.md D267). Prints what it would remove and what it
    keeps; with `--dry-run` that is all. Otherwise it refuses while calu processes run
    (`calu serve`, `calu web`, an editor's `calu ide`, ...), asks for
    `calu.uninstall.CONFIRM_PHRASE` typed by hand in no less than
    `calu.uninstall.MIN_SECONDS`, and removes the items one by one, the installation last.
    Run as root (`sudo calu uninstall`) it removes the root installation, root's files and
    those of the user who ran sudo.

    Args:
        args: Parsed arguments (`dry_run`).
        store: The credential store (unused: nothing is decrypted).

    Raises:
        CliError: If calu processes still run, the confirmation is missing, wrong or too fast
            (nothing is removed then), or an item could not be removed (the others are).
    """
    import calu
    from calu import uninstall

    euid = os.geteuid()
    chosen = uninstall.plan(euid, dict(os.environ), Path(calu.__file__))
    if not chosen.items:
        print("Nothing of calu was found to remove on this machine.")
        for line in chosen.lines():
            print(line)
        return
    print("calu uninstall removes:")
    for line in chosen.lines():
        print(line)
    if args.dry_run:
        print("(--dry-run: nothing was removed)")
        return
    running = machine.calu_processes(euid, os.getpid())
    if running:
        listed = "\n".join(f"  {pid}  {line}" for pid, line in running)
        raise CliError(
            f"calu still runs; stop these first (close the editor for `calu ide`), then run "
            f"`calu uninstall` again. Nothing was removed.\n{listed}"
        )
    try:
        uninstall.confirm()
    except uninstall.NotConfirmed as exc:
        raise CliError(str(exc)) from None
    compose = local_db.compose_file()
    failed = uninstall.remove(chosen, compose, uninstall.compose_image(compose), euid)
    if failed:
        raise CliError(
            "these were not removed (the rest was):\n" + "\n".join(f"  {f}" for f in failed)
        )
    print("calu is removed. Run the install script again for a fresh start.")


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
        CliError: If "auto" finds no credentials at all (the message says what to add), or if
            `requested` is a `claude:` model, which calu no longer supports (PLAN.md D258).
    """
    if requested == "default":
        return (
            "",
            effort,
            "the default model: " + ANTHROPIC_HAIKU + ", or in Bedrock mode " + (BEDROCK_EU_HAIKU),
        )
    if requested and requested.startswith("claude:"):
        raise CliError(
            f"{requested}: calu no longer runs checkers through the `claude` CLI; it runs only "
            "the main coding agent. Give an API model, or `auto` to choose one from the keys "
            "found"
        )
    if requested and requested != "auto":
        return requested, effort, "as given"
    choice = detect_model(os.environ)  # `main` has loaded calu's env file into it
    if choice is None:
        raise CliError(
            "no model provider found: no AWS credentials that STS accepts, and none of "
            "ANTHROPIC_API_KEY, FIREWORKS_API_KEY, MISTRAL_API_KEY in the environment or in "
            f"{user_env_file()}. Add one, or give a model explicitly"
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


def _setup_reader() -> tuple[int, int] | None:
    """Name the user who may read the password-free user login of a root setup (D279).

    Returns:
        (uid, gid) of the user who ran `sudo calu setup` (`SUDO_UID`, `SUDO_GID`), or None
        when root ran it without sudo: the file is then root's alone.
    """
    uid, gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if not uid.isdigit() or not gid.isdigit() or int(uid) == 0:
        return None
    return int(uid), int(gid)


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
        "The database user below administers calu's database: it creates the tables and "
        "calu's login role (calu_admin). It needs a database of its own and the right to "
        "create roles; it does not need to be a superuser. calu keeps its login, protected "
        "by your calu user password, for later migrations."
    )
    host = args.host or ask("Host", None, "--host")
    port = args.port or _port(ask("Port", "5432", "--port"))
    dbname = args.dbname or ask("Database", "calu", "--dbname")
    user = args.user or ask("User", None, "--user")
    password = read_password("Password of that database user (not echoed): ")
    try:
        ca = _keep_ca_file(store, args.sslrootcert) if args.sslrootcert else None
        owner = from_fields(
            host,
            port,
            dbname,
            user,
            PasswordAuth(password=password.decode()),
            args.sslmode,
            ca,
        )
    finally:
        wipe(password)
    if owner.conninfo.get("sslmode") == "disable":
        print("warning: sslmode disable sends the password and all data unencrypted.")
    try:
        asyncio.run(_check_owner_login(owner))
    except CliError as exc:
        if not _auth_requirement_refused(str(exc)) or not _keep_server_auth(args, owner, exc):
            raise
        owner = owner.with_server_auth()
        asyncio.run(_check_owner_login(owner))
    return owner


AUTH_REQUIREMENT_ERRORS = ("authentication method requirement", "channel binding")
"""Parts of libpq's messages when a server does not offer the login method calu requires."""


def _auth_requirement_refused(message: str) -> bool:
    """Tell whether a failed connection failed on calu's login requirements alone.

    Args:
        message: The error of the connection attempt.

    Returns:
        True when libpq refused the server's login method: no SCRAM, or no channel binding.
    """
    return any(part in message for part in AUTH_REQUIREMENT_ERRORS)


def _keep_server_auth(args: argparse.Namespace, owner: Target, refused: CliError) -> bool:
    """Ask whether an existing server may keep its own login method (D279).

    calu's default for a server on another host is SCRAM with channel binding. A server that
    exists already is configured by its administrator; calu does not force its default on
    it, but it does not weaken the login without a person's word either: a machine in the
    middle could cause the same refusal on purpose.

    Args:
        args: Parsed arguments (`keep_server_auth`).
        owner: The login that was refused.
        refused: The refusal.

    Returns:
        True when the person agreed (or `--keep-server-auth` was given).

    Raises:
        CliError: If the answer is neither yes nor no.
    """
    if args.keep_server_auth:
        return True
    print(
        f"{owner.place} does not offer the login method calu asks of a server on another "
        f"host: SCRAM, and channel binding over TLS ({refused}). Without them a machine in "
        "the middle can read the password or pass the login on. If this server is set up "
        "that way on purpose (md5, or a connection pooler without channel binding), calu "
        "can keep its own method for this database."
    )
    answer = ask("Keep the server's own login method? yes/no", "no", "--keep-server-auth")
    if answer.lower() in ("yes", "y"):
        return True
    if answer.lower() in ("no", "n"):
        return False
    raise CliError(f"{answer!r} is neither yes nor no")


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
        "rds-db:connect for that database user. calu's own login (calu_admin) uses the calu "
        "user password."
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
    ("keep_server_auth", "--keep-server-auth", ("server",)),
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
            (the `db-admin` entry, or the `admin` entry of before D265), whose options are
            then all fixed.

    Raises:
        CliError: If an option of `_SETUP_OPTIONS` was given for a stored database or for a
            variant it does not apply to, `--stop-db-with-server` for a new database other
            than docker, or `--set-password` together with `--no-password`.
    """
    for attribute, option, variants in _SETUP_OPTIONS:
        if getattr(args, attribute) is None:
            continue
        if variant is None:
            raise CliError(
                f"{option} describes a new database, and one is set up already; leave it "
                "out, or remove the stored logins first to set up another one (`calu "
                "credential list` shows them, `calu credential remove NAME` removes one)"
            )
        if variant not in variants:
            raise CliError(
                f"{option} does not apply to the database {variant}; it is for "
                f"{' and '.join(variants)}"
            )
    if args.stop_db_with_server is not None and variant not in (None, "docker"):
        raise CliError(
            f"--stop-db-with-server and --no-stop-db-with-server do not apply to the database "
            f"{variant}; they are for docker, the local database calu starts and stops"
        )
    if args.set_password and args.no_password:
        raise CliError(
            "--set-password and --no-password contradict each other: --no-password is "
            "--set-password with an empty password; give one of them"
        )


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


def choose_stop_with_server(flag: bool | None) -> bool:
    """Decide whether a new local database stops with `calu serve` (PLAN.md D269).

        --stop-db-with-server / --no-stop-db-with-server   ──► that value
        a terminal                                         ──► the question; default: no
        no terminal, no option                             ──► no (it keeps running)

    Side effects: on a terminal without the option, prints one line and reads one answer.

    Args:
        flag: The option, or None.

    Returns:
        True if the database is to stop when `calu serve` ends and stay stopped after a
        restart of the machine.

    Raises:
        CliError: If the answer is neither yes nor no.
    """
    if flag is not None:
        return flag
    if not sys.stdin.isatty():
        return False
    print(
        "The local database can keep running in Docker (and start again with the machine), "
        "or stop when `calu serve` ends. Either way `calu run` and `calu serve` start it "
        "when it does not run."
    )
    answer = ask("Stop it when calu serve ends? yes/no", "no", "--stop-db-with-server").lower()
    if answer in ("yes", "y"):
        return True
    if answer in ("no", "n"):
        return False
    raise CliError(f"{answer!r} is neither yes nor no")


def _choose_user_password(empty: bool, local: bool) -> bytearray:
    """Ask for a new calu user password, after one paragraph that explains it.

    Side effects: prints `logins.choose_intro` and reads the password twice, unless `empty`.

    Args:
        empty: `--no-password`: the password is empty, nothing is asked.
        local: Whether the database is calu's local one (for the explanation).

    Returns:
        The password, possibly empty; the caller wipes it.

    Raises:
        CliError: `logins.MISMATCH` if the two answers differ; `logins.NOT_ENTERED` if the
            input ended.
    """
    if empty:
        return bytearray()
    print(logins.choose_intro(local))
    return read_password(logins.NEW_PROMPT, confirm=True)


def _save_login(store: CredentialStore, name: str, login: Target, password: bytearray) -> None:
    """Store a login under the user password, or without encryption for an empty one.

    Args:
        store: The credential store.
        name: `logins.USER` or `logins.DB_ADMIN`.
        login: The login.
        password: The user password; empty stores the entry protected by the file's mode.
    """
    secret = bytearray(login.dump())
    try:
        store.save(name, secret, password or None)
    finally:
        wipe(secret)


def _first_start_local(
    store: CredentialStore,
    port: int,
    data: str,
    password: bytearray,
    stop_with_server: bool = False,
) -> Target:
    """Create the local database: store the `db-admin` credential, then start the container.

    The credential is saved before the container starts, so that a start that fails can be
    repeated with `calu db up`: the generated owner password is not lost.

    Args:
        store: The credential store; it must hold no `db-admin` entry.
        port: The port on 127.0.0.1.
        data: An absolute directory prepared with `local_db.prepare_data_dir`, or
            `local_db.VOLUME_DATA`.
        password: The user password, which protects the entry; empty for none.
        stop_with_server: Whether the database stops with `calu serve` and stays stopped
            after a restart of the machine (`DockerBlock.stop_with_server`).

    Returns:
        The owner login.

    Raises:
        local_db.DockerError: If docker fails.
    """
    owner = local_db.owner_target(local_db.new_owner_password(), port, data, stop_with_server)
    _save_login(store, logins.DB_ADMIN, owner, password)
    assert isinstance(owner.auth, PasswordAuth)
    local_db.up(port, owner.auth.password, data)
    local_db.set_restart(stop_with_server)
    where = "the Docker volume calu-db_data" if data == local_db.VOLUME_DATA else data
    print(f"local database initialized in {where}, running on 127.0.0.1:{port}")
    return owner


def _new_database(args: argparse.Namespace, store: CredentialStore) -> tuple[Target, bytearray]:
    """Set up the database of the chosen variant, choose the user password, store `db-admin`.

    Args:
        args: Parsed arguments of `calu setup`.
        store: The credential store; it holds no `db-admin` entry.

    Returns:
        (the owner login, the new user password, possibly empty; the caller wipes it).

    Side effects: asks the questions of the variant and the new user password; stores
    `db-admin`; for the local database, starts its container.

    Raises:
        CliError: On an option that does not apply to the chosen variant
            (`_check_setup_options`), an invalid answer, a failed connection or a failed
            preflight, or a password that is not confirmed; nothing is stored then, except
            for the local database, whose credential is stored before its container starts.
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
        stops = choose_stop_with_server(args.stop_db_with_server)
        password = _choose_user_password(args.no_password, local=True)
        try:
            return _first_start_local(store, port, data, password, stops), password
        except BaseException:
            wipe(password)
            raise
    owner = _setup_server(args, store) if variant == "server" else _setup_rds(args, store)
    password = _choose_user_password(args.no_password, local=False)
    _save_login(store, logins.DB_ADMIN, owner, password)
    return owner, password


def _convert_old_store(
    args: argparse.Namespace, store: CredentialStore
) -> tuple[Target, bytearray]:
    """Open the owner login of a store written before D265, and choose the user password.

    The old `admin` entry is the database owner's login. Its password is asked once
    (`logins.OLD_ADMIN_PROMPT`); the person may keep it as the new user password, which is
    then asked at no further step. Nothing is stored or removed here: `_store_logins` does
    that once the database accepted the setup.

    Args:
        args: Parsed arguments of `calu setup` (`no_password`, `set_password`).
        store: The credential store; it holds `admin` and no `db-admin`.

    Returns:
        (the owner login, the user password, possibly empty; the caller wipes it).

    Raises:
        CliError: If the store has no `admin` entry (`logins.old_logins`), the input ended,
            every try was wrong, or a new password is not confirmed.
        TargetError: If the entry holds no database login this version can read.
    """
    if "admin" not in store:
        raise CliError(logins.old_logins(False))
    print(
        "This machine has the logins of an earlier calu (admin, session, operator). This "
        "setup replaces them by one calu user login."
    )
    owner = load_target(store, "admin", logins.OLD_ADMIN_PROMPT)
    old = _typed.pop("admin", "")
    if args.no_password:
        return owner, bytearray()
    if old and not args.set_password:
        answer = ask("Keep that password as your calu user password? yes/no", "yes", "")
        if answer.lower() in ("yes", "y"):
            return owner, bytearray(old.encode())
    return owner, _choose_user_password(False, local=owner.docker is not None)


def cmd_setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run `_setup`, then forget the passwords that were typed for it (`_typed`).

    Args:
        args: Parsed arguments of `calu setup`.
        store: The credential store.

    Raises:
        CliError: As `_setup`.
        CredentialError: If the store is corrupt.
        local_db.DockerError: If docker fails.
        psycopg.Error: On database errors.
    """
    try:
        _setup(args, store)
    finally:
        _typed.clear()


def _setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Set calu's database up: choose it on the first run, then migrate, seed and store logins.

    See agent/records/PLAN.md D265. Three starting points:

        no logins stored             ──► which database (`--database`: a local one in Docker,
                                         a server that exists, RDS with IAM); the new user
                                         password; `db-admin` is stored
        `db-admin` stored            ──► the user password opens it (none if it is empty);
                                         `--set-password` chooses a new one, `--no-password`
                                         an empty one
        logins of before D265        ──► the old admin password opens `admin` (the owner);
                                         it may be kept as the user password

    Then, on every run: the migrations, the default checkers, the password of the role
    `calu_admin` (the user password; with an empty one a generated password, kept if the
    stored `user` login has one already), the sandbox question (`_apply_sandbox_choice`),
    the offer to update outdated shipped instructions, and the models. Last, `user` and
    `db-admin` are stored under the user password (without one if it is empty), the entries
    of before D265 are removed, and as root the place file is written (`_store_logins`).

    Without `--checker-model` no model is stored: every checker, the narrator and the
    summarizer then use the default model (`calu.config.default_model`), and a database that
    is already set up keeps its models. `--checker-model M` sets M for every role;
    `--checker-model auto` picks one from the credentials this machine has.

    A new user password stops every login that used the old one: a running `calu serve` or
    `calu web` cannot reconnect after its connection drops and needs a restart.

    Args:
        args: Parsed arguments (`checker_model`, `database` and the options of its variants,
            `no_password`, `set_password`, `sandbox`, `update_instructions`).
        store: The credential store.

    Raises:
        CliError: If an answer or password is missing, wrong or not confirmed, the database
            cannot be reached or set up as it is, `--database` is given although a database
            is stored, the store holds a `user` login but no `db-admin` login, an option
            does not apply (`_check_setup_options`), `--checker-model auto` finds no
            provider's credentials, or the sandbox answer is neither on nor off.
        CredentialError: If the store is corrupt.
        local_db.DockerError: If docker fails.
        psycopg.Error: On database errors.
    """
    explicit = args.checker_model is not None
    model, effort, how = _chosen_model(args.checker_model, None) if explicit else ("", None, "")
    old = [name for name in logins.OLD_ENTRIES if name in store]
    if logins.USER in store and logins.DB_ADMIN not in store and not old:
        # A machine that was given the user login of a database set up elsewhere
        # (`calu credential set user`): setting up a new database here would hide that one.
        raise CliError(
            "this machine has only the user login of a database set up elsewhere, not the "
            "database admin's login that setup needs. Run `calu setup` where the database was "
            "set up, or store that login here with `calu credential set db-admin`; to set up "
            "a new database instead, `calu credential remove user` first"
        )
    if logins.DB_ADMIN in store or old:
        if args.database:
            raise CliError(
                "a database is set up already; leave out --database, or remove the stored "
                "logins first to set up another one (`calu credential list` shows them)"
            )
        _check_setup_options(args, None)
    if logins.DB_ADMIN in store:
        owner = _db_admin_target(store)
        if args.stop_db_with_server is not None:
            owner = _with_stop_with_server(owner, args.stop_db_with_server)
        if args.set_password or args.no_password:
            password = _choose_user_password(args.no_password, owner.docker is not None)
        else:
            if "user" not in _typed and logins.USER in store and store.needs_password(logins.USER):
                # A `db-admin` entry without a password next to a protected `user` entry:
                # the user password is kept, never dropped by a setup that did not ask.
                wipe(load_credential(store, logins.USER, logins.USER_PROMPT))
            password = bytearray(_typed.get("user", "").encode())
        _finish_local_start(owner)
    elif old:
        owner, password = _convert_old_store(args, store)
        _finish_local_start(owner)
    else:
        owner, password = _new_database(args, store)
    models = (model, effort, how) if explicit else None
    try:
        report, role_password, sandbox_on = asyncio.run(
            _setup_steps(args, store, owner, bytes(password).decode(), models)
        )
        print(report)
        _store_logins(store, owner, password, role_password, old)
    finally:
        wipe(password)
    _print_sandbox_notes([sandbox_on], store)
    print("next: `calu project add . --name NAME` in a project directory, then `calu run`")


def _with_stop_with_server(owner: Target, stops: bool) -> Target:
    """Change whether the stored local database stops with `calu serve` (PLAN.md D269).

    Side effects: sets the running container's restart policy (`local_db.set_restart`) and
    prints one line; the changed login is stored at the end of the setup (`_store_logins`).

    Args:
        owner: The stored owner login.
        stops: `--stop-db-with-server` (True) or `--no-stop-db-with-server` (False).

    Returns:
        The owner login with the setting changed.

    Raises:
        CliError: If the stored database is not calu's local one.
        local_db.DockerError: If docker refuses the change.
    """
    block = owner.docker
    if block is None:
        raise CliError(
            "--stop-db-with-server and --no-stop-db-with-server are for calu's local database; "
            f"{owner.place} is a server that calu does not start or stop"
        )
    local_db.set_restart(stops)
    print(
        "the local database "
        + ("stops when calu serve ends" if stops else "keeps running when calu serve ends")
    )
    return owner.model_copy(update={"docker": block.model_copy(update={"stop_with_server": stops})})


def _finish_local_start(owner: Target) -> None:
    """Start the local container an earlier setup stored but did not get running.

    An earlier setup may have stored the owner login and then failed to start the container
    (docker missing, the port taken): that first start is finished now.

    Args:
        owner: The owner login.

    Raises:
        local_db.DockerError: If docker fails.
    """
    block = owner.docker
    if (
        block is not None
        and isinstance(owner.auth, PasswordAuth)
        and local_db.mount_of(block.data) is not None
        and not local_db.initialized(block.data)
    ):
        local_db.up(block.port, owner.auth.password, block.data)
        print(f"local database initialized in {block.data}")


async def _setup_steps(
    args: argparse.Namespace,
    store: CredentialStore,
    owner: Target,
    password: str,
    models: tuple[str, str | None, str] | None,
) -> tuple[str, str, bool]:
    """Connect as the owner and run the database steps of `_setup`.

    Args:
        args: Parsed arguments (`sandbox`, `update_instructions`).
        store: The credential store, for a generated password of `calu_admin` to keep.
        owner: The owner login.
        password: The user password; "" for an empty one.
        models: (model, effort, how it was chosen) to store for every role, from
            `--checker-model`; None leaves the stored models as they are.

    Returns:
        (the report, the password `calu_admin` now has: the user password, or a generated
        one for an empty user password, whether Claude Code's sandbox is on).

    Raises:
        CliError: If the database cannot be set up as it is, or the sandbox answer is
            neither on nor off.
        psycopg.Error: On database errors.
    """
    conn = await open_stored(owner)
    try:
        db = Db(conn)
        fresh = not await _has_checker_types(db)
        try:
            new_types_model = models[0] if models is not None else ""
            report = await setup_database(conn, new_types_model, update_outdated=False)
        except SetupRefused as exc:
            raise CliError(f"calu cannot be set up in {owner.place} as it is:\n{exc}") from exc
        if password:
            role_password = password
            report += "\nuser login (calu_admin): its password is the calu user password"
        else:
            role_password = _kept_generated_password(store, owner) or generated_password()
            report += (
                "\nuser login (calu_admin): no user password, so calu asks for none; the "
                "role has a generated password, kept in the store"
            )
        await set_user_password(conn, role_password)
        enabled, line = await _apply_sandbox_choice(db, args.sandbox)
        report += f"\n{line}"
        for said in await _offer_instruction_updates(db, args.update_instructions):
            report += f"\n{said}"
        if models is not None:
            model, effort, how = models
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
        return report, role_password, enabled
    finally:
        await conn.close()


def _kept_generated_password(store: CredentialStore, owner: Target) -> str | None:
    """Find the generated password `calu_admin` got from an earlier setup with no password.

    Keeping it lets running servers and clients reconnect after a second `calu setup`.

    Args:
        store: The credential store.
        owner: The owner login, for the database's place.

    Returns:
        The password of the stored password-free `user` login of the same database, else
        that of root's place file; None if neither has one.
    """
    candidates: list[Target] = []
    try:
        if logins.USER in store and not store.needs_password(logins.USER):
            secret = store.load(logins.USER)
            try:
                candidates.append(Target.parse(secret))
            finally:
                wipe(secret)
    except CredentialError, OSError, ValueError:
        pass
    shared = _shared_user()
    if shared is not None:
        candidates.append(shared)
    for found in candidates:
        if found.place == owner.place and isinstance(found.auth, PasswordAuth):
            if found.auth.password:
                return found.auth.password
    return None


def _store_logins(
    store: CredentialStore,
    owner: Target,
    password: bytearray,
    role_password: str,
    old: Sequence[str],
) -> None:
    """Finish a setup: store `db-admin` and `user`, remove old entries, write the place file.

    Side effects: saves both entries under the user password (without encryption for an
    empty one); removes the entries of before D265 that the setup started with; as root,
    writes where the database is to `calu.db.place.PATH`, and, when the user password is
    empty, the generated password of `calu_admin` to `calu.db.place.login_path()`, readable
    only by the user who ran `sudo calu setup` (D279; an empty password is the owner's ruling
    of 2026-11-18). A failure of the place file is
    reported and does not fail the setup. Prints what was stored and, for an empty
    password, who can use the login.

    Args:
        store: The credential store.
        owner: The owner login.
        password: The user password; empty for none.
        role_password: The password `calu_admin` now has.
        old: The entries of before D265 that were stored when the setup started.

    Raises:
        CliError: If the owner login names a libpq service, from which the user login
            cannot be derived.
    """
    _save_login(store, logins.DB_ADMIN, owner, password)
    try:
        _save_login(store, logins.USER, user_login(owner, role_password), password)
    except ValueError as exc:  # TargetError: the owner login names a libpq service
        raise CliError(f"the user login could not be stored: {exc}") from exc
    protection = "the calu user password" if password else "no password (the file's mode)"
    print(f"stored the logins 'user' and 'db-admin' in {store.path}, protected by {protection}")
    removed = [name for name in old if store.remove(name)]
    if removed:
        print(f"removed the logins of the earlier calu: {', '.join(removed)}")
    root = os.geteuid() == 0
    if not password:
        if root:
            print(
                f"no password protects the logins in {store.path}: only root can read them, "
                "so `calu run --sudo`, `calu serve --sudo`, `calu attach --sudo` and `calu "
                "web --sudo` ask for none"
            )
            who = _setup_reader()
            readers = (
                f"uid {who[0]} can read: nothing but the checkers keeps that user's programs, "
                "Claude Code's commands included, from deciding escalations or changing the "
                "checkers"
                if who
                else "only root can read: commands without sudo find no login there"
            )
            print(
                f"WARNING: the user login is also in {db_place.login_path()}, which {readers}. "
                "`sudo calu setup --set-password` sets a password and removes that file.",
                file=sys.stderr,
            )
        else:
            print(f"WARNING: {logins.EMPTY_MEANS}", file=sys.stderr)
    if root:
        try:
            db_place.write(
                owner,
                admin_password=None if password else role_password,
                reader=_setup_reader(),
            )
        except (OSError, ValueError) as exc:
            print(f"note: {db_place.PATH} was not written ({exc})", file=sys.stderr)


def _print_sandbox_notes(sandbox_on: list[bool], store: CredentialStore) -> None:
    """Print `sandbox_setup_notes` to stderr, each as a WARNING line.

    Args:
        sandbox_on: Holds whether the sandbox is on; empty if setup did not get that far.
        store: The credential store, as the setup left it.
    """
    for note in sandbox_setup_notes(sandbox_on[0], store) if sandbox_on else []:
        print(f"WARNING: {note}", file=sys.stderr)


def cmd_credential(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, set or remove credentials.

    `set` stores a secret the user has, usually the connection string of the user login of
    a database another machine set up (`calu credential set user`). It asks for the
    password that protects it; an empty answer, or `--no-password`, stores it protected by
    the file's mode alone, and then every command that uses it asks nothing.

    Args:
        args: Parsed arguments (`action`, `name`, `no_password`).
        store: The credential store.

    Raises:
        CliError: If `name` is missing, a password confirmation does not match or the input
            ended, or `set` of a login entry (`LOGIN_ENTRIES`) was given a secret that is no
            database login (`Target.parse`).
        CredentialError: If the store is corrupt.
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
    secret = read_password("Connection string (not echoed): ")
    if args.name in LOGIN_ENTRIES:
        # Checked before the entry's password is asked: a typo in the string would otherwise
        # only show at the next command that uses the entry.
        try:
            Target.parse(secret)
        except TargetError as exc:
            wipe(secret)
            raise CliError(f"credential {args.name!r} not stored: {exc}") from None
    password = bytearray()
    protected = False
    try:
        if not args.no_password:
            if args.name in LOGIN_ENTRIES:
                print(logins.EMPTY_MEANS)
                password = read_password(logins.NEW_PROMPT, confirm=True)
            else:
                password = read_password(f"New password for {args.name!r}: ", confirm=True)
        protected = bool(password)
        store.save(args.name, secret, password or None)
    finally:
        wipe(secret)
        wipe(password)
    if not protected:
        print(
            f"stored credential {args.name!r} without a password: every command that uses it "
            "runs without a prompt, and only the file's mode protects it. Anyone who can read "
            f"{store.path} -- another process of your user, a backup, a synchronised dotfile "
            "directory -- has this credential. `calu credential set "
            f"{args.name}` again with a password puts one back on it."
        )
        return
    print(f"stored credential {args.name!r}")


LOGIN_ENTRIES = (logins.USER, logins.DB_ADMIN)
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


def _session_login(store: CredentialStore) -> bytearray:
    """Return the user login for a session process (`calu run`, `calu serve`).

    Side effects: clears the process's "dumpable" flag before the password is read, so that
    neither the password nor the login is ever in a process another process of this user can
    read; asks for the user password where the `user` entry has one (`_user_target`); starts
    the local database when that variant is configured and its port does not answer (the
    docker command gets no secret).

    Args:
        store: The credential store.

    Returns:
        The login, as the store holds it; the caller wipes it.

    Raises:
        CliError: As `_user_target`.
        local_db.DockerError: If the local database cannot be started.
    """
    harden_process()
    login = _user_target(store)
    url = bytearray(login.dump())
    try:
        # After the password, because where the local database is comes out of the decrypted
        # entry; before anything connects.
        block = login.docker
        if block is not None and local_db.ensure_running(block):
            print(f"started the local database on 127.0.0.1:{block.port}", flush=True)
    except BaseException:
        wipe(url)
        raise
    return url


def cmd_serve(args: argparse.Namespace, store: CredentialStore) -> None:
    """Answer session requests in the foreground until interrupted (agent/records/PLAN.md D208).

    Asks for the user password once; every session that `calu start` asks for afterwards
    runs in this process without a further password. With `--web` it also runs `calu web`
    as its child for as long as it runs (`_start_web_child`, PLAN.md D268): the same login,
    no second password, the child's lines on the same terminal; it ends with the server. A
    local database set up to stop with the server (`DockerBlock.stop_with_server`) is stopped
    when it ends, unless other calu processes use it (`_stop_db_after_serve`, D269).

    Args:
        args: Parsed arguments (`max_sessions`, `idle_timeout`, `user`, `sudo`, `secure`,
            `insecure`, `relay_port`, `no_relay`, `projects`, `web`, `web_port`).
        store: The credential store.

    Raises:
        CliError: If there is no user login, `--max-sessions` is below 1,
            `--idle-timeout` is not above 0, `--relay-port` is no port, the user cannot be
            switched (`_run_as`), the projects directory is refused
            (`calu.projects_dir.check_projects_dir`, before the password is asked), the
            database refused the user login, no relay port can be bound, `--web-port`
            is no port, or `--web-tls-cert`/`--web-tls-key` cannot be used (`_tls_files`).
        CredentialError: On a wrong password.
        OSError: If `calu web` cannot be started.
        psycopg.Error: On database errors at the start.
    """
    _check_security_flags(args)
    if args.web and not 1 <= args.web_port <= 65535:
        raise CliError(f"--web-port takes a number from 1 to 65535, not {args.web_port}")
    if (args.web_tls_cert or args.web_tls_key) and not args.web:
        raise CliError("--web-tls-cert and --web-tls-key belong to --web")
    # Checked here as well as in the child, so that a mistake shows before the password.
    web_tls = _tls_files(args.web_tls_cert, args.web_tls_key) if args.web else None
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
    block = Target.parse(url).docker
    web: subprocess.Popen[bytes] | None = None
    try:
        if args.web:
            web = _start_web_child(args.web_port, url, "user" in _typed, run_as, web_tls)
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
        if web is not None:
            _stop_web_child(web)
        if block is not None and block.stop_with_server:
            _stop_db_after_serve()
    if code:
        raise CliError("the server ended with an error (see the lines above and the log file)")


def _stop_db_after_serve() -> None:
    """Stop the local database that stops with `calu serve`, unless calu still uses it.

    See PLAN.md D269. Another calu process of this machine (`calu run`, another `calu serve`,
    `calu web`, an editor's `calu ide`, a client; `calu.machine.calu_processes`) keeps it
    running, with a line that names them. A failure of docker is reported, never raised:
    the server has ended either way.

    Side effects: `docker compose down` of the local database; prints one line.
    """
    others = machine.calu_processes(os.geteuid(), os.getpid())
    if others:
        pids = ", ".join(str(pid) for pid, _line in others)
        print(
            f"the local database keeps running: other calu processes use it (pid {pids})",
            file=sys.stderr,
        )
        return
    try:
        local_db.down()
    except local_db.DockerError as exc:
        print(f"the local database was not stopped: {exc}", file=sys.stderr)
        return
    print("stopped the local database (the next calu run or calu serve starts it)", flush=True)


WEB_CHILD_READY_S = 60.0
"""How long `calu serve --web` waits for its `calu web` to take its port and print its address."""

WEB_CHILD_STOP_S = 10.0
"""How long `calu serve --web` waits for its `calu web` to end after SIGTERM before SIGKILL."""


def _start_web_child(
    port: int,
    login: bytearray,
    typed: bool,
    run_as: RunAs | None,
    tls: tuple[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start `calu web` as a child of `calu serve --web`, with the login serve already holds.

    The login goes to the child through a pipe the child inherits (`calu web --login-fd`),
    never through the environment, the command line or a file. The child shares this
    process's stdout and stderr, so its address appears among serve's lines, and it gets the
    kernel's signal SIGTERM when this process dies, however it dies (`PR_SET_PDEATHSIG`). As
    root for a user (`run_as`) the child is told `--user`, so it does its file access as that
    user, as `calu web --sudo` does.

    Side effects: starts a process; writes the login into the pipe and closes it. No thread
    is started: a root server switches to its user's file identity next, which other threads
    would escape (`calu.asuser.enter` refuses then).

    Args:
        port: The port of the web interface on 127.0.0.1.
        login: The user login, as the store holds it; not wiped here.
        typed: Whether the user password was typed for it (then a browser signs in with
            it; else the child prints an address with a token).
        run_as: The user Claude Code runs as, when calu runs as root for one.
        tls: The certificate and key for HTTPS (`--web-tls-cert`, `--web-tls-key`), or None
            for HTTP; the child checks them.

    Returns:
        The child process, once it has taken its port and printed its address (it writes one
        byte into a second pipe, `--ready-fd`).

    Raises:
        OSError: If the process cannot be started.
        CliError: If it ends before it is ready (its reason is on stderr, which it shares
            with this process), or is not ready within `WEB_CHILD_READY_S`.
    """
    import select

    read_end, write_end = os.pipe()
    ready_read, ready_write = os.pipe()
    # `-I` and the working directory `/`: `python -m` would otherwise import a `calu` package or
    # any other module from the working directory first -- the projects directory, which the
    # user Claude Code runs as can write -- and this child starts as root (D279).
    argv = [
        sys.executable,
        "-I",
        "-m",
        "calu",
        "web",
        "--port",
        str(port),
        "--login-fd",
        str(read_end),
        "--ready-fd",
        str(ready_write),
    ]
    if typed:
        argv.append("--login-typed")
    if run_as is not None:
        argv += ["--user", run_as.user or str(run_as.uid)]
    if tls is not None:
        argv += ["--tls-cert", tls[0], "--tls-key", tls[1]]
    try:
        child = subprocess.Popen(
            argv, pass_fds=(read_end, ready_write), preexec_fn=_die_with_parent, cwd="/"
        )
    except BaseException:
        os.close(ready_read)
        raise
    finally:
        os.close(read_end)
        os.close(ready_write)
    try:
        with os.fdopen(write_end, "wb") as pipe:
            pipe.write(bytes(login))
        waiting, _, _ = select.select([ready_read], [], [], WEB_CHILD_READY_S)
        ready = bool(waiting) and os.read(ready_read, 1) == b"1"
    except BrokenPipeError:  # the child ended before it read the login
        ready = False
    finally:
        os.close(ready_read)
    if not ready:
        _stop_web_child(child)
        raise CliError(
            "calu web did not start (its reason is above); the server does not start "
            "without the web interface it was asked for"
        )
    print(
        f"started calu web (pid {child.pid}) on port {port}; it ends with this server", flush=True
    )
    return child


def _die_with_parent() -> None:
    """Ask the kernel to send this (child) process SIGTERM when its parent dies.

    Runs in the child between fork and exec (`subprocess.Popen(preexec_fn=...)`); Linux only
    (`prctl(PR_SET_PDEATHSIG)`). A failure leaves the child without that signal: serve's own
    `_stop_web_child` still ends it on every orderly exit.
    """
    import ctypes

    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # 1: PR_SET_PDEATHSIG


def _stop_web_child(child: subprocess.Popen[bytes]) -> None:
    """End the `calu web` child: SIGTERM, then SIGKILL after `WEB_CHILD_STOP_S`.

    Side effects: signals and reaps the process; prints a line if it had ended by itself
    before, with its exit code (0, 130 and the signals SIGINT and SIGTERM, which Ctrl-C
    gives the child too, are not reported).

    Args:
        child: The process `_start_web_child` started.
    """
    code = child.poll()
    if code is not None:
        if code not in (0, 130, -signal.SIGINT, -signal.SIGTERM):
            print(f"calu web had ended earlier (exit code {code})", file=sys.stderr)
        return
    child.terminate()
    try:
        child.wait(WEB_CHILD_STOP_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def cmd_start(args: argparse.Namespace, store: CredentialStore) -> None:
    """Ask the running `calu serve` for a session and attach to it (agent/records/PLAN.md D208).

    Side effects: writes a `session_requests` row and, unless `--no-attach`, a client token
    that is revoked when the client ends; prints the session's ids.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `prompt_file`, `resume`,
            `fork`, `fork_before_turn`, `model`, `effort`, `no_attach`, `plain`, `thinking`).
        store: The credential store (the user login).

    Raises:
        CliError: If the options contradict each other (also `--no-attach` with `--plain` or
            `--thinking`), no server answered, the server refused or failed to start the
            session, or the client ended with an error.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
        KeyboardInterrupt: On Ctrl-C while waiting for the server; the request was withdrawn
            first where no server had claimed it yet.
    """
    _token_from_file(args)
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

    endpoint, claude_id = _herd_value(store, work)
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

    Asks for the user password, decrypts the `user` credential, and hands it to
    `calu.run.run_session`, which wipes it before Claude Code starts.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `resume`, `fork`,
            `fork_before_turn`, `model`, `effort`, `batch`, `secure`, `insecure`).
        store: The credential store.

    Raises:
        CliError: If there is no user login, the directory does not exist, the
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


async def _with_admin(url: str | Target, work: Callable[[Db], Awaitable[str]]) -> str:
    """Connect with a login, run `work` and close the connection.

    Args:
        url: The login, or its connection string.
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


def _herd_command(store: CredentialStore, work: Callable[[Db], Awaitable[str]]) -> None:
    """Run a command with the user login (`_user_target`) and print its report.

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.

    Raises:
        CliError: As `_user_target`.
        psycopg.Error: On database errors.
    """
    print(asyncio.run(_with_admin(_user_target(store), work)))


def _admin_command(
    store: CredentialStore,
    work: Callable[[Db], Awaitable[str]],
    args: argparse.Namespace | None = None,
) -> None:
    """Run a command that may change settings with the user login and print its report.

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.
        args: The command's parsed arguments, for `_settings_login` (whether the command
            only reads).

    Raises:
        CliError: As `_user_target`.
        psycopg.Error: On database errors.
    """
    url = _settings_login(store, args)
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
            "a session elsewhere is reached with its address and its token in CALU_TOKEN "
            "through an SSH tunnel"
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
    """Keep the user login for a root client that found its session through a socket.

    A client that runs as root for a user (`_hold`) cannot read root's credential store after
    it switched to the user's file identity, so it keeps a login in memory (`_held`). Found
    through a root-owned socket it needed none to attach (D226); it takes one here only if
    it is there without a password (an empty user password: root's `user` entry, or the
    place file), and asks for nothing. Without one, the client attaches anyway and its
    database features (`/running`, `/switch`, `/new`, `/fork`, saving settings) are offered
    by `_hold_for_client`, which asks the password once.

    Side effects: may set `_held` and `_admin_place`; reads root's credential store.

    Args:
        store: Root's credential store.
    """
    if not _hold or _held:
        return
    target = _password_free_login(store) or _shared_user()
    if target is not None:
        _held[:] = [target]
        with contextlib.suppress(ValueError):
            _admin_place[:] = [user_login(target, "")]


def _password_free_login(store: CredentialStore) -> Target | None:
    """Return the `user` login if root's store keeps it without a password.

    An entry that needs a password, cannot be read or holds no usable login gives None (and
    a log line); the decrypted connection string is wiped.

    Args:
        store: Root's credential store.

    Returns:
        The login, or None.
    """
    try:
        if logins.USER not in store or store.needs_password(logins.USER):
            return None
        url = store.load(logins.USER)
    except (CredentialError, OSError) as exc:
        _log.info("the 'user' credential could not be read: %s", exc)
        return None
    try:
        return Target.parse(url)
    except ValueError as exc:
        _log.info("the 'user' credential holds no usable login: %s", exc)
        return None
    finally:
        wipe(url)


def _hold_for_client(store: CredentialStore, may_ask: bool) -> str | None:
    """Give a root client the user login it keeps for its database features (D214, D249).

    A client that runs as root for a user (`calu attach --sudo`, `calu start --sudo`) asks
    the user password at most once per run, before it switches to the user's file identity,
    and keeps the login in `_held`:

        a login is held already (`_user_target` ran)  ──► kept, nothing asked
        the `user` login without a password (root's
          store, or the place file)                   ──► that login, nothing asked
        may_ask, and root's store has `user` or the
          place file names the database               ──► the user password is asked, up to
                                                         `logins.MAX_ATTEMPTS` times: it
                                                         opens root's `user` entry, or the
                                                         database accepts it for `calu_admin`

    The login stays in this process's memory: it is not written anywhere, not put in an
    environment variable, and not given to the session or to a program the client starts.

    Side effects: may ask for the password on the terminal, open root's `user` entry with
    it or log in once as `calu_admin` to check it; sets `_held` and `_admin_place`; prints
    a line on stderr after a password that opens nothing.

    Args:
        store: Root's credential store, still readable (before `_become_client_of`).
        may_ask: Whether the password may be asked: True for the full-screen client, whose
            database features need it; False for the line client.

    Returns:
        None if the client holds the login, or holds none and asks none; otherwise why its
        database features are not available (skipped with Ctrl-D, or every try refused).
    """
    if _held:
        return None
    free = _password_free_login(store) or _shared_user()
    if free is not None:
        _held[:] = [free]
        with contextlib.suppress(ValueError):
            _admin_place[:] = [user_login(free, "")]
        return None
    if not may_ask or not (logins.USER in store or _place_or_none() is not None):
        return None
    for attempt in range(1, logins.MAX_ATTEMPTS + 1):
        try:
            password = read_password(logins.ONCE_PROMPT)
        except CliError:
            return SKIPPED_PASSWORD
        try:
            login = _login_for_password(store, password)
            blank = not password
        finally:
            wipe(password)
        if login is not None:
            _held[:] = [login]
            with contextlib.suppress(ValueError):
                _admin_place[:] = [user_login(login, "")]
            return None
        left = logins.MAX_ATTEMPTS - attempt
        if left:
            print(f"[client] {logins.wrong(left, blank)}", file=sys.stderr, flush=True)
    return REFUSED_PASSWORD


SKIPPED_PASSWORD = (
    "no password was given, so /running, /switch, /new, /fork and saving settings are not "
    "available in this client; `calu attach --sudo` again asks it once"
)
"""Why a root client offers no database features after Ctrl-D at the user password."""

REFUSED_PASSWORD = (
    f"the calu user password was not accepted {logins.MAX_ATTEMPTS} times, so /running, "
    "/switch, /new, /fork and saving settings are not available in this client; `calu attach "
    f"--sudo` again asks it once. {logins.FORGOT}"
)
"""Why a root client offers no database features after wrong user passwords."""


def _login_for_password(store: CredentialStore, password: bytearray) -> Target | None:
    """Turn the user password a root client was given into the login it keeps.

        it opens root's `user` entry              ──► that login
        the database accepts it for `calu_admin`
          where root's place file says            ──► that login (`is_user_password`)
        neither                                   ──► None

    Args:
        store: Root's credential store.
        password: The typed password; not wiped here.

    Returns:
        The login, or None for a password that opens nothing.
    """
    try:
        if logins.USER in store and store.needs_password(logins.USER):
            secret = store.load(logins.USER, password)
            try:
                return Target.parse(secret)
            finally:
                wipe(secret)
    except (CredentialError, OSError, ValueError) as exc:
        _log.info("the 'user' credential was not opened: %s", exc)
    place = _place_or_none()
    if place is None:
        return None
    text = bytes(password).decode()
    try:
        if asyncio.run(is_user_password(place, text)):
            return user_login(place, text)
    except (psycopg.Error, ValueError) as exc:
        _log.info("the user login could not be checked: %s", exc)
    return None


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
        store: The credential store (the user login; with an empty user password
            nothing is asked).
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

    return _herd_value(store, work, may_ask)


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
        _herd_value(store, work, may_ask)
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


def _herd_value[T](
    store: CredentialStore, work: Callable[[Db], Awaitable[T]], may_ask: bool = True
) -> T:
    """Run `work` with the user login (`_user_target`) and return its value.

    `_herd_command` prints what `work` returns, which is right for a report and useless for a
    caller that needs the value.

    Args:
        store: The credential store.
        work: Called with the connection.
        may_ask: Whether a password may be asked for. False for a process whose stdin carries
            something else -- `calu acp` speaks the editor's protocol on it, and
            `read_password` reads a line from stdin when it is not a terminal, so a prompt
            there would eat the editor's first request and call it the password. A protected
            `user` entry is then `NeedsPassword`, whose message says what to do instead.

    Returns:
        What `work` returned.

    Raises:
        NeedsPassword: If the password is needed and `may_ask` is False.
        CliError: As `_user_target`.
        psycopg.Error: On database errors.
    """
    why = (
        "its standard input carries the editor's protocol; a client can also be given the "
        "session's address and its token (CALU_TOKEN or --token-file), which needs no database"
    )
    login = _user_target(store, may_ask, why)
    return asyncio.run(_with_admin_value(login, work))


async def _with_admin_value[T](url: str | Target, work: Callable[[Db], Awaitable[T]]) -> T:
    """Connect, run `work`, close the connection and return its value.

    Args:
        url: The login, or its connection string.
        work: Called with the wrapped connection.

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
            then asks the user password at most once (`_hold_for_client`), does its file
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
    elif _held:
        # A root client's own login, complete: no dialog asks a password again (D249).
        database = _held[0]
    elif _admin_place:
        # The user password is asked when a change needs it, and not kept (D213).
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
        return ("CALU_TOKEN=<the same token> " if endpoint.token else "") + again
    if not given:
        return "calu attach"
    return ("CALU_TOKEN=<the same token> " if endpoint.token else "") + (
        f"calu attach {endpoint.address}"
    )


RELAY_CLIENT = (
    "this client reaches the session through the relay of `calu serve`: /new, /fork, /rewind, "
    "/running and /switch go through the relay; /rename and saving checker instructions work "
    "in a client on the server's machine (ssh -t <that host> calu attach)"
)
"""Why the full-screen client cannot change settings when it is attached through a relay."""


REMOTE_CLIENT = (
    "this client was given the session's address and this machine has no calu login: /new, "
    "/fork, /rewind, /running, /switch, /rename and saving checker instructions work in a "
    "client on the session's machine (ssh -t <that host> calu attach)"
)
"""Why the full-screen client cannot reach the database, when it was given a session's
address on a machine without a calu login: the remote way of attaching."""


def _admin_here(store: CredentialStore | None) -> bool:
    """Tell whether this machine's store holds the user login.

    Args:
        store: The credential store; None counts as one without it.

    Returns:
        True if the entry `user` is there; False without it or if the store cannot be read.
    """
    if store is None:
        return False
    try:
        return logins.USER in store
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
                f"shows the id); then `CALU_TOKEN=<token> calu attach {address}` (or "
                "--token-file). A session started with --insecure lets local clients in "
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
    """Fill in the password of the user login that root's place file publishes (D228).

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
    _token_from_file(args)
    if args.token and not args.address:
        raise CliError("--token names the session's address too, e.g. ws://127.0.0.1:8765")
    if args.address:
        # Without `--token` the client tries `CALU_TOKEN`, then none: a session started with
        # `--insecure` lets a local client in (D227); any other refuses, and on a terminal
        # the token is asked for (`_reach`).
        return _reach(Endpoint(args.address, args.token or ""), getattr(args, "session", None))
    return _find_endpoint(store, args.session, label, may_ask)


def _token_from_file(args: argparse.Namespace) -> None:
    """Read the token of `--token-file` into `args.token` (D279).

    A token on the command line (`--token`) can be read by every user of the machine in the
    process list; a file of mode 600, or `CALU_TOKEN` (the environment is the process owner's
    alone), cannot.

    Args:
        args: Parsed arguments with `token` and `token_file`; changed in place.

    Raises:
        CliError: If both `--token` and `--token-file` are given, or the file cannot be read
            or holds no token.
    """
    path = getattr(args, "token_file", None)
    if not path:
        return
    if args.token:
        raise CliError("give the token with --token-file or --token, not both")
    try:
        token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"--token-file {path} cannot be read: {exc}") from None
    if not token:
        raise CliError(f"--token-file {path} holds no token")
    args.token = token


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
    password: an editor gives this process no terminal, so this works with an empty user password
    (`calu setup --no-password`); otherwise give the address and the token.

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


def _web_token_line(
    host: str, port: int, token: str, has_admin_password: bool, scheme: str = "http"
) -> str:
    """Build the start line of `calu web` when the address carries the login token.

    Args:
        host: The host name the interface is opened under.
        port: Its port.
        token: The login token.
        has_admin_password: Whether a user password was typed for this command although
            the role `calu_admin` has no login (a schema before migration 0033): `calu
            setup` would make that password the browser's login. With an empty user
            password there is none, and the token is the only login there is.
        scheme: "https" with `--tls-cert`, else "http".

    Returns:
        One line: the address with the token, what the token is, and, only with a user
        password, how the password becomes the login.
    """
    line = (
        f"calu web on {scheme}://{host}:{port}/?token={token}"
        " (open it once per browser; the token is your login, keep it to yourself)."
    )
    if has_admin_password:
        line += " `calu setup` makes the calu user password the login instead."
    return line


def _web_sockets(port: int) -> list[socket.socket]:
    """Take the port of `calu web` on both loopback addresses, `127.0.0.1` and `::1` (D279).

    Browsers open `calu.localhost` on either address, Chrome on `::1` first. A program that
    listened on the address this server left free would be served under the same origin, with
    the login cookie, the browser's key and the password field. So both are taken, or the
    server does not start. A machine without IPv6 has no `::1` for anyone to take.

    Args:
        port: The port.

    Returns:
        The bound sockets (not listening yet: uvicorn starts that).

    Raises:
        CliError: If the port is taken on either address.
    """
    import errno

    bound: list[socket.socket] = []
    try:
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
            except OSError:  # no IPv6 in this kernel
                continue
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind((address, port))
            except OSError as exc:
                sock.close()
                if family == socket.AF_INET6 and exc.errno in (
                    errno.EADDRNOTAVAIL,
                    errno.EAFNOSUPPORT,
                ):
                    continue  # no IPv6 loopback address
                raise CliError(
                    f"port {port} on {address} cannot be used ({exc.strerror}): another "
                    "program listens there, and a browser could reach it instead of calu web; "
                    "choose another port with --port"
                ) from None
            bound.append(sock)
    except BaseException:
        for sock in bound:
            sock.close()
        raise
    return bound


def _tls_files(cert: str | None, key: str | None) -> tuple[str, str] | None:
    """Check the certificate and key of `calu web --tls-cert/--tls-key` before serving.

    TLS is optional: on the loopback interface the connection does not leave the machine. A
    certificate that browsers trust (e.g. one made with `mkcert calu.localhost`) avoids
    their warning; a self-signed one works after the browser was told to accept it.

    Side effects: reads both files (as the user the process works for); prints a note when
    the certificate does not name `calu.localhost`.

    Args:
        cert: The certificate file (PEM, with its chain), or None.
        key: Its private key (PEM, not encrypted), or None.

    Returns:
        (certificate, key) as absolute paths; None when neither was given.

    Raises:
        CliError: If only one is given, a file cannot be read, the key can be read by the
            group or others, or the two are no certificate and key that belong together.
    """
    import ssl

    if not cert and not key:
        return None
    if not (cert and key):
        raise CliError("--tls-cert and --tls-key go together: give both, or neither for HTTP")
    cert_path, key_path = Path(cert).expanduser().absolute(), Path(key).expanduser().absolute()
    try:
        mode = key_path.stat().st_mode
    except OSError as exc:
        raise CliError(f"--tls-key {key}: {exc.strerror}") from None
    if mode & 0o077:
        raise CliError(
            f"--tls-key {key} can be read by others (mode {oct(mode & 0o777)}); "
            f"`chmod 600 {key}` first"
        )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        context.load_cert_chain(cert_path, key_path, password=lambda: b"")
    except (OSError, ssl.SSLError) as exc:
        raise CliError(
            f"--tls-cert {cert} and --tls-key {key} cannot be used together ({exc}); the key "
            "must not be encrypted"
        ) from None
    if WEB_TLS_NAME not in _certificate_names(cert_path):
        print(
            f"note: {cert} does not name {WEB_TLS_NAME}; browsers will warn about it "
            f"(e.g. `mkcert {WEB_TLS_NAME}` makes one that names it)",
            flush=True,
        )
    return str(cert_path), str(key_path)


def _certificate_names(cert: Path) -> set[str]:
    """Return the DNS names a PEM certificate is valid for (its subject alternative names).

    Args:
        cert: The certificate file; its first certificate is read.

    Returns:
        The names; empty when there are none or the file cannot be parsed.
    """
    from cryptography import x509

    try:
        found = x509.load_pem_x509_certificate(cert.read_bytes())
        names = found.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except OSError, ValueError, x509.ExtensionNotFound:
        return set()
    return set(names.get_values_for_type(x509.DNSName))


WEB_TLS_NAME = "calu.localhost"
"""The name a certificate of `calu web` has to carry (`calu.web.app.WEB_HOST_NAME`)."""


def _pane_line(host: str, port: int, token: str | None, scheme: str = "http") -> str:
    """Build the line of `calu web` that names the chat pane (agent/records/PLAN.md D261).

    Args:
        host: The host name the interface is opened under.
        port: Its port.
        token: The login token to put into the address's fragment, for an iframe of another
            site (the browser never sends a fragment to a server); None when the browser signs
            in with the user password, which a frame of another site cannot use.
        scheme: "https" with `--tls-cert`, else "http".

    Returns:
        One line with the pane's address.
    """
    base = f"{scheme}://{host}:{port}/pane"
    if token is None:
        return (
            f"chat pane: {base} (in its own window or a frame of this interface, after signing "
            "in; add #dir=/path/to/project to start in a project)"
        )
    return (
        f"chat pane: {base}#token={token} (also in an iframe of a local app such as Jupyter; "
        "add &dir=/path/to/project to start in a project)"
    )


def _read_login_fd(fd: int) -> bytearray:
    """Read the login `calu serve --web` wrote into the pipe it gave this process.

    Side effect: closes the file descriptor.

    Args:
        fd: The read end of the pipe (`--login-fd`).

    Returns:
        The login as the store holds it; the caller wipes it.

    Raises:
        CliError: If the descriptor cannot be read or holds no login.
    """
    try:
        with os.fdopen(fd, "rb") as pipe:
            secret = bytearray(pipe.read())
    except OSError as exc:
        raise CliError(f"--login-fd {fd} cannot be read: {exc}") from exc
    try:
        Target.parse(secret)
    except ValueError as exc:
        wipe(secret)
        raise CliError(f"--login-fd {fd} holds no database login: {exc}") from exc
    return secret


def cmd_web(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve the web interface: every session of every host, their history, the checkers.

    It uses the user login (`_settings_login`), because it also changes what steers the
    checkers, and it serves the loopback interface only: a browser signs in with the user
    password (D216), or, with an empty user password, with the token in the address it prints;
    there are no accounts (agent/records/PLAN.md Q4), so reach it from another machine through
    an SSH tunnel.

    The address it prints names the host `calu.localhost` (`calu.web.app.WEB_HOST_NAME`), not
    the bound address: the server answers to no other name, so that the browser keeps the
    login apart from every other server on the loopback interface. The decrypted connection
    string stays in memory until the server stops, because a lost database connection is
    opened again with it (`calu.web.database.ReopeningDb`).

    Args:
        args: Parsed arguments (`host`, `port`, `user`, `sudo`, `tls_cert`, `tls_key`;
            `login_fd` and `login_typed` from `calu serve --web`, whose child this then is).
        store: The credential store.

    Raises:
        CliError: If the web dependencies are missing, the address is not local, the port is
            not between 1 and 65535, there is no user login (`_user_target`), or
            `--tls-cert`/`--tls-key` cannot be used (`_tls_files`).
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
    if args.login_fd is not None:
        # The child of `calu serve --web` (D268): the login comes from the server, through a
        # pipe only the two processes hold, and nothing is asked here.
        url = _read_login_fd(args.login_fd)
        typed_password = args.login_typed
    else:
        url = _settings_login(store, args)
        # With an empty user password nobody knows the password of `calu_admin`, so a
        # browser cannot be asked for it: the address carries the token instead (D228). A
        # password that was typed for this command is the user password, which the browser
        # is asked for.
        typed_password = "user" in _typed
    if run_as is not None:
        # As root for a user (`calu web --sudo`): the login was read from root's store and
        # stays in this process's memory; from here on the process touches files as the user.
        _become_client_of(run_as)
    # Read as the user the interface works for: the certificate and key are theirs.
    tls = _tls_files(args.tls_cert, args.tls_key)
    scheme = "https" if tls else "http"

    async def serve() -> None:
        """Take the port, open the connection, build the app and serve until interrupted."""
        import uvicorn

        # The port first, on both loopback addresses, and only then the address is printed.
        sockets = _web_sockets(args.port)
        # A connection the database closed (a restart, a failover, an idle timeout) is opened
        # again, so the server outlives the database's restarts.
        db = ReopeningDb(await open_stored(url.decode()), lambda: open_stored(url.decode()))
        try:
            values = await admin.settings(db)
            web_values = values.get("web") or {}
            template = str(web_values.get("git_url_template") or "")
            ancestors = web_values.get("pane_frame_ancestors") or None
            # The login token (D161): any local process could otherwise decide escalations,
            # Claude's own `curl` included. New on every start, never stored.
            token = secrets.token_urlsafe(24)
            # Where `calu_admin` has the user password, a browser logs in with it, which the
            # database checks (D216); the token is then not shown.
            place = Target.parse(url)
            by_password = typed_password and bool(
                await db.scalar("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (USER_ROLE,))
            )
            app = create_app(
                db,
                git_url_template=template,
                token=token,
                host_name=WEB_HOST_NAME,
                check_password=functools.partial(is_user_password, place) if by_password else None,
                store=store,
                pane_frame_ancestors=[str(a) for a in ancestors]
                if isinstance(ancestors, list)
                else None,
                tls=tls is not None,
            )
            # Flushed: this is the one line a user needs, and the process then serves for
            # hours. Without it the address sits in a buffer whenever stdout is a pipe -- a
            # log file, a systemd unit, `tmux pipe-pane` -- and the user sees nothing (D125).
            if by_password:
                print(
                    f"calu web on {scheme}://{WEB_HOST_NAME}:{args.port}/ (sign in with the "
                    "calu user password)",
                    flush=True,
                )
            else:
                print(
                    _web_token_line(WEB_HOST_NAME, args.port, token, typed_password, scheme),
                    flush=True,
                )
            print(
                _pane_line(WEB_HOST_NAME, args.port, None if by_password else token, scheme),
                flush=True,
            )
            config = uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level="warning",
                access_log=False,
                # A chat message may carry images (D220).
                ws_max_size=MAX_FRAME_BYTES,
                ssl_certfile=tls[0] if tls else None,
                ssl_keyfile=tls[1] if tls else None,
            )
            if args.ready_fd is not None:
                # `calu serve --web` waits for this byte (`_start_web_child`).
                os.write(args.ready_fd, b"1")
                os.close(args.ready_fd)
            await uvicorn.Server(config).serve(sockets=sockets)
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
        CliError: If there is no user login (`_user_target`).
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
        CliError: If `count` is below 1, or if there is no user login
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
        CliError: If there is no user login (`_user_target`).
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
    With `--db` the user login is used to ask whether the database answers and whether
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
        url = bytearray(_user_target(store).dump())
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
        CliError: If there is no user login or no editor is available.
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

    url = _settings_login(store, args)
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
        CliError: If there is no user login, or no
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
        CliError: If there is no user login, or the
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
        f"    CALU_TOKEN=<the token above> calu attach {local}"
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
        CliError: If there is no user login, the arguments do not fit the action, or the
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
            f"    CALU_TOKEN=<the token above> calu attach ws://127.0.0.1:{port}"
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
        CliError: If there is no user login, a case file cannot
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
        CliError: If there is no user login, the directory does not exist, the project
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
        CliError: If there is no user login, or the key or value is refused.
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
        CliError: If there is no user login, the checker is unknown, or the change is
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
            there is no user login.
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
        CliError: If there is no user login, the checker is unknown, or no editor is
            available.
        CredentialError: On a wrong password.
        admin.AdminError: If the edited instructions are refused (e.g. empty).
        psycopg.Error: On database errors.
    """
    url = _settings_login(store, args)
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
        CliError: If there is no user login, the list name is missing, or no editor is
            available.
        CredentialError: On a wrong password.
        admin.AdminError: If the list name is unknown or a pattern does not compile.
        psycopg.Error: On database errors.
    """
    if not args.target:
        raise CliError(f"rules edit needs a list name ({', '.join(admin.RULE_LISTS)})")
    scope = args.project or "global"
    url = _settings_login(store, args)
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

    The commands use the user login: a `user` certification made here rests on the user
    password (or, without one, on the protection of the credential store). `trust`,
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
    url = _settings_login(store, args)
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
        "it again, empty; asks for the user password and a typed confirmation",
    ).set_defaults(handler=cmd_db_reset)

    p = sub.add_parser(
        "setup",
        help="set the database up: choose it on the first run (a local one in Docker, an "
        "existing server, or RDS with IAM authentication), then migrate and store the user "
        "login (one password, the calu user password)",
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
    p.add_argument(
        "--user", help="server, rds-iam: the database user that administers calu's database"
    )
    p.add_argument(
        "--sslmode",
        choices=("disable", "allow", "prefer", "require", "verify-ca", "verify-full"),
        help="server: libpq sslmode; left out, a remote host gets `require`",
    )
    p.add_argument(
        "--keep-server-auth",
        action="store_true",
        default=None,
        help="server: if the server refuses calu's login requirements for a remote host "
        "(SCRAM, channel binding over TLS), keep its own method (md5, a pooler) without asking",
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
        "--no-password",
        action="store_true",
        help="an empty calu user password: calu never asks for one. The logins are stored "
        "protected by the store file's mode alone; as root the user login is also written to "
        "/var/lib/calu/database.json, which every user and program of the machine can read "
        "(Claude Code's commands included). Weak security by choice, for example for tests; "
        "`calu setup --set-password` puts a password back",
    )
    p.add_argument(
        "--stop-db-with-server",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="docker: stop the local database when `calu serve` ends and leave it stopped "
        "after a restart of the machine (or keep it running, the default), instead of the "
        "question; for a database set up already, changes the setting. `calu run` and "
        "`calu serve` start it either way",
    )
    p.add_argument(
        "--set-password",
        action="store_true",
        help="choose a new calu user password (the present one is asked first, if there is "
        "one); an empty answer means calu never asks. Servers and web interfaces that run "
        "need a restart afterwards",
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
    p.add_argument(
        "--web",
        action="store_true",
        help="also run the web interface (`calu web`) as a child of this server, with the "
        "same login and no second password; it prints its address here and ends with the "
        "server (Ctrl-C ends both)",
    )
    p.add_argument(
        "--web-port", type=int, default=8800, metavar="N", help="the port of --web (default: 8800)"
    )
    p.add_argument(
        "--web-tls-cert",
        metavar="FILE",
        help="--web serves HTTPS with this certificate (see `calu web --tls-cert`)",
    )
    p.add_argument("--web-tls-key", metavar="FILE", help="the private key of --web-tls-cert")
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
        "the client asks for it. On the command line every user of the machine can read it: "
        "prefer CALU_TOKEN or --token-file",
    )
    p.add_argument(
        "--token-file",
        metavar="FILE",
        help="read the token from this file instead (mode 600); unlike --token it does not "
        "appear in the process list, where every user of the machine can read it",
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
        "tries CALU_TOKEN, then none, which a session started with --insecure accepts. On the "
        "command line every user of the machine can read it: prefer CALU_TOKEN or --token-file",
    )
    p.add_argument(
        "--token-file",
        metavar="FILE",
        help="read the token from this file instead (mode 600); unlike --token it does not "
        "appear in the process list, where every user of the machine can read it",
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
        "tries CALU_TOKEN, then none, which a session started with --insecure accepts. On the "
        "command line every user of the machine can read it: prefer CALU_TOKEN or --token-file",
    )
    p.add_argument(
        "--token-file",
        metavar="FILE",
        help="read the token from this file instead (mode 600); unlike --token it does not "
        "appear in the process list, where every user of the machine can read it",
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
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="a loopback address (checked; the port is always taken on both 127.0.0.1 and ::1)",
    )
    p.add_argument("--port", type=int, default=8800)
    p.add_argument(
        "--sudo",
        action="store_true",
        help="run the web interface as root through sudo, for you: it reads calu's "
        "credentials in /etc/calu, and nothing Claude Code runs can reach it",
    )
    p.add_argument("--user", metavar="NAME|UID[:GID]", help="as root: the user to work for")
    p.add_argument(
        "--tls-cert",
        metavar="FILE",
        help="serve HTTPS with this certificate (PEM; it should name calu.localhost, e.g. from "
        "`mkcert calu.localhost`); needs --tls-key. Default: plain HTTP, which does not leave "
        "the machine",
    )
    p.add_argument(
        "--tls-key",
        metavar="FILE",
        help="the certificate's private key (PEM, not encrypted, mode 600)",
    )
    p.add_argument("--from-sudo", action="store_true", help=argparse.SUPPRESS)
    # `calu serve --web` hands its child the login through an inherited pipe (D268).
    p.add_argument("--login-fd", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--login-typed", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ready-fd", type=int, default=None, help=argparse.SUPPRESS)
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
    rules.set_defaults(handler=cmd_rules)

    p = sub.add_parser(
        "uninstall",
        help="remove calu from this machine: its logins, state, local database and "
        "installation (as root: the root installation, root's files and yours); keeps "
        "prerequisites and your projects. Asks you to type `remove calu`",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="only list what would be removed and what stays"
    )
    p.set_defaults(handler=cmd_uninstall)

    p = sub.add_parser("credential", help="manage the encrypted credential store")
    p.add_argument("action", choices=["list", "set", "remove"])
    p.add_argument(
        "name",
        nargs="?",
        help="the entry: `user` (the calu login), `db-admin` (the database owner's login), or "
        "another name for a secret of your own",
    )
    p.add_argument(
        "--no-password",
        action="store_true",
        help="store it protected by the file mode only, so no command asks for a password "
        "(prints what it costs)",
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
