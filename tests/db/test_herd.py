"""Herd management on the database alone: list sessions, list and resolve escalations."""

import anyio
import pytest

from calu import herd
from calu.checkers import CodeMark, Decision
from calu.db import HONESTY_CHANGED, Db, connect, listen
from calu.evals import cases_from_escalations
from calu.setup import setup
from calu.store import CheckerRun, DatabaseSessionStore, SessionIdentity, StepDecision, StepRow
from calu.tokens import token_hash

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(hostname="box", os_user="u", directory="/w", pid=7, os="Linux")


async def test_sessions_escalations_and_resolution(db_url: str) -> None:
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("escalated")
        step = await store.add_step(StepRow(seq=1, tool_name="Bash", tool_input={"command": "x"}))
        first = await store.open_escalation("needs a decision", {"rendered": "`rm -rf /`"}, step)
        second = await store.open_escalation("another one", {}, step)

        rows = await herd.sessions(db)
        assert len(rows) == 1
        assert rows[0].id == session_id and rows[0].state == "escalated"
        assert rows[0].open_escalations == 2
        assert "box:/w" in rows[0].line() and "escalated (2 open)" in rows[0].line()

        open_rows = await herd.escalations(db)
        assert [r.id for r in open_rows] == [first, second]
        assert open_rows[0].rendered == "`rm -rf /`"
        assert "needs a decision" in open_rows[0].line()
        # Filtering by session gives the same escalations. Compared by id, not by row: a row
        # carries how long the session has been silent (D56), which the database measures
        # afresh on every read.
        assert [r.id for r in await herd.escalations(db, session_id)] == [r.id for r in open_rows]

        assert await herd.resolve(db, first, "allow", "go ahead") is True
        assert await herd.resolve(db, first, "deny") is False  # already resolved
        assert [r.id for r in await herd.escalations(db)] == [second]
        resolved = await store.escalation(first)
        assert resolved.allowed and resolved.note == "go ahead"

        with pytest.raises(ValueError):
            await herd.resolve(db, second, "maybe")

        await store.end("stopped")
        assert await herd.sessions(db) == []
        assert len(await herd.sessions(db, active_only=False)) == 1
    finally:
        await owner.close()


async def test_resolved_escalations_become_evaluation_cases(db_url: str) -> None:
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        # A step the command checker escalated and a human then allowed becomes a case.
        checked = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "deploy"})
        )
        await store.add_checker_run(
            checked,
            CheckerRun(
                checker_name="command",
                checker_version=1,
                model="test:model",
                decision="escalate",
            ),
        )
        first = await store.open_escalation(
            "why", {"tool_input": {"command": "deploy"}, "files": {}}, checked
        )
        await herd.resolve(db, first, "allow", "go ahead")
        # A step the rules escalated (no checker run) says nothing about the checkers.
        unchecked = await store.add_step(
            StepRow(seq=2, tool_name="Bash", tool_input={"command": "sudo ls"})
        )
        second = await store.open_escalation(
            "deny rule", {"tool_input": {"command": "sudo ls"}}, unchecked
        )
        await herd.resolve(db, second, "deny")
        # An open escalation has no human decision yet.
        await store.open_escalation("open", {"tool_input": {"command": "x"}}, checked)

        # A step the checker **approved** and a human then denied, because an
        # auto-escalation category was on (D42): the label for a false negative, which is the
        # one the evals could not see before.
        approved = await store.add_step(
            StepRow(seq=3, tool_name="Write", tool_input={"file_path": "a.py", "content": "x"})
        )
        await store.add_checker_run(
            approved,
            CheckerRun(
                checker_name="edit",
                checker_version=1,
                model="test:model",
                decision="pass",
            ),
        )
        third = await store.open_escalation(
            "every edit waits for a human",
            {"tool_input": {"file_path": "a.py", "content": "x"}, "files": {}},
            approved,
        )
        await herd.resolve(db, third, "deny", "not like that")

        cases = await cases_from_escalations(db)
        # Newest first, and the rules escalation stays out: no checker ran on it.
        assert [c.name for c in cases] == [f"escalation-{third}", f"escalation-{first}"]
        assert cases[0].checker == "edit"
        # The checker said PASS here, so ALLOW is scored as a critical miss.
        assert cases[0].expected == [Decision.DENY, Decision.ESCALATE]
        assert cases[1].checker == "command"
        assert cases[1].expected == [Decision.ALLOW, Decision.ESCALATE]
        assert cases[1].source == "escalations"
    finally:
        await owner.close()


