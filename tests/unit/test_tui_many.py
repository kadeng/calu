"""The full-screen client with many agents, several open decisions and many sessions.

What a run through `agent/records/notes/test-plan.md` section C corrected: steps that calu holds
back while a decision is open are shown as held; the row of agent tabs keeps the selected tab in
view; a decision is named by its escalation id; a session can be named; a client that lost its
session says what to do next. Scripted sessions and a stand-in for the database only.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("textual")

from calu import herd
from calu.protocol.messages import CLOSE_TOKEN_REVOKED, ENDED_WITH_SESSION
from calu.tui import render, scenarios
from calu.tui.capabilities import Capabilities
from calu.tui.fake import FakeAdmin, FakeSession
from calu.tui.model import MAIN, SessionModel, StepBlock, own_words
from calu.tui.screens.text import TextScreen
from tests.unit.test_tui_app import Running, running
from tests.unit.test_tui_model import feed, steps
from tests.unit.test_tui_render import plain, view

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, as Textual does."""
    return "asyncio"


async def command(run: Running, line: str) -> None:
    """Type a slash command and press Enter."""
    await run.type(line)
    await run.press("enter")
    await run.pilot.pause()


def tool_use(tool_use_id: str, agent: str | None, command_line: str) -> scenarios.Item:
    """Build the `tool.use` of a Bash call that no check has been reported for yet."""
    params = {
        "agent_id": agent,
        "tool_use_id": tool_use_id,
        "name": "Bash",
        "input": {"command": command_line},
    }
    return scenarios.Item("tool.use", params, 0.1)


def swarm(count: int = 3) -> tuple[SessionModel, list[dict[str, Any]]]:
    """Return a model with `count` running subagents `a1`..`aN`, and their `AgentInfo`s."""
    model = SessionModel()
    agents: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        agents.append(scenarios.agent_info(f"a{number}", f"Job number {number}", "Explore", False))
        feed(model, scenarios.subagent(agents, f"do job {number}"))
    return model, agents


def deploy(escalation_id: str, agent: str) -> list[scenarios.Item]:
    """Build an escalated deploy command of one subagent."""
    return scenarios.escalation(
        escalation_id,
        f"t-{escalation_id}",
        "Bash",
        {"command": "./deploy.sh"},
        "a deploy needs a person",
        "deploy",
        agent=agent,
    )


# ------------------------------------------------------------------ held steps


def test_a_step_without_a_check_is_held_while_another_steps_escalation_is_open() -> None:
    model, _ = swarm()
    feed(model, deploy("7", "a1"))
    feed(model, [tool_use("t2", "a2", "make test")])
    held = model.step("t2")
    assert held is not None and model.held(held)
    assert [step.tool_use_id for step in model.held_steps()] == ["t2"]
    assert model.activity_of(model.agents["a2"]) == "held by a decision"
    assert model.activity_of(model.agents["a1"]) == "waits for you"
    assert model.activity_of(model.agents[MAIN]) == "running 3 subagents"
    lines = plain(render.step_lines(held, model, view(100)))
    assert lines[0].startswith("  held      Bash   make test")
    assert lines[1] == "    held    calu approves no step while decision #7 is open"
    panel, _ = render.panel_lines(model, view(34), Capabilities())
    assert plain(panel)[0] == "NEEDS YOU (1)  1 step held"


def test_the_held_line_names_several_decisions_in_the_plural() -> None:
    model, _ = swarm()
    feed(model, deploy("7", "a1"))
    feed(model, deploy("8", "a3"))
    feed(model, [tool_use("t2", "a2", "make test")])
    held = model.step("t2")
    assert held is not None and model.held(held)
    lines = plain(render.step_lines(held, model, view(100)))
    assert lines[1] == "    held    calu approves no step while decisions #7, #8 are open"


def test_the_hold_ends_with_the_decision_and_the_held_lines_are_drawn_again() -> None:
    model, _ = swarm()
    feed(model, deploy("7", "a1"))
    feed(model, [tool_use("t2", "a2", "make test")])
    held = model.step("t2")
    assert held is not None
    change = model.apply("escalation.resolved", {"escalation_id": "7", "decision": "allow"}, 200.0)
    assert held.id in change.blocks_updated, "its badge changes from held to check…"
    assert not model.held(held) and model.held_steps() == []
    assert plain(render.step_lines(held, model, view(100)))[0].startswith("  check…    Bash")
    assert model.activity_of(model.agents["a2"]) == "checking Bash"


