"""Manual end-to-end check of `calu serve` with the real Claude Code CLI (D208).

What it covers that the automated tests do not: several real Claude Code processes under one
calu process at the same time, and a fork that is started through the server.

    uv run python spikes/smoke_serve.py <directory A> <directory B> [--model haiku]

It creates a throwaway database on the test server, starts a `calu.serve.Server` in this
process (the Agent SDK runner, rule-based fake checkers, the sandbox off) and then plays the
requester, as `calu start` does:

    1. a new session in A (told a word to remember) and a new session in B, at the same time
    2. a fork of A's conversation, asked for the word
    3. `shutdown` to each session; the server is stopped

It prints each answer and what the database holds. The database is dropped at the end; an
interrupted run leaves it behind (see `spikes/smoke_session.py`).

Needs: `docker compose up -d caludb --wait`, a logged-in `claude` (or `ANTHROPIC_API_KEY`),
and two throwaway directories: the commands Claude runs are not isolated.
"""

import argparse
import json
import socket
import uuid
from pathlib import Path

import anyio
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu import herd, session_requests
from calu.config import SETTINGS_KEY
from calu.db import ConnectionSource, Db, connect
from calu.protocol.client import connect as connect_client
from calu.serve import Server, os_user
from calu.session_requests import SessionRequest
from calu.setup import setup
from calu.testing import fake_models

TEST_SERVER = "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu"


async def converse(db: Db, wanted: SessionRequest, name: str) -> tuple[str, str, str]:
    """Ask the server for a session, wait for the answer to its prompt, and shut it down.

    Args:
        db: The requester's connection.
        wanted: The request; it has a prompt.
        name: What to call the session in the output.

    Returns:
        Claude's answer, the Claude session id and the session's address.
    """
    done = await session_requests.wait_for(db, await session_requests.file(db, wanted))
    if done.state != "started" or done.session_id is None:
        raise SystemExit(f"{name}: not started: {done.message}")
    row = await db.one(
        "SELECT address, claude_session_id FROM sessions WHERE id = %s::uuid", (done.session_id,)
    )
    assert row is not None
    address, claude_id = row
    _, token = await herd.mint_token(db, done.session_id, "smoke_serve")
    texts: list[str] = []
    finished = anyio.Event()

    async def on_event(method: str, params: dict) -> None:
        """Collect Claude's text and note the end of the turn.

        Args:
            method: The event's name.
            params: Its content.
        """
        if method == "assistant.text":
            texts.append(params.get("text", ""))
        if method == "turn.result":
            finished.set()

    async with connect_client(address, token, "smoke_serve", on_event) as client:
        with anyio.fail_after(180):
            await finished.wait()
        print(f"=== {name}: {claude_id} at {address}: {' '.join(texts)!r}", flush=True)
        await client.request("shutdown")
    return " ".join(texts), claude_id, address


async def main(args: argparse.Namespace) -> None:
    """Create a database, run the server and the three sessions, report, drop the database.

    Args:
        args: The parsed command line.
    """
    name = f"calu_smoke_{uuid.uuid4().hex[:8]}"
    admin = await psycopg.AsyncConnection.connect(TEST_SERVER, autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(TEST_SERVER)
    params["dbname"] = name
    url = make_conninfo("", **params)
    settings = {
        "claude": {"sandbox": {"enabled": False}, "model": args.model, "stream_partial": False},
        "context": {"narrate": False},
    }
    host, user = socket.gethostname(), os_user()
    a, b = (str(Path(d).resolve()) for d in (args.a, args.b))
    try:
        owner = await connect(url)
        try:
            _, session_url = await setup(owner, url, "test:model")
            await owner.execute(
                "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
                (SETTINGS_KEY, json.dumps(settings)),
            )
            db = Db(owner)
            server = Server(ConnectionSource(session_url), set(), checker_models=fake_models())
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run)
                await server.listening.wait()
                results: dict[str, tuple[str, str, str]] = {}

                async def one(key: str, wanted: SessionRequest) -> None:
                    """Run one conversation and keep its result.

                    Args:
                        key: The name of the session.
                        wanted: The request.
                    """
                    results[key] = await converse(db, wanted, key)

                async with anyio.create_task_group() as both:
                    both.start_soon(
                        one,
                        "A",
                        SessionRequest(
                            hostname=host,
                            os_user=user,
                            directory=a,
                            prompt="Remember the word HERON. Answer with the single word ALPHA.",
                        ),
                    )
                    both.start_soon(
                        one,
                        "B",
                        SessionRequest(
                            hostname=host,
                            os_user=user,
                            directory=b,
                            prompt="Answer with the single word BRAVO.",
                        ),
                    )
                await one(
                    "fork of A",
                    SessionRequest(
                        hostname=host,
                        os_user=user,
                        directory=a,
                        kind="fork",
                        claude_session_id=results["A"][1],
                        prompt="Which word did I ask you to remember? Answer with that word.",
                    ),
                )
                with anyio.fail_after(30):
                    while server.sessions:
                        await anyio.sleep(0.1)
                tg.cancel_scope.cancel()
            for row in await db.all(
                "SELECT directory, state, claude_session_id, pid FROM sessions ORDER BY started_at"
            ):
                print("   sessions:", row)
            for row in await db.all(
                "SELECT id, kind, state, message FROM session_requests ORDER BY id"
            ):
                print("   requests:", row)
        finally:
            await owner.close()
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await admin.close()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("a", help="throwaway project directory of the first session")
parser.add_argument("b", help="throwaway project directory of the second session")
parser.add_argument("--model", default="haiku")

if __name__ == "__main__":
    anyio.run(main, parser.parse_args())
