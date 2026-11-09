"""The database-only view of an escalation: everything a decision needs, nothing live."""

from datetime import UTC, datetime

from calu import herd
from calu.herd import EscalationRow


def test_an_escalation_renders_everything_needed_to_decide_it() -> None:
    """D30: the database alone must carry what a decision needs — no session, no live stream."""
    row = EscalationRow(
        id=12,
        session_id="s-1",
        state="open",
        reason="the deny list matched: sudo",
        created_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        rendered="## Step",
        hostname="box",
        directory="/w",
    )
    context = {
        "tool_name": "Bash",
        "tool_input": {"command": "sudo -n ls /root"},
        "rendered": "## Step\n\n    sudo -n ls /root",
        "recent": "## Turn 1\n\n**User:**\n\n    have a look",
        "chat_summary": "Nothing folded yet.",
        "files": {"a.py": "old\n"},
        "changeset": "",
    }
    shown = herd.render_escalation(row, context)
    assert "escalation 12 [open]  box:/w" in shown
    assert "the deny list matched" in shown
    assert "sudo -n ls /root" in shown  # the command line
    assert "The conversation, as the checkers see it" in shown
    assert "have a look" in shown  # the narrated context
    assert "a.py, before the change" in shown
    assert "calu resolve 12 allow|deny" in shown  # how to answer it from the database


def test_a_question_renders_its_options_and_how_to_answer() -> None:
    row = EscalationRow(
        id=13,
        session_id="s-1",
        state="open",
        reason="Claude asks: Tabs or spaces?",
        created_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        rendered="",
        hostname="box",
        directory="/w",
    )
    context = {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "Tabs or spaces?",
                    "options": [
                        {"label": "Tabs", "description": "tab characters"},
                        {"label": "Spaces"},
                    ],
                }
            ]
        },
    }
    shown = herd.render_escalation(row, context)
    assert "Claude asks" in shown
    assert "- Tabs: tab characters" in shown and "- Spaces" in shown
    assert 'calu resolve 13 answer "your answer"' in shown


def test_long_sections_are_cut_unless_asked_for_in_full() -> None:
    row = EscalationRow(
        id=14,
        session_id="s-1",
        state="allowed",
        reason="r",
        created_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        rendered="",
        hostname="box",
        directory="/w",
        resolved_by="operator",
        note="go ahead",
    )
    context = {"recent": "x" * (herd.MAX_SECTION_CHARS + 500)}
    cut = herd.render_escalation(row, context)
    assert "[... cut; run with --full]" in cut
    assert "answered by operator: go ahead" in cut
    assert "calu resolve" not in cut  # it is not open any more
    assert "[... cut" not in herd.render_escalation(row, context, full=True)
