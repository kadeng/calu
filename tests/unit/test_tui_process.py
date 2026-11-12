"""The full-screen client against a real session: protocol server, session and the fake runner.

Everything between the keys and the scripted Claude is the real code: `protocol_connector`
opens a WebSocket to a `SessionServer`, the session checks the scripted tool call with the
fake checkers, escalates it and waits, and the side question goes through the runner's side
channel.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot

from calu.claude import FakeRunner
from calu.tui.app import CaluApp, TuiOptions
from calu.tui.connection import protocol_connector
from calu.tui.start import TuiExit, exit_lines, wants_full_screen
from tests.unit.test_protocol_server import served

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run the tests of this file on asyncio, the loop Textual uses."""
    return "asyncio"


async def until(pilot: Pilot[None], condition: Callable[[], bool], what: str) -> None:
    """Let the app run until a condition holds.

    Args:
        pilot: The pilot of the running app.
        condition: Checked after every short pause.
        what: Named in the failure if it does not hold within ten seconds.
    """
    for _ in range(500):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError(f"timed out waiting for: {what}")


async def test_a_turn_with_an_escalation_and_a_side_question_through_a_real_session(
    tmp_path: Path,
) -> None:
    turns = [
        [
            {"thinking": "The user wants a deployment."},
            {"text": "I deploy now."},
            {"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "deployed"},
            {"text": "Deployed."},
        ]
    ]
    async with served(tmp_path, turns) as run:
        app = CaluApp(
            protocol_connector(run.address, run.token), TuiOptions(bell=False), live=False
        )
        async with app.run_test(size=(120, 36), notifications=False) as pilot:
            model = app.model
            await until(pilot, lambda: model.connection.status == "attached", "the attach")
            assert model.state == "stopped" and app.capabilities.has("side_question")

            await pilot.press(*"/start", "enter")
            await until(pilot, lambda: model.state == "idle", "the session to start")
            await pilot.press(*"deploy", "space", *"it", "enter")
            await until(pilot, lambda: bool(model.decisions), "the escalation")
            card = app.query_one("#card").text  # type: ignore[attr-defined]
            assert card.startswith("DECISION 1 of 1  ·  main  ·  Bash")
            assert "deploy # calu:escalate" in card
            text = app.transcript.all_text()
            assert text[0].startswith("▌ you  deploy it")
            assert "  ┊ The user wants a deployment." in text and "  I deploy now." in text

            # A side question while the step waits: answered, and nothing else changes.
            await pilot.press("f3", *"why", "enter")
            await until(pilot, lambda: bool(model.side) and not model.side[0].pending, "the answer")
            assert model.side[0].answer == "Side answer to: why"
            runner = run.session.runner
            assert isinstance(runner, FakeRunner) and runner.side_questions == [("why", [])]
            assert len(model.decisions) == 1 and model.state == "escalated"
            assert [entry.text for entry in model.queue] == []

            await pilot.press("escape", "w", "w")
            await until(pilot, lambda: type(app.screen).__name__ == "DetailScreen", "the detail")
            await until(
                pilot,
                lambda: "the conversation as the checker saw it" in app.screen.text,  # type: ignore[attr-defined]
                "the checker's context",
            )
            assert "deploy it" in app.screen.text  # type: ignore[attr-defined]
            await pilot.press("a", "a")
            await until(pilot, lambda: model.state == "idle" and model.usage.turns == 1, "the end")
            text = app.transcript.all_text()
            assert any(row.startswith("  ALLOWED   Bash") for row in text)
            assert "  Deployed." in text and any("── turn 1" in row for row in text)
            assert not model.decisions and type(app.screen).__name__ != "DetailScreen"
            await until(pilot, lambda: model.usage.context is not None, "the usage figures")
            assert "ctx 21%" in app.query_one("#status").text  # type: ignore[attr-defined]

            await pilot.press(*"/model", "space", *"opus", "enter")
            await until(pilot, lambda: runner.model == "opus", "the model change")
            await pilot.press("ctrl+d")
        assert run.session.state == "idle", "detaching leaves the session running"


def test_calu_attach_uses_the_line_client_unless_a_terminal_is_there(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from calu.tui import start

    def terminal(stdin: bool, stdout: bool) -> None:
        """Pretend that standard input and output are (not) terminals."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout, raising=False)

    monkeypatch.delenv("CALU_ATTACH", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal(True, True)
    assert wants_full_screen(plain=False)
    assert not wants_full_screen(plain=True)
    terminal(False, True)
    assert not wants_full_screen(plain=False), "a pipe into calu attach"
    terminal(True, False)
    assert not wants_full_screen(plain=False), "calu attach into a pipe"
    terminal(True, True)
    monkeypatch.setenv("TERM", "dumb")
    assert not wants_full_screen(plain=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("CALU_ATTACH", "plain")
    assert not wants_full_screen(plain=False)
    monkeypatch.delenv("CALU_ATTACH")
    monkeypatch.setattr(start, "problem", lambda: start.NOT_INSTALLED)
    assert capsys.readouterr().err == ""
    assert not wants_full_screen(plain=False)
    assert "pip install 'calu[tui]'" in capsys.readouterr().err


def test_cmd_attach_starts_the_chosen_client_and_prints_what_is_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from calu import cli
    from calu.cli import build_parser

    started: list[tuple[str, str, int]] = []

    def run_tui(address: str, token: str, after_seq: int = 0) -> TuiExit:
        """Stand in for the full-screen client."""
        started.append((address, token, after_seq))
        return TuiExit(0, "escalated", "abc", 2)

    async def attach(address: str, token: str, after_seq: int, thinking: bool) -> int:
        """Stand in for the line client."""
        started.append(("line", address, after_seq))
        return 0

    monkeypatch.setattr(cli.tui_start, "run_tui", run_tui)
    monkeypatch.setattr(cli, "attach", attach)
    arguments = ["attach", "ws://127.0.0.1:1", "--token", "t", "--after-seq", "7"]
    args: argparse.Namespace = build_parser().parse_args(arguments)
    monkeypatch.setattr(cli.tui_start, "wants_full_screen", lambda plain: not plain)
    cli.cmd_attach(args, store=None)  # type: ignore[arg-type]
    assert started == [("ws://127.0.0.1:1", "t", 7)]
    assert capsys.readouterr().out.splitlines() == [
        "detached; the session keeps running",
        "session abc: escalated",
        "2 steps wait for a decision",
        "attach again with: calu attach   (the line client: calu attach --plain)",
    ]
    args = build_parser().parse_args([*arguments, "--plain"])
    cli.cmd_attach(args, store=None)  # type: ignore[arg-type]
    assert started[-1] == ("line", "ws://127.0.0.1:1", 7)


def test_the_lines_after_the_client_say_why_a_connection_ended() -> None:
    lines = exit_lines(TuiExit(1, "idle", None, 1, "the client token was revoked"))
    assert lines[:3] == [
        "the session closed the connection: the client token was revoked",
        "session no Claude session yet: idle",
        "1 step waits for a decision",
    ]
