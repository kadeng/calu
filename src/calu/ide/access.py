"""The database logins of `calu ide`, chosen without ever asking on a terminal.

    reading (sessions, settings, tokens, session requests, renaming a session):
      place file with a password ─► that login            (`sudo calu setup --no-password`)
      a password-free entry      ─► operator, calu_admin, admin: the narrowest first
      a held login               ─► what `unlock` opened: operator, calu_admin, admin, the role
      a protected entry          ─► operator, admin, calu_admin: needs `password`
      only root's place file     ─► NEEDS_PASSWORD "admin": `unlock` holds the role's login

    changing (settings, models, checker instructions):
      password-free calu_admin   ─► that entry
      place file with a password ─► that login
      password-free admin        ─► that entry
      a held login               ─► what `unlock` opened: admin, calu_admin, the role
      protected admin, calu_admin─► that entry, needs `password`
      only operator              ─► `calu_admin` at the operator's database, logged in with
                                    `password` as the admin password; nothing is stored
      only root's place file     ─► NEEDS_PASSWORD "admin", as for reading

    unlock(password)  (`ide.unlock`: one password, once per bridge process)
      every protected entry (operator, admin, calu_admin) ─► opened with it? then held
      a place for the database is known                   ─► accepted as the password of the
                                                             role `calu_admin`? then held

The standard input of `calu ide` is the editor's protocol stream, so a prompt would read a
request and take it for the password. A login that needs a password the request did not bring
is therefore the JSON-RPC error `NEEDS_PASSWORD` with `data: {"needs": <credential name>}`;
the editor asks its user and repeats the request with `password`. A wrong password is the same
error again, with a message that says so. `data.unlocked` lists what is held, so that the
editor can tell "never asked" from "the one password did not cover this". A blank password is
a password like any other.

Every request opens its own connection and closes it: the bridge lives as long as the editor
does and must not hold a database connection for that long. The logins that `unlock` opened
are held in this process's memory until it ends; they are never written to a file, a log
record or a notification.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anyio
import psycopg

from calu import admin
from calu.acp.peer import RpcError
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db import place as db_place
from calu.db.connection import Db, open_stored
from calu.db.target import PasswordAuth, Target
from calu.setup import admin_login, is_admin_password

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

PROTECTED = ("operator", "admin", ADMIN_ENTRY)
"""The store entries `unlock` tries a password on."""

ROLE = "calu_admin role"
"""The name, in `unlocked`, of the login of the database role `calu_admin` with a typed
password; unlike the store entry `calu_admin` it is no entry of the credential store."""

HELD_READING = ("operator", ADMIN_ENTRY, "admin", ROLE)
"""Held logins a reading request uses, the narrowest first."""

HELD_CHANGING = ("admin", ADMIN_ENTRY, ROLE)
"""Held logins that may change settings."""

CONNECT_TIMEOUT_S = 30.0
"""How long opening a database connection may take, the start of a stopped local database
included, before the request fails."""


class NeedsPassword(RpcError):
    """The error that asks the editor for a credential's password (`NEEDS_PASSWORD`)."""

    def __init__(self, name: str, wrong: bool = False, unlocked: Sequence[str] = ()) -> None:
        """Create the error with `data: {"needs": name, "unlocked": [...]}`.

        Args:
            name: The credential whose password is needed ("admin", "operator",
                "calu_admin").
            wrong: Whether a password was given and refused.
            unlocked: The names of the logins the bridge holds (`Access.unlocked`).
        """
        message = (
            f"wrong password for the {name!r} credential"
            if wrong
            else f"the {name!r} credential needs its password"
        )
        super().__init__(NEEDS_PASSWORD, message, {"needs": name, "unlocked": list(unlocked)})


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
        self._held: dict[str, Target] = {}

    def unlocked(self) -> list[str]:
        """Name the logins `unlock` opened and the bridge holds.

        Returns:
            Store entry names ("operator", "admin", "calu_admin") and `ROLE`, in the order
            they were opened.
        """
        return list(self._held)

    def locked(self) -> bool:
        """Tell whether the editor should ask for the one password.

        Returns:
            True if nothing is held and either the store holds a protected entry among
            `PROTECTED`, or root's place file names a database that no login of this user
            reaches (`_only_place`: calu runs as root, the admin password opens it); False
            otherwise, and when the store cannot be read.
        """
        if self._held:
            return False
        try:
            if any(self._has(name) and not self._free(name) for name in PROTECTED):
                return True
        except RpcError:
            return False
        return self._only_place()

    def can_read(self) -> bool:
        """Tell whether a reading request finds a login without a password parameter.

        Returns:
            True with the place file's login, a password-free entry or a held login.
        """
        try:
            free = any(self._free(name) for name in READ_FREE)
        except RpcError:
            free = False
        return free or shared_login() is not None or bool(self._held)

    def can_change(self) -> bool:
        """Tell whether a changing request finds a login without a password parameter.

        Returns:
            True with the place file's login, a password-free `calu_admin` or `admin`
            entry, or a held login that may change settings.
        """
        try:
            free = self._free(ADMIN_ENTRY) or self._free("admin")
        except RpcError:
            free = False
        held = any(name in self._held for name in HELD_CHANGING)
        return free or shared_login() is not None or held

    def _only_place(self) -> bool:
        """Tell whether root's place file is the only way to the database.

        With `sudo calu setup` and passwords, a user's `calu ide` finds no entry in its own
        store and a place file without a secret: the admin password, given to `unlock`,
        is the login (as the role `calu_admin`). The editor then asks for it instead of
        reporting that no credential is stored.

        Returns:
            True if no login is held, the store has none of the `PROTECTED` entries and no
            password-free one, and a trustworthy place file exists; False otherwise, also
            when the store cannot be read.
        """
        if self._held or shared_login() is not None:
            return False
        try:
            if any(self._has(name) for name in PROTECTED):
                return False
        except RpcError:
            return False
        try:
            db_place.read()
        except db_place.PlaceError:
            return False
        return True

    async def unlock(self, password: str) -> dict[str, Any]:
        """Try one password on everything it may open, and hold what opened.

            protected store entries (operator, admin, calu_admin), not held yet
              ─► decrypted with the password? the login is held under the entry's name
            no login that changes settings yet, and the database's place is known
              (a held login, a password-free entry, the place file)
              ─► the database accepts the password for the role `calu_admin`?
                 that login is held as `ROLE`

        Side effects: reads the credential store; may open and close one database
        connection as `calu_admin`; keeps the logins in memory until the process ends.
        The password itself is not kept.

        Args:
            password: The password the user typed; a blank one is tried like any other.

        Returns:
            `{unlocked, reading, changing}`: the names of all held logins, and whether a
            reading and a changing request now find a login without a password.

        Raises:
            NeedsPassword: If the password opened nothing and a login is still missing;
                `data.needs` names the first credential that is missing.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        opened = 0
        for name in PROTECTED:
            if name in self._held or not self._has(name) or self._free(name):
                continue
            try:
                secret = await anyio.to_thread.run_sync(self.store.load, name, password)
            except WrongPassword:
                continue
            except (CredentialError, OSError) as error:
                log.info("the %r credential could not be read: %s", name, error)
                continue
            try:
                self._held[name] = Target.parse(secret)
                opened += 1
            except ValueError:
                log.info("the %r credential holds no database login this version reads", name)
            finally:
                wipe(secret)
        if not self.can_change():
            place = await self._place_login()
            if place is not None:
                try:
                    with anyio.fail_after(CONNECT_TIMEOUT_S):
                        accepted = await is_admin_password(place, password)
                    if accepted:
                        self._held[ROLE] = admin_login(place, password)
                        opened += 1
                except (psycopg.Error, ValueError, TimeoutError) as error:
                    # Also TargetError. The text of a connection error holds no password.
                    log.info("the admin password could not be checked: %s", _first_line(error))
        reading, changing = self.can_read(), self.can_change()
        if not opened and not (reading and changing):
            missing = next(
                (
                    n
                    for n in PROTECTED
                    if self._has(n) and not self._free(n) and n not in self._held
                ),
                "admin",
            )
            raise NeedsPassword(missing if not reading else "admin", True, self.unlocked())
        return {"unlocked": self.unlocked(), "reading": reading, "changing": changing}

    async def _place_login(self) -> Target | None:
        """Find any login that says where the database is, for trying the admin password.

        Returns:
            A held login, else a password-free store entry, else the place file's record
            (which needs no password of its own for this); None if none of them exists or
            can be read.
        """
        for target in self._held.values():
            return target
        for name in READ_FREE:
            try:
                if not self._free(name):
                    continue
                secret = await anyio.to_thread.run_sync(self.store.load, name, None)
            except RpcError, CredentialError, OSError:
                continue
            try:
                return Target.parse(secret)
            except ValueError:
                continue
            finally:
                wipe(secret)
        try:
            return db_place.read()
        except db_place.PlaceError:
            return None

    async def read[T](
        self,
        work: Callable[[Db], Awaitable[T]],
        password: str | None = None,
        start_local: bool = True,
    ) -> T:
        """Run `work` with the reading login of this machine, and close the connection.

        "Reading" names the login, not what `work` may do with it: the login chosen
        (`_reading_login`: the operator's, or an admin login where only that is stored) also
        writes what an operator may, and callers use it for that: client tokens, session
        requests, session names, and the closing of gone sessions' escalations
        (`herd.sessions`).

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
        if not self._free(name) and password is None:
            raise NeedsPassword(name, unlocked=self.unlocked())
        try:
            # Deriving the key takes a noticeable time: not on the event loop.
            return await anyio.to_thread.run_sync(self.store.load, name, password)
        except WrongPassword as error:
            raise NeedsPassword(name, True, self.unlocked()) from error
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
            NeedsPassword: If the login needs a password that is missing or wrong, or only
                root's place file names the database (`_only_place`; `ide.unlock` with the
                admin password then holds a login).
            NoCredential: If no credential is stored and no place file names a database.
            RpcError: `NO_DATABASE` if the store cannot be read.
        """
        shared = shared_login()
        if shared is not None:
            return shared, "place file"
        for name in READ_FREE:
            if self._free(name):
                return await self._load(name, None), name
        for name in HELD_READING:
            if name in self._held:
                return self._held[name], name
        for name in READ_PROTECTED:
            if self._has(name):
                return await self._load(name, password), name
        if self._only_place():
            raise NeedsPassword("admin", unlocked=self.unlocked())
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
            NeedsPassword: If the login needs a password that is missing or wrong, or only
                root's place file names the database (`_only_place`).
            NoCredential: If no credential is stored and no place file names a database.
            RpcError: `NO_DATABASE` if the store or the operator's login cannot be read.
        """
        if self._free(ADMIN_ENTRY):
            return await self._load(ADMIN_ENTRY, None), ADMIN_ENTRY, False
        shared = shared_login()
        if shared is not None:
            return shared, "place file", False
        if self._free("admin"):
            return await self._load("admin", None), "admin", False
        for name in HELD_CHANGING:
            if name in self._held:
                return self._held[name], name, False
        for name in ("admin", ADMIN_ENTRY):
            if self._has(name):
                return await self._load(name, password), name, False
        if not self._has("operator"):
            if self._only_place():
                raise NeedsPassword("admin", unlocked=self.unlocked())
            raise NoCredential("change")
        if password is None:
            raise NeedsPassword("admin", unlocked=self.unlocked())
        if "operator" in self._held:
            place = self._held["operator"]
        else:
            secret = await self._load("operator", password)
            try:
                place = Target.parse(secret)
            except ValueError as error:
                raise RpcError(
                    NO_DATABASE, f"the 'operator' credential names no usable database: {error}"
                ) from error
            finally:
                wipe(secret)
        try:
            return admin_login(place, password), "admin", True
        except ValueError as error:  # TargetError: a login found through a service file
            raise RpcError(
                NO_DATABASE, f"the 'operator' credential names no usable database: {error}"
            ) from error

    async def _run[T](
        self,
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
                refused = "password authentication failed" in str(error)
                if typed and (refused or "no password supplied" in str(error)):
                    raise NeedsPassword(name, True, self.unlocked()) from error
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
