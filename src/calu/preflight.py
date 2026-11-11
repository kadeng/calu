"""Checks before a session starts: sandbox tools and checker API keys.

The warnings are reported to the clients as `log` warnings at every start
(`Session(startup_warnings=...)`), so an operator sees why a session cannot work before Claude
Code fails or a checker escalates every step.
"""

import json
import os
import shutil
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from calu.claude_model import is_cli_model
from calu.config import PROVIDER_KEYS_BY_PREFIX, Config, user_env_file
from calu.herd import STALE_AFTER_S

SANDBOX_TOOLS = ("bwrap", "socat")
"""Programs that Claude Code's Linux sandbox needs (`spikes/RESULTS.md`, Q1a)."""


def sandbox_warnings(config: Config) -> list[str]:
    """Report missing prerequisites of the sandbox (PLAN.md D15).

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
            "(PLAN.md D4). Only the checkers and the deny lists protect this machine."
        ]
    missing = [tool for tool in SANDBOX_TOOLS if shutil.which(tool) is None]
    if not missing:
        return []
    consequence = (
        "Claude Code refuses to start without a working sandbox (sandbox.fail_if_unavailable)."
        if sandbox.fail_if_unavailable
        else "Claude Code would run commands without a sandbox."
    )
    return [
        f"The sandbox needs {' and '.join(missing)}, which {'is' if len(missing) == 1 else 'are'}"
        f" not installed. {consequence} Install them (Debian/Ubuntu: apt install bubblewrap"
        " socat)."
    ]


def configured_models(config: Config) -> dict[str, str]:
    """List the model strings the session will call, by role.

    Args:
        config: The session configuration.

    Returns:
        Role name ("command checker", "edit checker", "changeset checker", "subagent result
        checker", "narrator", "summarizer") -> Pydantic AI model string. Roles that are switched
        off are left out.
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


def change_warnings(config: Config) -> list[str]:
    """Return warnings about a write-set configuration that cannot work (PLAN.md D31).

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


def network_warnings(config: Config) -> list[str]:
    """Return warnings about the sandbox's network settings (PLAN.md D39).

    Args:
        config: The session configuration, with the project's network policy already applied.

    Returns:
        A warning if the commands may reach **every** domain (the wildcard of the "full"
        policy), because that is a deliberate choice worth seeing at every start; one if they
        can reach nothing, which is the safe end but explains every failing download, so a
        session that is stuck on an install says why; nothing in between.
    """
    sandbox = config.claude.sandbox
    if not sandbox.enabled:
        return []
    if not sandbox.allowed_domains:
        return [
            "the sandbox blocks every outgoing connection of a command (network policy "
            '"none", or "proxy" with an empty allow list), so a download or an install will '
            "fail. Let one through with `calu config add claude.sandbox.allowed_domains "
            "<domain> [--project <project>]`."
        ]
    if "*" not in sandbox.allowed_domains:
        return []
    denied = f", except {', '.join(sandbox.denied_domains)}" if sandbox.denied_domains else ""
    return [
        "the sandbox lets commands reach every domain through its proxy (network policy "
        f'"full"{denied}). Narrow it with `calu project network --name <project> --network '
        "proxy` and `claude.sandbox.allowed_domains`."
    ]


def heartbeat_warnings(config: Config) -> list[str]:
    """Warn when the heartbeat interval is too long for a herd to trust the session's state.

    `clock.check_interval_s` is also the heartbeat interval (PLAN.md D56): the session stamps
    `sessions.last_seen` that often, and a herd calls a session stale after
    `herd.STALE_AFTER_S` seconds of silence. Setting the interval near or above that threshold
    makes every live session look dead in `calu sessions` and in the fleet view, which is worse
    than a slow clock check, so it is said at the start rather than discovered later.

    Args:
        config: The session configuration.

    Returns:
        One warning, or an empty list while the interval leaves room (it must stay under half
        the threshold). Switching the watch off (0) is not warned about: that is a deliberate
        choice and the row keeps the stamp of its insert.
    """
    interval = config.clock.check_interval_s
    if interval <= 0 or interval < STALE_AFTER_S / 2:
        return []
    return [
        f"clock.check_interval_s is {interval:g} s, and a session counts as stale after "
        f"{STALE_AFTER_S:g} s without a sign of life, so `calu sessions` and the fleet view "
        "will call this session stale although it is running. Lower the interval, or keep it "
        "under half of that."
    ]


def honesty_warnings(config: Config, checkers: Collection[str] = ()) -> list[str]:
    """Warn when the honesty register is switched on by halves (PLAN.md D90).

    The feature needs two things that are set separately on purpose (D85): a checker that
    records dishonest code, and a mode that says what calu does with the record. Each without
    the other is silent and useless, which is the worst kind of misconfiguration -- the user
    believes the feature is on.

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
            f"honesty.mode is '{mode}', but the honesty checker is disabled, so nothing records "
            "dishonest code and the register stays empty. Switch it on with `calu checker set "
            "honesty --enable`, or set honesty.mode to 'off'. (If another checker of yours "
            "records the marks, this warning does not apply.)"
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

    `key_warnings` cannot cover Bedrock: its credentials come from the environment, `~/.aws`,
    SSO or an instance role, not from one variable. Leaving it unchecked meant a session whose
    checkers are on Bedrock started without a word and then escalated every step (PLAN.md
    D132). This asks boto3's credential chain; whether AWS **accepts** the credentials is
    `calu doctor --keys`. Measured: 7 ms when credentials are found, and about 2 s when none
    are, because the chain's last step asks the EC2 metadata service and waits for it -- so the
    delay is paid only in the misconfiguration this warning exists for. The metadata probe is
    not switched off, because on EC2 an instance role is a legitimate source and the warning
    would then be false.

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
    """Report the roles that run on the machine's Claude Code account (PLAN.md D117).

    A `claude:` model is not an API client: each call runs the `claude` CLI, which calu starts
    with `--tools ""`. Such a checker therefore cannot read a file, glob or grep, however its
    instructions are written -- and that is invisible in the configuration, so it is said at
    every start. It is one warning for all of them, because a user who chose this chose it on
    purpose and does not need it repeated per role.

    Args:
        config: The session configuration.

    Returns:
        One warning naming the roles, or [].
    """
    roles = sorted(role for role, model in configured_models(config).items() if is_cli_model(model))
    if not roles:
        return []
    return [
        f"{', '.join(roles)} run on this machine's Claude Code account (a `claude:` model), "
        "which needs no API key -- and gets no tools: such a checker cannot read files, glob or "
        "grep, so instructions that tell it to go and look will not work. Each check also "
        "starts the CLI once, which costs a second or two."
    ]


USER_SETTINGS_KEYS = ("hooks", "enabledPlugins", "env")
"""Keys of `~/.claude/settings.json` whose absence changes what Claude does in a session."""


def setting_source_warnings(
    config: Config, home: Path | None = None, workspace: Path | None = None
) -> list[str]:
    """Name the user's own Claude Code setup that a calu session leaves out (PLAN.md D144, Q21).

    `claude.setting_sources` defaults to `["project"]` (D116), so Claude Code in a calu session
    does not read `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/` or the
    project's `.claude/settings.local.json`. That is deliberate -- a personal permission rule
    would widen what Claude Code approves by itself -- but it was silent: a user's hooks and
    skills simply did not run. This says which of them exist, once per start, and how to
    include them. It reads files only; a file that cannot be read or parsed is skipped.

    Args:
        config: The session configuration.
        home: The home directory; None uses `Path.home()`.
        workspace: The project directory, for `.claude/settings.local.json`; None skips it.

    Returns:
        One warning listing what is left out and the command that includes it, or [] when
        nothing is left out.
    """
    sources = set(config.claude.setting_sources)
    home = home if home is not None else Path.home()
    missing: list[str] = []
    if "user" not in sources:
        settings = _json_object(home / ".claude" / "settings.json")
        keys = [key for key in USER_SETTINGS_KEYS if settings.get(key)]
        if keys:
            missing.append(f"~/.claude/settings.json ({', '.join(keys)})")
        if (home / ".claude" / "CLAUDE.md").is_file():
            missing.append("~/.claude/CLAUDE.md")
        skills = home / ".claude" / "skills"
        if skills.is_dir() and any(skills.iterdir()):
            missing.append("~/.claude/skills/")
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
    checkers: Collection[str] = (),
    workspace: Path | None = None,
) -> list[str]:
    """Return all start-up warnings (sandbox, API keys, write sets, network, heartbeat).

    Args:
        config: The session configuration.
        environ: The environment to inspect; None uses `os.environ`.
        checkers: Names of the enabled checker definitions, for the checks that depend on
            which checkers exist rather than only on the configuration.
        workspace: The project directory, for `setting_source_warnings`; None skips the
            project-local file.

    Returns:
        The warnings of `sandbox_warnings`, `key_warnings`, `change_warnings`,
        `network_warnings`, `heartbeat_warnings`, `honesty_warnings`, `cli_model_warnings`,
        `bedrock_warnings` and `setting_source_warnings`.
    """
    return (
        sandbox_warnings(config)
        + key_warnings(config, environ)
        + change_warnings(config)
        + network_warnings(config)
        + heartbeat_warnings(config)
        + honesty_warnings(config, checkers)
        + cli_model_warnings(config)
        + bedrock_warnings(config)
        + setting_source_warnings(config, workspace=workspace)
    )
