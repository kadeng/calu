"""The web interface against a real database: the pages, resolving, minting, editing."""

import httpx
import pytest

from calu import admin, herd
from calu.context.turns import StepRecord, TurnRecord
from calu.db import Db, connect
from calu.setup import setup
from calu.store import (
    CheckerRun,
    DatabaseSessionStore,
    SessionIdentity,
    StepDecision,
    StepRow,
)

# The web interface is an optional extra, so a checkout synced without it skips these
# tests instead of failing to collect them (`uv sync --all-extras`, docs/TESTING.md).
WEB_EXTRA = "the web interface needs the `web` extra: uv sync --all-extras"
pytest.importorskip("starlette", reason=WEB_EXTRA)
pytest.importorskip("jinja2", reason=WEB_EXTRA)

from calu.web.app import NotLocal, check_host, create_app  # ruff: ignore[module-import-not-at-top-of-file] (imported after the guard above)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(hostname="box", os_user="u", directory="/w", pid=7, os="Linux")


async def populate(db: Db) -> tuple[str, int]:
    """Create a session with two steps, a finished turn and one open escalation."""
    store = DatabaseSessionStore(db)
    session_id = await store.start(IDENTITY)
    await store.set_state("escalated")
    await store.set_address("ws://127.0.0.1:8765")
    await store.set_claude_session("claude-abc")
    first = await store.add_step(
        StepRow(seq=1, tool_name="Bash", tool_input={"command": "make test"}, turn=1)
    )
    await store.decide_step(
        first,
        StepDecision(
            decision="pass", reason="", summary="Runs the test suite.", narration="Claude said it"
        ),
    )
    await store.finish_step(first, "2 passed")
    await store.add_checker_run(
        first,
        CheckerRun(
            checker_name="command",
            checker_version=1,
            model="test:model",
            decision="pass",
            duration_ms=120,
        ),
    )
    await store.record_changes(first, "tree-a", "tree-b", ["a.py"], ["a.py"])
    second = await store.add_step(
        StepRow(seq=2, tool_name="Bash", tool_input={"command": "sudo ls"}, turn=1)
    )
    await store.decide_step(
        second, StepDecision(decision="escalate", reason="the deny list matched", summary="sudo")
    )
    escalation = await store.open_escalation(
        "the deny list matched",
        # The same shape a session writes (`Session._open_escalation`), so the pages and the
        # triage API are tested against what they really get.
        {
            "rendered": "## Step\n\n    sudo ls",
            "recent": "## Turn 1",
            "tool_name": "Bash",
            "tool_input": {"command": "sudo ls"},
        },
        second,
    )
    await store.save_turn(
        TurnRecord(
            index=1,
            user_inputs=["run the tests"],
            steps=[StepRecord(tool_name="Bash", decision="pass", summary="Runs the test suite.")],
            answer="Done.",
        )
    )
    return session_id, escalation


async def client_for(db: Db) -> httpx.AsyncClient:
    """An HTTP client that speaks to the application in this event loop."""
    app = create_app(db, git_url_template="")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=True
    )


def test_only_the_loopback_interface_is_served() -> None:
    check_host("127.0.0.1")
    check_host("::1")
    for host in ("0.0.0.0", "192.168.1.10", "example.com"):
        with pytest.raises(NotLocal):
            check_host(host)


