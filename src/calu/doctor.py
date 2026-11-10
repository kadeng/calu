"""`calu doctor`: what is in place, and what a session would miss.

The start-up warnings of `calu.preflight` only reach someone who already got a session to run.
This answers the question before that: is `claude` there, can the sandbox be built, where do the
credentials live, which checker keys are set, and — with the `admin` credential — does the
database answer and is its schema current.

    calu doctor          # everything that needs no password
    calu doctor --db     # ... and the database, with the `admin` credential

Nothing here changes anything: it reads, and it says what to do about what it finds.
"""

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from calu import __version__
from calu.changes import default_root
from calu.config import PROVIDER_KEYS_BY_PREFIX, user_env_file
from calu.credentials import CredentialStore
from calu.db import applied_versions, connect, migrations

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


def claude_version() -> tuple[bool, str]:
    """Ask the Claude Code CLI for its version.

    Returns:
        (whether it answered, the version or the reason it did not). Never raises: a missing or
        broken CLI is a finding, not an error.
    """
    path = shutil.which("claude")
    if path is None:
        return False, "not on PATH"
    try:
        done = subprocess.run(  # a fixed program, no shell
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            check=False,
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
    return found


def credential_findings(store: CredentialStore) -> list[Finding]:
    """Check the encrypted credential store.

    Args:
        store: The store to inspect; nothing is decrypted, only the names are read.

    Returns:
        A finding for each of the two credentials calu uses.
    """
    names = set(store.names())
    return [
        Finding(
            "credential 'admin'",
            "admin" in names,
            "stored" if "admin" in names else "not stored",
            "`calu db up` creates it for a local database, or `calu credential set admin` for "
            "an existing server.",
        ),
        Finding(
            "credential 'session'",
            "session" in names,
            "stored" if "session" in names else "not stored",
            "`calu setup` creates it; without it `calu run` cannot start a session.",
            essential=True,
        ),
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
            ", ".join(have) if have else "none of the known providers has a key",
            "Put one in ~/.config/calu/.env, e.g. ANTHROPIC_API_KEY (an Anthropic subscription "
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
    """Check the optional dependencies of the commands that need them.

    The web interface is the `web` extra, so an installation made without it has every other
    command but fails on `calu web`. That is worth saying here rather than only when the command
    is run.

    Returns:
        One finding for the web interface; never essential, because a session does not need it.
    """
    missing = [name for name in ("starlette", "jinja2", "uvicorn") if find_spec(name) is None]
    if missing:
        return [
            Finding(
                "web interface",
                False,
                f"the `web` extra is not installed ({', '.join(missing)} missing)",
                "`uv tool install 'calu[web]'` (or `uv sync --all-extras` in a clone); "
                "every other command works without it.",
            )
        ]
    return [Finding("web interface", True, "the `web` extra is installed", "")]


async def database_findings(url: str) -> list[Finding]:
    """Check that the database answers and that its schema is current.

    Args:
        url: The owner connection string from the `admin` credential.

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
                "`calu db up` starts a local one; for a server, check the `admin` credential.",
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


def local_findings(store: CredentialStore) -> list[Finding]:
    """Collect everything that needs no password.

    Args:
        store: The credential store.

    Returns:
        The tool, credential, key, directory and optional-dependency findings, in that order.
    """
    return [
        *tool_findings(),
        *credential_findings(store),
        *key_findings(),
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


def workspace_findings(workspace: Path) -> list[Finding]:
    """Check one project directory.

    Args:
        workspace: The directory a session would run in.

    Returns:
        Findings for the directory itself and for whether it is a git repository (which the
        changeset checker and the commit links need).
    """
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
    ]
