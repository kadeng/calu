"""Client tokens of a served session against the real database: revocation and decisions.

The unit tests cover the same behaviour with the in-memory store. These run the protocol
server on a `DatabaseSessionStore`, so that what `calu token revoke` writes, what the session
revokes itself when it ends (D108) and what `calu_client_decision` accepts are the real thing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from calu import herd
from calu.bus import Bus
from calu.claude import FakeRunner, FakeScript
from calu.db import Db, connect
from calu.protocol.client import RequestFailed
from calu.protocol.client import connect as attach
from calu.protocol.server import SessionServer
from calu.session import Session
from calu.setup import setup
from calu.store import DatabaseSessionStore
from calu.testing import fake_models
from calu.tokens import new_token, token_hash
from tests.unit.helpers import default_settings
from tests.unit.test_protocol_server import Events

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@dataclass
class Running:
    """A started session on the database, its protocol server and its printed token."""

    db: Db
    store: DatabaseSessionStore
    server: SessionServer
    session: Session
    token: str


@asynccontextmanager
async def running(
    db_url: str, workspace: Path, turns: list[list[dict[str, Any]]]
) -> AsyncGenerator[Running]:
    """Start a session as `calu run` does: server, session row, printed token recorded."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        bus = Bus()
        session = Session(
            workspace,
            default_settings(workspace),
            store,
            lambda: FakeRunner(FakeScript(turns=turns), workspace),
            bus.publish,
            checker_models=fake_models(),
            snapshot_root=workspace.parent / "snapshots",
        )
        token = new_token()
        async with anyio.create_task_group() as tasks:
            await session.start(tasks)
            server = SessionServer(session, bus, token_hash(token), tasks)
            tasks.start_soon(server.listen, "127.0.0.1", 0)
            with anyio.fail_after(10):
                await server.ready.wait()
            await store.set_address(server.address)
            await store.add_client_token(token_hash(token), server.address)
            try:
                yield Running(db, store, server, session, token)
            finally:
                await session.stop()
                tasks.cancel_scope.cancel()
    finally:
        await conn.close()


async def printed_row(run: Running) -> herd.TokenRow:
    """The `client_tokens` row of the printed token of the store's current session."""
    rows = await herd.list_tokens(run.db, run.store.session_id)
    assert len([row for row in rows if not row.label]) == 1
    return next(row for row in rows if not row.label)


async def test_revoking_the_printed_token_ends_it_for_new_and_open_connections(
    db_url: str, tmp_path: Path
) -> None:
    """`calu token revoke` on the printed token's row: `calu token list` says revoked, and
    the token must then really be dead, also for a client that is already connected."""
    async with running(db_url, tmp_path, [[{"text": "hi"}]]) as run:
        async with attach(run.server.address, run.token, "tmux scrollback", Events()) as thief:
            await thief.request("state.get")
            assert await herd.revoke_token(run.db, (await printed_row(run)).id) is True

            with pytest.raises(RequestFailed) as refused:
                await thief.request("shutdown")
            assert refused.value.code == -32001
            assert not run.server.shutdown.is_set()
            with anyio.fail_after(10):
                await thief.closed.wait()
            assert thief.close_code == 4001

        with pytest.raises(RequestFailed) as again:
            async with attach(run.server.address, run.token, "again", Events()):
                pass
        assert again.value.code == -32001


async def test_the_scheduled_check_finds_a_revoked_token_without_any_request(
    db_url: str, tmp_path: Path
) -> None:
    async with running(db_url, tmp_path, [[{"text": "hi"}]]) as run:
        token_id, minted = await herd.mint_token(run.db, run.store.session_id or "", "zed")
        async with attach(run.server.address, minted, "zed", Events()) as zed:
            await run.server.sweep()
            assert not zed.closed.is_set()
            await herd.revoke_token(run.db, token_id)
            await run.server.sweep()
            with anyio.fail_after(10):
                await zed.closed.wait()
            assert zed.close_code == 4001


async def test_a_minted_client_still_records_its_decision_after_the_printed_token_is_revoked(
    db_url: str, tmp_path: Path
) -> None:
    """The decision is checked against the deciding client's own token. With one token for
    all, revoking the printed token turned every later decision into "cancelled"."""
    turns = [[{"tool": "Bash", "input": {"command": "deploy # calu:escalate"}, "result": "ok"}]]
    async with running(db_url, tmp_path, turns) as run:
        _, minted = await herd.mint_token(run.db, run.store.session_id or "", "zed")
        await herd.revoke_token(run.db, (await printed_row(run)).id)
        events = Events()
        async with attach(run.server.address, minted, "zed", events) as zed:
            await zed.request("chat.send", {"text": "ship it"})
            request = (await events.wait_for("escalation.request"))[0]
            await zed.request(
                "escalation.decide",
                {"escalation_id": request["escalation_id"], "decision": "allow"},
            )
            await events.wait_for("turn.result")
        rows = await run.db.all("SELECT state, resolved_by FROM escalations")
        assert rows == [("allowed", "client:zed")]


async def test_a_new_session_in_the_same_process_keeps_its_clients_and_a_revocable_printed_token(
    db_url: str, tmp_path: Path
) -> None:
    """Ending a session revokes its tokens in the database (D108). The clients of the process
    stay, the printed token gets a row for the next session, and that row can be revoked."""
    async with running(db_url, tmp_path, [[{"text": "one"}], [{"text": "two"}]]) as run:
        first_session = run.store.session_id
        async with attach(run.server.address, run.token, "attach", Events()) as client:
            await run.server.sweep()
            await client.request("session.stop")
            await run.server.sweep()
            await client.request("session.start", {})
            assert run.store.session_id != first_session
            await run.server.sweep()
            assert not client.closed.is_set()
            row = await printed_row(run)  # recorded for the new session row
            assert not row.revoked

            await herd.revoke_token(run.db, row.id)
            await run.server.sweep()
            with anyio.fail_after(10):
                await client.closed.wait()
            assert client.close_code == 4001
