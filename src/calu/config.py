"""Session configuration, built from the database (agent/records/PLAN.md D6).

Sources, later ones override earlier ones:

    Config() defaults
    config rows, scope "global"         key "settings", value: a partial `Config` as JSON
    config rows, scope "project"        same, scope_id = project name
    checker_types rows                  model and limits of the command, edit and
                                        subagent-result checkers and the narrator model

Nested objects merge key by key (see `merge_settings`); lists and scalars are replaced. The
checker instructions themselves are not part of `Config`; the session passes them to the
checkers directly.

Checker API keys are not configuration: calu reads them from its own environment and from
`~/.config/calu/.env` (`load_secrets`), and hides them from Claude Code.
"""

import functools
import importlib.util
import inspect
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calu.checkers.model import CheckerEffort
from calu.checkers.policy import Route
from calu.paths import calu_private_paths

__all__ = ["calu_private_paths"]  # re-exported: `calu.paths` holds it, callers import it here

EMPTY_CHAT_SUMMARY = "# Chat summary\n\nNo turns have been folded into this summary yet.\n"
"""Chat summary of a session without folded turns."""

SETTINGS_KEY = "settings"
"""Key of the `config` rows that hold a partial `Config`."""


ANTHROPIC_HAIKU = "anthropic:claude-haiku-5-5"
"""The model of every role whose model is left blank: the current Haiku on the Anthropic API."""

BEDROCK_EU_HAIKU = "bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0"
"""The same default in Bedrock mode (`claude.use_bedrock`): Haiku through AWS Bedrock's EU
inference profile, which keeps the requests in EU regions."""


def default_model(use_bedrock: bool) -> str:
    """Return the model a role uses when its model is left blank (agent/records/PLAN.md D194).

    Args:
        use_bedrock: Whether Bedrock mode is on (`claude.use_bedrock`).

    Returns:
        `BEDROCK_EU_HAIKU` in Bedrock mode, else `ANTHROPIC_HAIKU`.
    """
    return BEDROCK_EU_HAIKU if use_bedrock else ANTHROPIC_HAIKU


