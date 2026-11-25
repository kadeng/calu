"""Checks before a session starts: sandbox tools, checker API keys, settings files, the mode.

The warnings are reported to the clients as `log` warnings at every start
(`Session(startup_warnings=...)`), so an operator sees why a session cannot work before Claude
Code fails or a checker escalates every step. One of the checks is more than a warning:
`settings_file_sandbox_blockers` returns the reasons for which the runner does not start a
session at all (`calu.claude.sdk_runner.SdkRunner.run`).
"""

import json
import os
import shutil
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from calu import asuser
from calu.config import (
    KEYLESS_PREFIXES,
    PROVIDER_KEYS,
    PROVIDER_KEYS_BY_PREFIX,
    ClaudeConfig,
    Config,
    PermissionMode,
    default_model,
    provider_key_variables,
    user_env_file,
)
from calu.credentials import CredentialError, CredentialStore


def claude_home() -> Path:
    """Return the home directory Claude Code of a session reads its user settings from.

    Returns:
        The home of the user `calu.asuser.enter` switched to (`RunAs.home`), or `Path.home()`
        while calu works as the user it runs as. Under `--user` calu runs as root, so
        `Path.home()` would name root's home, not the one Claude Code gets.
    """
    run_as = asuser.active()
    return Path.home() if run_as is None else Path(run_as.home)


SANDBOX_TOOLS = ("bwrap", "socat")
"""Programs that Claude Code's Linux sandbox needs (`spikes/RESULTS.md`, Q1a)."""


def sandbox_warnings(config: Config) -> list[str]:
    """Report missing prerequisites of the sandbox (agent/records/PLAN.md D15).

    Args:
        config: The session configuration.

    Returns:
        One warning naming every missing program, or an empty list if the sandbox is off or all
        programs are present. With `sandbox.fail_if_unavailable` the warning says that Claude
        Code will refuse to start.
    """
    sandbox = config.claude.sandbox
    if not sandbox.enabled:
        return [
            "The sandbox for Bash commands is off: commands run with calu's own rights "
            "(agent/records/PLAN.md D4). Only the checkers and the deny lists protect this machine."
        ]
    missing = missing_sandbox_tools()
    if not missing:
        return []
    consequence = (
        "Claude Code refuses to start without a working sandbox (sandbox.fail_if_unavailable)."
        if sandbox.fail_if_unavailable
        else "Claude Code would run commands without a sandbox."
    )
    return [
        f"The sandbox needs {' and '.join(missing)}, which {'is' if len(missing) == 1 else 'are'}"
        f" not installed. {consequence} Install them (Debian/Ubuntu: apt install"
        f" {SANDBOX_PACKAGES})."
    ]


UNIX_SOCKET_PATH_MAX = 107
"""Longest path a Linux Unix socket can be bound to (`sun_path` holds 108 bytes with the NUL)."""

SANDBOX_SOCKET_NAME_LENGTH = len("/claude-socks-") + 16 + len(".sock")
"""What Claude Code's Linux sandbox appends to the temporary directory for its proxy bridge
sockets (`<tmpdir>/claude-socks-<16 hex digits>.sock`, the longer of its two names; seen in
Claude Code 2.1.319)."""


def sandbox_socket_warnings(config: Config, environ: Mapping[str, str] | None = None) -> list[str]:
    """Report a temporary directory too long for the sandbox's bridge sockets (Linux).

    Claude Code's Linux sandbox creates two Unix sockets in its temporary directory (Node's
    `os.tmpdir()`: `TMPDIR`, else `TMP`, else `TEMP`, else `/tmp`). When the socket path is
    longer than `UNIX_SOCKET_PATH_MAX` bytes the sandbox does not start ("Failed to create
    bridge sockets after 5 attempts"), and with `sandbox.fail_if_unavailable` every Bash
    command of the session fails. Under `--user` or `--sudo` Claude Code gets no `TMPDIR` from
    calu (`calu.launch`), so the check is skipped there.

    Args:
        config: The session configuration.
        environ: The environment Claude Code inherits; None uses `os.environ`.

    Returns:
        One warning naming the directory and the fix, or an empty list if the sandbox is off,
        the platform is not Linux, Claude Code runs as another user, or the path fits.

    Example:
        TMPDIR of 80 bytes: 80 + 35 = 115 > 107, so a warning.
    """
    if not config.claude.sandbox.enabled or not sys.platform.startswith("linux"):
        return []
    if asuser.active() is not None:
        return []
    env = os.environ if environ is None else environ
    tmpdir = next((env[name] for name in ("TMPDIR", "TMP", "TEMP") if env.get(name)), "/tmp")
    tmpdir = tmpdir.rstrip("/") or "/"
    length = len(tmpdir.encode()) + SANDBOX_SOCKET_NAME_LENGTH
    if length <= UNIX_SOCKET_PATH_MAX:
        return []
    return [
        f"The temporary directory {tmpdir} is too long for the sandbox: Claude Code creates its "
        f"proxy sockets there, and their path ({length} bytes) exceeds the {UNIX_SOCKET_PATH_MAX}"
        ' bytes a Unix socket allows. The sandbox will not start ("Failed to create bridge '
        "sockets\"), so Claude's commands fail. Start calu with a shorter TMPDIR (at most "
        f"{UNIX_SOCKET_PATH_MAX - SANDBOX_SOCKET_NAME_LENGTH} bytes), for example unset it."
    ]


def configured_models(config: Config) -> dict[str, str]:
    """List the model strings the session will call, by role.

    Args:
        config: The session configuration.

    Returns:
        Role name ("command checker", "edit checker", "changeset checker", "subagent result
        checker", "narrator", "summarizer", "honesty reviewer", "trace redaction agent") ->
        Pydantic AI model string. Roles that are switched off are left out: the honesty
        reviewer while `honesty.mode` is "off", the trace redaction agent unless
        `traces.enabled` and `traces.redact` are both on.
    """
    models = {
        "command checker": config.command_checker.model,
        "edit checker": config.edit_checker.model,
        "changeset checker": config.changeset_checker.model,
        "summarizer": config.context.summarizer_model,
    }
    if config.check_subagent_results:
        models["subagent result checker"] = config.subagent_checker.model
    if config.context.narrate:
        models["narrator"] = config.context.narrator_model
    if config.honesty.mode != "off":
        models["honesty reviewer"] = config.honesty.reviewer_model.strip() or default_model(
            config.claude.use_bedrock
        )
    if config.traces.enabled and config.traces.redact:
        models["trace redaction agent"] = config.traces.redactor_model.strip() or default_model(
            config.claude.use_bedrock
        )
    return models