def test_a_step_held_after_its_approval_was_recorded_shows_held_until_the_session_says_so() -> None:
    """The client derived the hold from a missing check, so a step whose approval was
    recorded while another escalation came up showed ALLOW while calu held it back."""
    model, _ = swarm()
    feed(model, deploy("7", "a1"))
    feed(model, [tool_use("t2", "a2", "make test")])
    check = {
        "tool_use_id": "t2",
        "agent_id": "a2",
        "tool_name": "Bash",
        "route": "command",
        "decision": "ALLOW",
    }
    feed(model, [scenarios.Item("check.result", check, 0.1)])
    step = model.step("t2")
    assert step is not None and not model.held(step), "without the session's word: not held"
    held = {"tool_use_id": "t2", "agent_id": "a2", "tool_name": "Bash", "reason": "escalation 7"}
    change = model.apply("step.held", {**held, "held": True}, 1.0)
    assert step.id in change.blocks_updated and model.held(step)
    assert plain(render.step_lines(step, model, view(100)))[0].startswith("  held      Bash")
    assert model.activity_of(model.agents["a2"]) == "held by a decision"
    model.apply("step.held", {**held, "held": False, "reason": ""}, 2.0)
    assert not model.held(step)
    assert plain(render.step_lines(step, model, view(100)))[0].startswith("  ALLOW     Bash")


def test_a_question_holds_nothing_and_an_agent_call_with_its_subagent_is_not_held() -> None:
    model, _ = swarm(2)
    feed(model, scenarios.question("9", "q1", scenarios.RUNNER_QUESTIONS, agent="a1"))
    feed(model, [tool_use("t2", "a2", "make test")])
    step = model.step("t2")
    assert step is not None and not model.held(step) and model.holding() == []
    feed(model, deploy("7", "a1"))
    agent_calls = [block for block in steps(model) if block.tool == "Agent"]
    assert agent_calls and not any(model.held(call) for call in agent_calls)
    # An Agent call no check was reported for, whose subagent the session lists already.
    started = scenarios.agent_info("a9", "Late job", "Explore", False)
    call = {"tool_use_id": "a9", "name": "Agent", "input": {"description": "Late job"}}
    feed(model, [scenarios.Item("agents.update", {"agents": [started]}, 0.1)])
    feed(model, [scenarios.Item("tool.use", {**call, "agent_id": None}, 0.1)])
    unchecked = model.step("a9")
    assert unchecked is not None and not model.held(unchecked)
    first = plain(render.step_lines(unchecked, model, view(100)))[0]
    assert first.startswith("            Agent") and first.endswith("running"), first


def test_the_review_of_a_subagents_result_does_not_become_its_last_tool() -> None:
    model, _ = swarm(1)
    feed(model, scenarios.step("s1", "Grep", {"pattern": "x"}, "searched", agent="a1"))
    review = {
        "tool_use_id": "a1",
        "agent_id": "a1",
        "tool_name": "Agent",
        "route": "subagent",
        "decision": "PASS",
        "reason": "",
        "step_summary": "the result answers the task",
        "comment": "",
    }
    model.apply("check.result", review, 150.0)
    assert model.agents["a1"].last_tool == "Grep"


# ------------------------------------------------------------------ what the user said


def test_under_a_decided_step_stand_the_users_words_not_the_sessions_sentences() -> None:
    assert own_words("The user denied this step. Note from the user: not yet") == "not yet"
    assert own_words("The user denied this step.") == ""
    answered = (
        "The user answered your question: Hello\nThe AskUserQuestion call itself was not run; "
        "this is the answer. Carry on with it."
    )
    assert own_words(answered) == "Hello"
    assert own_words("go ahead") == "go ahead"
    model, _ = swarm(1)
    feed(model, deploy("7", "a1"))
    note = "The user denied this step. Note from the user: write the changelog first"
    model.apply("escalation.resolved", {"escalation_id": "7", "decision": "deny", "note": note})
    step = model.step("t-7")
    assert step is not None and step.note == "write the changelog first"


def test_an_answered_question_shows_the_answer_and_no_error_line() -> None:
    model = SessionModel()
    feed(model, scenarios.question("9", "q1", scenarios.RUNNER_QUESTIONS))
    told = "The user answered your question: pytest\nThe AskUserQuestion call itself was not run"
    model.apply("escalation.resolved", {"escalation_id": "9", "decision": "deny", "note": told})
    result = {"agent_id": None, "tool_use_id": "q1", "is_error": True, "content": told}
    model.apply("tool.result", result, 300.0)
    step = model.step("q1")
    assert isinstance(step, StepBlock)
    lines = plain(render.step_lines(step, model, view(100)))
    assert lines[0].lstrip().startswith("ANSWERED")
    assert lines[1] == "    answer  pytest"
    assert not any(line.lstrip().startswith("error") for line in lines)


