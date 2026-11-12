"""A checker model that runs on the machine's Claude Code account (PLAN.md §3, D16, D117).

It lives next to `calu.models` rather than in the `calu.claude` package, which imports the
checkers and would make the import circular.

REQUIREMENTS 22 asks for the Anthropic **subscription** to work for the model layer out of the
box, and an API client cannot use it: a claude.ai subscription authenticates the `claude` CLI,
not arbitrary API calls. So a checker whose model string starts with `claude:` is not an API
client at all -- each check runs the CLI once in headless mode and reads its structured answer:

    claude -p --system-prompt-file <instructions> --output-format json
           --json-schema '<the schema>' --model <alias> --tools "" --no-session-persistence
           --strict-mcp-config --disable-slash-commands          < <the content to judge>

Measured against CLI 2.1.319 (D16, re-measured 2026-11-10): the answer comes back in the
`structured_output` field of the result JSON, with `usage` and `total_cost_usd` beside it, so a
check costs and reports like any other. `--json-schema` takes the schema **inline**, not a path
-- a path is refused with "not valid JSON".

How the prompt reaches the CLI (measured 2026-11-12 against the same version):

- **The content to judge goes in on standard input**, which `claude -p` reads when no prompt
  argument is given. As an argument it was limited to 128 KiB by the kernel (`MAX_ARG_STRLEN`:
  "Argument list too long" for a large commit), and it was readable in the process list.
- **The checker's instructions are the system prompt**, from a file of mode 0600 passed with
  `--system-prompt-file`. They used to be the first paragraphs of the same user text as the
  step written by the agent under review; now the two are in different roles, as they are for
  an API checker. The flag replaces Claude Code's own system prompt; the structured answer
  works without it (about 2.9k instead of 5.9k tokens of fixed prompt per call).
- `--strict-mcp-config` and `--disable-slash-commands`: no MCP server and no skill of the
  user's Claude Code setup is loaded into a reviewer.

Not done, on purpose: `--setting-sources ""` would keep the user-level `~/.claude` settings
out of the call, but those settings are also where an installation says how to authenticate
(`apiKeyHelper`, the Bedrock or Vertex variables in `env`), and a checker that cannot sign in
escalates every step. The user-level `CLAUDE.md` is therefore still read by the CLI.

What this model type cannot do, and why it is not the default:

- **No tools.** The CLI runs with `--tools ""`, so a checker on this model cannot read a file,
  glob or grep (`calu.checkers.tools`). It judges what the context gives it. A checker whose
  instructions tell it to go and look will do worse here than on an API model, which is why
  `calu.preflight` warns when one is configured.
- **One process per check**, so about 1-3 s of start-up on top of the model's own time.
- **A fixed part of Claude Code's own prompt** (tool-less, about 3k tokens, cached by the CLI
  between calls) is part of every call, so the token numbers are larger than an API checker's
  for the same context.

The CLI runs in an **empty temporary directory**, never in the project: Claude Code reads the
working directory's `CLAUDE.md` and settings, and that would let the project under review write
instructions straight into the prompt of the agent reviewing it.
"""

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

log = logging.getLogger(__name__)

PREFIX = "claude:"
"""Model strings this module answers: `claude:haiku`, `claude:sonnet`, `claude:opus`, ..."""

DEFAULT_TIMEOUT_S = 180.0
"""How long one check may take before it is reported as a failure.

A process start plus a model answer is seconds; three minutes is the point at which something
is wrong rather than slow, and `Checker.check` turns the failure into an escalation.
"""


def is_cli_model(model: str) -> bool:
    """Tell whether a model string means "run the check through Claude Code".

    Args:
        model: The configured model string.

    Returns:
        True for `claude:<alias>`.
    """
    return model.startswith(PREFIX)


def alias_of(model: str) -> str:
    """Return the Claude Code model alias of a `claude:` model string.

    Args:
        model: E.g. `claude:haiku`.

    Returns:
        The alias (`haiku`), or "" if none was given, which leaves the CLI's own default.
    """
    return model[len(PREFIX) :].strip()


def split_prompt(messages: Sequence[ModelMessage], info: AgentInfo) -> tuple[str, str]:
    """Split what the agent would have sent to an API into instructions and content.

    The instructions are the agent's instructions and every system part, in order: text the
    operator wrote. The content is every user part in order: the context and the step, which
    hold text of the agent under review. `CachePoint` markers are dropped: they are
    instructions to an API provider, and the CLI does its own caching. Binary content is
    replaced by a note rather than skipped silently, so a checker that was given an image
    learns that it did not get it.

    Args:
        messages: The conversation as Pydantic AI built it.
        info: The agent's own description; `info.instructions` holds the system instructions.

    Returns:
        `(instructions, content)`, each joined with blank lines; either may be "".
    """
    instructions: list[str] = [info.instructions] if info.instructions else []
    content: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                instructions.append(str(part.content))
            elif isinstance(part, UserPromptPart):
                if isinstance(part.content, str):
                    content.append(part.content)
                    continue
                for piece in part.content:
                    if isinstance(piece, str):
                        content.append(piece)
                    elif type(piece).__name__ != "CachePoint":
                        content.append(f"[a {type(piece).__name__} that this model cannot read]")
    return (
        "\n\n".join(part for part in instructions if part.strip()),
        "\n\n".join(part for part in content if part.strip()),
    )


