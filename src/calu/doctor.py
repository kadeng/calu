"""`calu doctor`: what is in place, and what a session would miss.

The start-up warnings of `calu.preflight` only reach someone who already got a session to run.
This answers the question before that: is `claude` there, can the sandbox be built, where do the
credentials live, which checker keys are set, and — with the `admin` credential — does the
database answer and is its schema current.

    calu doctor          # everything that needs no password
    calu doctor --db     # ... and the database, with the `admin` credential

Nothing here changes anything: it reads, and it says what to do about what it finds.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

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
        store: The store to inspect; nothing is decrypted, only the names and the kind of
            protection are read.

    Returns:
        A finding for each of the two credentials calu uses, and one more when an entry is
        stored without a password (D104), which says so even when that is the right choice.
    """
    names = set(store.names())
    unprotected = sorted(name for name in names if not store.needs_password(name))
    found = [
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
    if unprotected:
        # Not a defect: it is a choice, made for the commands that would otherwise ask for a
        # password every time (D104). It is reported because it is invisible otherwise, and
        # because `session` and `admin` are the wrong entries to choose it for.
        # `session` cannot get here any more (D115 refuses it), but a store written by an
        # older calu can still hold one, and that is exactly when a report has to say so.
        risky = [name for name in unprotected if name in ("admin", "session")]
        found.append(
            Finding(
                "credential protection",
                not risky,
                f"{', '.join(unprotected)} stored without a password (file mode only)",
                f"{', '.join(risky)} is the wrong entry for that: 'admin' owns the "
                "schema, and 'session' can read any session's rows by claiming its id. "
                "Store those with a password (`calu credential set <name>`) and leave "
                "only 'operator' unprotected -- it reads and resolves and can change "
                "nothing else."
                if risky
                else "",
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
        One failed finding for that combination, or [].
    """
    unprotected = sorted(name for name in store.names() if not store.needs_password(name))
    missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool) is None]
    if not unprotected or not missing:
        return []
    return [
        Finding(
            "self-approval",
            False,
            f"{', '.join(unprotected)} has no password and the sandbox cannot run here "
            f"({' and '.join(missing)} missing)",
            "Any program Claude runs could read it and resolve its own escalations. Install "
            "bubblewrap and socat, or put a password back (`calu credential set operator`).",
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
        *self_approval_findings(store),
        *key_findings(),
        *key_value_findings(),
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
    path = shutil.which("claude")
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
        changeset checker and the commit links need), and for the sandbox the CLI would build
        here (`posture_findings`).
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
        *posture_findings(workspace),
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
    """Ask each provider whether the key calu holds for it is accepted (PLAN.md D131).

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
    """Ask AWS STS whether the credentials boto3 finds are accepted (PLAN.md D132).

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
