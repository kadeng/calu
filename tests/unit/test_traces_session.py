"""Session traces in a running session: the whole file, the reload, the guards, the settings."""

import re
import subprocess
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

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
        "### Tool call 1: Bash\n"
        "\n"
        "`command`:\n"
        "```bash\n"
        "pytest -q\n"
        "```\n"
        "\n"
        "`description`:\n"
        "```\n"
        "Run the tests\n"
        "```\n"
        "\n"
        "### Check of tool call 1 (Bash): PASS\n"
        "\n"
        "Route: `command`\n"
        "\n"
        "Summary of the step:\n"
        "> Fake summary of Tool call by the main agent: `Bash`.\n"
        "\n"
        "### Result of tool call 1 (Bash)\n"
        "\n"
        "```\n"
        "2 passed\n"
        "[redacted: aws access key]\n"
        "```\n"
        "\n"
        "### Tool call 2: Agent (starts subagent 1)\n"
        "\n"
        "`description`:\n"
        "```\n"
        "look around\n"
        "```\n"
        "\n"
        "`prompt`:\n"
        "```\n"
        "\n"
        "```\n"
        "\n"
        "`subagent_type`:\n"
        "```\n"
        "general-purpose\n"
        "```\n"
        "\n"
        "`run_in_background`: `false`\n"
        "\n"
        "#### Subagent 1: text and thinking, narrated\n"
        "\n"
        "> Narrated: Looking.\n"
        "\n"
        "#### Subagent 1: tool call 3: Read\n"
        "\n"
        "`file_path`:\n"
        "```\n"
        "a.py\n"
        "```\n"
        "\n"
        "#### Subagent 1: check of tool call 3 (Read): PASS\n"
        "\n"
        "Route: `allow`\n"
        "\n"
        "Summary of the step:\n"
        "> Read a.py\n"
        "\n"
        "#### Subagent 1: result of tool call 3 (Read)\n"
        "\n"
        "```\n"
        "x = 1\n"
        "```\n"
        "\n"
        "#### Subagent 1: check of the result handed back by tool call 2 (Agent): PASS\n"
        "\n"
        "Route: `subagent`\n"
        "\n"
        "Summary of the step:\n"
        "> Fake review of a subagent result.\n"
        "\n"
        "### Result of tool call 2 (Agent)\n"
        "\n"
        "```\n"
        "Subagent finished.\n"
        "```\n"
        "\n"
        "### Tool call 4: Bash\n"
        "\n"
        "`command`:\n"
        "```bash\n"
        "deploy # calu:escalate\n"
        "```\n"
        "\n"
        "### Check of tool call 4 (Bash): ESCALATE\n"
        "\n"
        "Route: `command`\n"
        "\n"
        "Reason:\n"
        "> Escalated by the rule-based fake checker (calu:escalate).\n"
        "\n"
        "Summary of the step:\n"
        "> Fake summary of Tool call by the main agent: `Bash`.\n"
        "\n"
        "### Escalation 5: tool call 4 (Bash) waits for a person\n"
        "\n"
        "Reason:\n"
        "> Escalated by the rule-based fake checker (calu:escalate).\n"
        "\n"
        "### Escalation 5: allowed\n"
        "\n"
        "Decided by: `client:tui`\n"
        "\n"
        "Note:\n"
        "> fine by me\n"
        "\n"
        "### Result of tool call 4 (Bash)\n"
        "\n"
        "```\n"
        "deployed\n"
        "```\n"
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


async def test_the_plain_form_holds_claudes_text_and_no_thinking(tmp_path: Path):
    session, rec, _ = make_session(tmp_path, SCRIPT, traced(content="plain", redact=False))
    await run_script(session, rec)
    text = trace_files(tmp_path)[0].read_text()
    assert "- Content: plain (Claude's text as written" in text
    assert "(`traces.redact` is off): personal data is not removed" in text
    assert "### Claude\n\n> I will run the tests. Mail jane.roe@example.org if they fail.\n" in text
    assert "The user wants the tests run" not in text, "thinking is left out"
    assert "[redacted: aws access key]" in text, "the deterministic stage always runs"
    assert "#### Subagent 1: text\n\n> Looking.\n" in text


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

        session.apply_settings(default_settings(tmp_path, traced(max_result_chars=300)))
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