class ConfigModel(BaseModel):
    """Base of the configuration models: the docstring under a field is its description.

    Pydantic then carries the text into `FieldInfo.description`, which is what the generated
    reference (`calu.configdoc`, `docs/CONFIGURATION.md`), the JSON schema of the protocol and
    the web interface's settings form read. A field without a docstring is therefore a field
    nobody can look up, so every one of them has one.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)


class CheckerConfig(ConfigModel):
    """Model, thinking effort and limits of one checker. `model` is a Pydantic AI model string.

    `max_tokens` caps one answer, `output_tokens_limit` all answers of one check (thinking
    included on most providers). A check that hits either is retried once, then escalated.
    `effort` buys accuracy with latency and tokens, so a cheap checker on a simple route can run
    with "off" while the changeset checker thinks.
    """

    model: str = ""
    """Pydantic AI model string of this checker; blank uses the default: Haiku on the Anthropic
    API, or Haiku through AWS Bedrock in an EU region when `claude.use_bedrock` is on. Other values
    are e.g. "mistral:mistral-small-2603", "anthropic:claude-haiku-4-5", "fireworks:accounts/..." or
    "bedrock:eu.anthropic....". A claude.ai subscription cannot answer checks: it signs in only the
    `claude` CLI, which runs the main coding agent (agent/records/PLAN.md D258)."""
    effort: CheckerEffort | None = None
    """Thinking effort: "off" for none, "low" to "max", or None for the model's default.
    More effort buys accuracy with latency and tokens."""
    request_limit: int = 5
    """Maximum model requests for one check, including the tool calls a checker makes. Five
    covers the request itself, two tool calls and one retry after an answer that was too long;
    with less, a check that uses its tools and then retries fails instead of deciding
    (measured, agent/records/PLAN.md D72)."""
    output_tokens_limit: int = 24_000
    """Token budget for all answers of one check (thinking included on most providers).
    A check that hits it is retried once, then escalated. It holds three times `max_tokens`,
    because a budget that one maximal answer exhausts leaves no room for that retry."""
    max_tokens: int = 8_000
    """Token cap for one answer of this checker. On a provider that counts thinking towards the
    answer, a reasoning model can spend several thousand tokens before writing its decision, so
    a lower cap makes it fail rather than decide (measured, agent/records/PLAN.md D72)."""


CHANGESET_REQUEST_LIMIT = 80
"""Model requests of one changeset check, tool calls included (a step checker has 5)."""

CHANGESET_OUTPUT_TOKENS_LIMIT = 240_000
"""Token budget for all answers of one changeset check, thinking included."""

CHANGESET_MAX_TOKENS = 32_000
"""Token cap for one answer of the changeset checker."""


def changeset_limits() -> CheckerConfig:
    """Return the default limits of the changeset checker (agent/records/PLAN.md D197).

    Returns:
        A `CheckerConfig` with a blank model and the three `CHANGESET_*` limits.
    """
    return CheckerConfig(
        request_limit=CHANGESET_REQUEST_LIMIT,
        output_tokens_limit=CHANGESET_OUTPUT_TOKENS_LIMIT,
        max_tokens=CHANGESET_MAX_TOKENS,
    )


class ContextConfig(ConfigModel):
    """Context summary settings (see `calu.context`).

    Every `fold_every` unfolded turns, the oldest `fold_count` turns are folded into the chat
    summary, and earlier when the unfolded turns outgrow `max_recent_chars`. No file of the
    workspace is part of a checker's context: the agent under review can write there, so
    project rules that must bind belong in the checker instructions
    (agent/records/PLAN.md D193). A stored `context.files` or `context.skill_dirs` of an earlier
    version is ignored.
    """

    fold_every: int = Field(default=30, ge=2)
    """Fold the oldest turns into the chat summary once this many turns are unfolded."""
    fold_count: int = Field(default=20, ge=1)
    """How many of the oldest turns are folded into the chat summary each time."""
    summarizer_model: str = ""
    """Model that writes the chat summary when turns are folded; blank uses the default (see
    a checker's `model`)."""
    narrate: bool = True
    """Narrate Claude's thinking and text for the checkers (`calu.context.narrator`)."""
    narrator_model: str = ""
    """Model that narrates Claude's thinking and text in indirect speech, so that a checker
    does not read it as an instruction; blank uses the default (see a checker's `model`)."""
    escalate_on_cut: bool = True
    """A step waits for a human when a checker could not see all of what it judges, because
    a text was longer than calu's (generous) limits: Claude's thinking and text before the
    step, or a subagent's task or result. The checker's verdict and the size of the cut are in
    the escalation. A cut of the command or diff itself always escalates."""
    max_recent_chars: int = Field(default=100_000, ge=1_000)
    """Upper bound in characters for the unfolded turns and the running turn in a checker's
    context. Above it the oldest turns are folded into the chat summary at once, as many as it
    takes to get under it, not only after `fold_every` turns."""


DEFAULT_DENY_READ = (
    "~/.config/calu",
    "~/.local/state/calu",
    "~/.claude/.credentials.json",
    "~/.claude.json",
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.gnupg",
    "~/.config/gh",
    "~/.npmrc",
    "~/.pypirc",
)
"""Paths that sandboxed commands must not read (agent/records/PLAN.md D15): calu's encrypted
credentials, Claude Code's own credentials, SSH, cloud and registry credentials. Claude Code's
built-in defaults additionally deny the container sockets, `/run/dbus` and `/run/user`.

The two calu entries are listed for the reader of the setting; removing them changes nothing,
because `SandboxConfig.settings` always adds `calu_private_paths()`."""


DEFAULT_ALLOWED_DOMAINS = (
    # Python
    "pypi.org",
    "files.pythonhosted.org",
    # JavaScript
    "registry.npmjs.org",
    # Rust
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    # Go
    "proxy.golang.org",
    "sum.golang.org",
    # git over HTTPS and the archives a checkout or an install pulls
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    # Debian and Ubuntu packages
    "deb.debian.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
)
"""What a coding task normally has to reach: package registries, git over HTTPS, OS packages.

`calu project add` writes this into a new project's settings as the starter allow list of the
"proxy" policy (agent/records/PLAN.md D45), where `calu config add|remove
claude.sandbox.allowed_domains ...` edits it one entry at a time. It is deliberately **not** the
default of `SandboxConfig.allowed_domains`: a session whose project says nothing, or that runs
without a project at all, reaches nothing, and that is the side to err on.

Claude Code itself runs outside the sandbox (D4), so the model API is not in this list; only the
commands Claude runs are limited by it.
"""


DEFAULT_NETWORK_POLICY = "proxy"
"""The policy a new project gets.

See agent/records/PLAN.md D45, proposed; Q5 asks the owner to confirm it.

"proxy" together with `DEFAULT_ALLOWED_DOMAINS` lets a session install packages and use git
over HTTPS and reach nothing else. `calu project add --network full` gives a project the old
behaviour, and `calu project network --name <p> --network <policy>` changes it later.
"""

NetworkPolicy = Literal["none", "proxy", "full"]
"""What a project's commands may reach (`projects.network_policy`, agent/records/PLAN.md §7 and
D39).

Measured with the real CLI (`spikes/RESULTS.md`, "The sandbox's network policy"): the sandbox
runs a filtering HTTP proxy and exports it as `HTTPS_PROXY`, and raw TCP is blocked whatever the
policy says. So the three policies are expressed through the proxy's allow list:

    none    `allowedDomains` = []: nothing is reachable, not even through the proxy
    proxy   `allowedDomains` = the configured list: only those domains
    full    `allowedDomains` = ["*"]: everything, minus `deniedDomains`, which still wins
"""


class SandboxConfig(ConfigModel):
    """Isolation of Bash commands by Claude Code's own sandbox (agent/records/PLAN.md D15).

    calu passes these settings with `--settings`, which has precedence over the project's and the
    user's settings files, so a settings file does not switch the sandbox off. `settings()` writes
    each key of `PINNED_SANDBOX_KEYS`, also when its value is empty or false, so that the document
    itself says what every one of them is. Measured with CLI 2.1.319 (`spikes/RESULTS.md`, "Can a
    settings file widen the pinned sandbox?"): while `--settings` carries that document,
    `filesystem.allowRead`, `network.allowedDomains` and `excludedCommands` of the project's
    `.claude/settings.json` and `.claude/settings.local.json` have no effect. Not measured: the
    user's `~/.claude/settings.json`, the other keys, and other CLI versions. calu therefore does
    not rely on the precedence alone. It looks for a loaded settings file that holds a sandbox
    entry going beyond calu's configuration (`calu.preflight.settings_file_sandbox_findings`).
    With `fail_if_unavailable` on, a session does not start while such an entry exists, and a Bash
    step waits for a human. With it off, such entries are named in a start-up warning. The sandbox
    needs `bwrap` and `socat` on the machine (`calu.preflight`).

    calu's own directories (`calu_private_paths()`) are always on the read deny list; the
    `deny_read` setting holds further paths and cannot remove them.

    `fail_if_unavailable` makes Claude Code refuse to start without a working sandbox, instead of
    running commands unsandboxed, and makes calu refuse to start a session whose settings files hold
    sandbox entries beyond calu's configuration. `allow_unsandboxed_commands` False stops a call
    from leaving the sandbox with `dangerouslyDisableSandbox`. `auto_allow_bash` stays False: Claude
    Code would otherwise approve sandboxed Bash calls by itself, which would skip the auto-mode
    classifier that agent/records/PLAN.md D5 uses as the second opinion next to the checkers.

    `excluded_commands` run outside the sandbox and are therefore only covered by the checkers
    and the deny lists; the default is empty. `allowed_domains` and `denied_domains` configure
    the sandbox's network: with an empty `allowed_domains` the sandbox blocks outgoing network
    access (verified in `spikes/RESULTS.md`).
    """

    enabled: bool = True
    """Isolate Bash commands with Claude Code's own sandbox. Off removes that isolation and
    leaves only the checkers and the deny lists."""
    fail_if_unavailable: bool = True
    """Refuse to start when calu cannot be sure of the sandbox: when none can be built
    (Claude Code would otherwise run commands unsandboxed), and when one of Claude Code's
    settings files holds a sandbox entry that goes beyond calu's configuration (a Bash step
    also waits for a human while such an entry exists). False turns both into warnings."""
    allow_unsandboxed_commands: bool = False
    """Let a call leave the sandbox with `dangerouslyDisableSandbox`. False keeps it in. Set
    it true for a project to allow occasional unsandboxed commands: the command checker is
    told which calls run outside the sandbox and judges them more strictly."""
    auto_allow_bash: bool = False
    """Let Claude Code approve sandboxed Bash calls by itself. Stays False: it would skip
    auto mode's classifier, which agent/records/PLAN.md D5 uses as the second opinion next to the
    checkers."""
    weaker_nested_sandbox: bool = False
    """Claude Code's `enableWeakerNestedSandbox`. Measured in calu's own container
    (agent/records/PLAN.md D134): it does **not** make the sandbox work under Docker's default
    seccomp profile -- bubblewrap still cannot create a user namespace there, so the container needs
    `seccomp:unconfined` either way -- and where the namespaces do work, calu's probe saw no
    difference in isolation. There is no known reason to switch it on."""
    excluded_commands: list[str] = Field(default_factory=list)
    """Commands that run outside the sandbox, covered only by the checkers and the deny
    lists."""
    deny_read: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_READ))
    """Paths a sandboxed command must not read (see `DEFAULT_DENY_READ`). Edit single
    entries with `calu config add|remove claude.sandbox.deny_read <path>`. calu's own
    directories (its credentials, its env file with the checker keys, its snapshots) are
    denied whatever this list holds, also when it is empty."""
    allowed_domains: list[str] = Field(default_factory=list)
    """Domains a sandboxed command may reach through the sandbox's HTTP proxy; `["*"]` is
    every domain and an empty list is none. A project's network policy sets this
    (agent/records/PLAN.md D39, D45); raw TCP is blocked whatever it says."""
    denied_domains: list[str] = Field(default_factory=list)
    """Domains that stay blocked even when `allowed_domains` holds the wildcard, which is how
    "everything except these" is written."""
    allow_unix_sockets: list[str] = Field(default_factory=list)
    """Unix sockets a sandboxed command may connect to. Claude Code applies this list on
    macOS only: on Linux it ignores the list, and its sandbox refuses every Unix socket a
    command creates (measured 2026-11-20 with CLI 2.1.329), so a database socket is not
    reachable there; a session start warns about it."""

    def with_policy(self, policy: NetworkPolicy) -> SandboxConfig:
        """Return a copy whose network settings express `policy` (agent/records/PLAN.md D39).

        The deny list is kept in every case: it still overrides the wildcard of "full", which is
        how "everything except these domains" is written.

        Args:
            policy: The project's network policy.

        Returns:
            A copy with `allowed_domains` set accordingly: empty for "none", the wildcard for
            "full", and unchanged for "proxy" — where an empty list means nothing is reachable,
            which `calu.preflight` warns about.
        """
        if policy == "none":
            return self.model_copy(update={"allowed_domains": []})
        if policy == "full":
            return self.model_copy(update={"allowed_domains": ["*"]})
        return self

    def outside_reason(self, command: str, disable_sandbox: bool) -> str:
        """Say why a Bash command runs, or may run, outside the sandbox, for the checker.

        The command runs outside the sandbox in either of two cases, checked in this order,
        and only while `enabled` is True:

        - `disable_sandbox` (the call's `dangerouslyDisableSandbox`) with
          `allow_unsandboxed_commands` True: the command runs outside the sandbox.
        - The command matches an entry of `excluded_commands`: it may run outside. The match
          approximates Claude Code's pattern syntax and errs towards a match: `*` stands for
          any text, a final " *" also matches the command without arguments, and an entry
          without `*` matches the command alone or followed by a space and arguments. Claude
          Code itself keeps some matching command lines in the sandbox (chains, some
          redirects), which this does not model; hence "may".

        Args:
            command: The command line as Claude wrote it.
            disable_sandbox: Whether the call sets `dangerouslyDisableSandbox` to true.

        Returns:
            One sentence for the step's "Sandbox:" line, or "" when the command stays in the
            sandbox (also when the sandbox is off: then every command runs outside it).

        Example:
            With `excluded_commands=["scripts/db-tests.sh *"]`,
            `outside_reason("scripts/db-tests.sh -q", False)` starts with "this command may
            run OUTSIDE".
        """
        if not self.enabled:
            return ""
        access = "Claude Code's sandbox, with the session user's full file and network access"
        if disable_sandbox and self.allow_unsandboxed_commands:
            return (
                f"this command runs OUTSIDE {access}: it sets `dangerouslyDisableSandbox`, "
                "which this project allows (`claude.sandbox.allow_unsandboxed_commands`)."
            )
        line = command.strip()
        for entry in self.excluded_commands:
            pattern = entry.strip()
            if not pattern:
                continue
            parts = [re.escape(part) for part in pattern.removesuffix(" *").split("*")]
            tail = "(?: .*)?" if pattern.endswith(" *") or "*" not in pattern else ""
            if re.fullmatch(".*".join(parts) + tail, line, re.DOTALL):
                return (
                    f"this command may run OUTSIDE {access}: it matches `{entry}` of the "
                    "project's excluded commands (`claude.sandbox.excluded_commands`)."
                )
        return ""

    def denied_read_paths(self) -> list[str]:
        """Return the read deny list as Claude Code gets it.

        Returns:
            `calu_private_paths()` followed by the entries of `deny_read`, without duplicates.
            Never empty: calu's own directories are denied whatever `deny_read` holds.
        """
        paths = calu_private_paths()
        for path in self.deny_read:
            if path not in paths:
                paths.append(path)
        return paths

    def settings(self) -> dict[str, Any]:
        """Return the Claude Code settings document for `--settings`.

        The document holds exactly the keys of `PINNED_SANDBOX_KEYS`, each of them always,
        also with an empty or false value. Five of them have no setting in calu and a fixed
        value: `filesystem.allowRead` and `filesystem.allowWrite` are empty (calu re-opens
        nothing inside a denied path and adds no writable path), and
        `enableWeakerNetworkIsolation`, `network.allowAllUnixSockets` and
        `network.allowLocalBinding` are false. The complete document was run through CLI
        2.1.319, which accepts it (`spikes/spike_settings_widen.sh`).

        Returns:
            `{"sandbox": {...}}` with the keys Claude Code expects (camel case).
            `filesystem.denyRead` is `denied_read_paths()`.

        Example:
            `SandboxConfig().settings()["sandbox"]["failIfUnavailable"]` is True, and
            `SandboxConfig(deny_read=[]).settings()["sandbox"]["filesystem"]["denyRead"]`
            still starts with "~/.config/calu".
        """
        sandbox: dict[str, Any] = {
            "enabled": self.enabled,
            "failIfUnavailable": self.fail_if_unavailable,
            "allowUnsandboxedCommands": self.allow_unsandboxed_commands,
            "autoAllowBashIfSandboxed": self.auto_allow_bash,
            "enableWeakerNestedSandbox": self.weaker_nested_sandbox,
            "enableWeakerNetworkIsolation": False,
            "excludedCommands": list(self.excluded_commands),
            "filesystem": {
                "denyRead": self.denied_read_paths(),
                "allowRead": [],
                "allowWrite": [],
            },
            "network": {
                "allowedDomains": list(self.allowed_domains),
                "deniedDomains": list(self.denied_domains),
                "allowUnixSockets": list(self.allow_unix_sockets),
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
            },
        }
        return {"sandbox": sandbox}


