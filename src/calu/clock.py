"""Watching calu's own clock, because the attribution of file changes rests on it (PLAN.md D32).

A checker tells which step changed a file by comparing the file's ctime with the times of the
steps (`calu.checkers.tools.changed_files`). An unprivileged process cannot forge a ctime, so
that comparison is sound — as long as the wall clock does not move. If it jumps backwards, a
change made now can look as if it belonged to an earlier step.

So calu compares its own two clocks:

    drift = (CLOCK_REALTIME now - before) - (CLOCK_BOOTTIME now - before)

`CLOCK_BOOTTIME` cannot be set and keeps counting while the host sleeps, so a drift means the
wall clock was moved, not that time passed differently. Boottime rather than `CLOCK_MONOTONIC`
is used on purpose: monotonic stops during suspend, so a laptop or a WSL host that was asleep
would look like a forward jump of hours.

- A backward jump of more than `max_backward_s` (default 1 s, far above the slew of a
  time-server sync) is reported as tampering: the session escalates its next tool call, so
  nothing runs unsupervised until a human has seen it.
- A forward jump of more than `max_forward_s` is only logged: a first time-server sync after a
  boot legitimately jumps forward.

Setting the clock needs CAP_SYS_TIME, which the agent does not have, so this is defence in
depth — and it also catches an administrator or a broken sync quietly corrupting the
attribution. calu never trusts a time the agent reports; only its own two clocks and the file
system.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio

log = logging.getLogger(__name__)

Clock = Callable[[], float]
"""Reads one clock in seconds."""


def realtime() -> float:
    """Read the wall clock, which can be set.

    Returns:
        Seconds since the epoch (`CLOCK_REALTIME`).
    """
    return time.time()


def boottime() -> float:
    """Read the clock that cannot be set and includes time spent suspended.

    Returns:
        Seconds since boot (`CLOCK_BOOTTIME`).
    """
    return time.clock_gettime(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class ClockJump:
    """A detected move of the wall clock.

    `seconds` is the size of the move and always positive; `backward` says which way it went.
    `at` is the wall-clock time after the move, as seconds since the epoch.
    """

    backward: bool
    seconds: float
    at: float

    def describe(self) -> str:
        """Describe the jump for a human and for the reason of an escalation.

        Returns:
            One sentence naming the direction and the size.
        """
        way = "backwards" if self.backward else "forwards"
        return (
            f"the wall clock moved {way} by {self.seconds:.1f} s relative to the boot clock, "
            f"which cannot be set"
        )


class ClockWatch:
    """Samples the two clocks and reports a jump of the wall clock.

    `check` is pure and does the comparison; `run` is the loop that samples. A caller that wants
    no task can call `check` itself.
    """

    def __init__(
        self,
        on_jump: Callable[[ClockJump], Awaitable[None]],
        interval_s: float = 10.0,
        max_backward_s: float = 1.0,
        max_forward_s: float = 300.0,
        wall: Clock = realtime,
        boot: Clock = boottime,
    ) -> None:
        """Create the watch; `run` starts sampling.

        Args:
            on_jump: Called for every jump beyond the limits; it decides what happens (the
                session escalates a backward jump and logs a forward one).
            interval_s: Seconds between samples. It only changes how soon a jump is noticed: the
                comparison is made against the previous sample, so no jump is missed.
            max_backward_s: Backward move that still counts as a time-server correction.
            max_forward_s: Forward move that is still reported, below which nothing is said.
            wall: Reads the wall clock (tests pass their own).
            boot: Reads the boot clock (tests pass their own).
        """
        self.on_jump = on_jump
        self.interval_s = interval_s
        self.max_backward_s = max_backward_s
        self.max_forward_s = max_forward_s
        self._wall = wall
        self._boot = boot
        self._last_wall = wall()
        self._last_boot = boot()

    def check(self) -> ClockJump | None:
        """Sample both clocks once and compare them with the previous sample.

        Side effect: the sample becomes the base of the next comparison, so a jump is reported
        once and not again at every later check.

        Returns:
            The jump if the wall clock moved beyond the limits, else None.
        """
        wall, boot = self._wall(), self._boot()
        drift = (wall - self._last_wall) - (boot - self._last_boot)
        self._last_wall, self._last_boot = wall, boot
        if drift < -self.max_backward_s:
            return ClockJump(backward=True, seconds=-drift, at=wall)
        if drift > self.max_forward_s:
            return ClockJump(backward=False, seconds=drift, at=wall)
        return None

    async def run(self) -> None:
        """Sample the clocks every `interval_s` seconds and report jumps, until cancelled.

        Side effects: calls `on_jump` for every jump beyond the limits. An exception from
        `on_jump` is logged and the loop continues, because a watch that dies silently is worse
        than one that misses a report.
        """
        while True:
            await anyio.sleep(self.interval_s)
            jump = self.check()
            if jump is None:
                continue
            log.warning("clock jump: %s", jump.describe())
            try:
                await self.on_jump(jump)
            except Exception:
                log.exception("reporting the clock jump failed")
