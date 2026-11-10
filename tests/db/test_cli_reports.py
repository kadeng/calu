"""What the command line prints for an operator, against a real database.

The handlers of `calu.cli` are what a person reads: `calu escalations` is how they find a step
that waits, `calu escalation <id>` is what they decide from, and `calu sessions` is the fleet.
The functions behind them are covered elsewhere; only here does the text an operator sees get
checked, and a stale or empty line is exactly the kind of thing no other test notices.

The credential path is replaced for these tests: `_with_admin` is given the test database, so
no credential store and no password are involved.
"""

import os
from typing import Any, cast

import anyio
import pytest

from calu import admin, cli
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
    # The credential *is* the connection string, so the stand-in hands out the test
    # database: `calu doctor --db` connects with it for real.
    monkeypatch.setattr(
        cli, "load_credential", lambda *_args, **_kwargs: bytearray(db_url.encode())
    )
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


async def test_the_editor_flow_stores_what_was_written(prepared, capsys, tmp_path) -> None:
    """`calu checker edit` and `calu summary --edit` go through $EDITOR (PLAN.md D40).

    The editor is a stub script that rewrites the file it is given, so the whole path is
    covered: calu writes the current text out, the editor changes it, calu reads it back and
    stores it. An editor that fails must store nothing.
    """
    db, session_id, _escalation = prepared
    editor = tmp_path / "fake-editor"
    editor.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(os.environ['EDITOR_WRITES'])\n"
        "sys.exit(int(os.environ.get('EDITOR_EXIT', '0')))\n",
        encoding="utf-8",
    )
    editor.chmod(0o755)
    os.environ["EDITOR"] = str(editor)
    os.environ["EDITOR_WRITES"] = "Only one rule: say no to everything with rm -rf.\n"
    try:
        assert await run(["checker", "edit", "command"]) == 0
        said = capsys.readouterr().out
        assert "editing" in said and "command" in said
        assert await admin.instructions(db, "command") == (
            "Only one rule: say no to everything with rm -rf.\n"
        )
        rows = await admin.checkers(db)
        edited = next(row for row in rows if row.name == "command")
        assert edited.version > 1, "a changed definition gets a new version"

        # The same for the folded conversation, which is what the checkers read (D40).
        await db.execute(
            "INSERT INTO chat_summaries (session_id, summary, covered_turns)"
            " VALUES (%s::uuid, 'old', 2)",
            (session_id,),
        )
        os.environ["EDITOR_WRITES"] = "## Folded\n\nThe user asked for a rewrite.\n"
        assert await run(["summary", session_id, "--edit"]) == 0
        capsys.readouterr()
        assert await admin.chat_summary(db, session_id) == (
            "## Folded\n\nThe user asked for a rewrite.\n"
        )

        # An editor that fails stores nothing and says so.
        os.environ["EDITOR_WRITES"] = "this must not be stored"
        os.environ["EDITOR_EXIT"] = "1"
        assert await run(["summary", session_id, "--edit"]) == 1
        complaint = capsys.readouterr()
        assert "nothing was stored" in complaint.out + complaint.err
        assert await admin.chat_summary(db, session_id) == (
            "## Folded\n\nThe user asked for a rewrite.\n"
        )
    finally:
        for name in ("EDITOR", "EDITOR_WRITES", "EDITOR_EXIT"):
            os.environ.pop(name, None)


async def test_doctor_checks_the_database_it_is_pointed_at(prepared, capsys) -> None:
    """`calu doctor --db` answers "does this database match this calu?"."""
    db, _session_id, _escalation = prepared
    assert await run(["doctor", "--db"]) == 0
    said = capsys.readouterr().out
    # It reaches the database and finds the schema current, because the fixture ran `setup`.
    assert "database: answers" in said
    assert "not applied" not in said

    # A migration missing from `schema_migrations` is a note with the command that fixes it --
    # a note, not a failure, because the session can still run against an older schema until
    # it needs what the migration adds.
    await db.execute(
        "DELETE FROM schema_migrations WHERE version = (SELECT max(version) FROM schema_migrations)"
    )
    assert await run(["doctor", "--db"]) == 0
    behind = capsys.readouterr().out
    assert "1 migration(s) not applied" in behind
    assert "calu db migrate" in behind

    # A database that cannot be reached is essential and makes the command fail.
    await db.execute("SELECT 1")  # the fixture's connection is still fine
    import calu.cli as cli_module

    original = cli_module.load_credential
    cli_module.load_credential = lambda *_a, **_k: bytearray(  # type: ignore[assignment]
        b"postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    )
    try:
        assert await run(["doctor", "--db"]) == 1
        failed = capsys.readouterr()
        assert "cannot connect" in failed.out
        assert "calu db up" in failed.out
    finally:
        cli_module.load_credential = original  # type: ignore[assignment]
