"""Command line entry point `calu` (PLAN.md §8).

    calu db up [--port N]          start the local PostgreSQL (Docker); first start creates
                                   the `admin` credential
    calu db down                   stop the local PostgreSQL (data is kept)
    calu db migrate                apply schema migrations (asks for the admin password)
    calu setup [--checker-model M] migrate, load default checkers and rules, create the
                                   `session` credential (asks for both passwords)
    calu credential list|set NAME|remove NAME
                                   manage the encrypted credential store
    calu run [DIR]                 start a checked Claude Code session in DIR (asks for the
                                   session password; see `calu.run`)
    calu attach ADDRESS --token T  terminal client for a running session
    calu acp ADDRESS --token T     serve the session to an ACP editor (stdin/stdout)
    calu escalation ID             everything needed to decide it, from the database
    calu summary ID [--edit]       the conversation the checkers read, folded
    calu doctor [--db]             what is in place, and what a session would miss
    calu token mint SESSION        client token for an editor or a remote interface
    calu web                       the herd overview in a browser (127.0.0.1 only)
    calu protocol [--out FILE]     JSON Schema of the client protocol, to generate client types
    calu sessions [--all]          list the sessions of all hosts
    calu escalations [SESSION]     list the open escalations
    calu resolve ID allow|deny [--note TEXT]
                                   resolve an escalation; the waiting session continues
    calu evals [FILE ...]          measure how well the configured checkers decide
    calu project add DIR|list      register a project in the database
    calu config get|set|unset KEY  configuration values (`claude.model`, `context.narrate`, ...)
    calu checker list|show|set|edit
                                   checker definitions; `edit NAME` opens the instructions in
                                   $EDITOR and stores them on save
    calu rules list [LIST] | add LIST PATTERN | edit LIST | remove|enable|disable ID
                                   the allow and deny lists (`edit` opens one in $EDITOR)

Passwords are read from the terminal without echo. If stdin is not a terminal, each password
is read as one line from stdin (for scripts and tests).
"""

import argparse
import asyncio
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import psycopg
from psycopg.conninfo import conninfo_to_dict

from calu import __version__, admin, doctor, herd
from calu.acp import serve_acp
from calu.attach import attach
from calu.config import DEFAULT_ALLOWED_DOMAINS, DEFAULT_NETWORK_POLICY, EMPTY_CHAT_SUMMARY
from calu.credentials import CredentialError, CredentialStore, wipe
from calu.db import Db, connect, migrate
from calu.db import local as local_db
from calu.evals.cases import case_line, cases_to_yaml
from calu.evals.cli import collect_cases, run_evals
from calu.protocol.client import RequestFailed
from calu.protocol.schema import protocol_schema
from calu.protocol.server import check_socket_path
from calu.run import RunOptions, default_socket_path, run_session
from calu.setup import DEFAULT_CHECKER_MODEL, enable_operator_login, setup


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
    """Ask for a password and decrypt the credential `name`.

    Args:
        store: The credential store.
        name: The credential name.
        prompt: The password prompt.

    Returns:
        The decrypted secret; the caller should `wipe` it.

    Raises:
        CliError: If the credential does not exist.
        CredentialError: If the password is wrong or the store is corrupt.
    """
    if name not in store:
        raise CliError(f"no credential {name!r}; run `calu db up` or `calu credential set {name}`")
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