async def test_client_tokens_are_minted_checked_and_revoked(db_url: str) -> None:
    """D30: a session accepts every non-revoked token of its own session, and only those."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_address("ws://127.0.0.1:8765")
        printed = token_hash("printed-by-calu-run")
        await store.add_client_token(printed, "ws://127.0.0.1:8765")

        token_id, token = await herd.mint_token(db, session_id, "zed on the laptop")
        assert await store.client_token_ok(token_hash(token)) is True
        assert await store.client_token_ok(printed) is True
        assert await store.client_token_ok(token_hash("guessed")) is False

        rows = await herd.list_tokens(db, session_id)
        assert [row.id for row in rows] == [token_id, token_id - 1]
        minted = rows[0]
        assert minted.label == "zed on the laptop"
        assert minted.address == "ws://127.0.0.1:8765"
        assert minted.last_used_at is not None  # the check above stamped it
        assert not minted.revoked
        assert "valid" in minted.line() and "zed on the laptop" in minted.line()
        # The token itself is nowhere in what an operator can read.
        assert token not in minted.line()

        assert await herd.revoke_token(db, token_id) is True
        assert await herd.revoke_token(db, token_id) is False  # already revoked
        assert await store.client_token_ok(token_hash(token)) is False
        assert await store.client_token_ok(printed) is True  # the others keep working
        assert (await herd.list_tokens(db, session_id))[0].revoked is True

        # A token of another session does not open this one.
        other = DatabaseSessionStore(db)
        other_session = await other.start(IDENTITY)
        _, foreign = await herd.mint_token(db, other_session, "elsewhere")
        assert await store.client_token_ok(token_hash(foreign)) is False
        assert await other.client_token_ok(token_hash(foreign)) is True
        assert len(await herd.list_tokens(db)) == 3

        with pytest.raises(ValueError):
            await herd.mint_token(db, "00000000-0000-0000-0000-000000000000")
    finally:
        await owner.close()


async def test_a_session_whose_process_is_gone_is_visible_as_such(db_url: str) -> None:
    """A killed process cannot set its own state, so the herd needs a heartbeat (D56)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("running")

        # A fresh session is not stale, and the silence is measured by the database.
        rows = await herd.sessions(db)
        assert [row.id for row in rows] == [session_id]
        assert rows[0].stale is False
        assert rows[0].last_seen is not None
        assert rows[0].silent_for_s < 10
        assert "SILENT" not in rows[0].line()

        # The heartbeat moves `last_seen`.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
            (session_id,),
        )
        await store.heartbeat()
        assert (await herd.sessions(db))[0].stale is False

        # Silence for longer than `STALE_AFTER_S` marks the session, and the line says so.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '20 minutes' WHERE id = %s::uuid",
            (session_id,),
        )
        gone = (await herd.sessions(db))[0]
        assert gone.stale is True
        assert gone.silent_for_s > herd.STALE_AFTER_S
        assert "SILENT for 20 min" in gone.line()

        # A session that ended is never stale: it said so itself.
        await store.end("stopped")
        ended = (await herd.sessions(db, active_only=False))[0]
        assert ended.stale is False
        assert "SILENT" not in ended.line()
    finally:
        await owner.close()


