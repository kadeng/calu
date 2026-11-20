"""Worktree spike, part 2: what calu's path rules, the checkers' read tools and the honesty state
make of paths in worktrees (spikes/RESULTS.md, "Worktrees, 2026-11-20").

Runs inside `calu-uitest` with the installed calu, after `spike_worktrees.sh setup` and
`plain` created the project ~/work/wt with two Claude Code worktrees below
`.claude/worktrees/` and the `git worktree add ../wt-outside` worktree beside it:

    PY="$(uv tool dir)/calu/bin/python"
    "$PY" /workspace/calu/spikes/spike_worktrees.py [project]

No model call, no database: the rules are the shipped default lists, the read tools are the
checkers' own (`calu.checkers.tools.CodeTools`), the state is a fresh `HonestyState`.
"""

import asyncio
import sys
from importlib import resources
from pathlib import Path

from calu.checkers.model import Step
from calu.checkers.rules import RULE_LISTS, Rules
from calu.checkers.tools import CodeTools
from calu.honesty import HonestyState


def default_rules(workspace: Path) -> Rules:
    """Return the rules of `workspace` with the default lists calu ships.

    Args:
        workspace: The project directory.

    Returns:
        The rules; lists without a shipped file are empty.
    """
    folder = resources.files("calu.defaults") / "rules"
    texts: dict[str, str] = {}
    for name in RULE_LISTS:
        entry = folder / f"{name}.txt"
        if entry.is_file():
            texts[name] = entry.read_text(encoding="utf-8")
    return Rules.from_texts(workspace, texts)


async def main(workspace: Path) -> None:
    """Print, per path, the rules' form, the deny verdicts, the honesty key and a read.

    Side effects: reads files below `workspace` and the outside worktree; prints to stdout.

    Args:
        workspace: The project directory (the main checkout).
    """
    trees = sorted(p for p in (workspace / ".claude" / "worktrees").iterdir() if p.is_dir())
    outside = workspace.parent / "wt-outside"
    rules = default_rules(workspace)
    tools = CodeTools(workspace, rules)
    state = HonestyState(workspace)
    paths = [workspace / "calc.py", workspace / "CLAUDE.md"]
    for tree in trees:
        paths += [tree / "calc.py", tree / "CLAUDE.md", tree / ".claude" / "settings.json"]
    paths += [outside / "calc.py", outside / "CLAUDE.md"]
    for path in paths:
        shown = path.relative_to(workspace) if path.is_relative_to(workspace) else path
        print(f"== {shown}")
        print(f"   rules match_form : {rules.match_form(path)}")
        print(f"   read/write denied: {rules.read_denied(path)}/{rules.write_denied(path)}")
        print(f"   honesty key      : {state.key(path)}")
        write = Step(
            tool_name="Write",
            tool_input={"file_path": str(path), "content": "x"},
            cwd=str(workspace),
        )
        outcome = rules.evaluate(write)
        print(f"   rules on a Write : ask={outcome.ask} notes={outcome.notes}")
        asked = str(shown) if not Path(shown).is_absolute() else str(path)
        answer = await tools.read_file(asked, 1, 3)
        print(f"   read_file({asked!r}): {answer.splitlines()[-1][:110]}")
        if not Path(shown).is_absolute():
            continue
        relative = Path("..") / path.relative_to(workspace.parent)
        answer = await tools.read_file(str(relative), 1, 3)
        print(f"   read_file({str(relative)!r}): {answer.splitlines()[-1][:110]}")
    print("== find_files('**/calc.py')")
    print("  ", (await tools.find_files("**/calc.py")).replace("\n", "\n   "))
    print("== grep_files('return a', '**/*.py')")
    print("  ", (await tools.grep_files("return a", "**/*.py")).replace("\n", "\n   "))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "work" / "wt")))