def cmd_setup(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the first-time setup and store the `session` credential.

    Args:
        args: Parsed arguments (`checker_model`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential or a password is empty or mismatched.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
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
                return await setup(conn, admin_url, args.checker_model)
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

    Args:
        args: Parsed arguments (`action`, `name`).
        store: The credential store.

    Raises:
        CliError: If `name` is missing or unknown, a password is empty or mismatched, or
            `new` was asked for something other than the operator.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors (for `new`).
    """
    if args.action == "list":
        for name in store.names():
            print(name)
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
    secret = read_password("Connection string (not echoed): ")
    password = read_password(f"New password for {args.name!r}: ", confirm=True)
    try:
        store.save(args.name, secret, password)
    finally:
        wipe(secret)
        wipe(password)
    print(f"stored credential {args.name!r}")


def cmd_run(args: argparse.Namespace, store: CredentialStore) -> None:
    """Start a checked Claude Code session in the foreground.

    Asks for the session password, decrypts the `session` credential, and hands it to
    `calu.run.run_session`, which wipes it before Claude Code starts.

    Args:
        args: Parsed arguments (`directory`, `project`, `prompt`, `resume`, `model`, `effort`,
            `batch`, `thinking`).
        store: The credential store.

    Raises:
        CliError: If there is no `session` credential, the directory does not exist, or the
            session could not be started.
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
        fork_before_turn=args.fork_before_turn,
        socket_path=_socket_path(args.socket),
        model=args.model,
        effort=args.effort,
        interactive=not args.batch,
        show_thinking=args.thinking,
    )
    if options.interactive and not options.prompt and sys.stdin.isatty():
        print("Type a message for Claude; /quit ends the session.")
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

    Needs no database credential: the session process authenticates the client with the token
    it printed at its start.

    Args:
        args: Parsed arguments (`address`, `token`, `after_seq`, `thinking`).
        store: The credential store (unused).

    Raises:
        CliError: If the address cannot be reached or the token is refused.
    """
    try:
        code = asyncio.run(attach(args.address, args.token, args.after_seq, args.thinking))
    except (OSError, RequestFailed) as exc:
        raise CliError(f"cannot attach to {args.address}: {exc}") from exc
    if code:
        raise CliError("the client ended with an error")


def cmd_acp(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve a running session to an editor that speaks the Agent Client Protocol.

    The editor spawns this command and talks ACP on its stdin and stdout; stdout therefore
    carries protocol lines only and every message goes to stderr. Needs no database credential,
    like `calu attach`: the session authenticates the client with the token it printed.

    Args:
        args: Parsed arguments (`address`, `token`).
        store: The credential store (unused).

    Raises:
        CliError: If the address cannot be reached or the token is refused.
    """
    try:
        code = asyncio.run(serve_acp(args.address, args.token))
    except (OSError, RequestFailed) as exc:
        raise CliError(f"cannot attach to {args.address}: {exc}") from exc
    if code:
        raise CliError("the adapter ended with an error")


def cmd_web(args: argparse.Namespace, store: CredentialStore) -> None:
    """Serve the web interface: every session of every host, their history, the checkers.

    It needs the `admin` credential, because it also changes what steers the checkers, and it
    serves the loopback interface only: there is no login yet (PLAN.md Q4), so reach it from
    another machine through an SSH tunnel.

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
        from calu.web.app import NotLocal, check_host, create_app
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

        conn = await connect(url.decode())
        try:
            db = Db(conn)
            values = await admin.settings(db)
            template = str((values.get("web") or {}).get("git_url_template") or "")
            app = create_app(db, git_url_template=template)
            print(f"calu web on http://{args.host}:{args.port} (no login: local only)")
            config = uvicorn.Config(
                app, host=args.host, port=args.port, log_level="warning", access_log=False
            )
            await uvicorn.Server(config).serve()
        finally:
            await conn.close()

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
    workspace. With `--db` the `admin` credential is used to ask whether the database answers
    and whether its schema is current. Nothing is changed.

    Args:
        args: Parsed arguments (`db`, `directory`).
        store: The credential store.

    Raises:
        CliError: If something a session cannot run without is missing, after the whole report
            was printed — so the reader sees everything, not only the first problem.
        CredentialError: On a wrong password (only with `--db`).
    """
    findings = doctor.local_findings(store)
    if args.directory:
        findings += doctor.workspace_findings(Path(args.directory).resolve())
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
            return "the chat summary was replaced; the session applies it at its next check"

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
    if args.action == "add" and not name:
        directory = Path(args.directory or ".").resolve()
        if not directory.is_dir():
            raise CliError(f"{directory} is not a directory")
        name = directory.name

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
        report = [
            f"added project {row.name!r}; use it with `calu run --project {row.name}`",
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


def cmd_config(args: argparse.Namespace, store: CredentialStore) -> None:
    """Read or change configuration values of the global or a project scope.

    `get`, `set` and `unset` work on whole values; `add` and `remove` change one entry of a
    list setting and keep the others, starting from the list the scope effectively uses
    (`calu.admin.add_to_setting`).

    Args:
        args: Parsed arguments (`action`, `key`, `value`, `project`).
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
    if args.action not in ("list", "stats") and not args.name:
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
        if args.action == "stats":
            # From the record, not by running anything: free, and about this project's real
            # steps rather than the eval set (PLAN.md D61).
            stats = await herd.checker_stats(db, args.days)
            if not stats:
                return "no checker has run yet"
            window = f" in the last {args.days} day(s)" if args.days else ""
            return "\n".join(
                [f"what the checkers decided{window}:", *(row.line() for row in stats)]
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


def cmd_rules(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the allow and deny lists, or add, remove, enable or disable one rule.

    `target` is the list name for `list` and `add`, and the rule id for `remove`, `enable` and
    `disable`.

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

    p = sub.add_parser("setup", help="first-time setup of the database")
    p.add_argument(
        "--checker-model",
        default=DEFAULT_CHECKER_MODEL,
        help=f"Pydantic AI model of new checker types (default: {DEFAULT_CHECKER_MODEL})",
    )
    p.set_defaults(handler=cmd_setup)

    p = sub.add_parser("run", help="start a checked Claude Code session")
    p.add_argument("directory", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--project", help="name of the project row whose settings and rules apply")
    p.add_argument("-p", "--prompt", help="first message for Claude")
    p.add_argument("--prompt-file", help="read the first message from this file")
    p.add_argument("--resume", help="Claude session id to resume")
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
    p.add_argument("--thinking", action="store_true", help="also print Claude's thinking")
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("attach", help="terminal client for a running session")
    p.add_argument("address", help="WebSocket address that `calu run` printed")
    p.add_argument("--token", required=True, help="client token that `calu run` printed")
    p.add_argument(
        "--after-seq", type=int, default=0, help="continue after this event sequence number"
    )
    p.add_argument("--thinking", action="store_true", help="also print Claude's thinking")
    p.set_defaults(handler=cmd_attach)

    p = sub.add_parser("acp", help="serve a running session to an ACP editor (stdin/stdout)")
    p.add_argument("address", help="WebSocket address of the session, e.g. ws://127.0.0.1:8765")
    p.add_argument("--token", required=True, help="client token that `calu run` printed")
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

    p = sub.add_parser("escalations", help="list the open escalations")
    p.add_argument("session", nargs="?", help="only escalations of this session id")
    p.add_argument("--rendered", action="store_true", help="also print the rendered step")
    p.set_defaults(handler=cmd_escalations)

    p = sub.add_parser("doctor", help="report what is in place and what a session would miss")
    p.add_argument("directory", nargs="?", help="also check this workspace")
    p.add_argument("--db", action="store_true", help="also ask the database (needs the password)")
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
    config.add_argument("action", choices=["get", "set", "unset", "add", "remove"])
    config.add_argument("key", nargs="?", default="", help="dotted key, e.g. claude.model")
    config.add_argument("value", nargs="?", help="value (JSON, or plain text)")
    config.epilog = (
        "add and remove change one entry of a list setting and keep the others, e.g. "
        "`calu config add claude.sandbox.deny_read ~/.ssh`; set replaces the whole list."
    )
    config.add_argument("--project", help="the project's scope instead of the global one")
    config.set_defaults(handler=cmd_config)

    checker = sub.add_parser("checker", help="checker definitions: instructions, model, limits")
    checker.add_argument("action", choices=["list", "show", "set", "edit", "stats"])
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
        help="for `stats`: only count the checks of the last N days (default: all of them)",
    )
    checker.set_defaults(handler=cmd_checker)

    rules = sub.add_parser(
        "rules",
        help="the allow and deny lists",
        description=(
            "calu rules list [LIST] | add LIST PATTERN | edit LIST | remove|enable|disable ID"
        ),
    )
    rules.add_argument("action", choices=["list", "add", "edit", "remove", "enable", "disable"])
    rules.add_argument(
        "target", nargs="?", help="list name (list, add) or rule id (remove, enable, disable)"
    )
    rules.add_argument("pattern", nargs="?", help="regular expression (for add)")
    rules.add_argument("--comment", help="why the rule exists")
    rules.add_argument("--project", help="the project's rules instead of the global ones")
    rules.add_argument("--position", type=int, help="position in the list (default: append)")
    rules.set_defaults(handler=cmd_rules)

    p = sub.add_parser("credential", help="manage the encrypted credential store")
    p.add_argument("action", choices=["list", "set", "remove", "new"])
    p.add_argument("name", nargs="?")
    p.set_defaults(handler=cmd_credential)
    return parser


def main(
    argv: list[str] | None = None, store_factory: Callable[[], CredentialStore] = CredentialStore
) -> int:
    """Run `calu` with the given arguments.

    Args:
        argv: Arguments without the program name; None uses `sys.argv[1:]`.
        store_factory: Creates the credential store (tests pass a store in a temporary dir).

    Returns:
        The exit code: 0 on success, 1 on a reported error.
    """
    args = build_parser().parse_args(argv)
    try:
        args.handler(args, store_factory())
    except (
        CliError,
        CredentialError,
        admin.AdminError,
        local_db.DockerError,
        psycopg.Error,
        ValueError,
    ) as exc:
        print(f"calu: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
