"""File synchronisation of `calu ide`: every access to the project's files.

    Tree(root)                      holds one descriptor of the project directory
      │
      ├─ a path of a request ──► one openat per directory, O_NOFOLLOW each ──► (parent, name)
      │                           a symbolic link on the way is an error, never followed
      ├─ state / read           the file is opened with O_NOFOLLOW and checked with fstat
      ├─ temp ─► put ─► commit  .calu-upload-<id> beside the target, then one rename
      ├─ delete, rename         compare first, then unlink or link + unlink
      └─ listing, describe      git ls-files (or a walk), then lstat and streamed hashes

Every read and write of a file names it relative to an open directory descriptor, so that a
directory which is replaced by a link while a request runs cannot lead it out of the project.
The listing (the walk, git and its existence checks) works on paths: a directory swapped for
a link during a listing can add names from outside to it, never their contents, which are
read through the descriptors (`describe`). Nothing here needs more rights than the user who
started the bridge has. All methods block: call them from a worker thread. The rules that need
no file access are in `calu.ide.files`, the contract in
`agent/records/notes/jetbrains-sync-spec.md`.
"""

import contextlib
import errno
import hashlib
import logging
import os
import stat
import uuid
from collections.abc import Collection, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from calu.acp.peer import RpcError
from calu.honesty import GitError, run_git
from calu.ide.files import (
    BASE_MISMATCH,
    IO_ERROR,
    MAX_FILE_BYTES,
    MAX_LISTED,
    NO_MANIFEST,
    NOT_FOUND,
    NOT_REGULAR,
    PATH_REFUSED,
    TARGET_EXISTS,
    TOO_LARGE,
    TRACE_DIRECTORY,
    UPLOAD_PREFIX,
    WALK_IGNORED,
    HashCache,
    Listing,
    Signature,
    has_plain_name,
    is_excluded,
    listed_paths,
    private_prefixes,
    status_paths,
    under,
)

log = logging.getLogger(__name__)

BLOCK = 1024 * 1024
"""Bytes read at once while a file is hashed."""

DEFAULT_MODE = 0o644
"""The permission bits of a new file whose request names none."""

MAX_OPEN_DIRECTORIES = 128
"""Directory descriptors one listing or scan keeps open at a time."""

LS_FILES = ("--literal-pathspecs", "ls-files", "-co", "--exclude-standard", "-z", "--")
"""The git call that lists tracked and untracked files without the ignored ones; the paths
after it are literal file names."""

STATUS = ("status", "--porcelain=v2", "-z", "--untracked-files=all", "--no-renames", "--", ".")
"""The git call that lists what differs from HEAD in and below the project directory."""

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def fail_io(error: OSError, what: str) -> NoReturn:
    """Report a refusal of the operating system: always raises.

    Args:
        error: The refusal.
        what: What was tried, e.g. "writing src/a.py".

    Raises:
        RpcError: `IO_ERROR` with `data.errno` and `data.strerror`.
    """
    text = error.strerror or type(error).__name__
    raise RpcError(
        IO_ERROR, f"{what} failed: {text}", {"errno": error.errno, "strerror": text}
    ) from error


def kind_of(mode: int) -> str:
    """Name what a file mode says the file is.

    Args:
        mode: `st_mode`.

    Returns:
        "file", "directory", "symlink", "fifo", "socket" or "device".
    """
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "device"


def signature(status: os.stat_result) -> Signature:
    """Take the signature of a file from its status.

    Args:
        status: The result of a stat call.

    Returns:
        (size, mtime in ns, inode).
    """
    return (status.st_size, status.st_mtime_ns, status.st_ino)


@dataclass(frozen=True)
class State:
    """What is at a path at one moment."""

    kind: str
    """ "missing", or a value of `kind_of`."""
    sha256: str | None = None
    """The content's hash; None unless a regular file within the size limit."""
    size: int | None = None
    mode: int | None = None
    """The permission bits; None for a missing path."""
    signature: Signature | None = None

    def data(self) -> dict[str, Any]:
        """Render the state as the `data` of a base mismatch.

        Returns:
            `{sha256, size, mode}`, each null for a missing file.
        """
        return {"sha256": self.sha256, "size": self.size, "mode": self.mode}


MISSING = State("missing")
"""The state of a path at which nothing is."""


