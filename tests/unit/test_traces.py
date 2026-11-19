"""Session traces: the blocks, the file's path, the two redaction stages and the writer (D232)."""

import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.checkers.model import Decision
from calu.checkers.policy import Route
from calu.config import TracesConfig
from calu.context.narrator import ClaudeOutput
from calu.defaults.instructions import (
    default_redaction_instructions,
    redaction_instructions,
    redaction_system_part,
)
from calu.protocol.messages import (
    AgentInfo as SubagentInfo,
)
from calu.protocol.messages import (
    AgentsUpdate,
    AssistantDelta,
    AssistantText,
    AssistantThinking,
    ChatImage,
    CheckResult,
    EscalationRequest,
    EscalationResolved,
    Event,
    SessionState,
    ToolResult,
    ToolUse,
    TurnResult,
    UserInput,
)
from calu.testing import _last_prompt, excerpt_of, fake_redactor
from calu.traces.redact import (
    CHUNK_CHARS,
    WITHHELD,
    RedactionFailed,
    Redactor,
    Secrets,
    checked,
    chunks,
    kind_of_variable,
    redact_deterministic,
    redact_shapes,
    request_head,
)
from calu.traces.render import (
    LAYOUTS,
    NOT_NARRATED,
    Header,
    TraceFileError,
    TraceRenderer,
    create_trace_file,
    cut,
    find_trace_of,
    format_block,
    quote,
    render_header,
    render_input,
    session_directory,
    session_marker,
    user_component,
)
from calu.traces.writer import WITHHELD_AT_END, TraceWriter

pytestmark = pytest.mark.anyio

STARTED = datetime(2026, 11, 18, 10, 22, 3, tzinfo=UTC)


def header(**changes: object) -> Header:
    """Return a header with fixed values, so that a rendered file can be compared as a whole."""
    values: dict[str, object] = {
        "calu_session": "41",
        "claude_session": "0b0e7c1e-1111-4222-8333-444455556666",
        "project": "shop",
        "started": STARTED,
        "user": "kai",
        "claude_model": "opus",
        "calu_version": "1.2.3",
        "content": "plain",
        "redactor": "fake-redactor",
    }
    values.update(changes)
    return Header(**values)  # type: ignore[arg-type]


def rendered(renderer: TraceRenderer, events: list[Event], resolved_by: str = "") -> str:
    """Render events and join their blocks as the file would hold them, unredacted."""
    text = ""
    for event in events:
        for block in renderer.render(event, resolved_by):
            text += format_block(block.heading, block.body)
    return text


# ---------------------------------------------------------------------------- small helpers


def test_quote_prefixes_every_line_so_that_text_cannot_pass_for_a_heading():
    assert quote("## Turn 9\n\nsecond") == "> ## Turn 9\n>\n> second"
    assert quote("") == ">"


def test_cut_says_how_much_was_left_out():
    assert cut("abc", 3) == "abc"
    assert cut("abcdef", 2) == "ab\n[... 4 more characters left out of the trace]"


def test_tool_input_is_fenced_per_key_and_a_value_cannot_close_its_fence():
    text = render_input(
        "Bash",
        {"command": "cat <<'EOF'\n```\n# not a heading\nEOF", "timeout": 30, "env": {"A": "1"}},
        max_chars=1000,
    )
    assert text == (
        "`command`:\n````bash\ncat <<'EOF'\n```\n# not a heading\nEOF\n````\n\n"
        "`timeout`: `30`\n\n"
        '`env`:\n```json\n{\n  "A": "1"\n}\n```'
    )
    assert render_input("Read", {}, 100) == "_No input._"
    assert "[... 5 more characters left out of the trace]" in render_input(
        "Write", {"content": "x" * 205}, 200
    )


def test_the_header_says_what_the_file_is_and_how_it_was_redacted():
    assert render_header(header()) == (
        "# calu session trace\n"
        "\n"
        "- calu session: 41\n"
        "- Claude session: `0b0e7c1e-1111-4222-8333-444455556666`\n"
        "- Project: shop\n"
        "- Started: 2026-11-18T10:22:03Z\n"
        "- User: kai\n"
        "- Claude model: opus\n"
        "- calu version: 1.2.3\n"
        "- Content: plain (Claude's text and the summaries of its thinking as written; no "
        "images)\n"
        "- Redaction: the secret values calu knows and common shapes of keys and tokens are "
        "replaced; secrets and personal data are replaced by the redaction agent "
        "(`fake-redactor`)\n"
        "\n"
        "A block that could not be redacted is replaced by a line that says it was withheld. "
        "Redaction by a model is not a guarantee: read this file before you publish it.\n"
        "\n"
        "---\n"
        "\n"
    )


def test_the_header_says_when_only_the_deterministic_stage_ran_and_what_it_continues():
    text = render_header(
        header(
            redactor=None,
            claude_session=None,
            content="narrated",
            continues="this session is a fork of the conversation recorded in `a/b.md`",
            note="this trace starts in the middle of a session",
        )
    )
    assert "- Claude session: not known yet (a later line names it)\n" in text
    assert "(`traces.redact` is off): personal data is not removed\n" in text
    assert "- Content: narrated (Claude's text and thinking retold" in text
    assert "- Continues: this session is a fork of the conversation recorded in `a/b.md`\n" in text
    assert "- Note: this trace starts in the middle of a session\n" in text


# ---------------------------------------------------------------------------- rendering


