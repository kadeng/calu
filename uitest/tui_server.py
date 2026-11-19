"""The scripted server of `fake_server.py` with two more scenarios, for the terminal clients.

It adds to `fake_server.SCENARIOS` and then runs `fake_server.main`, so the command line and
everything else are those of `fake_server.py`::

    uv run python uitest/tui_server.py --insecure --max-sessions 20 </dev/null

The scenarios (first message `scenario: <name>`):

    many-subagents  ten subagents at the same time. Two of them ("Deploy to staging", "Publish
                    the package") escalate in the same moment after 4 s. "Migrate the database
                    schema" reaches an escalating command after 8 s; calu holds that call while
                    another escalation is open, so its card appears when the first two are
                    decided. "Choose the colour scheme" asks a question after 10 s (a question
                    is not held). The other six stream text slowly and run commands of 2 s;
                    their commands wait as well while an escalation is open. The turn ends when
                    every subagent has reported.
    long            sixty turns of thirteen items each (thinking, streamed text, a Read, an
                    Edit, six commands, text with a code block); every tenth turn has a result
                    of 200 lines. Send the sixty messages yourself, or all at once: a message
                    sent during a turn starts the next one.

    control         text, a command and a result that hold terminal control characters: an
                    escape sequence that would colour, clear the screen or set the window
                    title, a carriage return that would overwrite the start of the line, a
                    backspace, and a bidirectional override. The command escalates. A client
                    must show each of them as a visible sign and obey none.
    echo            no scripted turn: every message is answered with "Echo: <message>". For
                    filling the list of stored conversations: the words after `echo` tell
                    them apart, e.g. `scenario: echo payment refactoring`.

`agent/records/notes/test-plan.md` section C says how the terminal clients were tested with these.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import fake_server  # the sibling module: this file is run as a script from `uitest/`
from fake_server import bash, edit

from calu.claude.fake_runner import FakeScript

LONG_TURNS = 60
"""Scenario `long`: how many scripted turns it has."""

SLOW_TEXT = " ".join(
    f"Part {n}: I compare the old and the new behaviour of the module and note what differs."
    for n in range(1, 9)
)
"""Scenario `many-subagents`: what a working subagent streams, 0.25 s per word (about 30 s)."""

JOBS: tuple[tuple[str, str], ...] = (
    ("Review the parser", "work"),
    ("Run the unit tests", "work"),
    ("Deploy to staging", "escalate"),
    ("Publish the package", "escalate"),
    ("Profile the start-up", "work"),
    ("Update the changelog", "work"),
    ("Choose the colour scheme", "question"),
    ("Audit the dependencies", "work"),
    ("Migrate the database schema", "late"),
    (
        "Translate the error messages into German, French and Spanish, keeping the placeholders",
        "work",
    ),
)
"""Scenario `many-subagents`: each subagent's description and what it does (see `_job`)."""

COLOUR_QUESTION: list[dict[str, Any]] = [
    {
        "question": "Which colour scheme should the report use?",
        "header": "Colours",
        "multiSelect": False,
        "options": [
            {"label": "Light", "description": "Dark text on a white page."},
            {"label": "Dark", "description": "Light text on a dark page."},
        ],
    }
]
"""Scenario `many-subagents`: the question one subagent asks."""


def _job(number: int, description: str, kind: str, workspace: Path) -> dict[str, Any]:
    """Build one subagent of the scenario `many-subagents`.

    Args:
        number: Its number among the ten, counted from 1; it appears in its commands.
        description: The subagent's description.
        kind: "work" (slow text, then commands of 2 s), "escalate" (an escalating command after
            4 s), "late" (an escalating command after 8 s) or "question" (a question after
            10 s). Each kind ends with two ordinary commands and a closing text.
        workspace: The project directory of the session.

    Returns:
        The Agent action's specification.
    """
    actions: list[dict[str, Any]] = [{"text": f"Starting: {description}.", "stream": True}]
    if kind == "escalate":
        actions += [{"sleep": 4}, bash(f"./deploy.sh job-{number}  # calu:escalate", "deployed")]
    elif kind == "late":
        actions += [{"sleep": 8}, bash(f"./migrate.sh job-{number}  # calu:escalate", "migrated")]
    elif kind == "question":
        actions += [
            {"sleep": 10},
            {"tool": "AskUserQuestion", "input": {"questions": COLOUR_QUESTION}},
        ]
    else:
        actions += [{"text": SLOW_TEXT, "stream": True, "delay": 0.25}]
    actions += [
        bash(f"make job-{number}-a", "ok", duration=2),
        bash(f"make job-{number}-b", "ok", duration=2),
        {"text": f"Finished: {description}."},
    ]
    return {
        "description": description,
        "prompt": f"{description}, in {workspace}. Report what you changed.",
        "subagent_type": "Explore" if number % 3 == 0 else "general-purpose",
        "actions": actions,
        "result": f"{description}: done.",
    }


