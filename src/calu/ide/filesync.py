"""File synchronisation of `calu ide`: the `ide.fs.*` requests and the change notifications.

    editor ── ide.fs.manifest / read ───────────────► Tree (calu.ide.filestore), worker thread
           ── ide.fs.write / delete / rename ─┐
           ── ide.fs.write.begin/chunk/commit ┤  1. is the file held by the agent's turn?
                                              │  2. is the file at the request's base?
                                              │  3. tell the attached session: "the user
                                              │     writes this file" (files.user_edit)
                                              │  4. compare again and replace in one step
                                              └► change {origin: "ide"} ─┐
    attached session ── edit.diff, tool.result ─► "look now" ─┐           │
    every `poll_s` seconds ───────────────────────────────────┴► scan ────┤
                              (listing + lstat; hash what changed)        ▼
                                                              ChangeLog ─► ide.fs.changed

The requests exist for an editor that works on a copy of the project and reaches the server over SSH
only (`agent/records/notes/jetbrains-sync-spec.md`). A write of the editor is the user's own
edit: no checker judges it. One lock serialises the writes and the scans of one bridge, so
that the comparison with the base and the replacement have no other write of the bridge
between them, and a scan never reports the bridge's own write a second time.

The watcher polls: every `poll_s` seconds (longer if a scan is slow, see
`calu.ide.files.poll_interval`) it lists the files as a manifest would and compares size,
mtime and inode with the scan before; only files whose signature changed are read. calu has
no file-watching library among its dependencies, and polling also sees changes on network
file systems.
"""

import base64
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
import anyio.to_thread

from calu.acp.peer import RpcError
from calu.ide.files import (
    BATCH_LATEST_S,
    BATCH_QUIET_S,
    HELD,
    INVALID_PARAMS,
    MAX_FILE_BYTES,
    MAX_PATHS,
    MAX_UPLOADS,
    NO_MANIFEST,
    NOT_FOUND,
    PAGE_ENTRIES,
    REQUEST_BYTES,
    TARGET_EXISTS,
    TOO_LARGE,
    UPLOAD_FAILED,
    UPLOAD_IDLE_S,
    Change,
    ChangeLog,
    Hints,
    Listing,
    Signature,
    TurnFiles,
    changed_paths,
    clean_path,
    decode_content,
    edited_paths,
    int_param,
    parse_cursor,
    poll_interval,
    session_prefixes,
    sha_param,
)
from calu.ide.filestore import State, Temp, Tree, check_base

log = logging.getLogger(__name__)

POLL_S = 2.0
"""Seconds between two scans of the watcher when a scan is fast."""

KEPT_LISTINGS = 4
"""Manifests whose further pages can still be asked for."""

BIGGEST = 2**53
"""The largest integer a request may carry (what a JSON number holds exactly)."""

Handler = Callable[[dict[str, Any]], Awaitable[Any]]
"""Answers one request: takes its params, returns its result."""

Notify = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Sends one notification to the editor: `(method, params)`."""

Report = Callable[[list[str]], Awaitable[None]]
"""Tells the attached session which files the user is about to write; never raises."""


@dataclass
class Upload:
    """One chunked write between `ide.fs.write.begin` and its commit or abort."""

    id: str
    path: str
    base: str | None
    size: int
    """The announced size of the whole content."""
    sha256: str
    """The announced hash of the whole content."""
    mode: int | None
    during_turn: bool
    received: int = 0
    temp: Temp | None = None
    """The temporary file; created with the first chunk."""
    touched: float = field(default_factory=time.monotonic)
    """`time.monotonic()` of the upload's last request."""
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    """Serialises the requests of this upload."""


def _flag(params: Mapping[str, Any], key: str) -> bool:
    """Read a boolean parameter that defaults to false.

    Args:
        params: The request's params.
        key: The parameter's name.

    Returns:
        The value; False if it is absent or null.

    Raises:
        RpcError: `INVALID_PARAMS` if the value is not a boolean.
    """
    value = params.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RpcError(INVALID_PARAMS, f"the parameter {key!r} must be true or false")
    return value