def key_warnings(config: Config, environ: Mapping[str, str] | None = None) -> list[str]:
    """Report checker model providers whose API key is missing.

    A key counts as present if any of the provider's variables (`PROVIDER_KEYS_BY_PREFIX`) is
    set to a non-empty value. Call this after `calu.config.load_secrets`, which copies the keys
    from calu's env file into the environment. Providers that need no key here (e.g. "bedrock",
    which uses the AWS credentials of the environment) are not checked.

    Args:
        config: The session configuration.
        environ: The environment to inspect; None uses `os.environ`.

    Returns:
        One warning per provider with a missing key, naming the variable, the affected roles
        and the env file to put the key in. Empty if all keys are present.
    """
    env = os.environ if environ is None else environ
    roles_by_provider: dict[str, list[str]] = {}
    for role, model in configured_models(config).items():
        prefix = model.partition(":")[0]
        if prefix in PROVIDER_KEYS_BY_PREFIX:
            roles_by_provider.setdefault(prefix, []).append(role)
    warnings: list[str] = []
    for prefix, roles in roles_by_provider.items():
        keys = PROVIDER_KEYS_BY_PREFIX[prefix]
        if any(env.get(key) for key in keys):
            continue
        # What a missing key costs depends on the role: a checker that cannot answer escalates
        # its step, while a narrator or a summarizer that cannot answer only leaves the
        # checkers with less context. Saying "every check ... escalates" for all of them was
        # wrong for two of the six roles.
        # The roles are already named in the sentence above, so the consequence does not
        # repeat them; only the two non-checker roles are worth naming by what they produce.
        produces = {"narrator": "the narration", "summarizer": "the folded summary"}
        missing = [produces[role] for role in sorted(roles) if role in produces]
        consequence = []
        if any(role.endswith("checker") for role in roles):
            consequence.append("every check of these escalates to you instead of deciding")
        if missing:
            subject = " and ".join(missing)
            verb = "stays" if len(missing) == 1 else "stay"
            consequence.append(f"{subject} {verb} empty, so the checkers see less context")
        warnings.append(
            f"{' or '.join(keys)} is not set, but {prefix!r} models are configured for: "
            f"{', '.join(sorted(roles))}. Put the key in {user_env_file()} or in calu's "
            f"environment; until then {'; '.join(consequence)}."
        )
    return warnings


