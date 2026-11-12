"""Scripted sessions for tests, screenshots and the demo of the full-screen client.

A scenario is a list of `Item`s: protocol events with the time that passes on the client's
clock before each. `play` sends them through a `calu.tui.fake.FakeSession` to the attached
client as live events; `preload` records them as history, which a client that attaches later
gets replayed (without times).

The builders (`user`, `text`, `step`, `subagent`, `escalation`, `question`, `turn_end`) return
the events one thing in a session produces, in the order a session sends them. `SCENARIOS`
names the complete scripts:

    simple_turn     a prompt, thinking, text, three checked steps with a diff, the turn end
    escalation      the same turn, a denied step, two queued messages, one open escalation
    showcase        two subagents, an escalation, a question, the queue, an honesty mark
    question_multi  one set of three questions, one of them with several choices
    two_subagents   a foreground and a background subagent with steps of their own
    stopped         a session that has stopped
    long_session    2,000 steps in 200 turns
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from calu.tui.fake import FakeSession


@dataclass(frozen=True)
class Item:
    """One event of a scenario and the seconds that pass on the client clock before it."""

    method: str
    params: dict[str, Any]
    after: float = 0.0


BEFORE = '''def check_step(step: Step, rules: Rules) -> Verdict:
    """Judge one step against the rules."""
    for rule in rules.deny:
        if rule.matches(step):
            return Verdict.ASK
    return Verdict.PASS


def gate(step: Step, rules: Rules) -> bool:
    """Tell whether the step may run."""
    return check_step(step, rules) is Verdict.PASS
'''
"""The file content before the edit of the scenarios."""

AFTER = BEFORE.replace("check_step", "judge_step")
"""The file content after the edit of the scenarios."""

FORCE_PUSH = "git push --force origin main"
"""The command the scenarios' escalation is about."""

RUNNER_QUESTIONS: list[dict[str, Any]] = [
    {
        "question": "Which test runner should the new tests use?",
        "header": "Runner",
        "multiSelect": False,
        "options": [
            {"label": "pytest", "description": "what the existing unit tests use"},
            {"label": "unittest", "description": "standard library only"},
        ],
    },
]
"""A set of one question with one choice."""

SETUP_QUESTIONS: list[dict[str, Any]] = [
    *RUNNER_QUESTIONS,
    {
        "question": "Which checks should run before each commit?",
        "header": "Checks",
        "multiSelect": True,
        "options": [
            {"label": "unit tests", "description": "uv run pytest"},
            {"label": "type check", "description": "uv run pyright"},
            {"label": "lint", "description": "uv run ruff check ."},
        ],
    },
    {
        "question": "Where do the fixtures of the new tests go?",
        "header": "Fixtures",
        "multiSelect": False,
        "options": [
            {"label": "tests/unit/conftest.py", "description": "shared with the other tests"},
            {"label": "next to each test", "description": "one file per test module"},
        ],
    },
]
"""A set of three questions; the second one takes several options."""


def wait(seconds: float) -> Item:
    """Build a pause: time passes on the client clock and no event is sent.

    Args:
        seconds: How long.

    Returns:
        An item with an empty method, which `play` and `preload` do not send.
    """
    return Item("", {}, seconds)


def state(name: str, after: float = 0.0) -> Item:
    """Build a `session.state` event.

    Args:
        name: The session state.
        after: Seconds on the clock before it.

    Returns:
        The item.
    """
    return Item("session.state", {"state": name}, after)


def user(message: str, source: str = "user") -> list[Item]:
    """Build the events of a message Claude takes in at the start of a turn.

    Args:
        message: The text.
        source: `user`, `system` or `cross_session`.

    Returns:
        `user.input` and the state "running".
    """
    return [Item("user.input", {"text": message, "source": source}), state("running")]


def text(message: str, agent: str | None = None, after: float = 1.0) -> list[Item]:
    """Build a completed text of Claude.

    Args:
        message: The Markdown text.
        agent: The subagent that wrote it; None for the main agent.
        after: Seconds on the clock before it.

    Returns:
        One `assistant.text` event.
    """
    return [Item("assistant.text", {"text": message, "agent_id": agent}, after)]


