"""The web interface against a real database: the pages, resolving, minting, editing."""

import httpx
import psycopg
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
# tests instead of failing to collect them (`uv sync`, docs/TESTING.md).
WEB_EXTRA = "the web interface's packages are missing: uv sync"
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
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", follow_redirects=True
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
            # On the session's own machine neither the address nor a token is needed (D105),
            # and that is the form offered first, with the directory it has to be run in.
            assert "<pre>calu attach</pre>" in page
            assert "<pre>calu acp</pre>" in page
            assert "in /w on box" in page, "the directory and host the short form needs"
            # Each command has a Copy button carrying exactly the command (D139).
            assert 'data-copy="calu attach"' in page

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


def area(page: str, name: str) -> str:
    """Return the text of the editor with the given form name on the settings page.

    Args:
        page: The HTML of the page.
        name: The form field name, e.g. "rules:command-deny".

    Returns:
        What the text area holds, HTML-unescaped.
    """
    import html

    body = page.split(f'name="{name}"', 1)[1].split(">", 1)[1].split("</textarea>", 1)[0]
    return html.unescape(body)


async def test_one_settings_page_holds_the_values_the_instructions_and_the_rules(
    db_url: str,
) -> None:
    """The owner's ask: checker prompts and allow and deny lists are settings too, on one page."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.add_rule_set(db, "recommended")  # D179: the lists start empty
        async with await client_for(db) as client:
            page = (await client.get("/settings")).text
            # Configuration values, each generated from the model with the right control.
            assert 'name="claude.model"' in page and 'list="claude-models"' in page
            assert 'name="claude.effort"' in page and ">xhigh<" in page
            assert 'name="claude.use_bedrock"' in page and 'type="checkbox"' in page
            assert 'name="command_checker.model"' in page
            # A checker's model: a list by provider that fills a free text field, and the page
            # that documents every other model string (the owner's ask).
            for offered in (
                "anthropic:claude-sonnet-5-5",
                "bedrock:anthropic.claude-opus-5-5",
                "fireworks:accounts/fireworks/models/",
                "mistral:mistral-small-2603",
                "claude:haiku",
            ):
                assert f'<option value="{offered}' in page
            assert 'data-model-for="f-command_checker-model"' in page
            assert 'href="https://pydantic.dev/docs/ai/models/overview/"' in page
            assert 'name="changeset_checker.effort"' in page
            assert 'name="claude.sandbox.deny_read"' in page  # a list, one per line
            assert 'name="clock.max_backward_s"' in page
            # Each control carries what the setting does, from the field's own docstring (D47),
            # with code spans rendered as code.
            assert "Reasoning effort of the Claude session" in page
            assert "<code>CLAUDE_CODE_USE_BEDROCK=1</code>" in page
            # Every checker's instructions and every rule list is a text area of the same page.
            for name in ("command", "edit", "changeset", "subagent-result", "honesty", "narrator"):
                assert f'name="prompt:{name}"' in page
            for name in admin.RULE_LISTS:
                assert f'name="rules:{name}"' in page
            assert "^(sudo|su|doas)( |$)" in area(page, "rules:command-deny")
            assert "<table" not in page

            # The old addresses lead to the place on the new page.
            old = await client.get("/checkers/command", follow_redirects=False)
            assert old.headers["location"].endswith("/settings#checker-command")
            old = await client.get("/rules?project=demo", follow_redirects=False)
            assert old.headers["location"].endswith("/settings?project=demo#rules-command")
    finally:
        await conn.close()


async def test_a_save_writes_only_what_changed(db_url: str) -> None:
    """One form for everything: values, a prompt and a rule list are stored by one save."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        before = next(r for r in await admin.checkers(db) if r.name == "command")
        edit_before = next(r for r in await admin.checkers(db) if r.name == "edit")
        async with await client_for(db) as client:
            rules = "# Never deploy from a session.\n^deploy( |$)\n\n^sudo( |$)\n"
            saved = await client.post(
                "/settings",
                data={
                    "claude.model": "sonnet",
                    "claude.effort": "high",
                    "claude.use_bedrock": "on",
                    "command_checker.effort": "off",
                    "command_checker.model": "",
                    "escalation_timeout_s": "3600",
                    "prompt:command": "# New instructions\r\n\r\nBe brief.\r\n",
                    "prompt:edit": await admin.instructions(db, "edit"),
                    "rules:command-deny": rules,
                },
            )
            assert saved.status_code == 200 and "saved=7" in str(saved.url)
            assert "7 changes saved" in saved.text
            values = await admin.settings(db)
            assert values["claude"]["model"] == "sonnet"
            assert values["claude"]["effort"] == "high"
            assert values["claude"]["use_bedrock"] is True
            assert values["command_checker"]["effort"] == "off"
            assert values["escalation_timeout_s"] == pytest.approx(3600.0)
            # An unchanged value is not written, so the audit log stays meaningful.
            assert "model" not in values.get("command_checker", {})
            # A switch the form did not contain is not switched off by its absence.
            assert "check_subagent_results" not in values

            after = next(r for r in await admin.checkers(db) if r.name == "command")
            assert after.version == before.version + 1  # a change bumps the version
            assert await admin.instructions(db, "command") == "# New instructions\n\nBe brief."
            edit_after = next(r for r in await admin.checkers(db) if r.name == "edit")
            assert edit_after.version == edit_before.version, "unchanged instructions stay"

            rows = await admin.rules(db, rule_list="command-deny")
            assert [(r.pattern, r.comment) for r in rows if r.enabled] == [
                ("^deploy( |$)", "Never deploy from a session."),
                ("^sudo( |$)", ""),
            ]
            assert "# Never deploy from a session.\n^deploy( |$)" in area(
                saved.text, "rules:command-deny"
            )

            # A switch the form contains and leaves off is switched off.
            await client.post("/settings", data={"_present": "claude.use_bedrock"})
            assert (await admin.settings(db))["claude"]["use_bedrock"] is False

            # "Reset" removes the stored value, so the default applies again.
            await client.post("/settings", data={"claude.effort": "", "_reset": "claude.effort"})
            assert "effort" not in (await admin.settings(db))["claude"]
    finally:
        await conn.close()


