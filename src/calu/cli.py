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

Passwords are read from the terminal without echo. If stdin is not a terminal, each password
is read as one line from stdin (for scripts and tests).
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from calu import __version__
from calu.credentials import CredentialError, CredentialStore, wipe
from calu.db import connect, migrate
from calu.db import local as local_db
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
    except (CliError, CredentialError, local_db.DockerError, psycopg.Error, ValueError) as exc:
        print(f"calu: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
