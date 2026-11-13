"""What the full-screen client changes in the database, with the `admin` credential.

A session may read the checkers' instructions and its settings and cannot change them: it
must not be able to change what it is judged by. Two things of the client therefore do not go
through the session at all:

* saving a checker's instructions (the checkers screen), as `calu checker edit` does;
* setting `honesty.mode` (`/honesty off | track | enforce`), as `calu config set` does.

Both use the `admin` credential of the local credential store (`calu.credentials`), which is
the connection string of the database owner, and the functions of `calu.admin`. The client
asks for the credential's password when it has one. Where the machine has no such credential,
`AdminAccess.problem` says so and the client shows the command that does the same.

`AdminAccess` is the interface the client uses; `StoreAdmin` is the real one, and tests pass
their own. This module imports nothing from Textual.

The database of this machine's credential need not be the one the attached session reads
(another host, a second local database). Nothing here can tell; the client therefore compares
what the session reports after a change with what was stored, and says so when they differ.
A save names the version the editor was opened on (`base_version`): if the stored definition
has another version by then, somebody changed it meanwhile and the save is refused.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import anyio
import psycopg

from calu import admin
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db.connection import Db
from calu.db.connection import open_stored as connect

CREDENTIAL = "admin"
"""The credential both changes need: only the database owner may write these tables."""

NO_CREDENTIAL = (
    "this machine has no 'admin' credential (calu credential set admin stores one; it is the "
    "connection string of the database owner)"
)
"""What `StoreAdmin.problem` says without the credential."""

CONNECT_TIMEOUT_S = 10.0
"""How long opening the database connection may take before the change is given up."""

MODES = ("off", "track", "enforce")
"""The values of `honesty.mode`, from the weakest to the strictest."""


class AdminFailed(Exception):
    """A change was not stored; the message says why in words for the user."""


class PasswordNeeded(AdminFailed):
    """The credential is protected by a password and none, or a wrong one, was given."""


@dataclass(frozen=True)
class SavedChecker:
    """A checker definition after its instructions were stored."""

    name: str
    version: int


@dataclass(frozen=True)
class SavedMode:
    """`honesty.mode` after it was stored."""

    mode: str
    scope: str
    """"global", or `project <name>` when the project's own setting was changed."""


class AdminAccess(Protocol):
    """The database changes the client can make (see the module docstring)."""

    def problem(self) -> str | None:
        """Say why nothing can be changed from this machine.

        Returns:
            None if the credential is there, else the sentence to show.
        """
        ...

    def needs_password(self) -> bool:
        """Tell whether the client has to ask for the credential's password.

        Returns:
            True for a credential stored with a password; False without one or without the
            credential.
        """
        ...

    async def save_instructions(
        self, name: str, text: str, password: str | None, base_version: int | None = None
    ) -> SavedChecker:
        """Store new instructions for one checker.

        Args:
            name: The checker's name.
            text: The Markdown instructions; must not be empty.
            password: The credential's password, or None if it has none.
            base_version: The version the edited text was read at; None stores whatever
                is there now.

        Returns:
            The checker with its new version.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the checker is unknown, the text is
                empty, the stored definition no longer has `base_version`, or the database
                refuses.
        """
        ...

    async def set_honesty_mode(
        self, mode: str, project: str | None, password: str | None
    ) -> SavedMode:
        """Store `honesty.mode`.

        Args:
            mode: One of `MODES`.
            project: The session's project, or None if the client does not know it.
            password: The credential's password, or None if it has none.

        Returns:
            The stored mode and the scope it was stored in.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the mode is unknown or the database
                refuses.
        """
        ...


def weakens(before: str, after: str) -> bool:
    """Tell whether a change of `honesty.mode` makes calu enforce less.

    Args:
        before: The mode now.
        after: The mode asked for.

    Returns:
        True for enforce to track or off and for track to off. False for every other change,
        also when one of the two is not a known mode.
    """
    if before not in MODES or after not in MODES:
        return False
    return MODES.index(after) < MODES.index(before)


