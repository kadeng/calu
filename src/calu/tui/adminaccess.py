"""What the full-screen client changes in the database, with the user login.

A session may read the checkers' instructions and its settings and cannot change them: it
must not be able to change what it is judged by. Three things of the client therefore do not
go through the session at all:

* saving a checker's instructions (the checkers screen), as `calu checker edit` does;
* setting `honesty.mode` (`/honesty off | track | enforce`), as `calu config set` does;
* adding, listing and removing temporary rules (`/rule`, `/rules`, `/rule-remove`), as
  `calu rule` does.

All use the user login (the role `calu_admin`, `calu.logins`, agent/records/PLAN.md D265):
the `user` entry of the local credential store (`calu.credentials`), or, given a database
login without a password, that login with the user password a person types. The checker
instructions and `honesty.mode` are written with the functions of `calu.admin`, the
temporary rules with those of `calu.temp_rules`. The client
asks for the user password when it is needed. Where the machine has no login,
`AdminAccess.problem` says so and the client shows the command that does the same.

`AdminAccess` is the interface the client uses; `StoreAdmin` is the real one, and tests pass
their own. This module imports nothing from Textual.

The database of this machine's credential need not be the one the attached session reads
(another host, a second local database). Nothing here can tell; the client therefore compares
what the session reports after a change with what was stored, and says so when they differ.
A save names the version the editor was opened on (`base_version`): if the stored definition
has another version by then, somebody changed it meanwhile and the save is refused.
"""

import getpass
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn, Protocol

import anyio
import psycopg

from calu import admin, herd, logins, servers, session_requests, temp_rules
from calu.credentials import CredentialError, CredentialStore, WrongPassword, wipe
from calu.db.connection import Db
from calu.db.connection import open_stored as connect
from calu.db.target import PasswordAuth, Target
from calu.protocol.client import RelayTarget, RequestFailed, relay_call
from calu.session_requests import SessionRequest

CREDENTIAL = logins.USER
"""The credential the changes use: the user login (`calu_admin`)."""

CONFLICT_HELP = "the text stays in the editor; leave it with esc esc, press r, edit again"
"""What follows "changed meanwhile to version N" when a checker's instructions were stored by
someone else since the editor read them: the keys in the order they work (`r` reads again
only outside the editor, and leaving the editor drops the text)."""

NO_CREDENTIAL = (
    "this machine has no calu login: `calu setup` sets one up (for a database another machine "
    "set up: `calu credential set user`)"
)
"""What `StoreAdmin.problem` says without the credential."""

CONNECT_TIMEOUT_S = 30.0
"""How long opening the database connection may take before the change is given up; as long
as libpq's own `connect_timeout` (`calu.db.connection.LIVENESS`)."""

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


@dataclass(frozen=True)
class Served:
    """A `calu serve` that runs, as `/running` names it (agent/records/PLAN.md D243)."""

    hostname: str
    os_user: str
    projects_directory: str
    """The directory it starts sessions in or below; "" if it did not record one."""

    def line(self) -> str:
        """Return the server as one line of the `/running` page.

        Returns:
            For example `calu serve of ada@box serves /home/ada/projects`.
        """
        where = self.projects_directory or "a directory it did not record"
        return f"calu serve of {self.os_user}@{self.hostname} serves {where}"


