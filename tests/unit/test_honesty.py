"""Code honesty outside a running session: the marks, the gate's decision, the clients.

`tests/unit/test_honesty_state.py` covers the state computation on git repositories and
`tests/unit/test_honesty_session.py` the session. This file covers the pieces that need
neither: the checks of a mark's path, the paths of a diff, the gate's decision from a given
state (`StepGate`), the fixed instruction part, and how the clients show the state and send
the requests.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu.acp.adapter import Adapter
from calu.attach import HELP, describe_honesty
from calu.checkers import CheckDecision, Checker, CheckOutcome, CodeMark, Decision, Route, Step
from calu.config import Config, HonestyConfig
from calu.defaults.instructions import compose_instructions
from calu.gate import COMMITS, EXECUTES, StepGate
from calu.honesty import (
    Baseline,
    HonestyMark,
    HonestySnapshot,
    Unreviewed,
    checked_marks,
    diff_paths,
)
from calu.protocol.messages import (
    REQUESTS,
    HonestyInfo,
    HonestyMarkInfo,
    HonestyTrustParams,
    HonestyUpdate,
)
from calu.run import format_event, honesty_lines
from calu.web.app import LIVE_METHODS
from tests.unit.helpers import make_session
from tests.unit.test_attach import console

pytestmark = pytest.mark.anyio

BASE = Baseline("a" * 40, "b" * 40, "user")


def open_mark(path: str = "src/parser.py", symbol: str = "fields", number: int = 1) -> HonestyMark:
    """Build an open mark.

    Args:
        path: The marked file.
        symbol: The marked symbol.
        number: The mark's id.

    Returns:
        The mark, opened by the edit checker.
    """
    return HonestyMark(
        id=number,
        path=path,
        symbol=symbol,
        reason="the docstring promises a sorted list",
        opened_at=datetime.now(UTC),
        opened_by="edit",
    )


def state(
    *,
    baseline: Baseline | None = BASE,
    marks: tuple[HonestyMark, ...] = (),
    unreviewed: tuple[str, ...] = (),
    review_required: bool = False,
) -> HonestySnapshot:
    """Build an honesty state.

    Args:
        baseline: The baseline; None for a session without one.
        marks: The open marks.
        unreviewed: The unreviewed files.
        review_required: Whether a review is required.

    Returns:
        The snapshot; without a baseline every file counts as unreviewed.
    """
    files = Unreviewed(everything=True) if baseline is None else Unreviewed(paths=unreviewed)
    return HonestySnapshot(baseline, marks, files, review_required)


# ------------------------------------------------------------------ marks and diffs


def test_a_mark_is_open_until_something_closes_it():
    mark = open_mark()
    assert mark.open and mark.label() == "src/parser.py:fields"
    assert open_mark(symbol="").label() == "src/parser.py"


def test_marks_with_a_path_outside_the_project_are_dropped(tmp_path: Path):
    """A mark's path is text a model wrote: only a relative path inside the project counts."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    marks = [
        CodeMark(path="a.py", reason="r"),
        CodeMark(path="/etc/passwd", reason="r"),
        CodeMark(path="../other/a.py", reason="r"),
        CodeMark(path="~/a.py", reason="r"),
        CodeMark(path="  ", reason="r"),
        CodeMark(path="missing.py", reason="r"),
    ]
    kept, rejected = checked_marks(marks, tmp_path, must_exist=True)
    assert [mark.path for mark in kept] == ["a.py"]
    assert len(rejected) == 5
    assert "missing.py: there is no such file in the project" in rejected
    # A clean mark may name a file that was deleted.
    kept, rejected = checked_marks(marks, tmp_path, must_exist=False)
    assert [mark.path for mark in kept] == ["a.py", "missing.py"]


def test_the_paths_of_a_diff_are_its_new_sides():
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "diff --git a/old.py b/new.py\nsimilarity index 90%\n"
    )
    assert diff_paths(diff) == {"src/a.py", "new.py"}
    assert diff_paths("") == set()


def test_the_state_is_clean_only_with_a_baseline_and_nothing_open():
    assert state().clean
    assert not state(baseline=None).clean
    assert not state(marks=(open_mark(),)).clean
    assert not state(unreviewed=("a.py",)).clean
    assert not state(review_required=True).clean


# ------------------------------------------------------------------ the fixed instruction part


