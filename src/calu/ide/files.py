"""File synchronisation of `calu ide`: the rules, without any file access.

    request path ──► clean_path ──► a canonical relative path, or the error "path refused"
    git's output ──► listed_paths / status_paths ──► the paths a manifest is made of
    (path, size, mtime, inode) ──► HashCache ──► the hash, if the file did not change
    session events ──► TurnFiles ──► which files the agent edits in the running turn (held),
                                     and which changes a scan may attribute to the agent
    changes ──► ChangeLog ──► `ide.fs.changed` notifications: merged per path, cut into
                              batches, numbered, kept for a replay

The contract is `agent/records/notes/jetbrains-sync-spec.md`. This module holds what can be decided
from values alone; `calu.ide.filestore` touches the files and `calu.ide.filesync` answers the
requests. Every function here is free of side effects unless its docstring names one.
"""

import base64
import binascii
import os
import re
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from calu.acp.peer import RpcError
from calu.checkers.policy import EDIT_TOOLS
from calu.paths import calu_private_dirs

INVALID_PARAMS = -32602
"""JSON-RPC's code for a parameter of the wrong type or value."""

PATH_REFUSED = -32030
"""The path is not a canonical relative path, leaves the root, or is in an excluded directory."""

BASE_MISMATCH = -32031
"""The file is not in the state the request's `base_sha256` names."""

TOO_LARGE = -32032
"""The content is above the per-file limit, or one request carries more than `REQUEST_BYTES`."""

NOT_REGULAR = -32033
"""The path, or a directory on the way to it, is not what the request needs: a directory, a
symbolic link, a device, a FIFO or a socket where a regular file is expected."""

NOT_FOUND = -32034
"""`ide.fs.read` or `ide.fs.rename` of a file that does not exist."""

HELD = -32035
"""The agent edits the file in the running turn and the request did not say `during_turn`."""

UPLOAD_FAILED = -32036
"""Unknown or expired upload, wrong offset, too many uploads, or size or hash differ."""

IO_ERROR = -32037
"""The operating system refused (permissions, disk full, read-only file system)."""

TARGET_EXISTS = -32038
"""`ide.fs.rename` to a path that exists."""

NO_MANIFEST = -32039
"""The file listing failed, is too long, or the cursor of a page is no longer valid."""

REQUEST_BYTES = 1024 * 1024
"""Raw content one request or response carries at most (1 MiB)."""

MAX_FILE_BYTES = 32 * 1024 * 1024
"""The per-file limit: a larger file is listed as skipped and neither read nor written."""

PAGE_ENTRIES = 2000
"""Paths one page of a manifest covers."""

PAGE_PATH_CHARS = 256 * 1024
"""Characters of paths one manifest page or one notification holds at most, so that a line
of the protocol stays below 2 MiB whatever the paths' lengths are."""

MAX_LISTED = 200_000
"""Paths one listing holds at most; a longer one is the error `NO_MANIFEST`."""

MAX_PATHS = 2000
"""Entries the `paths` filter of one manifest request holds at most."""

MAX_UPLOADS = 4
"""Chunked uploads one connection may have open."""

UPLOAD_IDLE_S = 120.0
"""Seconds without a request after which an open upload is discarded."""

BATCH_QUIET_S = 0.2
"""Seconds without a further change before the collected changes are sent."""

BATCH_LATEST_S = 1.0
"""Seconds after which collected changes are sent although more keep arriving."""

BATCH_CHANGES = 500
"""Changes one `ide.fs.changed` notification holds at most."""

KEPT_NOTIFICATIONS = 1000
"""`ide.fs.changed` notifications kept for a replay."""

UPLOAD_PREFIX = ".calu-upload-"
"""Start of the name of the temporary file of a write; such files are never listed."""

TRACE_DIRECTORY = "agent/traces"
"""The default directory of the session traces; excluded whatever the settings say."""

WALK_IGNORED = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".ruff_cache", ".idea"}
)
"""Directory names a listing outside a git repository does not enter."""

MAX_PATH_BYTES = 4096
"""The longest path a request may name, in bytes of its UTF-8 form."""

MAX_SEGMENT_BYTES = 255
"""The longest path segment, in bytes of its UTF-8 form."""

ACTIVE_STATES = frozenset({"running", "checking", "tool_call", "escalated"})
"""The values of `session.state` during which the turn's files are held."""

