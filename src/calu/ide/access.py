"""The database logins of `calu ide`, chosen without ever asking on a terminal.

    reading (sessions, settings, tokens, session requests, renaming a session):
      place file with a password ─► that login            (`sudo calu setup --no-password`)
      a password-free entry      ─► operator, calu_admin, admin: the narrowest first
      a protected entry          ─► operator, admin, calu_admin: needs `password`

    changing (settings, models, checker instructions):
      password-free calu_admin   ─► that entry
      place file with a password ─► that login
      admin                      ─► that entry (needs `password` if it is protected)
      protected calu_admin       ─► that entry, needs `password`
      only operator              ─► `calu_admin` at the operator's database, logged in with
                                    `password` as the admin password; nothing is stored

The standard input of `calu ide` is the editor's protocol stream, so a prompt would read a
request and take it for the password. A login that needs a password the request did not bring
is therefore the JSON-RPC error `NEEDS_PASSWORD` with `data: {"needs": <credential name>}`;
the editor asks its user and repeats the request with `password`. A wrong password is the same
error again, with a message that says so.

Every request opens its own connection and closes it: the bridge lives as long as the editor
does and must not hold a database connection, or a decrypted login, for that long.
"""

import logging
from collections.abc import Awaitable, Callable

import anyio
import psycopg

from calu import admin
from calu.acp.peer import RpcError
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db import place as db_place
from calu.db.connection import Db, open_stored
from calu.db.target import PasswordAuth, Target
from calu.setup import admin_login

log = logging.getLogger(__name__)

NEEDS_PASSWORD = -32010
"""A credential is protected by a password; `data.needs` names it."""

NO_DATABASE = -32017
"""No credential for the database is stored here, or the database cannot be reached."""

INVALID_PARAMS = -32602
"""JSON-RPC's code for parameters the method refuses; also a value calu's validation refuses."""

ADMIN_ENTRY = "calu_admin"
"""The store entry `calu setup --no-password` writes: the login of the role `calu_admin`."""

READ_FREE = ("operator", ADMIN_ENTRY, "admin")
"""Password-free entries a reading request tries, the narrowest login first."""

READ_PROTECTED = ("operator", "admin", ADMIN_ENTRY)
"""Protected entries a reading request tries, in this order, with the request's password."""

CONNECT_TIMEOUT_S = 30.0
"""How long opening a database connection may take, the start of a stopped local database
included, before the request fails."""


class NeedsPassword(RpcError):
    """The error that asks the editor for a credential's password (`NEEDS_PASSWORD`)."""

    def __init__(self, name: str, wrong: bool = False) -> None:
        """Create the error with `data: {"needs": name}`.

        Args:
            name: The credential whose password is needed ("admin", "operator",
                "calu_admin").
            wrong: Whether a password was given and refused.
        """
        message = (
            f"wrong password for the {name!r} credential"
            if wrong
            else f"the {name!r} credential needs its password"
        )
        super().__init__(NEEDS_PASSWORD, message, {"needs": name})


class NoCredential(RpcError):
    """The error of a machine without a usable database credential (`NO_DATABASE`)."""

    def __init__(self, what: str) -> None:
        """Create the error; its message names the commands that help.

        Args:
            what: "read" or "change", for the message.
        """
        hint = (
            "`calu setup` creates the database and its logins on this machine; for a "
            "database elsewhere, `calu credential set operator` stores a login that reads"
        )
        if what == "change":
            hint += " and `calu credential set admin` one that changes settings"
        super().__init__(NO_DATABASE, f"no database credential is stored here. {hint}")


