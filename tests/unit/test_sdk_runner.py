"""Offline tests of SdkRunner's option building and message translation (no CLI started)."""

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from calu.claude.runner import GateResult, HumanAnswer, Verdict
from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig
from calu.protocol.messages import (
    AgentsUpdate,
    AssistantText,
    ToolFiles,
    ToolUse,
    UserInput,
)


class AllowAll:
    async def pre_tool_use(self, step):
        return GateResult(Verdict.PASS)

    async def ask(self, step):
        return HumanAnswer(allow=True)

    async def review_subagent_result(self, result):
        return ""

    async def review_write_set(self, step):
        return ""


def test_claude_code_authenticates_as_configured(tmp_path: Path):
    """No billing rule: only checker keys are hidden; Bedrock, Vertex and API keys pass."""
    options = SdkRunner(tmp_path, ClaudeConfig(), {"MISTRAL_API_KEY"})._options(AllowAll(), None)
    assert options.env == {"MISTRAL_API_KEY": ""}
    # calu does not set the permission mode: Claude Code takes it from its own settings (D5).
    assert options.permission_mode is None
    chosen = SdkRunner(tmp_path, ClaudeConfig(permission_mode="default"))._options(AllowAll(), None)
    assert chosen.permission_mode == "default"


def test_init_message_records_the_session_for_any_auth_source(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    init = SystemMessage(
        subtype="init", data={"session_id": "s1", "apiKeySource": "ANTHROPIC_API_KEY"}
    )
    assert runner._translate(init) == []
    assert runner.session_id == "s1"


def test_subagent_messages_are_attributed(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    start = AssistantMessage(
        content=[
            ToolUseBlock(
                id="tu1", name="Agent", input={"description": "d", "run_in_background": True}
            )
        ],
        model="m",
    )
    events = runner._translate(start)
    assert isinstance(events[0], ToolUse) and isinstance(events[1], AgentsUpdate)
    assert events[1].agents[0].background is True
    sub = AssistantMessage(content=[TextBlock(text="hi")], model="m", parent_tool_use_id="tu1")
    (text,) = runner._translate(sub)
    assert isinstance(text, AssistantText) and text.agent_id == "tu1"


def test_checker_keys_are_hidden_from_claude(tmp_path: Path, monkeypatch):
    import os

    from calu.config import load_secrets

    env_file = tmp_path / "calu" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("MY_CHECKER_KEY=secret\n", encoding="utf-8")
    monkeypatch.delenv("MY_CHECKER_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    hidden = load_secrets()

    assert os.environ["MY_CHECKER_KEY"] == "secret"
    monkeypatch.delenv("MY_CHECKER_KEY")
    assert {"MY_CHECKER_KEY", "MISTRAL_API_KEY"} <= hidden
    env = SdkRunner(tmp_path, ClaudeConfig(), hidden)._options(AllowAll(), None).env
    assert env["MY_CHECKER_KEY"] == "" and env["MISTRAL_API_KEY"] == ""
    assert "ANTHROPIC_API_KEY" not in env  # Claude Code's own authentication is not touched


def test_skill_text_after_a_skill_call_is_no_user_input(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    skill_text = "Base directory for this skill: /w/.claude/skills/greet\n\n# Greet\n"
    # Without a Skill call, a message with the same start is the user's.
    (own,) = runner._translate(UserMessage(content="Base directory for this skill: mine"))
    assert isinstance(own, UserInput)
    runner._translate(
        AssistantMessage(
            content=[ToolUseBlock(id="s1", name="Skill", input={"skill": "greet"})], model="m"
        )
    )
    runner._translate(
        UserMessage(content=[ToolResultBlock(tool_use_id="s1", content="Launching skill: greet")])
    )
    assert runner._translate(UserMessage(content=[TextBlock(text=skill_text)])) == []
    (later,) = runner._translate(UserMessage(content="Thanks."))
    assert isinstance(later, UserInput) and later.text == "Thanks."


def test_skill_text_links_the_skill_file(tmp_path: Path):
    skill = tmp_path / "greet"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Greet\n")
    runner = SdkRunner(tmp_path, ClaudeConfig())
    runner._translate(
        AssistantMessage(
            content=[ToolUseBlock(id="s1", name="Skill", input={"skill": "greet"})], model="m"
        )
    )
    (files,) = runner._translate(
        UserMessage(content=f"Base directory for this skill: {skill}\n\n# Greet\n")
    )
    assert isinstance(files, ToolFiles) and files.tool_use_id == "s1"
    assert [(f.raw, f.path) for f in files.files] == [("greet", str(skill / "SKILL.md"))]


def test_use_bedrock_sets_the_bedrock_switch_for_claude_code(tmp_path: Path):
    plain = SdkRunner(tmp_path, ClaudeConfig())._options(AllowAll(), None).env
    assert "CLAUDE_CODE_USE_BEDROCK" not in plain
    bedrock = SdkRunner(tmp_path, ClaudeConfig(use_bedrock=True), {"MISTRAL_API_KEY"})
    env = bedrock._options(AllowAll(), None).env
    assert env == {"CLAUDE_CODE_USE_BEDROCK": "1", "MISTRAL_API_KEY": ""}


class FixedVerdict(AllowAll):
    def __init__(self, verdict: Verdict, context: str = "") -> None:
        self.verdict, self.context = verdict, context

    async def pre_tool_use(self, step):
        return GateResult(self.verdict, reason="checker says so", context=self.context)


async def run_hook(tmp_path: Path, gate, effective_mode: str, configured: Any = None) -> dict:
    runner = SdkRunner(tmp_path, ClaudeConfig(permission_mode=configured))
    options = runner._options(gate, None)
    # calu does not set the mode unless the configuration does (D5).
    assert options.permission_mode == configured
    hook: Any = options.hooks["PreToolUse"][0].hooks[0]  # type: ignore[index]
    data = {"tool_name": "Bash", "tool_input": {"command": "make"}}
    data["permission_mode"] = effective_mode
    return (await hook(data, "tu1", None))["hookSpecificOutput"]


@pytest.mark.anyio
async def test_auto_mode_decides_after_a_checker_pass(tmp_path: Path):
    """Checkers AND auto mode: PASS returns no decision (spike_auto_mode.py, D21)."""
    out = await run_hook(tmp_path, FixedVerdict(Verdict.PASS, "nit"), "auto")
    assert out == {"hookEventName": "PreToolUse", "additionalContext": "nit"}
    for verdict in (Verdict.DENY, Verdict.ASK):
        out = await run_hook(tmp_path, FixedVerdict(verdict), "auto")
        assert out["permissionDecision"] == verdict.value


@pytest.mark.anyio
async def test_an_explicit_allow_overrules_the_classifier(tmp_path: Path):
    """ALLOW is the deliberate exception: the step runs whatever auto mode would say (D21)."""
    out = await run_hook(tmp_path, FixedVerdict(Verdict.ALLOW), "auto")
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == "checker says so"


@pytest.mark.anyio
async def test_without_effective_auto_mode_pass_becomes_allow(tmp_path: Path):
    """A model without auto mode, or a user who chose another mode: the checkers decide alone."""
    for mode in ("default", "acceptEdits", "plan"):
        out = await run_hook(tmp_path, FixedVerdict(Verdict.PASS), mode)
        assert out["permissionDecision"] == "allow"
    # Only the mode the hook reports counts, not what the configuration asked for: a session
    # that runs in auto mode must not have its PASS turned into an "allow" that skips the
    # classifier.
    out = await run_hook(tmp_path, FixedVerdict(Verdict.PASS), "auto", configured="default")
    assert "permissionDecision" not in out


def test_the_hook_timeouts_match_what_each_hook_waits_for(tmp_path: Path):
    """PreToolUse never waits for a human; the result review hooks do (see RESULTS.md)."""
    from calu.claude.sdk_runner import HOOK_TIMEOUT_S, REVIEW_HOOK_TIMEOUT_S
    from calu.session import CHECKER_DEADLINE_S

    runner = SdkRunner(tmp_path, ClaudeConfig())
    hooks = runner._options(None, None).hooks  # type: ignore[arg-type]
    assert hooks is not None
    assert hooks["PreToolUse"][0].timeout == HOOK_TIMEOUT_S
    # The checker answers (or is escalated) before the hook's own timeout can strike.
    assert HOOK_TIMEOUT_S > CHECKER_DEADLINE_S
    for event in ("PostToolUse", "SubagentStop"):
        assert hooks[event][0].timeout == REVIEW_HOOK_TIMEOUT_S


@pytest.mark.anyio
async def test_a_workflow_agents_result_is_reviewed_with_its_task(tmp_path: Path):
    """D157: a Workflow script's `agent()` starts subagents without the Agent tool. Their steps
    pass the gate (verified in a real session), but SubagentStop called them unknown and their
    results went unreviewed. The task now comes from the subagent's transcript."""
    import json

    transcript = tmp_path / "agent-a1.jsonl"
    transcript.write_text(
        json.dumps({"type": "system"})
        + "\n"
        + json.dumps({"type": "user", "message": {"content": "Find the flaky tests"}})
        + "\n",
        encoding="utf-8",
    )
    reviewed = []

    class Recording(AllowAll):
        async def review_subagent_result(self, result):
            reviewed.append(result)
            return ""

    runner = SdkRunner(tmp_path, ClaudeConfig())
    hooks = runner._options(Recording(), None).hooks  # type: ignore[arg-type]
    assert hooks is not None
    stop: Any = hooks["SubagentStop"][0].hooks[0]  # called with plain dicts, as the CLI does
    await stop(
        {
            "agent_id": "a1",
            "agent_type": "workflow-subagent",
            "agent_transcript_path": str(transcript),
            "last_assistant_message": "tests/test_x.py is flaky",
        },
        None,
        None,
    )
    (result,) = reviewed
    assert result.prompt == "Find the flaky tests"
    assert result.result == "tests/test_x.py is flaky"
    assert "workflow-subagent" in result.description and "Workflow" in result.description