def test_the_instruction_part_says_what_the_specification_asks_for():
    """Section 6: do not rely on listed code; how to record and clear; clean marks ignored
    while a review is required; the command checker's answer about executing project code."""
    prompt = compose_instructions("command", "x", marks=True)
    for text in (
        "Do not rely on the names or the documentation",
        "or ESCALATE",
        "`marks_dirty`",
        "`marks_clean`",
        '"Review required: yes"',
        "`executes_project_code`",
    ):
        assert text in prompt, text
    assert "executes_project_code" not in compose_instructions("command", "x")
    assert "executes_project_code" in CheckDecision.model_json_schema()["properties"]
    plain = CheckDecision(decision=Decision.PASS, reason="", step_summary="s")
    assert plain.executes_project_code is None


# ------------------------------------------------------------------ the gate's decision


def gate_of(tmp_path: Path, mode: str = "enforce") -> StepGate:
    """Build a session's gate without starting anything.

    Args:
        tmp_path: The workspace.
        mode: `honesty.mode`.

    Returns:
        The gate.
    """
    config = Config(honesty=HonestyConfig(mode=mode))  # type: ignore[arg-type]
    session, _rec, _store = make_session(tmp_path, [[]], config=config)
    return session.gate


def answers(gate: StepGate, *executes: bool | None) -> list[tuple[Checker, CheckOutcome]]:
    """Build the outcomes of command checkers that passed the step.

    Args:
        gate: The gate, whose first command checker stands for every answer.
        executes: One `executes_project_code` per checker, the route's own first.

    Returns:
        The (checker, outcome) pairs as `StepGate.run_checkers` returns them.
    """
    checker = gate._checkers()[Route.COMMAND][0]
    return [
        (
            checker,
            CheckOutcome(
                decision=CheckDecision(
                    decision=Decision.PASS,
                    reason="",
                    step_summary="s",
                    executes_project_code=value,
                ),
                duration_ms=1,
            ),
        )
        for value in executes
    ]


def test_a_command_counts_as_executing_unless_the_command_checker_says_no(tmp_path: Path):
    """Section 5, fail closed: only an explicit "no" of the command checker, with no "yes" of
    another checker of the route, lets a shell command pass an unclean state."""
    gate = gate_of(tmp_path)
    assert gate._executes(Route.COMMAND, answers(gate, False), shell=True) is False
    assert gate._executes(Route.COMMAND, answers(gate, True), shell=True) is True
    assert gate._executes(Route.COMMAND, answers(gate, None), shell=True) is True
    assert gate._executes(Route.COMMAND, answers(gate, False, True), shell=True) is True
    assert gate._executes(Route.COMMAND, answers(gate, False, None), shell=True) is False
    assert gate._executes(Route.COMMAND, [], shell=True) is True


def test_another_tool_counts_as_executing_only_if_a_checker_says_so(tmp_path: Path):
    """A tool that runs no shell command executes project code only by an explicit answer."""
    gate = gate_of(tmp_path)
    assert gate._executes(Route.COMMAND, answers(gate, None), shell=False) is False
    assert gate._executes(Route.COMMAND, [], shell=False) is False
    assert gate._executes(Route.COMMAND, answers(gate, False), shell=False) is False
    assert gate._executes(Route.COMMAND, answers(gate, True), shell=False) is True
    assert gate._executes(Route.COMMAND, answers(gate, None, True), shell=False) is True


async def needs_clean(
    tmp_path: Path, before: HonestySnapshot, after: HonestySnapshot | None, problem: str = ""
) -> tuple[CheckDecision | None, list[str]]:
    """Ask the gate about a step that needs a clean state, with a given state and review.

    Args:
        tmp_path: The workspace.
        before: The state when the step reaches the gate.
        after: The state after the review ran.
        problem: What the review answers ("" for "it ran").

    Returns:
        (the gate's decision, None for "no objection"; one entry per review that was asked
        for).
    """
    gate = gate_of(tmp_path)
    asked: list[str] = []

    async def review(_step: Step, wait_s: float | None) -> str:
        """Record that the review was asked for, with the time the gate waits for it.

        Returns:
            `problem`.
        """
        asked.append("review" if wait_s is None else f"review within {wait_s:.0f} s")
        return problem

    async def now() -> HonestySnapshot | None:
        """Return the state after the review.

        Returns:
            `after`.
        """
        return after

    gate._honesty_review = review
    gate._honesty = now
    step = Step(tool_name="Bash", tool_input={"command": "make test"}, cwd=str(tmp_path))
    decision, _state = await gate._needs_clean(step, before, EXECUTES, "runs the tests")
    return decision, asked


