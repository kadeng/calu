"""Session traces in a running session: the whole file, the reload, the guards, the settings."""

import re
import subprocess
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

import calu
from calu import admin, preflight
from calu.changes import SnapshotStore
from calu.checkers.model import Step
from calu.checkers.tools import CodeTools, ReadRefused
from calu.config import Config, ContextConfig, TracesConfig
from calu.honesty import Baseline, HonestyState
from calu.ide.database import ROLE_LABELS
from calu.protocol.messages import (
    ChatSendParams,
    EscalationRequest,
    Log,
    ToolResult,
    TurnResult,
)
from calu.session import Session
from calu.testing import _last_prompt, excerpt_of, fake_models
from tests.unit.helpers import default_rules, default_settings, make_session

pytestmark = pytest.mark.anyio

SCRIPT = [
    [
        {"thinking": "The user wants the tests run."},
        {"text": "I will run the tests. Mail jane.roe@example.org if they fail."},
        {
            "tool": "Bash",
            "input": {"command": "pytest -q", "description": "Run the tests"},
            "result": "2 passed\nAKIAIOSFODNN7EXAMPLE",
        },
        {
            "agent": {
                "description": "look around",
                "actions": [
                    {"text": "Looking."},
                    {"tool": "Read", "input": {"file_path": "a.py"}, "result": "x = 1"},
                ],
            }
        },
        {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"},
        {"text": "All done."},
    ]
]
"""One turn with text, thinking, a checked command, a subagent and an escalation."""


def traced(**changes: object) -> Config:
    """Return a configuration with session traces on."""
    return Config(traces=TracesConfig(enabled=True, **changes))  # type: ignore[arg-type]


def trace_files(workspace: Path) -> list[Path]:
    """Return the trace files of a workspace, oldest first."""
    return sorted((workspace / "agent" / "traces").rglob("session_*.md"))


async def run_script(session: Session, rec, text: str = "go") -> None:
    """Start the session, send one message, decide the escalation, wait for the turn's end."""
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text=text))
        (request,) = await rec.wait_for(EscalationRequest)
        await session.decide_escalation(request.escalation_id, "allow", "fine by me", "tui")
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_a_scripted_session_is_traced_as_one_redacted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole file of a narrated session, compared character by character."""
    monkeypatch.setattr("calu.session._os_user", lambda: "kai")
    monkeypatch.setenv("SHOP_DEPLOY_TOKEN", "deploy-token-0123456789")
    workspace = tmp_path / "shop"
    workspace.mkdir()
    session, rec, _ = make_session(workspace, SCRIPT, traced())
    await run_script(
        session,
        rec,
        "Run the tests. My key is sk-proj-abc123DEF456ghi789JKL0 and the deploy token is "
        "deploy-token-0123456789.",
    )

    (path,) = trace_files(workspace)
    assert re.fullmatch(
        r"agent/traces/\d{4}-\d\d-\d\d/kai/session_001\.md", str(path.relative_to(workspace))
    )
    text = re.sub(r"(?m)^- Started: .*$", "- Started: <time>", path.read_text())
    text = re.sub(r"fake-tool-[0-9a-f]+-\d+", "fake-tool-<id>", text)
    assert text == (
        "# calu session trace\n"
        "\n"
        "- calu session: memory-session\n"
        "- Claude session: not known yet (a later line names it)\n"
        "- Project: shop\n"
        "- Started: <time>\n"
        "- User: kai\n"
        "- Claude model: opus\n"
        f"- calu version: {calu.__version__}\n"
        "- Content: narrated (Claude's text and thinking retold in indirect speech by the "
        "narrator; no images)\n"
        "- Redaction: the secret values calu knows and common shapes of keys and tokens are "
        "replaced; secrets and personal data are replaced by the redaction agent "
        "(`fake-redactor`)\n"
        "\n"
        "A block that could not be redacted is replaced by a line that says it was withheld. "
        "Redaction by a model is not a guarantee: read this file before you publish it.\n"
        "\n"
        "---\n"
        "\n"
        "_Claude session: `fake-session`_\n"
        "\n"
        "## Turn 1\n"
        "\n"
        "### User\n"
        "\n"
        "> Run the tests. My key is [redacted: api key] and the deploy token is "
        "[redacted: token].\n"
        "\n"
        "### Claude, narrated\n"
        "\n"
        "> Narrated: The user wants the tests run. I will run the tests. Mail "
        "[redacted: e-mail address] if they fail.\n"
        "\n"
        "### Tool call 1: Bash: PASS\n"
        "\n"
        "Summary:\n"
        "> Fake summary of Tool call by the main agent: `Bash`.\n"
        "\n"
        "### Tool call 2: Agent (starts subagent 1)\n"
        "\n"
        "### Task of subagent 1: PASS\n"
        "\n"
        "Summary:\n"
        "> Task for subagent fake-tool-<id>: Fake review of a subagent task.\n"
        "\n"
        "#### Subagent 1: text and thinking, narrated\n"
        "\n"
        "> Narrated: Looking.\n"
        "\n"
        "#### Subagent 1: tool call 3: Read: PASS\n"
        "\n"
        "Summary:\n"
        "> Read a.py\n"
        "\n"
        "#### Subagent 1: result handed back by tool call 2 (Agent): PASS\n"
        "\n"
        "Summary:\n"
        "> Fake review of a subagent result.\n"
        "\n"
        "### Tool call 4: Bash: ESCALATE\n"
        "\n"
        "Summary:\n"
        "> Fake summary of Tool call by the main agent: `Bash`.\n"
        "\n"
        "Reason:\n"
        "> Escalated by the rule-based fake checker (calu:escalate).\n"
        "\n"
        "### Escalation 6: tool call 4 (Bash) waits for a person\n"
        "\n"
        "Reason:\n"
        "> Escalated by the rule-based fake checker (calu:escalate).\n"
        "\n"
        "### Escalation 6: allowed\n"
        "\n"
        "Decided by: `client:tui`\n"
        "\n"
        "Note:\n"
        "> fine by me\n"
        "\n"
        "### Claude, narrated\n"
        "\n"
        "> Narrated: All done.\n"
        "\n"
        "### End of turn 1\n"
        "\n"
        "Result: success\n"
        "\n"
        "_The session ended (stopped)._\n"
        "\n"
    )
    assert any(
        isinstance(event, Log) and event.message.startswith("session trace: agent/traces/")
        for event in rec.events
    )


async def test_the_plain_form_holds_claudes_text_and_thinking(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, SCRIPT, traced(content="plain", redact=False))
    await run_script(session, rec)
    text = trace_files(tmp_path)[0].read_text()
    assert "- Content: plain (Claude's text and the summaries of its thinking as written" in text
    assert "(`traces.redact` is off): personal data is not removed" in text
    assert (
        "### Claude's thinking\n\n> The user wants the tests run.\n\n"
        "### Claude\n\n> I will run the tests. Mail jane.roe@example.org if they fail.\n"
    ) in text, "the thinking block once, before the text that follows it"
    assert text.count("The user wants the tests run.") == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in text and "2 passed" not in text, "no tool result"
    assert "pytest -q" not in text, "no command"
    assert "#### Subagent 1: text\n\n> Looking.\n" in text


async def run_turn(session: Session, rec) -> None:
    """Start the session, send one message, wait for the turn's end, stop."""
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_a_rewritten_thinking_block_is_traced_once_in_its_final_form(tmp_path: Path):
    """D241: the summary streams and is rewritten; the file holds its last version once."""
    script = [
        [
            {"thinking": "First draft of the plan", "message": "m1", "stream": True},
            {"thinking": "Run the tests, then report.", "message": "m1"},
            {"agent": {"description": "look", "actions": [{"thinking": "Sub plan"}]}},
            {"text": "Done."},
            {"thinking": "Last thought, at the end of the turn.", "stream": True},
        ]
    ]
    session, rec, _ = make_session(tmp_path, script, traced(content="plain", redact=False))
    await run_turn(session, rec)
    text = trace_files(tmp_path)[0].read_text()
    assert "First draft" not in text, "the rewritten version is not written"
    assert text.count("### Claude's thinking\n\n> Run the tests, then report.\n") == 1
    assert "#### Subagent 1: thinking\n\n> Sub plan\n" in text
    assert text.index("Run the tests, then report.") < text.index("> Done.")
    # Thinking with nothing after it in the turn is written before the turn's end, once.
    last = "### Claude's thinking\n\n> Last thought, at the end of the turn.\n"
    assert text.count(last) == 1 and text.index(last) < text.index("### End of turn 1")


