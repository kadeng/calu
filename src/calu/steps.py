"""What a tool call says about files, and what its result says, as the session stores them.

Functions without state, used where a step is recorded:

    workspace_relative_input   a tool input with its paths relative to the project directory
    paths_of_step                 the project-relative paths a step is about
    files_before               the content of the file an edit targets, before the edit
    result_summary             a tool result, cut to what the `steps` row keeps

This module reads files of the workspace and changes nothing. It does not know the session.
"""

from pathlib import Path
from typing import Any

from calu.checkers import Step
from calu.checkers.policy import EDIT_TOOLS
from calu.honesty import diff_paths
from calu.protocol.messages import ToolResult


def workspace_relative_input(tool_input: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Return `tool_input` with `file_path`/`notebook_path` relative to `workspace` if inside it.

    Paths in an escalation's context are stored relative, so that an evaluation case built from
    it can be reproduced in another directory.

    Args:
        tool_input: The tool call's input.
        workspace: The workspace root.

    Returns:
        A copy; paths outside the workspace stay absolute.
    """
    result = dict(tool_input)
    for key in ("file_path", "notebook_path"):
        value = result.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            continue
        try:
            result[key] = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            pass
    return result


def paths_of_step(step: Step, workspace: Path) -> set[str]:
    """Return the project-relative paths a step is about, however the tool names them.

    `files_before` only returns a file it could read, so a `Write` that creates a file is not in
    it; a changeset step names its files in its diff. Both are paths a checker may legitimately
    mark although they do not exist yet (D112).

    Args:
        step: The tool call.
        workspace: The project root.

    Returns:
        The relative paths, empty for a step that names no file.
    """
    found: set[str] = set()
    for key in ("file_path", "notebook_path"):
        raw = step.tool_input.get(key)
        if isinstance(raw, str) and raw:
            relative = workspace_relative_input({key: raw}, workspace)[key]
            if not Path(relative).is_absolute():
                found.add(relative)
    diff = step.tool_input.get("diff")
    if isinstance(diff, str) and diff:
        found |= diff_paths(diff)
    return found


def files_before(step: Step, workspace: Path, limit: int = 50_000) -> dict[str, str]:
    """Return the current content of the file an edit step targets, for evaluation cases.

    Args:
        step: The escalated step.
        workspace: The workspace root.
        limit: Files longer than this many characters are not recorded.

    Returns:
        {workspace-relative path: content}, or {} for calls that are not edits and for
        missing, long, binary or outside files.
    """
    raw = step.tool_input.get("file_path")
    if step.tool_name not in EDIT_TOOLS or not raw:
        return {}
    relative = workspace_relative_input({"file_path": raw}, workspace)["file_path"]
    if Path(relative).is_absolute():
        return {}
    try:
        content = (workspace / relative).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError, ValueError:
        return {}
    return {relative: content} if len(content) <= limit else {}


def result_summary(event: ToolResult, limit: int = 2_000) -> str:
    """Return a short description of a tool result for the `steps` row.

    Args:
        event: The tool result event.
        limit: Maximum number of characters kept.

    Returns:
        The result content, cut at `limit`, prefixed with "error: " if the tool failed.
    """
    content = event.content[:limit]
    return f"error: {content}" if event.is_error else content
