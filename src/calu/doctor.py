"""`calu doctor`: what is in place, and what a session would miss.

The start-up warnings of `calu.preflight` only reach someone who already got a session to run.
This answers the question before that: is `claude` there, can the sandbox be built, where do the
credentials live, which checker keys are set, and — with the user login — does the
database answer and is its schema current.

    calu doctor               # everything that needs no password
    calu doctor --db          # ... and the database, with the user login
    calu doctor --user ada    # as root: also what Claude Code would find as that user
    calu doctor --sudo        # the same for you, through sudo

Nothing here changes anything: it reads, and it says what to do about what it finds.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from calu import __version__, admin, asuser, launch, logins, preflight
from calu.changes import default_root
from calu.config import PROVIDER_KEYS_BY_PREFIX, SETTINGS_KEY, Config, user_env_file
from calu.credentials import CredentialStore
from calu.db import Db, applied_versions, migrations
from calu.db import place as db_place
from calu.db.connection import open_stored as connect
from calu.db.target import PasswordAuth
from calu.launch import RunAs

VERSION_TIMEOUT_S = 10.0
"""How long `claude --version` may take before it is reported as not answering."""


@dataclass(frozen=True)
class Finding:
    """One line of the report.

    `ok` False marks something that has to be fixed for the matching feature to work; `advice`
    says what to do about it. `essential` marks what no session can run without.
    """

    name: str
    ok: bool
    detail: str
    advice: str = ""
    essential: bool = False

    def line(self) -> str:
        """Render the finding as one terminal line.

        Returns:
            A mark, the name, what was found and, when something is wrong, the advice.
        """
        mark = "ok  " if self.ok else ("MISSING" if self.essential else "note")
        tail = f"\n      {self.advice}" if self.advice and not self.ok else ""
        return f"{mark:7} {self.name}: {self.detail}{tail}"


def claude_executable() -> str | None:
    """Return the Claude Code executable a session starts when `claude.cli_path` is not set.

    The same lookup as the session's (`calu.launch.claude_path`): the executable bundled with
    the Agent SDK, else the `claude` on `PATH`. A check that looked at `PATH` alone called
    Claude Code missing on a machine whose sessions run the bundled one, for example in the
    bridge an editor starts, whose `PATH` has no `~/.local/bin`.

    Returns:
        The path, or None when neither exists.
    """
    try:
        return launch.claude_path(None)
    except launch.LaunchError:
        return None


def claude_version() -> tuple[bool, str]:
    """Ask the Claude Code CLI for its version.

    Returns:
        (whether it answered, the version or the reason it did not). Never raises: a missing or
        broken CLI is a finding, not an error.
    """
    path = claude_executable()
    if path is None:
        return False, "not bundled with the Agent SDK and not on PATH"
    try:
        done = subprocess.run(  # a fixed program, no shell
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            check=False,
            **asuser.child(),  # as the user calu works for; a no-op without --user
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{path} did not answer ({type(exc).__name__})"
    if done.returncode != 0:
        return False, f"{path} exited {done.returncode}"
    return True, f"{done.stdout.strip()} ({path})"


def tool_findings() -> list[Finding]:
    """Check the programs calu starts or depends on.

    Returns:
        One finding for `claude`, the two sandbox programs and `git`.
    """
    answered, detail = claude_version()
    found = [
        Finding(
            "claude",
            answered,
            detail,
            "Install it (https://claude.ai/install.sh) and run `claude` once in a project to "
            "sign in and trust the folder; calu runs it exactly as it is set up there.",
            essential=True,
        )
    ]
    for tool, why in (
        ("bwrap", "the sandbox's namespaces"),
        ("socat", "the sandbox's network proxy"),
    ):
        path = shutil.which(tool)
        found.append(
            Finding(
                tool,
                path is not None,
                path or f"not on PATH, and the sandbox needs it for {why}",
                "apt install bubblewrap socat — or switch the sandbox off with "
                "`calu config set claude.sandbox.enabled false`, at the cost of its protection.",
            )
        )
    git = shutil.which("git")
    found.append(
        Finding(
            "git",
            git is not None,
            git or "not on PATH",
            "apt install git — the changeset checker and the write sets of a step need it.",
        )
    )
    return found + namespace_findings()


def namespace_findings() -> list[Finding]:
    """Check that bubblewrap can actually create a sandbox here, as the session's user.

    `bwrap` on `PATH` says nothing about the kernel: in a Docker container without extra
    rights, and on a distribution that keeps user namespaces from unprivileged programs,
    every sandboxed command fails although the program is installed (run in the root box,
    2026-11-18; `claude sandbox status` does not notice it either). This starts the smallest
    sandbox, `bwrap --ro-bind / / true`.

    Returns:
        One finding when bubblewrap is installed and the start failed, with what it printed;
        [] when it worked, when bubblewrap is missing (`tool_findings` says so) or when the
        attempt itself could not be made.
    """
    bwrap = shutil.which("bwrap")
    true = shutil.which("true")
    if bwrap is None or true is None:
        return []
    try:
        done = subprocess.run(  # fixed programs, no shell
            [bwrap, "--ro-bind", "/", "/", true],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            check=False,
            **asuser.child(),  # as the user calu works for; a no-op without --user
        )
    except OSError, subprocess.SubprocessError:
        return []
    if done.returncode == 0:
        return []
    said = " ".join((done.stderr or done.stdout).split())[:200]
    return [
        Finding(
            "sandbox start",
            False,
            f"bubblewrap is installed but cannot create a sandbox here ({said})",
            "With the sandbox on (the default), every command Claude runs fails. A container "
            "needs the right to create user namespaces; a distribution that restricts them "
            "needs its setting for bubblewrap. Or switch the sandbox off with `calu config "
            "set claude.sandbox.enabled false`, at the cost of its protection.",
        )
    ]


def credential_findings(store: CredentialStore) -> list[Finding]:
    """Check the encrypted credential store.

    Args:
        store: The store to inspect; nothing is decrypted, only the names and the kind of
            protection are read.

    Returns:
        One finding for the user login (`calu.logins.USER`) and one for the database owner's
        (`calu.logins.DB_ADMIN`); one more when the store holds the user login and still
        the logins of before D265, which are unused then (without the user login, the user
        login's advice says what to do with them, `calu.logins.old_logins`); and one when
        the `user` or `db-admin` entry is stored without a password (an empty user
        password), which says so even when that is the choice: in a store that root alone
        can read (`calu.asuser.only_root_writes`) as a plain
        statement, elsewhere with what it costs. An empty store on a machine whose calu runs
        as root (a trustworthy place file, `calu.db.place`) gives one line instead: this
        user needs no credential of their own.
    """
    names = set(store.names())
    if not names:
        try:
            root_db = db_place.read()
        except db_place.PlaceError:
            root_db = None
        if root_db is not None:
            return [
                Finding(
                    "credentials",
                    True,
                    f"none in this account; calu runs as root here (database {root_db.place}). "
                    "`calu attach --sudo` needs none; the editor and your own `calu attach` "
                    "ask for the calu user password",
                )
            ]
    old = [name for name in logins.OLD_ENTRIES if name in names]
    found = [
        Finding(
            "calu login ('user')",
            logins.USER in names,
            "stored" if logins.USER in names else "not stored",
            logins.old_logins("admin" in old) if old else logins.no_login(),
            essential=True,
        ),
        Finding(
            "database admin login ('db-admin')",
            logins.DB_ADMIN in names or logins.USER in names,
            "stored"
            if logins.DB_ADMIN in names
            else "not stored here; migrations run where it is"
            if logins.USER in names
            else "not stored",
            "`calu setup` stores it with the user login.",
        ),
    ]
    if old and logins.USER in names:
        found.append(
            Finding(
                "logins of an earlier calu",
                False,
                f"{', '.join(old)} still stored, unused",
                f"`calu credential remove NAME` removes each ({', '.join(old)}).",
            )
        )
    unprotected = sorted(
        name for name in names & {logins.USER, logins.DB_ADMIN} if not store.needs_password(name)
    )
    if unprotected:
        if asuser.only_root_writes(store.path):
            # `sudo calu setup --no-password` (D228): the store is root's alone, which is
            # what protects these entries; nothing Claude Code runs as its user reads it.
            found.append(
                Finding(
                    "credential protection",
                    True,
                    f"{', '.join(unprotected)} stored without a password; only root can "
                    f"read {store.path}",
                )
            )
        else:
            found.append(
                Finding(
                    "credential protection",
                    True,
                    f"{', '.join(unprotected)} stored without a password (an empty calu user "
                    "password): calu asks for none, and every program that runs as you can "
                    "use the login; `calu setup --set-password` sets a password",
                )
            )
    return found


def self_approval_findings(store: CredentialStore) -> list[Finding]:
    """Report a password-free credential on a machine where the sandbox cannot run (D164).

    The start-up warning of D162 says the same for a session; doctor runs before any password
    is entered, so it cannot read whether the sandbox is switched on, only whether it can run
    here at all (`bwrap` and `socat`). Without it, any program Claude runs can read a
    credential that has no password and resolve its own escalations.

    Args:
        store: The credential store; only names and kinds of protection are read.

    Returns:
        One failed finding for that combination, or []: also when the store is one that only
        root can read (`calu.asuser.only_root_writes`), which Claude Code's user cannot. "Cannot
        run" covers a missing program and a bubblewrap that the kernel refuses
        (`namespace_findings`).
    """
    unprotected = sorted(name for name in store.names() if not store.needs_password(name))
    if not unprotected or asuser.only_root_writes(store.path):
        # A store only root reads is out of reach of what Claude Code runs as its user.
        return []
    missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool) is None]
    if missing:
        why = f"{' and '.join(missing)} missing"
    elif namespace_findings():
        why = "bubblewrap is installed but cannot create a sandbox"
    else:
        return []
    return [
        Finding(
            "self-approval",
            False,
            f"{', '.join(unprotected)} {'has' if len(unprotected) == 1 else 'have'} no password "
            f"and the sandbox cannot run here ({why})",
            "Any program Claude runs could read "
            f"{'it' if len(unprotected) == 1 else 'them'} and resolve its own escalations. "
            "Make the sandbox work (bubblewrap and socat, and the right to create user "
            f"namespaces), or put passwords back ({preflight.password_commands(unprotected)}).",
        )
    ]


def key_findings(environ: Sequence[str] | None = None) -> list[Finding]:
    """Check which model provider keys are available to the checkers.

    Args:
        environ: Names to treat as set; None uses the environment.

    Returns:
        One finding naming the providers whose keys are present, and one for the env file.
    """
    present = (
        set(environ)
        if environ is not None
        else {name for name in os.environ if os.environ.get(name)}
    )
    have = sorted(
        prefix
        for prefix, names in PROVIDER_KEYS_BY_PREFIX.items()
        if any(name in present for name in names)
    )
    env_file = user_env_file()
    return [
        Finding(
            "checker keys",
            bool(have),
            (
                f"{', '.join(have)} (present; `calu doctor --keys` asks whether they are accepted)"
                if have
                else "none of the known providers has a key"
            ),
            f"Put one in {env_file}, e.g. ANTHROPIC_API_KEY (an Anthropic subscription "
            "with API credits covers it) or MISTRAL_API_KEY. Without a key every check fails "
            "and escalates to you.",
        ),
        Finding(
            "env file",
            env_file.is_file(),
            str(env_file) if env_file.is_file() else f"{env_file} does not exist",
            "Optional: calu also reads the keys from its own environment.",
        ),
    ]


def key_value_findings(environ: Mapping[str, str] | None = None) -> list[Finding]:
    r"""Check that no provider key carries leading or trailing whitespace.

    A key with a stray carriage return is what a `.env` written on Windows gives a shell that
    sources it (`. .env` keeps the `\r`), and what a Windows environment variable can carry.
    The provider then refuses the request at the HTTP layer, and the error a user sees is
    "Connection error." with nothing about a carriage return -- measured while writing this
    check, against Fireworks. calu's own env file is safe (python-dotenv strips the line end);
    this is about a key handed to calu from the outside.

    Args:
        environ: Variables to inspect; None uses the environment.

    Returns:
        One finding per provider key whose value is not stripped, or an empty list when every
        key is clean (nothing is reported when all is well, because this is a trap and not a
        setting). The findings are not marked essential: a session with such a key starts and
        runs, but every check of that provider's checkers fails and escalates.
    """
    values = os.environ if environ is None else environ
    names = [name for names in PROVIDER_KEYS_BY_PREFIX.values() for name in names]
    found: list[Finding] = []
    for name in names:
        value = values.get(name)
        if value is None or value == value.strip():
            continue
        what = (
            "carries a carriage return"
            if value.rstrip("\r\n") != value
            else "carries leading or trailing whitespace"
        )
        found.append(
            Finding(
                f"{name} value",
                False,
                what,
                f'Every model call will fail with a bare "Connection error.". Set {name} '
                "without it -- a `.env` written on Windows does this to a shell that sources "
                "it; `tr -d '\\r' < .env > .env.unix` repairs the file.",
            )
        )
    return found


def place_findings(owner: int = 0) -> list[Finding]:
    """Report what the place file of root's database gives away (D228).

    Args:
        owner: The uid that must own the file (root; a test passes its own).

    Returns:
        [] if there is no place file. One finding otherwise: a note when the file cannot be
        trusted (clients then ignore it), a note when it holds the password of the role
        `calu_admin` (`calu setup --no-password`: every user and every program of the
        machine can change calu's settings), else a line that says where root's database is.
    """
    path = db_place.default_path()
    if not path.exists() and not path.is_symlink():
        return []
    try:
        shared = db_place.read(path, owner)
    except db_place.PlaceError as exc:
        return [
            Finding(
                "place file",
                False,
                f"clients do not use it: {exc}",
                "`sudo calu setup` writes it again.",
            )
        ]
    if isinstance(shared.auth, PasswordAuth) and shared.auth.password:
        return [
            Finding(
                "settings login",
                False,
                f"{path} holds the password of the role that changes calu's settings, and "
                "every user and every program of this machine can read it (`calu setup "
                "--no-password`)",
                "Nothing but the checkers then keeps a program, Claude Code's commands "
                "included, from changing the checkers. `sudo calu setup` without "
                "--no-password gives the role a new password and takes it out of the file.",
            )
        ]
    return [Finding("place file", True, f"{path} names {shared.place} and holds no secret")]


def user_findings(run_as: RunAs, state_root: Path = asuser.STATE_ROOT) -> list[Finding]:
    """Check what calu as root needs to run Claude Code as another user (D206).

    Call it before `calu.asuser.enter`: it looks at calu's installation and at root's
    directories. Nothing is created.

    Args:
        run_as: The user Claude Code is to run as (`calu.asuser.resolve`).
        state_root: The root of calu's state for other users.

    Returns:
        Findings for the user and its home directory, for calu's installation (only root may
        be able to change what runs as root) and for the launcher `calu-claude`.
    """
    who = run_as.user or f"uid {run_as.uid}"
    home = Path(run_as.home)
    identity = f"uid {run_as.uid}, gid {run_as.gid}"
    if run_as.groups:
        identity += f", groups {' '.join(str(group) for group in run_as.groups)}"
    if run_as.user is None:
        # A bare uid: calu creates the home at the first run (`calu.asuser.prepare`).
        exists = home.is_dir()
        user = Finding(
            "session user",
            True,
            f"{who} ({identity}); Claude Code's home is {home}"
            + ("" if exists else ", which calu creates at the first run"),
        )
    else:
        user = Finding(
            "session user",
            home.is_dir(),
            f"{who} ({identity}); home {home}" + ("" if home.is_dir() else " does not exist"),
            "Claude Code keeps its login and its conversations in that user's home "
            "directory; a number (`--user UID`) names a user without one.",
            essential=True,
        )
    state = asuser.state_home(run_as, state_root)
    script = Path(sys.executable).parent / "calu"
    problems = asuser.writable_by_others(
        [script, Path(sys.executable), Path(asuser.__file__).parent]
    )
    install = Finding(
        "installation",
        not problems,
        "only root can change it"
        if not problems
        else "can be changed without root: " + "; ".join(problems[:2]),
        "calu as root must not run code another user can edit, and `--sudo` refuses it. "
        f"Install calu for root: {asuser.INSTALL_HINT}",
    )
    try:
        launcher: Finding = Finding("launcher", True, str(launch.launcher_path()))
    except launch.LaunchError as exc:
        launcher = Finding(
            "launcher",
            False,
            str(exc),
            "Without it Claude Code cannot be started as another user.",
            essential=True,
        )
    return [
        user,
        Finding("state directory", True, f"{state} (snapshots; created at the first run)"),
        install,
        launcher,
    ]


def root_without_user_findings() -> list[Finding]:
    """Say what a report as root, without `--user`, did not look at.

    Returns:
        One note: sessions of a root calu need `--user`, and this report read root's own
        Claude Code settings, not those of the user Claude Code will run as.
    """
    return [
        Finding(
            "session user",
            False,
            "calu runs as root here, and this report looked at root's own account",
            "`calu run` and `calu serve` need `--user NAME` or `--user UID[:GID]` as root. "
            "`calu doctor --user NAME` also checks that user's side: its home directory, "
            "Claude Code's settings and sandbox as that user, and calu's installation.",
        )
    ]


def directory_findings() -> list[Finding]:
    """Check where calu puts its runtime files.

    Returns:
        Findings for the socket directory and the snapshot directory, both of which calu
        creates when it needs them.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return [
        Finding(
            "socket directory",
            bool(runtime),
            f"{runtime}/calu" if runtime else "XDG_RUNTIME_DIR is not set; /tmp is used instead",
            "Only matters for `calu run --socket`.",
        ),
        Finding("snapshot directory", True, str(default_root()), ""),
    ]


def extra_findings() -> list[Finding]:
    """Check that the packages of the web interface can be imported.

    They are ordinary dependencies of calu (no optional extra since 2026-11-13), so a missing
    one means a damaged installation. Every other command works without them, which is worth
    saying here rather than only when `calu web` is run.

    Returns:
        One finding for the web interface; never essential, because a session does not need it.
    """
    missing = [name for name in ("starlette", "jinja2", "uvicorn") if find_spec(name) is None]
    if missing:
        return [
            Finding(
                "web interface",
                False,
                f"its packages are not installed ({', '.join(missing)} missing)",
                "install calu again: `uv tool install --reinstall calu` (or `uv sync` in a "
                "clone); every other command works without it.",
            )
        ]
    return [Finding("web interface", True, "its packages are installed", "")]


async def database_findings(url: str) -> list[Finding]:
    """Check that the database answers and that its schema is current.

    Args:
        url: The connection string of the user login.

    Returns:
        Findings for the connection and for the migrations. A connection that fails is one
        finding and the migrations are not checked.

    Raises:
        nothing: a failure to connect is reported as a finding.
    """
    try:
        conn = await connect(url)
    except Exception as exc:
        return [
            Finding(
                "database",
                False,
                f"cannot connect ({type(exc).__name__}: {exc})",
                "`calu setup` sets a database up; for a server, check the stored `user` login.",
                essential=True,
            )
        ]
    try:
        applied = await applied_versions(conn)
        known = {migration.version for migration in migrations()}
        missing = sorted(known - applied)
        return [
            Finding("database", True, "answers", ""),
            Finding(
                "schema",
                not missing,
                "current" if not missing else f"{len(missing)} migration(s) not applied",
                "`calu db migrate` applies them.",
            ),
        ]
    finally:
        await conn.close()


async def instruction_findings(url: str) -> list[Finding]:
    """Compare the stored checker instructions and text settings with the shipped texts (D248).

    Args:
        url: The connection string of the user login.

    Returns:
        One finding per stored text: ok for the present shipped text; a note for an older
        shipped one, unchanged, with the command that updates it; ok with the remark that it
        differs for the user's own text, which is never replaced. Empty if the database
        cannot be reached or read (`database_findings` reports that).

    Raises:
        nothing: a failure to read is left to `database_findings`.
    """
    try:
        conn = await connect(url)
    except Exception:
        return []
    try:
        states = await admin.instruction_states(Db(conn))
    except Exception:
        return []
    finally:
        await conn.close()
    found = []
    for state in states:
        kind = state.status.kind
        found.append(
            Finding(
                f"checker {state.checker}"
                if state.checker
                else state.key + (f" of project {state.project}" if state.project else ""),
                kind != "outdated",
                state.sentence() if kind != "current" else "the text calu ships now",
                f"{state.update_command} replaces it; nothing of yours is in it",
            )
        )
    return found


async def stored_sandbox(url: str) -> bool | None:
    """Read `claude.sandbox.enabled` from the global settings of the database.

    Args:
        url: The connection string of the user login.

    Returns:
        The stored value; True (the default) if the settings hold none; None if the database
        cannot be reached or read (`database_findings` reports that).
    """
    try:
        conn = await connect(url)
    except Exception:
        return None
    try:
        cur = await conn.execute(
            "SELECT value FROM config WHERE scope = 'global' AND scope_id = '' AND key = %s",
            (SETTINGS_KEY,),
        )
        row = await cur.fetchone()
    except Exception:
        return None
    finally:
        await conn.close()
    stored = row[0] if row else {}
    try:
        return Config.model_validate(
            stored if isinstance(stored, dict) else {}
        ).claude.sandbox.enabled
    except ValueError:
        return None


def sandbox_off_findings(
    enabled: bool | None, protection: preflight.LoginProtection
) -> list[Finding]:
    """Report the sandbox setting and, with it off, the recommendation of requirement 42.

    The same judgement as a session's start-up warning (`calu.preflight.sandbox_off_warnings`),
    with the setting read from the global settings of the database (`--db`); a project's own
    setting is not looked at.

    Args:
        enabled: `claude.sandbox.enabled` (`stored_sandbox`); None if it was not read.
        protection: What keeps Claude's commands from calu's logins, collected before the
            switch to the user (`calu.preflight.login_protection`).

    Returns:
        [] when the setting was not read; one ok finding "sandbox" when it is on, or off with
        the recommendation met; otherwise one note with the recommendation.
    """
    if enabled is None:
        return []
    if enabled:
        return [Finding("sandbox", True, "on (claude.sandbox.enabled in the global settings)")]
    off = Config.model_validate({"claude": {"sandbox": {"enabled": False}}})
    said = preflight.sandbox_off_warnings(off, protection)
    if not said:
        return [
            Finding(
                "sandbox",
                True,
                "off; calu's logins are protected (passwords, or calu runs as root for "
                "another user)",
            )
        ]
    return [
        Finding(
            "sandbox",
            False,
            "off (claude.sandbox.enabled in the global settings)",
            said[0],
        )
    ]


def report(findings: Sequence[Finding]) -> str:
    """Render the whole report.

    Args:
        findings: What was found, in the order to print it.

    Returns:
        The lines, followed by one summary line.
    """
    lines = [finding.line() for finding in findings]
    missing = [finding for finding in findings if not finding.ok and finding.essential]
    notes = [finding for finding in findings if not finding.ok and not finding.essential]
    summary = f"\ncalu {__version__}: "
    if missing:
        summary += "a session cannot run without " + ", ".join(f.name for f in missing)
    elif notes:
        summary += f"ready, with {len(notes)} note(s) above"
    else:
        summary += "everything in place"
    return "\n".join(lines) + "\n" + summary


def permission_mode_findings(
    workspace: Path | None = None, home: Path | None = None, config: Config | None = None
) -> list[Finding]:
    """State the permission mode a session would start in, as information.

    See agent/records/PLAN.md D5 and D241.

    A step needs the checkers **and** the classifier only in Claude Code's permission mode
    "auto"; in every other mode the checkers decide alone. calu starts Claude Code in "auto"
    unless calu's `claude.permission_mode` or `permissions.defaultMode` of Claude Code's
    settings files chooses another mode (`calu.preflight.expected_permission_mode`). Another
    mode is the user's choice, so the finding is never a problem: it is "ok" and says what
    follows. `calu doctor` runs without the database, so without `config` it reports what the
    settings files say, which is what holds while `claude.permission_mode` is unset.

    Args:
        workspace: The project directory, whose `.claude/settings.json` and
            `.claude/settings.local.json` have precedence over the user's file; None reads the
            user's file only.
        home: The home directory; None uses `Path.home()`.
        config: The configuration to judge; None uses the defaults (`claude.permission_mode`
            unset, `claude.require_auto_mode` false).

    Returns:
        One finding named "permission mode", always ok, whose detail is the line of
        `calu.preflight.permission_mode_line`.
    """
    _, line = preflight.permission_mode_line(config or Config(), home, workspace)
    return [
        Finding(
            "permission mode",
            True,
            line + ("" if config else " (Judged with claude.permission_mode unset.)"),
        )
    ]


def local_findings(store: CredentialStore) -> list[Finding]:
    """Collect everything that needs no password.

    Args:
        store: The credential store.

    Returns:
        The tool, the permission-mode, credential, place-file, key, directory and
        optional-dependency findings, in that order (`machine_findings` around
        `store_findings`).
    """
    machine = machine_findings()
    cut = next(
        (i for i, finding in enumerate(machine) if finding.name == "socket directory"),
        len(machine),
    )
    return [*machine[:cut], *store_findings(store), *machine[cut:]]


def store_findings(store: CredentialStore) -> list[Finding]:
    """Collect what is read from calu's own files: credentials, the place file, the keys.

    As root these are root's files (`/etc/calu`, `/var/lib/calu`), so `calu doctor --user`
    calls this before it switches its file access to the user.

    Args:
        store: The credential store.

    Returns:
        The credential, self-approval, place-file and key findings, in that order.
    """
    return [
        *credential_findings(store),
        *self_approval_findings(store),
        *place_findings(),
        *key_findings(),
        *key_value_findings(),
    ]


def machine_findings() -> list[Finding]:
    """Collect what is asked of the machine and of Claude Code's own settings.

    After `calu.asuser.enter` these are answered as the user Claude Code runs as: the
    programs are started as that user, and the settings files are that user's.

    Returns:
        The tool, the permission-mode, directory and optional-dependency findings, in that order.
    """
    return [
        *tool_findings(),
        *permission_mode_findings(),
        *directory_findings(),
        *extra_findings(),
    ]


def essential_missing(findings: Sequence[Finding]) -> bool:
    """Tell whether something essential is missing.

    Args:
        findings: The findings to judge.

    Returns:
        True if any essential finding failed, which the command turns into exit code 1.
    """
    return any(not finding.ok and finding.essential for finding in findings)


def sandbox_posture(workspace: Path) -> dict[str, Any] | None:
    """Ask the Claude Code CLI what sandbox it would build in `workspace`.

    `claude sandbox status` prints one JSON line and needs neither a session nor a login, so it
    is the authoritative answer to "would the sandbox come up here?" -- it knows which
    dependencies the installed CLI requires and whether a managed settings policy locks the
    posture. It reads the settings files of the current directory, so it is run **in** the
    workspace.

    What it does **not** tell (measured, `spikes/RESULTS.md`): whether the kernel will actually
    let bubblewrap create the namespaces. Inside a default Docker container, where
    `bwrap --unshare-user` fails, it still reports the sandbox as enabled with no reason
    against it.

    Args:
        workspace: The project directory whose settings should be taken into account.

    Returns:
        The parsed JSON, or None if the CLI is missing, failed, timed out or printed something
        that is not JSON. Never raises.
    """
    path = claude_executable()
    if path is None or not workspace.is_dir():
        return None
    try:
        done = subprocess.run(  # a fixed program, no shell
            [path, "sandbox", "status"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            cwd=workspace,
            check=False,
            # In the project, which the user controls: never with root's rights or keys.
            **asuser.child(),
        )
    except OSError, subprocess.SubprocessError:
        return None
    for line in done.stdout.splitlines():
        if line.startswith("{"):
            try:
                found = json.loads(line)
            except json.JSONDecodeError:
                return None
            return found if isinstance(found, dict) else None
    return None


def posture_findings(workspace: Path) -> list[Finding]:
    """Report what the CLI's own sandbox posture says and nothing more.

    The posture is about the **project's** settings, not calu's: calu asks for the sandbox with
    its own `--settings` when it starts a session. Measured consequences (`spikes/RESULTS.md`):
    the CLI names a missing dependency only when the project's settings switch the sandbox on,
    so silence there means nothing, and `calu doctor`'s own `bwrap`/`socat` checks are the
    honest answer to "are the tools here". What the posture adds that nothing else sees is a
    **managed settings policy that locks it**, which would override what calu asks for.

    So this reports two things and stays quiet otherwise: a locked policy, and a reason the CLI
    itself gives against building a sandbox.

    Args:
        workspace: The project directory a session would run in.

    Returns:
        A finding per thing worth saying; an empty list when the CLI did not answer or had
        nothing to report.
    """
    posture = sandbox_posture(workspace)
    if posture is None:
        return []
    findings: list[Finding] = []
    if posture.get("policyLocked"):
        findings.append(
            Finding(
                "sandbox policy",
                False,
                "a managed settings policy locks the sandbox posture",
                "calu's own `--settings` may not win against it; check the managed settings "
                "before trusting `claude.sandbox` in calu's configuration.",
            )
        )
    reason = str(posture.get("unavailableReason") or "")
    if reason:
        findings.append(
            Finding(
                "sandbox here",
                False,
                reason.split(" · ")[0],
                "From `claude sandbox status` in the workspace, which knows what the installed "
                "CLI needs.",
            )
        )
    return findings


def workspace_findings(workspace: Path) -> list[Finding]:
    """Check one project directory.

    Args:
        workspace: The directory a session would run in.

    Returns:
        Findings for the directory itself, for whether it is a git repository (which the
        changeset checker and the commit links need), for the sandbox the CLI would build
        here (`posture_findings`), for sandbox entries of the settings files that would keep a
        session from starting (`settings_file_findings`), and, when a settings file of the
        project sets the permission mode, the permission-mode finding with that file taken into
        account.
    """
    mode: list[Finding] = []
    if workspace.is_dir() and preflight.expected_permission_mode(Config(), workspace=workspace)[
        1
    ].startswith("permissions.defaultMode in .claude/"):
        mode = [
            Finding(f.name + " in this workspace", f.ok, f.detail, f.advice)
            for f in permission_mode_findings(workspace)
        ]
    return [
        Finding(
            "workspace",
            workspace.is_dir(),
            str(workspace) if workspace.is_dir() else f"{workspace} is not a directory",
            "`calu run <directory>` takes it as an argument.",
            essential=True,
        ),
        Finding(
            "git repository",
            (workspace / ".git").exists(),
            "yes" if (workspace / ".git").exists() else "no .git in the workspace",
            "The changeset checker reviews commits, and the write sets name a commit; without "
            "git both simply do less.",
        ),
        *posture_findings(workspace),
        *settings_file_findings(workspace),
        *mode,
    ]


def settings_file_findings(workspace: Path, home: Path | None = None) -> list[Finding]:
    """Report sandbox entries of Claude Code's settings files that calu would not start with.

    Judged against calu's default sandbox configuration, because `calu doctor` runs without
    the database: an entry that the project's own calu configuration also holds is reported
    here although a session would accept it.

    Args:
        workspace: The project directory a session would run in.
        home: The home directory; None uses `Path.home()`.

    Returns:
        One finding per settings file with such entries
        (`calu.preflight.settings_file_sandbox_findings`), else [].
    """
    return [
        Finding(
            "sandbox settings",
            False,
            f"{label} holds sandbox entries beyond calu's default configuration "
            f"({'; '.join(beyond)})",
            "A session does not start while a settings file goes beyond calu's own sandbox "
            f"configuration (claude.sandbox.fail_if_unavailable). {preflight.SANDBOX_ENTRY_REMEDY}",
        )
        for label, beyond in preflight.settings_file_sandbox_findings(Config(), home, workspace)
    ]


KEY_PROBES: dict[str, tuple[str, str, Callable[[str], dict[str, str]]]] = {
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "https://api.anthropic.com/v1/models",
        lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    ),
    "fireworks": (
        "FIREWORKS_API_KEY",
        "https://api.fireworks.ai/inference/v1/models",
        lambda key: {"Authorization": f"Bearer {key}"},
    ),
    "mistral": (
        "MISTRAL_API_KEY",
        "{mistral}/v1/models",
        lambda key: {"Authorization": f"Bearer {key}"},
    ),
    "openai": (
        "OPENAI_API_KEY",
        "https://api.openai.com/v1/models",
        lambda key: {"Authorization": f"Bearer {key}"},
    ),
}
"""Provider -> (key variable, a model-list URL, the headers that carry the key).

A model list is the cheapest call that proves a key: it generates nothing and costs nothing,
and a rejected key answers it with the provider's own reason ("Your API key expired on ...").
Each key is sent only to the provider it belongs to."""


def key_validity_findings(
    environ: Mapping[str, str] | None = None, client: Any = None
) -> list[Finding]:
    """Ask each provider whether the key calu holds for it is accepted (agent/records/PLAN.md D131).

    `key_findings` only sees that a key is **set**. A key that is set and expired passes it,
    and every check on that provider then fails and escalates the step -- which is how an
    expired Mistral key went unnoticed on the development machine. This makes one request per
    key that is set, to the provider it belongs to.

    Args:
        environ: The environment to read the keys from; None uses `os.environ`, into which the
            CLI has loaded calu's env file.
        client: An `httpx.Client` to send the requests with; None makes one with a 10 s
            timeout. Tests pass one with a mock transport.

    Returns:
        One finding per key that is set: accepted, rejected with the provider's reason, or not
        reachable (which is not called a bad key, since it may be the network). Providers whose
        key is not set are left out; `key_findings` reports those.
    """
    import httpx

    values = os.environ if environ is None else environ
    own = client is None
    http = client or httpx.Client(timeout=10.0)
    mistral = (values.get("MISTRAL_BASE_URL") or "https://api.eu.mistral.ai").rstrip("/")
    found: list[Finding] = []
    try:
        for provider, (variable, url, headers) in KEY_PROBES.items():
            key = (values.get(variable) or "").strip()
            if not key:
                continue
            name = f"{provider} key"
            try:
                answer = http.get(url.format(mistral=mistral), headers=headers(key))
            except httpx.HTTPError as exc:
                found.append(
                    Finding(
                        name,
                        True,
                        f"not checked: {type(exc).__name__} reaching the provider",
                        "The network decides this one, not the key.",
                    )
                )
                continue
            if answer.status_code == 200:
                found.append(Finding(name, True, f"{variable} is accepted"))
                continue
            found.append(
                Finding(
                    name,
                    False,
                    f"{variable} is refused ({answer.status_code}): {_reason(answer)}",
                    f"Every check on {provider} fails and escalates until it is replaced. "
                    f"Renew it in {user_env_file()}, or move the checkers to another provider "
                    "with `calu config models auto`.",
                )
            )
    finally:
        if own:
            http.close()
    return found


def aws_findings(session_factory: Callable[[], Any] | None = None) -> list[Finding]:
    """Ask AWS STS whether the credentials boto3 finds are accepted (agent/records/PLAN.md D132).

    The Bedrock counterpart of `key_validity_findings`: Bedrock is first in the order calu
    chooses models by (D129), and its credentials are not one key that can be shown to a model
    list. `GetCallerIdentity` needs no permission at all, creates nothing and costs nothing.

    Args:
        session_factory: Builds a `boto3.session.Session`; None uses boto3. Tests pass a
            stand-in.

    Returns:
        One finding when boto3 finds credentials: accepted (with the account's last four
        digits) or refused with AWS's reason. [] when boto3 is missing or finds none -- not
        every installation uses AWS, and `calu run` warns when a Bedrock model is configured
        without them.
    """
    try:
        if session_factory is None:
            from boto3.session import Session

            session_factory = Session
        session = session_factory()
        if session.get_credentials() is None:
            return []
    except Exception:  # boto3 missing, or a profile file it cannot read
        return []
    try:
        account = str(session.client("sts").get_caller_identity().get("Account", ""))
    except Exception as exc:
        return [
            Finding(
                "aws credentials",
                False,
                f"found, but STS refuses them: {' '.join(str(exc).split())[:200]}",
                "Checkers on Bedrock fail and escalate every step until they work. Renew them "
                "(e.g. `aws sso login`), or move the checkers with `calu config models auto`.",
            )
        ]
    return [Finding("aws credentials", True, f"accepted by STS (account ...{account[-4:]})")]


def _reason(answer: Any) -> str:
    """Pull the provider's own explanation out of a refusal.

    Args:
        answer: The HTTP response.

    Returns:
        The message the provider gave, shortened, or the status text.
    """
    try:
        body = answer.json()
    except ValueError:
        return (answer.text or answer.reason_phrase or "").strip()[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        for field in ("detail", "message", "error"):
            if isinstance(body.get(field), str):
                return str(body[field])[:200]
    return str(body)[:200]
