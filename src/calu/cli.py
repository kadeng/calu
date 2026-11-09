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
    calu sessions [--all]          list the sessions of all hosts
    calu escalations [SESSION]     list the open escalations
    calu resolve ID allow|deny [--note TEXT]
                                   resolve an escalation; the waiting session continues
    calu evals [FILE ...]          measure how well the configured checkers decide
    calu project add DIR|list      register a project in the database
    calu config get|set|unset KEY  configuration values (`claude.model`, `context.narrate`, ...)
    calu checker list|show|set     checker definitions: instructions, model, limits
    calu rules list [LIST] | add LIST PATTERN | remove|enable|disable ID
                                   the allow and deny lists

Passwords are read from the terminal without echo. If stdin is not a terminal, each password
is read as one line from stdin (for scripts and tests).
"""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from calu import __version__, admin, herd
from calu.attach import attach
from calu.credentials import CredentialError, CredentialStore, wipe
from calu.db import Db, connect, migrate
from calu.db import local as local_db
from calu.evals.cli import run_evals
from calu.protocol.client import RequestFailed
from calu.run import RunOptions, run_session
from calu.setup import DEFAULT_CHECKER_MODEL, setup


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


def cmd_credential(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, set or remove credentials.

    Args:
        args: Parsed arguments (`action`, `name`).
        store: The credential store.

    Raises:
        CliError: If `name` is missing or unknown, or a password is empty or mismatched.
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


def cmd_sessions(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the sessions of all hosts.

    Args:
        args: Parsed arguments (`all`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential.
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

    _admin_command(store, work)


def cmd_escalations(args: argparse.Namespace, store: CredentialStore) -> None:
    """List the open escalations.

    Args:
        args: Parsed arguments (`session`, `rendered`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential.
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

    _admin_command(store, work)


def cmd_resolve(args: argparse.Namespace, store: CredentialStore) -> None:
    """Allow or deny an open escalation; the waiting session continues.

    Args:
        args: Parsed arguments (`id`, `decision`, `note`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential or the escalation is not open.
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
        if not await herd.resolve(db, args.id, args.decision, args.note or ""):
            raise CliError(f"escalation {args.id} is not open (resolved, cancelled or unknown)")
        return f"escalation {args.id}: {args.decision}"

    _admin_command(store, work)


def cmd_evals(args: argparse.Namespace, store: CredentialStore) -> None:
    """Run the checker evaluation and print its report.

    Args:
        args: Parsed arguments (`files`, `from_db`, `no_builtin`, `model`, `repeat`,
            `directory`, `project`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, a case file cannot be read, or the run
            found a critical miss (a checker let a step through that it should not have).
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """
    workspace = Path(args.directory).resolve()
    critical = 0

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

    _admin_command(store, work)
    if critical:
        raise CliError(f"{critical} critical miss(es): a checker let a step through")


def cmd_project(args: argparse.Namespace, store: CredentialStore) -> None:
    """Register a project or list the registered ones.

    Args:
        args: Parsed arguments (`action`, `directory`, `name`, `git_remote`, `network`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, the directory does not exist, or the
            project exists already.
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
        """Add or list projects.

        Args:
            db: The connection.

        Returns:
            The report.
        """
        if args.action == "list":
            rows = await admin.projects(db)
            return "\n".join(row.line() for row in rows) or "no projects"
        row = await admin.add_project(db, name or "", args.git_remote, args.network)
        return f"added project {row.name!r}; use it with `calu run --project {row.name}`"

    _admin_command(store, work)


def cmd_config(args: argparse.Namespace, store: CredentialStore) -> None:
    """Read or change configuration values of the global or a project scope.

    Args:
        args: Parsed arguments (`action`, `key`, `value`, `project`).
        store: The credential store.

    Raises:
        CliError: If there is no `admin` credential, or the key or value is refused.
        CredentialError: On a wrong password.
        psycopg.Error: On database errors.
    """

    async def work(db: Db) -> str:
        """Read or change one value.

        Args:
            db: The connection.

        Returns:
            The report.

        Raises:
            CliError: If `set` was called without a value.
        """
        if args.action == "get":
            values = await admin.settings(db, args.project)
            if args.key:
                for part in args.key.split("."):
                    values = values.get(part, {}) if isinstance(values, dict) else {}
            return json.dumps(values, indent=2, sort_keys=True)
        if args.action == "set":
            if args.value is None:
                raise CliError("config set needs a KEY and a VALUE")
            await admin.set_setting(db, args.key, admin.parse_value(args.value), args.project)
            return f"set {args.key} in the {args.project or 'global'} scope"
        await admin.unset_setting(db, args.key, args.project)
        return f"unset {args.key} in the {args.project or 'global'} scope"

    _admin_command(store, work)


def cmd_checker(args: argparse.Namespace, store: CredentialStore) -> None:
    """List, show or change the checker definitions.

    `set --instructions-file` reads the new instructions from a file, or from stdin for "-".

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
    if args.action != "list" and not args.name:
        raise CliError(f"checker {args.action} needs a NAME")
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

    p = sub.add_parser("sessions", help="list the sessions of all hosts")
    p.add_argument("--all", action="store_true", help="also show sessions that have ended")
    p.set_defaults(handler=cmd_sessions)

    p = sub.add_parser("escalations", help="list the open escalations")
    p.add_argument("session", nargs="?", help="only escalations of this session id")
    p.add_argument("--rendered", action="store_true", help="also print the rendered step")
    p.set_defaults(handler=cmd_escalations)

    p = sub.add_parser("resolve", help="allow or deny an open escalation")
    p.add_argument("id", type=int, help="escalation id")
    p.add_argument("decision", choices=["allow", "deny"])
    p.add_argument("--note", help="text for Claude and for the record")
    p.set_defaults(handler=cmd_resolve)

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
    p.set_defaults(handler=cmd_evals)

    project = sub.add_parser("project", help="register a project in the database")
    project.add_argument("action", choices=["add", "list"])
    project.add_argument("directory", nargs="?", help="project directory (for add)")
    project.add_argument("--name", help="project name (default: the directory's name)")
    project.add_argument("--git-remote", help="git remote of the project")
    project.add_argument(
        "--network",
        choices=list(admin.NETWORK_POLICIES),
        default="full",
        help="network policy of the sandbox (default: full)",
    )
    project.set_defaults(handler=cmd_project)

    config = sub.add_parser("config", help="configuration values in the database")
    config.add_argument("action", choices=["get", "set", "unset"])
    config.add_argument("key", nargs="?", default="", help="dotted key, e.g. claude.model")
    config.add_argument("value", nargs="?", help="value (JSON, or plain text)")
    config.add_argument("--project", help="the project's scope instead of the global one")
    config.set_defaults(handler=cmd_config)

    checker = sub.add_parser("checker", help="checker definitions: instructions, model, limits")
    checker.add_argument("action", choices=["list", "show", "set"])
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
    checker.set_defaults(handler=cmd_checker)

    rules = sub.add_parser(
        "rules",
        help="the allow and deny lists",
        description="calu rules list [LIST] | add LIST PATTERN | remove|enable|disable ID",
    )
    rules.add_argument("action", choices=["list", "add", "remove", "enable", "disable"])
    rules.add_argument(
        "target", nargs="?", help="list name (list, add) or rule id (remove, enable, disable)"
    )
    rules.add_argument("pattern", nargs="?", help="regular expression (for add)")
    rules.add_argument("--comment", help="why the rule exists")
    rules.add_argument("--project", help="the project's rules instead of the global ones")
    rules.add_argument("--position", type=int, help="position in the list (default: append)")
    rules.set_defaults(handler=cmd_rules)

    p = sub.add_parser("credential", help="manage the encrypted credential store")
    p.add_argument("action", choices=["list", "set", "remove"])
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
