"""What the command line prints for an operator, against a real database.

The handlers of `calu.cli` are what a person reads: `calu escalations` is how they find a step
that waits, `calu escalation <id>` is what they decide from, and `calu sessions` is the fleet.
The functions behind them are covered elsewhere; only here does the text an operator sees get
checked, and a stale or empty line is exactly the kind of thing no other test notices.

The credential path is replaced for these tests: `_with_admin` is given the test database, so
no credential store and no password are involved.
"""

from typing import Any, cast

import anyio
import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.setup import setup
from calu.store import (
    CheckerRun,
    DatabaseSessionStore,
    SessionIdentity,
    StepDecision,
    StepRow,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(
    hostname="box", os="Linux", os_user="u", directory="/w", pid=4321, git_commit="abc123"
)


@pytest.fixture
async def prepared(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set a database up, point the CLI at it, and return it with one recorded session.

    Args:
        db_url: The owner connection string of a fresh database.
        monkeypatch: Used to replace the CLI's connection helper.

    Yields:
        (the connection wrapper, the session id, the open escalation's id).
    """
    conn = await connect(db_url)
    await setup(conn, db_url, "test:model")
    db = Db(conn)
    store = DatabaseSessionStore(db)
    session_id = await store.start(IDENTITY)
    await store.set_claude_session("claude-7", "haiku")
    await store.set_state("escalated")
    step = await store.add_step(
        StepRow(seq=1, tool_name="Bash", tool_input={"command": "rm -rf build"}, turn=1)
    )
    await store.decide_step(
        step,
        StepDecision(
            decision="escalate", reason="not reversible", summary="Deletes the build directory."
        ),
    )
    await store.add_checker_run(
        step,
        CheckerRun(
            checker_name="command",
            checker_version=1,
            model="test:model",
            decision="escalate",
            reason="rm -rf is not reversible",
        ),
    )
    escalation = await store.open_escalation(
        "the command deletes a directory",
        {"tool_input": {"command": "rm -rf build"}, "rendered": "$ rm -rf build", "files": {}},
        step,
    )

    async def fake_with_admin(_url: str, work: Any) -> str:
        """Run the handler's work on a connection of its own to the test database.

        The command line runs in a worker thread with an event loop of its own (see `run`), so
        it must not borrow the connection this fixture made in the test's loop.

        Args:
            _url: The credential's URL, ignored in favour of the test database.
            work: The handler's coroutine function.

        Returns:
            What it returned.
        """
        own = await connect(db_url)
        try:
            return await work(Db(own))
        finally:
            await own.close()

    monkeypatch.setattr(cli, "_with_admin", fake_with_admin)
    monkeypatch.setattr(cli, "load_credential", lambda *_args, **_kwargs: bytearray(b"unused"))
    try:
        yield db, session_id, escalation
    finally:
        await conn.close()


async def run(argv: list[str]) -> int:
    """Run the command line in a worker thread and return its exit code.

    `cli.main` calls `asyncio.run`, which refuses to start inside a running loop, so the whole
    command runs in a thread with a loop of its own.

    Args:
        argv: The arguments after the program name.

    Returns:
        The exit code.
    """
    return await anyio.to_thread.run_sync(
        lambda: cli.main(argv, store_factory=lambda: cast(CredentialStore, _AnyCredential()))
    )


class _AnyCredential:
    """A credential store that claims to hold every credential."""

    def __contains__(self, _name: str) -> bool:
        """Say that the credential is there.

        Args:
            _name: The credential name.

        Returns:
            True, always.
        """
        return True

    def names(self) -> list[str]:
        """List the credentials.

        Returns:
            The two names the commands ask for.
        """
        return ["admin", "session"]


async def test_the_fleet_and_the_waiting_steps_are_listed(prepared, capsys) -> None:
    """`calu sessions` and `calu escalations` are how a person finds what needs them."""
    _db, session_id, escalation = prepared
    assert await run(["sessions"]) == 0
    said = capsys.readouterr().out
    assert session_id[:8] in said
    assert "escalated" in said and "box" in said and "/w" in said

    assert await run(["escalations"]) == 0
    said = capsys.readouterr().out
    assert str(escalation) in said
    assert "the command deletes a directory" in said


async def test_one_escalation_shows_what_the_decision_needs(prepared, capsys) -> None:
    """`calu escalation <id>` must carry the decision on its own (PLAN.md D30)."""
    _db, _session_id, escalation = prepared
    assert await run(["escalation", str(escalation)]) == 0
    said = capsys.readouterr().out
    assert "the command deletes a directory" in said  # why it stopped
    assert "rm -rf build" in said  # what would run
    assert "command" in said and "rm -rf is not reversible" in said  # what the checker said


async def test_resolving_from_the_command_line_reports_what_it_did(prepared, capsys) -> None:
    """The exit code and the line both have to say whether the session was unblocked."""
    _db, _session_id, escalation = prepared
    assert await run(["resolve", str(escalation), "allow", "go ahead"]) == 0
    assert "allow" in capsys.readouterr().out
    # A second attempt finds it resolved and says so instead of claiming success.
    assert await run(["resolve", str(escalation), "deny"]) == 1
    said = capsys.readouterr()
    assert "not open" in said.out + said.err or "already" in said.out + said.err


async def test_the_configuration_commands_print_what_is_stored(prepared, capsys) -> None:
    """`config get`, `checker list`, `rules list` and `project list` are read by a person."""
    assert await run(["config", "get"]) == 0
    capsys.readouterr()

    assert await run(["checker", "list"]) == 0
    said = capsys.readouterr().out
    assert "command" in said and "edit" in said and "test:model" in said

    assert await run(["rules", "list"]) == 0
    said = capsys.readouterr().out
    assert "command-deny" in said or "deny" in said

    assert await run(["project", "list"]) == 0
    assert "no projects" in capsys.readouterr().out

    assert await run(["project", "add", ".", "--name", "demo"]) == 0
    assert "demo" in capsys.readouterr().out
    assert await run(["project", "list"]) == 0
    assert "demo" in capsys.readouterr().out


async def test_tokens_are_minted_listed_and_revoked_from_the_command_line(prepared, capsys) -> None:
    """A token is printed once; the list shows it without showing the token."""
    _db, session_id, _escalation = prepared
    assert await run(["token", "mint", session_id, "--label", "laptop"]) == 0
    minted = capsys.readouterr().out
    assert "only copy" in minted
    token = [line.strip() for line in minted.splitlines() if line.strip()][1]
    assert len(token) > 20

    assert await run(["token", "list", session_id]) == 0
    listed = capsys.readouterr().out
    assert "laptop" in listed
    assert token not in listed, "the token itself must never be printed again"

    assert await run(["token", "revoke", "1"]) == 0
    assert "revoked" in capsys.readouterr().out


async def test_the_summary_command_shows_the_folded_conversation(prepared, capsys) -> None:
    """`calu summary` is the checkers' view of what came before (PLAN.md D40)."""
    db, session_id, _escalation = prepared
    await db.execute(
        "INSERT INTO chat_summaries (session_id, summary, covered_turns) VALUES (%s::uuid, %s, 3)",
        (session_id, "## Folded\n\nThe user asked for a cleanup."),
    )
    assert await run(["summary", session_id]) == 0
    said = capsys.readouterr().out
    # Only the summary itself, so that `calu summary <id> > CHAT_SUMMARY.md` is usable.
    assert said.strip() == "## Folded\n\nThe user asked for a cleanup."