def test_a_plain_turn_is_rendered_block_by_block():
    renderer = TraceRenderer(narrated=False, max_result_chars=200)
    image = ChatImage(media_type="image/png", data="aGk=")
    text = rendered(
        renderer,
        [
            SessionState(state="idle", session_id="claude-1"),
            UserInput(text="Fix the bug\n# please", images=[image, image]),
            AssistantThinking(text="my thoughts"),
            AssistantDelta(message_id="m", text="I wi"),
            AssistantText(text="I will look."),
            ToolUse(tool_use_id="t1", name="Bash", input={"command": "ls"}),
            CheckResult(
                tool_use_id="t1",
                tool_name="Bash",
                route=Route.COMMAND,
                decision=Decision.ALLOW,
                reason="Lists files.",
                step_summary="Claude lists the directory.",
                comment="Prefer `ls -la`.",
            ),
            ToolResult(tool_use_id="t1", content="a.py\n" + "x" * 300),
            ToolUse(tool_use_id="t2", name="Read", input={"file_path": "a.py"}),
            CheckResult(
                tool_use_id="t2", tool_name="Read", route=Route.ALLOW, decision=Decision.PASS
            ),
            ToolResult(tool_use_id="t2", content="", is_error=True),
            TurnResult(session_id="claude-1", result="done", background_tasks=2),
        ],
    )
    assert text == (
        "_Claude session: `claude-1`_\n\n"
        "## Turn 1\n\n"
        "### User\n\n"
        "> Fix the bug\n> # please\n\n"
        "_2 images were attached; images are not part of the trace._\n\n"
        "### Claude's thinking\n\n"
        "> my thoughts\n\n"
        "### Claude\n\n"
        "> I will look.\n\n"
        "### Tool call 1: Bash\n\n"
        "`command`:\n```bash\nls\n```\n\n"
        "### Check of tool call 1 (Bash): ALLOW\n\n"
        "Route: `command`\n\n"
        "Reason:\n> Lists files.\n\n"
        "Summary of the step:\n> Claude lists the directory.\n\n"
        "Comment to Claude:\n> Prefer `ls -la`.\n\n"
        "### Result of tool call 1 (Bash)\n\n"
        "```\na.py\n" + "x" * 195 + "\n[... 105 more characters left out of the trace]\n```\n\n"
        "### Tool call 2: Read\n\n"
        "`file_path`:\n```\na.py\n```\n\n"
        "### Check of tool call 2 (Read): PASS\n\n"
        "Route: `allow`\n\n"
        "### Result of tool call 2 (Read): error\n\n"
        "_Empty result._\n\n"
        "### End of turn 1\n\n"
        "Result: success\n\n"
        "Background tasks still running: 2\n\n"
    )


def test_a_rewritten_thinking_block_is_rendered_once_in_its_final_form():
    """D241: fragments and earlier versions of a thinking summary never reach the file."""
    renderer = TraceRenderer(narrated=False)
    events: list[Event] = [
        UserInput(text="go"),
        AssistantDelta(message_id="s1", text="Draft ", thinking=True),
        AssistantDelta(message_id="s1", text="one", thinking=True),
    ]
    assert [b.heading for e in events for b in renderer.render(e)] == ["## Turn 1", "### User"]
    later: list[Event] = [
        AssistantThinking(text="Draft one", message_id="m1"),
        AssistantThinking(text="The final summary.", message_id="m1"),
        AssistantThinking(text="A second block.", message_id="m2"),
        AssistantText(text="Done.", message_id="m2"),
        TurnResult(result="done"),
    ]
    text = rendered(renderer, later)
    assert text == (
        "### Claude's thinking\n\n> The final summary.\n\n"
        "### Claude's thinking\n\n> A second block.\n\n"
        "### Claude\n\n> Done.\n\n"
        "### End of turn 1\n\nResult: success\n\n"
    )


def test_thinking_that_only_streamed_is_written_at_the_end_of_the_turn_or_trace():
    renderer = TraceRenderer(narrated=False)
    rendered(renderer, [UserInput(text="go")])
    fragments: list[Event] = [
        AssistantDelta(message_id="s1", text="Half a ", thinking=True),
        AssistantDelta(message_id="s1", text="thought", thinking=True),
        AssistantDelta(agent_id="sub", message_id="s2", text="Sub thought", thinking=True),
    ]
    assert rendered(renderer, fragments) == ""
    blocks = renderer.close_all()
    assert [(b.heading, b.body) for b in blocks] == [
        ("### Claude's thinking", "> Half a thought"),
        ("#### Subagent 1: thinking", "> Sub thought"),
    ]
    assert renderer.close_all() == [], "written once"


def test_messages_that_are_not_the_users_are_labelled_and_a_failed_turn_says_so():
    renderer = TraceRenderer(narrated=False)
    text = rendered(
        renderer,
        [
            UserInput(text="from next door", source="cross_session"),
            UserInput(text="task finished", source="system"),
            TurnResult(is_error=True, subtype="error_max_turns"),
            UserInput(text="again"),
        ],
    )
    assert text == (
        "## Turn 1\n\n"
        "### Message from another Claude session (not from the user)\n\n> from next door\n\n"
        "### Automatic message from calu or Claude Code (not from the user)\n\n"
        "> task finished\n\n"
        "### End of turn 1\n\nResult: error (`error_max_turns`)\n\n"
        "## Turn 2\n\n"
        "### User\n\n> again\n\n"
    )


def test_escalations_say_who_decided_and_carry_the_note():
    renderer = TraceRenderer(narrated=False)
    request = EscalationRequest(
        escalation_id="17",
        tool_use_id="t1",
        tool_name="Bash",
        tool_input={"command": "git push"},
        reason="Pushing waits for a person.",
        rendered="THE CHECKER'S CONTEXT",
    )
    text = rendered(
        renderer,
        [
            ToolUse(tool_use_id="t1", name="Bash", input={"command": "git push"}),
            request,
            EscalationResolved(escalation_id="17", decision="deny", note="Not today."),
        ],
        resolved_by="client:tui",
    )
    assert "THE CHECKER'S CONTEXT" not in text
    assert text.endswith(
        "### Escalation 17: tool call 1 (Bash) waits for a person\n\n"
        "Reason:\n> Pushing waits for a person.\n\n"
        "### Escalation 17: denied\n\n"
        "Decided by: `client:tui`\n\n"
        "Note:\n> Not today.\n\n"
    )
    allowed = rendered(renderer, [EscalationResolved(escalation_id="18", decision="allow")])
    assert allowed == "### Escalation 18: allowed\n\nDecided by: not recorded\n\n"
    cancelled = rendered(renderer, [EscalationResolved(escalation_id="19", decision="cancelled")])
    assert cancelled == "### Escalation 19: ended without a decision\n\n"


