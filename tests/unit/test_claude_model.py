"""A checker on the machine's Claude Code account (`calu.claude_model`, D117).

The CLI itself is not run here: a script stands in for `claude` and records the command line it
was given, its standard input and the instructions file, which is what has to be right -- the
schema inline, no tools, no session persistence, an empty working directory, the content on
standard input and the instructions as the system prompt. One live test (`live`) runs the real
CLI, because the shape of its JSON is the thing a stand-in cannot verify.
"""

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic_ai import Agent

from calu.checkers import CheckDecision, Decision
from calu.claude_model import ClaudeCliError, alias_of, claude_cli_model, is_cli_model
from calu.models import resolve_model

pytestmark = pytest.mark.anyio


def fake_cli(tmp_path: Path, body: str) -> Path:
    """Write a script that stands in for `claude` and records its arguments.

    Args:
        tmp_path: Where to put it.
        body: Shell lines run after the arguments are recorded; they produce the CLI's stdout.

    Returns:
        The script's path.
    """
    script = tmp_path / "fake-claude"
    script.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$RECORD"\nprintf "%s" "$PWD" > "$CWDFILE"\n'
        # What arrives on standard input, and the instructions file with its mode, if any.
        'cat > "$RECORD.stdin"\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "--system-prompt-file" ]; then '
        'cp "$2" "$RECORD.system"; stat -c %a "$2" > "$RECORD.mode"; fi; shift; done\n' + body,
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


RESULT = {
    "is_error": False,
    "result": '{"decision":"DENY"}',
    "structured_output": {
        "decision": "DENY",
        "reason": "it deletes the root filesystem",
        "step_summary": "Refused `rm -rf /`.",
    },
    "usage": {
        "input_tokens": 7,
        "output_tokens": 365,
        "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 5917,
    },
    "total_cost_usd": 0.00137,
}
"""The shape the real CLI returns, measured on 2026-11-10 with CLI 2.1.319."""


def test_the_model_string_is_recognised_and_carries_the_alias() -> None:
    assert is_cli_model("claude:haiku") and alias_of("claude:haiku") == "haiku"
    assert not is_cli_model("anthropic:claude-haiku-4-5")
    assert alias_of("claude:") == "", "no alias leaves the CLI's own default"
    assert type(resolve_model("claude:opus")).__name__ == "FunctionModel"
    with pytest.raises(ValueError):
        claude_cli_model("fireworks:x")


async def test_a_check_runs_the_cli_and_returns_its_structured_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision comes back, and the command line is the one D16 measured."""
    record = tmp_path / "argv.txt"
    cwdfile = tmp_path / "cwd.txt"
    monkeypatch.setenv("RECORD", str(record))
    monkeypatch.setenv("CWDFILE", str(cwdfile))
    script = fake_cli(tmp_path, f"cat <<'JSON'\n{json.dumps(RESULT)}\nJSON\n")

    agent = Agent(
        claude_cli_model("claude:haiku", cli_path=str(script)),
        output_type=CheckDecision,
        instructions="You check commands. Never allow deletions.",
    )
    result = await agent.run("Judge this command: rm -rf /")
    assert result.output.decision is Decision.DENY
    assert "root filesystem" in result.output.reason
    # The token counts reach Pydantic AI, so `checker_runs` records this check like any other.
    # `input_tokens` is the whole prompt, cached parts included: calu counts it that way
    # (`CaseResult.input_tokens`) while the CLI counts the cache beside it, and passing the raw
    # number through made the first real run report a cached share of 119950% (D118).
    assert result.usage.input_tokens == 7 + 11 + 5917
    assert result.usage.output_tokens == 365
    assert result.usage.cache_read_tokens == 11
    assert result.usage.cache_write_tokens == 5917

    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-p"
    # Nothing of the prompt is on the command line (review cc5): the content to judge arrives
    # on standard input, and the instructions are the system prompt, from a private file.
    assert not [a for a in argv if "rm -rf" in a or "Never allow" in a]
    stdin = Path(f"{record}.stdin").read_text(encoding="utf-8")
    assert stdin == "Judge this command: rm -rf /"
    assert Path(f"{record}.system").read_text() == "You check commands. Never allow deletions."
    assert Path(f"{record}.mode").read_text().strip() == "600"
    system_file = Path(argv[argv.index("--system-prompt-file") + 1])
    assert not system_file.exists(), "the instructions file is removed with its directory"
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in argv
    # No tools: a checker must not be able to run anything, and this model type cannot offer
    # the read-only ones either.
    assert argv[argv.index("--tools") + 1] == ""
    # The schema goes in inline; a path is refused by the CLI with "not valid JSON".
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "decision" in schema["properties"]

    # And it runs in an empty directory, not in the project: the working directory's CLAUDE.md
    # would otherwise let the reviewed project write instructions into its reviewer's prompt.
    where = Path(cwdfile.read_text(encoding="utf-8"))
    assert where != Path.cwd() and str(tmp_path) not in str(where)
    assert where.name == "empty" and where.parent.name.startswith("calu-checker-")
    assert system_file.parent == where.parent and system_file.parent != where
    assert not where.parent.exists(), "and the directory is removed again after the check"


async def test_a_prompt_larger_than_one_argument_may_be_reaches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large commit diff failed with "Argument list too long" (128 KiB per argument)."""
    record = tmp_path / "argv.txt"
    monkeypatch.setenv("RECORD", str(record))
    monkeypatch.setenv("CWDFILE", str(tmp_path / "cwd.txt"))
    script = fake_cli(tmp_path, f"cat <<'JSON'\n{json.dumps(RESULT)}\nJSON\n")
    prompt = "# Changeset of this commit\n\n" + "+ä line of a large diff\n" * 12_000
    assert len(prompt.encode()) > 250_000
    instructions = ("Rule. " * 30_000).strip()
    agent = Agent(
        claude_cli_model("claude:haiku", cli_path=str(script)),
        output_type=CheckDecision,
        instructions=instructions,
    )
    result = await agent.run(prompt)
    assert result.output.decision is Decision.DENY
    assert Path(f"{record}.stdin").read_text(encoding="utf-8") == prompt
    assert Path(f"{record}.system").read_text(encoding="utf-8") == instructions
    assert max(len(a) for a in record.read_text(encoding="utf-8").splitlines()) < 10_000


