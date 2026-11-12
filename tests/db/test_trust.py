"""Trusted commits and trusted file contents in the database, and `calu honesty trust ...`.

The tables of migration 0027 hold what was certified as honest, each row with the source of
its trust (user > review > check). These tests check who may read and change them (a session
only through the two functions, an operator also removes and prunes), that a stronger source
replaces a weaker one and never the reverse, what the store returns, and what the command line
prints.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any, cast

import anyio
import psycopg
import pytest

from calu import cli
from calu.credentials import CredentialStore
from calu.db import Db, connect
from calu.honesty import Baseline, GitCommit, find_baseline
from calu.paths import INSIDE_SESSION_ENV
from calu.setup import enable_operator_login, setup
from calu.store import DatabaseSessionStore, SessionIdentity, trust

pytestmark = [pytest.mark.db, pytest.mark.anyio]

IDENTITY = SessionIdentity(
    hostname="box", os="Linux", os_user="u", directory="/w", pid=4321, git_commit="abc123"
)
COMMIT, TREE = "c" * 40, "d" * 40
OTHER, OTHER_TREE = "e" * 40, "f" * 40
SHA = hashlib.sha256(b"content\n").hexdigest()


async def logins(db_url: str) -> tuple[str, str]:
    """Set a database up and return the session and the operator connection strings.

    Args:
        db_url: The owner connection string of a fresh database.

    Returns:
        (the `calu_session` login, the `calu_operator` login).
    """
    owner = await connect(db_url)
    try:
        _, session_url = await setup(owner, db_url, "test:model")
        operator_url = await enable_operator_login(owner, db_url)
    finally:
        await owner.close()
    return session_url, operator_url


async def test_a_session_certifies_through_the_function_and_reads_the_result(
    db_url: str,
) -> None:
    """The store records a certification with its method and the session that made it."""
    session_url, _operator_url = await logins(db_url)
    conn = await connect(session_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        with pytest.raises(RuntimeError):
            await store.certify_commit(COMMIT, TREE, "check", "calu")
        session_id = await store.start(IDENTITY)

        assert await store.trusted_among([COMMIT], [TREE]) == ({}, {})
        assert await store.certify_commit(COMMIT, TREE, "check", "calu", "clean commit")
        # The same source again changes nothing.
        assert not await store.certify_commit(COMMIT, TREE, "check", "other")
        assert await store.certify_commit(OTHER, OTHER_TREE, "user", "attach:kai")
        assert await store.trusted_among([COMMIT, "0" * 40], ["1" * 40, OTHER_TREE]) == (
            {COMMIT: "check"},
            {OTHER_TREE: "user"},
        )

        row = await (
            await owner.execute(
                "SELECT tree_id, certified_by, method, note, session_id::text, recorded_as"
                " FROM trusted_commits WHERE commit_id = %s",
                (COMMIT,),
            )
        ).fetchone()
        assert row == (TREE, "calu", "check", "clean commit", session_id, "calu_session")
        audit = await (
            await owner.execute(
                "SELECT actor, action, new_row->>'commit_id' FROM audit_log"
                " WHERE table_name = 'trusted_commits' ORDER BY id"
            )
        ).fetchall()
        assert audit == [("calu", "INSERT", COMMIT), ("attach:kai", "INSERT", OTHER)]

        assert await store.trusted_file_sources() == {}
        assert await store.trust_file(SHA, "/w/build/gen.py", "check", "calu", "edit")
        assert not await store.trust_file(SHA, "/elsewhere", "check", "calu")
        assert await store.trusted_file_sources() == {SHA: "check"}
        assert await store.trusted_file_sources([SHA, "0" * 64]) == {SHA: "check"}
        assert await store.trusted_file_sources(["0" * 64]) == {}
    finally:
        await conn.close()
        await owner.close()


async def test_the_session_role_changes_the_tables_only_through_the_functions(
    db_url: str,
) -> None:
    """No direct write, no removal, and no certification without a proven session."""
    session_url, _operator_url = await logins(db_url)
    unproven = await connect(session_url)
    proven = await connect(session_url)
    try:
        for statement in (
            f"SELECT calu_certify_commit('{COMMIT}', '{TREE}', 'check', 'calu', NULL)",
            f"SELECT calu_trust_file('{SHA}', '/w/x', 'check', 'calu', NULL)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await unproven.execute(statement.encode())

        store = DatabaseSessionStore(Db(proven))
        await store.start(IDENTITY)
        await store.certify_commit(COMMIT, TREE, "check", "calu")
        await store.trust_file(SHA, "/w/x", "user", "kai")
        for statement in (
            f"INSERT INTO trusted_commits (commit_id, tree_id, certified_by, method)"
            f" VALUES ('{OTHER}', '{OTHER_TREE}', 'me', 'user')",
            "UPDATE trusted_commits SET method = 'user'",
            "DELETE FROM trusted_commits",
            f"SELECT calu_untrust_commit('{COMMIT}')",
            f"INSERT INTO trusted_files (content_sha256, path, certified_by, method)"
            f" VALUES ('{'a' * 64}', '/w/y', 'me', 'user')",
            "UPDATE trusted_files SET method = 'user'",
            "DELETE FROM trusted_files",
            f"SELECT calu_untrust_file('{SHA}')",
            "SELECT calu_prune_trust(0)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await proven.execute(statement.encode())
        assert await store.trusted_among([COMMIT], []) == ({COMMIT: "check"}, {})

        # What is recorded has the shape of an id, a method and a name.
        for arguments in (
            ("not-an-id", TREE, "check", "calu"),
            (OTHER, "short", "check", "calu"),
            (OTHER, OTHER_TREE, "guess", "calu"),
            (OTHER, OTHER_TREE, "session", "calu"),
            (COMMIT, TREE, "guess", "calu"),
            (OTHER, OTHER_TREE, "check", ""),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                await proven.execute(b"SELECT calu_certify_commit(%s, %s, %s, %s, NULL)", arguments)
        for arguments in (("abc", "check"), (SHA, "guess"), ("b" * 64, "session")):
            with pytest.raises(psycopg.errors.CheckViolation):
                await proven.execute(
                    b"SELECT calu_trust_file(%s, '/w/x', %s, 'calu', NULL)", arguments
                )
    finally:
        await unproven.close()
        await proven.close()


async def test_an_operator_lists_adds_and_removes(db_url: str) -> None:
    """The operator login certifies as a user and removes; it writes to no table directly."""
    _session_url, operator_url = await logins(db_url)
    conn = await connect(operator_url)
    owner = await connect(db_url)
    try:
        db = Db(conn)
        assert await trust.certify_commit(db, COMMIT, TREE, "user", "kai", "reviewed by hand")
        assert not await trust.certify_commit(db, COMMIT, TREE, "user", "kai")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await trust.certify_commit(db, OTHER, OTHER_TREE, "check", "kai")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await trust.trust_file(db, SHA, "/w/x", "review", "kai")
        # The owner (the `admin` credential) may do the same.
        assert await trust.certify_commit(Db(owner), OTHER, OTHER_TREE, "user", "admin")

        listed = await trust.trusted_commits(db)
        assert {row.commit_id for row in listed} == {COMMIT, OTHER}
        mine = next(row for row in listed if row.commit_id == COMMIT)
        assert (mine.method, mine.certified_by, mine.note) == ("user", "kai", "reviewed by hand")
        assert "reviewed by hand" in mine.line() and COMMIT[:12] in mine.line()
        assert len(await trust.trusted_commits(db, limit=1)) == 1
        assert await trust.commits_with_prefix(db, "ccc") == [COMMIT]
        assert await trust.commits_with_prefix(db, "not hex") == []

        for statement in (
            "DELETE FROM trusted_commits",
            "UPDATE trusted_commits SET tree_id = commit_id",
            f"INSERT INTO trusted_files (content_sha256, path, certified_by, method)"
            f" VALUES ('{SHA}', '/w/y', 'me', 'user')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(statement.encode())

        assert await trust.untrust_commit(db, COMMIT)
        assert not await trust.untrust_commit(db, COMMIT)
        assert [row.commit_id for row in await trust.trusted_commits(db)] == [OTHER]

        assert await trust.trust_file(db, SHA, "/w/build/gen.py", "user", "kai")
        (row,) = await trust.trusted_files(db)
        assert (row.content_sha256, row.path, row.certified_by) == (SHA, "/w/build/gen.py", "kai")
        assert row.method == "user" and "user" in row.line()
        assert await trust.untrust_file(db, "/w/build/gen.py") == 1
        assert await trust.trust_file(db, SHA, "/w/build/gen.py", "user", "kai")
        assert await trust.untrust_file(db, SHA) == 1
        assert await trust.untrust_file(db, SHA) == 0

        removed = await (
            await owner.execute(
                "SELECT table_name, old_row->>'certified_by' FROM audit_log"
                " WHERE action = 'DELETE' AND table_name LIKE 'trusted%' ORDER BY id"
            )
        ).fetchall()
        assert removed == [
            ("trusted_commits", "kai"),
            ("trusted_files", "kai"),
            ("trusted_files", "kai"),
        ]
    finally:
        await conn.close()
        await owner.close()


async def test_a_change_of_trust_is_announced(db_url: str) -> None:
    """A session that listens on `trust_changed` can look up its baseline again."""
    await logins(db_url)
    owner = await connect(db_url)
    listener = await psycopg.AsyncConnection.connect(db_url, autocommit=True)
    try:
        await listener.execute("LISTEN trust_changed")
        await trust.certify_commit(Db(owner), COMMIT, TREE, "user", "kai")
        with anyio.fail_after(5):
            async for notification in listener.notifies():
                assert notification.payload == '{"table" : "trusted_commits"}'
                break
    finally:
        await listener.close()
        await owner.close()


async def test_a_stronger_source_replaces_a_weaker_one_and_never_the_reverse(
    db_url: str,
) -> None:
    """check < review < user, for commits and for file contents; each replacement is audited."""
    session_url, operator_url = await logins(db_url)
    conn = await connect(session_url)
    operator = await connect(operator_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        session_id = await store.start(IDENTITY)

        assert await store.certify_commit(COMMIT, TREE, "check", "calu", "by checks")
        assert await store.certify_commit(COMMIT, TREE, "review", "calu", "by review")
        assert not await store.certify_commit(COMMIT, TREE, "check", "calu", "weaker")
        assert await store.trusted_among([COMMIT], [TREE]) == ({COMMIT: "review"}, {TREE: "review"})
        # A person's certification from the command line replaces the session's.
        assert await trust.certify_commit(Db(operator), COMMIT, TREE, "user", "kai", "read it")
        assert not await store.certify_commit(COMMIT, TREE, "review", "calu")
        assert not await trust.certify_commit(Db(operator), COMMIT, TREE, "user", "someone")
        (row,) = await trust.trusted_commits(Db(operator))
        assert (row.method, row.certified_by, row.note) == ("user", "kai", "read it")
        kept = await (
            await owner.execute(
                "SELECT session_id, recorded_as FROM trusted_commits WHERE commit_id = %s",
                (COMMIT,),
            )
        ).fetchone()
        assert kept == (None, "calu_operator")
        # The strongest source of a tree is the one of any commit that records it.
        assert await store.certify_commit(OTHER, TREE, "check", "calu")
        assert await store.trusted_among([OTHER], [TREE]) == ({OTHER: "check"}, {TREE: "user"})

        assert await store.trust_file(SHA, "/w/a.py", "check", "calu")
        assert await store.trust_file(SHA, "/w/a.py", "review", "calu")
        assert not await store.trust_file(SHA, "/w/a.py", "check", "calu")
        assert await store.trust_file(SHA, "/w/b.py", "user", "attach:kai", "generated")
        assert not await store.trust_file(SHA, "/w/c.py", "review", "calu")
        (file_row,) = await trust.trusted_files(Db(operator))
        assert (file_row.method, file_row.path, file_row.certified_by, file_row.note) == (
            "user",
            "/w/b.py",
            "attach:kai",
            "generated",
        )

        audit = await (
            await owner.execute(
                "SELECT table_name, action, old_row->>'method', new_row->>'method',"
                " session_id::text FROM audit_log WHERE table_name LIKE 'trusted%%' ORDER BY id"
            )
        ).fetchall()
        assert audit == [
            ("trusted_commits", "INSERT", None, "check", session_id),
            ("trusted_commits", "UPDATE", "check", "review", session_id),
            ("trusted_commits", "UPDATE", "review", "user", None),
            ("trusted_commits", "INSERT", None, "check", session_id),
            ("trusted_files", "INSERT", None, "check", session_id),
            ("trusted_files", "UPDATE", "check", "review", session_id),
            ("trusted_files", "UPDATE", "review", "user", session_id),
        ]
    finally:
        await conn.close()
        await operator.close()
        await owner.close()


async def test_prune_removes_old_check_and_review_rows_and_never_user_rows(db_url: str) -> None:
    """`calu_prune_trust(days)` is the operator's; it keeps what a person certified."""
    session_url, operator_url = await logins(db_url)
    conn = await connect(session_url)
    operator = await connect(operator_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        await store.start(IDENTITY)
        db = Db(operator)
        await store.certify_commit(COMMIT, TREE, "check", "calu")
        await store.certify_commit(OTHER, OTHER_TREE, "user", "attach:kai")
        await store.certify_commit("1" * 40, "2" * 40, "review", "calu")
        await store.trust_file("a" * 64, "/w/a", "check", "calu")
        await store.trust_file("b" * 64, "/w/b", "review", "calu")
        await store.trust_file("c" * 64, "/w/c", "user", "attach:kai")
        await store.trust_file("d" * 64, "/w/d", "check", "calu")
        await owner.execute(
            "UPDATE trusted_commits SET certified_at = now() - interval '40 days'"
            " WHERE commit_id <> %s",
            ("1" * 40,),
        )
        await owner.execute(
            "UPDATE trusted_files SET certified_at = now() - interval '40 days'"
            " WHERE content_sha256 <> %s",
            ("d" * 64,),
        )

        assert await trust.prune(db, 60) == 0
        assert await trust.prune(db, 30) == 3
        assert {row.commit_id: row.method for row in await trust.trusted_commits(db)} == {
            OTHER: "user",
            "1" * 40: "review",
        }
        assert await trust.trusted_file_sources(db) == {"c" * 64: "user", "d" * 64: "check"}
        assert await trust.prune(db, 0) == 2
        assert await trust.trusted_file_sources(db) == {"c" * 64: "user"}
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            await trust.prune(db, -1)
        logged = await (
            await owner.execute(
                "SELECT table_name, old_row->>'pruned' FROM audit_log"
                " WHERE action = 'DELETE' AND old_row ? 'pruned' ORDER BY id"
            )
        ).fetchall()
        assert logged == [
            ("trusted_commits", "1"),
            ("trusted_files", "2"),
            ("trusted_commits", "1"),
            ("trusted_files", "1"),
        ]
    finally:
        await conn.close()
        await operator.close()
        await owner.close()


async def test_a_session_s_own_file_verifications_are_not_announced(db_url: str) -> None:
    """One row is written per verified edit; only a person's trust of a file is announced."""
    session_url, _operator_url = await logins(db_url)
    conn = await connect(session_url)
    listener = await psycopg.AsyncConnection.connect(db_url, autocommit=True)
    try:
        store = DatabaseSessionStore(Db(conn))
        await store.start(IDENTITY)
        await listener.execute("LISTEN trust_changed")
        await store.trust_file("a" * 64, "/w/a", "check", "calu")
        await store.trust_file("b" * 64, "/w/b", "review", "calu")
        await store.trust_file("c" * 64, "/w/c", "user", "attach:kai")
        seen: list[str] = []
        with anyio.move_on_after(2):
            async for notification in listener.notifies():
                seen.append(notification.payload)
        assert seen == ['{"table" : "trusted_files"}']
    finally:
        await listener.close()
        await conn.close()


# ------------------------------------------------------------------------- the command line


def git(repo: Path, *arguments: str) -> str:
    """Run git in a test repository and return its output.

    Args:
        repo: The repository.
        *arguments: The git arguments.

    Returns:
        The standard output, stripped.
    """
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


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


@pytest.fixture
async def project(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A git repository as the current directory, and the CLI pointed at the test database.

    The command line runs as the operator login, which is what `calu honesty trust` asks for
    first.

    Args:
        db_url: The owner connection string of a fresh database.
        tmp_path: The test's temporary directory.
        monkeypatch: Replaces the CLI's credential and connection helpers.

    Yields:
        (the repository's directory, the operator connection string).
    """
    _session_url, operator_url = await logins(db_url)
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli, "load_credential", lambda *_args, **_kwargs: bytearray(operator_url.encode())
    )
    monkeypatch.setattr(cli, "_os_user", lambda: "kai")
    yield repo, operator_url


async def run(argv: list[str]) -> int:
    """Run the command line in a worker thread and return its exit code.

    Args:
        argv: The arguments after the program name.

    Returns:
        The exit code.
    """
    return await anyio.to_thread.run_sync(
        lambda: cli.main(argv, store_factory=lambda: cast(CredentialStore, _AnyCredential()))
    )


async def test_trust_trusted_and_untrust_on_the_command_line(project, capsys) -> None:
    """A branch is certified as the commit it points at now, listed, and removed again."""
    repo, operator_url = project
    first = GitCommit(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}"))

    assert await run(["honesty", "trusted"]) == 0
    assert "none; `calu honesty trust <commit-or-branch>`" in capsys.readouterr().out

    assert await run(["honesty", "trust", "main", "--note", "read it myself"]) == 0
    assert f"commit {first.short} (main): trusted" in capsys.readouterr().out
    assert await run(["honesty", "trust", "HEAD"]) == 0
    assert "was trusted by a user already" in capsys.readouterr().out

    # The branch moves on; the certification stays with the commit.
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "second")
    conn = await connect(operator_url)
    try:
        db = Db(conn)

        async def lookup(commits: Any, trees: Any) -> Any:
            """Ask the test database which ids are trusted.

            Args:
                commits: Commit ids.
                trees: Tree ids.

            Returns:
                The trusted ones.
            """
            return await trust.trusted_among(db, commits, trees)

        assert await find_baseline(repo, lookup) == Baseline(first.commit_id, first.tree_id, "user")
        (row,) = await trust.trusted_commits(db)
        assert (row.commit_id, row.tree_id) == (first.commit_id, first.tree_id)
        assert (row.method, row.certified_by, row.note) == ("user", "kai", "read it myself")
    finally:
        await conn.close()

    assert await run(["honesty", "trusted"]) == 0
    said = capsys.readouterr().out
    assert first.short in said and "kai" in said and "read it myself" in said

    assert await run(["honesty", "untrust", "main"]) == 1
    assert "main is not a trusted commit" in capsys.readouterr().err
    assert await run(["honesty", "untrust", first.commit_id[:9]]) == 0
    assert f"commit {first.short}: no longer trusted" in capsys.readouterr().out
    assert await run(["honesty", "trusted"]) == 0
    assert first.short not in capsys.readouterr().out


async def test_what_cannot_be_resolved_is_not_certified(project, capsys) -> None:
    """An unknown name, a missing name and a name that is an option record nothing."""
    _repo, operator_url = project
    for argv in (
        ["honesty", "trust", "no-such-branch"],
        ["honesty", "trust"],
        ["honesty", "trust", "main", "--file", "a.txt"],
        ["honesty", "trust", "--file", "no-such-file"],
        ["honesty", "untrust", "abcdef"],
    ):
        assert await run(argv) == 1, argv
        assert "calu: CliError" in capsys.readouterr().err
    conn = await connect(operator_url)
    try:
        assert await trust.trusted_commits(Db(conn)) == []
        assert await trust.trusted_files(Db(conn)) == []
    finally:
        await conn.close()


async def test_a_file_content_is_trusted_by_hash_on_the_command_line(project, capsys) -> None:
    """`--file` records the SHA-256 of the present content and the path as a hint."""
    repo, operator_url = project
    (repo / "gen.py").write_text("print('x')\n", encoding="utf-8")
    sha = hashlib.sha256(b"print('x')\n").hexdigest()

    assert await run(["honesty", "trust", "--file", "gen.py", "--note", "generated"]) == 0
    assert f"(sha256 {sha[:12]}): trusted" in capsys.readouterr().out
    conn = await connect(operator_url)
    try:
        (row,) = await trust.trusted_files(Db(conn))
        assert (row.content_sha256, row.path) == (sha, str(repo / "gen.py"))
        assert (row.certified_by, row.note) == ("kai", "generated")
    finally:
        await conn.close()

    assert await run(["honesty", "trusted"]) == 0
    said = capsys.readouterr().out
    assert "trusted file contents (" in said and sha[:12] in said and "gen.py" in said

    assert await run(["honesty", "untrust", "--file", "gen.py"]) == 0
    assert "1 trusted file content(s) removed" in capsys.readouterr().out
    assert await run(["honesty", "untrust", "--file", sha]) == 1
    assert "no trusted file content" in capsys.readouterr().err


async def test_prune_on_the_command_line_and_the_source_in_the_listing(project, capsys) -> None:
    """`calu honesty prune --days N` reports what it removed; `trusted` names each source."""
    repo, operator_url = project
    first = GitCommit(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}"))
    owner_url = operator_url  # the listing below reads with the operator login
    assert await run(["honesty", "trust", "main"]) == 0
    assert await run(["honesty", "trust", "--file", "a.txt"]) == 0
    capsys.readouterr()

    assert await run(["honesty", "prune"]) == 1
    assert "needs --days N" in capsys.readouterr().err
    assert await run(["honesty", "prune", "--days", "-1"]) == 1
    capsys.readouterr()
    assert await run(["honesty", "prune", "--days", "0"]) == 0
    assert "0 record(s) of the sources check and review older than 0 day(s) removed" in (
        capsys.readouterr().out
    )
    assert await run(["honesty", "trusted"]) == 0
    said = capsys.readouterr().out
    commit_line = next(line for line in said.splitlines() if first.short in line)
    assert "  user   " in commit_line and "kai" in commit_line
    file_line = next(line for line in said.splitlines() if "a.txt" in line)
    assert "  user   " in file_line
    conn = await connect(owner_url)
    try:
        assert len(await trust.trusted_commits(Db(conn))) == 1
    finally:
        await conn.close()


async def test_the_certifying_commands_do_not_run_inside_a_session(
    project, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the marker of a session in the environment only the listing runs."""
    _repo, operator_url = project
    monkeypatch.setenv(INSIDE_SESSION_ENV, "1")
    for argv in (
        ["honesty", "trust", "main"],
        ["honesty", "trust", "--file", "a.txt"],
        ["honesty", "untrust", "main"],
        ["honesty", "prune", "--days", "0"],
    ):
        assert await run(argv) == 1, argv
        assert "does not run inside a calu session" in capsys.readouterr().err
    assert await run(["honesty", "trusted"]) == 0
    assert "none; `calu honesty trust" in capsys.readouterr().out
    conn = await connect(operator_url)
    try:
        assert await trust.trusted_commits(Db(conn)) == []
        assert await trust.trusted_files(Db(conn)) == []
    finally:
        await conn.close()
