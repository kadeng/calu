"""Commands outside Claude Code's sandbox: the step's "Sandbox:" line and the fixed rule (D255)."""

import json
from pathlib import Path

import anyio
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers import Step
from calu.checkers.render import render_step
from calu.claude.sdk_runner import SdkRunner
from calu.config import ClaudeConfig, Config, SandboxConfig
from calu.defaults.instructions import compose_instructions, system_part
from calu.protocol.messages import ChatSendParams, TurnResult
from calu.testing import fake_models, fixed_checker
from tests.unit.helpers import make_session

ALLOWED = SandboxConfig(allow_unsandboxed_commands=True)
EXCLUDED = SandboxConfig(excluded_commands=["scripts/db-tests.sh *", "docker"])


def test_a_call_that_disables_the_sandbox_runs_outside_only_where_allowed():
    """`dangerouslyDisableSandbox` leaves the sandbox only with the setting on."""
    reason = ALLOWED.outside_reason("curl https://example.net", True)
    assert reason.startswith("this command runs OUTSIDE")
    assert "dangerouslyDisableSandbox" in reason
    assert ALLOWED.outside_reason("curl https://example.net", False) == ""
    assert SandboxConfig().outside_reason("curl https://example.net", True) == ""
    # With the sandbox off every command runs outside it; there is nothing to single out.
    off = SandboxConfig(enabled=False, allow_unsandboxed_commands=True)
    assert off.outside_reason("ls", True) == ""


@pytest.mark.parametrize(
    ("command", "outside"),
    [
        ("scripts/db-tests.sh", True),
        ("scripts/db-tests.sh -q tests/db/test_x.py", True),
        ("docker ps", True),
        ("docker", True),
        ("./scripts/db-tests.sh -q", False),
        ("scripts/db-tests.shx", False),
        ("dockerd", False),
        ("uv run pytest", False),
    ],
)
def test_an_excluded_command_may_run_outside(command: str, outside: bool):
    """The match follows the pattern syntax closely enough to err towards a match.

    Args:
        command: The command line.
        outside: Whether the "Sandbox:" line is expected.
    """
    reason = EXCLUDED.outside_reason(command, False)
    assert reason.startswith("this command may run OUTSIDE") is outside
    assert (reason == "") is not outside


def test_the_step_header_holds_the_sandbox_line(tmp_path: Path):
    """calu's line stands in the header, before the command; without it there is none."""
    command = "scripts/db-tests.sh -q"
    step = Step(
        tool_name="Bash",
        tool_input={"command": command},
        cwd=str(tmp_path),
        sandbox=EXCLUDED.outside_reason(command, False),
    )
    text = render_step(step, tmp_path)
    assert "\nSandbox: this command may run OUTSIDE" in text
    assert "`scripts/db-tests.sh *`" in text
    assert text.index("Sandbox:") < text.index("Command:")
    plain = Step(tool_name="Bash", tool_input={"command": command}, cwd=str(tmp_path))
    assert "Sandbox:" not in render_step(plain, tmp_path)


def test_the_fixed_part_of_the_command_checker_holds_the_rule():
    """The rule is calu's fixed text: it reaches the checker whatever the user's part says."""
    assert "# Commands outside the sandbox" in system_part("command")
    prompt = compose_instructions("command", "Only my own rules.")
    assert "Judge such a call more strictly" in prompt
    assert "# Commands outside the sandbox" not in system_part("edit")


def test_the_setting_reaches_claude_code(tmp_path: Path):
    """`allow_unsandboxed_commands` is passed as `allowUnsandboxedCommands` with `--settings`."""
    runner = SdkRunner(tmp_path, ClaudeConfig(sandbox=ALLOWED))
    options = runner._options(None, None)  # type: ignore[arg-type]
    assert isinstance(options.settings, str)
    assert json.loads(options.settings)["sandbox"]["allowUnsandboxedCommands"] is True
    assert SandboxConfig().settings()["sandbox"]["allowUnsandboxedCommands"] is False


@pytest.mark.anyio
async def test_the_command_checker_sees_the_line_and_the_rule(tmp_path: Path):
    """A session adds the line to a Bash step that disables the sandbox, and only to that one."""
    seen: list[str] = []
    passing = fixed_checker("PASS")
    assert isinstance(passing, FunctionModel) and passing.function is not None
    answer = passing.function

    def capture(messages: list[ModelMessage], info: AgentInfo):
        """Record what the checker model got and answer PASS.

        Args:
            messages: The request messages.
            info: Agent information.

        Returns:
            The fixed checker's answer.
        """
        seen.append(repr(messages))
        return answer(messages, info)  # type: ignore[misc]

    models = fake_models() | {"command": FunctionModel(capture)}
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Bash",
                    "input": {
                        "command": "curl -sI https://pypi.org",
                        "dangerouslyDisableSandbox": True,
                    },
                    "result": "200",
                },
                {"tool": "Bash", "input": {"command": "make"}, "result": "ok"},
                {"text": "Done."},
            ]
        ],
        config=Config(claude=ClaudeConfig(sandbox=ALLOWED)),
        checker_models=models,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="Check that PyPI answers"))
        await rec.wait_for(TurnResult)
        await session.stop()
    assert len(seen) == 2
    assert "Sandbox: this command runs OUTSIDE" in seen[0]
    assert "Commands outside the sandbox" in seen[0]
    assert "Sandbox: this command" not in seen[1]