def check_base(path: str, found: State, base: str | None, limit: int) -> None:
    """Compare what is at a path with the base a request names.

    Args:
        path: The relative path, for the messages.
        found: What is there.
        base: The hash the request expects; None expects that nothing is there.
        limit: The per-file limit in bytes.

    Raises:
        RpcError: `NOT_REGULAR` (`data.kind`) if something other than a regular file is
            there; `TOO_LARGE` if the file is above the limit (its hash is not computed);
            `BASE_MISMATCH` (`data.sha256`, `data.size`, `data.mode` of what is there) if
            the file's hash is not `base`, or the file exists and `base` is None, or the
            file is missing and `base` is a hash.
    """
    if found.kind not in ("missing", "file"):
        raise RpcError(
            NOT_REGULAR, f"{path} is a {found.kind}, not a regular file", {"kind": found.kind}
        )
    if found.kind == "file" and found.sha256 is None:
        raise RpcError(
            TOO_LARGE,
            f"{path} is larger than the limit of {limit} bytes per file",
            {"size": found.size, "limit": limit},
        )
    if found.sha256 != base:
        there = "does not exist" if found.kind == "missing" else "has another content"
        expected = "no file was expected" if base is None else "the request's base differs"
        raise RpcError(BASE_MISMATCH, f"{path} {there}: {expected}", found.data())


@dataclass
class Place:
    """A path resolved to its parent directory: descriptors from the root down, and the name."""

    path: str
    name: str
    """The last segment."""
    chain: list[tuple[int, str]]
    """(descriptor of a directory, name of the next directory in it), from the root down;
    empty for a file directly in the root."""
    parent: int
    """Descriptor of the directory that holds `name`; -1 if a directory on the way does
    not exist."""
    created: int = 0
    """How many of the last directories of `chain` this resolution created."""
    owned: list[int] = field(default_factory=list)
    """The descriptors to close (the root's is not among them)."""

    def close(self) -> None:
        """Close the descriptors this place opened. Side effect: the place is unusable."""
        for descriptor in self.owned:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self.owned = []

    def remove_created(self) -> None:
        """Remove the directories this resolution created, if they are empty.

        Side effect: `rmdir` from the deepest one up; stops at the first that is not empty.
        """
        self.prune(self.created)
        self.created = 0

    def prune(self, levels: int | None = None) -> None:
        """Remove empty directories on the way to the path, from the deepest one up.

        Side effect: `rmdir`; stops at the first directory that is not empty or cannot be
        removed. The root is never removed.

        Args:
            levels: How many directories at most; None for all of the path's directories.
        """
        count = len(self.chain) if levels is None else min(levels, len(self.chain))
        for descriptor, name in reversed(self.chain[len(self.chain) - count :]):
            try:
                os.rmdir(name, dir_fd=descriptor)
            except OSError:
                return


@dataclass
class Temp:
    """The temporary file of one write, beside its target."""

    place: Place
    name: str
    """`.calu-upload-<id>`, in the target's directory."""
    descriptor: int
    """Open for writing; -1 once closed."""
    hasher: Any = field(default_factory=hashlib.sha256)
    """The SHA-256 of what was written so far."""
    size: int = 0

    def put(self, data: bytes) -> None:
        """Append content. Side effects: writes to the temporary file, updates the hash.

        Args:
            data: The bytes.

        Raises:
            RpcError: `IO_ERROR` if the write fails (disk full, quota).
        """
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(self.descriptor, view) :]
        except OSError as error:
            fail_io(error, f"writing {self.place.path}")
        self.hasher.update(data)
        self.size += len(data)

    def discard(self) -> None:
        """Give the write up. Never raises.

        Side effects: closes and removes the temporary file, removes the directories that
        were created for it if they are empty, closes the place.
        """
        if self.descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1
        if self.place.parent >= 0:
            with contextlib.suppress(OSError):
                os.unlink(self.name, dir_fd=self.place.parent)
        self.place.remove_created()
        self.place.close()


