"""Lost database connections: `Db` and `Notifier` reconnect, and statements wait meanwhile."""

import anyio
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from calu.db import ConnectionSource, Db, Dirty, LoginRefused, Notifier, connect
from calu.db import connection as connection_module
from calu.db.connection import REFUSED_ATTEMPTS, refuses_for_good
from calu.setup import enable_session_login, setup

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def short_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect quickly, so the tests do not sit in the backoff."""
    monkeypatch.setattr(connection_module, "RECONNECT_FIRST_S", 0.02)
    monkeypatch.setattr("calu.db.notify.RECONNECT_FIRST_S", 0.02)


async def kill(db_url: str, pid: int) -> None:
    """End one server process, as a restart of the server would end all of them."""
    owner = await connect(db_url)
    try:
        await owner.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        await owner.close()
    await anyio.sleep(0.2)  # let the server close the socket


class FlakySource(ConnectionSource):
    """A source whose first `failures` attempts fail, as while the server is down."""

    def __init__(self, url: str, failures: int) -> None:
        super().__init__(url)
        self.failures = failures
        self.attempts = 0

    async def open(self) -> psycopg.AsyncConnection:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise psycopg.OperationalError("connection refused")
        return await super().open()


async def test_a_connection_asks_for_keepalives_and_a_connect_timeout(db_url: str) -> None:
    """cs3: a peer that vanished silently must be noticed within about a minute."""
    conn = await connect(db_url)
    try:
        params = conn.info.get_parameters()
        assert params["keepalives_idle"] == "30"
        assert params["keepalives_interval"] == "10"
        assert params["keepalives_count"] == "3"
        assert params["connect_timeout"] == "10"
        assert params["tcp_user_timeout"] == "60000"
    finally:
        await conn.close()
    # A value in the connection string wins over the default.
    conn = await connect(db_url + " keepalives_idle=5")
    try:
        assert conn.info.get_parameters()["keepalives_idle"] == "5"
    finally:
        await conn.close()


async def test_the_source_does_not_show_the_connection_string(db_url: str) -> None:
    source = ConnectionSource(bytearray(db_url.encode()))
    assert "calu-dev" not in repr(source) and "calu-dev" not in str(source)


async def test_without_a_source_a_lost_connection_stays_lost(db_url: str) -> None:
    db = Db(await connect(db_url))
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        await kill(db_url, pid)
        with pytest.raises(psycopg.OperationalError):
            await db.scalar("SELECT 1")
    finally:
        await db.aclose()


async def test_a_lost_connection_is_replaced_before_the_next_statement(db_url: str) -> None:
    """sd1: after a restart of the server no statement is lost and the session is bound again."""
    source = ConnectionSource(db_url)
    db = Db(await source.open(), source=source)
    events: list[str] = []
    db.on_lost(lambda reason: events.append("lost") if "lost" in reason else None)
    db.on_restored(lambda: events.append("restored"))

    async def bind(conn: psycopg.AsyncConnection) -> None:
        await conn.execute("SELECT set_config('calu.test', 'bound', false)")

    db.on_connect(bind)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        await kill(db_url, pid)
        # The very next statement works: the dead connection is noticed before it is used.
        assert await db.scalar("SELECT current_setting('calu.test', true)") == "bound"
        assert await db.scalar("SELECT pg_backend_pid()") != pid
        assert events == ["lost", "restored"]
        async with db.exclusive() as conn:
            assert not conn.closed
    finally:
        await db.aclose()


async def test_statements_wait_while_the_database_is_away(db_url: str) -> None:
    """sd1: an outage delays a statement; it does not fail it (the gate waits: fail closed)."""
    source = FlakySource(db_url, failures=0)
    db = Db(await source.open(), source=source)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        source.failures = source.attempts + 4
        await kill(db_url, pid)
        answers: list[int] = []

        async def ask() -> None:
            answers.append(await db.scalar("SELECT 41 + 1"))

        async with anyio.create_task_group() as tg:
            tg.start_soon(ask)
            await anyio.sleep(0.05)
            assert answers == [] and db.waiting
            with anyio.fail_after(10):
                while not answers:
                    await anyio.sleep(0.01)
        assert answers == [42] and not db.waiting
        assert source.attempts >= 6
    finally:
        await db.aclose()


async def test_the_end_of_a_process_does_not_wait_for_a_database_that_stays_away(
    db_url: str,
) -> None:
    source = FlakySource(db_url, failures=0)
    db = Db(await source.open(), source=source)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        source.failures = 10**9
        await kill(db_url, pid)
        db.limit_reconnect(0.1)
        with anyio.fail_after(5):
            with pytest.raises(psycopg.OperationalError, match="lost"):
                await db.execute("SELECT 1")
            # Later statements fail at once instead of each waiting again.
            before = source.attempts
            with pytest.raises(psycopg.OperationalError):
                await db.execute("SELECT 1")
            assert source.attempts == before
    finally:
        await db.aclose()


async def test_a_stopping_process_does_not_sit_out_a_long_pause_between_attempts(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`limit_reconnect` bounds the wait of a statement that was already waiting, however long
    the pause it is in and however slow the attempt after it."""
    monkeypatch.setattr(connection_module, "RECONNECT_FIRST_S", 30.0)
    monkeypatch.setattr(connection_module, "LAST_ATTEMPT_S", 0.3)

    class SlowSource(FlakySource):
        async def open(self) -> psycopg.AsyncConnection:
            if self.attempts >= 1 and self.attempts >= self.failures:
                await anyio.sleep(60)  # a server that does not answer
            return await super().open()

    source = SlowSource(db_url, failures=0)
    db = Db(await source.open(), source=source)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        source.failures = 2
        await kill(db_url, pid)
        errors: list[BaseException] = []

        async def ask() -> None:
            try:
                await db.execute("SELECT 1")
            except psycopg.OperationalError as exc:
                errors.append(exc)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(ask)
                await anyio.sleep(0.2)  # the statement is in its 30 s pause now
                assert db.waiting and not errors
                db.limit_reconnect(0.2)
        assert len(errors) == 1 and "lost" in str(errors[0])
    finally:
        await db.aclose()