def many_subagents(workspace: Path) -> FakeScript:
    """Build the scenario `many-subagents` (see the module docstring).

    Args:
        workspace: The project directory of the session.

    Returns:
        The script: one turn with ten subagents at the same time, and a closing turn.
    """
    agents = [
        [{"agent": _job(number, description, kind, workspace)}]
        for number, (description, kind) in enumerate(JOBS, start=1)
    ]
    first: list[dict[str, Any]] = [
        {"text": "Ten jobs at the same time, one subagent each.", "stream": True},
        {"parallel": agents},
        {"text": "All ten subagents have reported."},
    ]
    return FakeScript(turns=[first, [{"text": "Nothing is left to do."}]])


def long(workspace: Path) -> FakeScript:
    """Build the scenario `long`: `LONG_TURNS` turns of thirteen items each.

    Args:
        workspace: The project directory of the session.

    Returns:
        The script.
    """

    def turn(number: int) -> list[dict[str, Any]]:
        """Build one turn.

        Args:
            number: The turn's number, counted from 1; it appears in every text and command.

        Returns:
            The actions.
        """
        result = (
            "\n".join(f"{line:04d} log line of turn {number}" for line in range(1, 201))
            if number % 10 == 0
            else f"turn {number}: 3 passed"
        )
        words = " ".join(f"step{number}-{word}" for word in range(1, 61))
        return [
            {"thinking": f"Turn {number}: the user wants the next part of the migration."},
            {"text": f"Turn {number} of {LONG_TURNS}. {words}", "stream": True},
            {
                "tool": "Read",
                "input": {"file_path": str(workspace / fake_server.EDITED_FILE)},
                "result": "…",
            },
            edit(workspace, new=f"def greet_turn_{number}("),
            *[
                bash(f"make part-{number}-{n}", f"part {n} of turn {number} ok")
                for n in range(1, 6)
            ],
            bash(f"python -m pytest -q tests/test_part_{number}.py", result),
            {
                "text": f"Turn {number} is done.\n\n```python\nPART = {number}\n```\n\n"
                f"The marker of this turn is needle-{number:03d}."
            },
        ]

    return FakeScript(turns=[turn(number) for number in range(1, LONG_TURNS + 1)])


def control(_workspace: Path) -> FakeScript:
    """Build the scenario `control` (see the module docstring).

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The script: one turn.
    """
    esc = "\x1b"
    text = (
        f"Coloured: {esc}[31mred{esc}[0m. Title: {esc}]0;owned{chr(7)}. "
        f"Overwritten: harmless\rDANGER. Erased: abc{chr(8)}{chr(8)}{chr(8)}xyz. "
        "Override: \u202etxt.exe\u202c. End of the text."
    )
    command = f"echo safe {esc}[2K\rrm -rf build  # calu:escalate \u202egnp.sh"
    result = f"line one{esc}[2J{esc}[H\nline two\rXX\n{esc}[?1049l"
    return FakeScript(
        turns=[[{"text": text}, bash("printf ok", result), bash(command, result), {"text": "End."}]]
    )


def echo(_workspace: Path) -> FakeScript:
    """Build the scenario `echo`: no scripted turn, so every message is echoed.

    Args:
        _workspace: The project directory of the session; unused.

    Returns:
        The empty script.
    """
    return FakeScript(turns=[])


fake_server.SCENARIOS.update(
    {"many-subagents": many_subagents, "long": long, "control": control, "echo": echo}
)

if __name__ == "__main__":
    sys.exit(fake_server.main())
