"""Session configuration, built from the database (PLAN.md D6).

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

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field

from calu.checkers.policy import Route

EMPTY_CHAT_SUMMARY = "# Chat summary\n\nNo turns have been folded into this summary yet.\n"
"""Chat summary of a session without folded turns."""

SETTINGS_KEY = "settings"
"""Key of the `config` rows that hold a partial `Config`."""


class CheckerConfig(BaseModel):
    """Model and limits of one checker. `model` is a Pydantic AI model string.

    `max_tokens` caps one answer, `output_tokens_limit` all answers of one check (thinking
    included on most providers). A check that hits either is retried once, then escalated.
    """

    model: str = "mistral:mistral-small-2603"  # Mistral Small 4
    request_limit: int = 3
    output_tokens_limit: int = 8_000
    max_tokens: int = 4_000


class ContextConfig(BaseModel):
    """Context summary settings (see `calu.context`).

    Every `fold_every` unfolded turns, the oldest `fold_count` turns are folded into
    CHAT_SUMMARY.md. `files` are workspace-relative files included verbatim in the cached context
    prefix when they exist; `skill_dirs` contribute the name and description of each skill.
    """

    fold_every: int = Field(default=30, ge=2)
    fold_count: int = Field(default=20, ge=1)
    summarizer_model: str = "mistral:ministral-3b-latest"
    narrate: bool = True
    """Narrate Claude's thinking and text for the checkers (`calu.context.narrator`)."""
    narrator_model: str = "mistral:mistral-small-2603"  # Mistral Small 4
    max_recent_chars: int = Field(default=40_000, ge=1_000)
    files: list[str] = Field(
        default_factory=lambda: ["AGENTS.md", "CLAUDE.md", "GLOSSARY.md", "MEMORY.md"]
    )
    skill_dirs: list[str] = Field(default_factory=lambda: [".claude/skills"])


DEFAULT_DENY_READ = (
    "~/.config/calu",
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
"""Paths that sandboxed commands must not read (PLAN.md D15): calu's encrypted credentials,
Claude Code's own credentials, SSH, cloud and registry credentials. Claude Code's built-in
defaults additionally deny the container sockets, `/run/dbus` and `/run/user`."""


class SandboxConfig(BaseModel):
    """Isolation of Bash commands by Claude Code's own sandbox (PLAN.md D15).

    calu passes these settings with `--settings`, which overrides the project's and the user's
    settings files, so the agent cannot switch the sandbox off by writing
    `.claude/settings.json`. The sandbox needs `bwrap` and `socat` on the machine
    (`calu.preflight`).

    `fail_if_unavailable` makes Claude Code refuse to start without a working sandbox, instead
    of running commands unsandboxed. `allow_unsandboxed_commands` False stops a call from
    leaving the sandbox with `dangerouslyDisableSandbox`. `auto_allow_bash` stays False: Claude
    Code would otherwise approve sandboxed Bash calls by itself, which would skip the auto-mode
    classifier that PLAN.md D5 uses as the second opinion next to the checkers.

    `excluded_commands` run outside the sandbox and are therefore only covered by the checkers
    and the deny lists; the default is empty. `allowed_domains` and `denied_domains` configure
    the sandbox's network: with an empty `allowed_domains` the sandbox blocks outgoing network
    access (verified in `spikes/RESULTS.md`).
    """

    enabled: bool = True
    fail_if_unavailable: bool = True
    allow_unsandboxed_commands: bool = False
    auto_allow_bash: bool = False
    weaker_nested_sandbox: bool = False
    """Weaker sandbox inside unprivileged Docker containers (Linux); reduces the isolation."""
    excluded_commands: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_READ))
    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    allow_unix_sockets: list[str] = Field(default_factory=list)

    def settings(self) -> dict[str, Any]:
        """Return the Claude Code settings document for `--settings`.

        Returns:
            `{"sandbox": {...}}` with the keys Claude Code expects (camel case). The `network`
            and `filesystem` entries are left out when they would be empty.

        Example:
            `SandboxConfig().settings()["sandbox"]["failIfUnavailable"]` is True.
        """
        sandbox: dict[str, Any] = {
            "enabled": self.enabled,
            "failIfUnavailable": self.fail_if_unavailable,
            "allowUnsandboxedCommands": self.allow_unsandboxed_commands,
            "autoAllowBashIfSandboxed": self.auto_allow_bash,
        }
        if self.weaker_nested_sandbox:
            sandbox["enableWeakerNestedSandbox"] = True
        if self.excluded_commands:
            sandbox["excludedCommands"] = list(self.excluded_commands)
        if self.deny_read:
            sandbox["filesystem"] = {"denyRead": list(self.deny_read)}
        network: dict[str, Any] = {}
        if self.allowed_domains:
            network["allowedDomains"] = list(self.allowed_domains)
        if self.denied_domains:
            network["deniedDomains"] = list(self.denied_domains)
        if self.allow_unix_sockets:
            network["allowUnixSockets"] = list(self.allow_unix_sockets)
        if network:
            sandbox["network"] = network
        return {"sandbox": sandbox}


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Reasoning effort levels of Claude Code (`--effort`, `/effort`)."""


class ClaudeConfig(BaseModel):
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

    `permission_mode`: None (default) leaves the permission mode to Claude Code, whose own
    default is the auto mode — the intended way to run calu, because then a step runs only if
    the checkers (or the allow lists) **and** auto mode's classifier approve it (PLAN.md D5).
    calu does not set the mode: changing it away from auto is a deliberate act of the user, in
    Claude Code's own settings or here. Whatever the mode ends up being, the checkers work: the
    hook reads the effective mode from its input, defers a PASS to the classifier where the mode
    is "auto", and otherwise answers "allow" itself, so that the checkers decide alone (D21).

    `stream_partial`: if true (default), text and thinking are also streamed token by token
    (`assistant.delta` events); otherwise they arrive per completed block only.

    `sandbox`: how Bash commands are isolated (`SandboxConfig`); calu passes it to Claude Code
    with `--settings`, which overrides the settings files in the project.
    """

    use_bedrock: bool = False
    permission_mode: (
        Literal["default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"] | None
    ) = None
    """Permission mode for Claude Code; None leaves it to Claude Code (its default is "auto")."""
    stream_partial: bool = True
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    """How Claude Code returns thinking: "summarized" (readable summaries, shown by front ends
    as `assistant.thinking`) or "omitted" (empty thinking blocks, nothing is shown)."""
    model: str | None = "opus"
    effort: Effort | None = None
    cli_path: str | None = None
    setting_sources: list[str] = Field(default_factory=lambda: ["project"])
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    """Isolation of Bash commands (PLAN.md D15, D4)."""