COMMAND_TOOLS = frozenset({"Bash"})
"""Tools that may write any file: a change found while one runs is attributed to the agent."""

CAPABILITIES = ("cwd", "ping", "paths", "fs", "fs.watch")
"""What `ide.initialize` lists; a result without the list is a bridge without these."""

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DRIVE = re.compile(r"[A-Za-z]:")

Signature = tuple[int, int, int]
"""What tells whether a file changed without reading it: (size, mtime in ns, inode)."""


def refuse(reason: str, path: object) -> NoReturn:
    """Refuse a path: always raises.

    Args:
        reason: "outside", "excluded" or "invalid".
        path: The refused value, for the message (cut and made printable).

    Raises:
        RpcError: `PATH_REFUSED` with `data.reason`.
    """
    shown = repr(path)
    shown = shown if len(shown) <= 120 else shown[:117] + "..."
    what = {
        "outside": "leaves the project directory",
        "excluded": "is in a directory the editor does not synchronise",
        "invalid": "is not a relative path of the form a/b/c",
    }[reason]
    raise RpcError(PATH_REFUSED, f"the path {shown} {what}", {"reason": reason})


def is_excluded(path: str, prefixes: Collection[str]) -> bool:
    """Tell whether a canonical relative path lies in a directory that is never synchronised.

    Args:
        path: A relative path with "/" separators.
        prefixes: Further excluded directories, relative to the root; "" excludes everything.

    Returns:
        True for a path with a `.git` segment (in any letter case), for a path with a
        segment that starts with `UPLOAD_PREFIX`, and for a path that equals a prefix or
        lies below one.
    """
    segments = path.split("/")
    if any(s.casefold() == ".git" or s.startswith(UPLOAD_PREFIX) for s in segments):
        return True
    return any(p == "" or path == p or path.startswith(p + "/") for p in prefixes)


def has_plain_name(path: str) -> bool:
    """Tell whether a path of the file system can be named in the protocol.

    Args:
        path: A relative path as the file system or git gave it (decoded with
            `os.fsdecode`, so undecodable bytes are lone surrogates).

    Returns:
        False for a path with a control character, a backslash, bytes that are not UTF-8,
        an empty, `.` or `..` segment, or a length above `MAX_PATH_BYTES` (a segment above
        `MAX_SEGMENT_BYTES`); True otherwise.
    """
    if not path or "\\" in path:
        return False
    if any(unicodedata.category(char) in ("Cc", "Cs") for char in path):
        return False
    if len(path.encode()) > MAX_PATH_BYTES:
        return False
    return all(
        segment not in ("", ".", "..") and len(segment.encode()) <= MAX_SEGMENT_BYTES
        for segment in path.split("/")
    )


def clean_path(value: object, prefixes: Collection[str] = ()) -> str:
    """Check the path of a request; it is returned unchanged or refused, never repaired.

    Args:
        value: The request's value.
        prefixes: Excluded directories besides `.git` (`is_excluded`).

    Returns:
        The path: relative, "/" separators, no empty, `.` or `..` segment.

    Raises:
        RpcError: `PATH_REFUSED` with `data.reason`: "outside" for an absolute path, a path
            with a drive letter and a path with a `..` segment; "invalid" for a value that
            is not a string, is empty, holds a NUL or another control character, a
            backslash, an empty or `.` segment, or is too long; "excluded" for a path in
            an excluded directory.

    Example:
        `clean_path("src/a.py")` is `"src/a.py"`; `clean_path("src//a.py")`,
        `clean_path("../a")`, `clean_path("C:/a")` and `clean_path(".git/config")` raise.
    """
    if not isinstance(value, str) or not value:
        refuse("invalid", value)
    if any(unicodedata.category(char) in ("Cc", "Cs") for char in value) or "\\" in value:
        refuse("invalid", value)
    segments = value.split("/")
    if value.startswith("/") or _DRIVE.match(value) or ".." in segments:
        refuse("outside", value)
    if not has_plain_name(value):
        refuse("invalid", value)
    if is_excluded(value, prefixes):
        refuse("excluded", value)
    return value


