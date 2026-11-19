"""A `calu serve` stand-in that plays scripted sessions, for UI tests of the editor plugins.

It runs the real `calu.serve.Server` (the same code path as `calu serve`: it claims the session
requests of this host and user from the database and runs each session as a task), but every
session gets a `calu.claude.FakeRunner` in place of Claude Code and rule-based fake models in
place of the checkers (`calu.testing.fake_models`). So no `claude`, no API key and no network
are needed, and the VSCode extension's "New session" plays the conversation below.

Run it inside the UI-testing desktop, from a checkout that has calu's dependencies::

    uv run python uitest/fake_server.py                  # session login from the credential store
    uv run python uitest/fake_server.py --dsn postgresql://calu_session:...@127.0.0.1/calu
    uv run python uitest/fake_server.py </dev/null       # a blank session password, no prompt

The session login is the `session` entry of the credential store (`calu setup` wrote it). If it
is protected by a password, the password is asked on the terminal, or read as one line from
stdin when stdin is no terminal. `--dsn` skips the store.

A session plays one scripted turn per user message. The first message chooses the scenario:
`scenario: <name>` (anything else plays `tour`). After the last scripted turn every message is
answered with "Echo: <message>".

    tour         turn 1: thinking; streamed Markdown with a code block; Read and Edit of
                 `greeter.py` (the edit is really written, so the diff view has a before and
                 an after); a command that passes, one the fake checker allows, one it denies,
                 one it escalates; a foreground and a background subagent; a usage limit; a
                 question with options; the closing text.
                 turn 2: text, then after 4 s a command that escalates (time to hide the chat
                 view and see the notification). turn 3: the same with a question.
    subagents    turn 1: a foreground subagent with text and two tool calls; two subagents at
                 the same time, one of which raises an escalation; a subagent that fails; a
                 background subagent that keeps running for a minute after the turn ended (the
                 turn result reports one background task). turn 2 starts by itself when it
                 finishes, as Claude Code's follow-up turn does.
    stop         for interrupts. turn 1: a text streamed slowly for a minute. turn 2: a
                 command that runs for a minute. turn 3: a command that escalates.
    queue        for messages sent while Claude works. turns 1 to 3: five commands of 3 s
                 each; turn 4: a command that escalates.
    levels       for the supervision level. Every turn: a Read, an Edit and a command that
                 the checkers pass.
    edge         turn 1: a line of 3,000 characters, a tool result of 60,000 characters, a
                 code block of 300 lines, an empty text, raw HTML and a `javascript:` link, a
                 failed tool call, text in several scripts. turn 2: a question with several
                 choices.
    many         one turn of 320 edits and 200 commands (more items than the views keep
                 diffs for).
    escalations  three commands at the same time, each of which escalates.
    bidi         text, a file name and a command that contain bidirectional control
                 characters (a client must show them, not obey them), next to ordinary
                 Arabic and Hebrew paragraphs (which must read right to left); the command
                 escalates, so the approval card shows it too.
    tall         a command of 90 lines that escalates: an approval card taller than a view.
    swarm        ten subagents at the same time, each with eight commands of 3 s; the third
                 escalates after about 6 s and the seventh after about 15 s while the others
                 keep working. turn 2: three background subagents that outlive the turn.
    nousage      as `levels`, but the session reports no context usage.
    honesty      turn 1: an edit that the fake honesty checker calls dishonest and a test
                 run; turn 2: a correcting edit and the test run again. Needs the honesty
                 checker enabled (`calu checker set honesty --enable`) and `honesty.mode` track
                 or enforce.

A message that begins with "/" is answered with a line that names the command: Claude Code's
own commands (`/compact`, `/usage`, `/clear`) are not played.

The context usage grows with every turn (42k, 63k, 84k ... of 200k tokens), and every turn
reports a five-hour limit.

The fake checkers decide by markers in the step (`calu.testing`): the scripted commands carry
`# calu:allow`, `# calu:deny` or `# calu:escalate` as a shell comment.

What the scripted runner does not play (`calu.claude.fake_runner`): a resumed or forked
session starts its script from the beginning (the runner has no transcript; the turns the
session inherits are real rows of the database, so `session.history` returns them and a
client can show them), a message to a subagent is not acted on (the session asks the main
agent to relay it, and the script has no answer to that), and a "next" message is taken in
after the running action, not inside a tool call.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from calu.claude import FakeRunner
from calu.claude.fake_runner import FakeScript
from calu.cli import CliError, load_credential
from calu.credentials import CredentialError, CredentialStore
from calu.protocol.messages import ContextUsage
from calu.serve import ServeOptions, serve
from calu.testing import DISHONEST_MARKER, fake_models

if TYPE_CHECKING:
    from collections.abc import Callable

    from calu.claude.runner import EventSink, ToolGate
    from calu.config import ClaudeConfig

EDITED_FILE = "greeter.py"
"""The project file the scripted Edit changes, relative to the project directory."""

GREETINGS = ("Hello", "Good day")
"""The two words the scripted Edit swaps: it replaces the one the file holds with the other, so
that every session finds something to change."""

INTRO = """I will rework the greeting and run the tests.