async def test_a_refused_value_is_reported_at_its_field_and_the_rest_is_saved(
    db_url: str,
) -> None:
    """A typo must not cost the other edits, and what was typed stays in the field."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.replace_rule_list(db, "command-deny", "^sudo( |$)\n")
        async with await client_for(db) as client:
            refused = await client.post(
                "/settings",
                data={
                    "claude.effort": "enormous",
                    "claude.model": "sonnet",
                    "rules:command-deny": "^deploy( |$)\n^unclosed(\n",
                    "prompt:edit": "   ",
                    "tool_routes": "{not json",
                },
            )
            assert refused.status_code == 400
            assert "4 values refused" in refused.text and "1 saved" in refused.text
            assert refused.text.count("Not saved:") == 4
            # What was typed is still there, and the page opens at the first refused field.
            assert "^unclosed(" in area(refused.text, "rules:command-deny")
            assert 'data-open="checker-edit"' in refused.text
            values = await admin.settings(db)
            assert values["claude"] == {"model": "sonnet"}
            rows = await admin.rules(db, rule_list="command-deny")
            assert [r.pattern for r in rows if r.enabled] == ["^sudo( |$)"]
            assert (await admin.instructions(db, "edit")).strip()
    finally:
        await conn.close()


async def test_a_project_scope_overrides_values_and_adds_rules(db_url: str) -> None:
    """A project shows what it inherits, stores only its own values and cannot edit prompts."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.add_project(db, "demo")
        await admin.set_setting(db, "claude.effort", "high")
        await admin.replace_rule_list(db, "command-allow", "^ls( |$)\n")
        command = await admin.instructions(db, "command")
        async with await client_for(db) as client:
            page = (await client.get("/settings?project=demo")).text
            # The global value is what the project inherits, and what "Reset" returns to.
            assert '<option value="high" selected>' in page
            assert "1 global pattern(s) apply as well" in page
            assert "global only" in page  # the instructions are shared by every project

            await client.post(
                "/settings?project=demo",
                data={
                    "claude.effort": "low",
                    "rules:command-allow": "^make( |$)\n",
                    "prompt:command": "A project may not change this.",
                },
            )
            assert (await admin.settings(db, "demo"))["claude"]["effort"] == "low"
            assert (await admin.settings(db))["claude"]["effort"] == "high"
            own = await admin.rules(db, "demo", "command-allow")
            assert [r.pattern for r in own if r.scope == "demo"] == ["^make( |$)"]
            assert any(r.scope == "global" for r in own), "the global list is untouched"
            assert await admin.instructions(db, "command") == command

            assert (await client.get("/settings?project=nobody")).status_code == 404
    finally:
        await conn.close()