def private_prefixes(root: Path, environ: Mapping[str, str] | None = None) -> list[str]:
    """Return calu's own directories that lie inside a project directory.

    Side effect: `calu.paths.calu_private_dirs` resolves links, so it reads the file system.

    Args:
        root: The resolved project directory.
        environ: The environment the XDG variables are read from; None uses `os.environ`.

    Returns:
        The directories relative to `root`, with "/" separators; `[""]` if `root` itself is
        one of calu's directories or lies in one (then every path is excluded).
    """
    found: list[str] = []
    for directory in calu_private_dirs(environ):
        if directory == root or directory in root.parents:
            return [""]
        if root in directory.parents:
            relative = directory.relative_to(root).as_posix()
            if relative not in found:
                found.append(relative)
    return found


def session_prefixes(values: object) -> tuple[str, ...]:
    """Read the excluded directories a session answered `files.user_edit` with.

    The answer comes from another process, so each entry is checked like a request's path.

    Args:
        values: The `excluded` value of the answer.

    Returns:
        The entries that are canonical relative paths; everything else is dropped.
    """
    if not isinstance(values, list):
        return ()
    kept: list[str] = []
    for value in values:
        try:
            kept.append(clean_path(value.rstrip("/") if isinstance(value, str) else value))
        except RpcError:
            continue
    return tuple(kept)


def sha_param(params: Mapping[str, Any], key: str, nullable: bool) -> str | None:
    """Read a hash parameter, which must be present.

    Args:
        params: The request's params.
        key: The parameter's name, e.g. "base_sha256".
        nullable: Whether null (the file must not exist) is a value.

    Returns:
        The 64 lowercase hex digits, or None for null.

    Raises:
        RpcError: `INVALID_PARAMS` if the key is absent, null though not nullable, or not a
            hash.
    """
    if key not in params:
        raise RpcError(INVALID_PARAMS, f"the parameter {key!r} is missing")
    value = params[key]
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RpcError(INVALID_PARAMS, f"the parameter {key!r} must be 64 lowercase hex digits")
    return value


def int_param(
    params: Mapping[str, Any], key: str, default: int | None, lowest: int, highest: int
) -> int | None:
    """Read an integer parameter within bounds.

    Args:
        params: The request's params.
        key: The parameter's name.
        default: The value of an absent or null parameter.
        lowest: The smallest value allowed.
        highest: The largest value allowed.

    Returns:
        The integer, or `default`.

    Raises:
        RpcError: `INVALID_PARAMS` if the value is not an integer or out of bounds.
    """
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lowest <= value <= highest:
        raise RpcError(
            INVALID_PARAMS, f"the parameter {key!r} must be an integer from {lowest} to {highest}"
        )
    return value