class FileSync:
    """Answers the file requests of one editor and sends it the changes (module docstring)."""

    def __init__(
        self,
        workspace: Path,
        notify: Notify,
        tasks: anyio.abc.TaskGroup,
        report: Report,
        *,
        poll_s: float = POLL_S,
        quiet_s: float = BATCH_QUIET_S,
        upload_idle_s: float = UPLOAD_IDLE_S,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        """Create the component; no file is touched before the first file request.

        Args:
            workspace: The resolved project directory.
            notify: Sends a notification to the editor.
            tasks: The task group the watcher and the upload clean-up run in.
            report: Tells the attached session about a write of the user before it happens.
            poll_s: Seconds between two scans of the watcher.
            quiet_s: Seconds without a further change before the changes are sent.
            upload_idle_s: Seconds without a request after which an upload is discarded.
            max_file_bytes: The per-file limit.
        """
        self.workspace = workspace
        self.poll_s = poll_s
        self.quiet_s = quiet_s
        self.upload_idle_s = upload_idle_s
        self.max_file_bytes = max_file_bytes
        self.turn = TurnFiles(workspace)
        """What the attached session's events say about the files (`calu.ide.files`)."""
        self.changes = ChangeLog()
        self.handlers: dict[str, Handler] = {
            "ide.paths": self._paths,
            "ide.fs.manifest": self._manifest,
            "ide.fs.read": self._read,
            "ide.fs.write": self._write,
            "ide.fs.write.begin": self._begin,
            "ide.fs.write.chunk": self._chunk,
            "ide.fs.write.commit": self._commit,
            "ide.fs.write.abort": self._abort,
            "ide.fs.delete": self._delete,
            "ide.fs.rename": self._rename,
            "ide.fs.subscribe": self._subscribe,
            "ide.fs.unsubscribe": self._unsubscribe,
        }
        self._notify = notify
        self._tasks = tasks
        self._report = report
        self._tree: Tree | None = None
        self._session_excluded: tuple[str, ...] = ()
        self._io = anyio.Lock()
        """Held by every write, delete, rename and scan of this bridge."""
        self._listings: dict[str, Listing] = {}
        self._uploads: dict[str, Upload] = {}
        self._janitor: anyio.CancelScope | None = None
        self._watcher: anyio.CancelScope | None = None
        self._known: dict[str, tuple[Signature, str | None]] = {}
        """Path -> (signature, hash or None if never read) at the last scan or own write."""
        self._skipped: dict[str, str] = {}
        """Path -> reason of each listed path the last scan skipped (a manifest's `skipped`)."""
        self._wake = anyio.Event()
        self._dirty = anyio.Event()
        self._released = anyio.Event()
        self._replay: list[dict[str, Any]] = []

    # ------------------------------------------------------------------------- plumbing

    def tree(self) -> Tree:
        """Return the project directory's file access, opened at the first use.

        Returns:
            The tree.

        Raises:
            RpcError: `NO_MANIFEST` (`reason: "unreadable"`) if the directory cannot be
                opened.
        """
        if self._tree is None:
            try:
                self._tree = Tree(self.workspace, self.max_file_bytes)
            except OSError as error:
                raise RpcError(
                    NO_MANIFEST,
                    f"the project directory {self.workspace} cannot be opened: {error.strerror}",
                    {"reason": "unreadable"},
                ) from error
            self._tree.session_excluded = self._session_excluded
        return self._tree

    def exclude(self, directories: object) -> None:
        """Take the attached session's own directories; they are excluded from now on.

        Args:
            directories: The `excluded` value of the session's answer to `files.user_edit`;
                entries that are no canonical relative path are dropped.
        """
        self._session_excluded = session_prefixes(directories)
        if self._tree is not None:
            self._tree.session_excluded = self._session_excluded

    def session_event(self, method: str, params: Mapping[str, Any]) -> None:
        """Take one event of the attached session. Side effect: may wake the watcher.

        Args:
            method: The event's method.
            params: Its params.
        """
        if self.turn.event(method, params):
            self._wake.set()

    def link_state(self, state: str) -> None:
        """Take a change of the connection to the attached session.

        Side effects: after "lost" nothing is held any more; every change wakes the watcher,
        because edits may have gone unannounced.

        Args:
            state: "attached", "reconnecting" or "lost".
        """
        if state == "lost":
            self.turn.detach()
        self._wake.set()

    async def after_response(self, method: str) -> None:
        """Let the notifications flow once the answer to `ide.fs.subscribe` is written.

        Side effect: sends the replay of a renewed subscription first.

        Args:
            method: The method whose response was just written.
        """
        if method != "ide.fs.subscribe":
            return
        replay, self._replay = self._replay, []
        for note in replay:
            await self._notify("ide.fs.changed", note)
        self._released.set()

    async def close(self) -> None:
        """End the component. Never raises.

        Side effects: stops the watcher and the clean-up task, discards every open upload
        (its temporary file and the directories made for it are removed) and closes the
        project directory.
        """
        for scope in (self._watcher, self._janitor):
            if scope is not None:
                scope.cancel()
        self._watcher = self._janitor = None
        with anyio.CancelScope(shield=True):
            for upload in list(self._uploads.values()):
                await self._discard(upload)
            if self._tree is not None:
                self._tree.close()
                self._tree = None

    def _path(self, params: Mapping[str, Any], key: str = "path") -> str:
        """Read and check a path parameter.

        Args:
            params: The request's params.
            key: The parameter's name.

        Returns:
            The canonical relative path.

        Raises:
            RpcError: `PATH_REFUSED` (`calu.ide.files.clean_path`).
        """
        return clean_path(params.get(key), self.tree().excluded)

    async def _held(self, paths: list[str], during_turn: bool) -> None:
        """Refuse a write to a file the agent edits in the running turn.

        Args:
            paths: The files the request changes.
            during_turn: The request's `during_turn`: True skips the check.

        Raises:
            RpcError: `HELD` with `data.session_id` and `data.sha256` (the file's hash now,
                null if it cannot be told).
        """
        if during_turn:
            return
        for path in paths:
            if not self.turn.held(path):
                continue
            digest: str | None = None
            try:
                digest = (await anyio.to_thread.run_sync(self.tree().state, path)).sha256
            except RpcError:
                pass
            raise RpcError(
                HELD,
                f"the agent is editing {path} in the running turn; the upload waits for the "
                "turn's end, or is repeated with during_turn",
                {"session_id": self.turn.session_id, "sha256": digest},
            )

    def _emit(self, change: Change) -> None:
        """Queue one change for the subscriber; without a subscription nothing is kept.

        Args:
            change: The change.
        """
        if self._watcher is None:
            return
        self.changes.add(change)
        self._dirty.set()

    def _wrote(self, path: str, before: str | None, state: State) -> None:
        """Record a write of this bridge and queue its change; no scan reports it again.

        The queued change carries `origin: "ide"`.

        Args:
            path: The written file.
            before: Its hash before; None if it did not exist.
            state: Its state now.
        """
        if state.signature is not None:
            self._known[path] = (state.signature, state.sha256)
        self._emit(Change(path, before, state.sha256, "ide"))

    # ------------------------------------------------------------------ paths and reads

    async def _paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.paths`: where the bridge works.

        Side effects: up to three git calls; lists the project directory once.

        Args:
            params: Unused.

        Returns:
            `{root, home, separator: "/", git, head, branch, case_sensitive, limits}`;
            `limits` holds `file_bytes`, `request_bytes`, `page_entries` and `uploads`.

        Raises:
            RpcError: `NO_MANIFEST` if the project directory cannot be opened.
        """
        tree = self.tree()
        git, head, branch = await anyio.to_thread.run_sync(tree.git_info)
        return {
            "root": str(self.workspace),
            "home": str(Path.home()),
            "separator": "/",
            "git": git,
            "head": head,
            "branch": branch,
            "case_sensitive": await anyio.to_thread.run_sync(tree.case_sensitive),
            "limits": {
                "file_bytes": self.max_file_bytes,
                "request_bytes": REQUEST_BYTES,
                "page_entries": PAGE_ENTRIES,
                "uploads": MAX_UPLOADS,
            },
        }

    async def _manifest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.manifest`: one page of the files with their hashes.

        Side effects: the first page lists the files (git, or a walk); every page hashes
        its files in a worker thread unless the hash cache knows them; a listing with
        further pages is kept (`KEPT_LISTINGS` at most).

        Args:
            params: `scope` ("all" or "dirty"), `paths` (files or directories; an empty
                list selects nothing), or `cursor` (the `next_cursor` of the page before).

        Returns:
            `{head, files, skipped, missing, next_cursor}`; `missing` is filled on the
            first page only.

        Raises:
            RpcError: `INVALID_PARAMS`; `PATH_REFUSED` for an entry of `paths`;
                `NO_MANIFEST` with `data.reason` ("git", "not-git", "too-many-files",
                "unreadable", "cursor-expired" for a cursor whose listing is no longer
                kept, "head-moved" if HEAD changed since the first page: the editor asks
                for the manifest again).
        """
        tree = self.tree()
        cursor = params.get("cursor")
        if cursor is not None:
            listing_id, offset = parse_cursor(cursor)
            listing = self._listings.get(listing_id)
            if listing is None or offset > len(listing.paths):
                raise RpcError(
                    NO_MANIFEST,
                    "the listing of this cursor is no longer kept; ask for the manifest again",
                    {"reason": "cursor-expired"},
                )
            if listing.git and (await anyio.to_thread.run_sync(tree.git_info))[1] != listing.head:
                del self._listings[listing_id]
                raise RpcError(
                    NO_MANIFEST,
                    "HEAD moved while the manifest was read; ask for the manifest again",
                    {"reason": "head-moved"},
                )
        else:
            scope = params.get("scope") or "all"
            if scope not in ("all", "dirty"):
                raise RpcError(INVALID_PARAMS, "the parameter 'scope' is 'all' or 'dirty'")
            wanted = params.get("paths")
            if wanted is not None and (not isinstance(wanted, list) or len(wanted) > MAX_PATHS):
                raise RpcError(
                    INVALID_PARAMS, f"the parameter 'paths' is a list of at most {MAX_PATHS}"
                )
            selected = [clean_path(path, tree.excluded) for path in wanted or []]
            if wanted is not None and not selected:
                _git, head, _branch = await anyio.to_thread.run_sync(tree.git_info)
                listing = Listing(head=head, git=True, paths=[], missing=[])
            else:
                listing = await anyio.to_thread.run_sync(tree.listing, scope, selected)
            offset = 0
        page, next_cursor = listing.page(offset)
        files, skipped, _signatures = await anyio.to_thread.run_sync(
            tree.describe, page, listing.git
        )
        self._listings.pop(listing.id, None)
        if next_cursor is not None:
            self._listings[listing.id] = listing
            while len(self._listings) > KEPT_LISTINGS:
                del self._listings[next(iter(self._listings))]
        return {
            "head": listing.head,
            "files": files,
            "skipped": skipped,
            "missing": listing.missing if offset == 0 else [],
            "next_cursor": next_cursor,
        }

    async def _read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.read`: a piece of a file and the hash of the whole file.

        Args:
            params: `path`, `offset` (default 0), `length` (default and at most
                `REQUEST_BYTES`; a larger value is read as that).

        Returns:
            `{content_b64, sha256, size, mode, eof}`.

        Raises:
            RpcError: `PATH_REFUSED`, `NOT_FOUND`, `NOT_REGULAR`, `TOO_LARGE` (the file is
                above the per-file limit), `IO_ERROR`, `INVALID_PARAMS`.
        """
        path = self._path(params)
        offset = int_param(params, "offset", 0, 0, BIGGEST) or 0
        length = min(int_param(params, "length", REQUEST_BYTES, 0, BIGGEST) or 0, REQUEST_BYTES)
        answer = await anyio.to_thread.run_sync(self.tree().read, path, offset, length)
        content = answer.pop("content")
        return {"content_b64": base64.b64encode(content).decode("ascii"), **answer}

    # ---------------------------------------------------------------------------- writes

    def _write_params(self, params: Mapping[str, Any]) -> tuple[str, str | None, int | None, bool]:
        """Read the parameters every kind of write shares.

        Args:
            params: The request's params.

        Returns:
            (`path`, `base_sha256`, `mode` or None, `during_turn`).

        Raises:
            RpcError: `PATH_REFUSED`; `INVALID_PARAMS` if `base_sha256` is absent or no
                hash, or `mode` is not an integer from 0 to 0o777.
        """
        return (
            self._path(params),
            sha_param(params, "base_sha256", nullable=True),
            int_param(params, "mode", None, 0, 0o777),
            _flag(params, "during_turn"),
        )

    async def _write(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.write`: create or replace a file with a content of at most 1 MiB.

        Side effects (in this order): refuses a held file; compares the file with the
        base; tells the attached session; writes a temporary file beside the target,
        compares again and renames it over the target (creating missing directories);
        logs the write; queues a change with the origin "ide".

        Args:
            params: `path`, `content_b64`, `base_sha256` (null: the file must not exist),
                `mode` (permission bits; absent keeps an existing file's, a new file gets
                0o644), `during_turn`.

        Returns:
            `{sha256, size}` of the written file.

        Raises:
            RpcError: `PATH_REFUSED`, `TOO_LARGE`, `HELD`, `BASE_MISMATCH`, `NOT_REGULAR`,
                `IO_ERROR`, `INVALID_PARAMS` (also for content that is not base64).
        """
        path, base, mode, during_turn = self._write_params(params)
        content = decode_content(params.get("content_b64"))
        tree = self.tree()
        if len(content) > tree.max_file_bytes:
            raise RpcError(
                TOO_LARGE,
                f"{path} would be larger than the limit of {tree.max_file_bytes} bytes per file",
                {"size": len(content), "limit": tree.max_file_bytes},
            )
        await self._held([path], during_turn)
        async with self._io:
            found = await anyio.to_thread.run_sync(tree.state, path)
            check_base(path, found, base, tree.max_file_bytes)
            await self._report([path])
            state = await anyio.to_thread.run_sync(tree.write, path, content, base, mode)
            self._wrote(path, base, state)
        log.info("ide.fs.write %s: %s -> %s, %d bytes", path, base, state.sha256, len(content))
        return {"sha256": state.sha256, "size": state.size}

    async def _begin(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.write.begin`: open a chunked upload; no file is changed.

        Side effects: discards uploads that were idle too long; registers the upload and
        starts the clean-up task.

        Args:
            params: `path`, `base_sha256`, `size` and `sha256` of the whole content,
                `mode`, `during_turn`.

        Returns:
            `{upload_id, chunk_size}`; a chunk carries at most `chunk_size` bytes.

        Raises:
            RpcError: `PATH_REFUSED`, `TOO_LARGE` (`size` above the per-file limit),
                `HELD`, `BASE_MISMATCH`, `NOT_REGULAR`, `UPLOAD_FAILED` (`reason:
                "too-many-uploads"`), `INVALID_PARAMS`.
        """
        path, base, mode, during_turn = self._write_params(params)
        size = int_param(params, "size", None, 0, BIGGEST)
        if size is None:
            raise RpcError(INVALID_PARAMS, "the parameter 'size' is missing")
        digest = sha_param(params, "sha256", nullable=False) or ""
        tree = self.tree()
        if size > tree.max_file_bytes:
            raise RpcError(
                TOO_LARGE,
                f"{path} would be larger than the limit of {tree.max_file_bytes} bytes per file",
                {"size": size, "limit": tree.max_file_bytes},
            )
        await self._expire()
        if len(self._uploads) >= MAX_UPLOADS:
            raise RpcError(
                UPLOAD_FAILED,
                f"{MAX_UPLOADS} uploads are open already; commit or abort one first",
                {"reason": "too-many-uploads", "received": 0},
            )
        await self._held([path], during_turn)
        found = await anyio.to_thread.run_sync(tree.state, path)
        check_base(path, found, base, tree.max_file_bytes)
        upload = Upload(uuid.uuid4().hex, path, base, size, digest, mode, during_turn)
        self._uploads[upload.id] = upload
        if self._janitor is None:
            self._janitor = anyio.CancelScope()
            self._tasks.start_soon(self._clean_up, self._janitor)
        return {"upload_id": upload.id, "chunk_size": REQUEST_BYTES}

    def _upload(self, params: Mapping[str, Any]) -> Upload:
        """Find the upload a request names. Side effect: the upload counts as used now.

        Args:
            params: The request's params with `upload_id`.

        Returns:
            The upload.

        Raises:
            RpcError: `UPLOAD_FAILED` (`reason: "unknown-upload"`) if no open upload has
                the id: it never existed, ended, or was discarded after `upload_idle_s`.
        """
        upload = self._uploads.get(str(params.get("upload_id") or ""))
        if upload is None:
            raise RpcError(
                UPLOAD_FAILED,
                "no upload with this id is open (it may have been discarded after "
                f"{self.upload_idle_s:.0f} s without a request)",
                {"reason": "unknown-upload", "received": 0},
            )
        upload.touched = time.monotonic()
        return upload

    async def _chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.write.chunk`: append one piece to an upload's temporary file.

        Side effects: the first chunk creates the target's missing directories and the
        temporary file beside the target. A chunk that would exceed the announced size,
        and a failed write, discard the upload.

        Args:
            params: `upload_id`, `offset` (must equal the bytes received so far),
                `content_b64` (at most `REQUEST_BYTES` raw).

        Returns:
            `{received}`: the bytes received so far.

        Raises:
            RpcError: `UPLOAD_FAILED` with `data.reason` ("unknown-upload", "offset",
                "size") and `data.received`; `TOO_LARGE`; `INVALID_PARAMS`; `IO_ERROR`.
        """
        upload = self._upload(params)
        offset = int_param(params, "offset", None, 0, BIGGEST)
        if offset is None:
            raise RpcError(INVALID_PARAMS, "the parameter 'offset' is missing")
        async with upload.lock:
            if upload.id not in self._uploads:
                self._upload(params)
            if offset != upload.received:
                raise RpcError(
                    UPLOAD_FAILED,
                    f"the chunk starts at {offset}, but {upload.received} bytes were received",
                    {"reason": "offset", "received": upload.received},
                )
            content = decode_content(params.get("content_b64"))
            try:
                if upload.received + len(content) > upload.size:
                    raise RpcError(
                        UPLOAD_FAILED,
                        f"the upload was announced with {upload.size} bytes and got more",
                        {"reason": "size", "received": upload.received},
                    )
                if upload.temp is None:
                    upload.temp = await anyio.to_thread.run_sync(self.tree().temp, upload.path)
                await anyio.to_thread.run_sync(upload.temp.put, content)
            except BaseException:
                await self._discard(upload)
                raise
            upload.received += len(content)
            upload.touched = time.monotonic()
            return {"received": upload.received}

    async def _commit(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.write.commit`: check an upload and put it in its target's place.

        Side effects: the upload ends, whatever the outcome; on any refusal its temporary
        file is removed and the target is untouched. Otherwise as `_write`: hold, base,
        report to the session, comparison and rename in one step, log, change "ide".

        Args:
            params: `upload_id`.

        Returns:
            `{sha256, size}` of the written file.

        Raises:
            RpcError: `UPLOAD_FAILED` with `data.reason` ("unknown-upload", "size" if
                fewer bytes arrived than announced, "sha256" if the content's hash is not
                the announced one); `HELD`; `BASE_MISMATCH`; `NOT_REGULAR`; `IO_ERROR`.
        """
        upload = self._upload(params)
        async with upload.lock:
            if self._uploads.pop(upload.id, None) is None:
                self._upload(params)
            tree = self.tree()
            try:
                received = upload.temp.hasher.hexdigest() if upload.temp else _EMPTY_SHA256
                if upload.received != upload.size or received != upload.sha256:
                    reason = "size" if upload.received != upload.size else "sha256"
                    raise RpcError(
                        UPLOAD_FAILED,
                        f"the uploaded content of {upload.path} is not the announced one "
                        f"({reason})",
                        {"reason": reason, "received": upload.received},
                    )
                await self._held([upload.path], upload.during_turn)
                async with self._io:
                    found = await anyio.to_thread.run_sync(tree.state, upload.path)
                    check_base(upload.path, found, upload.base, tree.max_file_bytes)
                    if upload.temp is None:
                        upload.temp = await anyio.to_thread.run_sync(tree.temp, upload.path)
                    await self._report([upload.path])
                    temp, upload.temp = upload.temp, None
                    state = await anyio.to_thread.run_sync(
                        tree.commit, temp, upload.base, upload.mode
                    )
                    self._wrote(upload.path, upload.base, state)
            except BaseException:
                await self._discard(upload)
                raise
        log.info(
            "ide.fs.write %s: %s -> %s, %d bytes (chunked)",
            upload.path,
            upload.base,
            state.sha256,
            upload.size,
        )
        return {"sha256": state.sha256, "size": state.size}

    async def _abort(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.write.abort`: give an upload up.

        Side effect: removes the upload's temporary file and the directories made for it.

        Args:
            params: `upload_id`.

        Returns:
            `{}`, also for an upload that is not open any more.
        """
        upload = self._uploads.get(str(params.get("upload_id") or ""))
        if upload is not None:
            async with upload.lock:
                await self._discard(upload)
        return {}

    async def _discard(self, upload: Upload) -> None:
        """End an upload without writing its target. Never raises.

        Side effects: forgets the upload; removes its temporary file and the directories
        that were created for it, in a worker thread, shielded from cancellation.

        Args:
            upload: The upload.
        """
        self._uploads.pop(upload.id, None)
        temp, upload.temp = upload.temp, None
        if temp is not None:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(temp.discard)

    async def _expire(self) -> None:
        """Discard the uploads without a request for `upload_idle_s` seconds."""
        now = time.monotonic()
        for upload in list(self._uploads.values()):
            if now - upload.touched > self.upload_idle_s and not upload.lock.locked():
                log.info("the upload of %s was idle and is discarded", upload.path)
                await self._discard(upload)

    async def _clean_up(self, scope: anyio.CancelScope) -> None:
        """Discard idle uploads until none is open; runs as a task.

        Args:
            scope: The scope `close` cancels.
        """
        with scope:
            while self._uploads:
                await anyio.sleep(max(0.05, min(5.0, self.upload_idle_s / 4)))
                await self._expire()
        if self._janitor is scope:
            self._janitor = None

    async def _delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.delete`: delete a file that is at the base.

        Side effects: hold, base, report to the session; `unlink`; the file's directories
        that became empty are removed; log; change "ide" with `deleted`.

        Args:
            params: `path`, `base_sha256` (the hash the file must have), `during_turn`.

        Returns:
            `{}`.

        Raises:
            RpcError: `PATH_REFUSED`, `HELD`, `BASE_MISMATCH` (also for a file that is
                gone: `data.sha256` null), `NOT_REGULAR`, `IO_ERROR`, `INVALID_PARAMS`.
        """
        path = self._path(params)
        base = sha_param(params, "base_sha256", nullable=False) or ""
        tree = self.tree()
        await self._held([path], _flag(params, "during_turn"))
        async with self._io:
            found = await anyio.to_thread.run_sync(tree.state, path)
            check_base(path, found, base, tree.max_file_bytes)
            await self._report([path])
            await anyio.to_thread.run_sync(tree.delete, path, base)
            self._known.pop(path, None)
            self._emit(Change(path, base, None, "ide"))
        log.info("ide.fs.delete %s: %s", path, base)
        return {}

    async def _rename(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.rename`: move a file that is at the base to a free path.

        Side effects: hold (both paths), base, report to the session; the target's missing
        directories are created, the source's empty ones removed; log; two changes "ide".

        Args:
            params: `from`, `to`, `base_sha256` (the hash of `from`), `during_turn`.

        Returns:
            `{}`.

        Raises:
            RpcError: `PATH_REFUSED`, `NOT_FOUND` (no file at `from`), `TARGET_EXISTS`
                (`data.sha256`), `HELD`, `BASE_MISMATCH`, `NOT_REGULAR`, `IO_ERROR`,
                `INVALID_PARAMS`.
        """
        source, target = self._path(params, "from"), self._path(params, "to")
        base = sha_param(params, "base_sha256", nullable=False) or ""
        tree = self.tree()
        await self._held([source, target], _flag(params, "during_turn"))
        async with self._io:
            found = await anyio.to_thread.run_sync(tree.state, source)
            if found.kind == "missing":
                raise RpcError(NOT_FOUND, f"{source} does not exist")
            check_base(source, found, base, tree.max_file_bytes)
            there = await anyio.to_thread.run_sync(tree.state, target)
            if there.kind != "missing":
                raise RpcError(TARGET_EXISTS, f"{target} exists", {"sha256": there.sha256})
            await self._report(edited_paths([source, target]))
            state = await anyio.to_thread.run_sync(tree.rename, source, target, base)
            self._known.pop(source, None)
            self._emit(Change(source, base, None, "ide"))
            self._wrote(target, None, state)
        log.info("ide.fs.rename %s -> %s: %s", source, target, base)
        return {}

    # ------------------------------------------------------------------- subscription

    async def _subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.subscribe`: from the answer on, changes arrive as `ide.fs.changed`.

        Side effects: the first subscription scans the files once (size, mtime and inode;
        no content is read) and starts the watcher. The replay of a renewed subscription
        is sent right after the answer.

        Args:
            params: `after_seq` and `epoch` of the last notification the editor saw, or
                neither for a first subscription.

        Returns:
            `{epoch, seq, complete}`. `complete` false: the editor compares everything
            (the epoch is another one, notifications after `after_seq` are no longer
            kept, or the watcher did not run since).

        Raises:
            RpcError: `INVALID_PARAMS`; `NO_MANIFEST` if the files cannot be listed.
        """
        after_seq = int_param(params, "after_seq", None, 0, BIGGEST)
        epoch = params.get("epoch")
        if epoch is not None and not isinstance(epoch, str):
            raise RpcError(INVALID_PARAMS, "the parameter 'epoch' must be a string")
        replay = self.changes.since(after_seq, epoch)
        if self._watcher is None:
            await self._scan(baseline=True)
            self._watcher = anyio.CancelScope()
            self._tasks.start_soon(self._subscribed, self._watcher)
        self._released = anyio.Event()
        self._replay = list(replay or [])
        return {
            "epoch": self.changes.epoch,
            "seq": self.changes.seq,
            "complete": replay is not None,
        }

    async def _unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `ide.fs.unsubscribe`: no further `ide.fs.changed`.

        Side effects:
            - Stops the watcher.
            - Numbers the changes that were collected but not yet sent and keeps them in the
              replay buffer; they are not sent to the editor.
            - Forgets the scan baseline, so that a later subscription compares everything.
            - A later subscription that names a sequence number is answered with
              `complete: false`, because changes are not watched meanwhile.

        Args:
            params: Unused.

        Returns:
            `{}`.
        """
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
            self.changes.cut()
            self.changes.gap()
            self._known = {}
            self._skipped = {}
        return {}

    async def _subscribed(self, scope: anyio.CancelScope) -> None:
        """Run the watcher and the sender of the notifications; runs as a task.

        Args:
            scope: The scope `_unsubscribe` and `close` cancel.
        """
        with scope:
            async with anyio.create_task_group() as both:
                both.start_soon(self._flush)
                await self._watch()

    async def _watch(self) -> None:
        """Scan the files again and again until the subscription ends.

        A scan starts `poll_s` seconds after the one before (later if scans are slow), or
        earlier when a session event asks for a look. After a scan that event waits for
        `min(2 * the scan's duration, poll_s)` seconds before the next scan starts. A
        failing scan is logged and tried again.
        """
        wait_s, gap_s = self.poll_s, 0.0
        while True:
            await anyio.sleep(gap_s)
            with anyio.move_on_after(max(0.0, wait_s - gap_s)):
                await self._wake.wait()
            self._wake = anyio.Event()
            started = time.monotonic()
            try:
                await self._scan()
            except RpcError as error:
                log.warning("the files could not be scanned: %s", error.message)
            except Exception:
                log.exception("scanning the files failed")
            took_s = time.monotonic() - started
            wait_s, gap_s = poll_interval(took_s, self.poll_s), min(2 * took_s, self.poll_s)

    async def _scan(self, baseline: bool = False) -> None:
        """Compare the listed files with the last scan and queue what changed.

        Side effects: holds the write lock; one listing, one `lstat` per file, and a read
        of every file whose signature changed. A changed file whose content is the known
        one is not reported. A file that left the listing is reported as deleted only if
        nothing is at its path any more. A listed path that is skipped now, and was not
        skipped for the same reason at the scan before, is reported in `skipped`. The editor
        can then list it at once, without waiting for its next full comparison. Examples:

        - a new file above the size limit
        - a file that grew above the size limit
        - a FIFO
        - a link

        Args:
            baseline: True for the first scan: the signatures are taken, nothing is
                reported and no file is read.

        Raises:
            RpcError: `NO_MANIFEST` if the files cannot be listed.
        """
        tree = self.tree()
        async with self._io:
            listing = await anyio.to_thread.run_sync(tree.listing)
            _files, skipped_now, current = await anyio.to_thread.run_sync(
                tree.describe, listing.paths, listing.git, False
            )
            skipped = {entry["path"]: entry["reason"] for entry in skipped_now}
            skipped_before, self._skipped = self._skipped, skipped
            # Taken after the look at the files: an event that arrived meanwhile may be
            # about a change this scan already sees.
            hints = Hints() if baseline else self.turn.take_hints()
            if baseline:
                self._known = {
                    path: (found, tree.cache.seen(path, found)) for path, found in current.items()
                }
                return
            for path, reason in skipped.items():
                if skipped_before.get(path) != reason and self._watcher is not None:
                    self.changes.skip(path, reason)
                    self._dirty.set()
            before = {path: known[0] for path, known in self._known.items()}
            changed, gone = changed_paths(before, current)
            if changed:
                files, _skipped, read = await anyio.to_thread.run_sync(
                    tree.describe, changed, listing.git, True
                )
                for entry in files:
                    path, digest = entry["path"], entry["sha256"]
                    known = self._known.get(path)
                    self._known[path] = (read[path], digest)
                    if known is None or known[1] != digest:
                        earlier = known[1] if known is not None else None
                        self._emit(Change(path, earlier, digest, hints.origin(path)))
            if gone:
                kinds = await anyio.to_thread.run_sync(
                    lambda: {path: tree.kind(path) for path in gone}
                )
                for path in gone:
                    _signature, digest = self._known.pop(path)
                    if kinds[path] == "missing":
                        tree.cache.forget(path)
                        self._emit(Change(path, digest, None, hints.origin(path)))

    async def _flush(self) -> None:
        """Send the queued changes as `ide.fs.changed` until the subscription ends.

        Changes are sent after `quiet_s` seconds without a further one, at the latest
        `BATCH_LATEST_S` seconds after the first, and never before the answer to
        `ide.fs.subscribe` is written. A notification that cannot be written is logged.
        """
        while True:
            await self._dirty.wait()
            first = time.monotonic()
            while True:
                self._dirty = anyio.Event()
                with anyio.move_on_after(self.quiet_s):
                    await self._dirty.wait()
                if not self._dirty.is_set() or time.monotonic() - first > BATCH_LATEST_S:
                    break
            self._dirty = anyio.Event()
            await self._released.wait()
            for note in self.changes.cut():
                try:
                    await self._notify("ide.fs.changed", note)
                except Exception:
                    log.info("a change notification could not be written", exc_info=True)


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
"""The hash of an upload that announced, and got, no content."""