def test_a_declined_question_is_not_shown_as_answered() -> None:
    model = SessionModel()
    feed(model, scenarios.question("9", "q1", scenarios.RUNNER_QUESTIONS))
    declined = {"escalation_id": "9", "decision": "deny", "note": "The user denied this step."}
    model.apply("escalation.resolved", declined)
    step = model.step("q1")
    assert isinstance(step, StepBlock) and step.waiting == "declined" and step.note == ""
    assert plain(render.step_lines(step, model, view(100)))[0].lstrip().startswith("DECLINED")


def test_the_cost_line_tells_a_session_without_a_cost_from_one_without_a_turn() -> None:
    model = SessionModel()
    lines, _ = render.panel_lines(model, view(34), Capabilities())
    assert any("no turn has ended yet" in line for line in plain(lines))
    model.apply("turn.result", {"session_id": "c1", "subtype": "success", "result": "done"}, 10.0)
    lines, _ = render.panel_lines(model, view(34), Capabilities())
    assert any(line.strip() == "cost     not reported" for line in plain(lines)), plain(lines)


def test_the_rendered_step_keeps_one_statement_per_line() -> None:
    source = "Tool call by the main agent: `Bash`\nWorking directory: `/w`\n```\nls  \n-l\n```"
    kept = render.keep_line_breaks(source).split("\n")
    assert kept[:2] == ["Tool call by the main agent: `Bash`  ", "Working directory: `/w`  "]
    assert kept[2:] == ["```", "ls  ", "-l", "```"], "nothing changes inside a fence"
    shown = plain(render.markdown_lines(render.keep_line_breaks(source), 80, view().palette))
    assert any(line.strip() == "Tool call by the main agent: Bash" for line in shown), shown


# ------------------------------------------------------------------ the row of tabs


def test_the_row_of_tabs_keeps_the_selected_tab_in_view_and_counts_what_is_cut() -> None:
    model, _ = swarm(10)
    feed(model, deploy("7", "a9"))
    line, spans = render.tabs_line(model, view(80, tab=MAIN))
    assert line.cell_len <= 80
    text = line.plain
    assert text.lstrip().startswith("main") and text.rstrip().endswith("▸!"), text
    marker = spans[-1]
    assert marker[2] == "a9", "a click on the marker selects the hidden tab that waits"
    line, spans = render.tabs_line(model, view(80, tab="a9"))
    assert "Job number 9 1!" in line.plain and line.plain.lstrip().startswith("◂")
    assert line.cell_len <= 80
    shown = [key for _, _, key in spans]
    assert "a9" in shown and spans[0][2] != MAIN, "the left marker leads to a hidden tab"
    assert "!" not in line.plain.split()[0], "no hidden tab on the left waits"


def test_a_row_of_tabs_that_fits_is_shown_whole() -> None:
    model, _ = swarm(2)
    line, spans = render.tabs_line(model, view(120, tab=MAIN), "alt+←/→ switches")
    assert "◂" not in line.plain and "▸" not in line.plain
    assert [key for _, _, key in spans] == [MAIN, "a1", "a2", "all"]
    assert line.plain.rstrip().endswith("alt+←/→ switches")


def test_the_window_of_tabs_grows_right_then_left() -> None:
    assert render.tabs_window([10, 10, 10, 10, 10], 0, 25) == (0, 2)
    assert render.tabs_window([10, 10, 10, 10, 10], 4, 25) == (3, 5)
    assert render.tabs_window([10, 10, 10, 10, 10], 2, 35) == (2, 5)
    assert render.tabs_window([10, 10, 10, 10, 10], 3, 35) == (2, 5)
    assert render.tabs_window([40], 0, 10) == (0, 1), "the selected tab is always shown"


# ------------------------------------------------------------------ decisions by id


async def test_the_card_and_the_panel_name_a_decision_by_its_escalation_id() -> None:
    async with running("showcase") as run:
        assert run.line("card").startswith("DECISION 1 of 2  ·  main  ·  Bash  ·  #e1")
        panel = run.line("panel-lines")
        assert ">#e1 main" in panel and " #e2 tests" in panel