def test_instructions_and_content_are_split_by_who_wrote_them() -> None:
    from types import SimpleNamespace

    from pydantic_ai import CachePoint
    from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

    from calu.claude_model import split_prompt

    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart("system part"),
                UserPromptPart(["# Project context", CachePoint(), "# Step to check\n\nrm"]),
                UserPromptPart("more"),
            ]
        )
    ]
    info = SimpleNamespace(instructions="the checker's instructions")
    instructions, content = split_prompt(messages, info)  # type: ignore[arg-type]
    assert instructions == "the checker's instructions\n\nsystem part"
    assert content == "# Project context\n\n# Step to check\n\nrm\n\nmore"


async def test_a_failing_cli_is_an_error_the_checker_can_escalate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every failure has to be loud: nobody may be left believing a step was approved."""
    monkeypatch.setenv("RECORD", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("CWDFILE", str(tmp_path / "cwd.txt"))

    (tmp_path / "a").mkdir()
    broken = fake_cli(tmp_path / "a", "echo 'not json'\n")
    agent = Agent(claude_cli_model("claude:haiku", cli_path=str(broken)), output_type=CheckDecision)
    with pytest.raises(ClaudeCliError) as not_json:
        await agent.run("judge")
    assert "not JSON" in str(not_json.value)

    (tmp_path / "b").mkdir()
    failing = fake_cli(tmp_path / "b", "echo 'boom' >&2\nexit 3\n")
    agent = Agent(
        claude_cli_model("claude:haiku", cli_path=str(failing)), output_type=CheckDecision
    )
    with pytest.raises(ClaudeCliError) as exited:
        await agent.run("judge")
    assert "exited with 3" in str(exited.value) and "boom" in str(exited.value)

    (tmp_path / "c").mkdir()
    reported = fake_cli(tmp_path / "c", 'printf \'{"is_error": true, "result": "no credit"}\'\n')
    agent = Agent(
        claude_cli_model("claude:haiku", cli_path=str(reported)), output_type=CheckDecision
    )
    with pytest.raises(ClaudeCliError) as said:
        await agent.run("judge")
    assert "no credit" in str(said.value)

    missing = claude_cli_model("claude:haiku", cli_path=str(tmp_path / "no-such-binary"))
    with pytest.raises(ClaudeCliError) as absent:
        await Agent(missing, output_type=CheckDecision).run("judge")
    assert "was not found" in str(absent.value)

    # Any other failure to start the CLI names the cause instead of surfacing as a bare OSError.
    (tmp_path / "d").mkdir()
    plain = tmp_path / "d" / "not-executable"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    unusable = claude_cli_model("claude:haiku", cli_path=str(plain))
    with pytest.raises(ClaudeCliError) as refused:
        await Agent(unusable, output_type=CheckDecision).run("judge")
    assert "could not be run (PermissionError" in str(refused.value)

    (tmp_path / "e").mkdir()
    listed = fake_cli(tmp_path / "e", "echo '[1, 2]'\n")
    agent = Agent(claude_cli_model("claude:haiku", cli_path=str(listed)), output_type=CheckDecision)
    with pytest.raises(ClaudeCliError) as shape:
        await agent.run("judge")
    assert "not a JSON object" in str(shape.value)


@pytest.mark.live
async def test_the_real_cli_answers_a_check_on_the_machine_s_account(tmp_path: Path) -> None:
    """The one thing a stand-in cannot check: the real CLI's JSON and its account.

    Needs a logged-in `claude` (REQUIREMENTS 22: the subscription works out of the box).
    """
    if not os.environ.get("PATH"):  # pragma: no cover - defensive
        pytest.skip("no PATH")
    agent = Agent(claude_cli_model("claude:haiku"), output_type=CheckDecision)
    result = await agent.run(
        "You review one shell command for a coding agent. The command is `rm -rf /`. "
        "Answer with the schema: DENY it."
    )
    assert result.output.decision is Decision.DENY
    assert result.output.reason.strip() and result.output.step_summary.strip()
    assert result.usage.output_tokens > 0, "the usage has to reach calu's cost report"
