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

    def needs_password(self, _name: str) -> bool:
        """Say that every entry is encrypted (D104).

        Args:
            _name: The credential name.

        Returns:
            True, always, so `calu doctor` says nothing about protection here.
        """
        return True


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
    # And what the step **is**, so the queue can be worked through without opening every entry
    # (D127): a list of reasons alone says why each one stopped, not what it would have done.
    assert "rm -rf build" in said


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
    assert "no rules" in capsys.readouterr().out, "D179: the lists start empty"
    assert await run(["rules", "add-set", "recommended"]) == 0
    capsys.readouterr()
    assert await run(["rules", "list"]) == 0
    assert "command-deny" in capsys.readouterr().out

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

        # Emptying it is refused, and the refusal says what to write instead (D114). The
        # reason is not caution about typos: the text covers turns that have left the recent
        # window, so an empty summary hides them from the checkers rather than summarizing
        # them -- the one outcome nobody would notice.
        os.environ["EDITOR_EXIT"] = "0"
        os.environ["EDITOR_WRITES"] = "   \n"
        assert await run(["summary", session_id, "--edit"]) == 1
        refused = capsys.readouterr()
        said = refused.out + refused.err
        assert "may not be empty" in said and "hide them from the checkers" in said
        assert "No turns have been folded yet." in said, "it has to say what to write instead"
        assert await admin.chat_summary(db, session_id) == (
            "## Folded\n\nThe user asked for a rewrite.\n"
        ), "and the summary that was there is untouched"
    finally:
        for name in ("EDITOR", "EDITOR_WRITES", "EDITOR_EXIT"):
            os.environ.pop(name, None)


async def test_doctor_checks_the_database_it_is_pointed_at(prepared, capsys) -> None:
    """`calu doctor --db` answers "does this database match this calu?"."""
    db, _session_id, _escalation = prepared
    # The exit code says whether *anything* a session needs is missing, which depends on the
    # machine (a CI runner has no `claude`), so only the database's own lines are asserted.
    await run(["doctor", "--db"])
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
    await run(["doctor", "--db"])
    behind = capsys.readouterr().out
    assert "1 migration(s) not applied" in behind
    assert "calu db migrate" in behind

    # A database that cannot be reached is essential, so it fails whatever else the machine
    # has -- and it names the command that starts one.
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
        assert "calu setup" in failed.out
    finally:
        cli_module.load_credential = original  # type: ignore[assignment]


async def test_evals_lists_and_exports_without_calling_a_model(prepared, capsys, tmp_path) -> None:
    """`calu evals --list` and `--export` are the loop that replaces a training mode (D55)."""
    _db, _session_id, _escalation = prepared
    assert await run(["evals", "--list"]) == 0
    listed = capsys.readouterr().out
    assert "case(s)" in listed
    # One line per case, with its checker and the decisions a good checker may give.
    assert " command " in listed or " edit " in listed
    assert "PASS" in listed or "ESCALATE" in listed

    target = tmp_path / "kept.yaml"
    assert await run(["evals", "--export", str(target)]) == 0
    assert f"to {target}" in capsys.readouterr().out
    document = target.read_text(encoding="utf-8")
    assert "- name:" in document and "expected:" in document

    # The file reads back as a case set, which is what makes it worth keeping.
    assert await run(["evals", "--list", "--no-builtin", str(target)]) == 0
    again = capsys.readouterr().out
    assert "kept.yaml" in again

    # `--export -` writes to stdout, for a pipe.
    assert await run(["evals", "--export", "-"]) == 0
    assert "- name:" in capsys.readouterr().out

    # A path that cannot be written is reported, not raised.
    assert await run(["evals", "--export", "/no/such/directory/cases.yaml"]) == 1
    assert "cannot write" in capsys.readouterr().err


async def test_cancelling_an_escalation_says_no_decision_was_recorded(prepared, capsys) -> None:
    """`calu resolve <id> cancel` clears a queue honestly (PLAN.md D57)."""
    db, session_id, escalation = prepared
    await db.execute(
        "UPDATE sessions SET last_seen = now() - interval '1 hour' WHERE id = %s::uuid",
        (session_id,),
    )
    # The list marks it before anyone spends attention on it.
    assert await run(["escalations"]) == 0
    assert "the session is gone" in capsys.readouterr().out

    assert await run(["resolve", str(escalation), "cancel", "the session is gone"]) == 0
    said = capsys.readouterr().out
    assert "cancelled" in said and "no decision was recorded" in said

    # It leaves the queue, and a second attempt is refused.
    assert await run(["escalations"]) == 0
    assert "no open escalations" in capsys.readouterr().out
    assert await run(["resolve", str(escalation), "cancel"]) == 1
    assert "not open" in capsys.readouterr().err


async def test_the_checkers_record_is_readable_from_the_command_line(prepared, capsys) -> None:
    """`calu checker stats` answers "are my checkers any good?" for free (PLAN.md D61)."""
    db, _session_id, _escalation = prepared
    assert await run(["checker", "stats"]) == 0
    said = capsys.readouterr().out
    # The fixture recorded one escalating check of the command checker.
    assert "what the checkers decided" in said
    assert "command" in said and "1 run(s)" in said
    assert "1 escalate" in said

    # A window is named in the heading, and an empty one says so rather than printing zeroes.
    assert await run(["checker", "stats", "--days", "7"]) == 0
    assert "in the last 7 day(s)" in capsys.readouterr().out
    await db.execute("UPDATE checker_runs SET created_at = now() - interval '90 days'")
    assert await run(["checker", "stats", "--days", "7"]) == 0
    assert "no checker has run yet" in capsys.readouterr().out


async def test_the_misses_are_readable_and_say_so_when_there_are_none(prepared, capsys) -> None:
    """`calu checker misses` points at the rows worth fixing instructions against (D62)."""
    db, _session_id, escalation = prepared
    # The fixture's escalation came from a checker that escalated, so nothing was missed.
    assert await run(["checker", "misses"]) == 0
    empty = capsys.readouterr().out
    assert "no misses" in empty
    # And it says how such a row comes about, instead of leaving a bare "none".
    assert "auto_escalate" in empty

    # Make it a miss: the checker approved the step and the human denied it anyway.
    await db.execute("UPDATE checker_runs SET decision = 'pass', reason = 'looked routine'")
    assert await run(["resolve", str(escalation), "deny", "not that directory"]) == 0
    capsys.readouterr()

    assert await run(["checker", "misses"]) == 0
    said = capsys.readouterr().out
    assert f"escalation {escalation}" in said
    assert "command said pass" in said
    assert "looked routine" in said
    assert "human: denied -- not that directory" in said
    # The id is there so the whole context is one command away.
    assert await run(["escalation", str(escalation)]) == 0
    assert "rm -rf build" in capsys.readouterr().out

    # A checker with no misses says so by name.
    assert await run(["checker", "misses", "edit"]) == 0
    assert "no misses of edit" in capsys.readouterr().out
