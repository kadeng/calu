"""Lost database connections: `Db` and `Notifier` reconnect, and statements wait meanwhile."""

import anyio
import psycopg
import pytest

from calu.db import ConnectionSource, Db, Dirty, Notifier, connect
from calu.db import connection as connection_module

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
    db.on_lost(lambda: events.append("lost"))
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


async def test_a_statement_that_timed_out_gets_a_fresh_connection(db_url: str) -> None:
    """sd2: one slow statement must not leave every later statement on a broken connection."""
    source = ConnectionSource(db_url)
    db = Db(await source.open(), timeout=0.2, source=source)
    try:
        pid = await db.scalar("SELECT pg_backend_pid()")
        with pytest.raises(TimeoutError):
            await db.execute("SELECT pg_sleep(5)")
        assert await db.scalar("SELECT 1") == 1
        assert await db.scalar("SELECT pg_backend_pid()") != pid
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