async def test_a_starter_rule_set_is_added_from_the_rules_pane(db_url: str) -> None:
    """D143: a set is offered beside the lists, added once, and then part of the text."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        async with await client_for(Db(conn)) as client:
            page = (await client.get("/settings")).text
            assert 'name="add_set" value="publishing"' in page
            assert "push( |$)" not in area(page, "rules:command-deny")

            first = await client.post("/settings", data={"add_set": "publishing"})
            assert first.status_code == 200
            assert "rule(s) added from the set" in first.text
            assert "^git( -[Cc] \\S+)* push( |$)" in area(first.text, "rules:command-deny")

            again = await client.post("/settings", data={"add_set": "publishing"})
            assert "0 rule(s) added from the set" in again.text, "a second press adds nothing"
            assert (await client.post("/settings", data={"add_set": "all"})).status_code == 404
    finally:
        await conn.close()


async def test_an_extra_checker_is_switched_on_and_given_a_model_on_the_page(db_url: str) -> None:
    """The honesty checker's row is edited on the Code honesty pane, beside `honesty.mode`."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        async with await client_for(db) as client:
            await client.post(
                "/settings",
                data={
                    "_present": "checker:honesty:enabled",
                    "checker:honesty:enabled": "on",
                    "checker:honesty:model": "claude:haiku",
                    "checker:honesty:request_limit": "7",
                    "honesty.mode": "track",
                    "honesty.rules": "# Our standard\n\nNumPy docstrings.",
                },
            )
            row = next(r for r in await admin.checkers(db) if r.name == "honesty")
            assert (row.enabled, row.model, row.request_limit) == (True, "claude:haiku", 7)
            values = await admin.settings(db)
            assert values["honesty"] == {
                "mode": "track",
                "rules": "# Our standard\n\nNumPy docstrings.\n",
            }
            # Emptying the honesty rules returns to the text calu ships.
            await client.post("/settings", data={"honesty.rules": ""})
            assert "rules" not in (await admin.settings(db))["honesty"]
            page = (await client.get("/settings")).text
            assert "What dishonesty is" in area(page, "honesty.rules")
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
            # One button per fork point, labelled with what the fork keeps and drops (D219).
            assert "new session that keeps turn 1 and drops turn 2" in page
            assert "new session that keeps turns 1 to 2 and drops turn 3" in page
            assert 'name="fork_before_turn" value="2"' in page
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


async def test_the_pages_say_when_an_answer_can_reach_nobody(db_url: str) -> None:
    """An operator should know before deciding, not after (D56)."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, escalation = await populate(db)
        async with await client_for(db) as client:
            listing = (await client.get("/escalations")).text
            detail = (await client.get(f"/escalations/{escalation}")).text
            assert "process is gone" not in listing and "process is gone" not in detail

            await db.execute(
                "UPDATE sessions SET last_seen = now() - interval '10 minutes' WHERE id = %s::uuid",
                (session_id,),
            )
            listing = (await client.get("/escalations")).text
            detail = (await client.get(f"/escalations/{escalation}")).text
            assert "process is gone" in listing
            assert "will not run" in listing
            assert "process is gone" in detail
    finally:
        await conn.close()


async def test_the_triage_queue_marks_an_entry_nobody_waits_for(db_url: str) -> None:
    """The queue is answered fast, so this has to be in the data the card is built from."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _escalation = await populate(db)
        async with await client_for(db) as client:
            waiting = (await client.get("/api/escalations")).json()["escalations"]
            assert waiting and waiting[0]["unanswerable"] is False

            await db.execute(
                "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
                (session_id,),
            )
            waiting = (await client.get("/api/escalations")).json()["escalations"]
            assert waiting[0]["unanswerable"] is True
    finally:
        await conn.close()