def shared_login() -> Target | None:
    """Return the login of `calu_admin` that `calu setup --no-password` made readable.

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

    async def read[T](
        self,
        work: Callable[[Db], Awaitable[T]],
        password: str | None = None,
        start_local: bool = True,
    ) -> T:
        """Run `work` with a login that reads, and close the connection.

        Args:
            work: Called with the open connection.
            password: The password of a protected credential, if the request brought one.
            start_local: False leaves a stopped local Docker database stopped (the status
                request must not start one).

        Returns:
            What `work` returned.

        Raises:
            RpcError: `NEEDS_PASSWORD` if the login needs a password that is missing or
                wrong; `NO_DATABASE` if no credential is stored or the database cannot be
                reached or refuses; `INVALID_PARAMS` if `work` raised `admin.AdminError`.
                An `RpcError` that `work` raises passes through.
        """
        login, name = await self._reading_login(password)
        return await self._run(login, name, False, work, start_local)

    async def change[T](self, work: Callable[[Db], Awaitable[T]], password: str | None = None) -> T:
        """Run `work` with a login that may change settings, and close the connection.

        Args:
            work: Called with the open connection.
            password: The password of a protected credential, or the admin password where
                only an `operator` credential is stored.

        Returns:
            What `work` returned.

        Raises:
            RpcError: As `read`.
        """
        login, name, typed = await self._changing_login(password)
        return await self._run(login, name, typed, work, True)

    def place(self) -> str:
        """Say where the database is that a reading request would use, without a secret.

        Returns:
            Host, port and database name of the first login that can be read without a
            password; "" if there is none or it cannot be read.
        """
        shared = shared_login()
        if shared is not None:
            return shared.place
        try:
            for name in READ_FREE:
                if name in self.store and not self.store.needs_password(name):
                    secret = self.store.load(name)
                    try:
                        return Target.parse(secret).place
                    finally:
                        wipe(secret)
        except CredentialError, OSError, ValueError:
            return ""
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

    async def _load(self, name: str, password: str | None) -> bytearray:
        """Read one entry of the store, with its password if it has one.

        Args:
            name: The credential name; it is stored.
            password: The request's password, or None.

        Returns:
            The secret; the caller wipes it.

        Raises:
            NeedsPassword: If the entry is protected and the password is missing or wrong.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        if not self._free(name) and not password:
            raise NeedsPassword(name)
        try:
            # Deriving the key takes a noticeable time: not on the event loop.
            return await anyio.to_thread.run_sync(self.store.load, name, password)
        except WrongPassword as error:
            raise NeedsPassword(name, wrong=True) from error
        except (CredentialError, OSError) as error:
            raise RpcError(
                NO_DATABASE, f"the {name!r} credential cannot be read: {error}"
            ) from error

    async def _reading_login(self, password: str | None) -> tuple[Target | bytearray, str]:
        """Choose the login of a reading request.

        Args:
            password: The request's password, or None.

        Returns:
            The login and the name of the credential it came from.

        Raises:
            NeedsPassword: If the login needs a password that is missing or wrong.
            NoCredential: If no credential is stored.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        shared = shared_login()
        if shared is not None:
            return shared, "place file"
        for name in READ_FREE:
            if self._free(name):
                return await self._load(name, None), name
        for name in READ_PROTECTED:
            if self._has(name):
                return await self._load(name, password), name
        raise NoCredential("read")

    async def _changing_login(self, password: str | None) -> tuple[Target | bytearray, str, bool]:
        """Choose the login of a request that changes settings.

        Args:
            password: The request's password, or None.

        Returns:
            The login, the name of the credential the editor would ask a password for, and
            whether the login's database password is the typed one (then a refusal by the
            database means a wrong password).

        Raises:
            NeedsPassword: If the login needs a password that is missing or wrong.
            NoCredential: If no credential is stored.
            RpcError: `NO_DATABASE` if the store or the operator's login cannot be read.
        """
        if self._free(ADMIN_ENTRY):
            return await self._load(ADMIN_ENTRY, None), ADMIN_ENTRY, False
        shared = shared_login()
        if shared is not None:
            return shared, "place file", False
        for name in ("admin", ADMIN_ENTRY):
            if self._has(name):
                return await self._load(name, password), name, False
        if not self._has("operator"):
            raise NoCredential("change")
        if not password:
            raise NeedsPassword("admin")
        secret = await self._load("operator", password)
        try:
            return admin_login(Target.parse(secret), password), "admin", True
        except ValueError as error:  # also TargetError: a login found through a service file
            raise RpcError(
                NO_DATABASE, f"the 'operator' credential names no usable database: {error}"
            ) from error
        finally:
            wipe(secret)

    @staticmethod
    async def _run[T](
        login: Target | bytearray,
        name: str,
        typed: bool,
        work: Callable[[Db], Awaitable[T]],
        start_local: bool,
    ) -> T:
        """Open a connection, run `work` and close the connection.

        Side effect: wipes `login` if it is a secret of the store.

        Args:
            login: The login.
            name: The credential the editor asks a password for when the database refuses a
                typed one.
            typed: Whether the login's password was typed by the user.
            work: Called with the open connection.
            start_local: Whether a stopped local Docker database is started.

        Returns:
            What `work` returned.

        Raises:
            NeedsPassword: If the database refuses a typed password.
            RpcError: `NO_DATABASE` if the database cannot be reached or refuses a
                statement; `INVALID_PARAMS` if `work` raised `admin.AdminError`.
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
                if typed and "password authentication failed" in str(error):
                    raise NeedsPassword(name, wrong=True) from error
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