class AutoEscalate(BaseModel):
    """Categories whose steps always wait for the human, whatever the checker decided.

    `commands`: Bash and other tool calls reviewed by the command checker. `edits`: file edits.
    `subagents`: final results of subagents. The checker still runs; where it would have
    approved (ALLOW / ACCEPT / ANNOTATE), the step is escalated instead, with the checker's
    decision and reason in the escalation. A checker DENY stays a DENY. Read-only tools that the
    policy allows without a check are not affected. With training mode on as well, the training
    review (labelling the checker's decision) comes first, then the escalation (running the step).
    """

    commands: bool = False
    edits: bool = False
    subagents: bool = False


class Config(BaseModel):
    """The complete configuration with defaults for every field."""

    command_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    edit_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    subagent_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    changeset_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    """The checker that reviews a whole change before a commit (PLAN.md D26)."""
    check_subagent_results: bool = True
    auto_escalate: AutoEscalate = Field(default_factory=AutoEscalate)
    """Categories that always wait for the human (see `AutoEscalate`); all off by default."""
    escalation_timeout_s: float = Field(default=8 * 3600, ge=0)
    """How long an escalation waits for a human before the session stops (PLAN.md D27).

    An unsupervised run can sit in front of an escalation for a long time; at some point the
    session should give the machine and the Claude session back instead of waiting for ever. On
    the timeout the step is denied and the session stops, with Claude's session id recorded, so
    that `calu run --resume <id>` takes the conversation up again. 0 waits without a limit.
    """
    hidden_env: list[str] = Field(default_factory=list)
    """Further environment variables (e.g. API keys of other checker providers) that Claude
    Code must not see, in addition to the ones from the env files and `PROVIDER_KEYS`."""
    tool_routes: dict[str, Route] = Field(default_factory=dict)
    default_route: Route = Route.COMMAND
    context: ContextConfig = Field(default_factory=ContextConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)

    def with_model(self, model: str) -> Config:
        """Return a copy in which every model role uses `model`.

        The roles are the command, edit and subagent result checkers, the narrator and the
        summarizer (e.g. for a test run with one model).

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
                "context": self.context.model_copy(
                    update={"narrator_model": model, "summarizer_model": model}
                ),
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

    Args:
        layers: Partial configurations in increasing precedence (global, then project).
        checker_rows: (name, model, request_limit, output_tokens_limit, max_tokens) of the
            enabled checker types. "command", "edit" and "subagent-result" set the checker
            fields of `Config`; "narrator" sets `context.narrator_model`; other names are
            ignored here.

    Returns:
        The validated configuration.

    Raises:
        pydantic.ValidationError: If the merged values do not match `Config`.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_settings(merged, layer)
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

PROVIDER_KEYS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "mistral": ("MISTRAL_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}
"""Pydantic AI model-string prefix -> environment variables, any one of which holds the key.
Prefixes that are not listed (e.g. "bedrock", "ollama", "test") need no key or take their
credentials from elsewhere, so they are not checked."""


def load_secrets(env_file: Path | None = None) -> set[str]:
    """Load checker API keys from calu's env file into `os.environ`.

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