async def test_the_checkers_page_shows_the_cases_to_fix_the_instructions_against(
    db_url: str,
) -> None:
    """The counts and the rows behind them, on the page where the instructions are edited."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        _session_id, escalation = await populate(db)
        async with await client_for(db) as client:
            page = (await client.get("/settings")).text
            # The fixture's checker escalated once, so there is a record but no miss.
            assert "Decided" in page and "1 run(s)" in page
            assert "Approved, then denied by a human" not in page

            # Make it a miss and the page shows the case, with both one-click destinations.
            # The fixture's checker run sits on the first step and the escalation on the
            # second, so the run that approved *this* step is added here.
            await db.execute(
                "INSERT INTO checker_runs"
                " (step_id, checker_name, checker_version, model, decision, reason)"
                " SELECT e.step_id, 'command', 1, 'test:model', 'pass', 'looked routine'"
                " FROM escalations e WHERE e.id = %s",
                (escalation,),
            )
            await db.execute(
                "UPDATE escalations SET state = 'denied', note = 'not that one',"
                " resolved_at = now() WHERE id = %s",
                (escalation,),
            )
            page = (await client.get("/settings")).text
            # The miss is on the command checker's pane, beside the instructions to correct.
            pane = page.split('id="checker-command"')[1].split("</section>")[0]
            assert "Approved, then denied by a human" in pane and "1 (a miss)" in pane
            assert "looked routine" in pane and "not that one" in pane
            assert f"/escalations/{escalation}" in pane
            assert 'name="prompt:command"' in pane
            # The old address of the checkers page leads to the overview.
            old = await client.get("/checkers", follow_redirects=False)
            assert old.headers["location"].endswith("/settings#checkers")
    finally:
        await conn.close()


async def test_the_checkers_page_shows_what_they_cost(db_url: str) -> None:
    """D63: the token totals and the cache share are readable without a model call."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)

        async def check(seq: int, cached: int) -> None:
            step = await store.add_step(
                StepRow(seq=seq, tool_name="Bash", tool_input={"command": "x"})
            )
            await store.add_checker_run(
                step,
                CheckerRun(
                    checker_name="command",
                    checker_version=1,
                    model="test:model",
                    decision="pass",
                    input_tokens=20_000,
                    output_tokens=50,
                    cache_read_tokens=cached,
                ),
            )

        async with await client_for(db) as client:
            # A checker that has never run says nothing about tokens rather than "0".
            assert ">Tokens<" not in (await client.get("/settings")).text

            await check(1, 19_000)
            await check(2, 19_000)
            warm = (await client.get("/settings")).text
            # The same page says how many steps reached a checker at all (D64).
            assert "2 step(s): 2 went to a checker, 0 (0%) did not" in warm
            assert "40k in / 100 out" in warm
            assert "95% of the input from cache" in warm
            assert "st-fact is-warn" not in warm, "a working cache must not be flagged"

            # With the prefix missing from the cache the share is marked, because the checks
            # then cost several times what they should.
            await db.execute("UPDATE checker_runs SET cache_read_tokens = 0")
            cold = (await client.get("/settings")).text
            assert "0% of the input from cache" in cold
            assert "st-fact is-warn" in cold
    finally:
        await conn.close()