def thinking(message: str, after: float = 1.0) -> list[Item]:
    """Build a completed thinking text of the main agent.

    Args:
        message: The text.
        after: Seconds on the clock before it.

    Returns:
        One `assistant.thinking` event.
    """
    return [Item("assistant.thinking", {"text": message}, after)]


def step(
    tool_use_id: str,
    tool: str,
    tool_input: dict[str, Any],
    summary: str,
    decision: str = "PASS",
    route: str = "command",
    comment: str = "",
    reason: str = "",
    result: str | None = "ok",
    duration: float = 0.2,
    agent: str | None = None,
    diff: tuple[str, str, str] | None = None,
    is_error: bool = False,
) -> list[Item]:
    """Build the events of one tool call that a checker judged.

    Args:
        tool_use_id: The id of the call.
        tool: The tool name.
        tool_input: The tool input.
        summary: The checker's one-line summary.
        decision: PASS, ALLOW or DENY.
        route: The route of the check.
        comment: A remark of the checker to Claude.
        reason: The checker's reason.
        result: The tool's result; None for a step that still runs. A denied step gets the
            refusal as its result.
        duration: Seconds the tool call takes on the clock.
        agent: The subagent that made the call; None for the main agent.
        diff: `(path, before, after)` of an edit.
        is_error: Whether the result is an error.

    Returns:
        `tool.use`, an `edit.diff` if given, `check.result` and `tool.result`.
    """
    items = [
        Item(
            "tool.use",
            {"tool_use_id": tool_use_id, "name": tool, "input": tool_input, "agent_id": agent},
            0.5,
        )
    ]
    if diff is not None:
        path, before, after = diff
        items.append(
            Item(
                "edit.diff",
                {
                    "tool_use_id": tool_use_id,
                    "agent_id": agent,
                    "path": path,
                    "before": before,
                    "after": after,
                    "start_line": 1,
                    "end_line": 11,
                },
            )
        )
    items.append(
        Item(
            "check.result",
            {
                "tool_use_id": tool_use_id,
                "agent_id": agent,
                "tool_name": tool,
                "route": route,
                "decision": decision,
                "reason": reason,
                "step_summary": summary,
                "comment": comment,
            },
            0.3,
        )
    )
    if decision == "DENY":
        result, is_error = f"The step was not run: {reason}", True
    if result is not None:
        items.append(
            Item(
                "tool.result",
                {
                    "tool_use_id": tool_use_id,
                    "agent_id": agent,
                    "content": result,
                    "is_error": is_error,
                },
                duration,
            )
        )
    return items


def agent_info(
    agent_id: str, description: str, kind: str, background: bool, status: str = "running"
) -> dict[str, Any]:
    """Build one `AgentInfo`.

    Args:
        agent_id: The id of the Agent call.
        description: What the subagent is for.
        kind: Its `subagent_type`.
        background: Whether it runs in the background.
        status: running, completed, failed or stopped.

    Returns:
        The entry for `agents.update`.
    """
    return {
        "agent_id": agent_id,
        "description": description,
        "subagent_type": kind,
        "background": background,
        "status": status,
    }


def subagent(agents: list[dict[str, Any]], prompt: str) -> list[Item]:
    """Build the events of the Agent call that starts the newest subagent of a list.

    Args:
        agents: Every subagent of the session so far; the last entry is the new one.
        prompt: The prompt the subagent is started with.

    Returns:
        `tool.use` of the Agent call, its `check.result` and `agents.update`.
    """
    new = agents[-1]
    call = {
        "description": new["description"],
        "prompt": prompt,
        "subagent_type": new["subagent_type"],
        "run_in_background": new["background"],
    }
    return [
        *step(
            new["agent_id"],
            "Agent",
            call,
            f"started the subagent {new['description']}",
            route="allow",
            result=None,
        ),
        Item("agents.update", {"agents": [dict(agent) for agent in agents]}),
    ]


