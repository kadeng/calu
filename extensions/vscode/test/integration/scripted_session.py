"""A scripted calu session for the extension's integration tests: no database, no Claude Code.

Usage (from the repository root, as `.vscode-test.mjs` starts it):

    uv run --frozen python extensions/vscode/test/integration/scripted_session.py WORKSPACE ENDPOINT

It runs a real `calu.session.Session` with the scripted runner, the rule-based fake checkers
and the in-memory store behind a real protocol server on 127.0.0.1, writes
`{"address": ..., "token": ...}` to the file ENDPOINT once the server listens, and runs until
it is ended (SIGTERM, SIGINT or stdin closed). The test attaches with `ide.attach {address,
token}`, for which `calu ide` asks no database.

    turn 1  text, `ls` (PASS), `git push origin main  # calu:escalate` (ESCALATE), text
    turn 2  text "Echo." (and every later turn)
"""

import argparse
import json
import signal
import sys
from pathlib import Path

import anyio

from calu.bus import Bus
from calu.checkers.rules import Rules
from calu.claude import FakeRunner, FakeScript
from calu.config import CheckerConfig, Config
from calu.defaults import default_instructions
from calu.protocol.server import SessionServer
from calu.session import Session
from calu.setup import CHECKER_DEFAULTS, recommended_rules
from calu.store import CheckerDefinition, MemorySessionStore, ProjectSettings
from calu.testing import fake_models
from calu.tokens import new_token, token_hash

TURNS = [
    [
        {"text": "Hello from the scripted session."},
        {"tool": "Bash", "input": {"command": "ls"}, "result": "greeter.py"},
        {
            "tool": "Bash",
            "input": {"command": "git push origin main  # calu:escalate"},
            "result": "pushed",
        },
        {"text": "After the decision."},
    ],
    *[[{"text": "Echo."}]] * 20,
]
"""The scripted turns (see the module docstring)."""


def settings(workspace: Path) -> ProjectSettings:
    """Build the settings a fresh `calu setup` stores, for a session without a database.

    Args:
        workspace: The project directory (the rules are relative to it).

    Returns:
        The enabled built-in checkers (version 1, model "test:model"; the fake checkers
        answer for them), the recommended rules and the default configuration.
    """
    checkers = {
        name: CheckerDefinition(
            name=name,
            route=route,
            instructions=default_instructions(name),
            model="test:model",
            request_limit=request_limit or CheckerConfig.model_fields["request_limit"].default,
            output_tokens_limit=CheckerConfig.model_fields["output_tokens_limit"].default,
            max_tokens=CheckerConfig.model_fields["max_tokens"].default,
            version=1,
        )
        for name, route, _, request_limit, enabled in CHECKER_DEFAULTS
        if enabled
    }
    rules = Rules.from_rows(
        workspace, ((r.list, r.pattern, r.position) for r in recommended_rules())
    )
    return ProjectSettings(config=Config(), checkers=checkers, rules=rules)


def write_endpoint(endpoint: Path, address: str, token: str) -> None:
    """Write the session's address and client token for the test, readable by the user only.

    Args:
        endpoint: The file; created with mode 0600 (an existing file keeps its mode).
        address: The WebSocket address of the protocol server.
        token: The client token the server accepts.
    """
    endpoint.touch(mode=0o600)
    endpoint.write_text(json.dumps({"address": address, "token": token}))


async def run(workspace: Path, endpoint: Path) -> None:
    """Serve the scripted session until a signal arrives or stdin closes.

    Side effects: starts the session (the in-memory store gives it an id), writes `endpoint`
    (mode 0600) once the server listens; the scripted runner plays `TURNS` and writes nothing
    into `workspace`; snapshots go to a sibling directory.

    Args:
        workspace: The project directory of the session.
        endpoint: The file that receives the address and the client token.
    """
    bus = Bus()
    script = FakeScript(turns=[list(turn) for turn in TURNS])
    session = Session(
        workspace,
        settings(workspace),
        MemorySessionStore(),
        lambda: FakeRunner(script, workspace),
        bus.publish,
        checker_models=fake_models(),
        snapshot_root=workspace.parent / f"{workspace.name}-snapshots",
    )
    token = new_token()
    async with anyio.create_task_group() as tg:
        # Started before a client connects, as `calu serve` does: the session has its id and
        # a client can attach and send at once.
        await session.start(tg)
        server = SessionServer(session, bus, token_hash(token), tg)
        tg.start_soon(server.listen, "127.0.0.1", 0)
        with anyio.fail_after(30):
            await server.ready.wait()
        write_endpoint(endpoint, server.address, token)
        print(f"scripted session at {server.address}", flush=True)

        async def stdin_closed() -> None:
            """Return when the parent closes stdin (the test runner ended)."""
            await anyio.to_thread.run_sync(sys.stdin.read, abandon_on_cancel=True)

        with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:

            async def on_signal() -> None:
                """Return at the first signal."""
                async for _ in signals:
                    return

            async with anyio.create_task_group() as wait:

                async def first(waiter: object) -> None:
                    """Run one waiter, then end the others.

                    Args:
                        waiter: `stdin_closed` or `on_signal`.
                    """
                    await waiter()  # type: ignore[operator]
                    wait.cancel_scope.cancel()

                wait.start_soon(first, stdin_closed)
                wait.start_soon(first, on_signal)
        await session.stop()
        tg.cancel_scope.cancel()


def main() -> int:
    """Parse the command line and serve the session.

    Returns:
        0 after the session was ended.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("endpoint", type=Path)
    args = parser.parse_args()
    anyio.run(run, args.workspace.resolve(), args.endpoint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
