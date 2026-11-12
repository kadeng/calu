"""The transcript of an agent tab as a Markdown document (`/export`).

A terminal offers no reliable clipboard and no selection across the parts of a full-screen
client, so the client can write what it shows to a file. The document holds what the model
holds: the user's messages, Claude's texts, one line per step with the verdict and the
checker's summary, the command or path of the step, the checker's note or reason, and the end
of each turn. Thinking is left out unless asked for. This module imports nothing from Textual.
"""

from calu.markdown import fence
from calu.tui.model import (
    ALL,
    JoinBlock,
    SessionModel,
    StepBlock,
    TextBlock,
    ThinkingBlock,
    TurnEndBlock,
    UserBlock,
)


def _step_markdown(step: StepBlock, model: SessionModel, tab: str) -> list[str]:
    """Render one step.

    Args:
        step: The step.
        model: The model, for the agent's name.
        tab: The exported tab; in the view of all agents the agent is named.

    Returns:
        A list item with verdict, tool and summary; indented under it the command or the
        path, and what the checker told Claude.
    """
    who = f" ({model.label(step.agent_id)})" if tab == ALL and step.agent_id else ""
    verdict = step.verdict or "RUNNING"
    what = step.input.get("command") or step.input.get("file_path") or step.input.get("pattern")
    summary = step.summary or (str(what) if what else step.tool)
    rows = [f"- **{verdict}** `{step.tool}`{who}: {' '.join(summary.split())}"]
    if isinstance(what, str) and what.strip() and what.strip() != summary.strip():
        rows += ["", *[f"  {row}" for row in fence(what.strip()).splitlines()], ""]
    for check in step.checks:
        if check.comment:
            rows.append(f"  - note to Claude: {' '.join(check.comment.split())}")
        elif check.reason and check.decision.lower() in {"deny", "escalate"}:
            rows.append(f"  - reason: {' '.join(check.reason.split())}")
    if step.waiting and step.waiting != "open" and step.note:
        rows.append(f"  - {step.waiting} with: {' '.join(step.note.split())}")
    return rows


def transcript_markdown(model: SessionModel, tab: str, thinking: bool = False) -> str:
    """Render the transcript of one tab as Markdown.

    Args:
        model: The model.
        tab: `MAIN`, `ALL` or the `agent_id` of a subagent.
        thinking: Whether thinking texts are included (as block quotes).

    Returns:
        The document: a title with project and session, then the blocks in order. Texts of
        the user and of Claude are taken over as they are.
    """
    title = f"# calu session {model.claude_session_id or ''}".rstrip()
    head = [title, ""]
    facts = [
        f"project: {model.project}" if model.project else "",
        f"directory: {model.workspace}" if model.workspace else "",
        f"agent: {model.label(None if tab == 'main' else tab)}" if tab != ALL else "all agents",
    ]
    head += [" · ".join(fact for fact in facts if fact), ""]
    parts: list[str] = head
    in_steps = False
    for block in model.blocks_of(tab):
        if isinstance(block, StepBlock):
            parts += _step_markdown(block, model, tab)
            in_steps = True
            continue
        if in_steps:
            parts.append("")
            in_steps = False
        match block:
            case UserBlock():
                who = "You" if block.source == "user" else "Automatic message"
                if block.relayed_to:
                    who += f" (to {block.relayed_to})"
                parts += [f"## {who}", "", block.text.strip(), ""]
            case TextBlock():
                if tab == ALL and block.agent_id:
                    parts += [f"_{model.label(block.agent_id)}:_", ""]
                parts += [block.text.strip(), ""]
            case ThinkingBlock():
                if thinking:
                    parts += [*[f"> {row}" for row in block.text.strip().splitlines()], ""]
            case TurnEndBlock():
                parts += ["---", "", f"_end of turn {block.number}_", ""]
            case JoinBlock():
                parts += [f"_{block.text}_", ""]
            case _:
                pass
    return "\n".join(parts).rstrip() + "\n"
