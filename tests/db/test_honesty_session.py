"""Code honesty of a session on the real database: certifying by hand and after a commit.

`tests/unit/test_honesty_session.py` runs the same flows on the in-memory store. This file
checks that the session's writes pass the database functions of migrations 0029 and 0044 with
the rights of the user login (`calu_admin`, which sessions use since D265): a person's
`/trust`, the stored verification of a checked edit, and the certification of a commit made in
a clean state.
"""

from pathlib import Path

import anyio
import pytest

from calu.checkers import CodeMark
from calu.claude import FakeRunner, FakeScript
from calu.config import Config, HonestyConfig
from calu.db import Db, connect
from calu.protocol.messages import ChatSendParams, HonestyUpdate, TurnResult
from calu.session import Session
from calu.store import DatabaseSessionStore
from calu.testing import fake_models, marking_checker
from tests.db.test_trust import git, user_login
from tests.unit.helpers import Recorder, default_settings

pytestmark = [pytest.mark.db, pytest.mark.anyio]

COMMIT = ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "second"]


async def test_a_session_certifies_by_hand_and_after_a_clean_commit(
    db_url: str, tmp_path: Path
) -> None:
    """A person's trust is recorded as "user"; the session's own commit as "check"."""
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    turns = [
        [
            {
                "tool": "Write",
                "input": {"file_path": "app.py", "content": "x = 2\n"},
                "writes": {"app.py": "x = 2\n"},
            },
            {"tool": "Bash", "input": {"command": "git add app.py"}, "run": ["git", "add", "-A"]},
            {"tool": "Bash", "input": {"command": "git commit -m second"}, "run": COMMIT},
        ]
    ]
    user_url = await user_login(db_url)
    conn = await connect(user_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        rec = Recorder()
        session = Session(
            repo,
            # With the honesty checker, whose HONEST verifies the edited file (D224).
            default_settings(repo, Config(honesty=HonestyConfig(mode="enforce")), honesty=True),
            store,
            lambda: FakeRunner(FakeScript(turns=turns), repo),
            rec,
            checker_models={**fake_models(), "command": marking_checker(executes=())},
            snapshot_root=tmp_path / "snapshots",
        )
        async with anyio.create_task_group() as tg:
            await session.start(tg)
            assert rec.of(HonestyUpdate)[-1].baseline is None
            done = await session.trust("main", client="attach:kai")
            assert done.commit_id == first and done.recorded
            assert rec.of(HonestyUpdate)[-1].clean
            await session.send_chat(ChatSendParams(text="go"))
            await rec.wait_for(TurnResult)
            await session.stop()
        head = git(repo, "rev-parse", "HEAD")
        assert head != first
        rows = await (
            await owner.execute(
                "SELECT commit_id, method, certified_by, session_id::text, recorded_as"
                " FROM trusted_commits ORDER BY certified_at"
            )
        ).fetchall()
        assert rows == [
            (first, "user", "attach:kai", store.session_id, "calu_admin"),
            (head, "check", "calu session", store.session_id, "calu_admin"),
        ]
        files = await (
            await owner.execute("SELECT path, method, certified_by FROM trusted_files")
        ).fetchall()
        assert files == [("app.py", "check", "calu session")]
        update = rec.of(HonestyUpdate)[-1]
        assert update.baseline == head and update.baseline_source == "check" and update.clean
    finally:
        await conn.close()
        await owner.close()


async def test_an_honesty_review_stores_what_it_verified_with_the_session_login(
    db_url: str, tmp_path: Path
) -> None:
    """A review of a repository without a trusted commit: the verified contents and HEAD are
    recorded with the source "review" through the database functions."""
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    head = git(repo, "rev-parse", "HEAD")
    user_url = await user_login(db_url)
    conn = await connect(user_url)
    owner = await connect(db_url)
    try:
        store = DatabaseSessionStore(Db(conn))
        rec = Recorder()
        session = Session(
            repo,
            default_settings(repo, Config(honesty=HonestyConfig(mode="enforce"))),
            store,
            lambda: FakeRunner(FakeScript(turns=[[]]), repo),
            rec,
            checker_models=fake_models(),
            snapshot_root=tmp_path / "snapshots",
        )
        async with anyio.create_task_group() as tg:
            await session.start(tg)
            assert rec.of(HonestyUpdate)[-1].baseline is None
            done = await session.request_review(client="attach:kai")
            assert done.ran and not done.problem
            await session.stop()
        commits = await (
            await owner.execute(
                "SELECT commit_id, method, certified_by, session_id::text FROM trusted_commits"
            )
        ).fetchall()
        assert commits == [(head, "review", "calu honesty review", store.session_id)]
        files = await (
            await owner.execute("SELECT path, method, certified_by FROM trusted_files")
        ).fetchall()
        assert files == [("app.py", "review", "calu honesty review")]
        update = rec.of(HonestyUpdate)[-1]
        assert update.baseline == head and update.baseline_source == "review" and update.clean
    finally:
        await conn.close()
        await owner.close()


async def test_a_mark_cleared_by_hand_outlasts_the_session(db_url: str, tmp_path: Path) -> None:
    """G9: the clearing is a person's decision about that content, stored with source "user"
    through the user login; the next session does not count the file as unreviewed."""
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    user_url = await user_login(db_url)
    owner = await connect(db_url)
    settings = default_settings(repo, Config(honesty=HonestyConfig(mode="enforce")))
    unreviewed: list[tuple[str, ...]] = []
    try:
        for run in (1, 2):
            conn = await connect(user_url)
            try:
                rec = Recorder()
                session = Session(
                    repo,
                    settings,
                    DatabaseSessionStore(Db(conn)),
                    lambda: FakeRunner(FakeScript(turns=[[]]), repo),
                    rec,
                    checker_models=fake_models(),
                    snapshot_root=tmp_path / "snapshots",
                )
                async with anyio.create_task_group() as tg:
                    await session.start(tg)
                    if run == 1:
                        await session.trust("main", client="attach:kai")
                    unreviewed.append((await session.honesty_snapshot()).unreviewed.paths)
                    if run == 1:
                        session.honesty.record(
                            [CodeMark(path="app.py", symbol="", reason="says 1")], [], "edit"
                        )
                        (open_mark,) = session.honesty.open_marks
                        await session.clear_mark(open_mark.id, client="attach:kai")
                        assert (await session.honesty_snapshot()).clean
                    await session.stop()
            finally:
                await conn.close()
        files = await (
            await owner.execute(
                "SELECT path, method, certified_by, recorded_as, note FROM trusted_files"
            )
        ).fetchall()
        assert files == [
            ("app.py", "user", "attach:kai", "calu_admin", "mark cleared by hand: app.py")
        ]
        assert unreviewed == [("app.py",), ()]
    finally:
        await owner.close()