async def test_thinking_is_redacted_like_every_other_block(tmp_path: Path):
    script = [
        [
            {"thinking": "Use sk-proj-abc123DEF456ghi789JKL0 and ask jane.roe@example.org."},
            {"text": "Done."},
        ]
    ]
    session, rec, _ = make_session(tmp_path, script, traced(content="plain"))
    await run_turn(session, rec)
    text = trace_files(tmp_path)[0].read_text()
    assert (
        "### Claude's thinking\n\n> Use [redacted: api key] and ask [redacted: e-mail address].\n"
    ) in text
    assert "sk-proj-" not in text and "jane.roe" not in text


async def test_narrated_content_without_a_narrator_is_plain_and_the_header_says_why(
    tmp_path: Path,
):
    config = traced().model_copy(update={"context": ContextConfig(narrate=False)})
    session, rec, _ = make_session(tmp_path, SCRIPT, config)
    await run_script(session, rec)
    text = trace_files(tmp_path)[0].read_text()
    assert "- Content: plain (" in text and "`context.narrate` is off" in text
    assert "### Claude\n\n> All done.\n" in text


async def test_tracing_is_off_by_default(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, SCRIPT)
    await run_script(session, rec)
    assert session.traces is None and not (tmp_path / "agent").exists()


async def test_a_failing_redaction_agent_withholds_blocks_and_the_session_goes_on(
    tmp_path: Path,
):
    from pydantic_ai.models.function import FunctionModel

    from calu.testing import fake_models
    from calu.traces import WITHHELD

    def failing(messages, info):
        raise RuntimeError("no such model")

    models = fake_models()
    models["trace-redactor"] = FunctionModel(failing)
    session, rec, _ = make_session(tmp_path, SCRIPT, traced(), checker_models=models)
    await run_script(session, rec, "the user's own words")
    assert rec.of(ToolResult)[-1].content == "deployed", "the session ran to its end"
    text = trace_files(tmp_path)[0].read_text()
    assert f"### User\n\n{WITHHELD}\n" in text
    assert "the user's own words" not in text and "pytest -q" not in text
    assert "### End of turn 1\n\nResult: success\n" in text


