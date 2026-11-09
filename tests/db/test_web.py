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
from calu.web.app import NotLocal, check_host, create_app

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
            assert "1 waiting" in page.text
            assert session_id in page.text
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