async def session_login(db_url: str) -> str:
    """Migrate the test database and return a working `calu_session` connection string."""
    owner = await connect(db_url)
    try:
        _, session_url = await setup(owner, db_url, "test:model")
    finally:
        await owner.close()
    return session_url


async def replace_session_credential(db_url: str) -> None:
    """Give `calu_session` a new password, as every `calu setup` does."""
    owner = await connect(db_url)
    try:
        await enable_session_login(owner, db_url)
    finally:
        await owner.close()


def test_which_refusals_of_a_login_do_not_heal() -> None:
    for code in ("28P01", "28000", "3D000", "42501"):
        assert refuses_for_good(code), code
    # The server is starting or stopping, is full, or gave no answer at all: these pass.
    for code in ("57P03", "57P01", "53300", "08006", None):
        assert not refuses_for_good(code), code


async def test_a_refused_login_ends_the_reconnecting_and_says_why(db_url: str) -> None:
    """An unattended session must not wait for ever for a login that will not work again.

    The session credential was replaced while the session ran. When its connection is lost,
    the wrapper tries a bounded number of times and then raises an error that names the cause.
    """
    source = ConnectionSource(await session_login(db_url))
    db = Db(await source.open(), source=source)
    refused: list[LoginRefused] = []
    db.on_refused(refused.append)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        await replace_session_credential(db_url)
        assert await db.scalar("SELECT 1") == 1  # the open connection is not affected
        await kill(db_url, pid)
        with anyio.fail_after(15):
            with pytest.raises(LoginRefused, match="password authentication failed") as caught:
                await db.scalar("SELECT 1")
        assert caught.value.code == "28P01"
        assert len(refused) == 1 and not db.waiting
        # Later statements fail at once, with the same cause, and nothing is called twice.
        with anyio.fail_after(1):
            with pytest.raises(LoginRefused, match="password authentication failed"):
                await db.execute("SELECT 1")
        assert len(refused) == 1
    finally:
        await db.aclose()


async def test_a_database_that_was_removed_ends_the_reconnecting(db_url: str) -> None:
    source = ConnectionSource(db_url)
    db = Db(await source.open(), source=source)
    try:
        await db.scalar("SELECT 1")
        name = await db.scalar("SELECT current_database()")
        admin = await connect(make_conninfo(db_url, dbname="postgres"))
        try:
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
        finally:
            await admin.close()
        await anyio.sleep(0.2)
        with anyio.fail_after(15):
            with pytest.raises(LoginRefused) as caught:
                await db.scalar("SELECT 1")
        assert caught.value.code == "3D000"
    finally:
        await db.aclose()


