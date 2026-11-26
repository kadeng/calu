"""The user's pause of a session: approved tool calls wait until it ends (the owner, 2026-11-26).

A client asks for a pause (`pause.set`) to have time to review what the session does. The
pause does not interrupt anything that runs. It takes effect at the next tool call that needs a
check -- an edit, a command, a commit, the start of a subagent, of any agent of the session:
the call is checked as usual and, if approved, waits before its answer goes to Claude Code.

    pause.set {paused: true}  ──►  paused until now + pause_timeout_s
    a checked call is approved ──► waits (`Pause.wait`) ──► the pause ends ──► the call runs
                                                          (pause.set {paused: false}, or the
                                                           time is up, or the hook's time is)
    a call that is denied or escalated is answered at once: there is nothing to hold back

The wait runs inside Claude Code's PreToolUse hook, whose timeout bounds it: a call whose check
took long, or that came late in a pause, is released when the hook's time is used up (the
caller passes the limit). The default length (10 minutes) leaves the hook's 15 minutes room
for the check itself.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import anyio

from calu.checkers.model import Step
from calu.protocol.messages import Event, Log, PauseChanged, PauseInfo, StepHeld

PAUSE_REASON = "the user's pause"
"""What holds a step during a pause, in its `step.held` event."""


class Pause:
    """Whether the session is paused, until when, and the waits of the calls it holds.

    Time is measured with `anyio.current_time` (monotonic); the end is shown to clients as a
    wall-clock time (`PauseInfo.until`). A pause that ran out is reported as ended by the
    next call that looks (`info`, `wait`), and every holding call ends its wait at that time
    by itself.
    """

    def __init__(self, emit: Callable[[Event], Awaitable[None]]) -> None:
        """Create a session's pause, not paused.

        Args:
            emit: Sends an event to the session's clients; must not raise.
        """
        self._emit = emit
        self._ends: float | None = None
        """`anyio.current_time()` at which the pause ends; None when not paused."""
        self._until: datetime | None = None
        """The same end as a wall-clock time, for the clients."""
        self._released = anyio.Event()

    def remaining(self) -> float:
        """Return the seconds the pause still lasts.

        Returns:
            0.0 when the session is not paused or the pause ran out.
        """
        if self._ends is None:
            return 0.0
        return max(self._ends - anyio.current_time(), 0.0)

    def info(self) -> PauseInfo:
        """Describe the pause for the snapshot.

        Returns:
            `paused` and the wall-clock end; not paused once the time is up.
        """
        if self.remaining() <= 0.0:
            return PauseInfo()
        return PauseInfo(paused=True, until=self._until)

    async def start(self, seconds: float, client: str) -> PauseInfo:
        """Pause the session for `seconds`, or start the time again if it is paused.

        Side effects: emits `pause.changed` and a `log` line.

        Args:
            seconds: How long the pause lasts at most; must be positive.
            client: Who asked, for the event and the log line.

        Returns:
            The pause as it is now.
        """
        if self._ends is None or self.remaining() <= 0.0:
            self._released = anyio.Event()
        self._ends = anyio.current_time() + seconds
        self._until = datetime.fromtimestamp(time.time() + seconds, UTC)
        info = PauseInfo(paused=True, until=self._until)
        await self._emit(PauseChanged(paused=True, until=self._until, client=client))
        await self._emit(
            Log(
                level="info",
                message=(
                    f"{client} paused the session until {self._until.astimezone():%H:%M}: "
                    "the next approved edit, command or subagent start waits"
                ),
            )
        )
        return info

    async def end(self, client: str, why: str = "") -> PauseInfo:
        """End the pause; every held call goes on.

        Side effects: releases the waiting calls; emits `pause.changed`, also when the session
        was not paused (a client that missed the end learns it this way), and a `log` line
        only when it was.

        Args:
            client: Who ended it ("" when the time ran out).
            why: The reason for the log line instead of the client's name, e.g. "the time is
                up"; "" names the client.

        Returns:
            The pause as it is now: not paused.
        """
        was = self._ends is not None
        self._ends = self._until = None
        self._released.set()
        await self._emit(PauseChanged(paused=False, until=None, client=client))
        if was:
            await self._emit(
                Log(level="info", message=f"the pause ended: {why or f'{client} resumed'}")
            )
        return PauseInfo()

    async def wait(self, step: Step, limit_s: float) -> None:
        """Hold an approved call while the session is paused.

        Returns at once when the session is not paused. Otherwise waits until the pause ends
        (`end`), its time is up, or `limit_s` passed (the hook's time); a cancellation ends
        the wait at once. A pause whose time ran out is ended here (`end` with "the time is
        up"), so the clients learn it.

        Side effects: emits `step.held` when the wait begins and when it ends, and a `log`
        line saying that the call waits.

        Args:
            step: The approved call.
            limit_s: The longest wait in seconds; a negative value counts as 0.
        """
        left = self.remaining()
        if left <= 0.0:
            if self._ends is not None:
                await self.end("", "the time is up")
            return
        released = self._released
        who = f"subagent {step.agent_id}" if step.agent_id else "the main agent"
        until = self._until.astimezone() if self._until else None
        await self._emit(
            Log(
                level="info",
                message=(
                    f"{step.tool_name} of {who} waits: the session is paused"
                    + (f" until {until:%H:%M}" if until else "")
                ),
            )
        )
        await self._emit(
            StepHeld(
                tool_use_id=step.tool_use_id,
                agent_id=step.agent_id,
                tool_name=step.tool_name,
                held=True,
                reason=PAUSE_REASON,
            )
        )
        try:
            with anyio.move_on_after(min(left, max(limit_s, 0.0))):
                await released.wait()
        finally:
            with anyio.CancelScope(shield=True):
                await self._emit(
                    StepHeld(
                        tool_use_id=step.tool_use_id,
                        agent_id=step.agent_id,
                        tool_name=step.tool_name,
                        held=False,
                    )
                )
                if self._ends is not None and self.remaining() <= 0.0:
                    await self.end("", "the time is up")
