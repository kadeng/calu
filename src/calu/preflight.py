"""Checks before a session starts: sandbox tools and checker API keys.

The warnings are reported to the clients as `log` warnings at every start
(`Session(startup_warnings=...)`), so an operator sees why a session cannot work before Claude
Code fails or a checker escalates every step.
"""

import os
import shutil
from collections.abc import Mapping

from calu.config import PROVIDER_KEYS_BY_PREFIX, Config, user_env_file

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


def warnings(config: Config, environ: Mapping[str, str] | None = None) -> list[str]:
    """Return all start-up warnings (sandbox, API keys, write sets, network).

    Args:
        config: The session configuration.
        environ: The environment to inspect; None uses `os.environ`.

    Returns:
        The warnings of `sandbox_warnings`, `key_warnings`, `change_warnings` and
        `network_warnings`.
    """
    return (
        sandbox_warnings(config)
        + key_warnings(config, environ)
        + change_warnings(config)
        + network_warnings(config)
    )