PINNED_SANDBOX_KEYS = (
    "enabled",
    "failIfUnavailable",
    "allowUnsandboxedCommands",
    "autoAllowBashIfSandboxed",
    "enableWeakerNestedSandbox",
    "enableWeakerNetworkIsolation",
    "excludedCommands",
    "filesystem.denyRead",
    "filesystem.allowRead",
    "filesystem.allowWrite",
    "network.allowedDomains",
    "network.deniedDomains",
    "network.allowUnixSockets",
    "network.allowAllUnixSockets",
    "network.allowLocalBinding",
)
"""The keys of the `sandbox` object that `SandboxConfig.settings` writes, as dotted paths. A
test compares this list with the document, so the docstrings that refer to it stay true."""


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Reasoning effort levels of Claude Code (`--effort`, `/effort`)."""


PermissionMode = Literal["default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]
"""Claude Code's permission modes, as `claude.permission_mode` and `--permission-mode` take them."""


class ClaudeConfig(ConfigModel):
    """Settings for the Claude Code session.

    `model`: a Claude Code model alias ("fable", "opus", "sonnet", "haiku": always the latest
    version of that family) or a full model id; None uses Claude Code's default. Default: "opus".
    `effort`: reasoning effort ("low", "medium", "high", "xhigh", "max"); None uses Claude
    Code's default. Both can be changed per session (`session.start`) and during a session with
    the `/model` and `/effort` slash commands.

    Claude Code authenticates as it is configured on the machine (claude.ai login, AWS Bedrock,
    Google Vertex, Microsoft Foundry or an API key); calu does not change that. `use_bedrock`:
    if true, calu sets `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it uses AWS Bedrock
    with the AWS credentials of the environment (e.g. an EC2 instance role or `AWS_PROFILE`);
    false (default) leaves the variable as it is.

    `permission_mode`: None (default) gives the mode a plain interactive `claude` starts in:
    `permissions.defaultMode` of Claude Code's settings files when one of them sets it, else
    "auto". Measured with CLI 2.1.319: an interactive start without that setting runs in auto
    mode, a start through the Agent SDK without `--permission-mode` in "default"; so calu passes
    "auto" when no settings file chooses a mode (`calu.preflight.launch_permission_mode`,
    agent/records/PLAN.md D241). A value set here is passed as it is. In the auto mode a step
    runs only if the checkers (or the allow lists) **and** auto mode's classifier approve it
    (D5). Whatever the mode ends up being, the checkers work: the hook reads the effective mode
    from its input, defers a PASS to the classifier where the mode is "auto", and otherwise
    answers "allow" itself, so that the checkers decide alone (D21). Another mode is the user's
    choice and gives no warning at the start; the runner reports the effective mode as a `log`
    event at the start and whenever it changes (a warning only when Claude Code does not take
    the "auto" calu passed, e.g. for a model without auto mode), and `calu doctor` states the
    expected mode (`calu.preflight.permission_mode_line`).

    `require_auto_mode`: if true, a step the checkers PASS while the effective mode is not
    "auto" waits for a human instead of running on the checkers' word alone.

    `stream_partial`: if true (default), text and thinking are also streamed token by token
    (`assistant.delta` events); otherwise they arrive per completed block only.

    `sandbox`: how Bash commands are isolated (`SandboxConfig`); calu passes it to Claude Code
    with `--settings`, which has precedence over the settings files in the project.
    """

    use_bedrock: bool = False
    """Set `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code, so that it runs on AWS Bedrock with
    the environment's AWS credentials. False leaves the variable as it is; calu does not
    otherwise touch how Claude Code authenticates."""
    permission_mode: PermissionMode | None = None
    """Permission mode for Claude Code. None (default): `permissions.defaultMode` of Claude
    Code's settings files when one sets it, else "auto", as in an interactive `claude` (calu
    passes it, because a start through the Agent SDK would run in "default"). In "auto" auto
    mode's classifier is the second approval next to the checkers; in another mode the checkers
    decide alone."""
    require_auto_mode: bool = False
    """Whether a step needs auto mode's classifier as well as the checkers. False (default):
    outside the auto mode (another mode was chosen, the model has none, or a tool switched
    the mode during the session) a step the checkers pass runs, and the session logs the mode
    it runs in. True: such a step waits for a human instead."""
    stream_partial: bool = True
    """Stream text and thinking token by token (`assistant.delta`). False delivers completed
    blocks only."""
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    """How Claude Code returns thinking: "summarized" (readable summaries, shown by front ends
    as `assistant.thinking`) or "omitted" (empty thinking blocks, nothing is shown)."""
    model: str | None = "opus"
    """Claude Code model alias ("fable", "opus", "sonnet", "haiku": always the latest of that
    family) or a full model id; None uses Claude Code's default."""
    effort: Effort | None = None
    """Reasoning effort of the Claude session ("low" to "max"); None uses Claude Code's
    default. `calu run --effort` overrides it for one session."""
    setting_sources: list[str] = Field(default_factory=lambda: ["user", "project", "local"])
    """Which of Claude Code's own settings files it reads ("user", "project", "local"). All of
    them by default, as a plain `claude` does: calu wraps Claude Code and leaves how it loads
    its configuration, skills, MCP servers, plugins and auth alone (D178, the owner's answer
    to Q21). A narrower list is possible; the start-up warning then names what it leaves out.
    calu's `--settings` document has precedence over them for the sandbox either way; a
    sandbox entry of these files that goes beyond calu's own configuration keeps a session
    from starting (`claude.sandbox.fail_if_unavailable`), or is named in a start-up warning
    when that setting is off."""
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    """Isolation of Bash commands (agent/records/PLAN.md D15, D4)."""
    pass_env: list[str] = Field(default_factory=list)
    """Further environment variables, by name, that Claude Code gets from calu's own
    environment when it runs as another user (`--sudo`, `--user`), e.g. `CALU_TEST_DSN`. Such
    a Claude Code starts with an environment built from an empty one (PATH, locale, proxies,
    `ANTHROPIC_*`, `CLAUDE_*` and a few more); every name listed here is added to it, with the
    value calu's process has at the session start. A name that is not set there is left out;
    sudo itself drops most variables before calu starts, so `--sudo` needs
    `sudo --preserve-env=NAME calu run --user "$USER"` instead. Without `--sudo` or `--user`
    Claude Code inherits calu's environment anyway, and the list changes nothing. Names
    only, no patterns. Refused: the checkers' provider keys (`PROVIDER_KEYS`), calu's own
    secret variables (`CALU_SECRET_ENV`), and, at the session start, every name calu hides
    from Claude Code (`hidden_env`, the names of calu's env file). Add or remove single names
    with `calu config add|remove claude.pass_env <NAME>`."""

    @field_validator("pass_env")
    @classmethod
    def _passable_names(cls, names: list[str]) -> list[str]:
        """Accept only plain variable names that are not secrets of calu or of the checkers.

        Args:
            names: The configured entries.

        Returns:
            The entries, unchanged.

        Raises:
            ValueError: If an entry is not a plain variable name (letters, digits and "_",
                not starting with a digit; so no pattern such as `CALU_*`), or names a
                provider key of the checkers or a secret variable of calu.
        """
        for name in names:
            if not _VARIABLE_NAME.fullmatch(name):
                raise ValueError(
                    f"claude.pass_env holds names of environment variables, and {name!r} is "
                    'not one: letters, digits and "_", not starting with a digit, no pattern'
                )
            if name in PROVIDER_KEYS or name in CALU_SECRET_ENV:
                raise ValueError(
                    f"claude.pass_env cannot pass {name} to Claude Code: calu keeps it from "
                    "Claude Code, because it holds a secret of the checkers or of calu"
                )
        return names