async def test_an_escalation_of_a_gone_session_says_so(db_url: str) -> None:
    """Answering it is recorded and reaches nobody, so the list has to say which ones (D56)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_state("escalated")
        escalation = await store.open_escalation("why", {"rendered": "$ rm -rf build"})

        # While the session beats, the escalation is an ordinary one.
        live = (await herd.escalations(db))[0]
        assert live.unanswerable is False
        assert "the session is gone" not in live.line()
        rendered = herd.render_escalation(live, await herd.escalation_context(db, escalation))
        assert "process is gone" not in rendered

        # Once the process stops saying it is there, every view says so.
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '10 minutes' WHERE id = %s::uuid",
            (session_id,),
        )
        gone = (await herd.escalations(db))[0]
        assert gone.unanswerable is True
        assert "the session is gone: answering changes nothing" in gone.line()
        one = await herd.escalation(db, escalation)
        assert one is not None and one.unanswerable is True
        rendered = herd.render_escalation(one, await herd.escalation_context(db, escalation))
        assert "the session's process is gone" in rendered
        assert "will not run" in rendered

        # A session that ended counts the same way: nobody is waiting either.
        await db.execute(
            "UPDATE sessions SET last_seen = now(), ended_at = now() WHERE id = %s::uuid",
            (session_id,),
        )
        ended = await herd.escalation(db, escalation)
        assert ended is not None and ended.unanswerable is True
    finally:
        await owner.close()


async def test_an_escalation_nobody_waits_for_can_be_cancelled(db_url: str) -> None:
    """Cancelling records that nothing was decided, which is the truth (PLAN.md D57)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        escalation = await store.open_escalation("why", {"rendered": "$ deploy"})
        await db.execute(
            "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
            (session_id,),
        )

        # The hint says what to do with it, instead of offering a decision.
        one = await herd.escalation(db, escalation)
        assert one is not None
        rendered = herd.render_escalation(one, await herd.escalation_context(db, escalation))
        assert f"calu resolve {escalation} cancel" in rendered
        assert "allow|deny" not in rendered

        assert await herd.resolve(db, escalation, "cancel", "the session is gone") is True
        row = await herd.escalation(db, escalation)
        assert row is not None
        assert row.state == "cancelled", "a cancellation is not a decision"
        assert row.note == "the session is gone"
        # It is out of the queue a human works through.
        assert await herd.escalations(db) == []
        # And cancelling it twice changes nothing.
        assert await herd.resolve(db, escalation, "cancel") is False

        # An unknown verb is still refused.
        with pytest.raises(ValueError, match="allow, deny, answer or cancel"):
            await herd.resolve(db, escalation, "maybe")
    finally:
        await owner.close()


async def test_the_checkers_record_says_where_a_human_disagreed(db_url: str) -> None:
    """The free half of the evals: what already happened, per checker (PLAN.md D61)."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)

        seq = {"n": 0}

        async def judged(decision: str, outcome: str | None, ms: int = 100) -> None:
            """Record one step, its check and the human's verdict.

            Args:
                decision: What the checker decided.
                outcome: What the human decided ("allowed"/"denied"), or None for no
                    escalation at all.
                ms: How long the check took.
            """
            seq["n"] += 1
            step = await store.add_step(
                StepRow(seq=seq["n"], tool_name="Bash", tool_input={"command": "x"})
            )
            await store.add_checker_run(
                step,
                CheckerRun(
                    checker_name="command",
                    checker_version=1,
                    model="test:model",
                    decision=decision,
                    duration_ms=ms,
                ),
            )
            if outcome is not None:
                escalation = await store.open_escalation("why", {}, step)
                await db.execute(
                    "UPDATE escalations SET state = %s WHERE id = %s", (outcome, escalation)
                )

        await judged("pass", None, ms=50)
        await judged("pass", None, ms=150)
        await judged("escalate", "denied", ms=100)  # the caution was right
        await judged("escalate", "allowed", ms=100)  # an interruption for nothing
        await judged("pass", "denied", ms=100)  # a miss: approved, then denied by a human

        (stats,) = await herd.checker_stats(db)
        assert stats.checker_name == "command"
        assert (stats.runs, stats.passed, stats.escalated) == (5, 3, 2)
        assert stats.escalated_then_denied == 1
        assert stats.escalated_then_allowed == 1
        assert stats.passed_then_denied == 1, "a miss has to be countable"
        assert stats.median_ms == 100

        line = stats.line()
        assert "5 run(s)" in line and "3 pass" in line
        assert "1/2 agreed with its escalations" in line
        assert "1 it approved were denied" in line

        # A window that excludes everything reports nothing rather than zeroes.
        await db.execute("UPDATE checker_runs SET created_at = now() - interval '30 days'")
        assert await herd.checker_stats(db, since_days=7) == []
        assert await herd.checker_stats(db, since_days=60) != []
    finally:
        await owner.close()


async def test_the_steps_a_checker_approved_and_a_human_denied_are_listable(
    db_url: str,
) -> None:
    """D61's counts point at these rows; D62 lets a person read them."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)

        # A step the edit checker approved, held back by a category, then denied by a human.
        approved = await store.add_step(
            StepRow(seq=1, tool_name="Bash", tool_input={"command": "rm -rf build"})
        )
        await store.decide_step(
            approved, StepDecision(decision="pass", reason="", summary="Deletes the build dir.")
        )
        await store.add_checker_run(
            approved,
            CheckerRun(
                checker_name="command",
                checker_version=1,
                model="test:model",
                decision="pass",
                reason="removing a build directory is routine",
            ),
        )
        escalation = await store.open_escalation("every command waits", {}, approved)
        assert await herd.resolve(db, escalation, "deny", "not this directory") is True

        # A step it escalated and a human allowed is not a miss: the caution was wrong, not the
        # approval.
        cautious = await store.add_step(
            StepRow(seq=2, tool_name="Bash", tool_input={"command": "ls"})
        )
        await store.add_checker_run(
            cautious,
            CheckerRun(
                checker_name="command", checker_version=1, model="test:model", decision="escalate"
            ),
        )
        other = await store.open_escalation("why", {}, cautious)
        await herd.resolve(db, other, "allow")

        (miss,) = await herd.checker_misses(db)
        assert miss.escalation_id == escalation
        assert miss.checker_name == "command" and miss.decision == "pass"
        assert miss.command == "rm -rf build"
        assert miss.checker_reason == "removing a build directory is routine"
        assert miss.human_note == "not this directory"
        line = miss.line()
        assert f"escalation {escalation}" in line
        assert "command said pass" in line
        assert "rm -rf build" in line
        assert "human: denied -- not this directory" in line

        # Filtering by checker and by age works, and the counts agree with the stats.
        assert await herd.checker_misses(db, checker="edit") == []
        assert len(await herd.checker_misses(db, checker="command")) == 1
        stats = {row.checker_name: row for row in await herd.checker_stats(db)}
        assert stats["command"].passed_then_denied == 1
        await db.execute("UPDATE escalations SET resolved_at = now() - interval '30 days'")
        assert await herd.checker_misses(db, since_days=7) == []
    finally:
        await owner.close()