async def test_the_gate_waits_for_the_review_only_within_its_own_time(tmp_path: Path):
    """The review gets the time the gate has left, less a margin for reading the state."""
    gate = gate_of(tmp_path)
    waits: list[float | None] = []

    async def review(_step: Step, wait_s: float | None) -> str:
        """Record the time the gate waits.

        Returns:
            The answer of a review that is still running.
        """
        waits.append(wait_s)
        return "The honesty review is still running."

    async def now() -> HonestySnapshot | None:
        """Return the unchanged state.

        Returns:
            A state with one unreviewed file.
        """
        return state(unreviewed=("a.py",))

    gate._honesty_review = review
    gate._honesty = now
    step = Step(tool_name="Bash", tool_input={"command": "make test"}, cwd=str(tmp_path))
    budget = anyio.CancelScope(deadline=anyio.current_time() + 100)
    decision, _state = await gate._needs_clean(
        step, state(unreviewed=("a.py",)), EXECUTES, "runs the tests", budget
    )
    assert decision is not None and decision.decision is Decision.ESCALATE
    assert "The honesty review is still running." in decision.reason
    assert waits[0] is not None and 70 < waits[0] <= 80


async def test_open_marks_deny_without_a_review(tmp_path: Path):
    decision, asked = await needs_clean(tmp_path, state(marks=(open_mark(),)), None)
    assert decision is not None and decision.decision is Decision.DENY
    assert "src/parser.py:fields (the docstring promises a sorted list)" in decision.reason
    assert EXECUTES in decision.reason and asked == []


async def test_no_baseline_escalates_without_a_review(tmp_path: Path):
    decision, asked = await needs_clean(tmp_path, state(baseline=None), None)
    assert decision is not None and decision.decision is Decision.ESCALATE
    assert "`/trust [commit-or-branch]`" in decision.reason and "/honesty-review" in decision.reason
    assert "certifies nothing" in decision.reason and asked == []


async def test_unreviewed_files_run_the_review_and_follow_its_result(tmp_path: Path):
    unreviewed = state(unreviewed=("a.py",))
    decision, asked = await needs_clean(tmp_path, unreviewed, state())
    assert decision is None and asked == ["review"], "clean after the review: the step goes on"
    decision, _ = await needs_clean(tmp_path, unreviewed, state(marks=(open_mark("a.py", ""),)))
    assert decision is not None and decision.decision is Decision.DENY and "a.py" in decision.reason
    decision, _ = await needs_clean(tmp_path, unreviewed, unreviewed, "The review failed.")
    assert decision is not None and decision.decision is Decision.ESCALATE
    assert "The review failed." in decision.reason
    assert "1 unreviewed file(s) (a.py)" in decision.reason
    decision, _ = await needs_clean(tmp_path, unreviewed, unreviewed)
    assert decision is not None and decision.decision is Decision.ESCALATE, "still not clean"


async def test_a_required_review_runs_the_review_although_marks_are_open(tmp_path: Path):
    """Section 3: with more marks than allowed, only the review clears them, so the gate asks
    for it instead of denying a step that no single edit could unblock."""
    many = tuple(open_mark(f"m{n}.py", "", n) for n in range(4))
    decision, asked = await needs_clean(tmp_path, state(marks=many, review_required=True), state())
    assert decision is None and asked == ["review"]
    decision, _ = await needs_clean(
        tmp_path,
        state(marks=many, review_required=True),
        state(marks=many, review_required=True),
    )
    assert decision is not None and decision.decision is Decision.DENY
    assert "honesty.max_open_marks" in decision.reason


