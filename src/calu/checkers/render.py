"""Render a `Step` as the text that a checker model reviews.

Commands are shown verbatim. Edits are shown as a unified diff of the affected file, computed by
applying the edit to the file's current content. Paths are shown relative to the workspace; a
path outside the workspace is marked as such.
"""

import difflib
import json
from pathlib import Path

from calu.checkers.model import Step
from calu.markdown import fence

MAX_CHARS = 20_000
"""Upper bound for rendered tool input; longer text is cut and marked as truncated."""


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    """Cut `text` to `limit` characters and append a marker that names the cut length.

    Args:
        text: The text to shorten.
        limit: Maximum number of characters kept.

    Returns:
        `text` unchanged if it fits, else its first `limit` characters plus a truncation line.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text) - limit} more characters]"


def _display_path(file_path: str, workspace: Path) -> str:
    """Return `file_path` relative to `workspace`, or absolute with an OUTSIDE marker.

    Args:
        file_path: Absolute path, or a path relative to the workspace.
        workspace: The workspace root.

    Returns:
        The workspace-relative POSIX path, or "<path> (OUTSIDE THE WORKSPACE)".
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = workspace / path
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return f"{path.as_posix()} (OUTSIDE THE WORKSPACE)"


def _read_text(path: Path) -> str | None:
    """Read a UTF-8 text file.

    Args:
        path: The file.

    Returns:
        The content, or None if the file cannot be read or is not valid UTF-8.

    Raises:
        ValueError: If the path contains a NUL character.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _apply_edit(content: str, old: str, new: str, replace_all: bool) -> str | None:
    """Return `content` with `old` replaced by `new`, or None if `old` does not occur.

    Args:
        content: The current text.
        old: The text to replace.
        new: The replacement.
        replace_all: Replace every occurrence if True, else only the first.

    Returns:
        The edited text, or None if `old` is not in `content`.
    """
    if old not in content:
        return None
    return content.replace(old, new) if replace_all else content.replace(old, new, 1)


def _unified_diff(before: str, after: str, label: str) -> str:
    """Return a unified diff (3 lines of context) between two texts.

    Every diff line ends with a newline, also where a text has no final newline.

    Args:
        before: The text before the change.
        after: The text after the change.
        label: File label for the header lines (`a/<label>`, `b/<label>`).

    Returns:
        The diff; "" if the texts are equal.
    """
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        n=3,
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in diff)


def edited_file(step: Step, workspace: Path) -> tuple[str, str, str] | None:
    """Return (display path, current content, content after the edit) for an edit step.

    Reads the target file from disk; relative paths resolve against `workspace`.

    Args:
        step: The tool call.
        workspace: The workspace root.

    Returns:
        The triple, or None for steps that are not Edit/Write/MultiEdit, have no `file_path`,
        or whose edit does not apply to the current file.

    Raises:
        ValueError: If the path contains a NUL character.
        AttributeError: If a MultiEdit `edits` entry is not an object.
    """
    file_path = step.tool_input.get("file_path")
    if not file_path or step.tool_name not in ("Edit", "Write", "MultiEdit"):
        return None
    resolved = Path(str(file_path))
    if not resolved.is_absolute():
        resolved = workspace / resolved
    contents = _edited_contents(step, resolved)
    if contents is None:
        return None
    return _display_path(str(file_path), workspace), *contents


def changed_lines(before: str, after: str) -> tuple[int, int] | tuple[()]:
    """Return the range of lines in `after` that differ from `before`.

    The range spans from the first to the last changed line (all hunks), 1-based and inclusive.
    For a pure deletion the range is empty: `(n, n - 1)` with n the line after the deletion.

    Args:
        before: Old file content.
        after: New file content.

    Returns:
        (start_line, end_line), or () if the contents have equal lines.
    """
    old, new = before.splitlines(), after.splitlines()
    opcodes = [
        op
        for op in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    if not opcodes:
        return ()
    start = opcodes[0][3] + 1
    end = opcodes[-1][4]
    return start, max(end, start - 1)


def _edited_contents(step: Step, file_path: Path) -> tuple[str, str] | None:
    """Return (current content, content after the edit) for Edit/Write/MultiEdit, else None.

    For Write, a file that does not exist or cannot be read has the current content "". The
    edits of a MultiEdit are applied in order, each to the result of the previous one.

    Args:
        step: The tool call.
        file_path: The resolved path of the target file.

    Returns:
        The pair, or None if the tool is not Edit/Write/MultiEdit, the file cannot be read (Edit,
        MultiEdit), or an edit does not apply (for example because `old_string` is not found).

    Raises:
        ValueError: If the path contains a NUL character.
        AttributeError: If a MultiEdit `edits` entry is not an object.
    """
    current = _read_text(file_path)
    inp = step.tool_input
    if step.tool_name == "Write":
        return current or "", str(inp.get("content", ""))
    if current is None:
        return None
    if step.tool_name == "Edit":
        edits = [inp]
    elif step.tool_name == "MultiEdit":
        edits = list(inp.get("edits", []))
    else:
        return None
    updated: str | None = current
    for e in edits:
        if updated is None:
            return None
        updated = _apply_edit(
            updated,
            str(e.get("old_string", "")),
            str(e.get("new_string", "")),
            bool(e.get("replace_all", False)),
        )
    return None if updated is None else (current, updated)


def render_step(step: Step, workspace: Path) -> str:
    """Return a Markdown description of `step` for a checker.

    The rule notes of the step (`Step.notes`) come first, as a list, then the narration of
    Claude's preceding thinking and text (`Step.narration`) as a block quote.
    Bash steps show the command; Edit/Write/MultiEdit steps show a diff against the target file,
    which is read from disk (relative paths resolve against `workspace`). If the file cannot be
    read or the edit does not apply, and for all other tools, the raw tool input is shown as JSON.
    Long text is truncated to `MAX_CHARS`.

    Unreadable or missing files do not raise. Malformed tool input can raise, e.g. a MultiEdit
    whose `edits` entries are not objects, a path with a NUL character, or input values that
    are not JSON-serializable. Callers that must not fail catch all exceptions (see
    `session._render_safely`).

    Args:
        step: The tool call.
        workspace: The workspace root.

    Returns:
        The Markdown text.

    Raises:
        ValueError: If the target path contains a NUL character.
        AttributeError: If a MultiEdit `edits` entry is not an object.
        TypeError: If the tool input is not JSON-serializable.
    """
    agent = f"subagent {step.agent_id}" if step.agent_id else "main agent"
    lines = [f"Tool call by the {agent}: `{step.tool_name}`", f"Working directory: {step.cwd}"]
    if step.notes:
        lines.append("Notes from the user's allow and deny lists:")
        lines += [f"- {note}" for note in step.notes]
    if step.narration:
        lines.append(
            "What Claude thought and wrote before this call, narrated (Claude's own statements, "
            "not the user's):\n" + _quote(step.narration)
        )
        lines.append("")  # ends the block quote
    inp = step.tool_input

    if step.tool_name == "Bash":
        if inp.get("description"):
            lines.append(f"Claude's description: {inp['description']}")
        if inp.get("run_in_background"):
            lines.append("Runs in the background.")
        lines.append("Command:\n" + fence(_truncate(str(inp.get("command", ""))), "bash"))
        return "\n".join(lines)

    file_path = inp.get("file_path") or inp.get("notebook_path")
    if file_path and step.tool_name in ("Edit", "Write", "MultiEdit"):
        label = _display_path(str(file_path), workspace)
        resolved = Path(str(file_path))
        if not resolved.is_absolute():
            resolved = workspace / resolved
        contents = _edited_contents(step, resolved)
        lines.append(f"File: {label}")
        if contents is not None:
            before, after = contents
            if not before:
                lines.append("The file is new (or empty).")
            diff = _unified_diff(before, after, label.split(" ")[0])
            lines.append("Diff:\n" + fence(_truncate(diff), "diff"))
            return "\n".join(lines)
        lines.append("The edit does not apply cleanly to the current file; raw input follows.")

    lines.append(
        "Input:\n" + fence(_truncate(json.dumps(inp, indent=2, ensure_ascii=False)), "json")
    )
    return "\n".join(lines)


def _quote(text: str) -> str:
    """Return `text` as a Markdown block quote.

    Args:
        text: Any text.

    Returns:
        Every line prefixed with "> ".
    """
    return "\n".join("> " + line for line in text.splitlines())
