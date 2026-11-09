"""The clock watch: a wall clock that moves against the boot clock is a jump (D32)."""

import anyio
import pytest

from calu.clock import ClockJump, ClockWatch

pytestmark = pytest.mark.anyio


class Clocks:
    """A pair of clocks a test moves by hand."""

    def __init__(self) -> None:
        self.wall = 1_700_000_000.0
        self.boot = 50.0

    def advance(self, boot: float, wall: float | None = None) -> None:
        """Let both clocks run; `wall` defaults to the same amount as `boot`."""
        self.boot += boot
        self.wall += boot if wall is None else wall


def watch(clocks: Clocks, **kwargs: float) -> ClockWatch:
    """A watch on those clocks that records nothing (the tests call `check` themselves)."""

    async def unused(jump: ClockJump) -> None:
        raise AssertionError("not expected")

    return ClockWatch(unused, wall=lambda: clocks.wall, boot=lambda: clocks.boot, **kwargs)


def test_clocks_that_run_together_are_no_jump() -> None:
    clocks = Clocks()
    eye = watch(clocks)
    clocks.advance(10)
    assert eye.check() is None


def test_a_host_that_slept_is_no_jump() -> None:
    """The reason for CLOCK_BOOTTIME: it keeps counting while the host is suspended."""
    clocks = Clocks()
    eye = watch(clocks)
    clocks.advance(3600)  # an hour of suspend: both clocks moved by it
    assert eye.check() is None


def test_a_time_server_correction_is_tolerated() -> None:
    clocks = Clocks()
    eye = watch(clocks)
    clocks.advance(10, wall=9.3)  # the wall clock was slewed back by 0.7 s
    assert eye.check() is None


def test_a_backward_jump_is_reported_once() -> None:
    clocks = Clocks()
    eye = watch(clocks)
    clocks.advance(1, wall=-4)  # the wall clock was set back by five seconds
    jump = eye.check()
    assert jump is not None
    assert jump.backward and jump.seconds == pytest.approx(5.0)
    assert "backwards by 5.0 s" in jump.describe()
    # The sample became the new base, so the same jump is not reported again.
    clocks.advance(10)
    assert eye.check() is None


def test_a_large_forward_jump_is_reported_as_well() -> None:
    clocks = Clocks()
    eye = watch(clocks)
    clocks.advance(1, wall=401)
    jump = eye.check()
    assert jump is not None
    assert not jump.backward and jump.seconds == pytest.approx(400.0)
    assert "forwards" in jump.describe()


def test_the_tolerances_are_configurable() -> None:
    clocks = Clocks()
    eye = watch(clocks, max_backward_s=30.0, max_forward_s=10.0)
    clocks.advance(1, wall=-20)  # 21 s backwards: still within the larger tolerance
    assert eye.check() is None
    clocks.advance(1, wall=13)
    jump = eye.check()
    assert jump is not None and not jump.backward


async def test_the_loop_reports_a_jump_and_survives_a_failing_report() -> None:
    clocks = Clocks()
    seen: list[ClockJump] = []

    async def on_jump(jump: ClockJump) -> None:
        seen.append(jump)
        if len(seen) == 1:
            raise RuntimeError("the session could not be told")

    eye = ClockWatch(
        on_jump,
        interval_s=0.01,
        wall=lambda: clocks.wall,
        boot=lambda: clocks.boot,
    )
    with anyio.move_on_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(eye.run)
            clocks.advance(1, wall=-10)
            while not seen:
                await anyio.sleep(0.01)
            clocks.advance(1, wall=-20)  # the loop kept running although the first report failed
            while len(seen) < 2:
                await anyio.sleep(0.01)
            tasks.cancel_scope.cancel()
    assert [round(jump.seconds) for jump in seen] == [11, 21]
