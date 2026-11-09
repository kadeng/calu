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
from calu.protocol.messages import AutoEscalate

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

    `auto_mode`: if true (default, PLAN.md D5), Claude Code runs in its "auto" permission mode
    and its classifier decides as well: a step runs only if the checkers (or the allow lists)
    and auto mode approve it. Checker DENY and ESCALATE work as without it. Models without auto
    mode (haiku) fall back to the default mode, where the checkers decide alone.

    `stream_partial`: if true (default), text and thinking are also streamed token by token
    (`assistant.delta` events); otherwise they arrive per completed block only.
    """

    use_bedrock: bool = False
    auto_mode: bool = True
    stream_partial: bool = True
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    """How Claude Code returns thinking: "summarized" (readable summaries, shown by front ends
    as `assistant.thinking`) or "omitted" (empty thinking blocks, nothing is shown)."""
    model: str | None = "opus"
    effort: Effort | None = None
    cli_path: str | None = None
    setting_sources: list[str] = Field(default_factory=lambda: ["project"])


class Config(BaseModel):
    """The complete configuration with defaults for every field."""

    command_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    edit_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    subagent_checker: CheckerConfig = Field(default_factory=CheckerConfig)
    check_subagent_results: bool = True
    auto_escalate: AutoEscalate = Field(default_factory=AutoEscalate)
    """Categories that always wait for the human (see `AutoEscalate`); all off by default."""
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