class StoreAdmin:
    """`AdminAccess` over the local credential store and `calu.admin`."""

    def __init__(self, store: CredentialStore | None = None) -> None:
        """Create the access.

        Args:
            store: The credential store; the default store of this user if None.
        """
        self.store = store or CredentialStore()

    def problem(self) -> str | None:
        """Say why nothing can be changed from this machine.

        Returns:
            None if the `admin` credential is stored, `NO_CREDENTIAL` if not, or the store's
            own message if its file cannot be read.
        """
        try:
            return None if CREDENTIAL in self.store else NO_CREDENTIAL
        except CredentialError as error:
            return f"the credential store cannot be read: {error}"

    def needs_password(self) -> bool:
        """Tell whether the `admin` credential is protected by a password.

        Returns:
            True if it is; False without a password, without the credential or when the
            store cannot be read.
        """
        try:
            return CREDENTIAL in self.store and self.store.needs_password(CREDENTIAL)
        except CredentialError:
            return False

    async def _with_db[T](self, password: str | None, work: Callable[[Db], Awaitable[T]]) -> T:
        """Open the owner's connection, run `work` with it and close it.

        The decrypted connection string is wiped as soon as the connection is open.

        Args:
            password: The credential's password, or None if it has none.
            work: `async (Db) -> T`.

        Returns:
            What `work` returned.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the database cannot be reached or it
                refuses the change.
        """
        missing = self.problem()
        if missing is not None:
            raise AdminFailed(missing)
        if self.store.needs_password(CREDENTIAL) and not password:
            raise PasswordNeeded("the 'admin' credential needs its password")
        try:
            # deriving the key takes a noticeable time: not on the event loop
            url = await anyio.to_thread.run_sync(self.store.load, CREDENTIAL, password)
        except WrongPassword as error:
            raise PasswordNeeded("wrong password for the 'admin' credential") from error
        except CredentialError as error:
            raise AdminFailed(str(error)) from error
        try:
            try:
                with anyio.fail_after(CONNECT_TIMEOUT_S):
                    conn = await connect(url)
            except TimeoutError as error:
                raise AdminFailed(
                    f"the database did not answer within {CONNECT_TIMEOUT_S:.0f} seconds"
                ) from error
            finally:
                wipe(url)
            try:
                return await work(Db(conn))
            finally:
                await conn.close()
        except admin.AdminError as error:
            raise AdminFailed(str(error)) from error
        except psycopg.Error as error:
            first = str(error).strip().splitlines()[0] if str(error).strip() else "no detail"
            raise AdminFailed(f"the database refused: {first}") from error

    async def save_instructions(
        self, name: str, text: str, password: str | None, base_version: int | None = None
    ) -> SavedChecker:
        """Store new instructions for one checker (`calu.admin.set_checker`).

        Args:
            name: The checker's name.
            text: The Markdown instructions; must not be empty.
            password: The credential's password, or None if it has none.
            base_version: The version the edited text was read at; None stores whatever
                is there now.

        Returns:
            The checker with the version the database gave the change.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the checker is unknown, the text is
                empty, the stored definition has another version than `base_version`
                ("changed meanwhile"), or the database refuses.
        """

        async def work(db: Db) -> SavedChecker:
            """Check that the checker exists, then store the text.

            Args:
                db: The owner's connection.

            Returns:
                The checker after the change.

            Raises:
                AdminFailed: If the stored definition has another version than the one
                    the edited text was read at.
            """
            await admin.instructions(db, name)  # an unknown name must not create a checker
            stored = next((row for row in await admin.checkers(db) if row.name == name), None)
            if base_version is not None and stored is not None and stored.version != base_version:
                raise AdminFailed(
                    f"changed meanwhile to version {stored.version}: press r, then edit again"
                )
            row = await admin.set_checker(db, name, instructions_text=text)
            return SavedChecker(row.name, row.version)

        return await self._with_db(password, work)

    async def set_honesty_mode(
        self, mode: str, project: str | None, password: str | None
    ) -> SavedMode:
        """Store `honesty.mode` where the session reads it from (`calu.admin.set_setting`).

        A project's own setting wins over the global one. The mode is therefore written to
        the project's settings if they already name `honesty.mode`, and to the global
        settings otherwise.

        Args:
            mode: One of `MODES`.
            project: The session's project, or None if the client does not know it.
            password: The credential's password, or None if it has none.

        Returns:
            The stored mode and the scope it was stored in.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the mode is unknown or the database
                refuses.
        """
        if mode not in MODES:
            raise AdminFailed(f"the mode is one of {', '.join(MODES)}")

        async def work(db: Db) -> SavedMode:
            """Find the scope that decides, then store the mode there.

            Args:
                db: The owner's connection.

            Returns:
                The stored mode and its scope.
            """
            scope: str | None = None
            if project:
                try:
                    stored = await admin.settings(db, project)
                except admin.AdminError:
                    stored = {}
                if "mode" in (stored.get("honesty") or {}):
                    scope = project
            await admin.set_setting(db, "honesty.mode", mode, scope)
            return SavedMode(mode, f"project {scope}" if scope else "global")

        return await self._with_db(password, work)