async def test_a_number_that_is_no_open_id_decides_nothing() -> None:
    session = FakeSession()
    async with running(session=session) as run:
        await run.play(deploy("12", None) + deploy("13", None))  # type: ignore[arg-type]
        await command(run, "/allow 2")
        assert not session.sent("escalation.decide"), "2 is a position, not an id"
        assert "no open decision 2" in run.app.message and "#12, #13" in run.app.message
        await command(run, "/deny #13 not this one")
        (sent,) = session.sent("escalation.decide")
        assert (sent["escalation_id"], sent["decision"]) == ("13", "deny")
        assert sent["note"] == "not this one"
        await command(run, "/allow 12")
        assert session.sent("escalation.decide")[-1]["escalation_id"] == "12"


# ------------------------------------------------------------------ sessions and their names


def row(session_id: str, directory: str, name: str = "", waiting: int = 0) -> herd.SessionRow:
    """Build a row of a running session of this machine for the scripted database access."""
    import socket

    now = datetime.now(UTC)
    return herd.SessionRow(
        id=session_id,
        hostname=socket.gethostname(),
        os_user="ada",
        directory=directory,
        state="escalated" if waiting else "idle",
        state_since=now,
        started_at=now,
        ended_at=None,
        claude_session_id=None,
        open_escalations=waiting,
        address="127.0.0.1:7000",
        name=name,
    )


async def test_rename_stores_the_name_and_the_status_line_shows_it() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/rename payment refactoring")
        assert access.renamed == [(session.calu_session_id, "payment refactoring")]
        assert '"payment refactoring"' in run.line("status")
        assert "now called" in run.app.message
        await command(run, "/rename " + "x" * 81)
        assert "at most 80 characters" in run.app.message and len(access.renamed) == 1
        await command(run, "/rename")
        assert access.renamed[-1] == (session.calu_session_id, "")
        assert '"' not in run.line("status") and "removed" in run.app.message


async def test_rename_without_the_credential_says_so() -> None:
    session = FakeSession()
    async with running("simple_turn", session=session, admin=FakeAdmin(session, False)) as run:
        await command(run, "/rename x")
        assert "not renamed here" in run.app.message


async def test_running_lists_one_line_per_session_waiting_first_and_filters() -> None:
    session = FakeSession()
    access = FakeAdmin(session)
    access.running = [
        row("aaaa1111-0000", "/w/one"),
        row("bbbb2222-0000", "/w/two", name="payment refactoring", waiting=2),
        row("cccc3333-0000", "/w/" + "deep/" * 30 + "three"),
        row(str(session.calu_session_id), "/work/myproject", name="this one"),
    ]
    async with running("simple_turn", session=session, admin=access, size=(100, 30)) as run:
        await command(run, "/running")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        lines = page.text.splitlines()
        assert lines[0].startswith('▸ bbbb2222  escalated · 2 open  "payment refactoring"')
        rows = [line for line in lines if "  for 0 s" in line]
        assert len(rows) == 4 and all(len(line) <= 100 for line in rows), "one line per session"
        assert any(line.startswith("▸ cccc3333") and "…" in line for line in lines)
        assert '"this one"' in run.line("status"), "the list told the client its session's name"
        await run.press("escape")
        await command(run, "/running PAYMENT")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        marked = [line for line in page.text.splitlines() if line.startswith("▸ ")]
        assert len(marked) == 1 and "1 of 4 running sessions" in page.text
        await run.press("escape")
        await command(run, "/switch pay")
        assert run.app.switch_to == "bbbb2222-0000", "the beginning of a name is enough"


async def test_running_names_the_projects_directory_of_each_server() -> None:
    """D243, D244: `/new` and `/fork` start sessions only inside a server's directory."""
    from calu.tui.adminaccess import Served

    session = FakeSession()
    access = FakeAdmin(session)
    access.running = [row("aaaa1111-0000", "/home/ada/projects/one")]
    access.servers = [Served("box", "ada", "/home/ada/projects")]
    async with running("simple_turn", session=session, admin=access, size=(100, 30)) as run:
        await command(run, "/running")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "calu serve of ada@box serves /home/ada/projects" in page.text
        await run.press("escape")
        access.servers = []
        await command(run, "/running")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "calu serve of" not in page.text and "aaaa1111" in page.text