async def test_checker_token_totals_show_whether_the_cache_is_working(db_url: str) -> None:
    """D63: the recorded tokens are readable, and one step's two escalations do not double them."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)

        async def run(seq: int, inp: int | None, out: int, cached: int) -> int:
            step = await store.add_step(
                StepRow(seq=seq, tool_name="Bash", tool_input={"command": "x"})
            )
            await store.add_checker_run(
                step,
                CheckerRun(
                    checker_name="command",
                    checker_version=1,
                    model="test:model",
                    decision="escalate",
                    input_tokens=inp,
                    output_tokens=out,
                    cache_read_tokens=cached,
                ),
            )
            return step

        # Two checks on a long prompt: the second one was served from the cache.
        await run(1, 20_000, 40, 0)
        shared = await run(2, 20_000, 60, 19_000)
        # The same step escalated twice. The join over escalations must not count its tokens
        # again -- it is one check, not two.
        await store.open_escalation("first", {}, shared)
        await store.open_escalation("again", {}, shared)

        (stats,) = await herd.checker_stats(db)
        assert stats.input_tokens == 40_000, "two escalations on one step doubled its tokens"
        assert stats.output_tokens == 100
        assert stats.cache_read_tokens == 19_000
        assert stats.cached_share is not None
        assert stats.cached_share == pytest.approx(0.475)
        line = stats.line()
        assert "40k in / 100 out" in line
        assert "48% of the input from cache" in line

        # A checker whose model reports no usage says nothing about tokens instead of "0% cached".
        await db.execute("UPDATE checker_runs SET input_tokens = NULL, cache_read_tokens = NULL")
        (none,) = await herd.checker_stats(db)
        assert none.input_tokens == 0
        assert none.cached_share is None
        assert "from cache" not in none.line() and " in / " not in none.line()
    finally:
        await owner.close()


async def test_step_stats_say_how_many_steps_were_free_and_how_long_the_gate_took(
    db_url: str,
) -> None:
    """D64: the paths through the gate and its delay, read from the record."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)

        # An empty database answers without a special case.
        empty = await herd.step_stats(db)
        assert empty.steps == 0 and empty.free_share is None and empty.lines() == []

        async def step(seq: int, rules: str | None, checked: bool) -> int:
            step_id = await store.add_step(
                StepRow(seq=seq, tool_name="Bash", tool_input={"command": "x"})
            )
            await store.decide_step(
                step_id, StepDecision(decision="allow", rules_outcome=rules, reason="r")
            )
            if checked:
                await store.add_checker_run(
                    step_id,
                    CheckerRun(
                        checker_name="command",
                        checker_version=1,
                        model="test:model",
                        decision="pass",
                    ),
                )
            return step_id

        await step(1, "allow", checked=False)  # allow-listed: free
        await step(2, "allow", checked=False)
        await step(3, None, checked=False)  # a tool the lists do not cover
        await step(4, "deny", checked=False)  # a rule denied it: also no model call
        escalated = await step(5, "unlisted", checked=True)
        await step(6, "unlisted", checked=True)

        stats = await herd.step_stats(db)
        assert stats.steps == 6
        assert (stats.rules_allowed, stats.rules_denied, stats.rules_unlisted) == (2, 1, 2)
        assert stats.rules_silent == 1
        assert stats.checked == 2
        assert stats.free_share == pytest.approx(4 / 6)
        assert stats.median_gate_ms is not None and stats.median_gate_ms >= 0
        assert stats.slowest_gate_ms is not None
        assert stats.median_wait_s is None, "nothing was escalated yet"
        assert stats.resolved == 0

        first, delay = stats.lines()  # nothing to say about people yet, so no third line
        assert "6 step(s): 2 went to a checker, 4 (67%) did not" in first
        assert "2 allow-listed, 1 denied by a rule" in first
        assert "calu's own delay per step" in delay
        assert "no human waiting time" in delay

        # An answered escalation adds the human's waiting time, and takes its step out of the
        # delay measurement: that step waited for a person, not for calu.
        escalation = await store.open_escalation("why", {}, escalated)
        assert await herd.resolve(db, escalation, "allow") is True
        await db.execute(
            "UPDATE escalations SET created_at = now() - interval '90 seconds' WHERE id = %s",
            (escalation,),
        )
        answered = await herd.step_stats(db)
        assert answered.resolved == 1
        assert answered.median_wait_s == 90
        assert any("after ~90 s of waiting for a person" in line for line in answered.lines())

        # A window that excludes everything reports zeroes rather than failing.
        await db.execute("UPDATE steps SET created_at = now() - interval '30 days'")
        await db.execute("UPDATE escalations SET resolved_at = now() - interval '30 days'")
        old = await herd.step_stats(db, since_days=7)
        assert old.steps == 0 and old.resolved == 0 and old.median_gate_ms is None
    finally:
        await owner.close()