## Plan

1. Read `greeter.py`
2. Change the greeting
3. Run the tests, then **push**

```python
def greet(name: str) -> str:
    return f"Good day, {name}!"
```

The table of what I touch:

| file | change |
|---|---|
| `greeter.py` | the greeting word |
| `test_greeter.py` | nothing |
"""
"""The streamed Markdown text of the first turn: heading, list, code block, table."""

QUESTIONS: list[dict[str, Any]] = [
    {
        "question": "Which greeting should the function use from now on?",
        "header": "Greeting",
        "multiSelect": False,
        "options": [
            {"label": "Good day", "description": "Formal, works at any time of the day."},
            {"label": "Hello", "description": "The greeting the project started with."},
            {"label": "Hi", "description": "Short and informal."},
        ],
    }
]
"""The question of the first turn (`AskUserQuestion`): one choice of three."""

FOLLOW_UP_QUESTIONS: list[dict[str, Any]] = [
    {
        "question": "Which checks should run before each commit?",
        "header": "Checks",
        "multiSelect": True,
        "options": [
            {"label": "ruff", "description": "Format and lint."},
            {"label": "pyright", "description": "Type check."},
            {"label": "pytest", "description": "The unit tests."},
        ],
    }
]
"""The question of the third turn: several choices."""


def swap_greeting(text: str) -> tuple[str, str]:
    """Choose which greeting word the scripted Edit replaces in a file's text.

    Args:
        text: The content of `EDITED_FILE`.

    Returns:
        `(old, new)`: the word of `GREETINGS` that occurs in `text` and the other one. If
        neither occurs, the first word is `old` (the scripted Edit then fails like an edit
        whose `old_string` is not in the file).
    """
    first, second = GREETINGS
    if second in text and first not in text:
        return second, first
    return first, second


def tour(workspace: Path) -> FakeScript:
    """Build the scenario `tour` for one session in `workspace`.

    The Edit of the first turn is built from the current content of `EDITED_FILE`, so that it
    applies in every new session of the same project.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: three turns (see the module docstring).
    """
    target = workspace / EDITED_FILE
    try:
        before = target.read_text(encoding="utf-8")
    except OSError:
        before = ""
    old, new = swap_greeting(before)
    old_line, new_line = f'return f"{old}, {{name}}!"', f'return f"{new}, {{name}}!"'
    after = before.replace(old_line, new_line)
    first_turn: list[dict[str, Any]] = [
        {"thinking": "The user wants another greeting. The function is in greeter.py."},
        {"text": INTRO, "stream": True},
        {"tool": "Read", "input": {"file_path": str(target)}, "result": before},
        {
            "tool": "Edit",
            "input": {"file_path": str(target), "old_string": old_line, "new_string": new_line},
            "writes": {EDITED_FILE: after} if after else {},
            "result": f"The file {target} has been updated.",
        },
        {
            "tool": "Bash",
            "input": {"command": "python -m pytest -q", "description": "Run the tests"},
            "result": "..                                                       [100%]\n2 passed",
        },
        {
            "tool": "Bash",
            "input": {"command": "git status --short  # calu:allow"},
            "result": f" M {EDITED_FILE}",
        },
        {"tool": "Bash", "input": {"command": "rm -rf build dist  # calu:deny"}},
        {
            "tool": "Bash",
            "input": {"command": "git push origin main  # calu:escalate"},
            "result": "To example.org:demo.git\n   1a2b3c4..5d6e7f8  main -> main",
        },
        {
            "agent": {
                "description": "Review the greeter",
                "prompt": "Read greeter.py and say whether the greeting is used consistently.",
                "subagent_type": "general-purpose",
                "actions": [
                    {"text": "I will search for the greeting.", "stream": True},
                    {
                        "tool": "Grep",
                        "input": {"pattern": new, "path": str(workspace)},
                        "result": f"{EDITED_FILE}:3:    {new_line}",
                    },
                    {"text": f"`{EDITED_FILE}` uses the greeting in one place."},
                ],
                "result": f"The greeting is used consistently: one place in `{EDITED_FILE}`.",
            }
        },
        {
            "agent": {
                "description": "Watch the test run",
                "prompt": "Run the tests in the background and report failures.",
                "subagent_type": "general-purpose",
                "background": True,
                "actions": [
                    {"tool": "Bash", "input": {"command": "python -m pytest -q"}, "result": "ok"},
                    {"text": "No failures."},
                ],
                "result": "The tests pass.",
            }
        },
        {"rate_limit": {"type": "five_hour", "status": "allowed", "utilization": 0.37}},
        {"tool": "AskUserQuestion", "input": {"questions": QUESTIONS}},
        {"text": f"Done: `{EDITED_FILE}` now says **{new}**. The push went through."},
    ]
    second_turn: list[dict[str, Any]] = [
        {"text": "I will tag the release. That needs your approval in a moment.", "stream": True},
        {"sleep": 4},
        {
            "tool": "Bash",
            "input": {"command": "git tag v1.0 && git push origin v1.0  # calu:escalate"},
            "result": " * [new tag]         v1.0 -> v1.0",
        },
        {"text": "The tag is pushed."},
    ]
    third_turn: list[dict[str, Any]] = [
        {"text": "One question before I set up the commit hook.", "stream": True},
        {"sleep": 4},
        {"tool": "AskUserQuestion", "input": {"questions": FOLLOW_UP_QUESTIONS}},
        {"text": "The hook is set up."},
    ]
    return FakeScript(turns=[first_turn, second_turn, third_turn])


def bash(command: str, result: str = "ok", **more: Any) -> dict[str, Any]:
    """Build a scripted Bash call.

    Args:
        command: The command line; a `# calu:<decision>` comment steers the fake checker.
        result: The output of the command.
        **more: Further keys of the action, e.g. `duration` or `fails`.

    Returns:
        The action.
    """
    return {"tool": "Bash", "input": {"command": command}, "result": result, **more}


def edit(workspace: Path, old: str = "def greet(", new: str = "def greet (") -> dict[str, Any]:
    """Build a scripted Edit of `EDITED_FILE` that is checked and shown but not written.

    Args:
        workspace: The project directory.
        old: Text that occurs in the file.
        new: Its replacement in the diff.

    Returns:
        The action. The session computes the diff from the file as it is, so the same action
        can be played any number of times.
    """
    path = str(workspace / EDITED_FILE)
    return {
        "tool": "Edit",
        "input": {"file_path": path, "old_string": old, "new_string": new},
        "result": f"The file {path} has been updated.",
    }


def subagents(workspace: Path) -> FakeScript:
    """Build the scenario `subagents` (see the module docstring).

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: a turn with five subagents and the turn that follows the background one.
    """
    reviewer = {
        "description": "Review the greeter",
        "prompt": "Read greeter.py and say whether the greeting is used consistently.",
        "subagent_type": "general-purpose",
        "actions": [
            {"text": "I will read the file and search for the greeting.", "stream": True},
            {"tool": "Read", "input": {"file_path": str(workspace / EDITED_FILE)}, "result": "…"},
            {"tool": "Grep", "input": {"pattern": "greet", "path": str(workspace)}, "result": "3"},
            {"text": "The greeting is used in one place."},
        ],
        "result": "The greeting is used consistently.",
    }
    tester = {
        "description": "Run the tests",
        "prompt": "Run the unit tests and report.",
        "subagent_type": "general-purpose",
        "actions": [
            {"text": "Running the tests.", "stream": True, "delay": 0.2},
            bash("python -m pytest -q", "2 passed", duration=3),
            {"text": "Both tests pass."},
        ],
        "result": "2 passed.",
    }
    publisher = {
        "description": "Publish the package",
        "prompt": "Build the package and upload it.",
        "subagent_type": "general-purpose",
        "actions": [
            {"text": "Building, then uploading.", "stream": True, "delay": 0.2},
            bash("python -m build", "built demo-1.0", duration=2),
            bash("twine upload dist/*  # calu:escalate", "uploaded"),
            {"text": "The upload is done."},
        ],
        "result": "demo-1.0 is published.",
    }
    broken = {
        "description": "Check the links",
        "prompt": "Check every link in README.md.",
        "subagent_type": "Explore",
        "fails": True,
        "actions": [
            {"text": "Fetching the links."},
            bash(
                "curl -sI https://example.invalid", "curl: (6) Could not resolve host", fails=True
            ),
        ],
        "result": "The subagent stopped: the network is not reachable.",
    }
    watcher = {
        "description": "Watch the deployment",
        "prompt": "Poll the deployment every few seconds and report when it is healthy.",
        "subagent_type": "general-purpose",
        "background": True,
        "detach": True,
        "actions": [
            step
            for n in range(1, 6)
            for step in (
                {"sleep": 12},
                bash(f"curl -s https://example.org/health  # poll {n}", '{"status": "starting"}'),
            )
        ]
        + [{"text": "The deployment is healthy."}],
        "result": "Healthy after five polls.",
    }
    first: list[dict[str, Any]] = [
        {"text": "I will hand the review to a subagent.", "stream": True},
        {"agent": reviewer},
        {"text": "Now the tests and the upload at the same time.", "stream": True},
        {"parallel": [[{"agent": tester}], [{"agent": publisher}]]},
        {"agent": broken},
        {"text": "The link check failed. I start a watcher in the background."},
        {"agent": watcher},
        {"text": "The watcher runs in the background; I will report when it is done."},
    ]
    second: list[dict[str, Any]] = [
        {"text": "The background watcher finished: the deployment is healthy.", "stream": True}
    ]
    return FakeScript(turns=[first, second])


def stop(_workspace: Path) -> FakeScript:
    """Build the scenario `stop`: three turns that wait to be interrupted.

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The script.
    """
    essay = " ".join(f"word{n}" for n in range(1, 401))
    return FakeScript(
        turns=[
            [{"text": f"A long answer: {essay}", "stream": True, "delay": 0.15}, {"text": "End."}],
            [
                {"text": "I start the long build."},
                bash("make all", "built", duration=60),
                {"text": "The build is done."},
            ],
            [
                {"text": "I will deploy."},
                bash("./deploy.sh production  # calu:escalate", "deployed"),
                {"text": "Deployed."},
            ],
        ]
    )


def queue(_workspace: Path) -> FakeScript:
    """Build the scenario `queue`: turns that take 15 s, to send messages into.

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The script.
    """
    busy = [
        {"text": "Five steps of three seconds each.", "stream": True},
        *[bash(f"make step-{n}", f"step {n} done", duration=3) for n in range(1, 6)],
        {"text": "All five steps are done."},
    ]
    waiting = [bash("./deploy.sh  # calu:escalate", "deployed"), {"text": "Deployed."}]
    return FakeScript(turns=[busy, busy, busy, waiting])


def levels(workspace: Path) -> FakeScript:
    """Build the scenario `levels`: the same three checked steps in every turn.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: eight equal turns.
    """
    turn = [
        {"text": "Read, edit, test.", "stream": True},
        {"tool": "Read", "input": {"file_path": str(workspace / EDITED_FILE)}, "result": "…"},
        edit(workspace),
        bash("python -m pytest -q", "2 passed"),
        {"text": "Done."},
    ]
    return FakeScript(turns=[turn] * 8)


def honesty(workspace: Path) -> FakeScript:
    """Build the scenario `honesty`: an edit the fake honesty checker calls dishonest, then its fix.

    Needs the honesty checker enabled and `honesty.mode` "track" or "enforce" (the chat's
    honesty menu sets the mode); the fake checker answers DISHONEST for a step that contains
    `calu.testing.DISHONEST_MARKER` and HONEST for every other edit.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: turn 1 edits `EDITED_FILE` with the marker and runs the tests (denied in
        "enforce" while the mark is open); turn 2 edits it again without the marker, which
        clears the mark, and runs the tests again; later turns repeat turn 2.
    """
    doc = '"""Return the greeting for `name`."""'
    dishonest = [
        {"text": "I change the docstring. The fake honesty checker calls this edit dishonest."},
        edit(workspace, doc, f"{doc}  # {DISHONEST_MARKER}"),
        bash("python -m pytest -q", "2 passed"),
        {"text": "The honesty chip shows the open mark."},
    ]
    fixed = [
        {"text": "I correct the edit."},
        edit(workspace),
        bash("python -m pytest -q", "2 passed"),
        {"text": "The mark is cleared."},
    ]
    return FakeScript(turns=[dishonest, fixed, fixed, fixed])


RICH_TEXT = """Raw HTML must stay text: <b>bold?</b> <img src=x onerror=alert(1)>
<script>alert(2)</script>

