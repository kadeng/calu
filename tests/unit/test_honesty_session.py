"""Code honesty in a running session: the state, the gate and the certification.

Each test runs a session with the scripted fake runner on a real temporary git repository,
with fake checker models and the in-memory store. `tests/unit/test_honesty_state.py` covers the
state computation itself; this file covers what the session and the gate do with it
(specification sections 3, 5, 6, 7 and 8).
"""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic_ai.models import Model

from calu.checkers import CodeMark, Decision, Route
from calu.config import ChangesConfig, Config, HonestyConfig
from calu.honesty import FileReview, Finding, HonestyState
from calu.protocol.client import RequestFailed
from calu.protocol.messages import (
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    HonestyUpdate,
    Log,
    ToolResult,
    TurnResult,
)
from calu.session import REVIEW_NEEDED, Session, SessionError
from calu.store import MemorySessionStore
from calu.testing import fake_models, marking_checker
from tests.unit.helpers import Recorder, default_settings, make_session
from tests.unit.test_protocol_server import Events, client, served

pytestmark = pytest.mark.anyio

PARSER = '''"""Parsing."""


def fields(line):
    """Return the fields of a line, sorted."""
    return sorted(line.split(","))
'''

CHANGED = PARSER.replace('sorted(line.split(","))', 'line.split(",")')

COMMIT = ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "second"]
"""The program a scripted `git commit` really runs."""

EXECUTING = ("make test",)
"""Texts of a command for which the fake command checker says that it executes project code."""


