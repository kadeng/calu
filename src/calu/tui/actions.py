"""What the user can ask of the session, one coroutine per intention.

No widget sends a request itself: every key, slash command and palette entry ends in a method
of `Actions`, which sends the request through the open connection and returns an `Outcome`. A
request the session refuses, a missing connection and a connection that closes while the
request waits all become an `Outcome` with `ok == False` and a message for the user; none of
them raises. The class needs no running app, so it is tested against
`calu.tui.fake.FakeSession` alone.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from calu.acp.modes import settings_for
from calu.config import AutoEscalate
from calu.protocol.client import RequestFailed
from calu.tui.connection import Link
from calu.tui.model import MAIN, Diff, SessionModel

METHOD_NOT_FOUND = -32601
"""The JSON-RPC code of a request the session does not know."""

NOT_ATTACHED = "not sent: this client is not attached to the session"
"""The message of a request made while no connection is open."""


@dataclass
class Outcome:
    """The result of one action."""

    ok: bool
    message: str = ""
    """What to tell the user: the session's refusal, or a confirmation. May be empty."""
    data: Any = None
    """The result of a request that reads something."""


class Actions:
    """Sends the user's intentions to the session (see the module docstring)."""

    def __init__(self, link: Link, model: SessionModel) -> None:
        """Create the actions for one link.

        Args:
            link: The link whose open connection carries the requests.
            model: The model, for the open decisions and the session's workspace.
        """
        self.link = link
        self.model = model

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Outcome:
        """Send one request and turn every failure into an `Outcome`.

        Args:
            method: The request method.
            params: Its params.

        Returns:
            `ok` with the result as `data`, or not `ok` with the session's error message, the
            reason the connection closed, or `NOT_ATTACHED`.
        """
        connection = self.link.connection
        if connection is None:
            return Outcome(False, NOT_ATTACHED)
        try:
            return Outcome(True, data=await connection.request(method, params or {}))
        except RequestFailed as error:
            if error.code == METHOD_NOT_FOUND:
                return Outcome(False, f"this session does not support it ({method})")
            return Outcome(False, str(error.error.get("message") or error))
        except ConnectionError as error:
            return Outcome(False, f"not sent: {error}")

    async def send(self, text: str, target: str = MAIN, delivery: str = "queued") -> Outcome:
        """Send a chat message (`chat.send`).

        Args:
            text: The message; must not be empty.
            target: `MAIN` or the `agent_id` of a running background subagent.
            delivery: "queued" (the next turn) or "inject" (after the running tool calls). A
                message to a subagent is always sent with "inject", as the session relays it
                through the main agent.

        Returns:
            The outcome; a refusal carries the session's message.
        """
        if not text.strip():
            return Outcome(False, "nothing to send")
        if target != MAIN:
            delivery = "inject"
        return await self.request("chat.send", {"text": text, "delivery": delivery, "to": target})

    async def decide(self, escalation_id: str, decision: str, note: str | None = None) -> Outcome:
        """Allow or deny an escalated step (`escalation.decide`).

        Side effects: marks the open item as decided by this client; a refusal takes the mark
        back and stores the session's message on the item, where the card shows it.

        Args:
            escalation_id: The escalation.
            decision: "allow" or "deny".
            note: A note that goes to Claude with the decision; None or "" for none.

        Returns:
            The outcome.
        """
        return await self._decide(
            escalation_id,
            {"escalation_id": escalation_id, "decision": decision, "note": note or None},
        )

    async def answer(self, escalation_id: str, text: str | None = None) -> Outcome:
        """Answer a question of the agent (`escalation.decide {answer}`).

        Args:
            escalation_id: The escalation of the question.
            text: The answer; None composes it from what the user chose on the card
                (`Decision.answer_text`).

        Returns:
            The outcome; not `ok` without a request if there is nothing to send.
        """
        item = self.model.decisions.get(escalation_id)
        if text is None and item is not None:
            text = item.answer_text()
        if not (text or "").strip():
            return Outcome(False, "no answer chosen yet")
        return await self._decide(escalation_id, {"escalation_id": escalation_id, "answer": text})

    async def _decide(self, escalation_id: str, params: dict[str, Any]) -> Outcome:
        """Send an `escalation.decide` and keep the open item's `own` and `error` in step.

        Args:
            escalation_id: The escalation.
            params: The request params.

        Returns:
            The outcome.
        """
        item = self.model.decisions.get(escalation_id)
        if item is not None:
            item.own = True
            item.error = ""
        outcome = await self.request("escalation.decide", params)
        item = self.model.decisions.get(escalation_id)
        if not outcome.ok and item is not None:
            item.own = False
            item.error = outcome.message
        return outcome

    async def why(self, escalation_id: str) -> Outcome:
        """Fetch what the checker saw and said for an escalation (`escalation.context`).

        Args:
            escalation_id: The escalation.

        Returns:
            The outcome with the `EscalationContextResult` as a dict in `data`.
        """
        return await self.request("escalation.context", {"escalation_id": escalation_id})

    async def interrupt(self) -> Outcome:
        """End Claude's current turn (`interrupt`).

        Side effects: marks the interrupt as this client's own until the turn ends, so that
        the steps it cancels are reported as cancelled by it; a refusal takes the mark back.

        Returns:
            The outcome.
        """
        self.model.own_interrupt = True
        outcome = await self.request("interrupt")
        if not outcome.ok:
            self.model.own_interrupt = False
        return outcome

    async def stop(self) -> Outcome:
        """Stop the Claude session (`session.stop`). The calu process keeps running.

        Returns:
            The outcome.
        """
        return await self.request("session.stop")

    async def start(
        self, resume_id: str | None = None, model: str | None = None, effort: str | None = None
    ) -> Outcome:
        """Start a Claude session, or resume one (`session.start`).

        Args:
            resume_id: The Claude session id to resume; None starts a new conversation.
            model: A model alias or id for this and later starts; None keeps the setting.
            effort: An effort for this and later starts; None keeps the setting.

        Returns:
            The outcome.
        """
        params = {"resume_id": resume_id, "model": model, "effort": effort}
        return await self.request(
            "session.start", {key: value for key, value in params.items() if value}
        )

    async def set_switches(self, switches: AutoEscalate) -> Outcome:
        """Replace the auto-escalation switches of the session (`auto_escalate.set`).

        Side effects: marks the change as this client's own, so that the event that follows
        is not reported as made elsewhere.

        Args:
            switches: The new switches.

        Returns:
            The outcome.
        """
        self.model.own_level_change = True
        outcome = await self.request("auto_escalate.set", switches.model_dump())
        if not outcome.ok:
            self.model.own_level_change = False
        return outcome

    async def set_level(self, mode_id: str) -> Outcome:
        """Set the supervision level to a ladder mode of `calu.acp.modes`.

        Args:
            mode_id: "checkers", "edits", "writes" or "everything".

        Returns:
            The outcome; not `ok` without a request for an unknown mode.
        """
        try:
            switches = settings_for(mode_id)
        except ValueError as error:
            return Outcome(False, str(error))
        return await self.set_switches(switches)

    async def ask_side(self, question: str) -> Outcome:
        """Ask Claude a question beside the running work (`side.ask`).

        Side effects: adds the question to the model's side questions at once, as pending, and
        fills in the answer or the session's refusal when the request returns. The earlier
        answered side questions go along as history. Nothing is sent to the main conversation.

        Args:
            question: The question; must not be empty.

        Returns:
            The outcome, with the answer text as `data`.
        """
        if not question.strip():
            return Outcome(False, "nothing to ask")
        history = self.model.side_history()
        exchange, _ = self.model.ask_side(question.strip())
        outcome = await self.request("side.ask", {"question": question.strip(), "history": history})
        exchange.pending = False
        if outcome.ok and isinstance(outcome.data, dict):
            exchange.answer = str(outcome.data.get("answer") or "")
            outcome.data = exchange.answer
        else:
            exchange.error = outcome.message or "no answer"
        return outcome

    async def refresh_usage(self) -> Outcome:
        """Ask for cost, context fill and usage limits and load them into the model (`usage.get`).

        Returns:
            The outcome; `ok` means the model's `usage` is current.
        """
        outcome = await self.request("usage.get")
        if outcome.ok and isinstance(outcome.data, dict):
            self.model.set_usage(outcome.data)
        return outcome

    async def set_model(self, name: str | None) -> Outcome:
        """Change the model: in the running session (`model.set`), or by starting a stopped one.

        Args:
            name: A Claude Code alias or model id; None or "default" for the default model.

        Returns:
            The outcome, with a confirmation as its message.
        """
        model = None if name in (None, "", "default") else name
        if self.model.state in {"stopped", "failed"}:
            outcome = await self.start(self.model.claude_session_id, model=model)
            done = f"session started with the model {model or 'default'}"
        else:
            outcome = await self.request("model.set", {"model": model})
            done = f"model changed to {model or 'the default'}: it applies from Claude's next step"
        if outcome.ok:
            self.model.model_name = model
            outcome.message = done
        return outcome

    async def read(self, method: str, params: dict[str, Any] | None = None) -> Outcome:
        """Send a request that only reads (`context.get`, `checkers.get`, `session.list`, ...).

        Args:
            method: The request method.
            params: Its params.

        Returns:
            The outcome with the result in `data`.
        """
        return await self.request(method, params)

    async def clear_mark(self, mark_id: int) -> Outcome:
        """Close one open mark of the honesty register by hand (`honesty.clear`).

        Args:
            mark_id: The id of the mark.

        Returns:
            The outcome.
        """
        return await self.request("honesty.clear", {"mark_id": mark_id})

    async def trust(self, rev: str = "") -> Outcome:
        """Certify a commit as honest (`honesty.trust`).

        The session resolves the name in its project directory, records the commit as trusted
        by the user and looks its baseline up again.

        Args:
            rev: A commit id or a branch name; "" means HEAD.

        Returns:
            The outcome; a session without this request answers "this session does not
            support it".
        """
        return await self.request("honesty.trust", {"rev": rev} if rev else {})

    async def trust_file(self, path: str) -> Outcome:
        """Certify the present content of one file as honest (`honesty.trust` with `path`).

        Args:
            path: The file, relative to the project directory or absolute.

        Returns:
            The outcome; a session without this request answers "this session does not
            support it".
        """
        return await self.request("honesty.trust", {"path": path})

    async def honesty_review(self) -> Outcome:
        """Ask calu to review the unreviewed files now (`honesty.review`).

        Returns:
            The outcome; a session without this request answers "this session does not
            support it".
        """
        return await self.request("honesty.review", {})

    async def comment(self, path: str, first: int, last: int, text: str) -> Outcome:
        """Send a comment on lines of a project file (`comment.add`, kind "file").

        The client reads the file itself, so this works only where the session's project
        directory exists on the client's machine.

        Args:
            path: The file, relative to the session's project directory.
            first: First line, counted from 1.
            last: Last line, inclusive.
            text: The comment.

        Returns:
            The outcome; not `ok` without a request if the directory or the file cannot be
            read here.
        """
        workspace = Path(self.model.workspace) if self.model.workspace else None
        if workspace is None or not workspace.is_dir():
            return Outcome(
                False,
                "a file comment needs this client on the session's machine: it reads the file",
            )
        try:
            content = (workspace / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            return Outcome(False, f"cannot read {path}: {error}")
        outcome = await self.request(
            "comment.add",
            {
                "kind": "file",
                "path": path,
                "start_line": first,
                "end_line": last,
                "after_text": content,
                "text": text,
            },
        )
        if outcome.ok:
            outcome.message = f"comment on {path}:{first}-{last} sent to Claude"
        return outcome

    async def comment_diff(
        self, diff: Diff, side: str, first: int, last: int, text: str
    ) -> Outcome:
        """Send a comment on lines of a diff (`comment.add`, kind "diff").

        The content before and after the edit comes from the step's `edit.diff` event, so
        this needs no file on the client's machine.

        Args:
            diff: The file with its content before and after the edit.
            side: "after" for lines of the new content, "before" for lines of the old one.
            first: First line on that side, counted from 1.
            last: Last line, inclusive.
            text: The comment; must not be empty.

        Returns:
            The outcome, with a confirmation as its message.
        """
        if not text.strip():
            return Outcome(False, "the comment is empty")
        outcome = await self.request(
            "comment.add",
            {
                "kind": "diff",
                "path": diff.path,
                "side": side,
                "start_line": first,
                "end_line": last,
                "after_text": diff.after,
                "before_text": diff.before,
                "text": text.strip(),
            },
        )
        if outcome.ok:
            lines = f"{first}" if first == last else f"{first}-{last}"
            which = "" if side == "after" else " (old content)"
            outcome.message = f"comment on {diff.path}:{lines}{which} sent to Claude"
        return outcome

    async def restart(
        self, resume_id: str | None, model: str | None = None, effort: str | None = None
    ) -> Outcome:
        """Stop the Claude session and start it again (`session.stop`, then `session.start`).

        This is how a running session gets another effort, another model where the session
        cannot change it while it runs, or another conversation. The running turn ends with
        the stop; background tasks and subagents of the session are lost.

        Args:
            resume_id: The Claude session id to resume; None starts a new conversation.
            model: A model alias or id for the start; None keeps the setting.
            effort: An effort for the start; None keeps the setting.

        Returns:
            The outcome of the start. If the stop is refused, its refusal and nothing was
            started. If the start fails after the stop, a message that says the session is
            stopped and why the start failed.
        """
        if self.model.state not in {"stopped", "failed"}:
            stopped = await self.stop()
            if not stopped.ok:
                return Outcome(False, f"not stopped: {stopped.message}")
        started = await self.start(resume_id, model, effort)
        if not started.ok:
            return Outcome(
                False, f"the session is stopped, and the start failed: {started.message}"
            )
        what = " / ".join(part for part in (model, effort) if part)
        started.message = "the session starts" + (f" with {what}" if what else "")
        return started

    async def shutdown(self) -> Outcome:
        """Stop the session and end the calu process (`shutdown`).

        Returns:
            The outcome.
        """
        return await self.request("shutdown")
