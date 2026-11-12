"""Command line entry point `calu` (PLAN.md §8).

    calu db up [--port N]          start the local PostgreSQL (Docker); first start creates
                                   the `admin` credential
    calu db down                   stop the local PostgreSQL (data is kept)
    calu db migrate                apply schema migrations (asks for the admin password)
    calu db reset                  drop and recreate everything in the database; asks for the
                                   admin password and a typed confirmation
    calu setup [--checker-model M] migrate, load default checkers and rules, create the
                                   `session` credential (asks for both passwords)
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
    calu checker list|show|set|edit
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
import getpass
import json
import math
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import psycopg
from psycopg.conninfo import conninfo_to_dict

from calu import __version__, admin, doctor, herd
from calu.acp import serve_acp
from calu.attach import attach
from calu.config import (
    ANTHROPIC_HAIKU,
    BEDROCK_EU_HAIKU,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_NETWORK_POLICY,
    EMPTY_CHAT_SUMMARY,
    Config,
    load_secrets,
    merge_settings,
    user_env_file,
)
from calu.configdoc import setting_rows
from calu.credentials import CredentialError, CredentialStore, wipe
from calu.db import Db, connect, migrate
from calu.db import local as local_db
from calu.defaults.instructions import honesty_rules
from calu.evals.cases import case_line, cases_to_yaml
from calu.evals.cli import collect_cases, run_evals
from calu.honesty import GitError, content_ids, resolve_commit
from calu.model_choice import detect_model
from calu.paths import INSIDE_SESSION_ENV
from calu.protocol.client import RequestFailed
from calu.protocol.schema import protocol_schema
from calu.protocol.server import check_socket_path
from calu.run import RunOptions, default_socket_path, harden_process, run_session
from calu.setup import enable_operator_login, seed_defaults, setup
from calu.store import trust
from calu.store.projects import register_directory
from calu.tui import start as tui_start


class CliError(Exception):
    """An error to report to the user without a traceback (exit code 1)."""


def read_password(prompt: str, confirm: bool = False) -> bytearray:
    """Read a password from the terminal (no echo) or, if stdin is no terminal, from a line.

    Args:
        prompt: The prompt, e.g. "Admin password: ".
        confirm: Ask a second time and require both to match (for new passwords).

    Returns:
        The password; the caller should `wipe` it.

    Raises:
        CliError: If the password is empty or the confirmation does not match.
    """

    def ask(text: str) -> str:
        """Read one password.

        Args:
            text: The prompt.

        Returns:
            The password without the line end.
        """
        if sys.stdin.isatty():
            return getpass.getpass(text)
        return sys.stdin.readline().rstrip("\n")

    first = ask(prompt)
    if not first:
        raise CliError("empty password")
    if confirm and ask("Repeat: ") != first:
        raise CliError("passwords do not match")
    return bytearray(first.encode())


def load_credential(store: CredentialStore, name: str, prompt: str) -> bytearray:
    """Return the secret of credential `name`, asking for its password if it has one.

    An entry stored with `calu credential set <name> --no-password` is read without asking: it
    is protected by the file's mode rather than by a password, and prompting for a password
    that is then thrown away would teach the user that the prompt means nothing (PLAN.md D104).

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
        raise CliError(f"no credential {name!r}; run `calu db up` or `calu credential set {name}`")
    if not store.needs_password(name):
        return store.load(name)
    password = read_password(prompt)
    try:
        return store.load(name, password)
    finally:
        wipe(password)


