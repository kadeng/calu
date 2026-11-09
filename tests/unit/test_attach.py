"""`calu attach`: rendering received events and turning typed lines into requests."""

from typing import Any

import pytest

from calu.attach import Console, Printer, describe_state, render
from calu.protocol.client import RequestFailed

pytestmark = pytest.mark.anyio


class FakeClient:
    """Records the requests a `Console` sends and answers them from `results`."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        """Create the client.

        Args:
            results: Result per method; a method that is missing answers `{}`.
        """
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.results = results or {}
        self.fail: str | None = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Record the request and return its prepared result.

        Args:
            method: The request method.
            params: Its params.
        """
        self.sent.append((method, params or {}))
        if self.fail == method:
            raise RequestFailed({"code": -32000, "message": "no"})
        return self.results.get(method, {})


def console(results: dict[str, Any] | None = None) -> tuple[Console, FakeClient]:
    """Return a console on a fake client."""
    client = FakeClient(results)
    return Console(client), client  # type: ignore[arg-type]


def test_events_are_rendered_like_the_terminal_of_calu_run():
    assert render("session.state", {"state": "idle", "seq": 1}) == "[idle]"
    assert render("assistant.text", {"text": "hi", "seq": 2}) == "hi"
    assert render("assistant.delta", {"message_id": "m", "text": "x", "seq": 3}) is None
    # An event of an unknown method or with unexpected params is shown raw, never dropped.
    assert "unknown.method" in str(render("unknown.method", {"a": 1}))
    assert "session.state" in str(render("session.state", {"state": "nonsense"}))


def test_the_state_line_names_the_open_escalations():
    line = describe_state(
        {
            "state": "escalated",
            "workspace": "/w",
            "model": "opus",
            "escalations": [{"escalation_id": "1"}],
        }
    )
    assert line == "[state] escalated · /w · model opus · 1 escalation(s) open"


async def test_plain_text_is_sent_as_a_chat_message():
    con, client = console()
    await con.line("fix the test")
    assert client.sent == [("chat.send", {"text": "fix the test"})]


async def test_commands_become_requests():
    con, client = console({"session.start": {"claude_session_id": "c1"}})
    await con.line("/start")
    await con.line("/start resume-me")
    await con.line("/stop")
    await con.line("/interrupt")
    await con.line("/inject later")
    await con.line("/allow 7 go ahead")
    await con.line("/deny 8")
    assert client.sent == [
        ("session.start", {}),
        ("session.start", {"resume_id": "resume-me"}),
        ("session.stop", {}),
        ("interrupt", {}),
        ("chat.send", {"text": "later", "delivery": "inject"}),
        (
            "escalation.decide",
            {"escalation_id": "7", "decision": "allow", "note": "go ahead"},
        ),
        ("escalation.decide", {"escalation_id": "8", "decision": "deny", "note": None}),
    ]
    assert not con.quit.is_set()


async def test_quit_and_shutdown_end_the_client():
    con, client = console()
    await con.line("/quit")
    assert con.quit.is_set()
    con, client = console()
    await con.line("/shutdown")
    assert client.sent == [("shutdown", {})] and con.quit.is_set()


async def test_bad_input_is_reported_and_does_not_send_anything(capsys):
    con, client = console()
    await con.line("/nonsense")
    await con.line("/allow")
    await con.line("/inject")
    assert client.sent == []
    output = capsys.readouterr().out
    assert "unknown command /nonsense" in output
    assert "/allow needs an escalation id" in output
    assert "/inject needs a text" in output


async def test_a_refused_request_is_printed_not_raised(capsys):
    con, client = console()
    client.fail = "chat.send"
    await con.line("hello")
    assert "[error] no (code -32000)" in capsys.readouterr().out
    assert not con.quit.is_set()


async def test_the_printer_hides_thinking_unless_asked(capsys):
    await Printer()("assistant.thinking", {"text": "hmm"})
    assert capsys.readouterr().out == ""
    await Printer(show_thinking=True)("assistant.thinking", {"text": "hmm"})
    assert "(thinking) hmm" in capsys.readouterr().out


async def test_a_comment_reads_the_file_and_sends_the_selection(tmp_path, capsys):
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    client = FakeClient(
        {"comment.add": {"comment": {"after": {"start_line": 2, "end_line": 2, "text": "two"}}}}
    )
    con = Console(client, tmp_path)  # type: ignore[arg-type]
    await con.line("/comment a.py 2 2 why is this here?")
    (method, params) = client.sent[0]
    assert method == "comment.add"
    assert params == {
        "kind": "file",
        "path": "a.py",
        "start_line": 2,
        "end_line": 2,
        "after_text": "one\ntwo\nthree\n",
        "text": "why is this here?",
    }
    assert "[comment] on a.py:2-2: two" in capsys.readouterr().out


async def test_a_comment_reports_bad_arguments_and_missing_files(tmp_path, capsys):
    con, client = console()
    await con.line("/comment a.py 2 2 text")  # no workspace known
    assert "did not say where its project is" in capsys.readouterr().out
    con = Console(client, tmp_path)  # type: ignore[arg-type]
    await con.line("/comment a.py 2")
    assert "needs a path, a first and last line" in capsys.readouterr().out
    await con.line("/comment a.py two three text")
    assert "not line numbers" in capsys.readouterr().out
    await con.line("/comment missing.py 1 1 text")
    assert "No such file" in capsys.readouterr().out
    assert client.sent == []


async def test_a_message_can_be_relayed_to_a_subagent():
    con, client = console()
    await con.line("/to fake-tool-1 look at this")
    assert client.sent == [
        ("chat.send", {"text": "look at this", "to": "fake-tool-1", "delivery": "inject"})
    ]
    await con.line("/to fake-tool-1")
    assert len(client.sent) == 1


async def test_history_prints_the_recorded_conversation(capsys):
    con, client = console(
        {
            "session.history": {
                "claude_session_id": "claude-old",
                "turns": [
                    {
                        "index": 1,
                        "user_inputs": ["fix the typo"],
                        "steps": [
                            {"tool_name": "Edit", "decision": "pass", "summary": "Fixes a typo."}
                        ],
                        "answer": "done",
                    }
                ],
                "steps": [{"seq": 1, "tool_name": "Edit"}],
            }
        }
    )
    await con.line("/history claude-old")
    assert client.sent == [("session.history", {"claude_session_id": "claude-old"})]
    out = capsys.readouterr().out
    assert "> fix the typo" in out
    assert "[Edit, pass] Fixes a typo." in out
    assert "done" in out
    assert "[history] 1 turn(s), 1 step(s) of claude-old" in out
    await con.line("/history")
    assert client.sent[-1] == ("session.history", {})