def test_a_question_of_the_agent_is_rendered_with_its_options_and_its_answer():
    renderer = TraceRenderer(narrated=False)
    text = rendered(
        renderer,
        [
            EscalationRequest(
                escalation_id="3",
                tool_use_id="q1",
                tool_name="AskUserQuestion",
                tool_input={},
                reason="Claude asks the user.",
                rendered="",
                questions=[
                    {
                        "question": "Which database?",
                        "header": "DB",
                        "options": [
                            {"label": "Postgres", "description": "the default"},
                            {"label": "SQLite"},
                        ],
                    }
                ],
            ),
            EscalationResolved(
                escalation_id="3", decision="deny", note="The user answered: Postgres"
            ),
        ],
        resolved_by="operator",
    )
    assert text == (
        "## Turn 1\n\n"
        "### Escalation 3: Claude asks the user (tool call 1 (AskUserQuestion))\n\n"
        "Reason:\n> Claude asks the user.\n\n"
        "Question 1:\n> Which database?\n> - Postgres: the default\n> - SQLite\n\n"
        "### Escalation 3: answered\n\n"
        "Decided by: `operator`\n\n"
        "Answer, as Claude received it:\n> The user answered: Postgres\n\n"
    )


def test_subagents_are_nested_under_the_step_that_started_them():
    renderer = TraceRenderer(narrated=False)
    text = rendered(
        renderer,
        [
            UserInput(text="go"),
            ToolUse(tool_use_id="a1", name="Agent", input={"description": "explore"}),
            AssistantText(agent_id="a1", text="Looking."),
            ToolUse(agent_id="a1", tool_use_id="t2", name="Read", input={"file_path": "a.py"}),
            ToolUse(agent_id="a1", tool_use_id="a2", name="Task", input={"description": "deeper"}),
            ToolUse(agent_id="a2", tool_use_id="t4", name="Grep", input={"pattern": "x"}),
            ToolResult(agent_id="a2", tool_use_id="t4", content="hit"),
            CheckResult(
                tool_use_id="a1",
                agent_id="a1",
                tool_name="Agent",
                route=Route.SUBAGENT,
                decision=Decision.PASS,
            ),
            ToolResult(tool_use_id="a1", content="done"),
            ToolUse(agent_id="unseen", tool_use_id="t9", name="LS", input={"path": "."}),
        ],
    )
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "## Turn 1",
        "### User",
        "### Tool call 1: Agent (starts subagent 1)",
        "#### Subagent 1: text",
        "#### Subagent 1: tool call 2: Read",
        "#### Subagent 1: tool call 3: Task (starts subagent 2)",
        "##### Subagent 2: tool call 4: Grep",
        "##### Subagent 2: result of tool call 4 (Grep)",
        "#### Subagent 1: check of the result handed back by tool call 1 (Agent): PASS",
        "### Result of tool call 1 (Agent)",
        "#### Subagent 3: tool call 5: LS",
    ]


def test_the_narrated_form_collects_text_until_the_agents_next_step():
    renderer = TraceRenderer(narrated=True)
    first = renderer.render(AssistantThinking(text="plan"))
    assert [block.heading for block in first] == ["## Turn 1", "### Claude, narrated"]
    block = first[1]
    assert renderer.render(AssistantText(text="I will.")) == []
    sub = renderer.render(AssistantText(agent_id="a1", text="sub says"))[0]
    assert sub.heading == "#### Subagent 1: text and thinking, narrated"
    assert block.outputs == [ClaudeOutput("thinking", "plan"), ClaudeOutput("text", "I will.")]
    assert not block.closed and not sub.closed

    renderer.render(ToolUse(tool_use_id="t1", name="Bash", input={"command": "ls"}))
    assert block.closed and not sub.closed, "only the main agent took a step"

    renderer.render(
        AgentsUpdate(agents=[SubagentInfo(agent_id="a1", status="completed")]),
    )
    assert sub.closed, "a finished subagent writes no more"

    last = renderer.render(AssistantText(text="Done."))[0]
    renderer.render(TurnResult())
    assert last.closed and last.outputs == [ClaudeOutput("text", "Done.")]


def test_text_after_a_delivered_narration_makes_the_narration_incomplete():
    renderer = TraceRenderer(narrated=True)
    block = renderer.render(AssistantText(text="one"))[1]
    block.narration = "Narrated: one"
    renderer.render(AssistantText(text="two"))
    assert block.narration is None


def test_a_message_from_another_session_is_narrated_in_the_narrated_form():
    renderer = TraceRenderer(narrated=True)
    block = renderer.render(UserInput(text="psst", source="cross_session"))[1]
    assert block.heading == (
        "### Message from another Claude session (not from the user), narrated"
    )
    assert block.outputs == [ClaudeOutput("message", "psst")] and block.closed and not block.body


# ---------------------------------------------------------------------------- the file's path