async def test_a_resumed_session_gets_a_new_file_that_names_the_one_it_continues(
    tmp_path: Path,
):
    session, rec, _ = make_session(tmp_path, [[{"text": "one"}], [{"text": "two"}]], traced())
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(TurnResult)
        await session.stop()
        await session.start(tg, resume_id="fake-session")
        await session.send_chat(ChatSendParams(text="second"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    first, second = trace_files(tmp_path)
    assert first.name == "session_001.md" and second.name == "session_002.md"
    header = second.read_text().split("---")[0]
    assert "- Claude session: `fake-session`\n" in header
    assert (
        "- Continues: this session is a continuation of the conversation recorded in "
        f"`{first.relative_to(tmp_path).as_posix()}`\n"
    ) in header
    assert "> first" in first.read_text() and "> first" not in second.read_text()


async def test_a_reload_starts_and_ends_the_trace(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path, [[{"text": "one"}], [{"text": "two"}], [{"text": "three"}]]
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(TurnResult)
        assert session.traces is None and not trace_files(tmp_path)

        # The session is idle: its file starts with its next block, not with the setting.
        session.apply_settings(default_settings(tmp_path, traced()))
        assert session.traces is None and not trace_files(tmp_path)
        await session.send_chat(ChatSendParams(text="second"))
        await rec.wait_for(TurnResult, 2)
        writer = session.traces
        assert writer is not None
        session.apply_settings(default_settings(tmp_path, traced()))
        assert session.traces is writer, "unchanged settings keep the file"

        session.apply_settings(default_settings(tmp_path, Config()))
        assert session.traces is None
        await session.send_chat(ChatSendParams(text="third"))
        await rec.wait_for(TurnResult, 3)
        await session.stop()
    (path,) = trace_files(tmp_path)
    text = path.read_text()
    assert "- Note: this trace starts in the middle of a session, because" in text
    assert "> second" in text and "> first" not in text and "> third" not in text
    assert text.endswith("_The trace ends here: the `traces` settings changed._\n\n")


async def test_changed_trace_settings_start_a_new_file_that_names_the_old_one(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, [[{"text": "one"}], [{"text": "two"}]], traced())
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(TurnResult)
        session.apply_settings(default_settings(tmp_path, traced(content="plain")))
        await session.send_chat(ChatSendParams(text="second"))
        await rec.wait_for(TurnResult, 2)
        await session.stop()
    first, second = trace_files(tmp_path)
    assert "- Content: narrated" in first.read_text()
    text = second.read_text()
    assert "- Content: plain" in text and "- Claude session: `fake-session`\n" in text
    assert f"the steps before are in `{first.relative_to(tmp_path).as_posix()}`" in text
    assert "### Claude\n\n> two\n" in text


async def test_a_changed_trace_setting_starts_no_file_in_an_idle_session(tmp_path: Path):
    """One `calu config set traces...` added a header-only file for every running session."""
    session, rec, _ = make_session(tmp_path, [[{"text": "one"}], [{"text": "two"}]], traced())
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="first"))
        await rec.wait_for(TurnResult)
        writer = session.traces
        assert writer is not None
        await writer.created.wait()

        # Limits of the writer's work change nothing of what a block holds: the file stays.
        session.apply_settings(
            default_settings(tmp_path, traced(queue_blocks=50, redactor_timeout_s=5.0))
        )
        assert session.traces is writer and not session._trace_stale

        # A setting that changes the output: nothing happens while the session is idle ...
        session.apply_settings(default_settings(tmp_path, traced(content="plain")))
        assert session.traces is writer and len(trace_files(tmp_path)) == 1
        # ... and changed back before the next block, the file is simply kept.
        session.apply_settings(default_settings(tmp_path, traced()))
        assert session.traces is writer and not session._trace_stale

        session.apply_settings(default_settings(tmp_path, traced(turn_numbers=False)))
        assert session.traces is writer and len(trace_files(tmp_path)) == 1
        await session.send_chat(ChatSendParams(text="second"))
        await rec.wait_for(TurnResult, 2)
        assert session.traces is not writer, "the next block went to a new file"
        await session.stop()
    first, second = trace_files(tmp_path)
    assert "> first" in first.read_text() and "> second" not in first.read_text()
    assert first.read_text().endswith("_The trace ends here: the `traces` settings changed._\n\n")
    assert "> second" in second.read_text()
    assert f"the steps before are in `{first.relative_to(tmp_path).as_posix()}`" in (
        second.read_text()
    )


async def test_a_session_without_a_turn_leaves_no_trace_file(tmp_path: Path):
    session, _rec, _ = make_session(tmp_path, [], traced())
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        writer = session.traces
        assert writer is not None
        await writer.created.wait()
        assert len(trace_files(tmp_path)) == 1
        # Tracing switched off and on again in the idle session: no file per change.
        session.apply_settings(default_settings(tmp_path, Config()))
        session.apply_settings(default_settings(tmp_path, traced(content="plain")))
        await session.stop()
    assert not trace_files(tmp_path)


# ---------------------------------------------------------------------------- the guards


async def test_a_write_into_the_trace_directory_waits_for_a_person(tmp_path: Path):
    session, rec, _ = make_session(
        tmp_path,
        [
            [
                {
                    "tool": "Write",
                    "input": {
                        "file_path": str(tmp_path / "agent/traces/2026-11-18/kai/session_001.md"),
                        "content": "rewritten",
                    },
                    "result": "written",
                }
            ]
        ],
        traced(),
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        (request,) = await rec.wait_for(EscalationRequest)
        assert "built-in (calu's session traces)" in request.reason
        assert not rec.of(ToolResult), "the call waits"
        await session.decide_escalation(request.escalation_id, "deny", None)
        await rec.wait_for(TurnResult)
        await session.stop()


def test_the_rule_covers_edits_and_commands_only_while_traces_are_written(tmp_path: Path):
    rules = default_rules(tmp_path)

    def asks(tool: str, tool_input: dict[str, str]) -> str:
        step = Step(tool_name=tool, tool_input=tool_input, cwd=str(tmp_path))
        return rules.evaluate(step).reason

    inside = {"file_path": "agent/traces/2026-11-18/kai/session_001.md", "content": "x"}
    command = {"command": "echo more >> agent/traces/2026-11-18/kai/session_001.md"}
    assert "session traces" not in asks("Write", inside)
    assert "session traces" not in asks("Bash", command)

    rules.trace_directory = tmp_path / "agent/traces"
    assert "built-in (calu's session traces) `^agent/traces(/|$)`" in asks("Write", inside)
    assert "session traces" in asks("Edit", {**inside, "old_string": "a", "new_string": "b"})
    assert "session traces" in asks("Bash", command)
    assert "session traces" not in asks("Write", {"file_path": "agent/notes.md", "content": "x"})
    assert "session traces" not in asks("Read", {"file_path": inside["file_path"]}), (
        "reading a trace is an ordinary read"
    )


def test_the_trace_directory_is_recognised_through_a_linked_directory(tmp_path: Path):
    rules = default_rules(tmp_path)
    rules.trace_directory = tmp_path / "agent/traces"
    (tmp_path / "agent/traces").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "agent")
    assert rules.in_trace_directory(tmp_path / "agent/traces")
    assert rules.in_trace_directory(tmp_path / "alias/traces/x.md")
    assert rules.write_denied(tmp_path / "alias/traces/x.md")
    assert not rules.in_trace_directory(tmp_path / "agent/traces-old/x.md")


def test_the_session_tells_the_guards_where_the_traces_are(tmp_path: Path):
    on, _, _ = make_session(tmp_path, [], traced(directory="docs/chats"))
    assert on.rules.trace_directory == tmp_path / "docs/chats"
    assert on.honesty.excluded == ("docs/chats",)
    assert on.write_sets.excluded == ("docs/chats",)
    off, _, _ = make_session(tmp_path, [])
    assert off.rules.trace_directory is None
    assert off.honesty.excluded == () and off.write_sets.excluded == ()
    off.apply_settings(default_settings(tmp_path, traced()))
    assert off.rules.trace_directory == tmp_path / "agent/traces"
    assert off.honesty.excluded == ("agent/traces",)


async def test_the_write_set_of_a_command_leaves_the_trace_directory_out(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    snapshots = SnapshotStore(
        workspace=project, root=tmp_path / "snapshots", exclude=("agent/traces",)
    )
    await snapshots.prepare()
    before = await snapshots.take()
    trace = project / "agent/traces/2026-11-18/kai"
    trace.mkdir(parents=True)
    (trace / "session_001.md").write_text("# calu session trace\n")
    (project / "agent/notes.md").write_text("mine\n")
    (project / "a.py").write_text("x = 1\n")
    after = await snapshots.take()
    change = await snapshots.changes(before, after)
    assert sorted(entry.path for entry in change.changes) == ["a.py", "agent/notes.md"]

    # A directory that is excluded later leaves the snapshots from then on.
    snapshots.exclude = ("agent/traces", "agent")
    (project / "agent/notes.md").write_text("changed\n")
    (project / "a.py").write_text("x = 2\n")
    later = await snapshots.changes(after, await snapshots.take())
    assert [entry.path for entry in later.changes] == ["a.py"]


def test_the_honesty_state_never_lists_a_trace_file(tmp_path: Path):
    def git(*arguments: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *arguments],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip()

    git("init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    state = HonestyState(tmp_path)
    state.baseline = Baseline(git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}"), "user")
    trace = tmp_path / "agent/traces/2026-11-18/kai/session_001.md"
    trace.parent.mkdir(parents=True)
    trace.write_text("# calu session trace\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    state.watch(trace)

    assert set(state.unreviewed().paths) == {
        "agent/traces/2026-11-18/kai/session_001.md",
        "b.py",
    }
    state.excluded = ("agent/traces",)
    assert state.unreviewed().paths == ("b.py",)
    assert state.left_out("agent/traces") and not state.left_out("agent/traces-old/x.md")
    plan = state.review_plan(max_files=10)
    assert [file.path for file in plan.files] == ["b.py"]


def test_a_checkers_code_tools_do_not_read_the_traces(tmp_path: Path):
    trace = tmp_path / "agent/traces/2026-11-18/kai/session_001.md"
    trace.parent.mkdir(parents=True)
    trace.write_text("# calu session trace\n")
    rules = default_rules(tmp_path)
    tools = CodeTools(workspace=tmp_path, rules=rules)
    relative = trace.relative_to(tmp_path).as_posix()
    assert tools.read_lines(relative) == ["# calu session trace"]
    rules.trace_directory = tmp_path / "agent/traces"
    with pytest.raises(ReadRefused, match="session traces"):
        tools.read_lines(relative)


# ---------------------------------------------------------------------------- the settings


def test_the_trace_directory_must_stay_inside_the_project():
    assert TracesConfig().directory == "agent/traces" and not TracesConfig().enabled
    assert TracesConfig(directory="./docs//chats/").directory == "docs/chats"
    for refused in ("/var/log/calu", "../outside", "a/../../b", "~/traces", "", " ", ".", "C:/x"):
        with pytest.raises(ValidationError, match=r"traces\.directory is a path inside"):
            TracesConfig(directory=refused)
    with pytest.raises(ValidationError):
        TracesConfig(content="verbatim")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TracesConfig(queue_blocks=0)


def test_one_model_for_every_role_covers_the_redaction_agent():
    assert Config().with_model("test:one").traces.redactor_model == "test:one"
    assert "traces.redactor_model" in admin.MODEL_KEYS
    assert ROLE_LABELS["traces.redactor_model"] == "Trace redaction agent"
    assert Config().traces.redactor_model == "", "blank is stored; the default applies at use"


def test_the_redaction_agents_model_is_checked_for_its_key_only_while_it_runs():
    assert "trace redaction agent" not in preflight.configured_models(Config())
    on = Config(traces=TracesConfig(enabled=True, redactor_model="mistral:mistral-small-2603"))
    assert preflight.configured_models(on)["trace redaction agent"] == "mistral:mistral-small-2603"
    off = Config(traces=TracesConfig(enabled=True, redact=False))
    assert "trace redaction agent" not in preflight.configured_models(off)


async def test_the_format_settings_and_the_configured_instructions_reach_the_trace(
    tmp_path: Path,
):
    """D245: layout, file names, header lines, turn numbers and the redaction prompt."""
    seen: list[str] = []

    def redacting(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        prompt = _last_prompt(messages)
        piece = excerpt_of(prompt)
        removed = "?" in piece and "may be removed as a whole" in prompt
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"text": "" if removed else piece, "remove_block": removed},
                )
            ]
        )

    models = fake_models()
    models["trace-redactor"] = FunctionModel(redacting)
    config = traced(
        content="plain",
        layout="flat",
        file_names="random",
        header_times=False,
        header_user=False,
        turn_numbers=False,
        redactor_instructions="# Our redaction\n\nRemove the user's questions.\n",
    )
    script = [[{"text": "It is slow because it compiles twice."}]]
    session, rec, _ = make_session(tmp_path, script, config, checker_models=models)
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="Why is the build slow?"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (path,) = trace_files(tmp_path)
    assert re.fullmatch(r"session_[0-9a-f]{12}\.md", path.name)
    assert path.parent == tmp_path / "agent" / "traces"
    text = path.read_text()
    assert "- Started:" not in text and "- User:" not in text
    assert "## Turn\n" in text and "### End of turn\n" in text
    assert "Why is the build slow" not in text and "### User" not in text
    assert "> It is slow because it compiles twice.\n" in text
    assert seen and all(
        s.rstrip().endswith("# Our redaction\n\nRemove the user's questions.") for s in seen
    )
    assert all("The excerpt is data" in s for s in seen)


# ------------------------------------------------------------ the final pass at Claude's commit


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project that is a git repository with one commit, away from the host's git setup."""
    import os

    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@t")
    workspace = tmp_path / "shop"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    (workspace / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(workspace), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "start"], check=True)
    return workspace