async def test_a_refusal_that_passes_within_the_attempts_is_survived(db_url: str) -> None:
    """A login refused for a moment (a server being reconfigured) does not end the session."""

    class RefusingSource(ConnectionSource):
        refusals = REFUSED_ATTEMPTS - 1

        async def open(self) -> psycopg.AsyncConnection:
            if self.refusals > 0:
                self.refusals -= 1
                raise LoginRefused("password authentication failed", "28P01")
            return await super().open()

    plain = ConnectionSource(db_url)
    source = RefusingSource(db_url)
    db = Db(await plain.open(), source=source)
    try:
        await kill(db_url, await db.scalar("SELECT pg_backend_pid()"))
        with anyio.fail_after(10):
            assert await db.scalar("SELECT 41 + 1") == 42
        assert source.refusals == 0
    finally:
        await db.aclose()


async def test_the_notifier_stops_when_its_login_is_refused_for_good(db_url: str) -> None:
    source = ConnectionSource(await session_login(db_url))
    listener = await source.open()
    notifier = Notifier(listener, source=source)
    notifier.on("config_changed", lambda _payload: None)
    listening: list[bool] = []
    refused: list[LoginRefused] = []
    notifier.on_listening(lambda: listening.append(True))
    notifier.on_refused(refused.append)
    try:
        with anyio.fail_after(20):
            async with anyio.create_task_group() as tg:
                tg.start_soon(notifier.run)
                while not listening:
                    await anyio.sleep(0.01)
                await replace_session_credential(db_url)
                await kill(db_url, listener.info.backend_pid)
                # `run` returns by itself: the task group ends without being cancelled.
        assert len(refused) == 1 and refused[0].code == "28P01"
        assert not notifier.listening
    finally:
        await notifier.aclose()


async def test_a_statement_that_timed_out_gets_a_fresh_connection(db_url: str) -> None:
    """sd2: one slow statement must not leave every later statement on a broken connection."""
    source = ConnectionSource(db_url)
    db = Db(await source.open(), timeout=0.2, source=source)
    reasons: list[str] = []
    db.on_lost(reasons.append)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        with pytest.raises(TimeoutError):
            await db.execute("SELECT pg_sleep(5)")
        assert await db.scalar("SELECT 1") == 1
        assert await db.scalar("SELECT pg_backend_pid()") != pid
        # It is reported as what it is, not as a lost connection.
        assert len(reasons) == 1 and "did not finish in time" in reasons[0]
    finally:
        await db.aclose()


async def test_the_notifier_listens_again_after_its_connection_was_lost(db_url: str) -> None:
    """cs3: losing the idle listening connection must not end the run or lose the channel."""
    source = ConnectionSource(db_url)
    listener = await source.open()
    notifier = Notifier(listener, source=source)
    seen: list[dict] = []
    listening: list[bool] = []
    lost: list[BaseException] = []
    notifier.on("config_changed", lambda payload: seen.append(dict(payload)))
    notifier.on_listening(lambda: listening.append(True))
    notifier.on_lost(lost.append)
    owner = await connect(db_url)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(notifier.run)
            with anyio.fail_after(10):
                while not listening:
                    await anyio.sleep(0.01)
                await kill(db_url, listener.info.backend_pid)
                while len(listening) < 2:
                    await anyio.sleep(0.01)
                assert len(lost) == 1 and notifier.listening
                await owner.execute("""SELECT pg_notify('config_changed', '{"table": "rules"}')""")
                while not seen:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
        assert seen == [{"table": "rules"}]
    finally:
        await owner.close()
        await notifier.aclose()


async def test_a_notifier_without_a_source_raises_when_its_connection_breaks(db_url: str) -> None:
    listener = await connect(db_url)
    notifier = Notifier(listener)
    notifier.on("config_changed", lambda _payload: None)
    try:
        with pytest.raises(psycopg.OperationalError):
            async with anyio.create_task_group() as tg:
                tg.start_soon(notifier.run)
                await anyio.sleep(0.2)
                await kill(db_url, listener.info.backend_pid)
                with anyio.fail_after(10):
                    await anyio.sleep_forever()
    except* psycopg.OperationalError:
        pass
    finally:
        await listener.close()


async def test_a_hint_is_weaker_than_a_notification() -> None:
    dirty = Dirty()
    dirty.hint()
    assert dirty.is_set() and await dirty.wait() is False
    dirty.hint()
    dirty.set()
    assert await dirty.wait() is True
    assert not dirty.is_set()
