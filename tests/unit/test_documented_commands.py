"""Every `calu ...` command in the documentation has to parse.

Three bugs of this shape have already been found by hand: a hint that printed a command the
parser refused, a documented `calu config set` that silently did nothing, and a `/help` line
that left out the command an escalation is decided with. A command line in the README is a
promise, so this test holds the parser to it: it takes every `calu ...` line out of the
Markdown code blocks and parses it with the real parser.

It checks that the line **parses**, not that it works -- running it would need a database and
credentials -- so it catches a renamed flag, a wrong order of positional arguments and a
command that no longer exists, which is what goes stale in a README.
"""

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from calu.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = ["README.md", "docs/TESTING.md", "docs/CONFIGURATION.md", "AGENTS.md", "HANDOFF.md"]
FENCE = re.compile(r"```(?:bash|sh|console)?\n(.*?)```", re.S)
DRAWING = re.compile(r"[\u2500-\u257f]")
"""Box-drawing characters: a diagram, not a command."""

PLACEHOLDER = re.compile(r"<[^>]+>")
"""`<session id>` and friends stand for one argument each."""


def documented_commands() -> list[tuple[str, str]]:
    """Collect the `calu ...` command lines of the documentation.

    Only lines inside fenced code blocks count: prose mentions calu by name in sentences, and
    those are not commands. Three kinds of line inside a block are not invocations either and
    are left out: one with `|`, which spells alternatives (`calu token list|revoke`); one drawn
    with box characters, because the README's architecture diagram has a box labelled "calu
    session process"; and the word "calu" alone. A placeholder in angle brackets becomes one
    word, so that `<session id>` is the single argument it stands for rather than two.

    Returns:
        (document, command line) pairs, with the trailing `#` comment removed and the
        placeholders collapsed.
    """
    found: list[tuple[str, str]] = []
    for name in DOCUMENTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for block in FENCE.findall(text):
            for raw in block.splitlines():
                line = raw.strip()
                if not line.startswith("calu ") or "|" in line:
                    continue
                if DRAWING.search(line):
                    continue
                command = line.split("#", 1)[0].strip()
                command = PLACEHOLDER.sub("PLACEHOLDER", command)
                if command.strip() != "calu":
                    found.append((name, command))
    return found


def test_the_documentation_has_commands_to_check() -> None:
    """A regular expression that matches nothing would make this suite silently useless."""
    commands = documented_commands()
    assert len(commands) > 20, f"only {len(commands)} documented commands found"
    # The README is where most of them live.
    assert sum(1 for document, _ in commands if document == "README.md") > 15


@pytest.mark.parametrize(("document", "command"), documented_commands(), ids=str)
def test_a_documented_command_parses(document: str, command: str) -> None:
    """The real parser accepts the line as the documentation writes it.

    Args:
        document: Which file it came from, for the failure message.
        command: The command line, without its comment.
    """
    words = shlex.split(command)[1:]  # drop the program name
    parser = build_parser()
    # argparse writes to stderr and raises SystemExit; both are captured so a failure reports
    # the line rather than argparse's own output.
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            parsed = parser.parse_args(words)
    except SystemExit as exit_code:  # pragma: no cover - only on a stale document
        pytest.fail(
            f"{document} documents `{command}`, which the parser refuses:\n"
            f"{errors.getvalue().strip()} (exit {exit_code.code})"
        )
    assert isinstance(parsed, argparse.Namespace)
    assert getattr(parsed, "handler", None) is not None, (
        f"{document} documents `{command}`, which parses but has no command behind it"
    )