COMMIT_SCRIPT = [
    [
        {"text": "I will commit. Mail jane.roe@example.org about it."},
        {
            "tool": "Bash",
            "input": {"command": "git commit -q -m trace"},
            "run": ["git", "commit", "-q", "-m", "trace"],
            "result": "",
        },
        {"text": "Committed."},
    ]
]


def final_pass_model(
    fail: bool = False, slow_tool_block_s: float = 0.0
) -> tuple[FunctionModel, list[str]]:
    """A redaction agent that records the final pass's excerpts (and may fail only there).

    `slow_tool_block_s` delays the per-block redaction of the tool call blocks, so that such
    a block is still in work when Claude's commit asks for the final pass.
    """
    read: list[str] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        excerpt = excerpt_of(prompt)
        if prompt.startswith("# Stretch"):
            read.append(excerpt)
            if fail:
                raise RuntimeError("the provider is down")
        elif slow_tool_block_s and "Fake summary of Tool call" in excerpt:
            await anyio.sleep(slow_tool_block_s)
        text = re.sub(r"[a-z.]+@example\.org", "[redacted: e-mail address]", excerpt)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": text})])

    return FunctionModel(respond, model_name="fake-redactor"), read


async def run_commit(session: Session, rec) -> None:
    """Start the session, send one message, wait for the turn's end, stop."""
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="commit please"))
        await rec.wait_for(TurnResult, timeout=20)
        await session.stop()


