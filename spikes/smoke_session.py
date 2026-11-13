"""Manual end-to-end check of a session with the real Claude Code CLI and fake checker models.

What it covers that the automated tests do not: the Claude Agent SDK, the PreToolUse hook, the
PostToolUse hook for Bash (`--changes`), `can_use_tool` (the wait for a human) and the sandbox
settings, all against a real `claude`.
The checkers are the rule-based fakes (`calu.testing`), so no checker API key is needed.

    uv run python spikes/smoke_session.py <project dir> [-p PROMPT] [--resolve allow|deny]
                                          [--sandbox] [--model haiku]
                                          [--checker-model MODEL|auto]
                                          [--honesty track|enforce] [--trust-head]

It creates a throwaway database on the test server, runs `calu setup` in it, runs one session
and prints what the database holds afterwards; the database is dropped at the end -- but a run
that is **interrupted** (Ctrl-C, a timeout around the command, a crash of the interpreter)
leaves it behind, so they collect. `docker compose exec caludb psql -U calu_owner -d calu -c
"SELECT datname FROM pg_database WHERE datname LIKE 'calu_smoke%'"` lists them, and
`DROP DATABASE <name> WITH (FORCE)` removes one; five had accumulated by the evening of
2026-11-10, all from interrupted runs. With
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
import os
import sys
import uuid
from pathlib import Path

import anyio
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from calu import asuser, herd
from calu.config import SETTINGS_KEY, load_secrets
from calu.db import Db, connect
from calu.model_choice import detect_model
from calu.run import RunOptions, run_session
from calu.setup import setup
from calu.store.trust import certify_commit
from calu.testing import fake_models

TEST_SERVER = "postgresql://calu_owner:calu-dev@127.0.0.1:55432/calu"
REPORT = (
    "SELECT state, model, claude_session_id, address FROM sessions",
    "SELECT seq, tool_name, decision, rules_outcome, left(reason, 60) FROM steps ORDER BY seq",
    "SELECT checker_name, checker_version, decision, duration_ms IS NOT NULL FROM checker_runs"
    " ORDER BY id",
    "SELECT state, resolved_by, note FROM escalations ORDER BY id",
    # What a human sees for an escalation: the tool, its input and the reason (PLAN.md D30).
    "SELECT reason, context->>'tool_name', context->'tool_input', left(coalesce("
    "context->>'rendered', ''), 200) FROM escalations ORDER BY id",
    "SELECT seq, tool_name, snapshot_before IS NOT NULL AS snapshotted, wrote FROM steps"
    " WHERE wrote IS NOT NULL ORDER BY seq",
    "SELECT number, user_inputs, left(answer, 60) FROM turns ORDER BY number",
    # Code honesty (--honesty): what was certified, by which source.
    "SELECT left(commit_id, 12), method, certified_by, note FROM trusted_commits"
    " ORDER BY certified_at",
    "SELECT left(content_sha256, 12), path, method, certified_by FROM trusted_files"
    " ORDER BY certified_at",
)


async def operator(url: str, decision: str, wait_s: float) -> None:
    """Wait for the first escalation and resolve it, as `calu resolve` would.

    It returns without resolving anything when no escalation is opened within `wait_s`.
    """
    owner = await connect(url)
    db = Db(owner)
    try:
        rows = []
        with anyio.move_on_after(wait_s):
            while not rows:
                rows = await herd.escalations(db)
                if not rows:
                    await anyio.sleep(0.2)
        if not rows:
            print(f"\n=== operator: no escalation within {wait_s:.0f} s")
            return
        print(f"\n=== operator: {decision} escalation {rows[0].id}: {rows[0].reason[:400]}")
        await herd.resolve(db, rows[0].id, decision, f"{decision} by the smoke test")
    finally:
        await owner.close()


async def trust_head(db: Db, directory: Path) -> None:
    """Certify HEAD of the project directory as a person would (`calu honesty trust HEAD`).

    Args:
        db: The owner's connection to the throwaway database.
        directory: The project directory, a git repository with at least one commit.
    """
    done = await anyio.run_process(["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=directory)
    ids = done.stdout.decode().split()
    await certify_commit(db, ids[0], ids[1], "user", "smoke test", "certified before the run")
    print(f"=== trusted commit {ids[0][:12]} (source user)")


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
            "sandbox": {"enabled": args.sandbox, "allowed_domains": list(args.allow)},
            "model": args.model,
            "stream_partial": False,
        },
        "context": {"narrate": False},
        "changes": {"snapshot": args.changes, "review": args.changes},
        "honesty": {"mode": args.honesty, "reviewer_model": args.reviewer_model},
    }
    try:
        owner = await connect(url)
        try:
            checker_model = "test:model"
            if args.checker_model:
                # Real checkers: the model calu would choose itself, or the one given (D129).
                load_secrets()
                if args.checker_model == "auto":
                    choice = detect_model(os.environ)
                    if choice is None:
                        raise SystemExit("--checker-model auto found no provider credentials")
                    checker_model, effort = choice.model, choice.effort
                    print(f"=== checkers on {checker_model} ({choice.provider}: {choice.reason})")
                else:
                    checker_model, effort = args.checker_model, None
                for role in ("command_checker", "edit_checker", "changeset_checker"):
                    settings[role] = {"model": checker_model}
                    if effort:
                        settings[role]["effort"] = effort
            _, session_url = await setup(owner, url, checker_model)
            await owner.execute(
                "INSERT INTO config (scope, scope_id, key, value) VALUES ('global', '', %s, %s)",
                (SETTINGS_KEY, json.dumps(settings)),
            )
            if args.trust_head:
                await trust_head(Db(owner), Path(args.directory).resolve())
        finally:
            await owner.close()
        codes: list[int] = []
        run_as = None
        if args.user:
            # calu as root, Claude Code and calu's work in the project as that user (D206).
            run_as = asuser.resolve(args.user)
            asuser.prepare(run_as)

        async def run() -> None:
            """Run the session and keep its exit code."""
            codes.append(
                await run_session(
                    session_url,
                    RunOptions(
                        workspace=Path(args.directory).resolve(),
                        prompt=args.prompt,
                        resume_id=args.resume,
                        fork_id=args.fork,
                        interactive=args.stay,
                        show_thinking=args.stay,
                        socket_path=Path(args.socket) if args.socket else None,
                        run_as=run_as,
                    ),
                    checker_models=None if args.checker_model else fake_models(),
                )
            )

        async with anyio.create_task_group() as tg:

            async def run_then_stop() -> None:
                """Run the session, then stop an operator that is still waiting."""
                await run()
                tg.cancel_scope.cancel()

            if args.resolve:
                tg.start_soon(operator, url, args.resolve, args.timeout)
            tg.start_soon(run_then_stop)
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
parser.add_argument(
    "--allow",
    action="append",
    default=[],
    metavar="DOMAIN",
    help="a domain the sandboxed commands may reach (repeatable); with --sandbox this is the"
    " allow list of the 'proxy' network policy, so everything else is blocked",
)
parser.add_argument(
    "--changes",
    action="store_true",
    help="measure and review what a command writes (PLAN.md D31): snapshot and review on",
)
parser.add_argument("--model", default="haiku")
parser.add_argument(
    "--user",
    help="as root: the user Claude Code runs as, a name or UID[:GID] (D206); see "
    "`spikes/smoke_privileged.sh`",
)
parser.add_argument(
    "--checker-model",
    metavar="MODEL|auto",
    help="real checkers instead of the rule-based fakes: a model string, or `auto` for the one "
    "calu would choose from the credentials found (D129). Costs real model calls.",
)
parser.add_argument(
    "--socket", metavar="PATH", help="serve the clients on a Unix socket at PATH (PLAN.md §6)"
)
parser.add_argument("--timeout", type=float, default=240, help="how long the operator waits")
parser.add_argument(
    "--resume",
    metavar="SESSION_ID",
    help="continue that Claude conversation (the `[session]` line of an earlier run names it)."
    " The throwaway database of this run does not hold its turns, so the checkers start"
    " without them; this only shows that Claude Code resumes under calu",
)
parser.add_argument(
    "--fork",
    metavar="SESSION_ID",
    help="continue a copy of that Claude conversation under a new session id (D203)",
)
parser.add_argument(
    "--stay",
    action="store_true",
    help="keep the session after the first turn and read further messages from stdin, so that"
    " a client can attach (the `[clients]` lines name the address and the token); end it with"
    " Ctrl-D",
)
parser.add_argument(
    "--honesty",
    choices=["off", "track", "enforce"],
    default="off",
    help="honesty.mode of the session (PLAN.md D196); useful with --checker-model only, because"
    " the rule-based fake checkers never say whether a command executes project code",
)
parser.add_argument(
    "--trust-head",
    action="store_true",
    help="certify HEAD of the project directory with the source `user` before the session"
    " starts, so that the session has a baseline",
)
parser.add_argument(
    "--reviewer-model",
    default="",
    metavar="MODEL",
    help="honesty.reviewer_model; blank is the default model of the checkers' credentials",
)
sys.exit(anyio.run(main, parser.parse_args()))