async def test_the_fleet_page_lists_the_sessions_and_their_escalations(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        async with await client_for(db) as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert "box" in page.text and "/w" in page.text
            assert "escalated" in page.text
            assert session_id in page.text
            # The session's own waiting count is in the table; the header badge is filled from
            # the API, so that every page shows it and not only this one.
            assert 'class="count"' in page.text
            assert 'id="attention"' in page.text

            fleet = (await client.get("/api/sessions")).json()
            assert fleet["waiting"] == 1
            assert [s["where"] for s in fleet["sessions"]] == ["box:/w"]
            assert fleet["sessions"][0]["group"] == "needs_you"
            assert fleet["sessions"][0]["open_escalations"] == 1
            assert fleet["sessions"][0]["url"].endswith(session_id)
            # It serves clients, so the palette jumps straight to the live view.
            assert fleet["sessions"][0]["live"].endswith(f"{session_id}/live")
    finally:
        await conn.close()


async def test_the_session_page_shows_every_level_of_detail(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        async with await client_for(db) as client:
            compact = (await client.get(f"/sessions/{session_id}")).text
            assert "Runs the test suite." in compact
            assert "run the tests" in compact
            assert "Claude said it" not in compact  # no narration at this level
            assert "make test" not in compact  # no tool input either

            summary = (await client.get(f"/sessions/{session_id}?detail=summary")).text
            assert "## Turn 1" in summary  # exactly what a checker reads

            detailed = (await client.get(f"/sessions/{session_id}?detail=detailed")).text
            assert "Claude said it" in detailed
            assert "the deny list matched" in detailed
            assert "make test" not in detailed

            full = (await client.get(f"/sessions/{session_id}?detail=full")).text
            assert "make test" in full  # the tool input
            assert "2 passed" in full  # the result
            assert "undeclared" in full  # the write set of D31
            assert "judged by" in full

            # An unknown level falls back to compact instead of failing.
            assert (await client.get(f"/sessions/{session_id}?detail=nonsense")).status_code == 200
            assert (await client.get("/sessions/does-not-exist")).status_code == 404
    finally:
        await conn.close()


async def test_a_session_page_offers_the_commands_to_connect_with(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        async with await client_for(db) as client:
            page = (await client.get(f"/sessions/{session_id}")).text
            assert "calu attach ws://127.0.0.1:8765" in page
            assert "calu acp ws://127.0.0.1:8765" in page
            assert "calu run --resume claude-abc" in page
            assert "mint a token above" in page

            # Minting shows the token once and records only its hash.
            after = await client.post(f"/sessions/{session_id}/token", data={"label": "zed"})
            assert after.status_code == 200
            tokens = await herd.list_tokens(db, session_id)
            assert [t.label for t in tokens] == ["zed"]
            shown = [
                line
                for line in after.text.splitlines()
                if "calu attach ws://127.0.0.1:8765 --token" in line
            ]
            assert shown and "mint a token above" not in shown[0]
            # The token is gone from the page after a reload: it is shown exactly once.
            assert "shown once" not in (await client.get(f"/sessions/{session_id}")).text
    finally:
        await conn.close()


async def test_an_escalation_can_be_resolved_from_the_page(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, escalation = await populate(db)
        async with await client_for(db) as client:
            listing = (await client.get("/escalations")).text
            assert "the deny list matched" in listing and "sudo ls" in listing

            detail = (await client.get(f"/escalations/{escalation}")).text
            assert "## Step" in detail and "## Turn 1" in detail
            # A rules escalation has no checker run, so the page says nothing about checkers
            # rather than showing an empty table.
            assert "What the checkers decided" not in detail

            # Once a checker has judged the step -- which is the normal case, and the only
            # source of truth when a category held an approved step back (D42, D53) -- the page
            # shows what it decided and why.
            await db.execute(
                "INSERT INTO checker_runs"
                " (step_id, checker_name, checker_version, model, decision, reason)"
                " SELECT e.step_id, 'command', 2, 'test:model', 'pass', 'nothing to object to'"
                " FROM escalations e WHERE e.id = %s",
                (escalation,),
            )
            judged = (await client.get(f"/escalations/{escalation}")).text
            assert "What the checkers decided" in judged
            assert "command v2" in judged and "nothing to object to" in judged

            answered = await client.post(
                f"/escalations/{escalation}/resolve", data={"decision": "deny", "note": "no"}
            )
            assert answered.status_code == 200
            rows = await herd.escalations(db)
            assert rows == []  # it is not open any more
            store = DatabaseSessionStore(db)
            store.session_id = session_id
            resolved = await store.escalation(escalation)
            assert not resolved.allowed and resolved.note == "no"

            bad = await client.post(
                f"/escalations/{escalation}/resolve", data={"decision": "maybe"}
            )
            assert bad.status_code == 400
    finally:
        await conn.close()


async def test_checker_instructions_can_be_read_and_written(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        async with await client_for(db) as client:
            listing = (await client.get("/checkers")).text
            assert "command" in listing and "edit" in listing and "changeset" in listing

            before = next(r for r in await admin.checkers(db) if r.name == "command")
            page = (await client.get("/checkers/command")).text
            assert "textarea" in page

            saved = await client.post(
                "/checkers/command", data={"instructions": "# New instructions\n\nBe brief."}
            )
            assert saved.status_code == 200
            assert "Be brief." in saved.text
            after = next(r for r in await admin.checkers(db) if r.name == "command")
            assert after.version == before.version + 1  # a change bumps the version
            assert await admin.instructions(db, "command") == "# New instructions\n\nBe brief."

            empty = await client.post("/checkers/command", data={"instructions": "  "})
            assert empty.status_code == 400
            assert (await client.get("/checkers/nonsense")).status_code == 404
    finally:
        await conn.close()


async def test_the_rules_page_groups_the_lists(db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        async with await client_for(Db(conn)) as client:
            page = (await client.get("/rules")).text
            assert "command-deny" in page and "command-allow" in page
            assert "sudo" in page
    finally:
        await conn.close()


async def test_the_settings_page_edits_what_the_model_knows(db_url: str) -> None:
    """The owner's ask: model and effort dropdowns, the checker models, use_bedrock."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        async with await client_for(db) as client:
            page = (await client.get("/settings")).text
            # The controls the owner named, each generated from the configuration model.
            assert 'name="claude.model"' in page and 'list="claude-models"' in page
            assert 'name="claude.effort"' in page and ">xhigh<" in page
            assert 'name="claude.use_bedrock"' in page and 'type="checkbox"' in page
            assert 'name="command_checker.model"' in page
            assert 'name="command_checker.effort"' in page
            assert 'name="changeset_checker.effort"' in page
            assert 'name="claude.sandbox.deny_read"' in page  # a list, one per line
            assert 'name="clock.max_backward_s"' in page

            # Each control carries what the setting does, from the field's own docstring
            # (D47): the form has rendered `info.description` all along, and until the
            # configuration documented itself every one of them was empty.
            assert "Reasoning effort of the Claude session" in page
            assert "Set `CLAUDE_CODE_USE_BEDROCK=1` for Claude Code" in page
            assert "Paths a sandboxed command must not read" in page

            saved = await client.post(
                "/settings",
                data={
                    "claude.model": "sonnet",
                    "claude.effort": "high",
                    "claude.use_bedrock": "on",
                    "command_checker.effort": "off",
                    "command_checker.model": "mistral:mistral-small-2603",
                    "escalation_timeout_s": "3600",
                },
            )
            assert saved.status_code == 200
            values = await admin.settings(db)
            assert values["claude"]["model"] == "sonnet"
            assert values["claude"]["effort"] == "high"
            assert values["claude"]["use_bedrock"] is True
            assert values["command_checker"]["effort"] == "off"
            assert values["escalation_timeout_s"] == pytest.approx(3600.0)
            # An unchanged value is not written, so the audit log stays meaningful.
            assert "model" not in values.get("command_checker", {})

            # The configuration is validated before anything is stored.
            refused = await client.post("/settings", data={"claude.effort": "enormous"})
            assert refused.status_code == 400
            assert "refused" in refused.text
            assert (await admin.settings(db))["claude"]["effort"] == "high"
    finally:
        await conn.close()


async def test_triage_serves_the_queue_and_answers_it(db_url: str) -> None:
    """The owner's ask: sit in one mode and answer what comes, with a queue behind it."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, first = await populate(db)
        store = DatabaseSessionStore(db)
        store.session_id = session_id
        step = await store.add_step(
            StepRow(seq=3, tool_name="Write", tool_input={"file_path": "b.py"}, turn=1)
        )
        second = await store.open_escalation("an edit to judge", {"rendered": "## Edit"}, step)
        async with await client_for(db) as client:
            assert "Triage" in (await client.get("/triage")).text

            listing = (await client.get("/api/escalations")).json()["escalations"]
            assert [entry["id"] for entry in listing] == [first, second]
            # The one being decided carries its context; the queue behind it stays light.
            assert "sudo ls" in listing[0]["rendered"]
            assert listing[0]["command"] == "sudo ls"  # offered as an exact rule
            assert listing[1]["rendered"] == "" and listing[1]["command"] == ""
            assert listing[0]["where"] == "box:/w"

            answered = await client.post(
                f"/api/escalations/{first}/resolve",
                json={"decision": "deny", "note": "no", "remember": "sudo ls"},
            )
            assert answered.json() == {"ok": True, "remembered": "^sudo\\ ls$"}
            # The remembered rule is anchored to exactly that command and nothing wider.
            patterns = [row.pattern for row in await admin.rules(db, rule_list="command-deny")]
            assert "^sudo\\ ls$" in patterns

            again = await client.post(
                f"/api/escalations/{first}/resolve", json={"decision": "deny"}
            )
            assert again.status_code == 400 and again.json()["ok"] is False
            bad = await client.post(
                f"/api/escalations/{second}/resolve", json={"decision": "maybe"}
            )
            assert bad.status_code == 400

            left = (await client.get("/api/escalations")).json()["escalations"]
            assert [entry["id"] for entry in left] == [second]
    finally:
        await conn.close()


async def test_a_question_of_the_agent_is_answered_from_the_triage_queue(db_url: str) -> None:
    """D35: the options reach the page, and the answer is recorded as an answer."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        questions = [
            {
                "question": "Tabs or spaces?",
                "header": "Indentation",
                "options": [{"label": "Tabs", "description": "tabs"}, {"label": "Spaces"}],
                "multiSelect": False,
            }
        ]
        step = await store.add_step(
            StepRow(seq=1, tool_name="AskUserQuestion", tool_input={"questions": questions})
        )
        asked = await store.open_escalation(
            "Claude asks: Tabs or spaces? (Tabs, Spaces)",
            {
                "rendered": "## Question",
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": questions},
            },
            step,
        )
        async with await client_for(db) as client:
            entry = (await client.get("/api/escalations")).json()["escalations"][0]
            assert entry["questions"] == questions
            assert entry["command"] == ""  # it is not a command to allow or deny

            answered = await client.post(
                f"/api/escalations/{asked}/resolve", json={"answer": "Spaces"}
            )
            assert answered.json()["ok"] is True
            row = await herd.escalation(db, asked)
            assert row is not None
            # Neither allowed nor denied: answered, with the answer as the note (migration 0011).
            assert row.state == "answered" and row.note == "Spaces"
    finally:
        await conn.close()


async def test_the_fork_points_of_a_session_are_offered_per_turn(db_url: str) -> None:
    """D36: a turn can be forked before when calu recorded the uuids of it and its predecessor."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        await store.set_claude_session("claude-xyz")
        for number, (prompt, last) in enumerate([("p1", "a1"), ("p2", "a2"), ("p3", "")], start=1):
            await store.save_turn(
                TurnRecord(
                    index=number,
                    user_inputs=[f"turn {number}"],
                    answer="ok",
                    prompt_uuid=prompt,
                    last_entry_uuid=last,
                )
            )

        # Turn 1 has no predecessor; turn 3 can be forked before (turn 2 has its last entry).
        points = await herd.fork_points(db, session_id)
        assert [point.turn for point in points] == [2, 3]
        assert points[0].resume_at == "a1" and points[0].drops_turn == "p2"
        assert points[0].command() == "calu run --resume claude-xyz --fork-before-turn 2"

        # The same by Claude's session id, which is what `calu run --resume` takes.
        by_claude = await herd.fork_point(db, "claude-xyz", 3)
        assert by_claude is not None
        assert by_claude.resume_at == "a2" and by_claude.drops_turn == "p3"
        assert await herd.fork_point(db, "claude-xyz", 1) is None  # nothing to keep
        assert await herd.fork_point(db, "claude-xyz", 9) is None  # no such turn
        assert await herd.fork_point(db, "no-such-session", 2) is None

        async with await client_for(db) as client:
            page = (await client.get(f"/sessions/{session_id}")).text
            assert "fork before this turn" in page
            assert "calu run --resume claude-xyz --fork-before-turn 2" in page
    finally:
        await conn.close()


async def test_a_fork_point_spans_the_runs_of_one_conversation(db_url: str) -> None:
    """A resumed conversation has a session row per run, and its turns are spread over them."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        first = DatabaseSessionStore(db)
        await first.start(IDENTITY)
        await first.set_claude_session("claude-same")
        await first.save_turn(
            TurnRecord(index=1, user_inputs=["one"], prompt_uuid="p1", last_entry_uuid="a1")
        )
        await first.end("stopped")

        # `calu run --resume claude-same` opens a second row for the same conversation.
        second = DatabaseSessionStore(db)
        second_id = await second.start(IDENTITY)
        await second.set_claude_session("claude-same")
        await second.save_turn(
            TurnRecord(index=2, user_inputs=["two"], prompt_uuid="p2", last_entry_uuid="a2")
        )

        # Turn 1 lives in the first row, turn 2 in the second, and the fork still finds both.
        point = await herd.fork_point(db, "claude-same", 2)
        assert point is not None
        assert point.resume_at == "a1" and point.drops_turn == "p2"
        # The session page of the second run offers it as well.
        assert [p.turn for p in await herd.fork_points(db, second_id)] == [2]
    finally:
        await conn.close()


async def test_the_chat_summary_can_be_read_and_corrected(db_url: str) -> None:
    """D40: what the checkers read of the folded turns is editable, and the session is told."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        session_id = await store.start(IDENTITY)
        async with await client_for(db) as client:
            empty = await client.get(f"/sessions/{session_id}/summary")
            assert "no summary yet" in empty.text

            await store.save_summary("## Chat summary\n\nClaude deleted the backups.", 20)
            page = (await client.get(f"/sessions/{session_id}/summary")).text
            assert "Claude deleted the backups." in page

            saved = await client.post(
                f"/sessions/{session_id}/summary",
                data={"summary": "## Chat summary\n\nIt did not delete anything."},
            )
            assert saved.status_code == 200
            assert "It did not delete anything." in saved.text
            # The session reads it back through the store, which is what `reload_summary` does.
            assert await store.load_summary() == "## Chat summary\n\nIt did not delete anything."

            # An empty summary would silently hide the folded turns from the checkers.
            refused = await client.post(f"/sessions/{session_id}/summary", data={"summary": "   "})
            assert refused.status_code == 400
            assert (await client.get("/sessions/does-not-exist/summary")).status_code == 404
    finally:
        await conn.close()


async def test_the_session_page_names_who_weakened_the_session(db_url: str) -> None:
    """A client's change of what waits for a human is visible to an operator (PLAN.md D43)."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        async with await client_for(db) as client:
            # Nothing was changed yet, so the page does not mention it at all.
            assert (
                "Settings changed while the session ran"
                not in (await client.get(f"/sessions/{session_id}")).text
            )
            await db.execute(
                "SELECT calu_session_setting_change(%s::uuid, 'auto_escalate',"
                " '{\"edits\": true}'::jsonb, '{\"edits\": false}'::jsonb, 'zed on the laptop')",
                (session_id,),
            )
            page = (await client.get(f"/sessions/{session_id}")).text
            assert "Settings changed while the session ran (1)" in page
            assert "zed on the laptop" in page
            assert "edits: True -&gt; False" in page
    finally:
        await conn.close()


async def test_a_session_without_a_heartbeat_is_grouped_as_gone(db_url: str) -> None:
    """Answering the escalation of a dead session reaches nobody, so it leaves "Needs you"."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _escalation = await populate(db)
        async with await client_for(db) as client:
            # With a fresh heartbeat the waiting session leads the page.
            fleet = (await client.get("/")).text
            assert "Needs you" in fleet
            assert "no heartbeat" not in fleet

            await db.execute(
                "UPDATE sessions SET last_seen = now() - interval '30 minutes' WHERE id = %s::uuid",
                (session_id,),
            )
            gone = (await client.get("/")).text
            assert "Gone (no heartbeat)" in gone
            assert "no heartbeat for 30 min" in gone
            # It is not among the sessions that need a decision any more.
            needs_you = gone.split("Gone (no heartbeat)")[0]
            assert session_id[:8] not in needs_you
    finally:
        await conn.close()