async def test_another_site_cannot_answer_an_escalation_or_read_the_interface(
    db_url: str,
) -> None:
    """D136: a page anywhere could submit "allow" for escalation 1, 2, 3 ... while this ran.

    The interface has no login, because it serves loopback only -- but a browser on this
    machine sends requests to 127.0.0.1 for any site it has open, and a form POST goes
    cross-site without asking. This was demonstrated against a running `calu web` before the
    fix: a request with `Origin: https://evil.example` resolved escalation 1.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        _session_id, escalation = await populate(db)
        async with await client_for(db) as client:
            forged = {"decision": "allow", "note": "forged"}
            for headers in (
                {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                {"Origin": "https://evil.example"},  # an older browser without Sec-Fetch-*
                {"Sec-Fetch-Site": "same-site"},  # another port on localhost is "same-site"
                {"Origin": "null"},  # a sandboxed iframe or a file:// page
            ):
                refused = await client.post(
                    f"/escalations/{escalation}/resolve", data=forged, headers=headers
                )
                assert refused.status_code == 403, headers
            still = await herd.escalation(db, escalation)
            assert still is not None and still.state == "open", "nothing was decided"

            # A page whose domain re-resolves to 127.0.0.1 (DNS rebinding) sends its own name.
            rebound = await client.get("/settings", headers={"Host": "evil.example:8800"})
            assert rebound.status_code == 403

            # The interface's own pages still work: same origin, and plain clients without the
            # browser headers.
            own = await client.post(
                f"/escalations/{escalation}/resolve",
                data={"decision": "deny", "note": "from the page itself"},
                headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
            )
            assert own.status_code == 200
            decided = await herd.escalation(db, escalation)
            assert decided is not None and decided.state == "denied"
    finally:
        await conn.close()


def test_the_guard_closes_a_websocket_from_another_site() -> None:
    """The live view's WebSocket can decide steps too, and browsers do not stop it cross-site."""
    from calu.web.app import LocalOnly

    guard = LocalOnly(app=None)

    def scope(kind: str, **headers: str) -> dict:
        """An ASGI scope with these headers."""
        return {
            "type": kind,
            "method": "GET",
            "path": "/ws/sessions/x",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }

    assert guard.refusal(scope("websocket", host="127.0.0.1:8800", origin="https://evil.example"))
    assert (
        guard.refusal(scope("websocket", host="127.0.0.1:8800", origin="http://127.0.0.1:8800"))
        is None
    )
    assert (
        guard.refusal(scope("websocket", host="localhost:8800", origin="http://localhost:8800"))
        is None
    )
    assert guard.refusal(scope("http", host="[::1]:8800")) is None, "IPv6 loopback is loopback"
    assert guard.refusal(scope("http", host="192.168.1.5:8800")), "not this machine's loopback"


async def test_without_the_token_nothing_is_served_and_nothing_decided(db_url: str) -> None:
    """D161: `LocalOnly` stops other websites, not other local processes -- Claude's own
    `curl -X POST .../resolve` included. `calu web` therefore prints a token, and only a
    browser that logged in with it (`?token=` once) gets through."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await populate(db)
        app = create_app(db, git_url_template="", token="s3cret")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as plain:
            refused = await plain.get("/")
            assert refused.status_code == 401
            assert "printed" in refused.text, "says how to get in"
            assert (
                await plain.post("/escalations/1/resolve", data={"decision": "allow"})
            ).status_code == 401
            assert (await plain.get("/?token=wrong")).status_code == 401
            async with plain.stream("GET", "/static/calu.css") as css:
                assert css.status_code == 401

            entered = await plain.get("/?token=s3cret&project=x")
            assert entered.status_code == 200
            assert 'content="/?project=x"' in entered.text, "the token leaves the URL"
            cookie = entered.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
            assert "s3cret" not in cookie, "the cookie is not the token"
            # The client now holds the cookie, as the browser does.
            assert (await plain.get("/")).status_code == 200
            async with plain.stream("GET", "/static/login.js") as script:
                assert script.status_code == 200, "the login page can load its script"
        # Nothing was decided by the refused POST.
        cur = await conn.execute("SELECT state FROM escalations ORDER BY id")
        assert {row[0] for row in await cur.fetchall()} == {"open"}
    finally:
        await conn.close()


async def test_a_captured_cookie_cannot_decide_an_escalation(db_url: str) -> None:
    """wa0: a cookie is sent to every port of its host, so a server the session starts sees it.

    The session's development server, opened in the browser that is logged in to `calu web`,
    received `Cookie: calu_web_token=<the token>`, and `curl -b` with it resolved the session's
    own escalation. Now the cookie names a login and is enough to read; resolving, minting a
    client token and saving settings need the key as well, which only the pages of the
    interface's own origin can read.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, escalation = await populate(db)
        app = create_app(db, token="s3cret", host_name="calu.localhost")
        transport = httpx.ASGITransport(app=app)
        base = "http://calu.localhost:8800"
        async with httpx.AsyncClient(transport=transport, base_url=base) as browser:
            page = await browser.get("/?token=s3cret")
            cookie = page.headers["set-cookie"].split(";")[0].split("=", 1)[1]
            key = page.text.split('name="calu-key" content="')[1].split('"')[0]

            # What the other server can do with what it saw: the cookie, nothing else.
            async with httpx.AsyncClient(
                transport=transport, base_url=base, cookies={"calu_web_session": cookie}
            ) as thief:
                allow = {"decision": "allow", "note": "self-approved"}
                for attempt in (
                    await thief.post(f"/escalations/{escalation}/resolve", data=allow),
                    await thief.post(f"/api/escalations/{escalation}/resolve", json=allow),
                    await thief.post(f"/sessions/{session_id}/token", data={"label": "mine"}),
                    await thief.post("/settings", data={"add_set": "recommended"}),
                    await thief.post(f"/sessions/{session_id}/summary", data={"summary": "x"}),
                ):
                    assert attempt.status_code == 401, attempt.request.url
                still = await herd.escalation(db, escalation)
                assert still is not None and still.state == "open", "nothing was decided"
                assert await herd.list_tokens(db, session_id) == [], "no client token minted"

            # The interface's own page sends the key with its form, and that decides.
            own = await browser.post(
                f"/escalations/{escalation}/resolve",
                data={"decision": "deny", "note": "from the page", "_key": key},
                follow_redirects=True,
            )
            assert own.status_code == 200
            decided = await herd.escalation(db, escalation)
            assert decided is not None and decided.state == "denied"

            # Under the bare loopback names the interface is not served at all, so the browser
            # never holds a cookie of it for `127.0.0.1` or `localhost`.
            for name in ("127.0.0.1:8800", "localhost:8800"):
                bare = await browser.get("/", headers={"Host": name})
                assert bare.status_code == 403
                assert "http://calu.localhost:8800/" in bare.text
    finally:
        await conn.close()


