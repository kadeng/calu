"""The trace writer: appends the blocks of a session to its trace file, redacted and in order.

    Session.emit(event) ──► TraceWriter.feed ──► TraceRenderer.render ──► Block(s)
                                                                            │ (never waits)
                                                                            ▼
          queue of entries, in the order of the events (at most `queue_blocks`)
             │ a closed block                              │ the first entry of the queue
             ▼                                             ▼
          up to `REDACTIONS_AT_ONCE` tasks:              one task: waits until that entry's
            narration (narrated form)                    text is ready, appends it to the
            stage 1, stage 2 (`calu.traces.redact`)      file, takes the next
            -> the entry's text is ready

`feed` only renders and queues, so a step never waits for the redaction agent. The blocks are
redacted side by side and written strictly in the order they were queued.

What is written when something goes wrong; the text of a block that was not redacted is
never written:

    the redaction agent fails, times out or       the block's heading and `redact.WITHHELD`
    answers unusably
    the queue is full                             the block is left out; the next block that
                                                  fits is preceded by a line that says how
                                                  many were left out
    the session ends and a block is still         after `flush_timeout_s`: `WITHHELD_AT_END`
    being redacted                                in place of each such block
    the file cannot be created or written         nothing more is written; the problem is
                                                  logged and reported once; the session goes on
    no event of the session added a block         the file (its header, and calu's own last
    before the trace was closed                   line) is removed: it would say nothing

The narrated form: a block of Claude's text and thinking is queued where the text appeared
and gets its narration later. The session passes each narration it computes for its checkers
to `narration`, which finds the block by its text; a block that got none within
`narration_wait_s` after it was closed is narrated by the writer's own call of `narrate`.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from calu.context.narrator import ClaudeOutput
from calu.protocol.messages import Event, SessionState
from calu.traces.redact import (
    WITHHELD,
    RedactionFailed,
    Redactor,
    Secrets,
    redact_deterministic,
)
from calu.traces.render import (
    NOT_NARRATED,
    Block,
    Counted,
    Header,
    TraceFileError,
    TraceRenderer,
    create_trace_file,
    find_trace_of,
    format_block,
    quote,
    render_header,
)

log = logging.getLogger(__name__)

REDACTIONS_AT_ONCE = 4
"""How many blocks are narrated and redacted at the same time."""

NARRATION_WAIT_S = 90.0
"""How long a closed block of the narrated form waits for the session's narration before the
writer narrates it itself; longer than the session's own narrator deadline."""

FLUSH_TIMEOUT_S = 45.0
"""How long the end of a session waits for the blocks that are still being redacted."""

CREATE_WAIT_S = 10.0
"""How long a writer waits for the file of the writer it follows before it creates its own."""

WITHHELD_AT_END = "[this step was withheld: the session ended before it was redacted]"
"""What the trace holds in place of a block whose redaction the end of the session cut off."""

Narrate = Callable[[list[ClaudeOutput]], Awaitable[str]]
"""Narrates Claude's text and thinking; returns "" if it cannot. It may raise."""

Report = Callable[[Literal["info", "warning"], str], Awaitable[None]]
"""Receives a level and one sentence about the trace, for the session's log: "info" with the
file's path once it exists, "warning" with a problem."""


@dataclass
class _Entry:
    """One queued block and what became of it."""

    block: Block
    ready: anyio.Event = field(default_factory=anyio.Event)
    """Set when `text` is final."""
    text: str = ""
    """What the block adds to the file, redacted."""
    submitted: bool = False
    """True once the block was handed to the redaction tasks."""
    narrated: anyio.Event = field(default_factory=anyio.Event)
    """Set when a narration was delivered for the block, or the wait for one is over."""