def git_out(workspace: Path, *args: str) -> str:
    """Return git's output in the project."""
    return subprocess.run(
        ["git", "-C", str(workspace), *args], check=True, capture_output=True, text=True
    ).stdout


async def test_claudes_commit_takes_the_trace_along_after_the_final_pass(git_repo: Path):
    from calu.traces.finalize import finalized_end

    models = fake_models()
    models["trace-redactor"], read = final_pass_model()
    session, rec, _ = make_session(git_repo, COMMIT_SCRIPT, traced(), checker_models=models)
    await run_commit(session, rec)

    (path,) = trace_files(git_repo)
    relative = path.relative_to(git_repo).as_posix()
    committed = git_out(git_repo, "show", f"HEAD:{relative}")
    assert git_out(git_repo, "log", "--format=%s") == "trace\nstart\n"
    assert len(read) == 1 and read[0].startswith("# calu session trace")
    assert finalized_end(committed) == len(committed), "the commit ends with the marker"
    assert "jane.roe" not in committed and "### Tool call 1: Bash" in committed
    assert "### End of turn 1" not in committed
    # The session's later blocks follow the committed part on disk; nothing was lost.
    text = path.read_text()
    assert text.startswith(committed) and "### End of turn 1" in text
    assert any("finalized for the commit" in log.message for log in rec.of(Log))