def escalation(
    escalation_id: str,
    tool_use_id: str,
    tool: str,
    tool_input: dict[str, Any],
    reason: str,
    summary: str,
    route: str = "command",
    agent: str | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> list[Item]:
    """Build the events of a step that waits for the human.

    Args:
        escalation_id: The id of the escalation.
        tool_use_id: The id of the tool call.
        tool: The tool name.
        tool_input: The tool input.
        reason: Why the step waits.
        summary: The one-line summary of the step.
        route: The route of the check ("escalate" for a deny rule, "question" for a question).
        agent: The subagent that made the call; None for the main agent.
        questions: The questions of an `AskUserQuestion` call.

    Returns:
        `tool.use`, `check.result` with ESCALATE, `escalation.request` and the state
        "escalated".
    """
    command = tool_input.get("command") or tool_input.get("file_path") or ""
    return [
        Item(
            "tool.use",
            {"tool_use_id": tool_use_id, "name": tool, "input": tool_input, "agent_id": agent},
            0.5,
        ),
        Item(
            "check.result",
            {
                "tool_use_id": tool_use_id,
                "agent_id": agent,
                "tool_name": tool,
                "route": route,
                "decision": "ESCALATE",
                "reason": reason,
                "step_summary": summary,
            },
            0.2,
        ),
        Item(
            "escalation.request",
            {
                "escalation_id": escalation_id,
                "tool_use_id": tool_use_id,
                "agent_id": agent,
                "tool_name": tool,
                "tool_input": tool_input,
                "reason": reason,
                "rendered": f"**{tool}**\n\n```\n{command}\n```" if command else f"**{tool}**",
                "questions": questions or [],
            },
        ),
        state("escalated"),
    ]


def question(
    escalation_id: str,
    tool_use_id: str,
    questions: list[dict[str, Any]],
    agent: str | None = None,
) -> list[Item]:
    """Build the events of a question of the agent.

    Args:
        escalation_id: The id of the escalation.
        tool_use_id: The id of the `AskUserQuestion` call.
        questions: The questions as Claude asked them.
        agent: The subagent that asks; None for the main agent.

    Returns:
        The events of `escalation` with the route "question".
    """
    return escalation(
        escalation_id,
        tool_use_id,
        "AskUserQuestion",
        {"questions": questions},
        "Claude asks a question",
        str(questions[0].get("question", "a question")),
        route="question",
        agent=agent,
        questions=questions,
    )


def turn_end(cost: float, background_tasks: int = 0, after: float = 1.0) -> list[Item]:
    """Build the events of the end of a turn.

    Args:
        cost: The cost of the Claude session so far.
        background_tasks: How many background tasks still run.
        after: Seconds on the clock before it.

    Returns:
        `turn.result` and the state "idle".
    """
    return [
        Item(
            "turn.result",
            {"cost_usd": cost, "num_turns": 1, "background_tasks": background_tasks},
            after,
        ),
        state("idle"),
    ]


def _opening() -> list[Item]:
    """Build the start every hand-written scenario shares: prompt, thinking, three steps.

    Returns:
        The events up to the passed test run.
    """
    return [
        state("idle"),
        *user("Rename the helper in gate.py and run the unit tests."),
        *thinking(
            "The helper is `check_step` in gate.py. It has two callers.\n"
            "I rename it, update both, then run the unit tests."
        ),
        *text("I will rename `check_step` to `judge_step` and update the callers."),
        *step("t1", "Read", {"file_path": "src/gate.py"}, "read gate.py", route="allow"),
        *step(
            "t2",
            "Edit",
            {"file_path": "src/gate.py", "old_string": "check_step", "new_string": "judge_step"},
            "renamed check_step to judge_step in gate.py",
            route="edit",
            comment="the docstring of judge_step still names the old function",
            reason="A rename of one function and its caller, as the user asked.",
            diff=("src/gate.py", BEFORE, AFTER),
        ),
        *step(
            "t3",
            "Bash",
            {"command": "uv run pytest tests/unit -q"},
            "ran the unit tests: 412 passed",
            reason="Runs the project's own tests; nothing outside the project is touched.",
            result="........................................ [100%]\n412 passed in 8.11s",
            duration=8.4,
        ),
    ]


def simple_turn() -> list[Item]:
    """Build a complete turn without anything that needs the human.

    Returns:
        A prompt, thinking, text, three checked steps with a diff, an answer, the turn end.
    """
    return [
        *_opening(),
        *text(
            "The rename is done.\n\n"
            "- `judge_step` replaces `check_step` in `src/gate.py`\n"
            "- both callers are updated\n"
            "- all 412 unit tests pass\n"
        ),
        *turn_end(0.84),
    ]


def _denied_and_waiting() -> list[Item]:
    """Build a denied step, two queued messages and the force push that waits.

    Returns:
        The events after the opening of the `escalation` and `showcase` scenarios.
    """
    return [
        *step(
            "t4",
            "Bash",
            {"command": "cat ~/.aws/credentials"},
            "read a credentials file",
            decision="DENY",
            reason="the task does not need this file",
        ),
        Item(
            "chat.queue",
            {
                "messages": [
                    {
                        "id": "q1",
                        "text": "also bump the version",
                        "delivery": "inject",
                        "to": "main",
                    },
                    {
                        "id": "q2",
                        "text": "then write the changelog entry",
                        "delivery": "queued",
                        "to": "main",
                    },
                ]
            },
        ),
        *escalation(
            "e1",
            "t5",
            "Bash",
            {"command": FORCE_PUSH},
            f"The command `{FORCE_PUSH}` matches the deny rule `git push --force` (force push).",
            "force push of main to origin",
            route="escalate",
        ),
    ]


def escalation_scenario() -> list[Item]:
    """Build a turn that ends at an open escalation.

    Returns:
        The opening, a denied step, two queued messages and one step that waits.
    """
    return [*_opening(), *_denied_and_waiting()]


def two_subagents() -> list[Item]:
    """Build a turn with a background subagent and a foreground subagent.

    Returns:
        The opening, then `explore` (background, two steps) and `tests` (foreground, one step
        and a text).
    """
    explore = agent_info("a1", "explore", "Explore", True)
    tests = agent_info("a2", "tests", "general-purpose", False)
    return [
        *_opening(),
        *subagent([explore], "Find every caller of check_step outside src/."),
        *step(
            "s1",
            "Grep",
            {"pattern": "check_step", "path": "."},
            "searched the project for check_step",
            route="allow",
            agent="a1",
        ),
        *subagent([explore, tests], "Write unit tests for judge_step."),
        *step(
            "s2",
            "Read",
            {"file_path": "tests/unit/test_gate.py"},
            "read the existing tests of gate.py",
            route="allow",
            agent="a2",
        ),
        *text("The existing tests cover `gate` but not `judge_step` itself.", agent="a2"),
        *step(
            "s3",
            "Grep",
            {"pattern": "check_step", "path": "docs"},
            "searched the documentation for check_step",
            route="allow",
            agent="a1",
            result=None,
        ),
    ]


def showcase() -> list[Item]:
    """Build the session of the screenshots: everything the main screen can show at once.

    Returns:
        Two subagents, a denied step, two queued messages, an honesty mark, an open
        escalation of the main agent and an open question of the subagent `tests`.
    """
    return [
        *two_subagents(),
        Item(
            "honesty.update",
            {
                "mode": "enforce",
                "marks": [
                    {
                        "id": 1,
                        "path": "src/gate.py",
                        "symbol": "judge_step",
                        "reason": "docstring names the old function",
                    }
                ],
            },
        ),
        *_denied_and_waiting(),
        wait(37.0),
        *question("e2", "s4", RUNNER_QUESTIONS, agent="a2"),
        wait(5.0),
    ]


def question_multi() -> list[Item]:
    """Build a turn that ends at a set of three questions.

    Returns:
        A prompt, a text and the open question set `SETUP_QUESTIONS`.
    """
    return [
        state("idle"),
        *user("Add tests for the gate."),
        *text("Before I start I need three decisions."),
        *question("e1", "t1", SETUP_QUESTIONS),
    ]


def stopped() -> list[Item]:
    """Build a session that has stopped after one turn.

    Returns:
        The simple turn, then the state "stopped".
    """
    return [*simple_turn(), state("stopped")]


def long_session(steps: int = 2000) -> list[Item]:
    """Build a long session: ten steps per turn.

    Args:
        steps: The number of tool calls.

    Returns:
        `steps` passed steps in turns of ten, each turn with a prompt, a text and its end.
    """
    items: list[Item] = [state("idle")]
    for number in range(steps):
        if number % 10 == 0:
            items += user(f"Task {number // 10 + 1}: check the next ten files.")
            items += text(f"I check files {number + 1} to {number + 10}.")
        items += step(
            f"long{number}",
            "Bash",
            {"command": f"wc -l src/file_{number}.py"},
            f"counted the lines of file_{number}.py",
            result=f"{number + 17} src/file_{number}.py",
        )
        if number % 10 == 9:
            items += turn_end(round(0.01 * (number // 10 + 1), 2))
    return items


def stored_history() -> dict[str, Any]:
    """Build what `session.history` returns for a session with two finished turns.

    Returns:
        A `SessionHistoryResult` as a dict: two turns with their inputs and answers, and three
        stored steps (a read, an edit, a denied command).
    """
    return {
        "calu_session_id": "calu-7d41",
        "claude_session_id": "5f0c2a9e-claude",
        "chat_summary": "",
        "turns": [
            {
                "index": 1,
                "user_inputs": ["Find out where the step gate decides."],
                "answer": "The decision is made in `check_step` in `src/gate.py`.",
                "ended_at": "2026-11-12T09:14:00Z",
            },
            {
                "index": 2,
                "user_inputs": ["Add a docstring to it."],
                "answer": "`check_step` has a docstring now.",
                "ended_at": "2026-11-12T09:20:00Z",
            },
        ],
        "steps": [
            {
                "seq": 1,
                "turn": 1,
                "tool_use_id": "h1",
                "tool_name": "Grep",
                "tool_input": {"pattern": "def check_step"},
                "decision": "pass",
                "summary": "searched for the definition of check_step",
                "result_summary": "src/gate.py:1",
            },
            {
                "seq": 2,
                "turn": 2,
                "tool_use_id": "h2",
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/gate.py"},
                "decision": "pass",
                "summary": "added a docstring to check_step",
            },
            {
                "seq": 3,
                "turn": 2,
                "tool_use_id": "h3",
                "tool_name": "Bash",
                "tool_input": {"command": "cat ~/.netrc"},
                "decision": "deny",
                "reason": "the task does not need this file",
                "summary": "read a credentials file",
            },
        ],
    }


SCENARIOS: dict[str, Callable[[], list[Item]]] = {
    "simple_turn": simple_turn,
    "escalation": escalation_scenario,
    "showcase": showcase,
    "question_multi": question_multi,
    "two_subagents": two_subagents,
    "stopped": stopped,
    "long_session": long_session,
}
"""Scenario name -> the function that builds its events."""


def preload(session: FakeSession, items: list[Item]) -> None:
    """Record a scenario as the session's history, which a client gets replayed on attach.

    Args:
        session: The scripted session.
        items: The events.
    """
    for item in items:
        if item.method:
            session.record(item.method, item.params)


async def play(
    session: FakeSession, items: list[Item], advance: Callable[[float], None] | None = None
) -> None:
    """Send a scenario to the attached clients as live events.

    Args:
        session: The scripted session.
        items: The events.
        advance: Called with the seconds that pass before each event, to move a test clock;
            None leaves the clock alone.
    """
    for item in items:
        if advance is not None and item.after:
            advance(item.after)
        if item.method:
            await session.emit(item.method, item.params)