def git(root: Path, *arguments: str) -> str:
    """Run git in a test repository.

    Args:
        root: The repository.
        arguments: The git arguments.

    Returns:
        git's standard output, stripped.
    """
    done = subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit: `src/parser.py`, `README.md`, and `build/` ignored.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The repository's directory.
    """
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "src" / "parser.py").write_text(PARSER, encoding="utf-8")
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


async def session_on(
    root: Path,
    turns: Sequence[Sequence[dict[str, Any]]],
    mode: str = "enforce",
    *,
    trusted: bool = True,
    models: dict[str, str | Model] | None = None,
    **honesty: Any,
) -> tuple[Session, Recorder, MemorySessionStore]:
    """Build a session on a test repository whose HEAD a person certified.

    Args:
        root: The repository.
        turns: The fake runner's script.
        mode: `honesty.mode`.
        trusted: Whether HEAD is recorded as trusted (source "user") before the session.
        models: Checker models that replace the defaults: the command checker says that a
            command with one of `EXECUTING` executes project code, the others pass.
        honesty: Further `honesty.*` settings.

    Returns:
        The session, the event recorder and the store.
    """
    config = Config(honesty=HonestyConfig(mode=mode, **honesty))  # type: ignore[arg-type]
    session, rec, store = make_session(
        root,
        turns,
        settings=default_settings(root, config),
        checker_models={
            **fake_models(),
            "command": marking_checker(executes=EXECUTING),
            **(models or {}),
        },
    )
    if trusted:
        head = git(root, "rev-parse", "HEAD")
        await store.certify_commit(head, git(root, "rev-parse", "HEAD^{tree}"), "user", "tester")
    return session, rec, store


async def run_turns(session: Session, rec: Recorder, count: int = 1) -> None:
    """Start the session, run `count` scripted turns and stop it.

    Args:
        session: The session.
        rec: Its event recorder.
        count: How many turns to run.
    """
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        for number in range(1, count + 1):
            await session.send_chat(ChatSendParams(text="go"))
            await rec.wait_for(TurnResult, number)
        await session.stop()


def bash(command: str, **more: Any) -> dict[str, Any]:
    """Build a scripted Bash call.

    Args:
        command: The command line.
        more: Further keys of the action ("writes", "run", "result").

    Returns:
        The action for the fake runner's script.
    """
    return {"tool": "Bash", "input": {"command": command}, "result": "ok", **more}


def write(path: str, content: str, **more: Any) -> dict[str, Any]:
    """Build a scripted Write call that really writes the file.

    Args:
        path: The file, relative to the workspace.
        content: The new content.
        more: Further keys of the action; "fails" makes the call fail without writing.

    Returns:
        The action for the fake runner's script.
    """
    action: dict[str, Any] = {
        "tool": "Write",
        "input": {"file_path": path, "content": content},
        "result": "ok",
        **more,
    }
    if not more.get("fails"):
        action["writes"] = {path: content}
    return action


def checks(rec: Recorder) -> list[CheckResult]:
    """Return the decisions of the recorded steps, in order.

    Args:
        rec: The event recorder.

    Returns:
        The `check.result` events.
    """
    return rec.of(CheckResult)


def logs(rec: Recorder) -> str:
    """Return the text of every recorded `log` event.

    Args:
        rec: The event recorder.

    Returns:
        The messages, one per line.
    """
    return "\n".join(event.message for event in rec.of(Log))


def mark(session: Session, path: str = "src/parser.py", symbol: str = "fields") -> None:
    """Open one mark in the session's honesty state, as a checker's answer would.

    Args:
        session: The session.
        path: The marked file.
        symbol: The marked symbol.
    """
    session.honesty.record(
        [CodeMark(path=path, symbol=symbol, reason="the docstring promises a sorted list")],
        [],
        "edit",
    )


# ------------------------------------------------------------------ the state in the session


async def test_the_session_finds_its_baseline_at_the_start_and_reports_the_state(repo: Path):
    """Section 2 and 8: the baseline is looked up at the start; `honesty.update` has it all."""
    session, rec, _store = await session_on(repo, [[]])
    await run_turns(session, rec)
    update = rec.of(HonestyUpdate)[0]
    assert update.mode == "enforce"
    assert update.baseline == git(repo, "rev-parse", "HEAD") and update.baseline_source == "user"
    assert update.clean and not update.marks and update.unreviewed_count == 0
    assert not update.review_required and not update.review_running
    assert session.snapshot().honesty.baseline == update.baseline


async def test_without_a_trusted_commit_there_is_no_baseline(repo: Path):
    """Section 2: no trusted ancestor means no baseline and every file unreviewed."""
    session, rec, _store = await session_on(repo, [[]], trusted=False)
    await run_turns(session, rec)
    update = rec.of(HonestyUpdate)[0]
    assert update.baseline is None and update.unreviewed_all and not update.clean


async def test_with_the_mode_off_nothing_of_this_runs(repo: Path):
    """Section 5: `off` keeps no state, sends no update and adds no note."""
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", CHANGED), bash("make test")]],
        mode="off",
        models={"edit": marking_checker(dirty=[("src/parser.py", "fields", "unsorted")])},
    )
    seen = notes_of(session, Route.COMMAND)
    await run_turns(session, rec)
    assert rec.of(HonestyUpdate) == []
    assert session.honesty.marks == [] and session.honesty.verified == {}
    assert seen == []
    assert all(check.decision is Decision.PASS for check in checks(rec))


async def test_marks_are_applied_only_after_a_successful_tool_result(repo: Path):
    """Section 2: a failed edit changes nothing; the same edit, succeeding, opens the mark."""
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", CHANGED, fails=True)], [write("src/parser.py", CHANGED)]],
        models={"edit": marking_checker(dirty=[("src/parser.py", "fields", "unsorted")])},
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        assert rec.of(ToolResult)[0].is_error
        assert session.honesty.marks == [], "the edit failed, so its marks are dropped"
        assert session.honesty.verified == {}
        await session.send_chat(ChatSendParams(text="again"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    (open_mark,) = session.honesty.open_marks
    assert open_mark.label() == "src/parser.py:fields" and open_mark.opened_by == "edit"
    assert rec.of(HonestyUpdate)[-1].marks[0].reason == "unsorted"


async def test_a_denied_edit_changes_nothing(repo: Path):
    """Section 2: the marks of a checker's answer to a denied step are not recorded."""
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", CHANGED)]],
        models={
            "edit": marking_checker(
                dirty=[("src/parser.py", "fields", "unsorted")], decision="DENY"
            )
        },
    )
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.DENY
    assert session.honesty.marks == [] and session.honesty.verified == {}


async def test_a_checked_edit_verifies_the_file_and_stores_the_verification(repo: Path):
    """Sections 2 and 13: an approved edit without a mark verifies the content, source check."""
    session, rec, store = await session_on(repo, [[write("src/parser.py", CHANGED)]])
    await run_turns(session, rec)
    state = await session.honesty_snapshot()
    assert state.clean, state
    held = session.honesty.verified["src/parser.py"]
    assert held.source == "check"
    assert store.trusted_files[held.sha256].method == "check"
    assert store.trusted_files[held.sha256].certified_by == "calu session"


async def test_an_edit_that_leaves_a_mark_is_not_stored_as_verified(repo: Path):
    """Section 13: a content with an open mark is not recorded in `trusted_files`."""
    session, rec, store = await session_on(
        repo,
        [[write("src/parser.py", CHANGED)]],
        models={"edit": marking_checker(dirty=[("src/parser.py", "fields", "unsorted")])},
    )
    await run_turns(session, rec)
    assert store.trusted_files == {}
    assert not (await session.honesty_snapshot()).clean


async def test_a_file_a_command_wrote_is_unreviewed(repo: Path):
    """Section 2: a change made by a shell command is not verified by the command's check."""
    session, rec, _store = await session_on(
        repo, [[bash("sed -i s/sorted// src/parser.py", writes={"src/parser.py": CHANGED})]]
    )
    await run_turns(session, rec)
    update = rec.of(HonestyUpdate)[-1]
    assert update.unreviewed == ["src/parser.py"] and update.unreviewed_count == 1
    assert not update.clean