def cmd_db_up(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start the local database; on the first start create the `admin` credential.

    Args:
        args: Parsed arguments (`port`).
        store: The credential store.

    Raises:
        CliError: If a first start finds an `admin` credential for another database.
        CredentialError: On a wrong password.
        local_db.DockerError: If docker fails.
    """
    if local_db.volume_exists():
        local_db.up(args.port)
        print(f"local database running on 127.0.0.1:{args.port}")
        return
    owner_password: str | None = None
    if "admin" in store:
        # The volume is gone but the credential exists (e.g. a failed first start): reuse it.
        secret = load_credential(store, "admin", "Admin password: ")
        try:
            owner_password = str(conninfo_to_dict(secret.decode()).get("password") or "")
        finally:
            wipe(secret)
        if not owner_password:
            raise CliError("the admin credential has no password; remove it first")
    else:
        print("First start: choose the admin password (it protects the database owner login).")
        password = read_password("New admin password: ", confirm=True)
        owner_password = local_db.new_owner_password()
        try:
            store.save("admin", local_db.owner_url(owner_password, args.port), password)
        finally:
            wipe(password)
    local_db.up(args.port, owner_password)
    print(f"local database initialized and running on 127.0.0.1:{args.port}")
    print("next: calu setup")


def cmd_db_down(args: argparse.Namespace, store: CredentialStore) -> None:
    """Stop the local database.

    Args:
        args: Parsed arguments (unused).
        store: The credential store (unused).

    Raises:
        local_db.DockerError: If docker fails.
    """
    local_db.down()
    print("local database stopped (data kept)")


async def _migrate(url: bytearray) -> int:
    """Apply migrations with the given owner connection string.

    Args:
        url: The owner connection string.

    Returns:
        The number of migrations applied.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await connect(url)
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
    conn = await connect(url)
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
    conn = await connect(url)
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


def cmd_setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the first-time setup and store the `session` credential.

    Without `--checker-model` no model is stored: every checker, the narrator and the
    summarizer then use the default model (`calu.config.default_model`: Haiku on the Anthropic
    API, or Haiku through AWS Bedrock in an EU region when `claude.use_bedrock` is on), and a
    database that is already set up keeps its models. `--checker-model M` sets M for every
    role; `--checker-model auto` picks one from the credentials this machine has
    (`calu.model_choice`).

    Args:
        args: Parsed arguments (`checker_model`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, a password is empty or mismatched, or
            `--checker-model auto` finds no provider's credentials.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    explicit = args.checker_model is not None
    model, effort, how = _chosen_model(args.checker_model, None) if explicit else ("", None, "")
    url = load_credential(store, "admin", "Admin password: ")
    try:
        admin_url = url.decode()

        async def run() -> tuple[str, str]:
            """Connect and run the setup steps.

            Returns:
                The report and the session connection string.
            """
            conn = await connect(admin_url)
            try:
                db = Db(conn)
                # Models are stored only when asked for; otherwise every role uses the default
                # model (D194), and a second `calu setup` leaves chosen models alone (D129).
                fresh = not await _has_checker_types(db)
                report, session_url = await setup(conn, admin_url, model)
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
                return report, session_url
            finally:
                await conn.close()

        print("Choose the session password (needed to start sessions with `calu run`).")
        session_password = read_password("New session password: ", confirm=True)
        try:
            report, session_url = asyncio.run(run())
            store.save("session", session_url, session_password)
        finally:
            wipe(session_password)
    finally:
        wipe(url)
    print(report)
    print("stored credential 'session'")


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
        conn = await connect(url.decode())
        try:
            return await enable_operator_login(conn, url.decode())
        finally:
            await conn.close()

    try:
        operator_url = asyncio.run(work())
    finally:
        wipe(url)
    print(
        "connection string for the operator role (this is the only copy):\n\n"
        f"    {operator_url}\n\n"
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
    (PLAN.md D48).

    `set --no-password` stores the connection string protected by the file's mode alone, so
    that the commands that use it ask nothing; it prints what that costs (PLAN.md D104). It is
    refused for the `session` credential, because starting a session is password protected
    (REQUIREMENTS 17, D115).

    Args:
        args: Parsed arguments (`action`, `name`, `no_password`).
        store: The credential store.

    Raises:
        CliError: If `name` is missing or unknown, a password is empty or mismatched, or
            `new` was asked for something other than the operator.
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


def cmd_run(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start a checked Claude Code session in the foreground.

    Asks for the session password, decrypts the `session` credential, and hands it to
    `calu.run.run_session`, which wipes it before Claude Code starts.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `resume`, `fork`,
            `fork_before_turn`, `model`, `effort`, `batch`, `thinking`).
        store: The credential store.

    Raises:
        CliError: If there is no `session` credential, the directory does not exist, the
            options contradict each other (`--fork` with `--resume` or with
            `--fork-before-turn`), `--resume` or `--fork` name no UUID, `--fork-before-turn`
            is below 2, or the session could not be started.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    workspace = Path(args.directory).resolve()
    if not workspace.is_dir():
        raise CliError(f"{workspace} is not a directory")
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CliError(f"cannot read {args.prompt_file}: {exc}") from exc
    options = RunOptions(
        workspace=workspace,
        project=args.project,
        prompt=prompt,
        resume_id=args.resume,
        fork_id=args.fork,
        fork_before_turn=args.fork_before_turn,
        socket_path=_socket_path(args.socket),
        model=args.model,
        effort=args.effort,
        interactive=not args.batch,
        show_thinking=args.thinking,
        background_timeout_s=args.background_timeout,
    )
    if args.background_timeout is not None and (
        not args.batch or not math.isfinite(args.background_timeout) or args.background_timeout <= 0
    ):
        raise CliError("--background-timeout needs --batch and a number of seconds above 0")
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
    if options.interactive and not options.prompt and sys.stdin.isatty():
        print("Type a message for Claude; /quit ends the session.")
    if "session" in store and not store.needs_password("session"):
        # REQUIREMENTS 17 on the read path as well (D115 guards the write path): an entry
        # without a password is unauthenticated, so whatever can write the credential file
        # could point the session at another database without anybody being asked anything.
        raise CliError(
            "the 'session' credential is stored without a password, and starting a session is "
            "password protected by design (REQUIREMENTS 17); store it again with "
            "`calu credential set session`"
        )
    # Before the password is read and the credential decrypted, so that neither is ever in a
    # process that another process of this user can read.
    harden_process()
    url = load_credential(store, "session", "Session password: ")
    try:
        code = asyncio.run(run_session(url, options))
    finally:
        wipe(url)
    if code:
        raise CliError("the session ended with an error (see the messages above)")


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
    conn = await connect(url)
    try:
        return await work(Db(conn))
    finally:
        await conn.close()


OPERATOR_FIRST = ("operator", "admin")
"""Credentials a read-and-resolve command tries, in order (PLAN.md D48).

`operator` is the `calu_operator` login: it reads every table and resolves an escalation and can
change nothing else, which is exactly what watching a herd needs. `admin` is the database owner
and works too, so a setup that never made an operator credential keeps working unchanged.
"""


def _herd_command(store: CredentialStore, work: Callable[[Db], Awaitable[str]]) -> None:
    """Run a read-and-resolve command with the narrowest credential available.

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
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


def _admin_command(store: CredentialStore, work: Callable[[Db], Awaitable[str]]) -> None:
    """Run a herd command with the `admin` credential and print its report.

    Args:
        store: The credential store.
        work: Called with the connection; its return value is printed.

    Raises:
        CliError: If there is no `admin` credential.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    url = load_credential(store, "admin", "Admin password: ")
    try:
        print(asyncio.run(_with_admin(url.decode(), work)))
    finally:
        wipe(url)


@dataclass(frozen=True)
class Endpoint:
    """Where a client should connect, and with which token (PLAN.md D105)."""

    address: str
    """`ws://host:port` or `unix:<path>`, as `sessions.address` holds it."""
    token: str
    """A client token for that session."""
    token_id: int | None = None
    """The id of a token minted for this one client, to be revoked when it disconnects; None
    for a token the user passed on the command line."""
    session_id: str = ""
    """Which session it belongs to, for messages."""


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
            "`calu sessions` lists the sessions of every host; a session elsewhere is reached "
            "with its address and `--token=<token>` through an SSH tunnel, and `calu run` "
            "starts one here"
        )
    # The nearest project directory wins: a session of a subproject is meant before the
    # session of the directory above it. Every candidate contains `here`, so of two candidate
    # directories one contains the other and the deepest is unique -- what can be left over is
    # several sessions of that one directory, which is a question for the user.
    deepest = max(len(Path(row.directory).parts) for row in matching)
    nearest = [row for row in matching if len(Path(row.directory).parts) == deepest]
    if len(nearest) > 1:
        lines = "\n".join(
            f"    {row.id}  started {row.started_at:%Y-%m-%d %H:%M:%S}" for row in nearest
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


def _find_endpoint(
    store: CredentialStore, hint: str | None, label: str, may_ask: bool = True
) -> Endpoint:
    """Find the local session a client should connect to and mint a token for it.

    This is the zero-argument path of `calu attach` and `calu acp` (PLAN.md D105): instead of
    the address and the 43-character token that `calu run` printed, the client asks the
    database which session runs in this directory on this host and mints its own token. The
    token is minted rather than read because only the hash of a token is stored, so a token
    printed once cannot be recovered; `calu attach` revokes the one it minted when it ends.

    Side effects: inserts a `client_tokens` row.

    Args:
        store: The credential store; the `operator` credential is enough, and with
            `calu credential set operator --no-password` (D104) nothing is asked.
        hint: `--session`, a session id or its beginning, or None for "this directory".
        label: What to write in the token's label, e.g. "calu attach".
        may_ask: Whether a password may be asked for; False for `calu acp`, whose standard
            input is the editor's protocol stream (see `_herd_value`).

    Returns:
        The endpoint, with `token_id` set to the minted token.

    Raises:
        CliError: If there is no database credential, or no single session matches.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> Endpoint:
        """Pick the session and mint a token.

        Args:
            db: The connection.

        Returns:
            The endpoint.
        """
        row = _pick_session(await herd.sessions(db, active_only=True), hint)
        token_id, token = await herd.mint_token(db, row.id, label)
        return Endpoint(row.address, token, token_id, row.id)

    return _herd_value(store, work, may_ask)


def _revoke(store: CredentialStore, token_id: int, may_ask: bool = True) -> None:
    """Revoke a token that `_find_endpoint` minted. Never raises.

    A client that ends should not leave a usable token behind, and `calu token list` should not
    fill up with one row per attach. A failure here is reported and ignored: the client's work
    is done, and the token stays visible in `calu token list` either way.

    Side effects: sets `revoked` on the `client_tokens` row; prints a line if that fails.

    Args:
        store: The credential store.
        token_id: The token to revoke.
        may_ask: Whether a password may be asked for; False for `calu acp`, where a prompt
            would read from the editor's protocol stream (see `_herd_value`). The token was
            minted without asking, so this only matters if the credential changed meanwhile.
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
    except (CliError, CredentialError, OSError, psycopg.Error) as exc:
        print(
            f"note: the client token {token_id} could not be revoked ({exc}); "
            f"`calu token revoke {token_id}` does it",
            file=sys.stderr,
        )


def _herd_value[T](
    store: CredentialStore, work: Callable[[Db], Awaitable[T]], may_ask: bool = True
) -> T:
    """Run `work` with the narrowest credential available and return its value.

    `_herd_command` prints what `work` returns, which is right for a report and useless for a
    caller that needs the value. Both choose the credential the same way (`OPERATOR_FIRST`).

    Args:
        store: The credential store.
        work: Called with the connection.
        may_ask: Whether a password may be asked for. False for a process whose stdin carries
            something else -- `calu acp` speaks the editor's protocol on it, and
            `read_password` reads a line from stdin when it is not a terminal, so a prompt
            there would eat the editor's first request and call it the password. An encrypted
            credential is then an error that says what to do instead.

    Returns:
        What `work` returned.

    Raises:
        CliError: If neither an `operator` nor an `admin` credential is stored, or if the one
            that is needs a password and `may_ask` is False.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    for name in OPERATOR_FIRST:
        if name in store:
            if not may_ask and store.needs_password(name):
                raise CliError(
                    f"the {name!r} credential is protected by a password, and this command has "
                    "no terminal to ask for one on -- its standard input carries the editor's "
                    "protocol. Either store it without a password (`calu credential set "
                    f"{name} --no-password`, which leaves the file's mode as its protection), "
                    "or give this command the session's address and `--token=<token>`, which "
                    "needs no database at all"
                )
            url = load_credential(store, name, f"{name.capitalize()} password: ")
            try:
                return asyncio.run(_with_admin_value(url.decode(), work))
            finally:
                wipe(url)
    raise CliError(
        "no credential 'operator' or 'admin'; `calu credential new operator` makes an operator "
        "connection string on the database machine, `calu credential set operator` stores it "
        "here. A client can also be given the address and `--token=<token>` that `calu run` "
        "printed, which needs no database at all"
    )


async def _with_admin_value[T](url: str, work: Callable[[Db], Awaitable[T]]) -> T:
    """Connect, run `work`, close the connection and return its value.

    Args:
        url: The connection string.
        work: Called with the wrapped connection.

    Returns:
        What `work` returned.

    Raises:
        psycopg.Error: On database errors.
    """
    conn = await connect(url)
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

    With an ADDRESS it needs no database credential: the session process authenticates the
    client with the token it printed at its start. Without one it finds the session running in
    this directory and mints its own token, which it revokes when it ends (PLAN.md D105).

    On a terminal this is the full-screen client (`calu.tui`, the extra `tui`); with `--plain`,
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
    endpoint = _endpoint_of(args, store, "calu attach")
    try:
        if tui_start.wants_full_screen(args.plain):
            # On a terminal: the full-screen client (`calu.tui`); it shows thinking by itself.
            ended = tui_start.run_tui(endpoint.address, endpoint.token, args.after_seq)
            print("\n".join(tui_start.exit_lines(ended)))
            code = ended.code
        else:
            code = asyncio.run(
                attach(endpoint.address, endpoint.token, args.after_seq, args.thinking)
            )
    except (OSError, RequestFailed) as exc:
        raise CliError(f"cannot attach to {endpoint.address}: {exc}") from exc
    finally:
        if endpoint.token_id is not None:
            _revoke(store, endpoint.token_id)
    if code:
        raise CliError("the client ended with an error")


def _endpoint_of(
    args: argparse.Namespace, store: CredentialStore, label: str, may_ask: bool = True
) -> Endpoint:
    """Return where a client should connect, from its arguments or from the database.

    An address on the command line is used as it is, with the token next to it; that is the
    remote path and it touches no database. Without an address the session of this directory is
    looked up and a token minted for this client (PLAN.md D105).

    Args:
        args: Parsed arguments (`address`, `token`, `session`).
        store: The credential store, used only for the lookup.
        label: The label of a minted token, e.g. "calu attach".
        may_ask: Whether a password may be asked for; False for `calu acp` (see
            `_herd_value`).

    Returns:
        The endpoint; `token_id` is set only when a token was minted here.

    Raises:
        CliError: If an address was given without a token, if a token was given without an
            address, or if no single session of this directory is running.
    """
    if args.address and not args.token:
        raise CliError(
            f"{args.address} needs the token of that session: `--token=<token>`. Without an "
            "address, the session running in this directory is found in the database and no "
            "token is needed"
        )
    if args.token and not args.address:
        raise CliError("--token names the session's address too, e.g. ws://127.0.0.1:8765")
    if args.address:
        return Endpoint(args.address, args.token)
    return _find_endpoint(store, args.session, label, may_ask)


def cmd_acp(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve a running session to an editor that speaks the Agent Client Protocol.

    The editor spawns this command and talks ACP on its stdin and stdout; stdout therefore
    carries protocol lines only and every message goes to stderr. With an ADDRESS it needs no
    database credential: the session authenticates the client with the token it printed.
    Without one -- which is what an editor is configured with, because it has neither an
    address nor a token to put in its arguments -- the session running in the editor's project
    directory is looked up and a token is minted for this adapter (PLAN.md D105). That lookup
    must not ask for a password: an editor gives this process no terminal, so the credential
    has to be stored with `calu credential set operator --no-password` (D104).

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
        raise CliError(f"cannot attach to {endpoint.address}: {exc}") from exc
    finally:
        if endpoint.token_id is not None:
            _revoke(store, endpoint.token_id, may_ask=False)
    if code:
        raise CliError("the adapter ended with an error")


def cmd_web(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve the web interface: every session of every host, their history, the checkers.

    It needs the `admin` credential, because it also changes what steers the checkers, and it
    serves the loopback interface only: its login is the token in the address it prints, there
    are no user accounts (PLAN.md Q4), so reach it from another machine through an SSH tunnel.

    The address it prints names the host `calu.localhost` (`calu.web.app.WEB_HOST_NAME`), not
    the bound address: the server answers to no other name, so that the browser keeps the
    login apart from every other server on the loopback interface. The decrypted connection
    string stays in memory until the server stops, because a lost database connection is
    opened again with it (`calu.web.database.ReopeningDb`).

    Args:
        args: Parsed arguments (`host`, `port`).
        store: The credential store.

    Raises:
        CliError: If the web dependencies are missing, the address is not local, or there is no
            `admin` credential.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    try:
        from calu.web.app import WEB_HOST_NAME, NotLocal, check_host, create_app
        from calu.web.database import ReopeningDb
    except ImportError as exc:  # the `web` extra is not installed
        raise CliError(
            "the web interface needs its extra dependencies: `uv tool install 'calu[web]'` "
            f"(or `uv sync --extra web` in a clone). {exc}"
        ) from exc
    try:
        check_host(args.host)
    except NotLocal as exc:
        raise CliError(str(exc)) from exc
    url = load_credential(store, "admin", "Admin password: ")

    async def serve() -> None:
        """Open the connection, build the app and serve until interrupted."""
        import uvicorn

        # A connection the database closed (a restart, a failover, an idle timeout) is opened
        # again, so the server outlives the database's restarts.
        db = ReopeningDb(await connect(url.decode()), lambda: connect(url.decode()))
        try:
            values = await admin.settings(db)
            template = str((values.get("web") or {}).get("git_url_template") or "")
            # The login token (D161): any local process could otherwise decide escalations,
            # Claude's own `curl` included. New on every start, never stored.
            token = secrets.token_urlsafe(24)
            app = create_app(db, git_url_template=template, token=token, host_name=WEB_HOST_NAME)
            # Flushed: this is the one line a user needs, and the process then serves for
            # hours. Without it the address sits in a buffer whenever stdout is a pipe -- a
            # log file, a systemd unit, `tmux pipe-pane` -- and the user sees nothing (D125).
            print(
                f"calu web on http://{WEB_HOST_NAME}:{args.port}/?token={token}"
                " (open it once per browser; the token is your login, keep it to yourself)",
                flush=True,
            )
            config = uvicorn.Config(
                app, host=args.host, port=args.port, log_level="warning", access_log=False
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
        rows = await herd.conversations(
            db, here, socket.gethostname(), getpass.getuser(), args.count
        )
        if not rows:
            return (
                f"no stored Claude session of {here} on this host for this user\n"
                "(a session is listed once its first turn has ended; `calu sessions --all` "
                "lists the sessions of every directory, host and user)"
            )
        lines = [row.line(width) for row in rows]
        lines.append("")
        lines.append("continue one:  calu run --resume <session id>")
        lines.append("copy one:      calu run --fork <session id>   (the original stays as it is)")
        if any(row.running for row in rows):
            lines.append("running:       a process still has that conversation; use calu attach")
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
    its schema is current. Nothing is changed.

    Args:
        args: Parsed arguments (`db`, `keys`, `directory`).
        store: The credential store.

    Raises:
        CliError: If something a session cannot run without is missing, after the whole report
            was printed — so the reader sees everything, not only the first problem.
        CredentialError: On a wrong password (only with `--db`).
    """
    findings = doctor.local_findings(store)
    if args.directory:
        findings += doctor.workspace_findings(Path(args.directory).resolve())
    if args.keys:
        findings += doctor.key_validity_findings()
        findings += doctor.aws_findings()
    if args.db:
        url = load_credential(store, "admin", "Admin password: ")
        try:
            findings += asyncio.run(doctor.database_findings(url.decode()))
        finally:
            wipe(url)
    # Flushed, so the report is on screen before the error line that may follow on stderr.
    print(doctor.report(findings), flush=True)
    if doctor.essential_missing(findings):
        raise CliError("something a session cannot run without is missing (see above)")


def cmd_summary(args: argparse.Namespace, store: CredentialStore) -> None:
    """Show or correct the chat summary a session's checkers read (PLAN.md D40).

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

        _admin_command(store, show)
        return

    url = load_credential(store, "admin", "Admin password: ")
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
    """Show everything needed to decide one escalation, from the database alone (PLAN.md D30).

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

    "answer" belongs to a question Claude asked (`AskUserQuestion`, PLAN.md D35): the note is
    the answer it receives, and the call itself does not run.

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
        if not await herd.resolve(db, args.id, args.decision, note):
            raise CliError(f"escalation {args.id} is not open (resolved, cancelled or unknown)")
        shown = f": {note}" if note else ""
        if args.decision == "cancel":
            return f"escalation {args.id}: cancelled{shown} (no decision was recorded)"
        return f"escalation {args.id}: {args.decision}{shown}"

    _herd_command(store, work)


def cmd_token(args: argparse.Namespace, store: CredentialStore) -> None:
    """Mint, list or revoke the client tokens of a session (PLAN.md D30).

    A minted token lets a client attach to that session without the terminal that started it:
    an editor's ACP adapter, a local command or a remote interface keeps its token in its own
    configuration, and revoking it ends that access. The session accepts a new token at the
    next `initialize`, without being restarted.

    Args:
        args: Parsed arguments (`action`, `session`, `label`, `id`).
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
        if args.action == "mint":
            if not args.session:
                raise CliError("calu token mint needs the id of a session (`calu sessions`)")
            try:
                token_id, token = await herd.mint_token(db, args.session, args.label or "")
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            return (
                f"token {token_id} for session {args.session}:\n\n    {token}\n\n"
                "This is the only copy; calu stores just its hash. Give it to one client and "
                f"end its access with `calu token revoke {token_id}`."
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
        return "\n".join(row.line() for row in rows) if rows else "no client tokens"

    _herd_command(store, work)


def cmd_evals(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the checker evaluation and print its report.

    Args:
        args: Parsed arguments (`files`, `from_db`, `no_builtin`, `model`, `repeat`,
            `directory`, `project`).
        store: The credential store.

    With `--list` or `--export` the cases are only collected: they are printed or written as
    YAML and no model is called, which is how the labels a herd produced are read, kept and
    later re-run as a fixed set (PLAN.md D55).

    Raises:
        CliError: If neither credential is stored (`operator` or `admin`), a case file cannot
            be read or written, or the run found a critical miss (a checker let a step through
            that it should not have).
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
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
        # path that replaces a training mode (PLAN.md D55, Q9).
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

    _admin_command(store, work)


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
    installation and says nothing about what a session will actually use. This answers the
    other question -- "what is set, what could be set, and where does each value come from"
    (PLAN.md D128). The keys and defaults come from `calu.configdoc`, the same walk the
    reference document is generated from.

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
            await admin.set_setting(db, args.key, admin.parse_value(args.value), args.project)
            return f"set {args.key} in {scope}"
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

    _admin_command(store, work)


def cmd_checker(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, show or change the checker definitions, or report how they have decided.

    `stats` reads what the checkers already decided and what humans did with it, so it costs
    nothing and describes this project's real steps rather than the eval set (PLAN.md D61).
    `misses` lists the rows those counts point at -- a step a checker approved that a human
    then denied -- with the escalation id, so `calu escalation <id>` shows the whole context
    and the instructions can be fixed against a real case (D62).

    `set --instructions-file` reads the new instructions from a file, or from stdin for "-";
    `edit` opens them in `$EDITOR` and stores them when the editor closes.

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
            # steps rather than the eval set (PLAN.md D61).
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
            return await admin.instructions(db, args.name)
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

    _admin_command(store, work)


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
    url = load_credential(store, "admin", "Admin password: ")
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
    url = load_credential(store, "admin", "Admin password: ")
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
    url = load_credential(store, "admin", "Admin password: ")
    try:
        admin_url = url.decode()

        async def current(db: Db) -> str:
            """Return the honesty rules in force for the scope.

            Args:
                db: The connection.

            Returns:
                The configured text of the most specific scope, or the shipped rules.
            """
            scopes = [args.project, None] if args.project else [None]
            for scope in scopes:
                value = (await admin.settings(db, scope)).get("honesty", {}).get("rules")
                if value:
                    return str(value)
            return honesty_rules()

        if args.action == "reset":

            async def reset(db: Db) -> str:
                """Remove the scope's `honesty.rules`.

                Args:
                    db: The connection.

                Returns:
                    The report.
                """
                await admin.unset_setting(db, "honesty.rules", args.project)
                return f"honesty.rules ({args.project or 'global'}) removed"

            print(asyncio.run(_with_admin(admin_url, reset)))
            return
        before = asyncio.run(_with_admin(admin_url, current))
        if args.action == "show":
            print(before, end="" if before.endswith("\n") else "\n")
            return
        after, path = edit_in_editor(before, "honesty-rules.md")
        if after == before:
            print("unchanged; nothing was stored")
            return

        async def write(db: Db) -> str:
            """Store the edited rules in the scope.

            Args:
                db: The connection.

            Returns:
                The report.
            """
            await admin.set_setting(db, "honesty.rules", after, args.project)
            return f"honesty.rules ({args.project or 'global'}) stored"

        try:
            print(asyncio.run(_with_admin(admin_url, write)))
        except admin.AdminError, psycopg.Error:
            print(f"the edited rules are kept in {path}", file=sys.stderr)
            raise
    finally:
        wipe(url)


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

    _admin_command(store, work)


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
    p.add_argument("--port", type=int, default=local_db.DEFAULT_PORT, help="host port")
    p.set_defaults(handler=cmd_db_up)
    db.add_parser("down", help="stop the local PostgreSQL").set_defaults(handler=cmd_db_down)
    db.add_parser("migrate", help="apply schema migrations").set_defaults(handler=cmd_db_migrate)
    db.add_parser(
        "reset",
        help="delete the whole database (sessions, rules, instructions, settings) and create "
        "it again, empty; asks for the admin password and a typed confirmation",
    ).set_defaults(handler=cmd_db_reset)

    p = sub.add_parser("setup", help="first-time setup of the database")
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
    p.add_argument("--thinking", action="store_true", help="also print Claude's thinking")
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("attach", help="terminal client for a running session")
    p.add_argument(
        "address",
        nargs="?",
        help="WebSocket address that `calu run` printed; left out, the session running in this "
        "directory is used",
    )
    p.add_argument("--token", help="client token that `calu run` printed (needed with an address)")
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
    p.set_defaults(handler=cmd_attach)

    p = sub.add_parser("acp", help="serve a running session to an ACP editor (stdin/stdout)")
    p.add_argument(
        "address",
        nargs="?",
        help="address of the session, e.g. ws://127.0.0.1:8765; left out, the session running "
        "in this directory is used, which is what an editor is configured with",
    )
    p.add_argument("--token", help="client token that `calu run` printed (needed with an address)")
    p.add_argument("--session", help="session id, when several run in this directory")
    p.set_defaults(handler=cmd_acp)

    p = sub.add_parser("web", help="the herd overview in a browser (127.0.0.1 only)")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (loopback only)")
    p.add_argument("--port", type=int, default=8800)
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

    token = sub.add_parser("token", help="client tokens of a session: mint, list, revoke")
    token.add_argument("action", choices=["mint", "list", "revoke"])
    token.add_argument(
        "session",
        nargs="?",
        help="session id for mint and list (`calu sessions`), token id for revoke",
    )
    token.add_argument("--label", default="", help='what the token is for, e.g. "zed on laptop"')
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

    checker = sub.add_parser("checker", help="checker definitions: instructions, model, limits")
    checker.add_argument("action", choices=["list", "show", "set", "edit", "stats", "misses"])
    checker.add_argument("name", nargs="?", help="checker name, e.g. command")
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
    return parser


def main(
    argv: list[str] | None = None, store_factory: Callable[[], CredentialStore] = CredentialStore
) -> int:
    """Run `calu` with the given arguments.

    Args:
        argv: Arguments without the program name; None uses `sys.argv[1:]`.
        store_factory: Creates the credential store (tests pass a store in a temporary dir).

    Side effects: loads calu's own env file (`~/.config/calu/.env`) into the process
    environment before any command runs, without overwriting what is already set. Every command
    then sees the same checker keys a session sees: only `calu run` used to read the file, so
    `calu doctor` reported "none of the known providers has a key" with a key in it, and
    `calu evals` ran its checkers without one (PLAN.md D131). The keys stay hidden from Claude
    Code, which only `calu run` starts and which removes them (`SdkRunner.hidden_env`).

    Returns:
        The exit code: 0 on success, 1 on a reported error.
    """
    args = build_parser().parse_args(argv)
    try:
        load_secrets()
        args.handler(args, store_factory())
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
        print(f"calu: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