def _usage_of(answer: dict[str, Any]) -> RequestUsage:
    """Read the token counts out of the CLI's result JSON.

    Args:
        answer: The parsed result object.

    Returns:
        The usage, with zeros for anything the CLI did not report, so that `checker_runs` holds
        the same four numbers as for an API checker. `input_tokens` is the **whole** prompt,
        cached parts included, which is calu's convention and not the CLI's. The CLI also
        reports `total_cost_usd`, which is **not** carried over: `RequestUsage.details` takes
        integers, and calu's reports price tokens themselves rather than trusting a per-call
        figure.
    """
    usage = answer.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    return RequestUsage(
        # The CLI counts the cached prompt **beside** `input_tokens`, and calu counts it
        # **inside** ("input tokens the run used, cached ones included": `CaseResult`,
        # `CheckerStats.cached_share`). Passing the raw number through made the first real run
        # report "119950% of them from the provider's cache" (D118).
        input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


class ClaudeCliError(RuntimeError):
    """The CLI could not be run, timed out, or did not answer with the schema.

    `Checker.check` catches it and escalates the step with the message, which is the behaviour
    a failing model has to have: nobody may be left believing a step was approved.
    """


def claude_cli_model(
    model: str, cli_path: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> FunctionModel:
    """Build the model that answers a check by running the Claude Code CLI once.

    Side effects (per call): creates a temporary directory (mode 0700) with the instructions
    file (mode 0600) and an empty working directory, starts `claude` in the latter as a
    subprocess with the content to judge on its standard input, and removes the directory
    afterwards. Nothing of the prompt is on the command line.

    Args:
        model: The configured model string, e.g. `claude:haiku`.
        cli_path: Path to the `claude` executable; None looks it up on PATH.
        timeout_s: How long one call may take.

    Returns:
        A `FunctionModel` usable wherever a checker model is expected. Building it starts
        nothing and needs no account, exactly like resolving an API model string.

    Raises:
        ValueError: If `model` is not a `claude:` string.
    """
    if not is_cli_model(model):
        raise ValueError(f"{model!r} is not a {PREFIX} model string")
    alias = alias_of(model)

    async def run_check(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Answer one check through the CLI.

        Args:
            messages: The conversation Pydantic AI built.
            info: The agent's description, including the output tool whose schema is asked for.

        Returns:
            A response holding the structured answer as a call of that output tool.

        Raises:
            ClaudeCliError: If the CLI is missing or cannot be started, fails, times out, or
                answers with something that is not its result JSON.
        """
        if not info.output_tools:
            raise ClaudeCliError(
                "a claude: checker needs a structured output type; this agent asked for free text"
            )
        tool = info.output_tools[0]
        instructions, content = split_prompt(messages, info)
        command = [
            cli_path or "claude",
            "-p",
            "--output-format",
            "json",
            # Inline, not a path: a path is refused with "--json-schema is not valid JSON".
            "--json-schema",
            json.dumps(tool.parameters_json_schema),
            # No tools: this model type cannot offer the checker's read-only tools, and a
            # checker must not be able to run anything.
            "--tools",
            "",
            "--no-session-persistence",
            # Nothing of the user's Claude Code setup that could act or speak in a reviewer:
            # no MCP servers, no skills or commands.
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if alias:
            command += ["--model", alias]
        with tempfile.TemporaryDirectory(prefix="calu-checker-") as scratch:
            empty = Path(scratch) / "empty"
            empty.mkdir()
            if instructions:
                # The instructions as the system prompt, in a file only this user can read: in
                # another role than the content, and not in the process list.
                system_file = Path(scratch) / "instructions.md"
                descriptor = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(instructions)
                command += ["--system-prompt-file", str(system_file)]
            try:
                with anyio.fail_after(timeout_s):
                    done = await anyio.run_process(
                        command, cwd=empty, check=False, input=content.encode("utf-8")
                    )
            except FileNotFoundError as exc:
                raise ClaudeCliError(
                    f"the Claude Code CLI was not found ({exc}); a claude: checker model needs "
                    "`claude` on PATH or `claude.cli_path` set"
                ) from exc
            except TimeoutError as exc:
                raise ClaudeCliError(f"the check did not answer within {timeout_s:g} s") from exc
            except (OSError, anyio.BrokenResourceError) as exc:
                raise ClaudeCliError(
                    f"the Claude Code CLI could not be run ({type(exc).__name__}: {exc}); "
                    f"the prompt was {len(content)} characters, passed on standard input"
                ) from exc
        if done.returncode != 0:
            raise ClaudeCliError(
                f"claude -p exited with {done.returncode}: "
                f"{done.stderr.decode(errors='replace').strip()[:400] or '(no output)'}"
            )
        try:
            answer = json.loads(done.stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise ClaudeCliError(f"the CLI's answer was not JSON: {exc}") from exc
        if not isinstance(answer, dict):
            raise ClaudeCliError("the CLI's answer was not a JSON object")
        if answer.get("is_error"):
            raise ClaudeCliError(f"the CLI reported an error: {answer.get('result') or answer}")
        structured = answer.get("structured_output")
        if not isinstance(structured, dict):
            # The CLI answered in prose. Returning that as text lets Pydantic AI retry with its
            # own "you must use the tool" message rather than failing the check outright.
            text = str(answer.get("result") or "")[:2000]
            log.warning("a claude: check answered without structured output: %s", text[:200])
            return ModelResponse(parts=[TextPart(text)], usage=_usage_of(answer))
        return ModelResponse(parts=[ToolCallPart(tool.name, structured)], usage=_usage_of(answer))

    return FunctionModel(run_check, model_name=f"claude-code-{alias or 'default'}")