async def test_a_closing_mark_needs_an_edit_of_that_file(repo: Path):
    """Section 3: `marks_clean` from the edit of the file closes the mark."""
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", PARSER + "\n")]],
        models={"edit": marking_checker(clean=[("src/parser.py", "fields()", "agrees again")])},
    )
    mark(session)
    await run_turns(session, rec)
    assert session.honesty.open_marks == []
    assert "1 cleared" in logs(rec)


async def test_more_marks_than_allowed_require_a_review_and_clean_marks_are_ignored(repo: Path):
    """Section 3: the fourth mark sets "review required"; a clean mark is then ignored."""
    for name in ("a", "b", "c"):
        (repo / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    session, rec, _store = await session_on(
        repo,
        [[write("src/parser.py", PARSER + "\n")]],
        models={"edit": marking_checker(clean=[("src/parser.py", "fields", "agrees again")])},
    )
    mark(session)
    for name in ("a", "b", "c"):
        mark(session, f"{name}.py", "")
    assert session.honesty.review_required
    await run_turns(session, rec)
    assert len(session.honesty.open_marks) == 4
    assert "a review is required, so a clean mark is ignored" in logs(rec)
    assert rec.of(HonestyUpdate)[-1].review_required


async def test_a_person_clears_a_mark_and_every_client_hears_it(repo: Path):
    """Section 3: `honesty.clear` closes one mark; an unknown id is an error."""
    session, rec, _store = await session_on(repo, [[]])
    mark(session)
    (open_mark,) = session.honesty.open_marks
    closed = await session.clear_mark(open_mark.id, client="tester")
    assert closed.closed_by == "tester (by hand)"
    assert rec.of(HonestyUpdate)[-1].marks == []
    with pytest.raises(SessionError, match="no open honesty mark"):
        await session.clear_mark(open_mark.id)


# ------------------------------------------------------------------ what the checkers are told


def notes_of(session: Session, route: Route) -> list[str]:
    """Record the notes that the first checker of a route is given from now on.

    Args:
        session: The session.
        route: The route.

    Returns:
        A list that receives the notes of every step the checker judges.
    """
    seen: list[str] = []
    checker = session.checkers[route][0]
    original = checker.build_prompt

    def build_prompt(step: Any, context: Any, extra: str = "") -> Any:
        """Record the step's notes, then build the prompt as usual.

        Args:
            step: The step.
            context: The check context.
            extra: A further block.

        Returns:
            What the checker's own `build_prompt` returns.
        """
        seen.extend(step.notes)
        return original(step, context, extra)

    checker.build_prompt = build_prompt  # type: ignore[method-assign]
    return seen


async def test_every_checker_gets_the_computed_state_with_its_own_marks_first(repo: Path):
    """Section 6: the note names the baseline, the marks, the unreviewed files, the review."""
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    session, rec, _store = await session_on(
        repo, [[write("src/parser.py", CHANGED), bash("ls src")]], mode="track"
    )
    mark(session)
    mark(session, "other.py", "")
    edit_notes = notes_of(session, Route.EDIT)
    command_notes = notes_of(session, Route.COMMAND)
    await run_turns(session, rec)
    note = "\n".join(edit_notes)
    assert "Code honesty state, computed by calu." in note
    assert f"Baseline: trusted commit {git(repo, 'rev-parse', 'HEAD')[:12]}" in note
    assert "Open marks on this step's files: src/parser.py:fields" in note
    assert "Open marks on other files: other.py" in note
    assert "Unreviewed files (1): other.py." in note
    assert "Review required: no." in note
    # `ls` is on the allow lists, and `track` leaves them in force: no checker ran for it.
    assert command_notes == []
    assert "marks_dirty" in session.checkers[Route.COMMAND][0].instructions
    assert "executes_project_code" in session.checkers[Route.COMMAND][0].instructions


# ------------------------------------------------------------------ enforcement: running code


async def test_with_open_marks_reading_passes_and_executing_is_denied(repo: Path):
    """Section 5: a command that only reads is not stopped; one that executes code is denied."""
    session, rec, _store = await session_on(
        repo,
        [[bash("grep -n fields src/parser.py"), bash("make test"), write("src/x.py", "x = 1\n")]],
    )
    mark(session)
    await run_turns(session, rec)
    read, run, edit = checks(rec)
    assert read.decision is Decision.PASS
    assert run.decision is Decision.DENY
    assert "src/parser.py:fields (the docstring promises a sorted list)" in run.reason
    assert "this command executes code of the project" in run.reason
    assert edit.decision is Decision.PASS, "editing is never blocked"
    denied = [r for r in rec.of(ToolResult) if r.is_error]
    assert len(denied) == 1 and "the docstring promises a sorted list" in denied[0].content


async def test_an_answer_without_the_field_counts_as_executing(repo: Path):
    """Section 5, fail closed: the command checker did not say, so the command needs a clean
    state."""
    session, rec, _store = await session_on(
        repo, [[bash("make lint")]], models={"command": marking_checker()}
    )
    mark(session)
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.DENY


async def test_the_allow_lists_do_not_skip_the_checker_while_the_state_is_not_clean(repo: Path):
    """Section 5: an allow-listed command is judged by the command checker while not clean."""
    session, rec, _store = await session_on(
        repo,
        [[bash("ls src")], [bash("ls src")]],
        models={"command": marking_checker(executes=["ls"])},
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        mark(session)
        await session.send_chat(ChatSendParams(text="again"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    clean, unclean = checks(rec)
    assert clean.decision is Decision.PASS and "allowlists" in clean.reason
    assert unclean.decision is Decision.DENY, "the checker said that it executes project code"
    assert "recorded as dishonest" in unclean.reason.lower() or "dishonest" in unclean.reason


async def test_without_a_baseline_an_executing_command_waits_for_a_person(repo: Path):
    """Section 5: no baseline escalates with the instruction to certify a commit."""
    session, rec, store = await session_on(
        repo, [[bash("grep -n fields src/parser.py"), bash("make test")]], trusted=False
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "/trust" in request.reason and "no trusted commit" in request.reason
        await session.decide_escalation(request.escalation_id, "allow", "this once")
        await rec.wait_for(TurnResult)
        await session.stop()
    read, run = checks(rec)
    assert read.decision is Decision.PASS and run.decision is Decision.ESCALATE
    assert not rec.of(ToolResult)[-1].is_error, "the person's allow lets the step run once"
    assert store.trusted_commits == {}, "and certifies nothing"


async def test_unreviewed_files_call_for_the_review_and_without_one_a_person_decides(repo: Path):
    """Section 5: unreviewed files and an executing command: the review is asked for. The
    review is not built yet, so the step is escalated with that reason."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, _store = await session_on(repo, [[bash("make test")]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "An honesty review is needed" in request.reason
        assert REVIEW_NEEDED in request.reason
        assert "1 unreviewed file(s) (src/parser.py)" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "not now")
        await rec.wait_for(TurnResult)
        await session.stop()
    assert rec.of(ToolResult)[-1].is_error


async def reviewing(session: Session, findings: tuple[Finding, ...] = ()) -> list[int]:
    """Replace the session's honesty review by one that judges `src/parser.py`.

    Args:
        session: The session.
        findings: What the review finds in the file; empty for "honest".

    Returns:
        A list that gets one entry per review that ran.
    """
    ran: list[int] = []

    async def review() -> str:
        """Verify the file at its present content, or record the findings.

        Returns:
            "": the review ran.
        """
        state: HonestyState = session.honesty
        sha256 = state.content_sha256("src/parser.py")
        assert sha256 is not None
        state.apply_review({"src/parser.py": FileReview(sha256, findings)})
        ran.append(1)
        return ""

    session.honesty_review = review  # type: ignore[method-assign]
    return ran


async def test_a_review_that_verifies_everything_lets_the_command_run(repo: Path):
    """Section 5: the step follows the review's result: clean, so it goes on."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, _store = await session_on(repo, [[bash("make test")]])
    ran = await reviewing(session)
    await run_turns(session, rec)
    assert ran == [1]
    assert checks(rec)[0].decision is Decision.PASS
    assert rec.of(HonestyUpdate)[-1].clean


async def test_a_review_with_findings_denies_the_command_with_them(repo: Path):
    """Section 5: findings of the review become marks, and the step is denied with them."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, _store = await session_on(repo, [[bash("make test")]])
    await reviewing(session, (Finding("fields", "returns the fields unsorted"),))
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.DENY
    assert "src/parser.py:fields (returns the fields unsorted)" in checks(rec)[0].reason


async def test_a_failed_review_escalates(repo: Path):
    """Section 4: a review that fails verifies nothing, and the step is escalated."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, _store = await session_on(repo, [[bash("make test")]])

    async def failed() -> str:
        """Stand in for a review whose model call failed.

        Returns:
            The problem.
        """
        return "The honesty review failed (the model did not answer)."

    session.honesty_review = failed  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "the model did not answer" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()
    assert not (await session.honesty_snapshot()).clean


async def test_track_mode_blocks_nothing(repo: Path):
    """Section 5: `track` keeps and shows the state; no step is denied or escalated for it."""
    (repo / "README.md").write_text("# changed outside\n", encoding="utf-8")
    session, rec, _store = await session_on(
        repo,
        [[bash("make test"), bash("git commit -m second", run=["git", "add", "-A"])]],
        mode="track",
    )
    mark(session)
    await run_turns(session, rec)
    assert [check.decision for check in checks(rec)] == [Decision.PASS, Decision.PASS]
    assert rec.of(EscalationRequest) == []
    assert rec.of(HonestyUpdate)[-1].mode == "track" and not rec.of(HonestyUpdate)[-1].clean


# ------------------------------------------------------------------ commits


async def test_a_commit_from_a_clean_state_is_certified(repo: Path):
    """Section 5 and 13: the new HEAD is recorded as trusted with the weakest source of its
    changes, and becomes the baseline."""
    session, rec, store = await session_on(
        repo,
        [
            [
                write("src/parser.py", CHANGED),
                bash("git add src/parser.py", run=["git", "add", "src/parser.py"]),
                bash("git commit -m second", run=COMMIT),
            ]
        ],
    )
    first = git(repo, "rev-parse", "HEAD")
    await run_turns(session, rec)
    head = git(repo, "rev-parse", "HEAD")
    assert head != first
    row = store.trusted_commits[head]
    assert row.method == "check" and row.certified_by == "calu session"
    assert row.tree_id == git(repo, "rev-parse", "HEAD^{tree}")
    update = rec.of(HonestyUpdate)[-1]
    assert update.baseline == head and update.baseline_source == "check" and update.clean
    assert f"commit {head[:12]} certified as trusted" in logs(rec)


async def test_a_commit_is_certified_in_track_mode_too(repo: Path):
    """Section 5: certification happens in `track` and `enforce`."""
    session, rec, store = await session_on(
        repo,
        [
            [
                write("src/parser.py", CHANGED),
                bash("git add src/parser.py", run=["git", "add", "src/parser.py"]),
                bash("git commit -m second", run=COMMIT),
            ]
        ],
        mode="track",
    )
    await run_turns(session, rec)
    assert git(repo, "rev-parse", "HEAD") in store.trusted_commits


async def test_a_commit_with_open_marks_is_denied(repo: Path):
    """Section 5: committing needs a clean state; open marks deny the commit, without a model."""
    session, rec, store = await session_on(repo, [[bash("git commit -m second", run=COMMIT)]])
    mark(session)
    await run_turns(session, rec)
    (commit,) = checks(rec)
    assert commit.decision is Decision.DENY
    assert "this command is a commit" in commit.reason and "src/parser.py:fields" in commit.reason
    assert len(store.trusted_commits) == 1


async def test_a_commit_with_unreviewed_files_follows_the_review(repo: Path):
    """Section 5: a commit with unreviewed files asks for the review; a person's allow lets the
    commit run once, and that commit is not certified."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    git(repo, "add", "-A")
    session, rec, store = await session_on(repo, [[bash("git commit -m second", run=COMMIT)]])
    first = git(repo, "rev-parse", "HEAD")
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "this command is a commit" in request.reason
        assert REVIEW_NEEDED in request.reason
        await session.decide_escalation(request.escalation_id, "allow", "this once")
        await rec.wait_for(TurnResult)
        await session.stop()
    head = git(repo, "rev-parse", "HEAD")
    assert head != first, "the commit ran"
    assert list(store.trusted_commits) == [first], "and was not certified"
    assert rec.of(HonestyUpdate)[-1].baseline == first


async def test_a_commit_without_a_baseline_waits_for_a_person(repo: Path):
    """Section 5: no baseline escalates a commit."""
    session, rec, _store = await session_on(
        repo, [[bash("git commit -m second", run=COMMIT)]], trusted=False
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "no trusted commit" in request.reason and "/trust" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_a_commit_in_a_longer_command_line_is_refused_while_not_clean(repo: Path):
    """Section 5: the commit gate judges a commit that is the whole command line."""
    session, rec, _store = await session_on(repo, [[bash("git add -A && git commit -m second")]])
    mark(session)
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.DENY
    assert "`git commit` is the whole command line" in checks(rec)[0].reason


# ------------------------------------------------------------------ certifying by hand


async def test_trust_certifies_the_commit_a_branch_points_at_now(repo: Path):
    """Section 7: `honesty.trust` records the commit with the source user and the client's
    name, and the session then has that baseline."""
    session, rec, store = await session_on(repo, [[]], trusted=False)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        assert rec.of(HonestyUpdate)[-1].baseline is None
        done = await session.trust("main", note="read it", client="tester")
        head = git(repo, "rev-parse", "HEAD")
        assert done.commit_id == head and done.recorded
        row = store.trusted_commits[head]
        assert (row.method, row.certified_by, row.note) == ("user", "tester", "read it")
        update = rec.of(HonestyUpdate)[-1]
        assert update.baseline == head and update.baseline_source == "user" and update.clean
        again = await session.trust(client="tester")
        assert again.commit_id == head and not again.recorded
        with pytest.raises(SessionError, match="names no commit"):
            await session.trust("no-such-branch")
        await session.stop()


async def test_trust_certifies_the_present_content_of_a_file(repo: Path):
    """Section 12: a person certifies one file by the hash of its content, also an ignored one."""
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("x = 1\n", encoding="utf-8")
    session, rec, store = await session_on(repo, [[]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        session.honesty.watch("build/gen.py")
        assert (await session.honesty_snapshot()).unreviewed.paths == ("build/gen.py",)
        done = await session.trust(path="build/gen.py", client="tester")
        assert done.path == "build/gen.py" and done.content_sha256 in store.trusted_files
        assert store.trusted_files[done.content_sha256 or ""].method == "user"
        assert rec.of(HonestyUpdate)[-1].clean
        (repo / "build" / "gen.py").write_text("x = 2\n", encoding="utf-8")
        assert not (await session.honesty_snapshot()).clean, "another content is unreviewed"
        with pytest.raises(SessionError, match="not a file calu can read"):
            await session.trust(path="build/missing.py")
        await session.stop()


async def test_a_certification_made_elsewhere_reaches_the_session(repo: Path):
    """Section 2: after `trust_changed` the session looks up its baseline again."""
    session, _rec, store = await session_on(repo, [[]], trusted=False)
    assert (await session.honesty_snapshot()).baseline is None
    head = git(repo, "rev-parse", "HEAD")
    await store.certify_commit(head, git(repo, "rev-parse", "HEAD^{tree}"), "user", "operator")
    assert (await session.honesty_snapshot()).baseline is None, "nothing announced the change"
    session.trust_changed()
    found = (await session.honesty_snapshot()).baseline
    assert found is not None and found.commit_id == head


async def test_a_commit_made_outside_moves_the_baseline_search(repo: Path):
    """Section 2: the baseline is the nearest trusted ancestor of HEAD as HEAD is now."""
    session, _rec, _store = await session_on(repo, [[]])
    first = git(repo, "rev-parse", "HEAD")
    assert (await session.honesty_snapshot()).clean
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    git(repo, "commit", "-q", "-a", "-m", "outside")
    state = await session.honesty_snapshot()
    assert state.baseline is not None and state.baseline.commit_id == first
    assert state.unreviewed.paths == ("README.md",)


async def test_raising_sufficient_trust_makes_a_checked_commit_no_baseline(repo: Path):
    """Section 13: a baseline whose source is weaker than `honesty.sufficient_trust` does not
    count."""
    session, _rec, store = await session_on(repo, [[]], trusted=False)
    head = git(repo, "rev-parse", "HEAD")
    await store.certify_commit(head, git(repo, "rev-parse", "HEAD^{tree}"), "check", "calu session")
    assert (await session.honesty_snapshot()).clean
    session.config = session.config.model_copy(
        update={"honesty": session.config.honesty.model_copy(update={"sufficient_trust": "review"})}
    )
    assert (await session.honesty_snapshot()).baseline is None


async def test_asking_for_a_review_answers_that_none_can_run_yet(repo: Path):
    """Section 8: `honesty.review` runs the review; until it is built it says why none ran."""
    session, _rec, _store = await session_on(repo, [[]])
    done = await session.request_review(client="tester")
    assert not done.ran and done.problem == REVIEW_NEEDED
    off, _rec2, _store2 = await session_on(repo, [[]], mode="off")
    with pytest.raises(SessionError, match=r"honesty\.mode is off"):
        await off.request_review()


async def test_a_client_certifies_and_asks_for_a_review_over_the_protocol(repo: Path):
    """Sections 7 and 8: `honesty.trust` and `honesty.review` as a client sends them; the
    certification carries the client's name, and the client gets `honesty.update`."""
    config = Config(honesty=HonestyConfig(mode="enforce"))
    async with served(repo, config=config) as run:
        events = Events()
        async with client(run, events, name="zed") as zed:
            await zed.request("session.start", {})
            assert (await zed.request("state.get"))["honesty"]["baseline"] is None
            done = await zed.request("honesty.trust", {"rev": "main", "note": "read it"})
            head = git(repo, "rev-parse", "HEAD")
            assert done == {
                "commit_id": head,
                "content_sha256": None,
                "path": None,
                "recorded": True,
            }
            row = run.store.trusted_commits[head]
            assert (row.method, row.certified_by) == ("user", "zed")
            snapshot = await zed.request("state.get")
            assert snapshot["honesty"]["baseline"] == head and snapshot["honesty"]["clean"]
            assert snapshot["honesty_mode"] == "enforce"
            review = await zed.request("honesty.review")
            assert review == {"ran": False, "problem": REVIEW_NEEDED}
            with pytest.raises(RequestFailed, match="names no commit"):
                await zed.request("honesty.trust", {"rev": "no-such-branch"})
            with anyio.fail_after(5):
                while not any(update["baseline"] == head for update in events.of("honesty.update")):
                    await anyio.sleep(0.01)


async def test_the_review_of_a_write_set_gets_the_state_and_its_marks_are_recorded(repo: Path):
    """Sections 3, 6 and 12: the changeset checkers that review what a command wrote get the
    note; their marks are recorded at once, because the command has run; and the written file
    is watched even where git ignores it."""
    config = Config(
        honesty=HonestyConfig(mode="track"),
        changes=ChangesConfig(snapshot=True, review=True, always_capture=["build/gen.py"]),
    )
    session, rec, _store = make_session(
        repo,
        [
            [
                bash(
                    "python tools/gen.py",
                    writes={"src/parser.py": CHANGED, "build/gen.py": "x = 1\n"},
                )
            ]
        ],
        settings=default_settings(repo, config),
        checker_models={
            **fake_models(),
            "changeset": marking_checker(dirty=[("src/parser.py", "fields", "unsorted")]),
        },
    )
    head = git(repo, "rev-parse", "HEAD")
    await session.store.certify_commit(head, git(repo, "rev-parse", "HEAD^{tree}"), "user", "t")
    seen = notes_of(session, Route.CHANGESET)
    await run_turns(session, rec)
    assert any("Code honesty state, computed by calu." in note for note in seen)
    assert [m.label() for m in session.honesty.open_marks] == ["src/parser.py:fields"]
    assert session.honesty.open_marks[0].opened_by == "changeset"
    assert "build/gen.py" in session.honesty.watched
    update = rec.of(HonestyUpdate)[-1]
    assert update.unreviewed == ["build/gen.py", "src/parser.py"]


# ------------------------------------------------------------------ corrections after review


def tool(name: str, **tool_input: Any) -> dict[str, Any]:
    """Build a scripted call of a tool that is neither Bash nor an edit.

    Args:
        name: The tool's name.
        tool_input: The tool's input.

    Returns:
        The action for the fake runner's script.
    """
    return {"tool": name, "input": tool_input, "result": "ok"}


async def test_a_mark_withdraws_the_stored_trust_of_the_marked_content(repo: Path):
    """Section 13: a content stored as verified and later found dishonest by a command's
    checker loses its stored row, so a fresh state (a restart) does not count it as verified."""
    session, rec, store = await session_on(
        repo,
        [[write("src/parser.py", CHANGED)], [bash("make lint")]],
        models={
            "command": marking_checker(
                dirty=[("src/parser.py", "fields", "unsorted")], executes=EXECUTING
            )
        },
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult, 1)
        assert [row.method for row in store.trusted_files.values()] == ["check"]
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    assert store.trusted_files == {}
    assert [m.label() for m in session.honesty.open_marks] == ["src/parser.py:fields"]
    fresh = HonestyState(repo)
    fresh.baseline = session.honesty.baseline
    fresh.trusted_hashes = await store.trusted_file_sources()
    assert fresh.unreviewed().paths == ("src/parser.py",)


async def test_a_mark_does_not_withdraw_what_a_person_certified(repo: Path):
    """Section 13: the session never removes a record of the source "user"."""
    session, rec, store = await session_on(
        repo,
        [[bash("make lint")]],
        models={"command": marking_checker(dirty=[("src/parser.py", "", "x")], executes=())},
    )
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    await session.trust(path="src/parser.py", client="tester")
    await run_turns(session, rec)
    assert [row.method for row in store.trusted_files.values()] == ["user"]
    assert len(session.honesty.open_marks) == 1


async def test_a_staged_content_nobody_verified_stops_a_commit(repo: Path):
    """Section 5: a commit records what the index holds. A staged content that is not
    verified makes the file unreviewed although the working file has the baseline's content."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    git(repo, "add", "src/parser.py")
    (repo / "src" / "parser.py").write_text(PARSER, encoding="utf-8")
    session, rec, store = await session_on(repo, [[bash("git commit -m second", run=COMMIT)]])
    first = git(repo, "rev-parse", "HEAD")
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "this command is a commit" in request.reason
        assert "src/parser.py" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()
    assert git(repo, "rev-parse", "HEAD") == first
    assert list(store.trusted_commits) == [first]
    update = rec.of(HonestyUpdate)[-1]
    assert update.unreviewed == ["src/parser.py"] and not update.clean


async def test_a_verified_content_stays_reviewed_in_the_index_after_a_further_edit(repo: Path):
    """A file that was verified, staged and verified again is not unreviewed because the index
    still holds the earlier verified content; the commit of that content is certified."""
    again = CHANGED + "\n\nLIMIT = 3\n"
    session, rec, store = await session_on(
        repo,
        [
            [
                write("src/parser.py", CHANGED),
                bash("git add src/parser.py", run=["git", "add", "src/parser.py"]),
                write("src/parser.py", again),
                bash("git commit -m second", run=COMMIT),
            ]
        ],
    )
    await run_turns(session, rec)
    assert [c.decision for c in checks(rec)] == [Decision.PASS] * 4
    head = git(repo, "rev-parse", "HEAD")
    assert store.trusted_commits[head].method == "check"
    assert rec.of(HonestyUpdate)[-1].clean


async def test_a_checker_s_escalation_is_denied_while_marks_are_open(repo: Path):
    """Section 5: a shell command without the checker's answer counts as executing also when
    the checker escalates; with open marks it is denied and nobody is asked."""
    session, rec, _store = await session_on(
        repo, [[bash("make test")]], models={"command": marking_checker(decision="ESCALATE")}
    )
    mark(session)
    await run_turns(session, rec)
    (check,) = checks(rec)
    assert check.decision is Decision.DENY
    assert "src/parser.py:fields" in check.reason
    assert "The checkers also escalated: the marking fake checker" in check.reason
    assert rec.of(EscalationRequest) == []


async def test_a_checker_s_escalation_names_what_is_not_verified(repo: Path):
    """Section 5: without marks the checker's escalation stands, no review runs, and the
    person is told what is not verified and that an allow certifies nothing."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, _store = await session_on(
        repo, [[bash("make test")]], models={"command": marking_checker(decision="ESCALATE")}
    )
    reviews = await reviewing(session)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert request.reason.startswith("the marking fake checker")
        assert "1 unreviewed file(s) (src/parser.py)" in request.reason
        assert "lets it run once and certifies nothing" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()
    assert reviews == [], "no review runs for a step a person decides anyway"


@pytest.mark.parametrize(
    "action",
    [
        tool("WebFetch", url="https://example.org/", prompt="read"),
        tool("WebSearch", query="calu"),
        tool("mcp__tracker__list_issues", project="calu"),
        tool("Read", file_path="../notes.txt"),
    ],
    ids=["WebFetch", "WebSearch", "mcp", "Read"],
)
async def test_a_tool_that_runs_no_command_is_not_stopped_by_open_marks(
    repo: Path, action: dict[str, Any]
):
    """Section 5: the default "no answer means it executes" holds for shell commands only.
    The Read names a file outside the project, which the command checker judges."""
    (repo.parent / "notes.txt").write_text("notes\n", encoding="utf-8")
    session, rec, _store = await session_on(repo, [[action]], models={"command": marking_checker()})
    mark(session)
    await run_turns(session, rec)
    assert [c.decision for c in checks(rec)] == [Decision.PASS]


async def test_another_tool_is_stopped_when_its_checker_says_it_executes(repo: Path):
    """Section 5: an answer "executes" counts for a tool that is not a shell command, except
    for the tools that only read."""
    session, rec, _store = await session_on(
        repo,
        [
            [
                tool("mcp__runner__run_tests", target="all"),
                tool("WebFetch", url="https://example.org/run_tests", prompt="read"),
            ]
        ],
        models={"command": marking_checker(executes=("run_tests",))},
    )
    mark(session)
    await run_turns(session, rec)
    mcp, fetch = checks(rec)
    assert mcp.decision is Decision.DENY and "src/parser.py:fields" in mcp.reason
    assert fetch.decision is Decision.PASS


@pytest.mark.parametrize("outcome", ["denied", "failed"])
async def test_a_command_checker_s_mark_is_recorded_whatever_becomes_of_the_command(
    repo: Path, outcome: str
):
    """Section 3: the dirty marks of a command's checkers name code that exists, so they are
    recorded when the checkers answer, also for a command that is denied or fails."""
    decision = "DENY" if outcome == "denied" else "PASS"
    session, rec, _store = await session_on(
        repo,
        [[bash("make test", fails=outcome == "failed", result="1 failed")]],
        mode="track",
        models={
            "command": marking_checker(
                dirty=[("src/parser.py", "fields", "unsorted")], decision=decision
            )
        },
    )
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision[decision]
    assert [m.label() for m in session.honesty.open_marks] == ["src/parser.py:fields"]
    assert logs(rec).count("1 spot(s) recorded as dishonest") == 1


async def test_files_of_a_write_set_reach_the_state_with_its_next_computation(repo: Path):
    """Section 12: the session collects the written paths on the event loop and hands them to
    the state when it is computed next."""
    session, _rec, _store = await session_on(repo, [[]])
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("x = 1\n", encoding="utf-8")
    session._watch_written(["build/gen.py"])
    assert session.honesty.watched == set()
    state = await session.honesty_snapshot()
    assert session.honesty.watched == {"build/gen.py"}
    assert state.unreviewed.paths == ("build/gen.py",)
