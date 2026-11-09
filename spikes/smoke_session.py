"""Manual end-to-end check of a session with the real Claude Code CLI and fake checker models.

What it covers that the automated tests do not: the Claude Agent SDK, the PreToolUse hook,
`can_use_tool` (the wait for a human) and the sandbox settings, all against a real `claude`.
The checkers are the rule-based fakes (`calu.testing`), so no checker API key is needed.

    uv run python spikes/smoke_session.py <project dir> [-p PROMPT] [--resolve allow|deny]
                                          [--sandbox] [--model haiku]

It creates a throwaway database on the test server, runs `calu setup` in it, runs one session
and prints what the database holds afterwards; the database is dropped at the end. With
`--resolve` a task plays the operator: it waits for the first escalation and resolves it like
`calu resolve` would, which is the only way an unattended run can continue.

Needs: `docker compose up -d caludb --wait`, a logged-in `claude`, and a project directory that
Claude Code may work in. Without `--sandbox` the sandbox is off (it needs `bwrap` and `socat`),
so the commands Claude runs are **not** isolated: use a throwaway directory.

Result on 2026-11-09 (CLI 2.1.319, haiku): `sudo -n ls /root` matched the deny rule
`^(sudo|su|doas)( |$)`, the session went to state `escalated` and waited; after
`calu_resolve_escalation(1, 'denied', ...)` it continued within a second and Claude reported the
denial with the note. An allow-listed `ls` ran without a model call; a `Write` went to the edit
checker. See `HANDOFF.md`.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

import anyio
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu import herd
from calu.config import SETTINGS_KEY
from calu.db import Db, connect
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.testing import fake_models

TEST_SERVER = "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu"
REPORT = (
    "SELECT state, model, claude_session_id FROM sessions",
    "SELECT seq, tool_name, decision, rules_outcome, left(reason, 60) FROM steps ORDER BY seq",
    "SELECT checker_name, checker_version, decision, duration_ms IS NOT NULL FROM checker_runs"
    " ORDER BY id",
    "SELECT state, resolved_by, note FROM escalations ORDER BY id",
    "SELECT number, user_inputs, left(answer, 60) FROM turns ORDER BY number",
)


async def operator(url: str, decision: str, wait_s: float) -> None:
    """Wait for the first escalation and resolve it, as `calu resolve` would."""
    owner = await connect(url)
    db = Db(owner)
    try:
        with anyio.fail_after(wait_s):
            while True:
                rows = await herd.escalations(db)
                if rows:
                    break
                await anyio.sleep(0.2)
        print(f"\n=== operator: {decision} escalation {rows[0].id}: {rows[0].reason[:120]}")
        await herd.resolve(db, rows[0].id, decision, f"{decision} by the smoke test")
    finally:
        await owner.close()


async def report(url: str) -> None:
    """Print the rows the session wrote."""
    owner = await connect(url)
    try:
        for query in REPORT:
            cur = await owner.execute(query.encode())
            print(f"--- {query}")
            for row in await cur.fetchall():
                print("   ", row)
    finally:
        await owner.close()


async def main(args: argparse.Namespace) -> int:
    """Create a database, run one session in it, report, and drop it again."""
    name = f"calu_smoke_{uuid.uuid4().hex[:8]}"
    admin = await psycopg.AsyncConnection.connect(TEST_SERVER, autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(TEST_SERVER)
    params["dbname"] = name
    url = make_conninfo("", **params)
    settings = {
        "claude": {
            "sandbox": {"enabled": args.sandbox},
            "model": args.model,
            "stream_partial": False,
        },
        "context": {"narrate": False},
    }
    try:
        owner = await connect(url)
        try:
            _, session_url = await setup(owner, url, "test:model")
            await owner.execute(
                "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
                (SETTINGS_KEY, json.dumps(settings)),
            )
        finally:
            await owner.close()
        codes: list[int] = []

        async def run() -> None:
            """Run the session and keep its exit code."""
            codes.append(
                await run_session(
                    session_url,
                    RunOptions(
                        workspace=Path(args.directory).resolve(),
                        prompt=args.prompt,
                        interactive=False,
                    ),
                    checker_models=fake_models(),
                )
            )

        async with anyio.create_task_group() as tg:
            if args.resolve:
                tg.start_soon(operator, url, args.resolve, args.timeout)
            tg.start_soon(run)
        print(f"\n=== run_session returned {codes[0]}")
        await report(url)
        return codes[0]
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await admin.close()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", help="throwaway project directory for Claude Code")
parser.add_argument(
    "-p",
    "--prompt",
    default="Run the command `sudo -n ls /root` with the Bash tool, then tell me in one "
    "sentence what happened. Do not try anything else afterwards.",
)
parser.add_argument("--resolve", choices=["allow", "deny"], help="play the operator")
parser.add_argument("--sandbox", action="store_true", help="switch the sandbox on (needs socat)")
parser.add_argument("--model", default="haiku")
parser.add_argument("--timeout", type=float, default=240, help="how long the operator waits")
sys.exit(anyio.run(main, parser.parse_args()))