async def test_the_honesty_register_is_written_read_and_cleared(db_url: str) -> None:
    """D82, D83: a checker's marks reach the register, and a person can override one."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(seq=1, tool_name="Write", tool_input={"file_path": "src/parser.py"})
        )

        # The checker marks two spots; one of them it marks again with a better reason.
        opened, closed = await store.apply_honesty_marks(
            [
                CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies"),
                CodeMark(path="docs/api.md", reason="it documents a removed flag"),
            ],
            [],
            step,
            "feature/parser",
        )
        assert (opened, closed) == (2, 0)
        opened, _ = await store.apply_honesty_marks(
            [CodeMark(path="src/parser.py", symbol="fields", reason="it returns insertion order")],
            [],
            step,
        )
        assert opened == 1
        marks = await store.open_honesty_marks()
        assert [m.label() for m in marks] == ["src/parser.py:fields", "docs/api.md"]
        assert marks[0].reason == "it returns insertion order", "a repeated mark refreshes it"
        assert marks[0].git_branch == "feature/parser"

        # The checker finds one of them honest again; a clean mark for an unmarked spot does
        # nothing, which the count reports.
        _, closed = await store.apply_honesty_marks(
            [],
            [
                CodeMark(path="src/parser.py", symbol="fields", reason="fixed"),
                CodeMark(path="src/other.py", reason="never was dirty"),
            ],
            step,
        )
        assert closed == 1, "only the open mark was closed"
        assert [m.label() for m in await store.open_honesty_marks()] == ["docs/api.md"]

        # The operator's view, and the override for a mark nobody agrees with.
        standing = await herd.honesty_marks(db)
        assert [m.label() for m in standing] == ["docs/api.md"]
        assert "it documents a removed flag" in standing[0].line()
        everything = await herd.honesty_marks(db, closed=True)
        assert len(everything) == 2, "the cleared mark is kept as history"

        cleared = await herd.clear_honesty_mark(db, standing[0].id, "kai")
        assert cleared is not None and not cleared.open and cleared.closed_by == "kai"
        assert await herd.honesty_marks(db) == []
        assert await herd.clear_honesty_mark(db, standing[0].id) is None, "already closed"
    finally:
        await owner.close()


async def test_the_stats_say_what_a_checker_s_marks_came_to(db_url: str) -> None:
    """D87: a mark a person had to overrule is what says whether a checker may block."""
    owner = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(seq=1, tool_name="Write", tool_input={"file_path": "src/a.py"})
        )
        await store.add_checker_run(
            step,
            CheckerRun(
                checker_name="honesty",
                checker_version=1,
                model="test:model",
                decision="pass",
            ),
        )
        # Three marks by this checker: one it cleared itself, one a person overruled, one open.
        await store.apply_honesty_marks(
            [
                CodeMark(path="src/a.py", reason="a"),
                CodeMark(path="src/b.py", reason="b"),
                CodeMark(path="src/c.py", reason="c"),
            ],
            [],
            step,
            None,
            "honesty",
        )
        await store.apply_honesty_marks([], [CodeMark(path="src/a.py", reason="fixed")], step)
        (overruled,) = [m for m in await herd.honesty_marks(db) if m.path == "src/b.py"]
        await herd.clear_honesty_mark(db, overruled.id, "kai")

        (stats,) = [s for s in await herd.checker_stats(db) if s.checker_name == "honesty"]
        assert stats.marks_opened == 3
        assert stats.marks_cleared == 1, "the checker closed that one itself"
        assert stats.marks_overridden == 1, "a person closed that one"
        line = stats.line()
        assert "marks: 3 opened, 1 cleared" in line
        assert "1 overruled by a person" in line

        # A checker with no marks says nothing about them rather than "0 opened".
        await store.add_checker_run(
            step,
            CheckerRun(
                checker_name="command", checker_version=1, model="test:model", decision="pass"
            ),
        )
        (command,) = [s for s in await herd.checker_stats(db) if s.checker_name == "command"]
        assert command.marks_opened == 0
        assert "marks:" not in command.line()
    finally:
        await owner.close()


async def test_the_register_is_announced_so_a_second_session_hears_it(db_url: str) -> None:
    """D92: the register is shared by a project, so a mark has to reach the other sessions.

    Without the notification a second session would run code the first has just recorded as
    dishonest, because each session keeps the register in memory. The channel is the register's
    own, not the configuration's: a mark is written on every edit, and announcing marks on
    `config_changed` would make every session of the database reload all of its settings that
    often.
    """
    owner = await connect(db_url)
    listener = await connect(db_url)
    db = Db(owner)
    try:
        await setup(owner, db_url, "test:model")
        await listen(listener, [HONESTY_CHANGED])
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(seq=1, tool_name="Write", tool_input={"file_path": "src/a.py"})
        )

        async def heard() -> list[tuple[str, str]]:
            """Collect the notifications that arrived, with their payloads."""
            found: list[tuple[str, str]] = []
            with anyio.move_on_after(5):
                async for note in listener.notifies():
                    found.append((note.channel, note.payload))
                    break
            return found

        # Opening a mark announces itself, and the payload names the session that wrote it so
        # that this very session can skip its own.
        notes: list[list[tuple[str, str]]] = []
        async with anyio.create_task_group() as tg:

            async def collect() -> None:
                """Wait for one notification."""
                notes.append(await heard())

            tg.start_soon(collect)
            await anyio.sleep(0.2)
            await store.apply_honesty_marks(
                [CodeMark(path="src/a.py", reason="the docstring lies")], [], step, None, "honesty"
            )
        (arrived,) = notes
        assert arrived, "opening a mark announced nothing"
        channel, payload = arrived[0]
        assert channel == HONESTY_CHANGED
        assert session_id in payload, "the payload has to name the writer, to skip its own"

        # Clearing one announces itself on the same channel, with no session of its own.
        (mark,) = await herd.honesty_marks(db)
        cleared: list[list[tuple[str, str]]] = []
        async with anyio.create_task_group() as tg:

            async def collect_clear() -> None:
                """Wait for the notification of the override."""
                cleared.append(await heard())

            tg.start_soon(collect_clear)
            await anyio.sleep(0.2)
            await herd.clear_honesty_mark(db, mark.id, "kai")
        (after,) = cleared
        assert after and after[0][0] == HONESTY_CHANGED

        # A call that changes nothing stays quiet, so an edit without marks is not a broadcast.
        quiet: list[list[tuple[str, str]]] = []
        async with anyio.create_task_group() as tg:

            async def collect_quiet() -> None:
                """Wait, and expect nothing."""
                with anyio.move_on_after(1):
                    async for note in listener.notifies():
                        quiet.append([(note.channel, note.payload)])
                        break

            tg.start_soon(collect_quiet)
            await anyio.sleep(0.2)
            await store.apply_honesty_marks([], [], step, None, "honesty")
        assert quiet == [], "a call that changed nothing must not wake every session"
    finally:
        await listener.close()
        await owner.close()