class AutoEscalate(ConfigModel):
    """How closely a session is supervised: what waits for the human, and whether checkers run.

    Each of the following categories always waits for the human, whatever the checker decided:

    - `commands`: Bash and other tool calls reviewed by the command checker.
    - `edits`: file edits.
    - `subagents`: final results of subagents.

    The checker still runs; where it
    would have approved (ALLOW / ACCEPT / ANNOTATE), the step is escalated instead, with the
    checker's decision and reason in the escalation. A checker DENY stays a DENY. Read-only
    tools that the policy allows without a check are not affected. With training mode on as
    well, the training review (labelling the checker's decision) comes first, then the
    escalation (running the step).

    `unchecked` is the other end (requirement 53, PLAN D299): no checker runs at all.

    - A step that a checker would judge passes to Claude Code's own decision (its permission
      mode and its sandbox).
    - calu's rules still apply: the rule lists, the built-in rules, the tool policy, the rule
      for generated files, and questions to the user.
    - Nothing is judged after a step either: no review of what a command wrote, no review of
      a subagent's task or result, no honesty check, and `honesty.mode` "enforce" stops
      nothing.
    - The three categories above still hold: with `edits` and `unchecked`, every edit waits
      for the human, without a checker's opinion.
    """

    commands: bool = False
    """Every command waits for a human, even one the command checker approved."""
    edits: bool = False
    """Every file edit waits for a human, even one the edit checker approved."""
    subagents: bool = False
    """Every subagent's final result waits for a human, even an accepted one."""
    unchecked: bool = False
    """No checker runs: a step the rules do not stop passes to Claude Code's own decision.
    calu's rule lists and built-in rules still apply."""


class WebConfig(ConfigModel):
    """Settings of the web interface (agent/records/PLAN.md phase 6)."""

    git_url_template: str = ""
    """URL of one commit in whatever git browser the project uses, with `{commit}` and
    `{remote}` as placeholders, e.g. "http://localhost:8080/calu/commit/{commit}". Empty guesses
    the URL from the project's git remote, which works for GitHub, GitLab and Bitbucket; calu
    does not browse git itself."""
    pane_frame_ancestors: list[str] = Field(default_factory=list)
    """Who may show the chat pane (`/pane`) in a frame, as CSP source expressions such as
    "'self'" or "http://localhost:*" (agent/records/PLAN.md D261). Empty allows this interface
    and pages of this machine's loopback addresses ("'self'", "http://localhost:*",
    "http://127.0.0.1:*"), e.g. a local Jupyter. Every other page of `calu web` cannot be framed.
    Read when `calu web` starts."""


class ChangesConfig(ConfigModel):
    """Whether calu measures what a command wrote, and how much of it (agent/records/PLAN.md D31).

    Off by default: it costs disk and hashing time in every session, while the attribution of
    `ClockConfig`/D32 needs nothing. A project turns it on when it wants the content of a
    command's writes and the ability to undo them.
    """

    snapshot: bool = False
    """Take a git snapshot of the project directory before and after every Bash step."""
    review: bool = False
    """Let a checker judge what a command wrote, once it has run, and undo the change if it
    refuses (needs `snapshot`). The agent waits for that review, so a refusal is not a comment
    on something that already stands: the files go back to the state from before the command."""
    max_file_bytes: int = Field(default=1_000_000, ge=0)
    """Files above this size are reported by path and size, but their content is not shown."""
    max_patch_bytes: int = Field(default=800_000, ge=0)
    """Upper bound on the patch a checker or a client ever sees; a checker shown a cut patch
    cannot approve (it escalates)."""
    always_capture: list[str] = Field(default_factory=lambda: [".env"])
    """Paths to snapshot even though a `.gitignore` covers them, so that a write to them is
    noticed; they are the ones that matter most and the ones git would otherwise hide."""


LockingBackend = Literal["files", "process"]
"""Where the locks of `LockingConfig` live (agent/records/notes/locking.md, section 8)."""


class LockingConfig(ConfigModel):
    """Locks that keep edits away from running commands (agent/records/notes/locking.md).

    Off by default. On, a command waits until the edits running in its worktree have landed,
    edits in a worktree wait while a command runs there, and an edit outside the worktrees
    waits for every running command. The calls of one agent get their locks in the order the
    agent issued them. A call that does not get its locks within its wait is denied with a note
    that nothing ran and that it can be repeated. Calu makes the same locking calls when this
    is off; they then take no lock and never wait.
    """

    enabled: bool = False
    """Take the locks. Off, every call runs as soon as it is approved, as before."""
    backend: LockingBackend = "files"
    """Where the locks live: "files" shares them with every Calu process of this user on this
    machine and in the same container (state files in `directory`); "process" keeps them inside
    this Calu process, which covers all sessions of one `calu serve`."""
    directory: str = ""
    """The directory of the "files" backend; blank uses `$XDG_STATE_HOME/calu/locks` (or
    `~/.local/state/calu/locks`). Every Calu process that shares the locks names the same one.
    It must belong to the user and must not be writable by the group or others."""
    command_wait_s: float = Field(default=600.0, gt=0, le=3600)
    """How long a command waits for its locks before it is denied. It never waits longer than
    Claude Code lets the permission check take (about 15 minutes, minus the check itself)."""
    edit_wait_s: float = Field(default=600.0, gt=0, le=3600)
    """How long an edit waits for its locks before it is denied, capped the same way."""


HonestyMode = Literal["off", "track", "enforce"]
"""What calu does with the code honesty state (`calu.honesty`)."""