def decode_content(value: object, limit: int = REQUEST_BYTES) -> bytes:
    """Decode the `content_b64` of a request; the size is checked before anything is decoded.

    Args:
        value: The request's value.
        limit: The most raw bytes the content may have.

    Returns:
        The bytes.

    Raises:
        RpcError: `TOO_LARGE` (`data.size`: the raw size the text stands for, `data.limit`)
            if the text is longer than the base64 form of `limit` bytes; `INVALID_PARAMS` if
            the value is not a string or not canonical base64 (standard alphabet, padded,
            no white space).
    """
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, "the parameter 'content_b64' must be a string")
    if len(value) > 4 * ((limit + 2) // 3):
        raise RpcError(
            TOO_LARGE,
            f"one request carries at most {limit} bytes of content",
            {"size": len(value) // 4 * 3, "limit": limit},
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RpcError(INVALID_PARAMS, f"content_b64 is not base64: {error}") from None


class HashCache:
    """Remembers file hashes by path and signature, so an unchanged file is not read again.

    An entry is only stored for a file whose mtime lies at least `racy_ns` before the moment
    it was hashed: a write within the same clock tick as the hashed content would leave the
    signature unchanged. `last` also answers for those files; it is what a change
    notification names as the content before.
    """

    def __init__(self, racy_ns: int = 2_000_000_000, clock: Callable[[], int] = time.time_ns):
        """Create an empty cache.

        Args:
            racy_ns: How old (in ns) a file's mtime must be for its hash to be kept.
            clock: Returns the wall-clock time in ns.
        """
        self._racy_ns = racy_ns
        self._clock = clock
        self._entries: dict[str, tuple[Signature, str]] = {}
        self._last: dict[str, tuple[Signature, str]] = {}

    def get(self, path: str, signature: Signature) -> str | None:
        """Return the stored hash of a file if its signature is the stored one.

        Args:
            path: The relative path.
            signature: The file's present signature.

        Returns:
            The hash, or None if the file has to be read.
        """
        entry = self._entries.get(path)
        return entry[1] if entry is not None and entry[0] == signature else None

    def put(self, path: str, signature: Signature, sha256: str) -> None:
        """Remember a hash that was just computed. Side effect: changes the cache.

        Args:
            path: The relative path.
            signature: The signature the file had before and after it was read.
            sha256: The hash of what was read.
        """
        self._last[path] = (signature, sha256)
        if self._clock() - signature[1] >= self._racy_ns:
            self._entries[path] = (signature, sha256)
        else:
            self._entries.pop(path, None)

    def last(self, path: str) -> str | None:
        """Return the hash last computed for a path, whatever its signature is now.

        Args:
            path: The relative path.

        Returns:
            The hash, or None if the path was never hashed or was forgotten.
        """
        entry = self._last.get(path)
        return entry[1] if entry is not None else None

    def seen(self, path: str, signature: Signature) -> str | None:
        """Return the hash last computed for a path if the file had this signature then.

        Unlike `get` this also answers for a file that was written a moment before it was
        hashed. Use it where a stale answer only makes a notification's "before" hash one
        write old, never to decide that a file needs no reading.

        Args:
            path: The relative path.
            signature: The file's present signature.

        Returns:
            The hash, or None.
        """
        entry = self._last.get(path)
        return entry[1] if entry is not None and entry[0] == signature else None

    def forget(self, path: str) -> None:
        """Drop what is known about a path (it was deleted). Side effect: changes the cache.

        Args:
            path: The relative path.
        """
        self._entries.pop(path, None)
        self._last.pop(path, None)


def listed_paths(output: bytes) -> list[str]:
    """Parse the output of `git ls-files -z`.

    Args:
        output: NUL-separated paths, relative to the directory git ran in.

    Returns:
        The paths, decoded with `os.fsdecode`, without duplicates, sorted; a trailing "/"
        (git's mark for an untracked nested repository) is removed.
    """
    return sorted({os.fsdecode(raw).rstrip("/") for raw in output.split(b"\0") if raw})


def status_paths(output: bytes, prefix: str = "") -> list[str]:
    """Parse the output of `git status --porcelain=v2 -z`: every path it mentions.

    Args:
        output: The records. Ordinary entries start with "1 " (8 fields before the path),
            renames with "2 " (9 fields; the original path is the next NUL-separated
            record), unmerged entries with "u " (10 fields), untracked files with "? ".
        prefix: The directory git ran in, relative to the repository's root, with a
            trailing "/" ("" at the root): status paths are relative to the root, and the
            result is relative to that directory.

    Returns:
        The paths at or below `prefix`, relative to it, without duplicates, sorted. Whether
        each exists is the caller's question.
    """
    records = output.split(b"\0")
    found: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        fields_before = {b"1 ": 8, b"2 ": 9, b"u ": 10, b"? ": 1}.get(record[:2])
        if fields_before is None:
            continue
        parts = record.split(b" ", fields_before)
        if len(parts) <= fields_before:
            continue
        raws = [parts[fields_before]]
        if record[:2] == b"2 " and index < len(records):
            raws.append(records[index])
            index += 1
        for raw in raws:
            path = os.fsdecode(raw).rstrip("/")
            if path.startswith(prefix) and len(path) > len(prefix):
                found.add(path[len(prefix) :])
    return sorted(found)


def under(path: str, selected: Collection[str]) -> bool:
    """Tell whether a path is one of the selected paths or lies below one.

    Args:
        path: A relative path.
        selected: Files or directories, relative.

    Returns:
        True if `path` equals an entry or starts with an entry plus "/".
    """
    return any(path == entry or path.startswith(entry + "/") for entry in selected)


@dataclass
class Listing:
    """The paths of one manifest, kept between its pages."""

    head: str | None
    """HEAD when the listing was made; None outside git or without a commit."""
    git: bool
    """Whether git made the listing (a listed directory is then a submodule)."""
    paths: list[str]
    """Every listed path, sorted; described page by page."""
    missing: list[str]
    """Requested or tracked paths that do not exist; reported with the first page."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def page(self, offset: int) -> tuple[list[str], str | None]:
        """Return the paths of the page that starts at an offset.

        Args:
            offset: Index of the page's first path.

        Returns:
            (at most `PAGE_ENTRIES` paths with at most about `PAGE_PATH_CHARS` characters,
            the cursor of the next page or None).
        """
        end, chars = offset, 0
        while end < len(self.paths) and end - offset < PAGE_ENTRIES and chars < PAGE_PATH_CHARS:
            chars += len(self.paths[end])
            end += 1
        return self.paths[offset:end], f"{self.id}:{end}" if end < len(self.paths) else None


def parse_cursor(cursor: object) -> tuple[str, int]:
    """Split the `cursor` of a manifest request.

    Args:
        cursor: The request's value: `<listing id>:<offset>`.

    Returns:
        (listing id, offset).

    Raises:
        RpcError: `INVALID_PARAMS` if the value has another form.
    """
    if isinstance(cursor, str):
        listing_id, _, offset = cursor.partition(":")
        if listing_id and offset.isascii() and offset.isdigit() and len(offset) < 10:
            return listing_id, int(offset)
    raise RpcError(INVALID_PARAMS, "the parameter 'cursor' is not a cursor of ide.fs.manifest")


@dataclass(frozen=True)
class Change:
    """One file changed on the bridge's side."""

    path: str
    before: str | None
    """The hash before; None if the file did not exist or the bridge does not know."""
    after: str | None
    """The hash now; None if the file is gone."""
    origin: str
    """ "agent", "ide" or "other"."""

    def json(self) -> dict[str, Any]:
        """Render the change as an entry of `ide.fs.changed`.

        Returns:
            `{path, before_sha256, after_sha256, deleted, origin}`.
        """
        return {
            "path": self.path,
            "before_sha256": self.before,
            "after_sha256": self.after,
            "deleted": self.after is None,
            "origin": self.origin,
        }


class ChangeLog:
    """Collects changes and cuts them into numbered `ide.fs.changed` notifications.

    add(change), skip(path, reason) ... ──► cut() ──► [{epoch, seq, changes, skipped}, ...]
                                                kept: the last `KEPT_NOTIFICATIONS`
    since(after_seq, epoch) ──► the kept notifications after `after_seq`, or None if
                                they are not all there
    """

    def __init__(self, kept: int = KEPT_NOTIFICATIONS, batch: int = BATCH_CHANGES) -> None:
        """Create a log with a random epoch and the sequence number 0.

        Args:
            kept: Notifications kept for a replay.
            batch: Changes per notification.
        """
        self.epoch = uuid.uuid4().hex
        """A random id of this bridge process."""
        self.seq = 0
        """The number of the last notification that was cut."""
        self.continuous_from = 0
        """A subscriber whose last number is below this one may have missed changes: they
        were not watched for a while (`gap`)."""
        self._batch = batch
        self._pending: dict[str, Change] = {}
        self._skipped: dict[str, str] = {}
        self._kept: deque[dict[str, Any]] = deque(maxlen=kept)

    def __bool__(self) -> bool:
        """Tell whether changes wait to be cut.

        Returns:
            True if `add` or `skip` took something since the last `cut`.
        """
        return bool(self._pending) or bool(self._skipped)

    def skip(self, path: str, reason: str) -> None:
        """Collect a path that became one the bridge does not synchronise.

        Side effect: drops a waiting change of the same path (the path is reported as
        skipped instead, the later fact). A later `add` of the path drops the skip in turn.

        Args:
            path: The path as a manifest's `skipped` entry names it.
            reason: The manifest's reason ("too-large", "not-regular", "symlink",
                "submodule", "unsupported-name").
        """
        self._pending.pop(path, None)
        self._skipped[path] = reason

    def add(self, change: Change) -> None:
        """Collect one change. Side effect: merges it with a waiting change of the same path.

        Merged, the earlier `before`, the later `after` and the later `origin` stay. A file
        that is back at a known earlier content (`before == after`) is dropped.

        Args:
            change: The change.
        """
        self._skipped.pop(change.path, None)
        earlier = self._pending.pop(change.path, None)
        if earlier is not None:
            change = Change(change.path, earlier.before, change.after, change.origin)
            if change.before is not None and change.before == change.after:
                return
        self._pending[change.path] = change

    def cut(self) -> list[dict[str, Any]]:
        """Turn the collected changes into notifications. Side effect: numbers and keeps them.

        Returns:
            The params of the new `ide.fs.changed` notifications, in order: at most `batch`
            entries of `changes` and `skipped` together with at most about
            `PAGE_PATH_CHARS` characters of paths each, `seq` one above the notification
            before. `skipped` holds `{path, reason}` per path that became one the bridge
            does not synchronise (the shape of a manifest's `skipped`); it is empty in most
            notifications.
        """
        entries: list[tuple[str, dict[str, Any]]] = [
            ("changes", change.json()) for change in self._pending.values()
        ] + [("skipped", {"path": path, "reason": why}) for path, why in self._skipped.items()]
        self._pending, self._skipped = {}, {}
        cut: list[dict[str, Any]] = []
        note: dict[str, Any] = {"changes": [], "skipped": []}
        count = chars = 0
        for index, (kind, entry) in enumerate(entries):
            note[kind].append(entry)
            count += 1
            chars += len(entry["path"])
            if count == self._batch or chars >= PAGE_PATH_CHARS or index == len(entries) - 1:
                self.seq += 1
                cut.append({"epoch": self.epoch, "seq": self.seq, **note})
                note, count, chars = {"changes": [], "skipped": []}, 0, 0
        self._kept.extend(cut)
        return cut

    def gap(self) -> None:
        """Record that changes were not watched from now on (the watcher stopped).

        Side effect: a later `since` with a number up to the present one answers None.
        """
        self.continuous_from = self.seq + 1

    def since(self, after_seq: int | None, epoch: str | None) -> list[dict[str, Any]] | None:
        """Return what a subscriber missed, if all of it is still known.

        Args:
            after_seq: The last number the subscriber saw; None if it names none.
            epoch: The epoch the subscriber saw it in; None if it names none.

        Returns:
            The kept notifications after `after_seq`, in order (empty without `after_seq`
            and `epoch`: a first subscription misses nothing). None if the epoch is another
            one, the number is above the present one, a notification after it was dropped,
            or changes were not watched since (`gap`).
        """
        if after_seq is None and epoch is None:
            return []
        if epoch != self.epoch or after_seq is None or not 0 <= after_seq <= self.seq:
            return None
        if after_seq < self.continuous_from:
            return None
        missed = [kept for kept in self._kept if kept["seq"] > after_seq]
        if len(missed) != self.seq - after_seq:
            return None
        return missed


@dataclass
class Hints:
    """What the session's events say about the changes the next scan finds."""

    paths: set[str] = field(default_factory=set)
    """Files an edit tool call named: a change of one is the agent's."""
    command: bool = False
    """A command ran: every change found is attributed to the agent."""

    def origin(self, path: str) -> str:
        """Name the origin of a change the scan found.

        Args:
            path: The changed file.

        Returns:
            "agent" or "other".
        """
        return "agent" if self.command or path in self.paths else "other"


class TurnFiles:
    """Follows the attached session's events: the held files and the hints for the scan.

        session.state  running/checking/tool_call/escalated ─► the turn's files are held
                       idle/stopped/failed, or turn.result  ─► nothing is held
        edit.diff, tool.use of an edit tool, tool.files     ─► the file joins the turn's files
        tool.result                                         ─► look at the files now

    A path is held while the state is one of `ACTIVE_STATES` and the path joined the
    turn's files since the last turn ended (specification section 6).
    """

    def __init__(self, workspace: Path) -> None:
        """Create the follower with no session attached.

        Args:
            workspace: The bridge's resolved project directory; the events' absolute paths
                are made relative to it.
        """
        self._workspace = workspace
        self.session_id = ""
        """The attached session's id; "" while none is attached."""
        self.state = "stopped"
        self._files: set[str] = set()
        self._calls: dict[str, tuple[str, set[str]]] = {}
        self._hints = Hints()

    def attach(self, session_id: str, state: object, events: Iterable[Mapping[str, Any]]) -> None:
        """Start following a session. Side effect: forgets the session followed before.

        Args:
            session_id: The session's id.
            state: The `state` of its snapshot.
            events: The events replayed at the attach (`{method, params}` each), oldest
                first; the snapshot's state is applied after them.
        """
        self.detach()
        self.session_id = session_id
        for event in events:
            params = event.get("params")
            self.event(str(event.get("method") or ""), params if isinstance(params, dict) else {})
        self.state = str(state or "stopped")
        if self.state not in ACTIVE_STATES:
            self._files.clear()

    def detach(self) -> None:
        """Stop following: nothing is held any more. Side effect: clears the state."""
        self.session_id = ""
        self.state = "stopped"
        self._files.clear()
        self._calls.clear()

    def relative(self, path: object) -> str | None:
        """Make a path of a session event relative to the workspace.

        Args:
            path: An absolute path, or one relative to the workspace.

        Returns:
            The relative path with "/" separators and without `.` and `..` segments, or
            None if the value is no path inside the workspace.
        """
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        normal = os.path.normpath(os.path.join(self._workspace, path))
        try:
            relative = PurePosixPath(normal).relative_to(self._workspace.as_posix())
        except ValueError:
            return None
        return None if str(relative) == "." else str(relative)

    def event(self, method: str, params: Mapping[str, Any]) -> bool:
        """Take one event of the attached session. Side effect: updates the held files.

        Args:
            method: The event's method, e.g. "edit.diff".
            params: Its params.

        Returns:
            True if the files should be looked at now (a tool call or the turn ended).
        """
        if method == "session.state":
            self.state = str(params.get("state") or "")
            if self.state not in ACTIVE_STATES:
                self._files.clear()
                self._calls.clear()
                return True
            return False
        if method == "turn.result":
            self._files.clear()
            return True
        call_id = str(params.get("tool_use_id") or "")
        if method == "edit.diff":
            self._touch(call_id, [params.get("path")])
        elif method == "tool.use":
            name = str(params.get("name") or "")
            call = self._calls.setdefault(call_id, (name, set()))
            self._calls[call_id] = (name, call[1])
            tool_input = params.get("input")
            if name in EDIT_TOOLS and isinstance(tool_input, dict):
                self._touch(call_id, [tool_input.get("file_path"), tool_input.get("notebook_path")])
        elif method == "tool.files":
            files = params.get("files")
            if isinstance(files, list):
                self._touch(call_id, [f.get("path") for f in files if isinstance(f, dict)])
        elif method == "tool.result":
            name, paths = self._calls.pop(call_id, ("", set()))
            self._hints.paths |= paths
            self._hints.command = self._hints.command or name in COMMAND_TOOLS
            return True
        return False

    def _touch(self, call_id: str, paths: Iterable[object]) -> None:
        """Add files to the turn's files and to those of one tool call.

        Args:
            call_id: The tool call's id; "" if the event names none.
            paths: The files as the event names them; values that are no path are skipped.
        """
        call = self._calls.setdefault(call_id, ("", set())) if call_id else None
        for path in paths:
            relative = self.relative(path)
            if relative is not None:
                self._files.add(relative)
                self._hints.paths.add(relative)
                if call is not None:
                    call[1].add(relative)

    def held(self, path: str) -> bool:
        """Tell whether the agent edits a file in the running turn.

        Args:
            path: The relative path.

        Returns:
            True while the session's state is one of `ACTIVE_STATES` and an edit of the
            running turn named the file.
        """
        return self.state in ACTIVE_STATES and path in self._files

    def take_hints(self) -> Hints:
        """Hand the hints to a scan. Side effect: starts a fresh set.

        Returns:
            The files edit tool calls named since the last call, and whether a command ran
            since or runs now.
        """
        hints, self._hints = self._hints, Hints()
        hints.command = hints.command or any(
            name in COMMAND_TOOLS for name, _paths in self._calls.values()
        )
        return hints


def changed_paths(
    known: Mapping[str, Signature], current: Mapping[str, Signature]
) -> tuple[list[str], list[str]]:
    """Compare two scans of the listed files.

    Args:
        known: Path -> signature at the scan before.
        current: Path -> signature now.

    Returns:
        (the paths that are new or have another signature, the paths that are no longer
        listed), each sorted.
    """
    changed = sorted(path for path, signature in current.items() if known.get(path) != signature)
    gone = sorted(path for path in known if path not in current)
    return changed, gone


def poll_interval(scan_s: float, lowest: float, highest: float = 30.0) -> float:
    """Choose the wait before the next scan, so that scanning takes about 5 % of the time.

    Args:
        scan_s: How long the last scan took.
        lowest: The shortest wait (the configured interval).
        highest: The longest wait.

    Returns:
        `20 * scan_s`, within `[lowest, max(lowest, highest)]`.
    """
    return min(max(lowest, 20 * scan_s), max(lowest, highest))


def edited_paths(paths: Sequence[str]) -> list[str]:
    """Order the paths of one report to the session.

    Args:
        paths: The files a request writes, deletes or renames.

    Returns:
        The paths without duplicates, in their first order.
    """
    return list(dict.fromkeys(paths))