async def test_running_offers_rename_only_where_a_session_can_be_renamed() -> None:
    class ThroughRelay(FakeAdmin):
        """Lists sessions (the relay answers) but changes nothing (no admin credential)."""

        def problem(self) -> str | None:
            return "through a relay /running and /switch go through the relay"

        def sessions_problem(self) -> str | None:
            return None

    session = FakeSession()
    access = ThroughRelay(session)
    access.running = [row("aaaa1111-0000", "/w/one")]
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/running")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "/switch" in page.text and "/rename" not in page.text
    session = FakeSession()
    access = FakeAdmin(session)
    access.running = [row("aaaa1111-0000", "/w/one")]
    async with running("simple_turn", session=session, admin=access) as run:
        await command(run, "/running")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "/rename <name> names the session this client shows" in page.text


def test_a_session_row_is_cut_at_the_start_of_its_directory() -> None:
    from calu.tui.app import session_row_text

    text = session_row_text(row("0123456789abcdef", "/a/very/long/" + "x" * 80 + "/end"), 80)
    assert len(text) <= 80 and text.startswith("01234567  idle") and "/end  for 0 s" in text


async def test_sessions_lists_only_conversations_in_which_something_was_said() -> None:
    session = FakeSession()
    entry = {"state": "stopped", "directory": "/w", "started_at": "2026-11-18T10:00:00"}
    session.results["session.list"] = {
        "sessions": [
            {**entry, "calu_session_id": "s1", "claude_session_id": "c-empty"},
            {
                **entry,
                "calu_session_id": "s2",
                "claude_session_id": "c-real",
                "first_input": "fix the login test",
            },
        ]
    }
    async with running("simple_turn", session=session) as run:
        offered: list[list[tuple[Any, ...]]] = []
        run.app.search_commands = lambda entries, **more: offered.append(list(entries))  # type: ignore[method-assign]
        await command(run, "/sessions")
        await run.pilot.pause(0.2)
        assert [title for title, *_ in offered[0]] == ["2026-11-18 10:00  fix the login test"]
        assert session.sent("session.list") == [{"limit": 200}]


def test_a_stored_admin_login_without_a_password_is_used_instead_of_asking(tmp_path) -> None:
    """After `calu setup --no-password` the client of `calu start` asked for a password
    nobody had chosen: it was handed the login of `calu_admin` without its password."""
    from calu.credentials import CredentialStore
    from calu.db.target import Target
    from calu.setup import admin_login
    from calu.tui.adminaccess import STORED_ADMIN, StoreAdmin

    owner = Target.parse("host=127.0.0.1 port=1 dbname=calu user=calu_owner")
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(STORED_ADMIN, admin_login(owner, "generated").dump(), None)
    access = StoreAdmin(store, database=admin_login(owner, ""))
    assert not access.needs_password(), "the stored login is complete"
    elsewhere = Target.parse("host=127.0.0.1 port=2 dbname=calu user=calu_owner")
    other = StoreAdmin(store, database=admin_login(elsewhere, ""))
    assert other.needs_password(), "a login for another database is not used"
    empty = StoreAdmin(CredentialStore(tmp_path / "none.json"), database=admin_login(owner, ""))
    assert empty.needs_password()


# ------------------------------------------------------------------ a client without a session


async def test_a_client_that_lost_its_session_says_what_leads_to_another_one() -> None:
    session = FakeSession()
    async with running("simple_turn", session=session) as run:
        session.drop(1001)
        await run.pilot.pause(0.2)
        assert "detached: the session ended" in run.line("status")
        head = run.line("composer-head")
        assert "nothing can be sent" in head and "/new starts a session here" in head
        footer = run.line("footer")
        assert "/new session here" in footer and "/running other sessions" in footer
        assert "enter send" not in footer and "^d leave" in footer


async def test_a_client_whose_token_ended_with_the_stopped_row_follows_the_restarted_one() -> None:
    # `/stop`, then Enter on the card: the session runs a new row, and the session closes the
    # connection of a minted token of the old row. The client attaches to the new row, with a
    # token of its own (`calu attach` mints it, as after `/switch`), instead of staying
    # detached with "attach again" and refusing `/switch` to "the session this client shows".
    session = FakeSession()
    async with running("simple_turn", session=session) as run:
        await session.emit("session.state", {"state": "stopped"})
        session.calu_session_id = "calu-8e52"
        await session.emit("session.state", {"state": "starting"})
        await session.emit("session.state", {"state": "idle"})
        await run.pilot.pause(0.2)
        assert run.app.model.calu_session_id == "calu-8e52"
        session.drop(CLOSE_TOKEN_REVOKED, ENDED_WITH_SESSION)
        await run.pilot.pause(0.2)
        assert run.app.switch_to == "calu-8e52"