class AdminAccess(Protocol):
    """The database changes the client can make (see the module docstring)."""

    def problem(self) -> str | None:
        """Say why nothing can be changed from this machine.

        Returns:
            None if the credential is there, else the sentence to show.
        """
        ...

    def sessions_problem(self) -> str | None:
        """Say why the running sessions can be neither listed nor asked for from here.

        `/running`, `/switch`, `/new`, `/fork` and `/rewind` depend on this, not on
        `problem`: through a relay (`RelayAdmin`) they work although settings cannot be
        changed.

        Returns:
            None if `sessions` and `request_session` can work, else the sentence to show.
        """
        ...

    def session_host(self) -> str:
        """Name the host whose sessions `/switch` can attach to.

        Returns:
            This machine's name, or the relay's server for `RelayAdmin`.
        """
        ...

    def needs_password(self) -> bool:
        """Tell whether the client has to ask for the credential's password.

        Returns:
            True for a credential stored with a password; False without one or without the
            credential.
        """
        ...

    async def sessions(self, password: str | None) -> list[herd.SessionRow]:
        """List the sessions that run, of every directory and host, newest first.

        Args:
            password: The credential's password, or None if none is needed.

        Returns:
            The sessions that have not ended.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the database cannot be reached.
        """
        ...

    async def served(self, password: str | None) -> list[Served]:
        """List the `calu serve` processes that run, with the directory each one serves.

        A server that died without ending its row is left out (`calu.servers.running`).

        Args:
            password: The credential's password, or None if none is needed.

        Returns:
            The servers; empty when they cannot be read with this login or schema.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the database cannot be reached.
        """
        ...

    async def rename_session(self, session_id: str, name: str, password: str | None) -> str:
        """Give a session a name, or take its name away (`calu.herd.rename_session`).

        Side effect: writes `sessions.name`.

        Args:
            session_id: Id of the `sessions` row.
            name: The name; "" removes it.
            password: The credential's password, or None if it has none.

        Returns:
            The stored name.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the name is too long, no session has that id, or the database
                cannot be reached or refuses (among them a schema older than migration 0036).
        """
        ...

    async def request_session(self, wanted: SessionRequest, password: str | None) -> str:
        """Ask the `calu serve` of this machine for a session and wait until it runs (D208).

        Side effect: writes a `session_requests` row; a server that claims it starts a
        session.

        Args:
            wanted: The session to start.
            password: The credential's password, or None if it has none.

        Returns:
            The id of the started session's row.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            NoServerAnswered: If no server claimed the request in time.
            AdminFailed: If the server did not start the session (the message is its
                reason), or the database cannot be reached.
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

    async def temporary_rules(
        self, session_id: str | None, password: str | None
    ) -> list[tuple[str, bool]]:
        """List the temporary rules that count now (`/rules`, `calu.temp_rules`).

        Args:
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            One (line, applies) per active rule, newest first: the rule in one line
            (`TemporaryRule.line`), and whether it applies to the attached session.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential or the database refuses.
        """
        ...

    async def add_temporary_rule(
        self, arguments: str, session_id: str | None, password: str | None
    ) -> str:
        """Store a temporary rule as `/rule` gives it (`temp_rules.add_from_client`).

        Args:
            arguments: What followed `/rule`: `[duration] [--session | --project | --global]
                text`.
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            The stored rule in one line.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the rule is refused (the message says why), there is no
                credential or the database refuses.
        """
        ...

    async def remove_temporary_rule(
        self, rule_id: int, session_id: str | None, password: str | None
    ) -> str:
        """End a temporary rule before it expires (`/rule-remove`).

        Args:
            rule_id: The rule's number.
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            The removed rule in one line.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If no active rule has the number, there is no credential or the
                database refuses.
        """
        ...


def _has_secret(database: Target) -> bool:
    """Tell whether a login can connect as it is, without a typed password.

    Args:
        database: The login.

    Returns:
        True for a password login with a password, and for a login that authenticates
        another way (a token); False for a password login whose password is empty.
    """
    return not isinstance(database.auth, PasswordAuth) or bool(database.auth.password)


class NoServerAnswered(AdminFailed):
    """No `calu serve` claimed a session request; it was withdrawn."""


def _os_user() -> str:
    """Name the person at this client, for the record of a temporary rule.

    Returns:
        The OS user name, or "unknown" if the system does not tell it.
    """
    try:
        return getpass.getuser() or "unknown"
    except OSError, KeyError:
        return "unknown"


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

    def __init__(
        self,
        store: CredentialStore | None = None,
        database: Target | None = None,
        unavailable: str | None = None,
    ) -> None:
        """Create the access.

        Args:
            store: The credential store; the default store of this user if None.
            database: The login of the role `calu_admin` at the database of the attached
                session, without its password (`calu.setup.user_login`). With it, every change
                logs in there as `calu_admin` with the user password the user types; nothing
                is stored (agent/records/PLAN.md D213). A client that runs as root passes
                instead the complete login it holds in memory: no password is asked at all
                (D249). None uses the store's `user` entry.
            unavailable: Why nothing can be changed from this client, when neither a
                database login nor this user's store applies (a session of root's calu whose
                database is not known, D228); `problem` then returns it and the store is
                not read. None otherwise.
        """
        self.store = store or CredentialStore()
        self.database = database
        self.unavailable = unavailable
        self._looked_in_store = False

    def _complete(self) -> None:
        """Take the password of a `calu_admin` login from the store, if it lies there open.

        An empty user password stores the login of `calu_admin` protected by the file's
        mode alone (`CREDENTIAL`). A client that was handed that login without its password
        (`database`) would ask the user for a password nobody chose. If the store holds the
        entry without a password and it names the same database and role, `database` becomes
        that complete login; nothing is asked afterwards. Looked up once; an unreadable
        store or another database leaves `database` as it is.
        """
        if self._looked_in_store or self.database is None or _has_secret(self.database):
            return
        self._looked_in_store = True
        try:
            if CREDENTIAL not in self.store or self.store.needs_password(CREDENTIAL):
                return
            url = self.store.load(CREDENTIAL, None)
        except CredentialError, OSError:
            return
        try:
            stored = Target.parse(url)
        except ValueError:
            return
        finally:
            wipe(url)
        same = stored.place == self.database.place and stored.user == self.database.user
        if same and _has_secret(stored):
            self.database = stored

    def problem(self) -> str | None:
        """Say why nothing can be changed from this machine.

        Returns:
            None with a database login or a stored `user` credential; `unavailable` if that
            was given; `calu.logins.old_logins` for a store that holds only the logins of
            before D265; `NO_CREDENTIAL` without the credential; or the store's own message
            if its file cannot be read.
        """
        if self.database is not None:
            return None
        if self.unavailable is not None:
            return self.unavailable
        try:
            if CREDENTIAL in self.store:
                return None
            old = [name for name in logins.OLD_ENTRIES if name in self.store]
            return logins.old_logins("admin" in old) if old else NO_CREDENTIAL
        except CredentialError as error:
            return f"the credential store cannot be read: {error}"

    def sessions_problem(self) -> str | None:
        """Say why the sessions cannot be read or asked for: the same as `problem`.

        Returns:
            What `problem` returns.
        """
        return self.problem()

    def session_host(self) -> str:
        """Name this machine, whose sessions `/switch` attaches to.

        Returns:
            The host name.
        """
        return socket.gethostname()

    def needs_password(self) -> bool:
        """Tell whether the user login needs the user password.

        Returns:
            True if it is; False without a password, without the credential or when the
            store cannot be read.
        """
        self._complete()
        if self.database is not None:
            return not _has_secret(self.database)
        try:
            return CREDENTIAL in self.store and self.store.needs_password(CREDENTIAL)
        except CredentialError:
            return False

    @staticmethod
    async def _as_admin[T](
        database: Target, password: str | None, work: Callable[[Db], Awaitable[T]]
    ) -> T:
        """Log in as `calu_admin` with the typed password, run `work` and close.

        Args:
            database: The login of `calu_admin` without its password, or a complete login
                that a client running as root holds in memory (then `password` is not used).
            password: The user password the user typed, or None.
            work: `async (Db) -> T`.

        Returns:
            What `work` returned.

        Raises:
            PasswordNeeded: If a password is needed and none was given, or the database
                refuses it.
            AdminFailed: If the database cannot be reached or refuses the change.
        """
        if _has_secret(database):
            login = database
        elif password is None:
            raise PasswordNeeded("this needs the calu user password")
        else:
            login = database.model_copy(update={"auth": PasswordAuth(password=password)})
        try:
            try:
                with anyio.fail_after(CONNECT_TIMEOUT_S):
                    conn = await connect(login, False)
            except TimeoutError as error:
                raise AdminFailed(
                    f"the database did not answer within {CONNECT_TIMEOUT_S:.0f} seconds"
                ) from error
            except psycopg.OperationalError as error:
                if logins.refused_by_database(str(error)):
                    if not _has_secret(database):
                        raise PasswordNeeded(logins.refused(not password)) from error
                    raise AdminFailed(logins.outdated(database.place)) from error
                first = str(error).strip().splitlines()[0] if str(error).strip() else ""
                raise AdminFailed(f"the database cannot be reached: {first}") from error
            try:
                return await work(Db(conn))
            finally:
                await conn.close()
        except admin.AdminError as error:
            raise AdminFailed(str(error)) from error
        except psycopg.Error as error:
            first = str(error).strip().splitlines()[0] if str(error).strip() else "no detail"
            raise AdminFailed(f"the database refused: {first}") from error

    async def _with_db[T](self, password: str | None, work: Callable[[Db], Awaitable[T]]) -> T:
        """Open the connection with the user login, run `work` with it and close it.

        The decrypted connection string is wiped as soon as the connection is open.

        Args:
            password: The user password, or None if none was typed.
            work: `async (Db) -> T`.

        Returns:
            What `work` returned.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential, the database cannot be reached or it
                refuses the change.
        """
        self._complete()
        if self.database is not None:
            return await self._as_admin(self.database, password, work)
        missing = self.problem()
        if missing is not None:
            raise AdminFailed(missing)
        if self.store.needs_password(CREDENTIAL) and password is None:
            raise PasswordNeeded("this needs the calu user password")
        try:
            # deriving the key takes a noticeable time: not on the event loop
            url = await anyio.to_thread.run_sync(self.store.load, CREDENTIAL, password)
        except WrongPassword as error:
            raise PasswordNeeded(logins.refused(not password)) from error
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
            except psycopg.OperationalError as error:
                if logins.refused_by_database(str(error)):
                    raise AdminFailed(logins.outdated(Target.parse(url).place)) from error
                first = str(error).strip().splitlines()[0] if str(error).strip() else ""
                raise AdminFailed(f"the database cannot be reached: {first}") from error
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

    async def sessions(self, password: str | None) -> list[herd.SessionRow]:
        """List the sessions that run, of every directory and host, newest first.

        Args:
            password: The credential's password, or None if none is needed.

        Returns:
            The sessions that have not ended.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the database cannot be reached.
        """
        return await self._with_db(password, herd.sessions)

    async def served(self, password: str | None) -> list[Served]:
        """List the `calu serve` processes that run, with the directory each one serves.

        A server that died without ending its row is left out (`calu.servers.running`).

        Args:
            password: The credential's password, or None if none is needed.

        Returns:
            The servers, newest first; empty when the login or the schema (before migration
            0040) cannot read them.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the database cannot be reached.
        """

        async def work(db: Db) -> list[Served]:
            """Read the servers.

            Args:
                db: The connection.

            Returns:
                The servers that run; empty if they cannot be read.
            """
            try:
                rows = await servers.running(db)
            except (
                psycopg.errors.UndefinedTable,
                psycopg.errors.UndefinedColumn,
                psycopg.errors.InsufficientPrivilege,
            ):
                return []
            return [Served(row.hostname, row.os_user, row.projects_directory) for row in rows]

        return await self._with_db(password, work)

    async def rename_session(self, session_id: str, name: str, password: str | None) -> str:
        """Give a session a name, or take its name away (`calu.herd.rename_session`).

        Side effect: writes `sessions.name`.

        Args:
            session_id: Id of the `sessions` row.
            name: The name; "" removes it.
            password: The credential's password, or None if it has none.

        Returns:
            The stored name.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the name is too long, no session has that id, or the database
                cannot be reached or refuses (among them a schema older than migration 0036).
        """

        async def work(db: Db) -> str:
            """Store the name.

            Args:
                db: The connection of the user login.

            Returns:
                The stored name.

            Raises:
                AdminFailed: If the name is refused or the session is unknown.
            """
            try:
                return await herd.rename_session(db, session_id, name)
            except (ValueError, LookupError) as error:
                raise AdminFailed(str(error)) from error

        return await self._with_db(password, work)

    async def request_session(self, wanted: SessionRequest, password: str | None) -> str:
        """Ask the `calu serve` of this machine for a session and wait until it runs (D208).

        Side effect: writes a `session_requests` row; a server that claims it starts a
        session.

        Args:
            wanted: The session to start.
            password: The credential's password, or None if it has none.

        Returns:
            The id of the started session's row.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            NoServerAnswered: If no server claimed the request in time.
            AdminFailed: If the server did not start the session (the message is its
                reason), or the database cannot be reached.
        """

        async def work(db: Db) -> str:
            """File the request and wait for its outcome.

            Args:
                db: The connection of the user login.

            Returns:
                The session's row id.

            Raises:
                NoServerAnswered: If no server claimed the request in time.
                AdminFailed: If the session was not started.
            """
            try:
                done = await session_requests.wait_for(db, await session_requests.file(db, wanted))
            except session_requests.NoServer:
                raise NoServerAnswered(
                    f"no `calu serve` answered for {wanted.os_user} on {wanted.hostname}; "
                    "start it in a terminal with `calu serve`"
                ) from None
            except TimeoutError as error:
                raise AdminFailed(str(error)) from error
            if done.state != "started" or done.session_id is None:
                raise AdminFailed(f"the server did not start the session: {done.message}")
            return done.session_id

        return await self._with_db(password, work)

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
                db: The connection of the user login.

            Returns:
                The checker after the change.

            Raises:
                AdminFailed: If the stored definition has another version than the one
                    the edited text was read at.
            """
            await admin.instructions(db, name)  # an unknown name must not create a checker
            stored = next((row for row in await admin.checkers(db) if row.name == name), None)
            if base_version is not None and stored is not None and stored.version != base_version:
                raise AdminFailed(f"changed meanwhile to version {stored.version}: {CONFLICT_HELP}")
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
                db: The connection of the user login.

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

    async def temporary_rules(
        self, session_id: str | None, password: str | None
    ) -> list[tuple[str, bool]]:
        """List the temporary rules that count now (see `AdminAccess.temporary_rules`).

        Args:
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            One (line, applies) per active rule, newest first.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If there is no credential or the database refuses.
        """

        async def work(db: Db) -> list[tuple[str, bool]]:
            """Read the rules and the session's scope.

            Args:
                db: The connection of the user login.

            Returns:
                The lines.
            """
            conversation, project = await temp_rules.session_scope(db, session_id)
            return [
                (
                    rule.line(conversation),
                    temp_rules.applies_to_session(rule, conversation, project),
                )
                for rule in await temp_rules.listed(db)
            ]

        return await self._with_db(password, work)

    async def add_temporary_rule(
        self, arguments: str, session_id: str | None, password: str | None
    ) -> str:
        """Store a temporary rule as `/rule` gives it (see `AdminAccess.add_temporary_rule`).

        Side effects: inserts a `temporary_rules` row with the user login.

        Args:
            arguments: What followed `/rule`.
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            The stored rule in one line.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If the rule is refused, there is no credential or the database
                refuses.
        """
        duration, scope, text = temp_rules.split_command(arguments)

        async def work(db: Db) -> str:
            """Store the rule.

            Args:
                db: The connection of the user login.

            Returns:
                The rule in one line.

            Raises:
                AdminFailed: If the rule is refused.
            """
            try:
                rule, conversation = await temp_rules.add_from_client(
                    db, session_id, text, duration, scope, f"calu attach:{_os_user()}"
                )
            except temp_rules.RuleError as error:
                raise AdminFailed(str(error)) from error
            return rule.line(conversation)

        return await self._with_db(password, work)

    async def remove_temporary_rule(
        self, rule_id: int, session_id: str | None, password: str | None
    ) -> str:
        """End a temporary rule (see `AdminAccess.remove_temporary_rule`).

        Side effects: marks the rule as removed with the user login.

        Args:
            rule_id: The rule's number.
            session_id: The attached calu session's id, or None.
            password: The credential's password, or None if it has none.

        Returns:
            The removed rule in one line.

        Raises:
            PasswordNeeded: If the password is missing or wrong.
            AdminFailed: If no active rule has the number, there is no credential or the
                database refuses.
        """

        async def work(db: Db) -> str:
            """Remove the rule.

            Args:
                db: The connection of the user login.

            Returns:
                The rule in one line.

            Raises:
                AdminFailed: If no active rule has the number.
            """
            conversation, _project = await temp_rules.session_scope(db, session_id)
            gone = await temp_rules.remove(db, rule_id, f"calu attach:{_os_user()}")
            if gone is None:
                raise AdminFailed(f"no active temporary rule #{rule_id}")
            return gone.line(conversation)

        return await self._with_db(password, work)