async def test_a_failure_to_compute_the_state_escalates_the_step(tmp_path: Path):
    """Fail closed: under "enforce", a state that cannot be computed is no approval."""
    gate = gate_of(tmp_path)

    async def broken() -> HonestySnapshot | None:
        """Stand in for a state that cannot be computed.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("git is gone")

    gate._honesty = broken
    step = Step(tool_name="Bash", tool_input={"command": "ls"}, cwd=str(tmp_path))
    decided = await gate.decide(step, None)
    assert decided.decision.decision is Decision.ESCALATE and decided.source == "internal"
    assert "git is gone" in decided.decision.reason


def test_the_reasons_name_what_the_step_is():
    assert "executes code" in EXECUTES and "commit" in COMMITS


# ------------------------------------------------------------------ the protocol and the clients


INFO = HonestyInfo(
    mode="enforce",
    marks=[HonestyMarkInfo(id=3, path="src/parser.py", symbol="fields", reason="unsorted")],
    baseline="a" * 40,
    baseline_source="user",
    unreviewed_count=22,
    unreviewed=[f"f{n}.py" for n in range(20)],
    review_required=True,
)


def test_the_protocol_has_the_three_honesty_requests():
    assert {"honesty.clear", "honesty.trust", "honesty.review"} <= set(REQUESTS)
    assert HonestyTrustParams().rev == "HEAD" and HonestyTrustParams().path is None
    assert {"honesty.trust", "honesty.review", "honesty.clear"} <= LIVE_METHODS


def test_the_update_carries_the_whole_state():
    update = HonestyUpdate(**INFO.model_dump())
    assert set(update.model_dump()) >= {
        "mode",
        "baseline",
        "baseline_source",
        "marks",
        "unreviewed_all",
        "unreviewed_count",
        "unreviewed",
        "review_required",
        "review_running",
        "clean",
    }


def test_the_terminal_prints_a_state_change():
    lines = honesty_lines(INFO)
    assert lines[0] == (
        "*** code honesty (enforce): not clean: baseline aaaaaaaaaaaa (user), 1 open mark(s), "
        "22 unreviewed file(s), review required"
    )
    assert lines[1] == "    mark 3: src/parser.py:fields: unsorted"
    assert lines[2].startswith("    unreviewed: f0.py, ") and lines[2].endswith(", and 2 more")
    assert format_event(HonestyUpdate(**INFO.model_dump())) == "\n".join(lines)
    assert honesty_lines(HonestyInfo()) == ["*** code honesty: off"]
    nothing = honesty_lines(HonestyInfo(mode="track", unreviewed_all=True))
    assert "no baseline (certify a commit with /trust)" in nothing[0]
    assert "every file unreviewed" in nothing[0]
    assert describe_honesty(INFO.model_dump()) == "\n".join(lines)


async def test_attach_sends_the_honesty_commands(capsys: pytest.CaptureFixture[str]):
    """Section 7 and 8: `/trust`, `/trust-file`, `/honesty-review`, `/honesty`, `/clear-mark`."""
    for command in ("/trust", "/trust-file", "/honesty-review", "/honesty", "/clear-mark"):
        assert command in HELP
    con, client = console(
        {
            "honesty.trust": {"commit_id": "c" * 40, "recorded": True},
            "honesty.review": {"ran": False, "problem": "An honesty review is needed."},
            "state.get": {"honesty": INFO.model_dump()},
        }
    )
    await con.line("/trust")
    await con.line("/trust feature/x")
    await con.line("/honesty-review")
    await con.line("/honesty")
    await con.line("/clear-mark 3")
    await con.line("/trust-file")
    assert client.sent == [
        ("honesty.trust", {"rev": "HEAD"}),
        ("honesty.trust", {"rev": "feature/x"}),
        ("honesty.review", {}),
        ("state.get", {}),
        ("honesty.clear", {"mark_id": 3}),
    ]
    out = capsys.readouterr().out
    assert "[honesty] commit cccccccccccc certified" in out
    assert "[honesty] no review ran: An honesty review is needed." in out
    assert "*** code honesty (enforce): not clean" in out
    assert "/trust-file needs a path" in out
    con, client = console(
        {"honesty.trust": {"path": "build/gen.py", "content_sha256": "d" * 64, "recorded": False}}
    )
    await con.line("/trust-file build/gen.py")
    assert client.sent == [("honesty.trust", {"path": "build/gen.py"})]
    assert "the content of build/gen.py (dddddddddddd) was already certified" in (
        capsys.readouterr().out
    )


async def test_an_acp_editor_is_told_the_state():
    """The ACP adapter renders the state as a message chunk, and says nothing while off."""
    sent: list[Any] = []
    adapter = Adapter.__new__(Adapter)

    async def update(chunk: Any) -> None:
        """Collect what the adapter would send to the editor.

        Args:
            chunk: The session update.
        """
        sent.append(chunk)

    adapter._update = update  # type: ignore[method-assign]
    await adapter._on_honesty(HonestyInfo().model_dump())
    assert sent == []
    await adapter._on_honesty(INFO.model_dump())
    await adapter._on_honesty(HonestyInfo(mode="track", clean=True, baseline="a" * 40).model_dump())
    first, second = (str(chunk) for chunk in sent)
    assert "recorded as dishonest: src/parser.py:fields" in first
    assert "22 unreviewed file(s)" in first and "an honesty review is required" in first
    assert "Commands that execute project code and commits wait" in first
    assert "code honesty: clean" in second
