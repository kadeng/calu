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
        warnings.append(
            f"{' or '.join(keys)} is not set, but {prefix!r} models are configured for: "
            f"{', '.join(sorted(roles))}. Put the key in {user_env_file()} or in calu's "
            "environment; until then every check of these roles fails and escalates."
        )
    return warnings


def warnings(config: Config, environ: Mapping[str, str] | None = None) -> list[str]:
    """Return all start-up warnings (sandbox and API keys).

    Args:
        config: The session configuration.
        environ: The environment to inspect; None uses `os.environ`.

    Returns:
        The warnings of `sandbox_warnings` followed by those of `key_warnings`.
    """
    return sandbox_warnings(config) + key_warnings(config, environ)
