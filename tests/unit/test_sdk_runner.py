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
        return GateResult(Verdict.ALLOW)

    async def ask(self, step):
        return HumanAnswer(allow=True)

    async def review_subagent_result(self, result):
        return ""


def test_claude_code_authenticates_as_configured(tmp_path: Path):
    """No billing rule: only checker keys are hidden; Bedrock, Vertex and API keys pass."""
    options = SdkRunner(tmp_path, ClaudeConfig(), {"MISTRAL_API_KEY"})._options(AllowAll(), None)
    assert options.env == {"MISTRAL_API_KEY": ""}
    assert options.permission_mode == "auto"  # calu default (PLAN.md D5)
    off = SdkRunner(tmp_path, ClaudeConfig(auto_mode=False))._options(AllowAll(), None)
    assert off.permission_mode == "default"


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


async def run_hook(tmp_path: Path, gate, auto_mode: bool, effective_mode: str) -> dict:
    runner = SdkRunner(tmp_path, ClaudeConfig(auto_mode=auto_mode))
    options = runner._options(gate, None)
    assert options.permission_mode == ("auto" if auto_mode else "default")
    hook: Any = options.hooks["PreToolUse"][0].hooks[0]  # type: ignore[index]
    data = {"tool_name": "Bash", "tool_input": {"command": "make"}}
    data["permission_mode"] = effective_mode
    return (await hook(data, "tu1", None))["hookSpecificOutput"]


@pytest.mark.anyio
async def test_auto_mode_lets_the_classifier_decide_after_a_checker_allow(tmp_path: Path):
    """Checkers AND auto mode: ALLOW returns no decision (spike_auto_mode.py)."""
    out = await run_hook(tmp_path, FixedVerdict(Verdict.ALLOW, "nit"), True, "auto")
    assert out == {"hookEventName": "PreToolUse", "additionalContext": "nit"}
    for verdict in (Verdict.DENY, Verdict.ASK):
        out = await run_hook(tmp_path, FixedVerdict(verdict), True, "auto")
        assert out["permissionDecision"] == verdict.value


@pytest.mark.anyio
async def test_without_effective_auto_mode_allow_stays_allow(tmp_path: Path):
    """E.g. haiku: auto mode unavailable, the effective mode is "default"."""
    out = await run_hook(tmp_path, FixedVerdict(Verdict.ALLOW), True, "default")
    assert out["permissionDecision"] == "allow"
    out = await run_hook(tmp_path, FixedVerdict(Verdict.ALLOW), False, "auto")
    assert out["permissionDecision"] == "allow"