class HonestyConfig(ConfigModel):
    """Code honesty: which code was verified to say what it does, and what that decides.

    Code is honest when its names and documentation say what it does (the honesty rules).
    calu tracks which code is verified: a trusted commit is the baseline, the checkers record
    dishonest spots as marks, a checked edit verifies a file's content, and every other
    changed file is unreviewed. With `dependents` on, code that uses a unit whose signature or
    doc comment changed is to be judged again. The state is clean when there is a baseline, no
    open mark, no code to judge again, no unreviewed file, and no review is required.
    """

    mode: HonestyMode = "off"
    """`off`: nothing of this runs. `track`: the state is kept in the session's memory and
    shown in `calu attach`, the web live view and an ACP editor; the checkers are told which
    code is marked or unreviewed; a commit made in a clean state is certified; nothing is
    blocked or escalated because of the state. `enforce`: additionally, a command that
    executes project code (the command checker says whether it does) and a `git commit` need a
    clean state. With open marks the step is denied with their reasons. Without a trusted
    commit the step waits for a person (`calu honesty trust`, `/trust`). With unreviewed
    files an honesty review decides. The allow lists do not skip the command checker while the
    state is not clean. Editing is never blocked. Default `off`."""
    rules: str | None = None
    """The honesty standard the checkers that judge changes read: what dishonesty is and what an
    API contract must say (agent/records/PLAN.md D181). Empty uses the rules calu ships
    (`calu honesty show`). Set it, globally or per project, to fit a project's documentation
    standards (`calu honesty edit [--project NAME]`); it is not meant to be edited often."""
    sufficient_trust: Literal["check", "review", "user"] = "check"
    """The weakest source of stored trust that counts. Every trusted commit and every verified
    file content records who established it: `user` (a person certified it), `review` (calu's
    honesty review verified it) or `check` (calu's step checkers judged the edit without
    leaving a mark); `user` is the strongest. A commit whose source is weaker than this
    setting is no baseline, and a file content whose source is weaker is unreviewed. With
    `review`, an edit check alone does not verify a file: it stays unreviewed until an honesty
    review judged it. With `user`, only a person's certification counts, and calu starts no
    honesty review by itself, because a review cannot verify a file then; a person may still
    ask for one. Raising the setting deletes nothing; lowering it makes the weaker records
    count again."""
    max_open_marks: int = Field(default=6, ge=0)
    """How many marks may be open, counted over all files, before an honesty review is
    required. With more, a checker's single clean marks are ignored and only an honesty review
    clears the marks, which keeps documentation updates close to the edits they belong to."""
    notify_after_dishonest_edits: int = Field(default=3, ge=0)
    """How many dishonest steps calu counts before it tells Claude's main agent about the open
    marks. A step counts when it opened at least one mark; it may be an edit or a command, by
    any agent. The notice comes with the next checked tool call (`Session._honesty_notice`),
    also under `track`, and the count starts again. 0 switches the notice off."""
    review_max_files: int = Field(default=300, ge=1)
    """The largest number of files one honesty review reads: the files that are unreviewed or
    carry a mark. With more, the review does not run, and the step that needed it is escalated
    to a person with the counts. The reviewer gets a file's change (since the content last
    verified in this session, or since the baseline), or the whole file where there is
    nothing to compare with, and may ask to read a changed file whole. The review does not
    judge a file whose text for the reviewer is larger than `review_file_bytes`, a file of
    more than 8,000,000 bytes, or a file that is not UTF-8 text; a person certifies such a
    file (`calu honesty trust --file`)."""
    reviewer_model: str = ""
    """Pydantic AI model string of the honesty reviewer; blank uses the default model (see
    `command_checker.model`). The reviewer judges whole files against the honesty rules, so a
    more capable model than a step checker's is a reasonable choice."""
    review_batch_files: int = Field(default=8, ge=1)
    """How many files one run of the reviewer agent judges at most. A review splits its files
    into batches of this size, and closes a batch earlier when its files' texts together
    exceed `review_batch_bytes`."""
    review_concurrency: int = Field(default=4, ge=1)
    """How many batches of one honesty review run at the same time."""
    generated_paths: list[str] = Field(default_factory=list)
    """Regular expressions (Python, `re.search`) for the paths of generated files, relative to
    the project directory with "/", e.g. `^src/calu/web/static/pane/` or `\\.min\\.js$`.
    Empty by default: nothing counts as generated until a person lists it. A generated file
    is not judged for honesty. An honesty review verifies it without a model, and a mark on it
    is not recorded. An edit of it by hand (Edit, Write, MultiEdit, NotebookEdit) waits for a
    person. Pair the setting with a test that compares the files with a fresh build."""
    generated_contents: list[str] = Field(default_factory=list)
    """Regular expressions searched in the first 8192 bytes of a file to tell that it is
    generated, e.g. `\\A"use strict";` for a bundle that starts so, or `@generated`. Anchor
    with `\\A` for "the file starts with". Empty by default; otherwise as
    `generated_paths`."""
    review_file_bytes: int = Field(default=600_000, ge=1_000)
    """The largest text, in bytes of UTF-8, that the reviewer gets for one file: its change,
    or the whole file (a new file, a small marked one, or one the reviewer asked to read
    whole). A file with a longer text is not judged, and a step that needs a clean state waits
    for a person. The default, about 150,000 tokens, fits a model with a 200,000-token
    context; lower it for a reviewer with a smaller one. The session copies it into each
    checkout's honesty state (`Session._refresh_trust`)."""
    review_batch_bytes: int = Field(default=300_000, ge=1_000)
    """The largest text of all files of one reviewer run together; a file whose text alone is
    longer gets a run of its own (`calu.checkers.honesty_review.batches`)."""
    review_context_lines: int = Field(default=20, ge=0, le=500)
    """Lines of context around each change when the reviewer is shown a file's change
    rather than the whole file. The change is the one since the content this session last
    verified, if its text is still in memory, else the one since the baseline. More context
    lets a small change of a large file be judged from its diff. The session copies it into
    each checkout's honesty state (`Session._refresh_trust`), and `HonestyState.review_plan`
    renders the diffs with it."""
    reviewer_request_limit: int = Field(default=30, ge=1)
    """Maximum model requests for one batch, including the reviewer's tool calls. A batch that
    runs out fails, and a failed review verifies nothing."""
    reviewer_output_tokens_limit: int = Field(default=60_000, ge=1)
    """Token budget for all answers of one batch (thinking included on most providers)."""
    review_timeout_s: float = Field(default=600.0, gt=0)
    """Seconds one honesty review may take in all. A review that takes longer fails and
    verifies nothing, and the step that needed it is escalated. A step waits for a review at
    most as long as the check of one step may take (seven minutes, less the time its checkers
    took); a review that needs longer goes on, the step is escalated meanwhile, and the
    review's result is there when the step is tried again."""
    dependents: bool = True
    """Judge again the code that uses a unit whose signature or doc comment an edit changed.
    On by default whenever `honesty.mode` is not "off" (the owner, requirement 54).
    Python files only. How it works:

    1. A word search finds the unit's name in the files of the same language.
    2. A language server and a model drop the hits that do not use the unit
       (`dependents_navigator`, `dependents_judge`).
    3. The code around each remaining hit is recorded as "to judge again".

    The state is not clean until an honesty review, or a checked edit of that file, has judged
    it."""
    dependents_judge: bool = True
    """Let a model read each hit of the word search and drop the ones that do not use the
    changed unit. Off: every hit counts. That is safe, and makes more code to judge again. The
    model is `honesty.reviewer_model` (blank: the default model)."""
    dependents_navigator: list[str] = Field(default_factory=list)
    """The command line of a language server that answers questions about Python code, for
    example `["basedpyright-langserver", "--stdio"]`. It settles most hits without a model
    call. Empty: none is used. calu does not install it; it runs as the session's user."""
    dependents_timeout_s: float = Field(default=120.0, gt=0)
    """Seconds the search for the code that uses a changed unit may take after an edit. The
    agent waits meanwhile. If the language server and the model take longer, every hit of the
    word search counts."""

    @field_validator("generated_paths", "generated_contents")
    @classmethod
    def _patterns(cls, patterns: list[str]) -> list[str]:
        """Accept only patterns that compile, so a typo fails at `calu config set`.

        Args:
            patterns: The configured regular expressions.

        Returns:
            The patterns, unchanged.

        Raises:
            ValueError: If a pattern is empty or does not compile.
        """
        for pattern in patterns:
            if not pattern:
                raise ValueError("an empty pattern would match every file")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"the pattern {pattern!r} does not compile: {exc}") from None
        return patterns


def project_relative(directory: str, setting: str) -> str:
    """Check a directory setting that names a path inside the project directory.

    Args:
        directory: The configured path.
        setting: The setting's dotted name, for the message.

    Returns:
        The path with forward slashes, without a trailing slash and without `.` components.

    Raises:
        ValueError: If the path is empty, absolute, starts with `~`, holds a `..` component
            or a NUL character, or names the project directory itself.
    """
    text = directory.strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if (
        not parts
        or text.startswith(("/", "~"))
        or ".." in parts
        or "\0" in text
        or (len(parts[0]) == 2 and parts[0][1] == ":")
    ):
        raise ValueError(
            f"{setting} is a path inside the project directory, and {directory!r} "
            'is not one: it must be relative, not empty, and without a ".." component'
        )
    return "/".join(parts)