def test_the_path_is_directory_date_user_and_the_next_free_number(tmp_path: Path):
    target = session_directory(tmp_path, "agent/traces", STARTED, "kai")
    assert target == tmp_path / "agent/traces/2026-11-18/kai"
    first, fd = create_trace_file(tmp_path, target)
    os.close(fd)
    assert first.name == "session_001.md"
    (target / "session_007.md").write_text("")
    (target / "notes.md").write_text("")
    eighth, fd = create_trace_file(tmp_path, target)
    os.close(fd)
    assert eighth.name == "session_008.md"
    (target / "session_999.md").write_text("")
    big, fd = create_trace_file(tmp_path, target)
    os.close(fd)
    assert big.name == "session_1000.md"


def test_a_user_name_becomes_one_safe_path_component():
    assert user_component("kai") == "kai"
    assert user_component("../etc") == "_etc"
    assert user_component("a b/c") == "a_b_c"
    assert user_component("") == "unknown"


def test_two_sessions_that_start_at_once_never_share_a_file(tmp_path: Path):
    target = tmp_path / "t" / "2026-11-18" / "kai"
    paths: list[Path] = []
    barrier = threading.Barrier(8)

    def create() -> None:
        barrier.wait()
        for _ in range(10):
            path, fd = create_trace_file(tmp_path, target)
            os.close(fd)
            paths.append(path)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(paths) == 80 and len(set(paths)) == 80


def test_a_number_taken_since_the_directory_was_listed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "t"
    target.mkdir()
    (target / "session_001.md").write_text("another session's")
    monkeypatch.setattr(os, "listdir", lambda _path: [])  # the listing came before that file
    path, fd = create_trace_file(tmp_path, target)
    os.close(fd)
    assert path.name == "session_002.md"
    assert (target / "session_001.md").read_text() == "another session's"


def test_a_trace_directory_that_leads_out_of_the_project_is_refused(tmp_path: Path):
    workspace = tmp_path / "proj"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "traces").symlink_to(outside)
    with pytest.raises(TraceFileError, match="out of the project directory"):
        create_trace_file(workspace, workspace / "traces" / "2026-11-18" / "kai")
    assert not list(outside.iterdir()), "not even the directories are created outside"


def test_the_trace_of_a_claude_session_is_found_by_its_marker(tmp_path: Path):
    root = tmp_path / "agent" / "traces"
    old = root / "2026-11-17" / "kai" / "session_001.md"
    new = root / "2026-11-18" / "kai" / "session_002.md"
    other = root / "2026-11-18" / "kai" / "session_001.md"
    for path, session in ((old, "abc"), (new, "abc"), (other, "xyz")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# calu session trace\n\n- {session_marker(session)}\n")
    os.utime(old, (1, 1))
    assert find_trace_of(root, "abc") == new
    assert find_trace_of(root, "abc", skip=new) == old
    assert find_trace_of(root, "nobody") is None
    assert find_trace_of(tmp_path / "missing", "abc") is None


# ---------------------------------------------------------------------------- stage 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("id AKIAIOSFODNN7EXAMPLE end", "id [redacted: aws access key] end"),
        ("ASIAABCDEFGHIJKLMNOP", "[redacted: aws access key]"),
        ("key sk-proj-abc123DEF456ghi789JKL0", "key [redacted: api key]"),
        ("sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", "[redacted: api key]"),
        ("ghp_" + "a1B2" * 9, "[redacted: github token]"),
        ("github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1", "[redacted: github token]"),
        ("AIza" + "B" * 35, "[redacted: api key]"),
        ("xoxb-1234567890-abcdefghij", "[redacted: token]"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "[redacted: jwt]",
        ),
        (
            "Authorization: Bearer abcdefghijklmnop1234",
            "Authorization: Bearer [redacted: token]",
        ),
        (
            "postgresql://calu:s3cr3t-pw@db.example:5432/calu",
            "postgresql://calu:[redacted: password]@db.example:5432/calu",
        ),
        (
            "https://api.example/v1?user=a&password=hunter2&x=1",
            "https://api.example/v1?user=a&password=[redacted: secret value]&x=1",
        ),
        (
            "https://api.example/v1?access_token=abc.def",
            "https://api.example/v1?access_token=[redacted: secret value]",
        ),
        (
            "host=db dbname=calu user=calu password=hunter2 sslmode=require",
            "host=db dbname=calu user=calu password=[redacted: password] sslmode=require",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----\nrest",
            "[redacted: private key]\nrest",
        ),
        (
            '{"key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQ\\n-----END PRIVATE KEY-----\\n"}',
            '{"key": "[redacted: private key]\\n"}',
        ),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n```",
            "[redacted: private key]```",
        ),
    ],
)
def test_a_shape_of_a_key_or_token_is_replaced_by_a_typed_placeholder(text: str, expected: str):
    assert redact_shapes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The task-management-service handles risk-assessment and disk-usage reports.",
        "connect(host=host, password=password)",
        'password = os.environ["DB_PASSWORD"]',
        "if token is None:\n    token = read_token()",
        "class sk-loading-spinner-animation-wrapper { color: red }",
        "git@github.com:kadeng/calu.git and https://github.com/kadeng/calu",
        "http://localhost:8080/path and https://example.com/a?page=2&sort=asc",
        "commit 0123456789abcdef0123456789abcdef01234567",
        "uuid 0b0e7c1e-1111-4222-8333-444455556666",
        "def test_password_is_hashed(): assert hash('pw') != 'pw'",
        "AKIA is the prefix of an access key id; eyJ starts a JWT.",
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
        "Mail the team at 10:30, room 4@B.",
    ],
)
def test_ordinary_text_and_code_are_left_alone(text: str):
    assert redact_deterministic(text, Secrets()) == text


def test_known_secret_values_are_replaced_longest_first_and_short_ones_are_kept():
    secrets = Secrets({"tok-12345678": "token", "tok-12345678-extended": "api key", "dev": "x"})
    secrets.add("  padded-secret  ", "password")
    assert len(secrets) == 3, "a value of three characters would be replaced all over the text"
    text = "a tok-12345678-extended b tok-12345678 c dev d padded-secret"
    assert secrets.redact(text) == (
        "a [redacted: api key] b [redacted: token] c dev d [redacted: password]"
    )