RELAY_START_TIMEOUT_S = 150.0
"""How long `RelayAdmin.request_session` waits for the relay's answer: a little longer than
the server waits for the session (`calu.serve.RELAY_START_WAIT_S`)."""


class RelayAdmin:
    """The client's database access when it is attached through a `calu serve` relay (D242).

    The relay answers for the server's sessions: `sessions` lists them and `request_session`
    asks the server for one (`relay.start`), both with the server token. Settings changes
    (checker instructions, `honesty.mode`, a session's name) and the temporary rules need
    the database and are refused with `unavailable`, which `problem` returns.
    """

    def __init__(self, relay: RelayTarget, unavailable: str) -> None:
        """Create the access.

        Args:
            relay: The relay, with the server token.
            unavailable: The sentence `problem` returns: why settings cannot be changed here.
        """
        self.relay = relay
        self.unavailable = unavailable or "settings are changed on the server's machine"

    def problem(self) -> str | None:
        """Say why nothing can be changed from this client.

        Returns:
            Always the sentence given at creation.
        """
        return self.unavailable

    def sessions_problem(self) -> str | None:
        """Say why the sessions cannot be read or asked for: never through a relay.

        Returns:
            None.
        """
        return None

    def session_host(self) -> str:
        """Name the relay's server host.

        Returns:
            The host the relay named, or this machine's name if it named none.
        """
        return self.relay.hostname or socket.gethostname()

    def needs_password(self) -> bool:
        """Tell whether a password is asked for: never, the server token is the access.

        Returns:
            False.
        """
        return False

    async def _call(self, method: str, params: dict[str, Any], timeout_s: float) -> Any:
        """Send one request to the relay.

        Args:
            method: The relay request.
            params: Its params.
            timeout_s: How long the exchange may take.

        Returns:
            The result.

        Raises:
            AdminFailed: If the relay cannot be reached, refuses, or does not answer in time.
        """
        try:
            _hello, result = await relay_call(
                self.relay.address, self.relay.token, method, params, timeout_s
            )
        except RequestFailed as error:
            raise AdminFailed(str(error.error.get("message") or error)) from error
        except (OSError, ConnectionError) as error:
            raise AdminFailed(
                f"the relay {self.relay.address} cannot be reached: {error}"
            ) from error
        return result

    async def sessions(self, password: str | None) -> list[herd.SessionRow]:
        """List the sessions of the relay's server, newest first.

        Args:
            password: Ignored; the server token is the access.

        Returns:
            One row per session, with the relay's address of it as `address`.

        Raises:
            AdminFailed: If the relay cannot be asked.
        """
        result = await self._call("relay.sessions", {}, 15.0)
        rows = []
        for entry in (result or {}).get("sessions") or []:
            started = datetime.fromisoformat(str(entry["started_at"]))
            since = entry.get("state_since")
            session_id = str(entry["calu_session_id"])
            rows.append(
                herd.SessionRow(
                    id=session_id,
                    hostname=self.session_host(),
                    os_user=self.relay.os_user,
                    directory=str(entry.get("directory") or ""),
                    state=str(entry.get("state") or "unknown"),
                    state_since=datetime.fromisoformat(str(since)) if since else started,
                    started_at=started,
                    ended_at=None,
                    claude_session_id=entry.get("claude_session_id"),
                    open_escalations=int(entry.get("open_escalations") or 0),
                    address=self.relay.session_address(session_id),
                    name=str(entry.get("name") or ""),
                )
            )
        return rows

    async def served(self, password: str | None) -> list[Served]:
        """Name the relay's server and the directory it serves.

        Args:
            password: Ignored; the server token is the access.

        Returns:
            The one server, as the relay described it in `initialize`.
        """
        return [Served(self.session_host(), self.relay.os_user, self.relay.projects_directory)]

    async def rename_session(self, session_id: str, name: str, password: str | None) -> NoReturn:
        """Refuse: a name is stored in the database.

        Args:
            session_id: Ignored.
            name: Ignored.
            password: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)

    async def request_session(self, wanted: SessionRequest, password: str | None) -> str:
        """Ask the relay's server for a session and wait until it runs (`relay.start`).

        Side effect: the server files a `session_requests` row through the server token and
        starts the session.

        Args:
            wanted: The session; its host and user are the server's own, whatever it says.
            password: Ignored.

        Returns:
            The id of the started session's row.

        Raises:
            AdminFailed: If the server refused or failed to start it (the message is its
                reason), or the relay cannot be asked.
        """
        params = {
            "kind": wanted.kind,
            "directory": wanted.directory,
            "claude_session_id": wanted.claude_session_id,
            "fork_before_turn": wanted.fork_before_turn,
            "prompt": wanted.prompt,
            "model": wanted.model,
            "effort": wanted.effort,
            "project": wanted.project,
        }
        result = await self._call("relay.start", params, RELAY_START_TIMEOUT_S)
        return str((result or {}).get("calu_session_id") or "")

    async def save_instructions(
        self, name: str, text: str, password: str | None, base_version: int | None = None
    ) -> NoReturn:
        """Refuse: checker instructions are stored in the database.

        Args:
            name: Ignored.
            text: Ignored.
            password: Ignored.
            base_version: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)

    async def set_honesty_mode(
        self, mode: str, project: str | None, password: str | None
    ) -> NoReturn:
        """Refuse: `honesty.mode` is stored in the database.

        Args:
            mode: Ignored.
            project: Ignored.
            password: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)

    async def temporary_rules(self, session_id: str | None, password: str | None) -> NoReturn:
        """Refuse: the temporary rules are stored in the database.

        Args:
            session_id: Ignored.
            password: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)

    async def add_temporary_rule(
        self, arguments: str, session_id: str | None, password: str | None
    ) -> NoReturn:
        """Refuse: the temporary rules are stored in the database.

        Args:
            arguments: Ignored.
            session_id: Ignored.
            password: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)

    async def remove_temporary_rule(
        self, rule_id: int, session_id: str | None, password: str | None
    ) -> NoReturn:
        """Refuse: the temporary rules are stored in the database.

        Args:
            rule_id: Ignored.
            session_id: Ignored.
            password: Ignored.

        Raises:
            AdminFailed: Always, with `unavailable`.
        """
        raise AdminFailed(self.unavailable)