async def test_the_final_pass_waits_for_the_blocks_still_being_redacted(git_repo: Path):
    """CI 2026-11-23: the block of the commit's own tool call was still being redacted when
    the final pass read the file, so the commit lacked it. The pass now waits for the blocks
    queued before it (`TraceWriter.settle`)."""
    models = fake_models()
    models["trace-redactor"], _ = final_pass_model(slow_tool_block_s=0.5)
    session, rec, _ = make_session(git_repo, COMMIT_SCRIPT, traced(), checker_models=models)
    await run_commit(session, rec)

    (path,) = trace_files(git_repo)
    committed = git_out(git_repo, "show", f"HEAD:{path.relative_to(git_repo).as_posix()}")
    assert "### Tool call 1: Bash" in committed and "Fake summary of Tool call" in committed
    assert committed.index("### Tool call 1: Bash") < committed.index("_[calu: the final")


async def test_claude_is_told_that_the_staged_trace_belongs_in_its_commit(git_repo: Path):
    """Dogfooding 2026-11-20: Claude saw a trace file in its commit that it had not staged,
    took it for a mistake and undid the commit; the approval now says why it is there."""
    models = fake_models()
    models["trace-redactor"], _ = final_pass_model()
    session, _rec, _ = make_session(git_repo, [], traced(), checker_models=models)
    commit = Step(
        tool_name="Bash",
        tool_input={"command": "git commit -q -m work"},
        tool_use_id="c1",
        cwd=str(git_repo),
    )
    results = []
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        results.append(await session.pre_tool_use(commit))
        await session.stop()
    (result,) = results
    (path,) = trace_files(git_repo)
    relative = path.relative_to(git_repo).as_posix()
    assert result.verdict.name in ("PASS", "ALLOW")
    assert f"`{relative}`" in result.context and "intended" in result.context
    assert relative in git_out(git_repo, "diff", "--cached", "--name-only")


async def test_a_failing_final_pass_refuses_claudes_commit(git_repo: Path):
    models = fake_models()
    models["trace-redactor"], read = final_pass_model(fail=True)
    session, rec, _ = make_session(git_repo, COMMIT_SCRIPT, traced(), checker_models=models)
    await run_commit(session, rec)

    assert read, "the final pass was tried"
    assert git_out(git_repo, "log", "--format=%s") == "start\n", "no commit was made"
    result = next(r for r in rec.of(ToolResult) if r.is_error)
    assert "final pass failed" in result.content and "provider is down" in result.content
    assert git_out(git_repo, "diff", "--cached", "--name-only") == ""
    assert any("the commit was refused" in log.message for log in rec.of(Log))


async def test_without_the_final_pass_claudes_commit_leaves_the_traces_alone(git_repo: Path):
    models = fake_models()
    models["trace-redactor"], read = final_pass_model()
    config = traced(final_redaction=False)
    session, rec, _ = make_session(git_repo, COMMIT_SCRIPT, config, checker_models=models)
    await run_commit(session, rec)
    assert read == []
    assert "agent/traces" not in git_out(git_repo, "show", "--name-only", "--format=", "HEAD")