def test_the_secret_values_of_the_environment_are_collected():
    environ = {
        "MISTRAL_API_KEY": "mistral-value-1",
        "PLAIN_NAME": "named-by-calu-2",
        "GITHUB_TOKEN": "token-value-333",
        "DB_PASSWORD": "password-value-4",
        "DATABASE_URL": "postgresql://calu:url-password-5@db/calu",
        "KEYBOARD_LAYOUT": "us-international",
        "HOME": "/home/someone",
        "EMPTY_API_KEY": "",
    }
    secrets = Secrets.from_environment({"PLAIN_NAME", "NOT_SET"}, environ)
    text = " ".join(environ.values())
    assert secrets.redact(text) == (
        "[redacted: api key] [redacted: secret value] [redacted: token] [redacted: password] "
        "postgresql://calu:[redacted: password]@db/calu us-international /home/someone "
    )
    assert kind_of_variable("AWS_SECRET_ACCESS_KEY") == "api key"
    assert kind_of_variable("SOMETHING") == "secret value"


def test_secrets_added_while_a_session_runs_count_from_then_on():
    secrets = Secrets()
    other = Secrets({"client-token-xyz": "client token"})
    assert secrets.redact("client-token-xyz") == "client-token-xyz"
    secrets.update(other)
    assert secrets.redact("client-token-xyz") == "[redacted: client token]"


# ---------------------------------------------------------------------------- stage 2


def test_a_long_text_is_cut_into_pieces_at_line_ends():
    text = "".join(f"line {number}\n" for number in range(40)) + "x" * 25 + "\nend"
    pieces = chunks(text, limit=20)
    assert "".join(pieces) == text
    assert all(len(piece) <= 20 for piece in pieces)
    assert pieces[0] == "line 0\nline 1\n"
    assert chunks("") == []
    assert chunks("short") == ["short"]


def test_an_answer_is_checked_against_its_text():
    assert checked("a\nb\n", "a\nb") == "a\nb\n", "a model drops the last line end"
    assert checked("abc", "[redacted: name]") == "[redacted: name]"
    with pytest.raises(RedactionFailed, match="empty"):
        checked("abc", "  ")
    with pytest.raises(RedactionFailed, match="characters"):
        checked("abc", "x" * 400)


def redactor(model: Model, timeout_s: float = 5.0) -> Redactor:
    """Return a redaction agent on a stand-in model."""
    return Redactor(redaction_instructions(), model, timeout_s=timeout_s)


async def test_the_agent_replaces_personal_data_and_returns_the_rest_unchanged():
    text = "> Ask jane.roe@example.org\n\n```\nprint('hi')\n```\n"
    assert await redactor(fake_redactor()).redact(text) == (
        "> Ask [redacted: e-mail address]\n\n```\nprint('hi')\n```\n"
    )
    assert await redactor(fake_redactor()).redact("  \n") == "  \n"


async def test_a_long_text_is_redacted_piece_by_piece():
    calls: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        piece = excerpt_of(_last_prompt(messages))
        calls.append(piece)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text": piece.replace("b", "B")})]
        )

    text = ("a" * 99 + "\n") * 100 + ("b" * 99 + "\n") * 100
    assert len(text) > 2 * CHUNK_CHARS
    result = await redactor(FunctionModel(answer)).redact(text)
    assert result == text.replace("b", "B")
    assert len(calls) == 3 and all(len(call) <= CHUNK_CHARS for call in calls)


