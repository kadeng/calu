"""A checker on the machine's Claude Code account (`calu.claude_model`, D117).

The CLI itself is not run here: a script stands in for `claude` and records the command line it
was given, which is what has to be right -- the schema inline, no tools, no session
persistence, and an empty working directory. One live test (`live`) runs the real CLI, because
the shape of its JSON is the thing a stand-in cannot verify.
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
        + body,
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

    agent = Agent(claude_cli_model("claude:haiku", cli_path=str(script)), output_type=CheckDecision)
    result = await agent.run("Judge this command: rm -rf /")
    assert result.output.decision is Decision.DENY
    assert "root filesystem" in result.output.reason
    # The token counts reach Pydantic AI, so `checker_runs` records this check like any other.
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 365
    assert result.usage.cache_read_tokens == 11

    argv = record.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-p"
    assert "Judge this command: rm -rf /" in argv[1]
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
    assert where.name.startswith("calu-checker-")
    assert not where.exists(), "and the directory is removed again after the check"


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