class TraceWriter:
    """Writes the trace file of one run of a session. See the module docstring.

    Life cycle: create it, `start` it in a task group, call `feed` for every event, `close`
    at the end. `feed`, `note` and `narration` never raise and never wait.
    """

    def __init__(
        self,
        workspace: Path,
        target: Path,
        header: Header,
        renderer: TraceRenderer,
        secrets: Secrets,
        redactor: Redactor | None = None,
        narrate: Narrate | None = None,
        report: Report | None = None,
        continues: tuple[str, str] | None = None,
        queue_blocks: int = 500,
        narration_wait_s: float = NARRATION_WAIT_S,
        flush_timeout_s: float = FLUSH_TIMEOUT_S,
        after: TraceWriter | None = None,
    ) -> None:
        """Set the writer up; nothing is created on disk before `run`.

        Args:
            workspace: The project directory.
            target: The directory the file is created in
                (`calu.traces.render.session_directory`).
            header: What the file's first lines say; `continues` is filled in by `run`.
            renderer: Turns the events into blocks; its form (narrated or plain) is the
                file's.
            secrets: The values calu knows to be secret (stage 1); read at every block, so
                values added later count from then on.
            redactor: The redaction agent (stage 2), or None to run stage 1 only.
            narrate: Narrates a block of the narrated form that the session did not narrate;
                None leaves such a block without its text (`NOT_NARRATED`).
            report: Awaited with the file's path once it exists, and once with a sentence
                when the trace cannot be written; None only logs both.
            continues: (Claude session id, "resumed" or "forked") if this session continues
                another conversation: `run` looks for that conversation's trace file and
                names it in the header.
            queue_blocks: How many blocks may wait to be written; further blocks are left
                out and counted.
            narration_wait_s: Seconds a closed block of the narrated form waits for the
                session's narration.
            flush_timeout_s: Seconds `close` waits for the blocks that are still in work.
            after: The writer of the same session that this one follows (the `traces`
                settings changed), or None. This writer creates its file once that one has
                created its own, so the files are numbered in the order of the session, and
                its header names that file.
        """
        self.workspace = workspace
        self.target = target
        self.header = header
        self.renderer = renderer
        self.secrets = secrets
        self.redactor = redactor
        self.path: Path | None = None
        """The trace file, once `run` created it."""
        self.counted = Counted()
        self.fed = 0
        """Blocks that events of the session added (`feed`), queued or left out; calu's own
        lines (`note`) are not counted. A file that got none is removed when it is closed."""
        self.removed = False
        """True once the file was removed at its end because no event added a block."""
        self._narrate = narrate
        self._report = report
        self._continues = continues
        self._queue_blocks = queue_blocks
        self._narration_wait_s = narration_wait_s
        self._flush_timeout_s = flush_timeout_s
        self._entries: deque[_Entry] = deque()
        """The blocks that are not written yet, in the order of the file."""
        self._dropped = 0
        """Blocks left out since the last block that was queued."""
        self._wake = anyio.Event()
        self._closing = False
        self._started = False
        self._failed = False
        """True once the file could not be created or written: nothing more is queued."""
        self._done = anyio.Event()
        self._after = after
        self.created = anyio.Event()
        """Set once the file exists, or will never exist."""
        self._scope: anyio.CancelScope | None = None
        self._fd: int | None = None
        send, receive = anyio.create_memory_object_stream[_Entry](math.inf)
        self._todo: MemoryObjectSendStream[_Entry] = send
        self._work: MemoryObjectReceiveStream[_Entry] = receive

    # ------------------------------------------------------------------ feeding

    def feed(self, event: Event, resolved_by: str = "") -> None:
        """Queue what one event of the session adds to the trace; never raises, never waits.

        Args:
            event: The event, in the order the session emitted it.
            resolved_by: For `escalation.resolved`: who decided, or "".
        """
        if self._failed or self._closing:
            return
        try:
            for block in self.renderer.render(event, resolved_by):
                # The line with Claude's session id (from `session.state`) is no step.
                self.fed += not isinstance(event, SessionState)
                self._append(block)
            self._submit_closed()
        except Exception:
            log.exception("rendering %s for the session trace failed", event.method)

    def note(self, text: str) -> None:
        """Queue a line of calu's own, e.g. that the session ended; never raises.

        Args:
            text: One sentence in calu's words. It passes stage 1 of the redaction only.
        """
        if self._failed or self._closing:
            return
        self._append(Block("", f"_{text}_", fixed=True))

    def narration(self, outputs: Sequence[ClaudeOutput], text: str) -> None:
        """Give the writer a narration the session computed; never raises.

        The narration is used for the oldest queued block of the narrated form that holds
        exactly these outputs and has no narration yet. A narration that fits no block is
        dropped: the block concerned is then narrated by the writer itself.

        Args:
            outputs: What was narrated, as the session passed it to its narrator.
            text: The narration; "" if the narrator gave none (the block then says so).
        """
        wanted = list(outputs)
        for entry in self._entries:
            block = entry.block
            if block.outputs is not None and block.narration is None and block.outputs == wanted:
                block.narration = text
                entry.narrated.set()
                return

    def _append(self, block: Block) -> None:
        """Put a block at the end of the queue, or count it as left out if the queue is full.

        Args:
            block: The block.
        """
        if len(self._entries) >= self._queue_blocks:
            self._dropped += 1
            self.counted.left_out += 1
            return
        if self._dropped:
            self._entries.append(self._ready_entry(self._left_out_line()))
        self._entries.append(_Entry(block))
        self._wake.set()

    def _left_out_line(self) -> str:
        """Return the line that says how many blocks were left out, and reset the count.

        Returns:
            The text of a block for the file.
        """
        count, self._dropped = self._dropped, 0
        noun = "block was" if count == 1 else "blocks were"
        return format_block(
            "",
            f"_[{count} {noun} left out of the trace here: the trace writer was behind]_",
        )

    @staticmethod
    def _ready_entry(text: str) -> _Entry:
        """Return an entry whose text is final already.

        Args:
            text: The text for the file, calu's own words.

        Returns:
            The entry.
        """
        entry = _Entry(Block("", fixed=True), text=text, submitted=True)
        entry.ready.set()
        return entry

    def _submit_closed(self) -> None:
        """Hand every queued block that is closed to the redaction tasks, once."""
        for entry in self._entries:
            if not entry.submitted and entry.block.closed:
                entry.submitted = True
                self._todo.send_nowait(entry)

    # ------------------------------------------------------------------ the tasks

    def start(self, tg: TaskGroup) -> None:
        """Start the writer's task (`run`) in a task group.

        Args:
            tg: The task group; it must outlive the trace, i.e. run until `close` returned.
        """
        self._started = True
        tg.start_soon(self.run)

    async def run(self) -> None:
        """Create the file, then redact and write the queued blocks until `close`.

        Called through `start`. Side effects: creates the trace file and appends to it. A
        failure to create it is logged and reported, and ends the task; the session is not
        affected.
        """
        self._started = True
        try:
            with anyio.CancelScope() as scope:
                self._scope = scope
                if self._after is not None:
                    with anyio.move_on_after(CREATE_WAIT_S):
                        await self._after.created.wait()
                try:
                    await anyio.to_thread.run_sync(self._create)
                except TraceFileError as exc:
                    await self._fail(str(exc))
                    return
                finally:
                    self.created.set()
                await self._say("info", f"session trace: {self._shown_path()}")
                async with anyio.create_task_group() as tg:
                    for _ in range(REDACTIONS_AT_ONCE):
                        tg.start_soon(self._redact_loop, self._work.clone())
                    await self._write_loop()
                    tg.cancel_scope.cancel()
        except Exception:
            log.exception("the trace writer failed")
        finally:
            self.created.set()
            self._finish()
            self._done.set()

    def _create(self) -> None:
        """Create the trace file and write its header (runs in a worker thread).

        Raises:
            TraceFileError: If the file cannot be created or the header cannot be written.
        """
        path, fd = create_trace_file(self.workspace, self.target)
        header = self.header
        earlier, self._after = self._after, None
        if earlier is not None and earlier.path is not None and earlier.fed:
            # (A file without a block of the session is removed at its end and not named.)
            before = f"the steps before are in `{earlier._shown_path()}`"
            header = dataclasses.replace(
                header, note=f"{header.note}; {before}" if header.note else before
            )
        if self._continues is not None:
            claude_session, how = self._continues
            root = self.target.parent.parent
            earlier = find_trace_of(root, claude_session, skip=path)
            verb = "a fork of" if how == "forked" else "a continuation of"
            if earlier is not None:
                try:
                    shown = earlier.relative_to(self.workspace).as_posix()
                except ValueError:
                    shown = earlier.as_posix()
                sentence = f"this session is {verb} the conversation recorded in `{shown}`"
            else:
                sentence = (
                    f"this session is {verb} Claude session `{claude_session}`; no trace "
                    "file of that conversation was found"
                )
            header = dataclasses.replace(header, continues=sentence)
        text = redact_deterministic(render_header(header), self.secrets)
        try:
            os.write(fd, text.encode("utf-8", errors="replace"))
        except OSError as exc:
            os.close(fd)
            raise TraceFileError(f"cannot write {path}: {exc}") from exc
        self.path, self._fd = path, fd

    async def _fail(self, problem: str) -> None:
        """Stop tracing after a problem with the file, and say so once.

        Args:
            problem: What went wrong.
        """
        self._failed = True
        self._entries.clear()
        log.error("no session trace: %s", problem)
        await self._say("warning", f"the session trace is not written: {problem}")

    def _shown_path(self) -> str:
        """Return the trace file's path for a message.

        Returns:
            The path relative to the project directory, or the whole path if it is not
            inside it; "" before the file exists.
        """
        if self.path is None:
            return ""
        try:
            return self.path.relative_to(self.workspace).as_posix()
        except ValueError:
            return self.path.as_posix()

    async def _say(self, level: Literal["info", "warning"], message: str) -> None:
        """Pass a message to `report`; never raises.

        Args:
            level: "info" or "warning".
            message: The sentence.
        """
        if self._report is None:
            return
        try:
            await self._report(level, message)
        except Exception:
            log.exception("reporting about the session trace failed")

    async def _write_loop(self) -> None:
        """Append the queued blocks to the file in order, each once its text is ready.

        Returns when `close` was called and the queue is empty, or after a write failed.
        """
        while not self._failed:
            while not self._entries:
                if self._closing:
                    return
                await self._wake.wait()
                self._wake = anyio.Event()
            entry = self._entries[0]
            await entry.ready.wait()
            if not self._append_to_file(entry.text):
                await self._fail(f"writing {self.path} failed")
                return
            if self._entries and self._entries[0] is entry:
                self._entries.popleft()
                self.counted.written += 1

    async def _redact_loop(self, work: MemoryObjectReceiveStream[_Entry]) -> None:
        """Make the text of one closed block after another.

        Args:
            work: The stream of closed blocks.
        """
        async with work:
            async for entry in work:
                entry.text = await self._text_of(entry)
                entry.ready.set()

    async def _text_of(self, entry: _Entry) -> str:
        """Return what a block adds to the file: narrated if need be, and redacted.

        Never raises (except for a cancellation): every failure gives the block's heading
        with `WITHHELD`.

        Args:
            entry: The queued block; it is closed.

        Returns:
            The redacted text of the block.
        """
        block = entry.block
        heading = redact_deterministic(block.heading, self.secrets)
        try:
            body, fixed = block.body, block.fixed
            if block.outputs is not None:
                narration = await self._narration_of(entry)
                body, fixed = (quote(narration), False) if narration else (NOT_NARRATED, True)
            body = redact_deterministic(body, self.secrets)
            if self.redactor is not None and not fixed and body.strip():
                body = redact_deterministic(await self.redactor.redact(body), self.secrets)
        except RedactionFailed as exc:
            self._withheld(str(exc))
            body = WITHHELD
        except Exception as exc:
            log.exception("making a block of the session trace failed")
            self._withheld(f"{type(exc).__name__}: {exc}")
            body = WITHHELD
        return format_block(heading, body)

    def _withheld(self, reason: str) -> None:
        """Count a withheld block and keep the reason.

        Args:
            reason: Why the block could not be redacted.
        """
        self.counted.withheld += 1
        self.counted.failures = [*self.counted.failures[-9:], reason]
        log.warning("a block of the session trace was withheld: %s", reason)

    async def _narration_of(self, entry: _Entry) -> str:
        """Return the narration of a closed block of the narrated form.

        Waits up to `narration_wait_s` for the session's narration (not at all once `close`
        was called), then narrates the block itself if it can.

        Args:
            entry: The queued block with `outputs`.

        Returns:
            The narration, or "" if there is none.
        """
        block = entry.block
        assert block.outputs is not None
        if block.narration is None and not self._closing:
            with anyio.move_on_after(self._narration_wait_s):
                await entry.narrated.wait()
        if block.narration is not None:
            return block.narration.strip()
        if self._narrate is None or not any(o.text.strip() for o in block.outputs):
            return ""
        try:
            return (await self._narrate(list(block.outputs))).strip()
        except Exception:
            log.exception("narrating a block of the session trace failed")
            return ""

    def _append_to_file(self, text: str) -> bool:
        """Append text to the trace file.

        Args:
            text: The text; "" writes nothing.

        Returns:
            False if the write failed (the error is logged).
        """
        if self._fd is None:
            return False
        data = text.encode("utf-8", errors="replace")
        try:
            while data:
                data = data[os.write(self._fd, data) :]
        except OSError:
            log.exception("writing the session trace %s failed", self.path)
            return False
        return True

    def _finish(self) -> None:
        """Write what is left in the queue and close the file (no waiting, no model call).

        A block whose text is ready is written; any other block is written as its heading
        with `WITHHELD_AT_END`, never as its own text. If no event added a block to the
        file, the file and the directories left empty by it are removed instead
        (`_remove_empty`).
        """
        if self._fd is None:
            return
        if not self.fed:
            self._remove_empty()
            return
        try:
            while self._entries and not self._failed:
                entry = self._entries.popleft()
                if entry.ready.is_set():
                    text = entry.text
                    self.counted.written += 1
                else:
                    heading = redact_deterministic(entry.block.heading, self.secrets)
                    text = format_block(heading, WITHHELD_AT_END)
                    self.counted.withheld += 1
                if not self._append_to_file(text):
                    break
            if self._dropped:
                self._append_to_file(self._left_out_line())
        finally:
            try:
                os.close(self._fd)
            except OSError:
                log.exception("closing the session trace %s failed", self.path)
            self._fd = None
            self._entries.clear()

    def _remove_empty(self) -> None:
        """Close and delete the trace file, which holds its header and no block of the session.

        Such a file says nothing: the session ended, or its `traces` settings changed,
        before anything happened in it. Side effects: closes the file descriptor, unlinks the
        file, removes the session directory levels it leaves empty (never the trace
        directory itself), sets `removed`, clears the queue. A failure is logged and leaves
        the file.
        """
        assert self._fd is not None
        try:
            os.close(self._fd)
        except OSError:
            log.exception("closing the session trace %s failed", self.path)
        self._fd = None
        self._entries.clear()
        if self.path is None:
            return
        try:
            self.path.unlink()
            self.removed = True
            # <date>/<user>: left empty by the only file of that user and day.
            for directory in (self.target, self.target.parent):
                directory.rmdir()
        except OSError as exc:
            if not self.removed:
                log.warning("removing the empty session trace %s failed: %s", self.path, exc)

    # ------------------------------------------------------------------ the end

    async def close(self, note: str = "") -> None:
        """Write what is queued and end the trace; safe to call twice.

        Waits up to `flush_timeout_s` for the blocks in work; after that the blocks that are
        not ready are written as `WITHHELD_AT_END`.

        Args:
            note: A last line of calu's own for the file, e.g. "The session ended.", or "".
        """
        if not self._closing:
            self.renderer.close_all()
            if note:
                self.note(note)
            self._closing = True
            try:
                self._submit_closed()
            except Exception:
                log.exception("queueing the last blocks of the session trace failed")
            for entry in self._entries:
                entry.narrated.set()  # no more narrations will be delivered
            self._wake.set()
        if not self._started:
            self._entries.clear()
            self._done.set()
            return
        with anyio.move_on_after(self._flush_timeout_s):
            await self._done.wait()
        if not self._done.is_set():
            log.warning("the session trace was not written within %g s", self._flush_timeout_s)
            if self._scope is not None:
                self._scope.cancel()
            with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                await self._done.wait()