def raising(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model that fails."""
    raise RuntimeError("the provider is down")


def chatty(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model that answers in prose instead of the output field."""
    return ModelResponse(parts=[TextPart("Sure! Here is the text: ...")])


def bloating(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model whose answer is much longer than the text it was given."""
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": "x" * 5000})])


async def sleeping(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model that does not answer in time."""
    await anyio.sleep(30)
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": "late"})])


@pytest.mark.parametrize("model", [raising, chatty, bloating, sleeping])
async def test_a_failure_of_the_agent_is_a_failed_redaction(model):
    with pytest.raises(RedactionFailed):
        await redactor(FunctionModel(model), timeout_s=0.2).redact("the raw text")


# ---------------------------------------------------------------------------- the writer


def writer_for(
    workspace: Path,
    model: Model | None,
    narrated: bool = False,
    **options: object,
) -> TraceWriter:
    """Return a writer on a stand-in redaction agent (None: stage 1 only)."""
    return TraceWriter(
        workspace,
        session_directory(workspace, "agent/traces", STARTED, "kai"),
        header(
            redactor=None if model is None else "fake-redactor",
            content="narrated" if narrated else "plain",
        ),
        TraceRenderer(narrated),
        options.pop("secrets", Secrets()),  # type: ignore[arg-type]
        redactor=None if model is None else redactor(model, timeout_s=2.0),
        **options,  # type: ignore[arg-type]
    )


def body_of(writer: TraceWriter) -> str:
    """Return the trace file's text after its header."""
    assert writer.path is not None
    return writer.path.read_text().split("---\n\n", 1)[1]


async def test_nothing_unredacted_reaches_the_disk_when_the_agent_fails(tmp_path: Path):
    writer = writer_for(tmp_path, FunctionModel(raising))
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(UserInput(text="my mail is jane.roe@example.org"))
        writer.feed(ToolUse(tool_use_id="t1", name="Bash", input={"command": "cat secret.txt"}))
        writer.feed(TurnResult())
        await writer.close("The session ended.")
    assert body_of(writer) == (
        "## Turn 1\n\n"
        f"### User\n\n{WITHHELD}\n\n"
        f"### Tool call 1: Bash\n\n{WITHHELD}\n\n"
        "### End of turn 1\n\nResult: success\n\n"
        "_The session ended._\n\n"
    )
    whole = writer.path.read_text() if writer.path else ""
    assert "jane.roe" not in whole and "secret.txt" not in whole
    assert writer.counted.withheld == 2 and "the provider is down" in writer.counted.failures[0]


async def test_with_redaction_off_only_the_deterministic_stage_runs(tmp_path: Path):
    writer = writer_for(tmp_path, None)
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(UserInput(text="jane.roe@example.org has key AKIAIOSFODNN7EXAMPLE"))
        await writer.close()
    assert body_of(writer) == (
        "## Turn 1\n\n### User\n\n> jane.roe@example.org has key [redacted: aws access key]\n\n"
    )
    assert "`traces.redact` is off" in (writer.path.read_text() if writer.path else "")


async def test_blocks_are_written_in_the_order_of_the_events_under_a_slow_agent(tmp_path: Path):
    async def slow_first(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        piece = excerpt_of(_last_prompt(messages))
        await anyio.sleep(0.3 if "message 0" in piece else 0)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": piece})])

    writer = writer_for(tmp_path, FunctionModel(slow_first))
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        before = anyio.current_time()
        for number in range(6):
            writer.feed(UserInput(text=f"message {number}"))
        assert anyio.current_time() - before < 0.1, "feeding never waits for the agent"
        await writer.close()
    order = [line for line in body_of(writer).splitlines() if line.startswith("> message")]
    assert order == [f"> message {number}" for number in range(6)]


async def test_a_full_queue_leaves_blocks_out_and_says_how_many(tmp_path: Path):
    writer = writer_for(tmp_path, fake_redactor(), queue_blocks=3)
    for number in range(6):  # nothing is written yet: the writer's task has not started
        writer.feed(UserInput(text=f"message {number}"))
    assert writer.counted.left_out == 4, "the turn heading and two messages fit"
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        with anyio.fail_after(5):
            while writer.counted.written < 3:
                await anyio.sleep(0.01)
        writer.feed(UserInput(text="message 6"))
        await writer.close()
    assert body_of(writer) == (
        "## Turn 1\n\n"
        "### User\n\n> message 0\n\n"
        "### User\n\n> message 1\n\n"
        "_[4 blocks were left out of the trace here: the trace writer was behind]_\n\n"
        "### User\n\n> message 6\n\n"
    )
    assert "message 3" not in body_of(writer)


async def test_the_end_of_a_session_does_not_wait_for_ever_and_writes_no_raw_text(
    tmp_path: Path,
):
    writer = writer_for(tmp_path, FunctionModel(sleeping), flush_timeout_s=0.2)
    writer.redactor = redactor(FunctionModel(sleeping), timeout_s=60)
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(UserInput(text="raw words of the user"))
        with anyio.fail_after(5):
            await writer.close("The session ended.")
    assert body_of(writer) == (
        f"## Turn 1\n\n### User\n\n{WITHHELD_AT_END}\n\n_The session ended._\n\n"
    )


async def test_a_file_that_cannot_be_created_is_reported_and_stops_nothing(tmp_path: Path):
    (tmp_path / "agent").write_text("a file where the directory should be")
    said: list[tuple[str, str]] = []

    async def report(level: str, message: str) -> None:
        said.append((level, message))

    writer = writer_for(tmp_path, fake_redactor(), report=report)
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(UserInput(text="hello"))
        await anyio.sleep(0.05)
        writer.feed(UserInput(text="again"))
        with anyio.fail_after(5):
            await writer.close("The session ended.")
    assert writer.path is None
    assert [level for level, _ in said] == ["warning"]
    assert "the session trace is not written" in said[0][1]


async def test_the_file_path_is_reported_and_a_resumed_session_names_the_file_it_continues(
    tmp_path: Path,
):
    earlier = tmp_path / "agent/traces/2026-11-17/kai/session_004.md"
    earlier.parent.mkdir(parents=True)
    earlier.write_text(f"# calu session trace\n\n- {session_marker('claude-abc')}\n")
    said: list[tuple[str, str]] = []

    async def report(level: str, message: str) -> None:
        said.append((level, message))

    writer = writer_for(tmp_path, None, report=report, continues=("claude-abc", "resumed"))
    forked = writer_for(tmp_path, None, continues=("claude-abc", "forked"))
    unknown = writer_for(tmp_path, None, continues=("claude-zzz", "resumed"))
    for one in (writer, forked, unknown):
        async with anyio.create_task_group() as tg:
            one.start(tg)
            one.feed(UserInput(text="go on"))
            await one.close()
    assert said == [("info", "session trace: agent/traces/2026-11-18/kai/session_001.md")]
    assert writer.path is not None and forked.path is not None and unknown.path is not None
    assert (
        "- Continues: this session is a continuation of the conversation recorded in "
        "`agent/traces/2026-11-17/kai/session_004.md`\n"
    ) in writer.path.read_text()
    assert "- Continues: this session is a fork of the conversation recorded in" in (
        forked.path.read_text()
    )
    assert (
        "- Continues: this session is a continuation of Claude session `claude-zzz`; no "
        "trace file of that conversation was found\n"
    ) in unknown.path.read_text()


async def test_a_file_that_got_no_block_is_removed_when_it_is_closed(tmp_path: Path):
    """A header and "The session ended." say nothing; the session id line is no step either."""
    empty = writer_for(tmp_path, None)
    path = tmp_path / "agent/traces/2026-11-18/kai/session_001.md"
    async with anyio.create_task_group() as tg:
        empty.start(tg)
        await empty.created.wait()
        assert empty.path == path and path.exists()
        empty.feed(SessionState(state="idle", session_id="claude-abc"))
        await empty.close("The session ended.")
    assert empty.removed and not path.exists()
    assert not (tmp_path / "agent/traces/2026-11-18").exists(), "the emptied directories go"
    assert (tmp_path / "agent/traces").is_dir()

    # The next file of the day starts at 001 again, and a file with a step stays.
    used = writer_for(tmp_path, None)
    follower = writer_for(tmp_path, None, after=empty)
    async with anyio.create_task_group() as tg:
        used.start(tg)
        used.feed(UserInput(text="go on"))
        await used.close("The session ended.")
        follower.start(tg)
        follower.feed(UserInput(text="and on"))
        await follower.close()
    assert not used.removed and used.path is not None and used.path.name == "session_001.md"
    assert "> go on\n" in used.path.read_text()
    assert follower.path is not None and "the steps before" not in follower.path.read_text()


async def test_a_secret_named_after_the_start_is_replaced_from_then_on(tmp_path: Path):
    secrets = Secrets()
    writer = writer_for(tmp_path, None, secrets=secrets)
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        secrets.add("client-token-0123456789", "client token")
        writer.feed(UserInput(text="attach with client-token-0123456789"))
        await writer.close()
    assert "> attach with [redacted: client token]\n" in body_of(writer)


async def test_the_sessions_narration_is_used_and_no_second_one_is_asked_for(tmp_path: Path):
    asked: list[list[ClaudeOutput]] = []

    async def narrate(outputs: list[ClaudeOutput]) -> str:
        asked.append(outputs)
        return "The writer's own narration."

    writer = writer_for(tmp_path, fake_redactor(), narrated=True, narrate=narrate)
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(AssistantThinking(text="plan"))
        writer.feed(AssistantText(text="I mail jane.roe@example.org."))
        # The session narrates for its checker before the tool call's event arrives.
        writer.narration(
            [
                ClaudeOutput("thinking", "plan"),
                ClaudeOutput("text", "I mail jane.roe@example.org."),
            ],
            "Claude said that it would mail jane.roe@example.org.",
        )
        writer.feed(ToolUse(tool_use_id="t1", name="Bash", input={"command": "ls"}))
        await writer.close()
    assert asked == []
    assert body_of(writer) == (
        "## Turn 1\n\n"
        "### Claude, narrated\n\n"
        "> Claude said that it would mail [redacted: e-mail address].\n\n"
        "### Tool call 1: Bash\n\n`command`:\n```bash\nls\n```\n\n"
    )


async def test_text_the_session_did_not_narrate_is_narrated_by_the_writer(tmp_path: Path):
    async def narrate(outputs: list[ClaudeOutput]) -> str:
        return "Claude said: " + " / ".join(output.text for output in outputs)

    writer = writer_for(
        tmp_path, fake_redactor(), narrated=True, narrate=narrate, narration_wait_s=0.05
    )
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(AssistantText(text="one"))
        writer.narration([ClaudeOutput("text", "something else")], "does not fit this block")
        writer.feed(ToolUse(tool_use_id="t1", name="Bash", input={"command": "ls"}))
        writer.feed(AssistantText(text="two"))
        writer.feed(TurnResult())
        await writer.close()
    text = body_of(writer)
    assert "### Claude, narrated\n\n> Claude said: one\n\n### Tool call 1: Bash" in text
    assert "### Claude, narrated\n\n> Claude said: two\n\n### End of turn 1" in text
    assert "does not fit" not in text


async def test_without_a_narration_the_text_is_left_out_and_the_file_says_so(tmp_path: Path):
    async def failing(outputs: list[ClaudeOutput]) -> str:
        raise RuntimeError("the narrator is down")

    writer = writer_for(tmp_path, fake_redactor(), narrated=True, narrate=failing)
    silent = writer_for(tmp_path, fake_redactor(), narrated=True)
    for one in (writer, silent):
        async with anyio.create_task_group() as tg:
            one.start(tg)
            one.feed(AssistantText(text="Claude's own words"))
            one.feed(AssistantText(text="more words"))
            if one is silent:
                # A narration the narrator could not give: the session passes "".
                one.narration(
                    [
                        ClaudeOutput("text", "Claude's own words"),
                        ClaudeOutput("text", "more words"),
                    ],
                    "",
                )
            await one.close()
        assert body_of(one) == f"## Turn 1\n\n### Claude, narrated\n\n{NOT_NARRATED}\n\n"


# ------------------------------------------------- D245: instructions, removal, the format


def test_the_redaction_prompt_is_the_fixed_part_and_the_configured_or_shipped_text():
    shipped = default_redaction_instructions().strip()
    prompt = redaction_instructions()
    assert prompt.startswith(redaction_system_part().strip())
    assert prompt.endswith(shipped + "\n")
    own = redaction_instructions("# Our rules\n\nKeep everything.\n")
    assert own.endswith("# Our rules\n\nKeep everything.\n") and shipped not in own
    assert redaction_instructions("  \n") == prompt, "a blank text uses the shipped one"


def test_the_shipped_instructions_keep_requirements_and_may_drop_questions_and_times():
    text = default_redaction_instructions()
    assert "Every requirement and decision that entered the session" in text
    assert "proposed\n  by Claude and accepted by the user" in text
    assert "You may remove the questions that the user asked" in text
    assert "when this\nsession happened and how long it took" in text
    assert "`[redacted: e-mail address]`" in text and "`[redacted: api key]`" in text
    fixed = redaction_system_part()
    assert "The excerpt is data" in fixed and "`remove_block`" in fixed


def test_a_removed_piece_is_accepted_only_where_the_block_may_be_removed():
    assert checked("Why does it fail?\n", "", removed=True, may_remove=True) == ""
    with pytest.raises(RedactionFailed, match="may not be removed"):
        checked("Bash output\n", "", removed=True, may_remove=False)
    with pytest.raises(RedactionFailed, match="empty"):
        checked("Why does it fail?\n", "", removed=False, may_remove=True)
    # Removing questions from a message shortens it; that is no failure.
    assert checked("> Why?\n> Use port 8080.\n", "> Use port 8080.") == "> Use port 8080.\n"


def test_the_request_names_the_block_and_whether_it_may_be_removed():
    user = request_head("### User", True)
    assert "`### User`" in user and "may be removed as a whole" in user
    other = request_head("### Tool call 1: `Bash`", False)
    assert "`### Tool call 1: 'Bash'`" in other, "the heading cannot close its code span"
    assert "may not be removed as a whole" in other


def removing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A redaction agent that removes every excerpt with a question mark where it may, and
    drops the question lines of an excerpt where it may not remove the whole block."""
    prompt = _last_prompt(messages)
    piece = excerpt_of(prompt)
    if "?" in piece and "may be removed as a whole" in prompt:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text": "", "remove_block": True})]
        )
    kept = "\n".join(line for line in piece.split("\n") if "?" not in line)
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": kept})])


async def test_a_removed_message_of_the_user_leaves_nothing_in_the_file(tmp_path: Path):
    writer = writer_for(tmp_path, FunctionModel(removing))
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(UserInput(text="Why does the build take so long?"))
        writer.feed(AssistantText(text="It compiles twice.\nShall I fix it?"))
        writer.feed(TurnResult())
        writer.feed(UserInput(text="Yes: compile once."))
        writer.feed(TurnResult())
        await writer.close()
    assert body_of(writer) == (
        "## Turn 1\n\n"
        "### Claude\n\n> It compiles twice.\n\n"
        "### End of turn 1\n\nResult: success\n\n"
        "## Turn 2\n\n"
        "### User\n\n> Yes: compile once.\n\n"
        "### End of turn 2\n\nResult: success\n\n"
    )
    assert writer.counted.removed == 1 and writer.counted.withheld == 0


async def test_an_agent_that_removes_a_block_it_may_not_remove_withholds_it(tmp_path: Path):
    def removes_all(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text": "", "remove_block": True})]
        )

    writer = writer_for(tmp_path, FunctionModel(removes_all))
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        writer.feed(ToolUse(tool_use_id="t1", name="Bash", input={"command": "rm -r build"}))
        await writer.close()
    assert body_of(writer) == f"## Turn 1\n\n### Tool call 1: Bash\n\n{WITHHELD}\n\n"
    assert writer.counted.withheld == 1 and writer.counted.removed == 0


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        ("date/user", "agent/traces/2026-11-18/kai"),
        ("user", "agent/traces/kai"),
        ("date", "agent/traces/2026-11-18"),
        ("flat", "agent/traces"),
    ],
)
def test_the_layout_chooses_the_folders_of_a_trace_file(layout: str, expected: str):
    target = session_directory(Path("/p"), "agent/traces", STARTED, "kai", layout)
    assert target == Path("/p") / expected
    assert set(TracesConfig.model_fields["layout"].annotation.__args__) == set(LAYOUTS)  # type: ignore[union-attr]


def test_a_random_name_says_nothing_about_the_number_of_sessions(tmp_path: Path):
    target = tmp_path / "agent" / "traces"
    first, fd1 = create_trace_file(tmp_path, target, random_name=True)
    second, fd2 = create_trace_file(tmp_path, target, random_name=True)
    os.close(fd1)
    os.close(fd2)
    assert first != second
    assert all(re.fullmatch(r"session_[0-9a-f]{12}\.md", p.name) for p in (first, second))
    # A numbered file next to random ones counts only the numbered ones.
    path, fd = create_trace_file(tmp_path, target)
    os.close(fd)
    assert path.name == "session_001.md"


async def test_a_flat_layout_with_random_names_is_found_and_never_removes_the_directory(
    tmp_path: Path,
):
    root = tmp_path / "agent" / "traces"
    earlier = root / "session_0123456789ab.md"
    root.mkdir(parents=True)
    earlier.write_text(f"{session_marker('c-1')}\n")
    assert find_trace_of(root, "c-1") == earlier

    writer = TraceWriter(
        tmp_path,
        root,
        header(),
        TraceRenderer(False),
        Secrets(),
        trace_root=root,
        random_name=True,
    )
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        await writer.created.wait()
        await writer.close()
    assert writer.removed and root.is_dir(), "an empty file goes, the trace directory stays"

    user_dir = root / "kai"
    writer = TraceWriter(
        tmp_path, user_dir, header(), TraceRenderer(False), Secrets(), trace_root=root
    )
    async with anyio.create_task_group() as tg:
        writer.start(tg)
        await writer.created.wait()
        await writer.close()
    assert writer.removed and not user_dir.exists() and root.is_dir()


def test_the_header_can_leave_out_the_time_and_the_user():
    full = render_header(header())
    assert "- Started: 2026-11-18T10:22:03Z\n" in full and "- User: kai\n" in full
    bare = render_header(header(started=None, user=None))
    assert "Started" not in bare and "User:" not in bare and "kai" not in bare
    assert "- Project: shop\n- Claude model: opus\n" in bare


def test_turns_can_be_left_unnumbered():
    events: list[Event] = [
        UserInput(text="hi"),
        TurnResult(),
        UserInput(text="again"),
        TurnResult(),
    ]
    numbered = rendered(TraceRenderer(False), events)
    assert "## Turn 2\n" in numbered and "### End of turn 2\n" in numbered
    bare = rendered(TraceRenderer(False, turn_numbers=False), events)
    assert bare.count("## Turn\n") == 2 and bare.count("### End of turn\n") == 2
    assert not re.search(r"Turn \d|turn \d", bare)