class TracesConfig(ConfigModel):
    """Session traces: the conversation written to a Markdown file inside the project (D232).

    With `enabled` on, calu appends every step of a session to a file below `directory`, so
    that the conversation can be committed and reviewed with the code it produced. The file
    only grows: nothing is folded or summarised, and it holds no images. Every block is
    redacted before it reaches the disk, and before a commit the part the commit adds is
    redacted once more as a whole (`final_redaction`, agent/records/PLAN.md D246). calu makes
    no commit of its own: a commit of Claude's in a session takes the finalized files along.
    """

    enabled: bool = False
    """Write a trace file for every session of the project. With the default `layout` and
    `file_names` the file is `<directory>/<date>/<user>/session_NNN.md`: the UTC date the
    session started, the OS user Claude Code runs as, and the next free three-digit number in
    that directory. Switching it on while a session runs starts a file from that point;
    switching it off ends the file."""
    content: Literal["narrated", "plain"] = "narrated"
    """What the file holds of Claude's own words. `narrated`: Claude's text and thinking retold
    in indirect speech by the narrator, as the checkers read them; with `context.narrate` off
    there is no narrator, and the file holds what `plain` holds. `plain`: Claude's text as
    written, and the summary of each thinking block in the last form Claude Code sent it."""
    directory: str = "agent/traces"
    """Where the trace files go, relative to the project directory. An absolute path, and a
    path with a `..` component, are refused. While `enabled` is on, a tool call of Claude that
    would write into this directory waits for a person, the files are left out of the write
    sets and of the code honesty state, and the checkers' code tools do not read them."""
    layout: Literal["date/user", "user", "date", "flat"] = "date/user"
    """The folders below `directory` that a trace file goes into. `date/user`:
    `<date>/<user>/` (the UTC date the session started, the OS user Claude Code runs as).
    `user`: `<user>/`, no date. `date`: `<date>/`, no user. `flat`: no folders, every file
    directly in `directory`. Leaving the date out keeps the path from saying on which day a
    session ran; leaving the user out keeps it from naming the user."""
    file_names: Literal["numbered", "random"] = "numbered"
    """How a trace file is named. `numbered`: `session_NNN.md`, the next free number in its
    folder, so with a date in `layout` the number counts the sessions of that user on that
    day. `random`: `session_<12 hex digits>.md`, a name that says nothing about how many
    sessions came before or in which order they ran."""
    header_times: bool = True
    """Write the time the trace started (UTC, to the second) into the file's header. Off: the
    header has no time; `layout` decides whether the path holds the date."""
    header_user: bool = True
    """Write the OS user Claude Code runs as into the file's header. Off: the header names no
    user; `layout` decides whether the path does."""
    turn_numbers: bool = True
    """Number the turns: `## Turn 3` ... `### End of turn 3`. Off: `## Turn` ... `### End of
    turn`, so that the headings do not count the messages of a session. The tool calls stay
    numbered, because the checks and results refer to them by number."""
    redact: bool = True
    """Let the redaction agent replace secrets and personal data (names of private persons,
    e-mail addresses, phone numbers, postal addresses, account and card numbers) by typed
    placeholders such as `[redacted: e-mail address]`, and leave out what its instructions
    allow (`redactor_instructions`), before a block is written. The values
    calu knows to be secret and the common shapes of keys and tokens are replaced in every
    case, also with this setting off; the file's header says which of the two applied. A
    block the agent could not redact is written as a line that says it was withheld."""
    redactor_model: str = ""
    """Pydantic AI model string of the redaction agent; blank uses the default model (see
    `command_checker.model`). The agent reads every block of the trace, so a small, fast
    model is a reasonable choice."""
    redactor_instructions: str | None = None
    """The redaction agent's instructions, as Markdown: what a trace must keep, what it may
    leave out, and which values are replaced by placeholders. Empty uses the text calu ships
    (`calu traces instructions show`): it keeps the requirements and decisions of the session
    and what was changed and why, may remove the user's questions and every hint of when the
    session happened and how long it took, and replaces secrets and personal data. Set it,
    globally or per project, with `calu traces instructions edit [--project NAME]`. calu puts
    a fixed part in front of it (the excerpt is data, the form of the answer)."""
    redactor_timeout_s: float = Field(default=60.0, gt=0)
    """Seconds the redaction agent may take for one block. A block that takes longer is
    withheld. A commit of Claude's also waits up to this long for the blocks still being
    redacted, so that they are part of the commit (agent/records/PLAN.md D262)."""
    final_redaction: bool = True
    """Before a commit, redact what the commit adds to each trace file once more, as a whole:
    the part of the file that is not in `HEAD` yet, read by the redaction agent in one go
    (pieces of up to 24,000 characters), with `redactor_instructions` and a fixed part for
    this pass. The part already committed is never changed. A line in the file marks how far
    the pass ran. It runs when Claude runs `git commit` in a session (the commit waits for it,
    and is refused if the pass fails), in the pre-commit hook of `calu traces hook install`,
    and with `calu traces finalize`. Off: trace files are committed as the per-block
    redaction wrote them."""
    final_timeout_s: float = Field(default=600.0, gt=0)
    """Seconds the final redaction pass may take for one trace file. A pass that takes longer
    fails, and the commit is refused."""
    queue_blocks: int = Field(default=500, ge=1)
    """How many blocks may wait to be redacted and written. A step never waits for the trace:
    when the redaction agent is so far behind that this many blocks wait, further blocks are
    left out, and the file says how many."""

    @field_validator("directory")
    @classmethod
    def _inside_the_project(cls, directory: str) -> str:
        """Accept only a relative path that stays inside the project directory.

        Args:
            directory: The configured path.

        Returns:
            The path with forward slashes, without a trailing slash and without `.`
            components.

        Raises:
            ValueError: If the path is empty, absolute, starts with `~`, holds a `..`
                component or a NUL character, or names the project directory itself.
        """
        return project_relative(directory, "traces.directory")


class HumanDiffsConfig(ConfigModel):
    """The user's own edits in the IDE, recorded as diffs inside the project (D253).

    With `enabled` on, every save of a file in the editor of VSCode or PyCharm that is attached
    to a session adds a unified diff of that save to a file below `directory`, so that a later
    prompt can show Claude how the user edits ("rewrite the documentation like this"). Only
    files that git tracks are recorded; Claude's edits, the writes of other programs and of
    the PyCharm synchronisation are not. The diffs are written as they are, without
    redaction.
    """

    enabled: bool = False
    """Record the user's saves in the IDE. Each save appends one block to
    `<directory>/<date>/<user>/<path>.tmp.diff` (the UTC date, the OS user the session works
    for, the file's path with "/" written as "__"). An editor that is attached to a session
    notices a change of this setting within a minute."""
    directory: str = "agent/human-diffs"
    """Where the diffs go, relative to the project directory. An absolute path, and a path
    with a `..` component, are refused. When calu creates the directory, it writes a
    `.gitignore` into it that keeps the `*.tmp.diff` files out of git. While `enabled` is on,
    a tool call of Claude that would write into this directory waits for a person, and the
    files are left out of the write sets and of the code honesty state."""
    keep_days: int = Field(default=14, ge=0)
    """Days a `*.tmp.diff` file is kept after its last change; then calu deletes it (when a
    session starts, and once a day while one runs). A file renamed to `<name>.diff` is never
    deleted. 0 keeps every file."""

    @field_validator("directory")
    @classmethod
    def _inside_the_project(cls, directory: str) -> str:
        """Accept only a relative path that stays inside the project directory.

        Args:
            directory: The configured path.

        Returns:
            The path with forward slashes, without a trailing slash and without `.`
            components.

        Raises:
            ValueError: If the path is empty, absolute, starts with `~`, holds a `..`
                component or a NUL character, or names the project directory itself.
        """
        return project_relative(directory, "human_diffs.directory")


class ClockConfig(ConfigModel):
    """How closely calu watches its own clock (agent/records/PLAN.md D32, `calu.clock`).

    A checker attributes a file change to a step by comparing the file's change time with the
    times of the steps, which only holds while the wall clock moves forward. calu therefore
    compares `CLOCK_REALTIME` with `CLOCK_BOOTTIME`, which cannot be set.
    """

    check_interval_s: float = Field(default=10.0, ge=0)
    """Seconds between samples; 0 switches the watch off. The interval only decides how soon a
    jump is noticed, not whether it is: each sample is compared with the previous one. The
    heartbeat that tells the database the session is still running has a fixed interval of its
    own and does not depend on this value."""
    max_backward_s: float = Field(default=1.0, ge=0)
    """A backward move up to this size counts as a time-server correction. Above it, the session
    escalates its next tool call, because the attribution of file changes is no longer sound."""
    max_forward_s: float = Field(default=300.0, ge=0)
    """A forward move above this size is logged (a first time-server sync after a boot jumps
    forward legitimately, so it is not an escalation)."""