async def test_a_token_that_ended_with_the_only_row_known_leaves_the_client_detached() -> None:
    session = FakeSession()
    async with running("simple_turn", session=session) as run:
        session.drop(CLOSE_TOKEN_REVOKED, ENDED_WITH_SESSION)
        await run.pilot.pause(0.2)
        assert run.app.switch_to is None
        assert "detached" in run.line("status")


async def test_the_stored_history_is_listed_in_plain_columns() -> None:
    session = FakeSession()
    async with running("simple_turn", session=session) as run:
        await command(run, "/history")
        page = run.app.screen
        assert isinstance(page, TextScreen)
        assert "::" not in page.text


# ------------------------------------------------------------------ what a terminal would obey


def test_control_characters_are_shown_and_not_passed_on() -> None:
    from calu.controls import visible, visible_in

    esc = chr(27)
    assert visible(f"ok{esc}[2Jrm") == "ok␛[2Jrm"
    assert visible("harmless\rDANGER") == "harmless␍DANGER"
    assert visible("one\r\ntwo\tthree\n") == "one\ntwo\tthree\n", "line ends and tabs stay"
    assert visible("report_\u202egpj.exe") == "report_⟦RLO⟧gpj.exe"
    assert visible("\u009b31m") == "⟦U+009B⟧31m"
    plain_text = "مرحبا שלום 你好 á"
    assert visible(plain_text) is plain_text, "letters of any script are not touched"
    nested = {"a": ["x", {"b": f"y{esc}"}], "n": 3}
    assert visible_in(nested) == {"a": ["x", {"b": "y␛"}], "n": 3}
    clean = {"a": ["x"], "n": 3}
    assert visible_in(clean) is clean


async def test_a_command_with_control_characters_is_shown_as_it_is_stored() -> None:
    """The card must show the command that would run, not what a terminal makes of it."""
    session = FakeSession()
    command_line = "echo safe \x1b[2K\rrm -rf build \u202egnp.sh"
    async with running(session=session) as run:
        await run.play(
            scenarios.escalation("7", "t7", "Bash", {"command": command_line}, "look", "a command")
        )
        card = run.line("card")
        assert "echo safe ␛[2K␍rm -rf build ⟦RLO⟧gnp.sh" in card
        assert "\x1b" not in card and "\u202e" not in card and "\r" not in card


async def test_the_key_that_switches_agents_is_offered_only_with_subagents() -> None:
    async with running("simple_turn", size=(80, 24)) as run:
        assert "alt+←/→" not in run.line("footer") and "enter send" in run.line("footer")
    async with running("two_subagents") as run:
        assert "alt+←/→ agent" in run.line("footer")


# ------------------------------------------------------------------ undo


async def test_rewind_asks_for_a_branch_before_a_turn_and_refuses_other_numbers() -> None:
    """With a scripted session and a stand-in for the database; nothing is started."""
    session = FakeSession()
    access = FakeAdmin(session)
    async with running("simple_turn", session=session, admin=access) as run:
        ended = run.app.model.turn
        assert ended >= 1
        for wrong, said in (
            ("/rewind", "usage: /rewind <turn>"),
            ("/rewind one", "usage: /rewind <turn>"),
            ("/rewind 1", "/new starts an empty session"),
            (f"/rewind {ended + 2}", f"/rewind takes 2 to {ended + 1}"),
        ):
            await command(run, wrong)
            assert said in run.app.message, wrong
            assert not access.requests and run.app.switch_to is None
        await command(run, f"/rewind {ended + 1} try it another way")
        await run.pilot.pause()
        (wanted,) = access.requests
        assert (wanted.kind, wanted.fork_before_turn) == ("resume", ended + 1)
        assert wanted.claude_session_id == session.claude_session_id
        assert wanted.prompt == "try it another way"
        assert run.app.switch_to is not None, "the client switches to the branch"


async def test_enter_is_offered_once_in_the_side_panel() -> None:
    """In the panel Enter shows the entry under the cursor; the footer named "enter send" too."""
    async with running("two_subagents") as run:
        await run.press("f2")
        footer = run.line("footer")
        assert "enter" in footer and "enter send" not in footer, footer
        await run.press("escape")
        assert "enter send" in run.line("footer") or "esc esc interrupt" in run.line("footer")