Links: [a javascript link](javascript:alert(3)), [a data link](data:text/html,<b>x</b>),
[a web link](https://example.org/docs) and a file, `greeter.py:3`.

Scripts and directions: Grüße, 你好, こんにちは, مرحبا بالعالم, שלום עולם, 👩‍💻🚀✅, á (combining),
and a right-to-left override: ‮evil‬.
"""
"""Text of the scenario `edge` that a renderer must not execute or be confused by."""


def edge(workspace: Path) -> FakeScript:
    """Build the scenario `edge` (see the module docstring).

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: the turn of unusual content and a turn with a multiple-choice question.
    """
    long_line = "A line without a break: " + "x" * 1500 + " and " + "/very/long/path" * 100
    code = "\n".join(f"line_{n} = {n}  # {'-' * (n % 60)}" for n in range(1, 301))
    huge = "\n".join(f"{n:05d} " + "output " * 12 for n in range(1, 701))
    first: list[dict[str, Any]] = [
        {"text": long_line},
        {"text": f"A code block of 300 lines:\n\n```python\n{code}\n```\n"},
        {"text": ""},
        {"text": RICH_TEXT, "stream": True},
        bash("cat build.log", huge),
        bash("x" * 400 + " --flag " + "y" * 400, "ok"),
        bash(
            "python -m pytest -q tests/missing", "ERROR: file not found: tests/missing", fails=True
        ),
        {
            "tool": "Edit",
            "input": {
                "file_path": str(workspace / EDITED_FILE),
                "old_string": "this text is not in the file",
                "new_string": "x",
            },
            "fails": True,
            "result": "String to replace not found in file.",
        },
        {"text": "That was the unusual content."},
    ]
    second: list[dict[str, Any]] = [
        {"tool": "AskUserQuestion", "input": {"questions": [*FOLLOW_UP_QUESTIONS, *QUESTIONS]}},
        {"text": "Thank you."},
    ]
    return FakeScript(turns=[first, second])


def many(workspace: Path) -> FakeScript:
    """Build the scenario `many`: one turn of 320 edits and 200 commands.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script.
    """
    edits = [edit(workspace, new=f"def greet_{n}(") for n in range(1, 321)]
    commands = [bash(f"echo {n}", str(n)) for n in range(1, 201)]
    return FakeScript(turns=[[{"text": "520 steps follow."}, *edits, *commands, {"text": "End."}]])


def escalations(_workspace: Path) -> FakeScript:
    """Build the scenario `escalations`: three escalating commands at the same time.

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The script.
    """
    calls = [
        [bash(f"./deploy.sh region-{n}  # calu:escalate", f"region {n} ok")] for n in (1, 2, 3)
    ]
    return FakeScript(
        turns=[[{"text": "Three deployments at once."}, {"parallel": calls}, {"text": "Done."}]]
    )


BIDI_TEXT = (
    "Text with bidirectional control characters; each must be visible as a placeholder:\n\n"
    "- an override inside a file name: `report_\u202egpj.exe`\n"
    "- an isolate around a word: before \u2067inside\u2069 after\n"
    "- marks: left\u200e right\u200f arabic\u061c end\n\n"
    "Ordinary right-to-left text must read as usual:\n\n"
    "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645\u060c "
    "\u0647\u0630\u0627 \u0646\u0635 \u0639\u0631\u0628\u064a.\n\n"
    "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd, "
    "\u05d6\u05d4 \u05d8\u05e7\u05e1\u05d8 \u05d1\u05e2\u05d1\u05e8\u05d9\u05ea.\n\n"
    "Mixed: the file `greeter.py` \u05d4\u05d5\u05d0 \u05e7\u05d5\u05d1\u05e5 (is a file).\n"
)
"""Markdown of the scenario `bidi`: control characters to show, and ordinary Arabic and Hebrew."""


def bidi(workspace: Path) -> FakeScript:
    """Build the scenario `bidi` (see the module docstring).

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: one turn with the text, a Read of a file whose name holds an override, a
        command with an override that passes and one that escalates.
    """
    turn: list[dict[str, Any]] = [
        {"text": BIDI_TEXT},
        {
            "tool": "Read",
            "input": {"file_path": str(workspace / "photo_\u202egnp.sh")},
            "fails": True,
            "result": "File does not exist.",
        },
        bash("ls -l invoice_\u202efdp.exe", "-rwxr-xr-x 1 cua cua 0 invoice_\u202efdp.exe"),
        bash("./run.sh \u202etxt.yolped  # calu:escalate", "done"),
        {"text": "That was the text with control characters."},
    ]
    return FakeScript(turns=[turn])


def tall(_workspace: Path) -> FakeScript:
    """Build the scenario `tall`: one escalating command of 90 lines.

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The script.
    """
    lines = "\n".join(f"echo step {n} of the migration" for n in range(1, 91))
    command = f"bash -s <<'SCRIPT'  # calu:escalate\n{lines}\nSCRIPT"
    return FakeScript(
        turns=[[{"text": "A long script follows."}, bash(command, "ok"), {"text": "Done."}]]
    )


SWARM_JOBS = (
    "Review the parser",
    "Run the unit tests",
    "Publish the package",
    "Check the documentation links",
    "Profile the start-up",
    "Update the changelog",
    "Migrate the database schema",
    "Audit the dependencies",
    "Rebuild the search index",
    "Translate the error messages into German, French and Spanish, keeping the placeholders",
)
"""What the ten subagents of the scenario `swarm` are asked to do (the last one is long, for
the layout of a list of subagents)."""

SWARM_ESCALATES = {2: 2, 6: 5}
"""Scenario `swarm`: the index of each subagent that escalates, with the number of ordinary
commands it runs first (3 s each), so the two escalations open about nine seconds apart."""


def swarm(workspace: Path) -> FakeScript:
    """Build the scenario `swarm`: ten subagents at the same time, two of which escalate.

    Each subagent streams a line, runs eight commands of 3 s and reports. The third subagent
    escalates after about 6 s and the seventh after about 15 s, while the other eight keep
    working; the turn ends when every subagent has finished, so both escalations have to be
    decided. Turn 2 starts three background subagents that outlive it.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script.
    """

    def worker(index: int, job: str) -> dict[str, Any]:
        """Build one subagent of the first turn.

        Args:
            index: Its position among the ten, counted from 0.
            job: Its description.

        Returns:
            The Agent action's specification.
        """
        actions: list[dict[str, Any]] = [{"text": f"Starting: {job}.", "stream": True}]
        for step in range(1, 9):
            if SWARM_ESCALATES.get(index) == step - 1:
                actions.append(bash(f"deploy --job {index + 1}  # calu:escalate", "deployed"))
            actions.append(
                bash(f"work --job {index + 1} --step {step}", f"step {step} ok", duration=3)
            )
        actions.append({"text": f"Finished: {job}."})
        return {
            "description": job,
            "prompt": f"{job}, in {workspace}.",
            "subagent_type": "Explore" if index % 3 == 0 else "general-purpose",
            "actions": actions,
            "result": f"{job}: done.",
        }

    def background(index: int) -> dict[str, Any]:
        """Build one background subagent of the second turn.

        Args:
            index: Its number, counted from 1.

        Returns:
            The Agent action's specification.
        """
        polls = [
            step
            for n in range(1, 5)
            for step in ({"sleep": 10}, bash(f"poll --watcher {index} --round {n}", "pending"))
        ]
        return {
            "description": f"Watcher {index}",
            "prompt": f"Watch service {index} until it is healthy.",
            "subagent_type": "general-purpose",
            "background": True,
            "detach": True,
            "actions": [*polls, {"text": f"Service {index} is healthy."}],
            "result": f"Service {index} is healthy.",
        }

    first: list[dict[str, Any]] = [
        {"text": "Ten jobs at the same time, one subagent each.", "stream": True},
        {"parallel": [[{"agent": worker(i, job)}] for i, job in enumerate(SWARM_JOBS)]},
        {"text": "All ten subagents have reported."},
    ]
    second: list[dict[str, Any]] = [
        {"text": "Three watchers in the background.", "stream": True},
        *({"agent": background(n)} for n in (1, 2, 3)),
        {"text": "The watchers run; I report when they are done."},
    ]
    third: list[dict[str, Any]] = [{"text": "The watchers have finished.", "stream": True}]
    return FakeScript(turns=[first, second, third])


SCENARIOS: dict[str, Callable[[Path], FakeScript]] = {
    "tour": tour,
    "subagents": subagents,
    "stop": stop,
    "queue": queue,
    "levels": levels,
    "edge": edge,
    "many": many,
    "escalations": escalations,
    "bidi": bidi,
    "tall": tall,
    "swarm": swarm,
    "nousage": levels,
    "honesty": honesty,
}
"""The scenarios by the name a first message `scenario: <name>` chooses."""

CONTEXT_STEP = 21_000
"""Tokens the reported context usage grows by per turn."""

CONTEXT_MAX = 200_000
"""The reported size of the context window."""


def scenario_name(text: str) -> str | None:
    """Read the scenario a first message asks for.

    Args:
        text: The message, e.g. "scenario: subagents".

    Returns:
        The name in lower case (it may be unknown); None if the message does not start with
        "scenario:".
    """
    head, colon, rest = text.partition(":")
    if not colon or head.strip().lower() != "scenario":
        return None
    words = rest.split()
    return words[0].lower() if words else ""


def context_after(turns: int) -> ContextUsage:
    """Compute the context usage a session reports after a number of turns.

    Args:
        turns: How many turns have started; at least 1.

    Returns:
        42k tokens after the first turn and `CONTEXT_STEP` more per turn, at most 95 % of
        `CONTEXT_MAX`.
    """
    tokens = min(2 * CONTEXT_STEP + (turns - 1) * CONTEXT_STEP, CONTEXT_MAX * 95 // 100)
    return ContextUsage(
        tokens=tokens,
        max_tokens=CONTEXT_MAX,
        percent=round(100 * tokens / CONTEXT_MAX, 1),
        model="fake-model",
    )


class ScenarioRunner(FakeRunner):
    """A scripted runner whose first message chooses the scenario (see the module docstring).

    It differs from `FakeRunner` in three more points: its messages carry transcript uuids,
    so that the session records fork points; the context usage it reports grows per turn (no
    usage at all in the scenario `nousage`, and none before the first turn); a message that
    begins with "/" gets a fixed answer and uses up no scripted turn.
    """

    def __init__(self, workspace: Path) -> None:
        """Create a runner that plays `tour` unless the first message names another scenario.

        Args:
            workspace: The project directory of the session.
        """
        super().__init__(tour(workspace), workspace)
        self.transcript_uuids = True
        self.context = None
        self._started = 0
        self._no_usage = False

    async def _turn(self, text: str, gate: ToolGate, sink: EventSink) -> None:
        """Run the next turn, after choosing the scenario on the first message.

        Args:
            text: The user message that started the turn.
            gate: Asked before every tool call.
            sink: Receives the events.
        """
        self._started += 1
        name = scenario_name(text) if self._started == 1 else None
        if name is not None:
            build = SCENARIOS.get(name)
            if build is None:
                known = ", ".join(SCENARIOS)
                answer = f"There is no scenario `{name}`. The scenarios are: {known}."
                self._turns = iter([[{"text": answer}]])
            else:
                self._turns = iter(build(self.workspace).turns)
                self._no_usage = name == "nousage"
        if text.startswith("/"):
            command = text.split()[0]
            answer = f"(scripted session) Claude Code would run its command `{command}` here."
            self._turns = itertools.chain([[{"text": answer}]], self._turns)
        self.context = None if self._no_usage else context_after(self._started)
        self._turns = itertools.chain(
            [
                [
                    {"rate_limit": limit(self._started)},
                    *next(self._turns, [{"text": f"Echo: {text}"}]),
                ]
            ],
            self._turns,
        )
        await super()._turn(text, gate, sink)


def limit(turns: int) -> dict[str, Any]:
    """Build the usage limit a turn reports.

    Args:
        turns: How many turns have started.

    Returns:
        The five-hour limit, used 30 % plus 7 % per turn; "allowed_warning" from 80 % on.
    """
    used = min(0.3 + 0.07 * turns, 0.99)
    status = "allowed_warning" if used >= 0.8 else "allowed"
    return {"type": "five_hour", "status": status, "utilization": round(used, 2)}


def make_runner(workspace: Path, _config: ClaudeConfig, _hidden: set[str]) -> FakeRunner:
    """Create the scripted runner of one session (the server's `runner_factory`).

    Args:
        workspace: The project directory of the session.
        _config: Claude Code's settings for the session; unused.
        _hidden: Names of environment variables hidden from Claude Code; unused.

    Returns:
        A runner that plays the scenario its first message chooses.
    """
    return ScenarioRunner(workspace)


def session_login(dsn: str | None) -> str | bytearray:
    """Return the login the server opens its database connections with.

    Side effect: may ask for the session password (on the terminal, or one line of stdin).

    Args:
        dsn: A connection string given on the command line, or None to read the `session`
            entry of the credential store.

    Returns:
        The connection string or the stored record of the role `calu_session`.

    Raises:
        CliError: If the store has no `session` entry.
        CredentialError: If the password is wrong or the store cannot be read.
    """
    if dsn:
        return dsn
    return load_credential(CredentialStore(), "session", "Session password: ")


def main(argv: list[str] | None = None) -> int:
    """Run the scripted server until SIGINT or SIGTERM.

    Side effects: everything `calu.serve.serve` does (database connections, one task and one
    protocol endpoint per session, lines about the sessions on stdout); the scripted Edit
    writes `EDITED_FILE` in the project directory of each session.

    Args:
        argv: The command line without the program name; None reads `sys.argv`.

    Returns:
        The exit code: 0 after a stop by signal, 1 if the login is missing or refused.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--dsn", help="the calu_session connection string (default: the store)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="local clients of the sessions need no token (as `calu serve --insecure`)",
    )
    parser.add_argument("--max-sessions", type=int, default=10, help="sessions at once")
    args = parser.parse_args(argv)
    try:
        url = session_login(args.dsn)
    except (CliError, CredentialError) as exc:
        print(f"fake_server: {exc}", file=sys.stderr)
        return 1
    options = ServeOptions(max_sessions=args.max_sessions, insecure=args.insecure)
    print("fake_server: scripted sessions, fake checkers; no Claude Code is started", flush=True)
    return asyncio.run(
        serve(url, options, runner_factory=make_runner, checker_models=fake_models())
    )


if __name__ == "__main__":
    sys.exit(main())