async def test_a_minted_token_is_in_the_answer_to_the_post_and_nowhere_else(db_url: str) -> None:
    """wa0: the token waited in the server for the next GET of the session page.

    A GET needs only the cookie, and a client token decides escalations of its session, so a
    captured cookie that polled the page could have picked the token up.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        session_id, _ = await populate(db)
        app = create_app(db, git_url_template="")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            minted = await client.post(f"/sessions/{session_id}/token", data={"label": "zed"})
            assert minted.status_code == 200, "the page itself, not a redirect to a GET"
            assert "shown once" in minted.text
            assert minted.headers["cache-control"] == "no-store"
            assert "shown once" not in (await client.get(f"/sessions/{session_id}")).text
            missing = await client.post("/sessions/no-such-session/token", data={})
            assert missing.status_code == 404
    finally:
        await conn.close()


async def test_what_a_request_carries_is_never_put_into_a_page_unescaped(db_url: str) -> None:
    """wa2: the 404 and 400 pages were built with an f-string from the path and the form.

    `GET /sessions/<img src=x onerror=...>` answered with that tag in the page. The script ran
    in the interface's origin, with the operator's login, and could allow every escalation.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        _session_id, escalation = await populate(db)
        attack = "<img src=x onerror=alert(1)>"
        quoted = "%3Cimg%20src=x%20onerror=alert(1)%3E"
        unknown = "00000000-0000-4000-8000-000000000000"
        async with await client_for(db) as client:
            # A session id is a UUID: anything else is no route at all, and nothing is echoed.
            for path in ("", "/live", "/summary"):
                no_route = await client.get(f"/sessions/{quoted}{path}")
                assert no_route.status_code == 404
                assert "<img" not in no_route.text and "onerror" not in no_route.text
            no_route = await client.post(f"/sessions/{quoted}/token", data={})
            assert no_route.status_code == 404 and "onerror" not in no_route.text
            answers = [
                await client.post(f"/escalations/{escalation}/resolve", data={"decision": attack}),
                await client.get(f"/settings?project={quoted}"),
            ]
            for answer in answers:
                assert answer.status_code in (400, 404), answer.request.url
                assert "<img" not in answer.text, answer.request.url
                assert "&lt;img src=x onerror=alert(1)&gt;" in answer.text, answer.request.url
                assert "default-src 'self'" in answer.headers["content-security-policy"]
            # A session that does not exist gets the same page, with its id on it.
            answers.insert(0, await client.get(f"/sessions/{unknown}"))
            assert answers[0].status_code == 404 and unknown in answers[0].text
            assert (await client.post(f"/sessions/{unknown}/token", data={})).status_code == 404
            # The error pages are pages of the interface: the way back is on them.
            assert 'href="/"' in answers[0].text
            still = await herd.escalation(db, escalation)
            assert still is not None and still.state == "open"
    finally:
        await conn.close()


