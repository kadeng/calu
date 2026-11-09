"""Does `calu run --fork-before-turn` really branch a conversation calu recorded? (PLAN.md D36)

`spike_fork_session.py` showed that the Agent SDK can fork at an earlier point. This checks
calu's own path end to end with the real CLI: the uuids Claude Code replays are recorded with
each turn, the fork point is found again across the `sessions` rows of one conversation (a
resumed run opens a new row), and the forked session does not know the turn that was dropped.

    uv run python spikes/spike_calu_fork.py <throwaway project dir>

Three runs against a throwaway database, all with the rule-based fake checkers (no API key):

    1. "create alpha.txt"              -> turn 1
    2. --resume                        -> turn 2, "create beta.txt", in a second sessions row
    3. --resume --fork-before-turn 2   -> asks which files it created; must say alpha only

Needs `docker compose up -d caludb --wait`, a logged-in `claude`, and a directory Claude may
write in. The sandbox is off, so use a throwaway directory.
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

from calu.config import SETTINGS_KEY
from calu.db import Db, connect
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.testing import fake_models

TEST_SERVER = "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu"
FIRST = "Create a file alpha.txt with the word alpha in it. Then say only: done alpha."
SECOND = "Create a file beta.txt with the word beta in it. Then say only: done beta."
ASK = (
    "Which files have you created in this conversation? Answer with their names only. Answer "
    "from our conversation, do not look at the file system."
)


async def one_run(session_url: str, workspace: Path, prompt: str, **extra: object) -> None:
    """Run one calu session to its end.

    Args:
        session_url: Connection string of the session role.
        workspace: The project directory.
        prompt: The message to send.
        **extra: Further `RunOptions` fields, e.g. `resume_id` or `fork_before_turn`.
    """
    code = await run_session(
        session_url,
        RunOptions(workspace=workspace, prompt=prompt, interactive=False, **extra),  # type: ignore[arg-type]
        checker_models=fake_models(),
    )
    print(f"  run_session returned {code}")


async def main(args: argparse.Namespace) -> int:
    """Record two turns, fork before the second, and report what the fork knows.

    Returns:
        0 if the fork knew the kept turn and not the dropped one, 1 otherwise.
    """
    name = f"calu_fork_{uuid.uuid4().hex[:8]}"
    admin = await psycopg.AsyncConnection.connect(TEST_SERVER, autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(TEST_SERVER)
    params["dbname"] = name
    url = make_conninfo("", **params)
    workspace = Path(args.directory).resolve()
    try:
        owner = await connect(url)
        try:
            _, session_url = await setup(owner, url, "test:model")
            await owner.execute(
                "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
                (
                    SETTINGS_KEY,
                    json.dumps(
                        {
                            "claude": {
                                "sandbox": {"enabled": False},
                                "model": args.model,
                                "stream_partial": False,
                            },
                            "context": {"narrate": False},
                        }
                    ),
                ),
            )
        finally:
            await owner.close()

        print("=== turn 1")
        await one_run(session_url, workspace, FIRST)
        owner = await connect(url)
        db = Db(owner)
        claude_id = str((await db.one("SELECT claude_session_id FROM sessions") or [""])[0])
        print(f"  claude session {claude_id}")

        print("=== turn 2 (a second run of the same conversation)")
        await owner.close()
        await one_run(session_url, workspace, SECOND, resume_id=claude_id)

        owner = await connect(url)
        db = Db(owner)
        print("\n=== what calu recorded")
        for row in await db.all(
            "SELECT t.number, left(t.user_inputs::text, 40), t.prompt_uuid, t.last_entry_uuid,"
            " t.session_id::text FROM turns t ORDER BY t.number"
        ):
            prompt_uuid = str(row[2] or "-")[:8]
            last = str(row[3] or "-")[:8]
            print(f"  turn {row[0]}  prompt {prompt_uuid}  last entry {last}  row {row[4][:8]}")
        from calu import herd

        point = await herd.fork_point(db, claude_id, 2)
        print(f"  fork point for turn 2: {point}")
        await owner.close()
        if point is None:
            print("\nverdict: INCONCLUSIVE — calu found no fork point for turn 2")
            return 1

        print("\n=== the fork: keep turn 1, drop turn 2")
        (workspace / "beta.txt").unlink(missing_ok=True)
        await one_run(session_url, workspace, ASK, resume_id=claude_id, fork_before_turn=2)

        owner = await connect(url)
        db = Db(owner)
        rows = await db.all(
            "SELECT s.claude_session_id, left(t.answer, 200) FROM turns t"
            " JOIN sessions s ON s.id = t.session_id ORDER BY s.started_at DESC, t.number DESC"
            " LIMIT 1"
        )
        await owner.close()
        answer = str(rows[0][1]) if rows else ""
        forked_id = str(rows[0][0]) if rows else ""
        print(f"\n  the fork answered: {answer.strip()[:200]!r}")
        print(f"  its session id differs from the original: {forked_id != claude_id}")
        knows_alpha = "alpha" in answer.lower()
        knows_beta = "beta" in answer.lower()
        print(f"  remembers alpha: {knows_alpha}   remembers beta: {knows_beta}")
        good = knows_alpha and not knows_beta
        print(f"\nverdict: {'calu forks a conversation' if good else 'INCONCLUSIVE'}")
        return 0 if good else 1
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        await admin.close()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", help="throwaway project directory for Claude Code")
parser.add_argument("--model", default="haiku")
sys.exit(anyio.run(main, parser.parse_args()))