class BuiltInRulesConfig(ConfigModel):
    """calu's own rules, applied before the allow and deny lists (`calu.checkers.rules`).

    Each one makes a kind of step ask a person, whatever a checker would say. Switched off,
    such a step is judged like any other: by the lists, the checkers and Claude Code's auto
    mode. A temporary rule can then tell the checkers to allow it
    (agent/records/notes/temporary-rules-design.md).
    """

    calu_commands: bool = True
    """A command that runs calu itself asks (agent/records/PLAN.md D160), however it is
    launched (by path, `uv run`, `uvx`, `pipx run`, `python -m calu`, behind wrappers). calu
    decides Claude's escalations: a `calu resolve`, `calu config set` or `calu rules` of
    Claude's would decide them or switch the checks off."""
    calu_container: bool = True
    """A `docker`, `podman` or `nerdctl` command that names calu's database container, compose
    project or volume (`calu-db`) asks (D171): that container holds the escalations."""
    calu_files: bool = True
    """Reading or writing calu's own directories asks: its configuration (with the logins and
    the checker keys) and its state, wherever `XDG_CONFIG_HOME` and `XDG_STATE_HOME` put them,
    and any path with a `.config/calu` component."""
    worktree_force_remove: bool = True
    """`git worktree remove --force` (or `-f`) asks (D251): it deletes a worktree with its
    uncommitted work, which Claude Code guards only for its own `ExitWorktree` tool."""


class Config(ConfigModel):
    """The complete configuration with defaults for every field."""

    built_in_rules: BuiltInRulesConfig = Field(default_factory=BuiltInRulesConfig)
    """calu's own rules that make some steps ask a person: running calu, calu's database
    container, calu's own files, a forced worktree removal. Each can be switched off."""

    command_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews Bash commands and every tool call routed to it."""
    edit_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews a file edit before it is written."""
    subagent_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews what a subagent hands back."""
    honesty: HonestyConfig = Field(default_factory=HonestyConfig)
    """Code honesty: whether calu tracks which code is verified, and whether running and
    committing code need a clean state."""
    changeset_checker: CheckerConfig = Field(default_factory=lambda: changeset_limits())
    """The checker that reviews a whole change before a commit (agent/records/PLAN.md D26). Its
    limits are far above a step checker's (`changeset_limits`): judging a whole change means reading
    the files around the diff, following callers and comparing the result with what was asked, which
    takes many tool calls and a long answer."""
    check_subagent_results: bool = True
    """Review the final result of every subagent with the subagent checker."""
    check_subagent_tasks: bool = True
    """Review with the subagent checker what Claude tells a subagent, before the subagent gets
    it: the task of an Agent call, and a later message of Claude's own to one of its subagents
    (calu's relay of the user's message is not reviewed). Off: such calls run without a check,
    and the checkers' context names a subagent's task only by its short description."""
    auto_escalate: AutoEscalate = Field(default_factory=AutoEscalate)
    """Categories that always wait for the human (see `AutoEscalate`); all off by default."""
    escalation_timeout_s: float = Field(default=8 * 3600, ge=0)
    """How long an escalation waits for a human before the session stops.

    See agent/records/PLAN.md D27, D282.

    An unsupervised run can sit in front of an escalation for a long time; at some point the
    session should end the turn and Claude Code instead of waiting for ever. On the timeout
    the step is denied and the session stops. A session of `calu serve`, or of `calu run` in a
    terminal, then resumes the same conversation at once and waits for the user's next message
    ("continue" goes on from there). An unattended run stays stopped, with Claude's session id
    recorded, so that `calu run --resume <id>` takes the conversation up again. 0 waits
    without a limit.
    """
    pause_timeout_s: float = Field(default=600.0, gt=0, le=780)
    """How long a user's pause of a session lasts at most (`pause.set`, the pause button of
    the chat).

    While a session is paused, an approved tool call that needed a check waits before it
    runs, so that the user can review what the session does. The wait runs inside Claude
    Code's PreToolUse hook, whose 15 minutes also hold the call's check: hence at most 780
    seconds."""
    store_failures_before_stop: int = Field(default=10, ge=0)
    """How many database writes may fail in a row before the session stops.

    See agent/records/PLAN.md D59.

    A session that cannot write is a session nobody can watch or unblock: its steps, its turns
    and its escalations do not reach the database, and with the heartbeat of D56 the fleet shows
    it as gone. Stopping at the first failure would make a network hiccup end an overnight run,
    so failures are tolerated and counted, and the count resets on the next write that works.
    0 never stops, which keeps a session running blind -- only sensible when somebody is
    attached to it.
    """
    hidden_env: list[str] = Field(default_factory=list)
    """Further environment variables (e.g. API keys of other checker providers) that Claude
    Code must not see, in addition to the ones from the env files and `PROVIDER_KEYS`. The
    runner blanks them in the environment of the Claude Code process, so its commands do not
    inherit them either. A changed list applies to the next session start. Each entry is the
    name of one variable (e.g. `XAI_API_KEY`), without a value. Add or remove single names
    with `calu config add|remove hidden_env <NAME>`."""
    tool_routes: dict[str, Route] = Field(default_factory=dict)
    """Route per tool name, overriding the built-in table (`calu.checkers.policy`), e.g.
    `{"mcp__deploy__ship": "escalate"}`."""
    default_route: Route = Route.COMMAND
    """Route for a tool the built-in table does not know, including MCP tools and tools a
    newer Claude Code adds. "command" means such a call is checked rather than allowed."""
    context: ContextConfig = Field(default_factory=ContextConfig)
    """What the checkers read as context, and how the conversation is folded."""
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    """The Claude Code session itself: model, effort, permission mode, sandbox."""
    clock: ClockConfig = Field(default_factory=ClockConfig)
    """Watching the system clock for tampering (agent/records/PLAN.md D32)."""
    changes: ChangesConfig = Field(default_factory=ChangesConfig)
    """Measuring what a command wrote, and reviewing it afterwards (agent/records/PLAN.md D31)."""
    locking: LockingConfig = Field(default_factory=LockingConfig)
    """Locks that keep edits away from running commands (agent/records/notes/locking.md); off
    by default."""
    web: WebConfig = Field(default_factory=WebConfig)
    """Settings of the web interface."""
    traces: TracesConfig = Field(default_factory=TracesConfig)
    """Session traces: the conversation as a redacted Markdown file inside the project, for
    version control and review (agent/records/PLAN.md D232); off by default."""
    human_diffs: HumanDiffsConfig = Field(default_factory=HumanDiffsConfig)
    """The user's own edits in the IDE, recorded as diffs inside the project, to show Claude
    later (agent/records/PLAN.md D253); off by default."""

    @field_validator("hidden_env")
    @classmethod
    def _variable_names(cls, names: list[str]) -> list[str]:
        """Accept only names an environment variable can have.

        An entry such as `A=B` would otherwise fail much later, when the Claude Code process
        is started with it, and with an error that says nothing about this setting.

        Args:
            names: The configured entries.

        Returns:
            The entries, unchanged.

        Raises:
            ValueError: If an entry is empty, or contains "=", a NUL character or white space.
        """
        for name in names:
            if not name or "=" in name or "\0" in name or any(c.isspace() for c in name):
                raise ValueError(
                    f"hidden_env holds names of environment variables, and {name!r} is not "
                    'one: a name is not empty and has no "=", white space or NUL character'
                )
        return names

    @model_validator(mode="after")
    def _fill_blank_models(self) -> Config:
        """Give every role whose model is blank the default model (agent/records/PLAN.md D194).

        Runs on every validation, so a `Config` never holds a blank model: readers use
        `command_checker.model`, `context.narrator_model` and the others as they are. What is
        stored in the database stays blank, so switching Bedrock mode later changes the
        default with it.

        Returns:
            The configuration itself, with the blank models filled.
        """
        model = default_model(self.claude.use_bedrock)
        for checker in CHECKER_FIELDS.values():
            section = getattr(self, checker)
            if not section.model.strip():
                section.model = model
        if not self.context.narrator_model.strip():
            self.context.narrator_model = model
        if not self.context.summarizer_model.strip():
            self.context.summarizer_model = model
        return self

    def with_model(self, model: str) -> Config:
        """Return a copy in which every model role uses `model`.

        The roles are the command, edit, changeset and subagent result checkers, the honesty
        reviewer, the narrator, the summarizer and the redaction agent of the session traces
        (e.g. for a test run with one model).

        Args:
            model: A Pydantic AI model string, e.g. "mistral:mistral-small-2603",
                "fireworks:accounts/fireworks/models/..." or "bedrock:eu.anthropic....".

        Returns:
            A new `Config`; `self` is unchanged.
        """
        return self.model_copy(
            update={
                "command_checker": self.command_checker.model_copy(update={"model": model}),
                "edit_checker": self.edit_checker.model_copy(update={"model": model}),
                "subagent_checker": self.subagent_checker.model_copy(update={"model": model}),
                "changeset_checker": self.changeset_checker.model_copy(update={"model": model}),
                "honesty": self.honesty.model_copy(update={"reviewer_model": model}),
                "context": self.context.model_copy(
                    update={"narrator_model": model, "summarizer_model": model}
                ),
                "traces": self.traces.model_copy(update={"redactor_model": model}),
            }
        )


def merge_settings(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a partial configuration into another, key by key.

    Args:
        base: The configuration so far (not modified).
        override: Keys to set; a dict value merges into a dict value of `base`, any other
            value replaces it.

    Returns:
        A new dict.

    Example:
        merge_settings({"claude": {"model": "opus", "effort": "low"}},
                       {"claude": {"model": "sonnet"}})
        == {"claude": {"model": "sonnet", "effort": "low"}}
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


CHECKER_FIELDS = {
    "command": "command_checker",
    "edit": "edit_checker",
    "subagent-result": "subagent_checker",
    "changeset": "changeset_checker",
}
"""Checker type names of the database mapped to the `Config` fields they configure."""


def build_config(
    layers: Iterable[Mapping[str, Any]],
    checker_rows: Iterable[tuple[str, str, int, int, int]] = (),
) -> Config:
    """Build the configuration from database values.

    A checker definition carries a model and limits of its own (`calu checker set --model`),
    and so does the configuration (`calu config set command_checker.model`). **The
    configuration wins**, because that is the layer a project overrides: the definition's
    values come first as the default, and the global and then the project settings are merged on
    top.

    Args:
        layers: Partial configurations in increasing precedence (global, then project).
        checker_rows: (name, model, request_limit, output_tokens_limit, max_tokens) of the
            enabled checker types. "command", "edit", "subagent-result" and "changeset" fill the
            checker fields of `Config`; "narrator" fills `context.narrator_model`; other names are
            ignored here.

    Returns:
        The validated configuration.

    Raises:
        pydantic.ValidationError: If the merged values do not match `Config`.
    """
    merged: dict[str, Any] = {}
    for name, model, request_limit, output_tokens_limit, max_tokens in checker_rows:
        if name in CHECKER_FIELDS:
            merged = merge_settings(
                merged,
                {
                    CHECKER_FIELDS[name]: {
                        "model": model,
                        "request_limit": request_limit,
                        "output_tokens_limit": output_tokens_limit,
                        "max_tokens": max_tokens,
                    }
                },
            )
        elif name == "narrator":
            merged = merge_settings(merged, {"context": {"narrator_model": model}})
    for layer in layers:
        merged = merge_settings(merged, layer)
    return Config.model_validate(merged)


def user_env_file() -> Path:
    """Return the path of calu's env file for checker API keys.

    Returns:
        `$XDG_CONFIG_HOME/calu/.env` or `~/.config/calu/.env`.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "calu" / ".env"


