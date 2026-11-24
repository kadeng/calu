"""The database login of `calu ide`, chosen without ever asking on a terminal.

One login serves every request, reading and changing alike: the user login (the role
`calu_admin`, `calu.logins`, agent/records/PLAN.md D265).

    held (`ide.unlock` opened it)           ─► that login
    root's place file with a password       ─► that login       (an empty user password,
                                                                  `sudo calu setup`)
    the `user` entry without a password     ─► that entry       (an empty user password)
    the `user` entry with a password        ─► opened with the request's `password`
    only root's place file                  ─► `calu_admin` there, logged in with the
                                               request's `password`
    nothing                                 ─► `NO_DATABASE` (the message names `calu setup`)

    unlock(password)  (`ide.unlock`: the user password, once per bridge process)
      the `user` entry opens with it                    ─► held
      root's place file names the database, and it
        accepts the password for `calu_admin`           ─► held

The standard input of `calu ide` is the editor's protocol stream, so a prompt would read a
request and take it for the password. A login that needs a password the request did not bring
is therefore the JSON-RPC error `NEEDS_PASSWORD` with `data: {"needs": "user"}`; the editor
asks its user and repeats the request with `password` (or calls `ide.unlock`). A wrong
password is the same error again, with a message that says so (`calu.logins.wrong`).
`data.unlocked` lists what is held. A blank password is a password like any other.

Every request opens its own connection and closes it: the bridge lives as long as the editor
does and must not hold a database connection for that long. The login that `unlock` opened is
held in this process's memory until it ends; it is never written to a file, a log record or
a notification.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anyio
import psycopg

from calu import admin, logins
from calu.acp.peer import RpcError
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db import place as db_place
from calu.db.connection import Db, open_stored
from calu.db.target import PasswordAuth, Target
from calu.setup import is_user_password, user_login

log = logging.getLogger(__name__)

NEEDS_PASSWORD = -32010
"""The user password is needed; `data.needs` is "user"."""

NO_DATABASE = -32017
"""No login for the database is stored here, or the database cannot be reached."""

INVALID_PARAMS = -32602
"""JSON-RPC's code for parameters the method refuses; also a value calu's validation refuses."""

CONNECT_TIMEOUT_S = 30.0
"""How long opening a database connection may take, the start of a stopped local database
included, before the request fails."""


class NeedsPassword(RpcError):
    """The error that asks the editor for the user password (`NEEDS_PASSWORD`)."""

    def __init__(
        self, wrong: bool = False, unlocked: Sequence[str] = (), blank: bool = False
    ) -> None:
        """Create the error with `data: {"needs": "user", "unlocked": [...]}`.

        Args:
            wrong: Whether a password was given and refused.
            unlocked: The names of the logins the bridge holds (`Access.unlocked`).
            blank: Whether the refused password was blank.
        """
        message = (
            logins.refused(blank)
            if wrong
            else "calu needs the calu user password (the one chosen at `calu setup`)"
        )
        super().__init__(
            NEEDS_PASSWORD, message, {"needs": logins.USER, "unlocked": list(unlocked)}
        )


class NoCredential(RpcError):
    """The error of a machine without a calu login (`NO_DATABASE`)."""

    def __init__(self, message: str) -> None:
        """Create the error.

        Args:
            message: What is missing and the command that helps (`calu.logins.no_login`,
                `calu.logins.old_logins`).
        """
        super().__init__(NO_DATABASE, message)


def shared_login() -> Target | None:
    """Return the user login that root's place file publishes (an empty user password, D228).

    Returns:
        The login with its password, from the place file (`calu.db.place`), or None if there
        is no such file, it cannot be trusted, or it holds no password.
    """
    try:
        shared = db_place.read()
    except db_place.PlaceError:
        return None
    if isinstance(shared.auth, PasswordAuth) and shared.auth.password:
        return shared
    return None


class Access:
    """Opens database connections for the requests of the bridge (see the module docstring)."""

    def __init__(self, store: CredentialStore) -> None:
        """Create the access.

        Args:
            store: The credential store of the user `calu ide` runs as.
        """
        self.store = store
        self._held: Target | None = None

    def unlocked(self) -> list[str]:
        """Name the login `unlock` opened and the bridge holds.

        Returns:
            ["user"] once it holds the user login, else [].
        """
        return [logins.USER] if self._held is not None else []

    def locked(self) -> bool:
        """Tell whether the editor should ask for the user password.

        Returns:
            True if nothing is held and the login needs the password: the `user` entry has
            one, or only root's place file names the database. False otherwise: also for a
            store that holds only the logins of before D265 (no password can help there;
            every request says to run `calu setup`), and when the store cannot be read.
        """
        if self._held is not None or shared_login() is not None:
            return False
        try:
            if self._has(logins.USER):
                return not self._free(logins.USER)
            if any(self._has(name) for name in logins.OLD_ENTRIES):
                return False
        except RpcError:
            return False
        return self._only_place()

    def can_read(self) -> bool:
        """Tell whether a request finds a login without a password parameter.

        Returns:
            True with the place file's login, a password-free `user` entry or a held login.
        """
        if self._held is not None or shared_login() is not None:
            return True
        try:
            return self._free(logins.USER)
        except RpcError:
            return False

    def can_change(self) -> bool:
        """Tell whether a request that changes settings finds a login without a password.

        Returns:
            The same as `can_read`: one login does both (D265).
        """
        return self.can_read()

    def _only_place(self) -> bool:
        """Tell whether root's place file is the only way to the database.

        With `sudo calu setup` and a user password, a user's `calu ide` finds no entry in its
        own store and a place file without a secret: the user password, given to `unlock` or
        a request, logs in as `calu_admin` there.

        Returns:
            True if the store has no `user` entry and a trustworthy place file exists; False
            otherwise, also when the store cannot be read.
        """
        try:
            if self._has(logins.USER):
                return False
        except RpcError:
            return False
        try:
            db_place.read()
        except db_place.PlaceError:
            return False
        return True

    async def unlock(self, password: str) -> dict[str, Any]:
        """Try the user password on the login and hold it when it opens.

            the `user` entry is protected       ─► decrypted with the password? held
            only root's place file              ─► the database accepts the password for
                                                   `calu_admin`? held

        Side effects: reads the credential store; may open and close one database
        connection as `calu_admin`; keeps the login in memory until the process ends. The
        password itself is not kept.

        Args:
            password: The password the user typed; a blank one is tried like any other.

        Returns:
            `{unlocked, reading, changing}`: the names of the held logins, and whether a
            reading and a changing request now find a login without a password (both the
            same, D265).

        Raises:
            NeedsPassword: If the password opened nothing.
            NoCredential: If there is no login to open (no `user` entry, no place file).
            RpcError: `NO_DATABASE` if the store cannot be read or the database cannot be
                reached to check the password.
        """
        if self._held is None and not self.can_read():
            self._held = await self._open(password)
        ready = self.can_read()
        return {"unlocked": self.unlocked(), "reading": ready, "changing": ready}

    async def _open(self, password: str) -> Target:
        """Open the login with the user password.

        Args:
            password: The typed password.

        Returns:
            The login.

        Raises:
            NeedsPassword: If the password is wrong.
            NoCredential: If there is no login to open.
            RpcError: `NO_DATABASE` if the store cannot be read or the database cannot be
                reached.
        """
        if self._has(logins.USER):
            secret = await self._load(password)
            try:
                return Target.parse(secret)
            except ValueError as error:
                raise RpcError(
                    NO_DATABASE, f"the 'user' credential holds no database login: {error}"
                ) from error
            finally:
                wipe(secret)
        self._refuse_old()
        try:
            place = db_place.read()
        except db_place.PlaceError as error:
            raise NoCredential(logins.no_login()) from error
        try:
            with anyio.fail_after(CONNECT_TIMEOUT_S):
                accepted = await is_user_password(place, password)
        except (psycopg.Error, ValueError, TimeoutError) as error:
            # Also TargetError. The text of a connection error holds no password.
            raise RpcError(
                NO_DATABASE, f"the database cannot be reached: {_first_line(error)}"
            ) from error
        if not accepted:
            raise NeedsPassword(True, self.unlocked(), blank=not password)
        return user_login(place, password)

    def _refuse_old(self) -> None:
        """Stop on a store that holds only the logins of before D265.

        Raises:
            NoCredential: With `calu.logins.old_logins` if any of them is stored.
        """
        old = [name for name in logins.OLD_ENTRIES if self._has(name)]
        if old:
            raise NoCredential(logins.old_logins("admin" in old))

    async def read[T](
        self,
        work: Callable[[Db], Awaitable[T]],
        password: str | None = None,
        start_local: bool = True,
    ) -> T:
        """Run `work` with the user login of this machine, and close the connection.

        Args:
            work: Called with the open connection.
            password: The user password, if the request brought one.
            start_local: False leaves a stopped local Docker database stopped (the status
                request must not start one).

        Returns:
            What `work` returned.

        Raises:
            RpcError: `NEEDS_PASSWORD` if the login needs a password that is missing or
                wrong; `NO_DATABASE` if no login is stored or the database cannot be
                reached or refuses; `INVALID_PARAMS` if `work` raised `admin.AdminError`.
                An `RpcError` that `work` raises passes through.
        """
        login, typed = await self._login(password)
        return await self._run(login, typed, work, start_local)

    async def change[T](self, work: Callable[[Db], Awaitable[T]], password: str | None = None) -> T:
        """Run `work`, a change of settings, with the user login, and close the connection.

        Args:
            work: Called with the open connection.
            password: The user password, if the request brought one.

        Returns:
            What `work` returned.

        Raises:
            RpcError: As `read`.
        """
        login, typed = await self._login(password)
        return await self._run(login, typed, work, True)

    def place(self) -> str:
        """Say where the database is, without a secret and without a password.

        Returns:
            Host, port and database name of the held login, the place file's or the
            password-free `user` entry's; "" if none of them can be read.
        """
        if self._held is not None:
            return self._held.place
        shared = shared_login()
        if shared is not None:
            return shared.place
        try:
            if logins.USER in self.store and not self.store.needs_password(logins.USER):
                secret = self.store.load(logins.USER)
                try:
                    return Target.parse(secret).place
                finally:
                    wipe(secret)
            return db_place.read().place
        except CredentialError, OSError, ValueError:
            return ""

    def _has(self, name: str) -> bool:
        """Tell whether the store holds an entry.

        Args:
            name: The credential name.

        Returns:
            True if it does.

        Raises:
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        try:
            return name in self.store
        except (CredentialError, OSError) as error:
            raise RpcError(NO_DATABASE, f"the credential store cannot be read: {error}") from error

    def _free(self, name: str) -> bool:
        """Tell whether the store holds an entry that needs no password.

        Args:
            name: The credential name.

        Returns:
            True for a stored, unprotected entry.

        Raises:
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        if not self._has(name):
            return False
        try:
            return not self.store.needs_password(name)
        except (CredentialError, OSError) as error:
            raise RpcError(NO_DATABASE, f"the credential store cannot be read: {error}") from error

    async def _load(self, password: str | None) -> bytearray:
        """Read the `user` entry, with the password if it has one.

        Args:
            password: The request's password, or None.

        Returns:
            The secret; the caller wipes it.

        Raises:
            NeedsPassword: If the entry is protected and the password is missing or wrong.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        if not self._free(logins.USER) and password is None:
            raise NeedsPassword(unlocked=self.unlocked())
        try:
            # Deriving the key takes a noticeable time: not on the event loop.
            return await anyio.to_thread.run_sync(self.store.load, logins.USER, password)
        except WrongPassword as error:
            raise NeedsPassword(True, self.unlocked(), blank=not password) from error
        except (CredentialError, OSError) as error:
            raise RpcError(NO_DATABASE, f"the 'user' credential cannot be read: {error}") from error

    async def _login(self, password: str | None) -> tuple[Target | bytearray, str | None]:
        """Choose the login of a request (the table of the module docstring).

        Args:
            password: The request's password, or None.

        Returns:
            The login, and the typed password if the login's database password is that one
            (then a refusal by the database means a wrong password), else None.

        Raises:
            NeedsPassword: If the login needs a password that is missing or wrong.
            NoCredential: If no login is stored and no place file names a database, or the
                store holds only logins of before D265.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        if self._held is not None:
            return self._held, None
        shared = shared_login()
        if shared is not None:
            return shared, None
        if self._has(logins.USER):
            return await self._load(password), None
        self._refuse_old()
        if not self._only_place():
            raise NoCredential(logins.no_login())
        if password is None:
            raise NeedsPassword(unlocked=self.unlocked())
        try:
            return user_login(db_place.read(), password), password
        except ValueError as error:  # PlaceError, TargetError
            raise RpcError(
                NO_DATABASE, f"the place file names no usable database: {error}"
            ) from error

    async def _run[T](
        self,
        login: Target | bytearray,
        typed: str | None,
        work: Callable[[Db], Awaitable[T]],
        start_local: bool,
    ) -> T:
        """Open a connection, run `work` and close the connection.

        Side effect: wipes `login` if it is a secret of the store.

        Args:
            login: The login.
            typed: The password the user typed, if the login's password is that one; None
                for a stored or held login.
            work: Called with the open connection.
            start_local: Whether a stopped local Docker database is started.

        Returns:
            What `work` returned.

        Raises:
            NeedsPassword: If the database refuses a typed password (`calu.logins.refused`,
                which calls a blank one blank).
            RpcError: `NO_DATABASE` if the database cannot be reached, refuses a stored
                login (`calu.logins.outdated`) or refuses a statement; `INVALID_PARAMS` if
                `work` raised `admin.AdminError`.
        """
        try:
            try:
                with anyio.fail_after(CONNECT_TIMEOUT_S):
                    conn = await open_stored(login, start_local)
            except TimeoutError as error:
                raise RpcError(
                    NO_DATABASE,
                    f"the database did not answer within {CONNECT_TIMEOUT_S:.0f} seconds",
                ) from error
            except psycopg.OperationalError as error:
                if logins.refused_by_database(str(error)):
                    if typed is not None:
                        raise NeedsPassword(True, self.unlocked(), blank=not typed) from error
                    place = login.place if isinstance(login, Target) else self.place()
                    raise RpcError(NO_DATABASE, logins.outdated(place)) from error
                raise RpcError(
                    NO_DATABASE, f"the database cannot be reached: {_first_line(error)}"
                ) from error
            finally:
                if isinstance(login, bytearray):
                    wipe(login)
            try:
                return await work(Db(conn))
            finally:
                with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                    await conn.close()
        except admin.AdminError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        except psycopg.Error as error:
            log.info("a database request failed", exc_info=True)
            raise RpcError(NO_DATABASE, f"the database refused: {_first_line(error)}") from error


def _first_line(error: Exception) -> str:
    """Return the first line of an error's text.

    Args:
        error: The error.

    Returns:
        Its first line, or "no detail" if it has no text.
    """
    text = str(error).strip()
    return text.splitlines()[0] if text else "no detail"