def unknown_provider_warnings(
    config: Config, env_file: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Name checker model providers whose API key Claude Code would inherit.

    calu blanks three sets of variables in the environment of the Claude Code process: the
    fixed `PROVIDER_KEYS`, every name defined in calu's env file, and the names in the
    `hidden_env` setting. Pydantic AI supports many more providers than `PROVIDER_KEYS`
    covers. For each configured one, the variables that hold its key are looked up in Pydantic
    AI itself (`calu.config.provider_key_variables`); the name is never guessed from the
    provider's prefix. Two cases give a warning:

    - A variable of the provider is set in calu's environment and is in none of the three
      sets. The warning names exactly that variable and the command that hides it.
    - calu cannot find out which variable the provider reads. The warning says so and names no
      variable; it stays until the model string is changed, because nothing calu could check
      would show that the right variable is hidden.

    Args:
        config: The session configuration.
        env_file: calu's env file; None uses `user_env_file()`. A missing or unreadable file
            defines nothing.
        environ: The environment Claude Code would inherit; None uses `os.environ`.

    Returns:
        One warning per provider prefix that is neither in `PROVIDER_KEYS_BY_PREFIX` nor in
        `KEYLESS_PREFIXES` and falls under one of the two cases.
    """
    env = os.environ if environ is None else environ
    path = env_file or user_env_file()
    try:
        in_file = set(dotenv_values(path)) if path.is_file() else set()
    except OSError:
        in_file = set()
    hidden = PROVIDER_KEYS | in_file | set(config.hidden_env)
    roles_by_provider: dict[str, list[str]] = {}
    for role, model in configured_models(config).items():
        prefix, colon, _ = model.partition(":")
        if colon and prefix not in PROVIDER_KEYS_BY_PREFIX and prefix not in KEYLESS_PREFIXES:
            roles_by_provider.setdefault(prefix, []).append(role)
    warnings: list[str] = []
    for prefix, roles in roles_by_provider.items():
        used = f"{prefix!r} models (configured for: {', '.join(sorted(roles))})"
        variables = provider_key_variables(prefix)
        if variables is None:
            warnings.append(
                f"calu does not know which environment variable holds the API key of {used}, "
                "so it cannot hide that key from Claude Code and its commands if the key is "
                f"exported in calu's environment. Put the key in {path} (every variable there "
                "is hidden), or name the variable that holds it: `calu config add hidden_env "
                "<VARIABLE>`. calu cannot check either, so this notice stays."
            )
            continue
        exposed = [name for name in variables if env.get(name) and name not in hidden]
        if exposed:
            commands = " and ".join(f"`calu config add hidden_env {name}`" for name in exposed)
            warnings.append(
                f"{' and '.join(exposed)} is set in calu's environment and holds a key of "
                f"{used}; Claude Code and its commands would inherit it. Hide it with "
                f"{commands}, or move the key into {path} (every variable there is hidden)."
            )
    return warnings


def change_warnings(config: Config) -> list[str]:
    """Return warnings about a write-set configuration that cannot work (agent/records/PLAN.md D31).

    Args:
        config: The session configuration.

    Returns:
        One warning if the review of a command's writes is on while the snapshot it needs is
        off, because the review would then silently never run; otherwise nothing.
    """
    if config.changes.review and not config.changes.snapshot:
        return [
            "changes.review is on but changes.snapshot is off, so no command's writes are "
            "reviewed: the review needs the snapshot that measures them. Set changes.snapshot "
            "to true, or changes.review to false."
        ]
    return []


def network_warnings(config: Config, platform: str | None = None) -> list[str]:
    """Return warnings about the sandbox's network settings (agent/records/PLAN.md D39).

    Args:
        config: The session configuration, with the project's network policy already applied.
        platform: The operating system as `sys.platform` names it; None reads `sys.platform`.
            Tests pass it to check the Linux case on any machine.

    Returns:
        A warning if the commands may reach **every** domain (the wildcard of the "full"
        policy), because that is a deliberate choice worth seeing at every start; one if they
        can reach nothing, which is the safe end but explains every failing download, so a
        session that is stuck on an install says why; nothing in between. On Linux also one
        when `allow_unix_sockets` names sockets: Claude Code ignores that list there (it is a
        macOS setting) and its sandbox refuses every Unix socket a command tries to create
        (measured 2026-11-20 with CLI 2.1.329, DOCUMENTATION.md "Developing Calu inside
        Calu"). An empty list for a sandbox that is off.
    """
    sandbox = config.claude.sandbox
    if not sandbox.enabled:
        return []
    said: list[str] = []
    if sandbox.allow_unix_sockets and (platform or sys.platform).startswith("linux"):
        said.append(
            "claude.sandbox.allow_unix_sockets has no effect on Linux: Claude Code applies it "
            "on macOS only, and its Linux sandbox refuses every Unix socket a command creates "
            f"({', '.join(sandbox.allow_unix_sockets)} included). A command that needs such a "
            "socket, e.g. a database test, cannot reach it in the session."
        )
    if not sandbox.allowed_domains:
        said.append(
            "the sandbox blocks every outgoing connection of a command (network policy "
            '"none", or "proxy" with an empty allow list), so a download or an install will '
            "fail. Let one through with `calu config add claude.sandbox.allowed_domains "
            "<domain> [--project <project>]`."
        )
    elif "*" in sandbox.allowed_domains:
        denied = f", except {', '.join(sandbox.denied_domains)}" if sandbox.denied_domains else ""
        said.append(
            "the sandbox lets commands reach every domain through its proxy (network policy "
            f'"full"{denied}). Narrow it with `calu project network --name <project> --network '
            "proxy` and `claude.sandbox.allowed_domains`."
        )
    return said


def heartbeat_warnings(config: Config) -> list[str]:
    """Return no warning: the heartbeat no longer depends on a setting (agent/records/PLAN.md D56).

    `clock.check_interval_s` used to be the heartbeat interval as well, so a long or
    switched-off clock watch made a live session look stale after `herd.STALE_AFTER_S` seconds,
    and this function warned about it. The session now stamps `sessions.last_seen` every
    `herd.HEARTBEAT_S` seconds whatever the clock watch is set to, so there is nothing left to
    warn about. The function stays so that `warnings` keeps its list of sources.

    Args:
        config: The session configuration (not read).

    Returns:
        An empty list.
    """
    del config
    return []


def honesty_warnings(config: Config, checkers: Collection[str] = ()) -> list[str]:
    """Warn when code honesty is switched on by halves.

    The honesty checker (an extra checker of the edit route) and `honesty.mode` are set separately.
    Honesty is judged by the honesty checker alone (agent/records/PLAN.md D224): with the mode on
    and the checker disabled, no step is judged for honesty, no edit verifies its file, and only the
    honesty review and a person's trust decide. With the checker enabled and the mode off, its marks
    are dropped.

    Args:
        config: The session configuration.
        checkers: Names of the enabled checker definitions, as `ProjectSettings.checkers`
            holds them.

    Returns:
        One warning for each half that is missing, or an empty list when the two agree
        (including both switched off, which is the default).
    """
    mode = config.honesty.mode
    enabled = "honesty" in set(checkers)
    if mode != "off" and not enabled:
        return [
            f"honesty.mode is '{mode}', but the honesty checker is disabled, so nothing judges "
            "honesty per step: no mark is recorded, no edit verifies its file, and only the "
            "honesty review and `calu honesty trust` decide what counts as verified. Switch "
            "the checker on with `calu checker set honesty --enable`, or set honesty.mode to "
            "'off'."
        ]
    if mode == "off" and enabled:
        return [
            "the honesty checker is enabled but honesty.mode is 'off', so it runs on every "
            "edit and its marks are thrown away -- a model call per edit for nothing. Set "
            "honesty.mode to 'track' to keep the marks, or disable the checker with "
            "`calu checker set honesty --disable`."
        ]
    return []


def bedrock_warnings(
    config: Config, credentials: Callable[[], object | None] | None = None
) -> list[str]:
    """Report Bedrock models configured on a machine where boto3 finds no AWS credentials.

    `key_warnings` cannot cover Bedrock: its credentials come from the environment, `~/.aws`, SSO or
    an instance role, not from one variable. Leaving it unchecked meant a session whose checkers are
    on Bedrock started without a word and then escalated every step (agent/records/PLAN.md D132).
    This asks boto3's credential chain; whether AWS **accepts** the credentials is `calu doctor
    --keys`. Measured: 7 ms when credentials are found, and about 2 s when none are, because the
    chain's last step asks the EC2 metadata service and waits for it -- so the delay is paid only in
    the misconfiguration this warning exists for. The metadata probe is not switched off, because on
    EC2 an instance role is a legitimate source and the warning would then be false.

    Args:
        config: The session configuration.
        credentials: Returns the credentials boto3 resolves, or None; None as the argument
            uses boto3 itself. Tests pass a stand-in.

    Returns:
        One warning naming the roles on Bedrock, or [] when none is, or when credentials exist.
    """
    roles = sorted(
        role for role, model in configured_models(config).items() if model.startswith("bedrock:")
    )
    if not roles:
        return []
    if credentials is None:
        try:
            from boto3.session import Session
        except ImportError:
            return [
                f"{', '.join(roles)} run on AWS Bedrock, and boto3 is not installed, so every "
                "check of them fails and escalates to you (install the `bedrock` extra of "
                "pydantic-ai)."
            ]
        credentials = lambda: Session().get_credentials()  # ruff: ignore[lambda-assignment]
    try:
        found = credentials()
    except Exception:  # a broken profile file reads as no credentials
        found = None
    if found is not None:
        return []
    return [
        f"{', '.join(roles)} run on AWS Bedrock, but no AWS credentials were found "
        "(environment, ~/.aws, SSO or an instance role): every check of them fails and "
        "escalates to you. `calu doctor --keys` checks them once they are set up, and "
        "`calu config models auto` moves the checkers to a provider that has credentials."
    ]


def cli_model_warnings(config: Config) -> list[str]:
    """Report the roles that still name the removed `claude:` model type (PLAN.md D258).

    `claude:<alias>` once ran a check through the `claude` CLI. It was removed: every checker,
    the narrator, the summarizer and the honesty reviewer run through Pydantic AI, and the CLI
    runs only the main coding agent. A stored configuration may still name such a model; it is
    not refused when it is loaded, so that an old configuration does not stop calu from
    starting. Every call of such a role fails instead (Pydantic AI: "Unknown model"), and a
    failed check escalates to the user. This warning says so at every start.

    Args:
        config: The session configuration.

    Returns:
        One warning naming the roles and how to choose an API model, or [].
    """
    roles = sorted(
        role for role, model in configured_models(config).items() if model.startswith("claude:")
    )
    if not roles:
        return []
    return [
        f"{', '.join(roles)} name a `claude:` model, which calu no longer supports: checkers "
        "run only through Pydantic AI, with an API model. Every check of these roles fails "
        "and escalates to you. Choose an API model, e.g. `calu config models auto`, which "
        "picks one from the provider keys in calu's env file."
    ]


def self_approval_warnings(config: Config, store: CredentialStore | None = None) -> list[str]:
    """Warn when a credential without a password is not shielded by a working sandbox (D162).

    calu runs as the same OS user as Claude. A credential stored without a password (D104) is
    protected only by the file mode, which does not separate the two. The sandbox makes calu's
    own directories unreadable to Claude's commands. Without a working sandbox, the built-in
    rules still make `calu ...` (D160) and every tool call on those directories wait for a
    human, and what else a command does with the file is left to the checker's judgement. Such
    a credential can resolve escalations, so that combination is named here, with the two
    ways out.

    Args:
        config: The session configuration.
        store: The credential store; None opens the default one. A store that cannot be read
            gives no warning (`calu doctor` reports it).

    Returns:
        One warning, or [].
    """
    sandbox = config.claude.sandbox
    working = sandbox.enabled and (
        sandbox.fail_if_unavailable or all(shutil.which(tool) for tool in SANDBOX_TOOLS)
    )
    if working:
        return []
    try:
        store = store if store is not None else CredentialStore()
        unprotected = sorted(n for n in store.names() if not store.needs_password(n))
    except CredentialError, OSError:
        return []
    if not unprotected:
        return []
    return [
        f"Without a working sandbox, the credential(s) {', '.join(unprotected)} stored without a "
        "password are kept from the programs Claude runs only by the checkers' judgement, and "
        "such a credential can resolve escalations. Switch the sandbox on, or put a password "
        f"back on them ({password_commands(unprotected)})."
    ]


def password_commands(unprotected: Sequence[str]) -> str:
    """Name the commands that put passwords back on credentials stored without one.

    `calu setup --set-password` encrypts the logins of `calu setup` (`user`, `db-admin`, and
    the entries of before D265 it converts); any other entry gets its password from `calu
    credential set NAME`.

    Args:
        unprotected: Names of the entries stored without a password.

    Returns:
        A text in backticks, e.g. "`calu setup --set-password`" or "`calu setup
        --set-password`, `calu credential set mykey`".
    """
    setup = {"user", "db-admin", "admin", "session", "operator", "calu_admin"}
    parts = ["`calu setup --set-password`"] if any(n in setup for n in unprotected) else []
    parts += [f"`calu credential set {n}`" for n in unprotected if n not in setup]
    return ", ".join(parts)


SANDBOX_PACKAGES = "bubblewrap socat"
"""The Debian/Ubuntu (and Fedora) packages of `SANDBOX_TOOLS`, for an install hint."""


def missing_sandbox_tools() -> list[str]:
    """Name the programs of `SANDBOX_TOOLS` that are not on `PATH`.

    Returns:
        The missing ones in the order of `SANDBOX_TOOLS`; [] when all are installed.
    """
    return [tool for tool in SANDBOX_TOOLS if shutil.which(tool) is None]


@dataclass(frozen=True)
class LoginProtection:
    """What keeps Claude Code's commands from calu's logins when the sandbox does not.

    Requirement 42: with the sandbox off, Claude's commands run with the rights of the OS user
    Claude Code runs as. Two arrangements keep calu's logins out of their reach: the logins
    are stored encrypted with a password, or calu runs as root and Claude Code as another
    user (`--sudo`, `--user`), so that the commands can read neither calu's store nor its
    memory. A `calu_admin` password published in the place file (`calu setup --no-password`
    as root, D228) is readable by every user and defeats both.
    """

    privileged: bool = False
    """calu runs as root and Claude Code as another user (`--sudo`, `--user`)."""
    unprotected: tuple[str, ...] | None = None
    """Names of the credential store's entries stored without a password; None if the store
    could not be read or holds no `user` entry (nothing is known then)."""
    published: bool | None = None
    """Whether the place file holds the password of `calu_admin`; None if not checked."""

    def met(self) -> bool:
        """Tell whether the recommendation of requirement 42 is known to be met.

        Returns:
            True if the place file is known to publish no password and either calu runs
            privileged or every store entry is known to have a password. False otherwise,
            also when a fact is unknown (None): then the warning is shown.
        """
        if self.published is not False:
            return False
        return self.privileged or self.unprotected == ()


def published_admin_login(path: Path | None = None) -> bool:
    """Tell whether a user other than root can read the password of `calu_admin` (D228, D279).

    It is in the login file next to the place file (`calu.db.place.login_path`, the user who
    ran `sudo calu setup`), or in a place file of before D279 (every user).

    Args:
        path: The place file; None is `calu.db.place.default_path()`.

    Returns:
        True if the place file is root's alone (so clients use it) and a password goes with
        it; False if it is missing, cannot be trusted (no client reads it then) or no
        password goes with it.
    """
    from calu.db import place  # imported here: `calu.db` loads psycopg

    try:
        shared = place.read(path)
    except place.PlaceError:
        return False
    return bool(getattr(shared.auth, "password", ""))


def login_protection(
    store: CredentialStore | None, privileged: bool, place_file: Path | None = None
) -> LoginProtection:
    """Collect the facts of `LoginProtection` for this process.

    Reads the store's entry names and kinds of protection, never a secret, and the place
    file. Call it before `calu.asuser.enter`: afterwards root's store cannot be read.

    Args:
        store: The credential store the session's login came from; None if unknown.
        privileged: Whether calu runs as root and Claude Code as another user.
        place_file: The place file; None is the default one.

    Returns:
        The facts. `unprotected` is None when the store is None, cannot be read, or holds
        no `user` entry.
    """
    unprotected: tuple[str, ...] | None = None
    if store is not None:
        try:
            names = store.names()
            if "user" in names:
                unprotected = tuple(n for n in names if not store.needs_password(n))
        except CredentialError, OSError:
            unprotected = None
    return LoginProtection(privileged, unprotected, published_admin_login(place_file))


SANDBOX_OFF_ADVICE = (
    "With Claude Code's sandbox off (claude.sandbox.enabled false), it is strongly "
    "recommended to at least protect calu's logins with a user password "
    "(`calu setup --set-password`) and/or run calu with --sudo, so that calu runs as a more "
    "privileged process than Claude Code and Claude's commands cannot reach its logins."
)
"""The recommendation of requirement 42, the same at setup, at a session's start and in
`calu doctor`."""


def sandbox_off_warnings(config: Config, protection: LoginProtection | None) -> list[str]:
    """Return the recommendation for a session without the sandbox (requirement 42).

    Args:
        config: The session configuration.
        protection: What protects calu's logins (`login_protection`); None if unknown.

    Returns:
        [] when the sandbox is on or the recommendation is known to be met
        (`LoginProtection.met`); otherwise `SANDBOX_OFF_ADVICE`, followed by what was found
        against it: the entries without a password, a published user login.
    """
    if config.claude.sandbox.enabled or (protection is not None and protection.met()):
        return []
    found: list[str] = []
    if protection is not None and protection.unprotected and not protection.privileged:
        found.append(f"stored without a password: {', '.join(protection.unprotected)}")
    if protection is not None and protection.published:
        found.append("a user other than root can read the user login (calu_admin)")
    return [SANDBOX_OFF_ADVICE + (f" Found: {'; '.join(found)}." if found else "")]


USER_SETTINGS_KEYS = ("hooks", "enabledPlugins", "env")
"""Keys of `~/.claude/settings.json` whose absence changes what Claude does in a session."""


def setting_source_warnings(
    config: Config, home: Path | None = None, workspace: Path | None = None
) -> list[str]:
    """Name the user's own Claude Code setup that a calu session leaves out.

    See agent/records/PLAN.md D144, Q21.

    `claude.setting_sources` loads every source by default, as a plain `claude` does (D178).
    Someone who narrows it (to keep personal permission rules out, say) leaves out
    `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/` or the project's
    `.claude/settings.local.json` -- and their hooks and skills then simply do not run. This
    says which of them exist, once per start, and how to include them again. It reads files
    only; a file that cannot be read or parsed is skipped.

    Args:
        config: The session configuration.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory, for `.claude/settings.local.json`; None skips it.

    Returns:
        One warning listing what is left out and the command that includes it, or [] when
        nothing is left out.
    """
    sources = set(config.claude.setting_sources)
    home = home if home is not None else claude_home()
    missing: list[str] = []
    if "user" not in sources:
        settings = _json_object(home / ".claude" / "settings.json")
        keys = [key for key in USER_SETTINGS_KEYS if settings.get(key)]
        if keys:
            missing.append(f"~/.claude/settings.json ({', '.join(keys)})")
        if (home / ".claude" / "CLAUDE.md").is_file():
            missing.append("~/.claude/CLAUDE.md")
        skills = home / ".claude" / "skills"
        try:
            if skills.is_dir() and any(skills.iterdir()):
                missing.append("~/.claude/skills/")
        except OSError:
            pass  # unreadable: nothing can be said about it, and a start must not fail on it
    local = workspace / ".claude" / "settings.local.json" if workspace is not None else None
    if "local" not in sources and local is not None and local.is_file():
        missing.append(".claude/settings.local.json")
    if not missing:
        return []
    return [
        "Claude Code reads only the settings sources "
        f"{sorted(sources) or 'none'} in this session (claude.setting_sources), so these of "
        f"yours are not loaded: {'; '.join(missing)}. To include them: "
        "`calu config add claude.setting_sources user` (and `local`) -- note that permission "
        "rules in them then let Claude Code approve steps by itself."
    ]


SANDBOX_LIST_KEYS = (
    ("excludedCommands", ("excludedCommands",), "excluded_commands"),
    ("filesystem.allowRead", ("filesystem", "allowRead"), None),
    ("filesystem.allowWrite", ("filesystem", "allowWrite"), None),
    ("network.allowedDomains", ("network", "allowedDomains"), "allowed_domains"),
    ("network.allowUnixSockets", ("network", "allowUnixSockets"), "allow_unix_sockets"),
)
"""Lists of a settings file's `sandbox` object that widen the sandbox: the name for a message,
the path of keys inside `sandbox`, and the `SandboxConfig` field that holds calu's own list
(None: calu has no such list, so every entry goes beyond its configuration)."""

SANDBOX_SWITCH_KEYS = (
    ("enabled", ("enabled",), False, "enabled"),
    ("failIfUnavailable", ("failIfUnavailable",), False, "fail_if_unavailable"),
    ("allowUnsandboxedCommands", ("allowUnsandboxedCommands",), True, "allow_unsandboxed_commands"),
    ("autoAllowBashIfSandboxed", ("autoAllowBashIfSandboxed",), True, "auto_allow_bash"),
    ("enableWeakerNestedSandbox", ("enableWeakerNestedSandbox",), True, "weaker_nested_sandbox"),
    ("enableWeakerNetworkIsolation", ("enableWeakerNetworkIsolation",), True, None),
    ("network.allowAllUnixSockets", ("network", "allowAllUnixSockets"), True, None),
    ("network.allowLocalBinding", ("network", "allowLocalBinding"), True, None),
)
"""Switches of a settings file's `sandbox` object: the name for a message, the path of keys
inside `sandbox`, the value that weakens the sandbox, and the `SandboxConfig` field that holds
calu's own value (None: calu always writes the other value)."""

SANDBOX_VALUE_KEYS = (
    ("network.httpProxyPort", ("network", "httpProxyPort")),
    ("network.socksProxyPort", ("network", "socksProxyPort")),
    ("ignoreViolations", ("ignoreViolations",)),
)
"""Keys of a settings file's `sandbox` object that calu never writes and whose presence alone
changes what the sandbox filters or reports: the name for a message and the path of keys."""


def settings_file_sandbox_findings(
    config: Config | ClaudeConfig, home: Path | None = None, workspace: Path | None = None
) -> list[tuple[str, list[str]]]:
    """List sandbox entries of Claude Code's settings files that go beyond calu's configuration.

    calu's sandbox settings come from the database and reach Claude Code as `--settings`, which has
    precedence over the settings files (agent/records/PLAN.md D15). The files of the loaded sources
    (`claude.setting_sources`: `~/.claude/settings.json`, `<workspace>/.claude/settings.json`,
    `<workspace>/.claude/settings.local.json`) may still hold a `sandbox` object. This reads those
    files and compares; it changes nothing. A missing file holds no entries. A file that exists and
    that calu cannot read as a JSON object is itself a finding, because calu cannot say what it
    sets; so is a `sandbox` key calu does not know.

    Args:
        config: The session configuration, or its `claude` part alone (which is all a runner
            has), with the project's network policy applied.
        home: The home directory; None uses `claude_home()`, or `$CLAUDE_CONFIG_DIR` for the
            user's file when that variable is set (Claude Code reads it from there then).
        workspace: The project directory; None skips the project and the local file.

    Returns:
        (the file as a message names it, its entries as "key: value" texts) for every loaded
        file that has such entries; [] when the sandbox is off (calu asks for none) or no file
        goes beyond calu's configuration. An entry of a list (`SANDBOX_LIST_KEYS`) counts when
        calu's own list does not hold it (for domains: unless calu allows every domain); a
        switch (`SANDBOX_SWITCH_KEYS`) counts when it has its weakening value and calu's
        configuration does not say the same; a key of `SANDBOX_VALUE_KEYS` counts when it is
        present with any value but null.
    """
    claude = config.claude if isinstance(config, Config) else config
    sandbox = claude.sandbox
    if not sandbox.enabled:
        return []
    sources = set(claude.setting_sources)
    files: list[tuple[str, Path]] = []
    if "user" in sources:
        files.append(user_settings_file(home))
    if workspace is not None and "project" in sources:
        files.append((".claude/settings.json", workspace / ".claude" / "settings.json"))
    if workspace is not None and "local" in sources:
        files.append((".claude/settings.local.json", workspace / ".claude" / "settings.local.json"))
    findings: list[tuple[str, list[str]]] = []
    for label, path in files:
        document = _json_document(path)
        if document is None:
            # calu cannot say what the file sets, so it does not count as holding nothing.
            findings.append((label, ["the file exists but calu cannot read it as a JSON object"]))
            continue
        found = document.get("sandbox")
        if found is None:
            continue
        if not isinstance(found, dict):
            findings.append((label, [f"sandbox: {json.dumps(found)[:80]} (not an object)"]))
            continue
        beyond: list[str] = []
        for name in _unknown_sandbox_keys(found):
            beyond.append(f"{name}: {json.dumps(_nested(found, tuple(name.split('.'))))[:80]}")
        for name, keys, field in SANDBOX_LIST_KEYS:
            value = _nested(found, keys)
            if value is None:
                continue
            if not isinstance(value, list):
                beyond.append(f"{name}: {json.dumps(value)[:80]} (not a list)")
                continue
            own = set(getattr(sandbox, field)) if field else set()
            if name == "network.allowedDomains" and "*" in own:
                continue
            extra = [str(entry) for entry in value if str(entry) not in own]
            if extra:
                beyond.append(f"{name}: {', '.join(extra)}")
        for name, keys, weak, field in SANDBOX_SWITCH_KEYS:
            own_value = getattr(sandbox, field) if field else not weak
            value = _nested(found, keys)
            # Anything but the strengthening value counts: `0` or "no" may be read as off.
            if value is not None and value is not (not weak) and own_value is not weak:
                beyond.append(f"{name}: {json.dumps(value)[:80]}")
        for name, keys in SANDBOX_VALUE_KEYS:
            value = _nested(found, keys)
            if value is not None:
                beyond.append(f"{name}: {json.dumps(value)[:80]}")
        if beyond:
            findings.append((label, beyond))
    return findings


SANDBOX_ENTRY_REMEDY = (
    "Remove them from the file and put what the session needs into calu's configuration "
    "(e.g. `calu config add claude.sandbox.allowed_domains <domain>`), or leave the file out "
    "with claude.setting_sources."
)
"""What to do about a settings file's sandbox entries; the same in the warning and the refusal."""


def settings_file_sandbox_blockers(
    config: Config | ClaudeConfig, home: Path | None = None, workspace: Path | None = None
) -> list[str]:
    """Return the reasons for which a session must not start: sandbox entries of settings files.

    calu passes its sandbox settings with `--settings`, and for the project's files and three
    keys that precedence was measured (`spikes/RESULTS.md`, "Can a settings file widen the
    pinned sandbox?"). For the user's file, the other keys and other CLI versions calu cannot
    be sure, and with `claude.sandbox.fail_if_unavailable` a sandbox it cannot be sure of is a
    reason not to run: the runner refuses the start, and during a session it makes a Bash step
    wait for a human while a finding exists (the files can change while a session runs).

    Args:
        config: The session configuration, or its `claude` part alone, with the project's
            network policy applied.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        One sentence per file of `settings_file_sandbox_findings`, naming the file and each
        entry; [] when there is no finding, or when `claude.sandbox.fail_if_unavailable` is
        false (the findings are then a warning, `settings_file_sandbox_warnings`).
    """
    claude = config.claude if isinstance(config, Config) else config
    if not claude.sandbox.fail_if_unavailable:
        return []
    return [
        f"{label} holds sandbox settings that go beyond calu's configuration "
        f"({'; '.join(beyond)}). calu passes its own sandbox settings with --settings, but "
        "cannot be sure that Claude Code leaves such entries of a settings file without "
        "effect, so no session starts, and a Bash step waits for a human, while they are "
        f"there (claude.sandbox.fail_if_unavailable). {SANDBOX_ENTRY_REMEDY}"
        for label, beyond in settings_file_sandbox_findings(config, home, workspace)
    ]


def settings_file_sandbox_warnings(
    config: Config, home: Path | None = None, workspace: Path | None = None
) -> list[str]:
    """Name sandbox entries of Claude Code's settings files that go beyond calu's configuration.

    Args:
        config: The session configuration, with the project's network policy applied.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        With `claude.sandbox.fail_if_unavailable`: `settings_file_sandbox_blockers`, so that
        the reason is in front of the reader before the start is refused. Without it: one
        warning per file of `settings_file_sandbox_findings` that names each entry and says
        that calu runs anyway.
    """
    if config.claude.sandbox.fail_if_unavailable:
        return settings_file_sandbox_blockers(config, home, workspace)
    return [
        f"{label} holds sandbox settings that go beyond calu's configuration "
        f"({'; '.join(beyond)}). calu passes its own sandbox settings with --settings, which "
        "has precedence; whether Claude Code leaves every such entry without effect was "
        "measured only for the project's files, three keys and Claude Code 2.1.319. The "
        "session runs anyway, because claude.sandbox.fail_if_unavailable is off. "
        f"{SANDBOX_ENTRY_REMEDY}"
        for label, beyond in settings_file_sandbox_findings(config, home, workspace)
    ]


AUTO_MODE = "auto"
"""The permission mode in which Claude Code's classifier decides next to calu's checkers."""


def settings_permission_mode(
    config: Config | ClaudeConfig, home: Path | None = None, workspace: Path | None = None
) -> tuple[str, str] | None:
    """Read `permissions.defaultMode` from the Claude Code settings files a session loads.

    Of the loaded sources (`claude.setting_sources`) the local file has precedence over the
    project's and that over the user's, as in Claude Code. This reads files only; a file that
    is missing or not a JSON object counts as not setting the mode.

    Args:
        config: The session configuration, or its `claude` part alone.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        (the mode, "permissions.defaultMode in <file>") for the first file that sets it, or
        None when no loaded file does.
    """
    claude = config.claude if isinstance(config, Config) else config
    sources = set(claude.setting_sources)
    home = home if home is not None else claude_home()
    files: list[tuple[str, Path]] = []
    if workspace is not None and "local" in sources:
        files.append((".claude/settings.local.json", workspace / ".claude" / "settings.local.json"))
    if workspace is not None and "project" in sources:
        files.append((".claude/settings.json", workspace / ".claude" / "settings.json"))
    if "user" in sources:
        files.append(("~/.claude/settings.json", home / ".claude" / "settings.json"))
    for label, path in files:
        mode = _nested(_json_object(path), ("permissions", "defaultMode"))
        if isinstance(mode, str) and mode:
            return mode, f"permissions.defaultMode in {label}"
    return None


def launch_permission_mode(
    config: Config | ClaudeConfig, home: Path | None = None, workspace: Path | None = None
) -> PermissionMode | None:
    """Choose the `--permission-mode` calu passes when it starts Claude Code.

    An interactive `claude` starts in auto mode when no settings file chooses a mode; a start
    through the Agent SDK without `--permission-mode` starts in "default" (both measured with
    CLI 2.1.319, agent/records/PLAN.md D241). calu passes "auto" in that case, so that a
    session gets the same mode as a plain `claude`. A mode the user chose is kept:
    `claude.permission_mode` is passed as it is, and a `permissions.defaultMode` in a loaded
    settings file (`settings_permission_mode`) makes calu pass nothing, so that Claude Code
    applies the file.

    Args:
        config: The session configuration, or its `claude` part alone.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        The mode to pass, or None to pass no mode.
    """
    claude = config.claude if isinstance(config, Config) else config
    if claude.permission_mode is not None:
        return claude.permission_mode
    if settings_permission_mode(claude, home, workspace) is not None:
        return None
    return "auto"


def expected_permission_mode(
    config: Config, home: Path | None = None, workspace: Path | None = None
) -> tuple[str, str]:
    """Work out the permission mode a session is expected to start in, and what sets it.

    `claude.permission_mode` decides when it is set, then `permissions.defaultMode` of a loaded
    settings file (`settings_permission_mode`); without either calu passes "auto"
    (`launch_permission_mode`). This reads files only. It is an expectation: a managed policy
    or a model without an auto mode can make Claude Code start in another mode, which is why
    the runner reports the mode Claude Code really runs in when the session starts.

    Args:
        config: The session configuration.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        (the mode, where it comes from as a phrase for a message).

    Example:
        With the default configuration and no settings file: `("auto", "calu's default, as
        in an interactive claude")`.
    """
    if config.claude.permission_mode is not None:
        return config.claude.permission_mode, "claude.permission_mode"
    from_file = settings_permission_mode(config, home, workspace)
    if from_file is not None:
        return from_file
    return AUTO_MODE, "calu's default, as in an interactive claude"


def permission_mode_line(
    config: Config, home: Path | None = None, workspace: Path | None = None
) -> tuple[bool, str]:
    """Say in one line whether auto mode's classifier is expected to decide next to the checkers.

    In the mode "auto" a step runs only if calu's checkers (or the allow lists) and Claude Code's
    classifier approve it (agent/records/PLAN.md D5). In every other mode the checkers decide
    alone, or, with `claude.require_auto_mode`, a step they pass waits for a human. Another mode
    is the user's choice; the line states it as information (`calu doctor`).

    Args:
        config: The session configuration.
        home: The home directory; None uses `claude_home()`.
        workspace: The project directory; None skips the project and the local file.

    Returns:
        (True if the expected mode is "auto", the line). The line names the mode, what sets it
        (`expected_permission_mode`) and what follows for a step the checkers pass.
    """
    mode, source = expected_permission_mode(config, home, workspace)
    if mode == AUTO_MODE:
        return True, (
            f"permission mode \"auto\" ({source}): a step needs calu's checkers and auto mode's "
            "classifier."
        )
    consequence = (
        "a step the checkers pass waits for a human (claude.require_auto_mode)"
        if config.claude.require_auto_mode
        else "a step the checkers pass runs on their approval alone"
    )
    return False, (
        f'permission mode "{mode}" ({source}): auto mode\'s classifier is off, so {consequence}.'
    )


def _nested(document: dict[str, object], keys: tuple[str, ...]) -> object:
    """Look up a value below nested JSON objects.

    Args:
        document: The outer object.
        keys: The keys from the outside in.

    Returns:
        The value, or None if a key is missing or a level is not an object.
    """
    value: object = document
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def user_settings_file(home: Path | None = None) -> tuple[str, Path]:
    """Locate the user's Claude Code settings file.

    Args:
        home: The home directory to look in; None follows Claude Code: `$CLAUDE_CONFIG_DIR` if
            set, else `.claude` in `claude_home()`.

    Returns:
        (the file as a message names it, its path).
    """
    if home is not None:
        return "~/.claude/settings.json", home / ".claude" / "settings.json"
    relocated = os.environ.get("CLAUDE_CONFIG_DIR")
    if relocated:
        return "$CLAUDE_CONFIG_DIR/settings.json", Path(relocated) / "settings.json"
    return "~/.claude/settings.json", claude_home() / ".claude" / "settings.json"


def _unknown_sandbox_keys(found: dict[str, object]) -> list[str]:
    """List the keys of a settings file's `sandbox` object that calu's tables do not know.

    Args:
        found: The `sandbox` object.

    Returns:
        Dotted names, e.g. "network.someKey", in the file's order. A key calu does not know
        may change the sandbox in a way calu cannot judge, so the caller reports it.
    """
    known = {keys for _name, keys, *_rest in (*SANDBOX_LIST_KEYS, *SANDBOX_SWITCH_KEYS)}
    known |= {keys for _name, keys in SANDBOX_VALUE_KEYS}
    known |= {("filesystem", "denyRead"), ("filesystem", "denyWrite"), ("network", "deniedDomains")}
    groups = {keys[0] for keys in known if len(keys) > 1}
    unknown: list[str] = []
    for key, value in found.items():
        if key in groups and isinstance(value, dict):
            unknown += [f"{key}.{inner}" for inner in value if (key, inner) not in known]
        elif (key,) not in known:
            unknown.append(key)
    return unknown


def _json_document(path: Path) -> dict[str, object] | None:
    """Read a JSON object from a file, telling a missing file from an unreadable one.

    Args:
        path: The file.

    Returns:
        The object; {} if the file does not exist; None if it exists and cannot be read as a
        JSON object.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _json_object(path: Path) -> dict[str, object]:
    """Read a JSON object from a file, or return {} if it is missing or not a JSON object.

    Args:
        path: The file.

    Returns:
        The object, or {} on any read or parse failure.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def warnings(
    config: Config,
    environ: Mapping[str, str] | None = None,
    checkers: Collection[str] | Mapping[str, object] = (),
    workspace: Path | None = None,
    protection: LoginProtection | None = None,
) -> list[str]:
    """Return all start-up warnings (sandbox, API keys, write sets, network, settings files).

    The permission mode gives no warning: calu starts Claude Code in auto mode unless the user
    chose another mode (`launch_permission_mode`), and a mode the user chose is not repeated
    at every start (agent/records/PLAN.md D241); `calu doctor` states it.

    Args:
        config: The session configuration.
        environ: The environment to inspect; None uses `os.environ`.
        checkers: Names of the enabled checker definitions, for the checks that depend on
            which checkers exist rather than only on the configuration (a mapping name ->
            definition, `ProjectSettings.checkers`, is read for its names).
        workspace: The project directory, for `setting_source_warnings` and
            `settings_file_sandbox_warnings`; None skips the project's files.
        protection: What keeps Claude's commands from calu's logins, for
            `sandbox_off_warnings`; None if unknown (the recommendation is then shown
            whenever the sandbox is off).

    Returns:
        The warnings of `sandbox_warnings`, `sandbox_socket_warnings`,
        `sandbox_off_warnings`, `key_warnings`,
        `unknown_provider_warnings`, `change_warnings`, `network_warnings`,
        `heartbeat_warnings`, `honesty_warnings`, `cli_model_warnings`, `bedrock_warnings`,
        `setting_source_warnings`, `settings_file_sandbox_warnings` and
        `self_approval_warnings`.
    """
    return (
        sandbox_warnings(config)
        + sandbox_socket_warnings(config, environ)
        + sandbox_off_warnings(config, protection)
        + key_warnings(config, environ)
        + unknown_provider_warnings(config, environ=environ)
        + change_warnings(config)
        + network_warnings(config)
        + heartbeat_warnings(config)
        + honesty_warnings(config, checkers)
        + cli_model_warnings(config)
        + bedrock_warnings(config)
        + setting_source_warnings(config, workspace=workspace)
        + settings_file_sandbox_warnings(config, workspace=workspace)
        + self_approval_warnings(config)
    )