async def test_a_database_that_is_gone_is_said_so_and_found_again(db_url: str) -> None:
    """wa3: `calu web` held one connection for its whole life.

    After a database restart every page answered 500 until `calu web` was restarted, and the
    triage page kept showing its last queue. Now the answer says what is wrong (503, as a page
    or as JSON), and the next request opens a new connection.
    """
    from calu.web.database import ReopeningDb

    conn = await connect(db_url)
    killer = await connect(db_url)
    reachable = True

    async def reopen() -> psycopg.AsyncConnection:
        """Open the next connection, unless the test says the database is down."""
        if not reachable:
            raise psycopg.OperationalError("connection refused (the test's database is down)")
        return await connect(db_url)

    db = ReopeningDb(conn, reopen, probe_after=0.0)
    try:
        await setup(conn, db_url, "test:model")
        await populate(db)
        app = create_app(db, git_url_template="")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1",
        ) as client:
            assert (await client.get("/api/escalations")).status_code == 200

            # The database goes away: the server's connection is closed under it.
            reachable = False
            await killer.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            polled = await client.get("/api/escalations")
            assert polled.status_code == 503
            assert polled.json()["error"] == "database unreachable"
            assert (await client.get("/api/sessions")).status_code == 503
            page = await client.get("/")
            assert page.status_code == 503
            assert "cannot reach the database" in page.text
            assert "location.reload" in page.text, "a watch window recovers by itself"
            answered = await client.post(
                "/api/escalations/1/resolve", json={"decision": "allow", "note": ""}
            )
            assert answered.status_code == 503, "not answered, and it says so"

            # The database is back: no restart of `calu web`, the next request just works.
            reachable = True
            again = await client.get("/api/escalations")
            assert again.status_code == 200
            assert len(again.json()["escalations"]) == 1, "the escalation is still open"
            assert (await client.get("/")).status_code == 200
    finally:
        await db.close()
        await killer.close()


async def test_a_non_ascii_token_is_refused_not_a_server_error(db_url: str) -> None:
    """D168: `secrets.compare_digest` raised on non-ASCII text, which gave 500, not 401."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        app = create_app(Db(conn), git_url_template="", token="s3cret")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/?token=%C3%A4bc")).status_code == 401
    finally:
        await conn.close()


async def test_only_images_inside_the_sessions_directory_are_served(db_url: str, tmp_path) -> None:
    """The thumbnails of the live view; writes a few small files into `tmp_path`."""
    import dataclasses
    import socket

    project = tmp_path / "project"
    project.mkdir()
    (project / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (project / "notes.txt").write_text("plain")
    (tmp_path / "secret.png").write_bytes(b"outside")
    (project / "link.png").symlink_to(tmp_path / "secret.png")
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        here = dataclasses.replace(IDENTITY, hostname=socket.gethostname(), directory=str(project))
        mine = await DatabaseSessionStore(db).start(here)
        elsewhere = await DatabaseSessionStore(db).start(
            dataclasses.replace(here, hostname="another-machine")
        )
        async with await client_for(db) as client:
            served = await client.get(f"/sessions/{mine}/file", params={"path": "shot.png"})
            assert served.status_code == 200 and served.headers["content-type"] == "image/png"
            absolute = await client.get(
                f"/sessions/{mine}/file", params={"path": str(project / "shot.png")}
            )
            assert absolute.status_code == 200
            for refused in (
                "notes.txt",
                "link.png",
                "../secret.png",
                str(tmp_path / "secret.png"),
                "",
            ):
                answer = await client.get(f"/sessions/{mine}/file", params={"path": refused})
                assert answer.status_code == 404, refused
            other = await client.get(f"/sessions/{elsewhere}/file", params={"path": "shot.png"})
            assert other.status_code == 404, "a session of another machine has its files there"
    finally:
        await conn.close()