PROVIDER_KEYS = frozenset(
    {
        "MISTRAL_API_KEY",
        "FIREWORKS_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "LOGFIRE_TOKEN",
    }
)
"""Keys of model providers used by checkers; always hidden from Claude Code."""

CALU_SECRET_ENV = frozenset({"CALU_TOKEN", "CALU_DB_OWNER_PASSWORD", "CALU_LAUNCH"})
"""calu's own variables that `claude.pass_env` refuses: a client token (`calu attach`), the
password of a local database's owner (`calu setup`), and the launch record of
`calu-claude`, which the launcher sets itself."""

_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""A plain environment variable name, as `claude.pass_env` takes it."""

PROVIDER_KEYS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "mistral": ("MISTRAL_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai-chat": ("OPENAI_API_KEY",),
    "openai-responses": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}
"""Pydantic AI model-string prefix -> environment variables, any one of which holds the key.
These are the providers whose missing key `calu.preflight.key_warnings` reports. Every variable
here except `ANTHROPIC_API_KEY` (which Claude Code itself may use) is in `PROVIDER_KEYS`."""

KEYLESS_PREFIXES = frozenset({"bedrock", "claude", "function", "google-vertex", "ollama", "test"})
"""Model-string prefixes that take no API key from an environment variable of their own: AWS and
Google application credentials, a local server, and the test models. `claude` is the removed CLI
model type (D258); `calu.preflight.cli_model_warnings` reports it instead."""

_GETENV = re.compile(r"""os\.getenv\(\s*['"]([A-Z][A-Z0-9_]*)['"]""")
"""A variable a Pydantic AI provider module reads, as its source spells it."""
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)")
"""Which of those variables hold a credential (and not a base URL, a region or a version)."""


@functools.cache
def provider_key_variables(prefix: str) -> tuple[str, ...] | None:
    """Return the environment variables that hold the API key of a model provider.

    The names in `PROVIDER_KEYS_BY_PREFIX` are used where the prefix is listed. For every other
    provider the names are taken from the installed Pydantic AI itself: the provider's module is
    the code that reads the key, so the variables it reads with `os.getenv` and that are named
    like a credential are the ones a key can be in. Nothing is guessed from the prefix: the
    variable of `xai:` is `XAI_API_KEY` and that of `huggingface:` is `HF_TOKEN`.

    Reads the module's source file; the provider's own SDK is not needed, and nothing of the
    provider is called. The result is cached per prefix.

    Args:
        prefix: The part of a model string before the colon, e.g. "xai" or "azure".

    Returns:
        The variable names in the order the module reads them; an empty tuple for a prefix of
        `KEYLESS_PREFIXES`; None when Pydantic AI has no provider of that name or its module
        reads no credential variable, which means calu does not know the variable.

    Example:
        `provider_key_variables("xai")` is `("XAI_API_KEY",)`;
        `provider_key_variables("no-such-provider")` is None.
    """
    if prefix in PROVIDER_KEYS_BY_PREFIX:
        return PROVIDER_KEYS_BY_PREFIX[prefix]
    if prefix in KEYLESS_PREFIXES:
        return ()
    source = _provider_source(prefix)
    names = [name for name in _GETENV.findall(source) if _SECRET_NAME.search(name)]
    return tuple(dict.fromkeys(names)) or None


def _provider_source(prefix: str) -> str:
    """Return the source text of the Pydantic AI module that implements a provider.

    Args:
        prefix: The provider name of a model string.

    Returns:
        The module's source, or "" when Pydantic AI knows no such provider or the source
        cannot be read. A provider whose own SDK is not installed is still found, by the
        module name Pydantic AI uses for it (the prefix with "-" as "_").
    """
    path: str | None = None
    try:
        from pydantic_ai.providers import infer_provider_class

        path = inspect.getsourcefile(infer_provider_class(prefix))
    except Exception:  # unknown name (ValueError), missing SDK (ImportError), no source
        try:
            spec = importlib.util.find_spec(f"pydantic_ai.providers.{prefix.replace('-', '_')}")
        except ImportError, ValueError:
            spec = None
        path = spec.origin if spec is not None else None
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


CLAUDE_PATH_ENV = "CALU_CLAUDE_PATH"
"""The environment variable that names the `claude` executable a session starts, for this
machine: set in the environment of calu or in calu's env file (`user_env_file`, e.g.
`/etc/calu/.env` as root). It is no database setting (D279): whoever can change calu's settings
in the database would otherwise choose the program every session runs, as its user."""


def claude_cli_path() -> str | None:
    """Return the `claude` executable of this machine, if `CALU_CLAUDE_PATH` names one.

    Returns:
        The path, or None to use the one bundled with the Agent SDK, else the one on `PATH`
        (`calu.launch.claude_path`).
    """
    value = os.environ.get(CLAUDE_PATH_ENV, "").strip()
    return value or None


def load_secrets(env_file: Path | None = None) -> set[str]:
    """Load the variables of calu's env file into `os.environ`, among them the checker API keys.

    Variables that are already set in the environment are not overwritten. A project's own
    `.env` is deliberately not read: it may hold unrelated secrets, which Claude must not see.

    Args:
        env_file: The env file; None uses `user_env_file()`. A missing file is skipped.

    Returns:
        The names of all variables defined in the file, plus `PROVIDER_KEYS`; the runner hides
        them from Claude Code.
    """
    names = set(PROVIDER_KEYS)
    path = env_file or user_env_file()
    if path.is_file():
        values = dotenv_values(path)
        names |= set(values)
        for key, value in values.items():
            if value is not None and key not in os.environ:
                os.environ[key] = value
    return names