class Tree:
    """The project directory as the file requests see it (see the module docstring)."""

    def __init__(
        self,
        root: Path,
        max_file_bytes: int = MAX_FILE_BYTES,
        cache: HashCache | None = None,
    ) -> None:
        """Open the project directory. Side effect: holds one directory descriptor.

        Args:
            root: The resolved project directory.
            max_file_bytes: The per-file limit.
            cache: The hash cache; None creates one.

        Raises:
            OSError: If the directory cannot be opened.
        """
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.cache = cache or HashCache()
        self.fixed_excluded: tuple[str, ...] = (TRACE_DIRECTORY, *private_prefixes(root))
        """Excluded whatever a session says: the default trace directory and calu's own
        directories inside the project."""
        self.session_excluded: tuple[str, ...] = ()
        """The attached session's own directories (its trace directory)."""
        self._root = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    @property
    def excluded(self) -> tuple[str, ...]:
        """The excluded directories besides `.git`.

        Returns:
            `fixed_excluded` and `session_excluded`.
        """
        return (*self.fixed_excluded, *self.session_excluded)

    def close(self) -> None:
        """Close the root's descriptor. Side effect: the tree is unusable afterwards."""
        with contextlib.suppress(OSError):
            os.close(self._root)

    # --------------------------------------------------------------------- resolving

    def place(self, path: str, create: bool = False) -> Place:
        """Resolve a checked relative path to its parent directory without following links.

        Side effects: opens one descriptor per directory (the caller closes the place);
        with `create`, makes the missing directories.

        Args:
            path: A path `calu.ide.files.clean_path` returned.
            create: Whether missing directories are created.

        Returns:
            The place; its `parent` is -1 if a directory does not exist and `create` is
            False.

        Raises:
            RpcError: `PATH_REFUSED` (`reason: "outside"`) if a directory on the way is a
                symbolic link that leads out of the root; `NOT_REGULAR` (`kind`) if it is a
                link that stays inside, or another non-directory; `IO_ERROR` for any other
                refusal of the operating system.
        """
        *directories, name = path.split("/")
        place = Place(path=path, name=name, chain=[], parent=self._root)
        try:
            for index, directory in enumerate(directories):
                made = False
                try:
                    descriptor = os.open(directory, _DIRECTORY, dir_fd=place.parent)
                except FileNotFoundError:
                    if not create:
                        place.parent = -1
                        return place
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(directory, dir_fd=place.parent)
                        made = True
                    descriptor = -1
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR):
                        self._not_a_directory(
                            "/".join(directories[: index + 1]), directory, place.parent
                        )
                    fail_io(error, f"opening the directory of {path}")
                place.chain.append((place.parent, directory))
                place.created += made
                if descriptor < 0:
                    descriptor = os.open(directory, _DIRECTORY, dir_fd=place.parent)
                place.owned.append(descriptor)
                place.parent = descriptor
        except RpcError:
            place.remove_created()
            place.close()
            raise
        except OSError as error:
            place.remove_created()
            place.close()
            fail_io(error, f"opening the directory of {path}")
        return place

    def _not_a_directory(self, path: str, name: str, parent: int) -> NoReturn:
        """Refuse a path because one of its segments is no directory: always raises.

        Args:
            path: The relative path up to and including the segment.
            name: The segment.
            parent: Descriptor of the directory that holds it.

        Raises:
            RpcError: `PATH_REFUSED` with `reason: "outside"` for a symbolic link whose
                target is not inside the root; else `NOT_REGULAR` with the segment's kind.
        """
        try:
            kind = kind_of(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
        except OSError:
            kind = "missing"
        if kind == "symlink":
            target = Path(os.path.realpath(self.root / path))
            if target != self.root and self.root not in target.parents:
                raise RpcError(
                    PATH_REFUSED,
                    f"{path} is a symbolic link that leads out of the project directory",
                    {"reason": "outside"},
                )
        raise RpcError(
            NOT_REGULAR,
            f"{path} is a {kind}, not a directory; links are not followed",
            {"kind": kind},
        )

    @contextlib.contextmanager
    def _placed(self, path: str, create: bool = False) -> Generator[Place]:
        """Resolve a path for the duration of a block and close the place afterwards.

        Args:
            path: A checked relative path.
            create: Whether missing directories are created.

        Yields:
            The place.

        Raises:
            RpcError: As `place`.
        """
        place = self.place(path, create)
        try:
            yield place
        finally:
            place.close()

    # ----------------------------------------------------------------------- reading

    def _open(self, place: Place) -> tuple[int, os.stat_result] | State:
        """Open the regular file at a place for reading.

        Args:
            place: The resolved path.

        Returns:
            (descriptor, status) of a regular file, which the caller closes; else the
            state of what is there (missing, or its kind).

        Raises:
            OSError: If the operating system refuses for another reason.
        """
        if place.parent < 0:
            return MISSING
        try:
            before = os.stat(place.name, dir_fd=place.parent, follow_symlinks=False)
        except FileNotFoundError, NotADirectoryError:
            return MISSING
        if not stat.S_ISREG(before.st_mode):
            return State(kind_of(before.st_mode), mode=stat.S_IMODE(before.st_mode))
        try:
            descriptor = os.open(place.name, _READ, dir_fd=place.parent)
        except FileNotFoundError:
            return MISSING
        except OSError as error:
            if error.errno == errno.ELOOP:
                return State("symlink")
            raise
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            os.close(descriptor)
            return State(kind_of(status.st_mode), mode=stat.S_IMODE(status.st_mode))
        return descriptor, status

    def _hash(self, path: str, descriptor: int, status: os.stat_result) -> str | None:
        """Hash an open regular file, through the cache.

        Side effects: reads the file from its start in blocks of `BLOCK` bytes unless the
        cache knows its signature; stores the hash if the file did not change meanwhile.

        Args:
            path: The relative path (the cache's key).
            descriptor: The open file.
            status: Its status at the open.

        Returns:
            The hash; None if the file is above the per-file limit (also if it grew above
            it while it was read).

        Raises:
            OSError: If reading fails.
        """
        if status.st_size > self.max_file_bytes:
            return None
        known = self.cache.get(path, signature(status))
        if known is not None:
            return known
        hasher = hashlib.sha256()
        offset = 0
        while True:
            block = os.pread(descriptor, BLOCK, offset)
            if not block:
                break
            offset += len(block)
            if offset > self.max_file_bytes:
                return None
            hasher.update(block)
        digest = hasher.hexdigest()
        if signature(os.fstat(descriptor)) == signature(status) and offset == status.st_size:
            self.cache.put(path, signature(status), digest)
        return digest

    def _state(self, place: Place) -> State:
        """Describe what is at a resolved path, with the hash of a regular file.

        Args:
            place: The resolved path.

        Returns:
            The state; `sha256` is None for a file above the per-file limit.

        Raises:
            RpcError: `IO_ERROR` if the file cannot be opened or read.
        """
        try:
            opened = self._open(place)
            if isinstance(opened, State):
                return opened
            descriptor, status = opened
            try:
                digest = self._hash(place.path, descriptor, status)
            finally:
                os.close(descriptor)
        except OSError as error:
            fail_io(error, f"reading {place.path}")
        return State(
            "file", digest, status.st_size, stat.S_IMODE(status.st_mode), signature(status)
        )

    def state(self, path: str) -> State:
        """Describe what is at a path.

        Args:
            path: A checked relative path.

        Returns:
            The state (`MISSING` also when a directory on the way does not exist).

        Raises:
            RpcError: As `place`; `IO_ERROR` if the file cannot be read.
        """
        with self._placed(path) as place:
            return self._state(place)

    def kind(self, path: str) -> str:
        """Name what is at a path without reading it.

        Args:
            path: A checked relative path.

        Returns:
            "missing" (also when a directory on the way does not exist or is a link), or a
            value of `kind_of`.
        """
        try:
            with self._placed(path) as place:
                if place.parent < 0:
                    return "missing"
                status = os.stat(place.name, dir_fd=place.parent, follow_symlinks=False)
        except OSError, RpcError:
            return "missing"
        return kind_of(status.st_mode)

    def read(self, path: str, offset: int, length: int) -> dict[str, Any]:
        """Read a piece of a regular file together with the hash of the whole file.

        Args:
            path: A checked relative path.
            offset: The first byte.
            length: How many bytes at most (the caller bounds it).

        Returns:
            `{content, sha256, size, mode, eof}`; `content` is bytes. The hash, the size
            and the piece come from the same open file; if the file changed while it was
            read, it is read again (three attempts).

        Raises:
            RpcError: `NOT_FOUND` if nothing is there; `NOT_REGULAR` (`kind`) for anything
                but a regular file; `TOO_LARGE` above the per-file limit; `IO_ERROR`; the
                errors of `place`.
        """
        with self._placed(path) as place:
            try:
                for _attempt in range(2):
                    answer, steady = self._read_once(place, offset, length)
                    if steady:
                        return answer
                return self._read_once(place, offset, length)[0]
            except OSError as error:
                fail_io(error, f"reading {path}")

    def _read_once(self, place: Place, offset: int, length: int) -> tuple[dict[str, Any], bool]:
        """Read a piece and the hash of a file through one descriptor.

        Args:
            place: The resolved path.
            offset: The first byte.
            length: How many bytes at most.

        Returns:
            (the result of `read`, whether the file kept its signature while it was read).

        Raises:
            RpcError: `NOT_FOUND`, `NOT_REGULAR` or `TOO_LARGE`, as `read`.
            OSError: If the operating system refuses.
        """
        path = place.path
        opened = self._open(place)
        if isinstance(opened, State):
            if opened.kind == "missing":
                raise RpcError(NOT_FOUND, f"{path} does not exist")
            raise RpcError(
                NOT_REGULAR,
                f"{path} is a {opened.kind}, not a regular file",
                {"kind": opened.kind},
            )
        descriptor, status = opened
        try:
            digest = self._hash(path, descriptor, status)
            if digest is None:
                raise RpcError(
                    TOO_LARGE,
                    f"{path} is larger than the limit of {self.max_file_bytes} bytes per file",
                    {"size": status.st_size, "limit": self.max_file_bytes},
                )
            content = os.pread(descriptor, length, offset)
            steady = signature(os.fstat(descriptor)) == signature(status)
        finally:
            os.close(descriptor)
        answer = {
            "content": content,
            "sha256": digest,
            "size": status.st_size,
            "mode": stat.S_IMODE(status.st_mode),
            "eof": offset + len(content) >= status.st_size,
        }
        return answer, steady

    # ----------------------------------------------------------------------- writing

    def temp(self, path: str) -> Temp:
        """Create the temporary file of a write beside its target.

        Side effects: creates the target's missing directories and an empty file
        `.calu-upload-<id>` with the mode 0o600 in the target's directory.

        Args:
            path: A checked relative path: the target.

        Returns:
            The temporary file; the caller ends it with `commit` or `Temp.discard`.

        Raises:
            RpcError: As `place`; `IO_ERROR` if the file cannot be created.
        """
        place = self.place(path, create=True)
        name = UPLOAD_PREFIX + uuid.uuid4().hex
        try:
            descriptor = os.open(name, _CREATE, 0o600, dir_fd=place.parent)
        except OSError as error:
            place.remove_created()
            place.close()
            fail_io(error, f"writing {path}")
        return Temp(place=place, name=name, descriptor=descriptor)

    def commit(self, temp: Temp, base: str | None, mode: int | None) -> State:
        """Put a finished temporary file in its target's place if the target is at the base.

        Side effects: on success one `rename` replaces the target (a reader sees the old or
        the new content) and the temporary file's place is closed. On any failure the
        temporary file is discarded and the target is untouched, unless the failure comes
        after the rename (reading the new file's state): then the new content is in place
        although the request reports `IO_ERROR`.

        Args:
            temp: The temporary file with the whole content.
            base: The hash the target must have; None if it must not exist.
            mode: The new permission bits; None keeps an existing file's bits and gives a
                new file `DEFAULT_MODE`.

        Returns:
            The target's new state.

        Raises:
            RpcError: As `check_base`; `IO_ERROR` if the operating system refuses.
        """
        place = temp.place
        try:
            found = self._state(place)
            check_base(place.path, found, base, self.max_file_bytes)
            bits = mode if mode is not None else found.mode
            try:
                os.fchmod(temp.descriptor, DEFAULT_MODE if bits is None else bits & 0o777)
                os.close(temp.descriptor)
                temp.descriptor = -1
                os.replace(temp.name, place.name, src_dir_fd=place.parent, dst_dir_fd=place.parent)
                status = os.stat(place.name, dir_fd=place.parent, follow_symlinks=False)
            except OSError as error:
                fail_io(error, f"writing {place.path}")
        except BaseException:
            temp.discard()
            raise
        place.close()
        digest = temp.hasher.hexdigest()
        self.cache.put(place.path, signature(status), digest)
        return State(
            "file", digest, status.st_size, stat.S_IMODE(status.st_mode), signature(status)
        )

    def write(self, path: str, content: bytes, base: str | None, mode: int | None) -> State:
        """Create or replace a file with a content if the file is at the base.

        Side effects: as `temp`, `Temp.put` and `commit`. The target is never opened for
        writing and never truncated.

        Args:
            path: A checked relative path.
            content: The whole new content.
            base: The hash the file must have; None if it must not exist.
            mode: The new permission bits, or None (`commit`).

        Returns:
            The file's new state.

        Raises:
            RpcError: As `place`, `check_base` and `commit`.
        """
        with self._placed(path) as place:
            check_base(path, self._state(place), base, self.max_file_bytes)
        temp = self.temp(path)
        try:
            temp.put(content)
        except BaseException:
            temp.discard()
            raise
        return self.commit(temp, base, mode)

    def delete(self, path: str, base: str) -> None:
        """Delete a file if it is at the base, and its directories that became empty.

        Side effects: `unlink`, then `rmdir` upwards while directories are empty (never the
        root).

        Args:
            path: A checked relative path.
            base: The hash the file must have.

        Raises:
            RpcError: As `place` and `check_base` (a missing file is a base mismatch with
                `sha256: null`); `IO_ERROR`.
        """
        with self._placed(path) as place:
            check_base(path, self._state(place), base, self.max_file_bytes)
            try:
                os.unlink(place.name, dir_fd=place.parent)
            except OSError as error:
                fail_io(error, f"deleting {path}")
            self.cache.forget(path)
            place.prune()

    def rename(self, source: str, target: str, base: str) -> State:
        """Move a file to a path at which nothing is, if the file is at the base.

        Side effects: creates the target's missing directories; `link` then `unlink` (so an
        existing target is never replaced; where the file system has no hard links, a
        check and a `rename`); removes the source's directories that became empty. If the
        source cannot be removed after the link, the file is at both paths and the request
        reports `IO_ERROR`.

        Args:
            source: A checked relative path: the file.
            target: A checked relative path: its new place.
            base: The hash the file must have.

        Returns:
            The state of the file at its new place.

        Raises:
            RpcError: `NOT_FOUND` if the source does not exist; `TARGET_EXISTS`
                (`data.sha256`, null unless a regular file) if something is at the target;
                as `place` and `check_base`; `IO_ERROR`.
        """
        with self._placed(source) as old:
            found = self._state(old)
            if found.kind == "missing":
                raise RpcError(NOT_FOUND, f"{source} does not exist")
            check_base(source, found, base, self.max_file_bytes)
            with self._placed(target) as probe:
                there = self._state(probe)
            if there.kind != "missing":
                raise RpcError(TARGET_EXISTS, f"{target} exists", {"sha256": there.sha256})
            new = self.place(target, create=True)
            try:
                try:
                    os.link(
                        old.name,
                        new.name,
                        src_dir_fd=old.parent,
                        dst_dir_fd=new.parent,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise RpcError(TARGET_EXISTS, f"{target} exists", {"sha256": None}) from None
                except OSError:
                    # No hard links here (some network and Windows-backed file systems).
                    try:
                        os.stat(new.name, dir_fd=new.parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RpcError(
                            TARGET_EXISTS, f"{target} exists", {"sha256": None}
                        ) from None
                    os.rename(old.name, new.name, src_dir_fd=old.parent, dst_dir_fd=new.parent)
                else:
                    os.unlink(old.name, dir_fd=old.parent)
                status = os.stat(new.name, dir_fd=new.parent, follow_symlinks=False)
            except RpcError:
                new.remove_created()
                raise
            except OSError as error:
                new.remove_created()
                fail_io(error, f"renaming {source}")
            finally:
                new.close()
            self.cache.forget(source)
            old.prune()
        if found.sha256 is not None:
            self.cache.put(target, signature(status), found.sha256)
        return State(
            "file", found.sha256, status.st_size, stat.S_IMODE(status.st_mode), signature(status)
        )

    # ----------------------------------------------------------------------- listing

    def _git(self, arguments: Sequence[str], accept: tuple[int, ...] = (0,)) -> bytes:
        """Run git in the project directory (`calu.honesty.run_git`).

        Args:
            arguments: The arguments after git's own options.
            accept: The exit codes that are no error.

        Returns:
            The standard output.

        Raises:
            GitError: If git is missing, too slow or exits with another code.
        """
        return run_git(self.root, arguments, accept=accept)

    def git_info(self) -> tuple[bool, str | None, str | None]:
        """Ask git about the project directory. Side effect: up to three git calls.

        Returns:
            (whether the directory is inside a git work tree, the full id of the HEAD
            commit or None, the checked-out branch or None). Without git, or where git
            refuses the directory, the answer is `(False, None, None)`.
        """
        try:
            inside = self._git(["rev-parse", "--is-inside-work-tree"], (0, 128)).strip()
            if inside != b"true":
                return False, None, None
            head = self._git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], (0, 1, 128))
            branch = self._git(["symbolic-ref", "--short", "-q", "HEAD"], (0, 1, 128))
        except GitError:
            return False, None, None
        return True, head.decode().strip() or None, os.fsdecode(branch).strip() or None

    def _walk(self) -> list[str]:
        """List the files below the root without git and without following links.

        Returns:
            The relative paths of everything that is not a directory, sorted. Directories
            named in `WALK_IGNORED` and excluded directories are not entered.

        Raises:
            RpcError: `NO_MANIFEST` (`reason: "too-many-files"`) above `MAX_LISTED` paths,
                (`reason: "unreadable"`) if the root cannot be read.
        """
        found: list[str] = []
        pending = [""]
        excluded = self.excluded
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(self.root / directory) as entries:
                    for entry in entries:
                        path = f"{directory}/{entry.name}" if directory else entry.name
                        if is_excluded(path, excluded):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in WALK_IGNORED:
                                pending.append(path)
                        else:
                            found.append(path)
            except OSError as error:
                if not directory:
                    raise RpcError(
                        NO_MANIFEST,
                        f"the project directory cannot be listed: {error.strerror}",
                        {"reason": "unreadable"},
                    ) from error
                log.info("the directory %s is not listed: %s", directory, error.strerror)
            if len(found) > MAX_LISTED:
                raise RpcError(
                    NO_MANIFEST,
                    f"the project directory holds more than {MAX_LISTED} files",
                    {"reason": "too-many-files"},
                )
        return sorted(found)

    def listing(self, scope: str = "all", selected: Collection[str] = ()) -> Listing:
        """List the paths a manifest covers.

        Side effects: git calls (`ls-files -co --exclude-standard`, or `status
        --porcelain=v2` for the dirty scope) in a git work tree, else a walk.

        Args:
            scope: "all", or "dirty" for the files that differ from HEAD or are untracked.
            selected: Checked relative paths (files or directories) the listing is limited
                to; empty for the whole tree.

        Returns:
            The listing: the paths that exist, without the excluded ones; in `missing` the
            selected paths at which nothing is and, in the dirty scope, the tracked files
            that are gone.

        Raises:
            RpcError: `NO_MANIFEST` with `data.reason`: "git" if a git call failed,
                "not-git" for the dirty scope outside a git work tree, "too-many-files",
                "unreadable".
        """
        git, head, _branch = self.git_info()
        if scope == "dirty" and not git:
            raise RpcError(
                NO_MANIFEST,
                "the dirty scope needs a git repository; ask for the scope 'all'",
                {"reason": "not-git"},
            )
        try:
            if not git:
                paths = [path for path in self._walk() if not selected or under(path, selected)]
            elif scope == "dirty":
                prefix = os.fsdecode(self._git(["rev-parse", "--show-prefix"])).strip()
                output = self._git(STATUS)
                paths = [
                    path
                    for path in status_paths(output, prefix)
                    if not selected or under(path, selected)
                ]
            else:
                output = self._git([*LS_FILES, *sorted(selected)])
                paths = listed_paths(output)
        except GitError as error:
            raise RpcError(
                NO_MANIFEST, f"the files could not be listed: {error}", {"reason": "git"}
            ) from error
        if len(paths) > MAX_LISTED:
            raise RpcError(
                NO_MANIFEST,
                f"the project directory holds more than {MAX_LISTED} files",
                {"reason": "too-many-files"},
            )
        excluded = self.excluded
        paths = [path for path in paths if not is_excluded(path, excluded)]
        there: list[str] = []
        missing = [path for path in selected if not os.path.lexists(self.root / path)]
        if scope == "dirty":
            for path in paths:
                (there if os.path.lexists(self.root / path) else missing).append(path)
            paths = there
        return Listing(head=head, git=git, paths=paths, missing=sorted(set(missing)))

    def describe(
        self, paths: Sequence[str], git: bool = True, hashes: bool = True
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Signature]]:
        """Describe listed paths: the regular files, and why the others are skipped.

        Side effects: one `openat` per directory (descriptors are shared between the paths
        of one call), an `lstat` per path and, with `hashes`, a streamed read of every file
        the hash cache does not know.

        Args:
            paths: Relative paths from a listing.
            git: Whether git listed them (a directory is then a submodule or a nested
                repository; else it is not reported).
            hashes: Whether the files are hashed.

        Returns:
            (`{path, size, mode, sha256}` per regular file within the size limit, with
            `sha256` None if `hashes` is off; `{path, reason}` per skipped path, with the
            reason "symlink", "too-large", "not-regular", "submodule" or
            "unsupported-name"; path -> signature of each file). A path at which nothing
            is, or which cannot be read, is in none of them. A path whose directory
            cannot be opened is skipped with the reason "symlink", whatever the cause is:
            a link on the way, another non-directory or a read error such as a denied
            permission.
        """
        files: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        signatures: dict[str, Signature] = {}
        directories: dict[str, Place | None] = {}
        try:
            for path in paths:
                if not has_plain_name(path):
                    shown = os.fsencode(path).decode("utf-8", "replace")
                    skipped.append({"path": shown, "reason": "unsupported-name"})
                    continue
                directory, _, name = path.rpartition("/")
                if directory not in directories:
                    if len(directories) >= MAX_OPEN_DIRECTORIES:
                        self._close_all(directories)
                    try:
                        directories[directory] = self.place(path)
                    except RpcError:
                        directories[directory] = None
                place = directories[directory]
                if place is None:
                    skipped.append({"path": path, "reason": "symlink"})
                    continue
                here = Place(path=path, name=name, chain=[], parent=place.parent)
                try:
                    entry = self._entry(here, hashes)
                except OSError, RpcError:
                    continue
                if isinstance(entry, str):
                    if entry and (git or entry != "submodule"):
                        skipped.append({"path": path, "reason": entry})
                    continue
                files.append(entry[0])
                signatures[path] = entry[1]
        finally:
            self._close_all(directories)
        return files, skipped, signatures

    @staticmethod
    def _close_all(directories: dict[str, Place | None]) -> None:
        """Close the places a `describe` call holds. Side effect: empties the mapping.

        Args:
            directories: Directory -> its place, or None for one that could not be opened.
        """
        for place in directories.values():
            if place is not None:
                place.close()
        directories.clear()

    def _entry(self, place: Place, hashes: bool) -> tuple[dict[str, Any], Signature] | str:
        """Describe one listed path.

        Args:
            place: The resolved path.
            hashes: Whether a regular file is hashed.

        Returns:
            (the manifest entry, the signature) of a regular file within the size limit;
            else the reason it is skipped, "" for a path at which nothing is.

        Raises:
            OSError: If the file cannot be read.
        """
        if place.parent < 0:
            return ""
        try:
            status = os.stat(place.name, dir_fd=place.parent, follow_symlinks=False)
        except FileNotFoundError, NotADirectoryError:
            return ""
        kind = kind_of(status.st_mode)
        if kind != "file":
            return {"symlink": "symlink", "directory": "submodule"}.get(kind, "not-regular")
        if status.st_size > self.max_file_bytes:
            return "too-large"
        digest: str | None = None
        if hashes:
            digest = self.cache.get(place.path, signature(status))
            if digest is None:
                opened = self._open(place)
                if isinstance(opened, State):
                    return "" if opened.kind == "missing" else "not-regular"
                descriptor, status = opened
                try:
                    digest = self._hash(place.path, descriptor, status)
                finally:
                    os.close(descriptor)
                if digest is None:
                    return "too-large"
        entry = {
            "path": place.path,
            "size": status.st_size,
            "mode": stat.S_IMODE(status.st_mode),
            "sha256": digest,
        }
        return entry, signature(status)

    def case_sensitive(self) -> bool:
        """Tell whether the root's file system distinguishes letter case.

        Side effect: lists the root and stats one name in the other letter case; nothing
        is created.

        Returns:
            False if a name of the root is also found in swapped case as the same file;
            True otherwise, and when the root holds no name with a letter.
        """
        try:
            names = set(os.listdir(self._root))
        except OSError:
            return True
        for name in sorted(names):
            swapped = name.swapcase()
            if swapped == name or swapped in names:
                continue
            try:
                one = os.stat(name, dir_fd=self._root, follow_symlinks=False)
                other = os.stat(swapped, dir_fd=self._root, follow_symlinks=False)
            except OSError:
                return True
            return (one.st_dev, one.st_ino) != (other.st_dev, other.st_ino)
        return True
