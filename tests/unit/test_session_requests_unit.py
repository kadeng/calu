"""`calu.session_requests.wait_for` without a database: a claim that is never answered expires.

A fake `Db` answers the module's queries; no database is used.
"""

from typing import Any

import psycopg
import pytest

from calu import session_requests

pytestmark = pytest.mark.anyio

ROW = (
    5, "h", "u", "/p", "new", None, None, None, None, None, None, "", "claimed", None, "",
)  # fmt: skip
"""A claimed request as `SELECT {COLUMNS}` returns it."""


class FakeDb:
    """Answers `read` with a claimed row and records the functions called through `scalar`.

    `calls` lists the SQL of each `scalar` call.
    """

    def __init__(self, expire: bool | Exception = True) -> None:
        """Prepare the fake.

        Args:
            expire: What `calu_expire_session_request` does: returns the value, or raises it.
        """
        self.expire = expire
        self.calls: list[str] = []

    async def one(self, _sql: str, _params: Any = None) -> tuple:
        """Return the claimed row.

        Args:
            _sql: Ignored.
            _params: Ignored.

        Returns:
            The row.
        """
        return ROW

    async def scalar(self, sql: str, _params: Any = None) -> Any:
        """Record the call and answer the expiry.

        Args:
            sql: The statement.
            _params: Ignored.

        Returns:
            The configured answer.

        Raises:
            Exception: The configured exception.
        """
        self.calls.append(sql)
        if isinstance(self.expire, Exception):
            raise self.expire
        return self.expire


@pytest.mark.parametrize("expire", [True, False, psycopg.errors.UndefinedFunction("old schema")])
async def test_a_claim_that_is_not_answered_in_time_is_expired(
    monkeypatch: pytest.MonkeyPatch, expire: bool | Exception
):
    """A wait for a claim that nobody answers ends with `TimeoutError` after one expiry call.

    The result of the expiry (`True` or `False`) does not matter. An expiry that fails because
    the function is missing (an older schema) also ends the wait with `TimeoutError`.
    """
    monkeypatch.setattr(session_requests, "POLL_S", 0.0)
    db = FakeDb(expire)
    with pytest.raises(TimeoutError):
        await session_requests.wait_for(db, 5, claim_wait_s=1, start_wait_s=0.01)  # type: ignore[arg-type]
    assert db.calls == ["SELECT calu_expire_session_request(%s)"]


async def test_expire_reports_what_the_database_did():
    assert await session_requests.expire(FakeDb(True), 5) is True  # type: ignore[arg-type]
    assert await session_requests.expire(FakeDb(False), 5) is False  # type: ignore[arg-type]
