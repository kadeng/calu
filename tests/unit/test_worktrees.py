"""Git worktrees in a session (PLAN.md D251, `agent/records/notes/worktree-design.md`).

The cases are numbered as in the design's test plan (U1-U14). Each runs on a real temporary
git repository; the session cases use the scripted fake runner, fake checker models and the
in-memory store.
"""

import subprocess
from pathlib import Path

import pytest

from calu.checkers import Step
from calu.checkouts import Checkout, Checkouts, parse_worktrees

pytestmark = pytest.mark.anyio


def git(root: Path, *arguments: str) -> str:
    """Run git in a test repository.

    Args:
        root: The repository or one of its worktrees.
        arguments: The git arguments.

    Returns:
        git's standard output, stripped.
    """
    done = subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit: `helper.py` and `README.md`.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The repository's directory.
    """
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "helper.py").write_text('"""Help."""\n', encoding="utf-8")
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


def worktree(root: Path, name: str) -> Path:
    """Create a worktree where Claude Code puts them, on a new branch `worktree-<name>`.

    Args:
        root: The repository.
        name: The worktree's name.

    Returns:
        The worktree's directory.
    """
    git(root, "worktree", "add", "-q", f".claude/worktrees/{name}", "-b", f"worktree-{name}")
    return root / ".claude" / "worktrees" / name


# ------------------------------------------------------------------ U1: the registry


def test_u1_porcelain_is_parsed_with_branches_and_detached_heads() -> None:
    output = (
        b"worktree /p\0HEAD 1111\0branch refs/heads/main\0\0"
        b"worktree /p/.claude/worktrees/a\0HEAD 2222\0branch refs/heads/worktree-a\0\0"
        b"worktree /tmp/x\0HEAD 3333\0detached\0\0"
    )
    assert parse_worktrees(output) == [
        (Path("/p"), "main"),
        (Path("/p/.claude/worktrees/a"), "worktree-a"),
        (Path("/tmp/x"), ""),
    ]


def test_u1_the_nearest_worktree_wins_and_outside_ones_are_checkouts(repo: Path) -> None:
    inner = worktree(repo, "wta")
    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "--detach", str(outside))
    registry = Checkouts(repo)
    assert registry.refresh()
    labels = {checkout.label: checkout for checkout in registry.others}
    assert set(labels) == {".claude/worktrees/wta", str(outside.resolve())}
    assert labels[".claude/worktrees/wta"].branch == "worktree-wta"
    assert labels[str(outside.resolve())].outside and labels[str(outside.resolve())].branch == ""
    assert registry.of(inner / "helper.py") == labels[".claude/worktrees/wta"]
    assert registry.of(".claude/worktrees/wta") == labels[".claude/worktrees/wta"]
    assert registry.of("helper.py") is registry.main
    assert registry.of(".claude/settings.json") is registry.main
    assert registry.of(outside / "x.py") == labels[str(outside.resolve())]
    assert registry.of("/usr/bin") is None, "another directory is no checkout"
    assert registry.nested() == (".claude/worktrees/wta",)
    assert not registry.refresh(), "nothing changed"


def test_u1_a_worktree_created_later_is_found_by_the_lookup_itself(repo: Path) -> None:
    registry = Checkouts(repo)
    registry.refresh()
    assert registry.others == ()
    inner = worktree(repo, "late")  # as EnterWorktree or a worktree subagent does
    found = registry.of(inner / "helper.py")
    assert found is not None and found.label == ".claude/worktrees/late"
    git(repo, "worktree", "remove", str(inner))
    assert registry.refresh() and registry.others == ()


def test_u1_without_a_repository_there_is_only_the_main_checkout(tmp_path: Path) -> None:
    registry = Checkouts(tmp_path)
    assert not registry.refresh()
    assert registry.all() == (registry.main,)
    assert registry.of("a.py") is registry.main


def test_u1_a_session_started_inside_a_worktree_takes_it_as_its_main_checkout(
    repo: Path,
) -> None:
    inner = worktree(repo, "wta")
    registry = Checkouts(inner)
    registry.refresh()
    assert registry.main.root == inner.resolve()
    (other,) = registry.others
    assert other.root == repo.resolve() and other.outside


# ------------------------------------------------------------------ U2: a step's checkouts


def test_u2_a_cd_into_a_worktree_belongs_to_main_and_to_the_worktree(repo: Path) -> None:
    worktree(repo, "b")
    registry = Checkouts(repo)
    registry.refresh()
    from calu.checkers.shell import parse_command

    parsed = parse_command("cd .claude/worktrees/b && python x.py", repo)
    found = registry.step_checkouts(repo, parsed.directories)
    assert [checkout.label for checkout in found] == [".", ".claude/worktrees/b"]
    inside = registry.step_checkouts(repo / ".claude" / "worktrees" / "b")
    assert [checkout.label for checkout in inside] == [".claude/worktrees/b"]
    elsewhere = registry.step_checkouts("/usr")
    assert elsewhere == [registry.main], "outside every checkout: as before, the main one"


async def test_u2_the_session_names_the_checkouts_of_a_scripted_step(repo: Path) -> None:
    from tests.unit.helpers import make_session

    worktree(repo, "b")
    session, _rec, _store = make_session(repo, [[]])
    await session._refresh_checkouts()  # pyright: ignore[reportPrivateUsage]
    step = Step(
        tool_name="Bash",
        tool_input={"command": "cd .claude/worktrees/b && python x.py"},
        cwd=str(repo),
    )
    assert [checkout.label for checkout in session.step_checkouts(step)] == [
        ".",
        ".claude/worktrees/b",
    ]
    edit = Step(
        tool_name="Edit",
        tool_input={"file_path": "x.py"},
        cwd=str(repo / ".claude" / "worktrees" / "b"),
    )
    assert [checkout.label for checkout in session.step_checkouts(edit)] == [".claude/worktrees/b"]


def test_a_checkout_names_itself_for_a_checker() -> None:
    assert Checkout(Path("/p"), ".", main=True).describe() == "`.` (main checkout)"
    nested = Checkout(Path("/p/w"), ".claude/worktrees/wta", branch="worktree-wta")
    assert nested.describe() == "`.claude/worktrees/wta` (worktree, branch `worktree-wta`)"
    outside = Checkout(Path("/tmp/x"), "/tmp/x", outside=True)
    assert outside.describe() == (
        "`/tmp/x` (worktree outside the project directory, detached HEAD)"
    )


async def test_the_fake_runner_runs_a_call_in_its_own_working_directory(repo: Path) -> None:
    """The scripted runner's per-call `cwd`: the step's working directory, writes go there."""
    from calu.protocol.messages import TurnResult
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    inner = worktree(repo, "wta")
    seen: list[str] = []
    session, rec, _store = make_session(
        repo,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "echo x > out.txt"},
                    "cwd": ".claude/worktrees/wta",
                    "writes": {"out.txt": "x\n"},
                    "result": "ok",
                }
            ]
        ],
    )
    original = session.pre_tool_use

    async def spy(step: Step):
        seen.append(step.cwd)
        return await original(step)

    session.pre_tool_use = spy  # type: ignore[method-assign]
    await run_turns(session, rec)
    assert rec.of(TurnResult)
    assert seen == [str(inner)]
    assert (inner / "out.txt").read_text() == "x\n" and not (repo / "out.txt").exists()


# ------------------------------------------------------------------ honesty per checkout

DISHONEST = (
    '"""Help."""\n\n\ndef add(a, b):\n    """Return the sum of a and b."""\n    return a - b\n'
)
HONEST = '"""Help."""\n\n\ndef add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def run_in(where: str, command: str = "make test") -> dict[str, object]:
    """Build a scripted Bash call that the fake command checker reads as running code.

    Args:
        where: The call's working directory, relative to the project ("" for the project).
        command: The command line.

    Returns:
        The action for the fake runner's script.
    """
    return {"tool": "Bash", "input": {"command": command}, "cwd": where, "result": "ok"}


def write_in(where: str, path: str, content: str) -> dict[str, object]:
    """Build a scripted Write call in a working directory that really writes the file.

    Args:
        where: The call's working directory, relative to the project.
        path: The file, relative to `where`.
        content: The new content.

    Returns:
        The action for the fake runner's script.
    """
    return {
        "tool": "Write",
        "input": {"file_path": path, "content": content},
        "cwd": where,
        "writes": {path: content},
        "result": "ok",
    }


def decisions(rec: object) -> list[str]:
    """List the decision of every recorded step, in order.

    Args:
        rec: The session's event recorder.

    Returns:
        The decisions' values ("pass", "deny", "escalate", ...).
    """
    from calu.protocol.messages import CheckResult

    return [check.decision.value.lower() for check in rec.of(CheckResult)]  # type: ignore[attr-defined]


async def test_u3_a_mark_in_one_worktree_does_not_stop_the_others(repo: Path) -> None:
    from calu.testing import honesty_answering
    from tests.unit.test_honesty_session import run_turns, session_on

    worktree(repo, "a")
    worktree(repo, "b")
    session, rec, _store = await session_on(
        repo,
        [
            [
                write_in(".claude/worktrees/a", "helper.py", DISHONEST),
                run_in(".claude/worktrees/b"),
                run_in(""),
                run_in(".claude/worktrees/a"),
            ]
        ],
        models={
            "honesty": honesty_answering(
                dishonest=[(".claude/worktrees/a/helper.py", "add", "subtracts")]
            )
        },
    )
    await run_turns(session, rec)
    assert decisions(rec) == ["pass", "pass", "pass", "deny"]
    (mark,) = [m for state in session._states.values() for m in state.open_marks]  # pyright: ignore[reportPrivateUsage]
    assert mark.path == ".claude/worktrees/a/helper.py"
    assert session.honesty.open_marks == [], "the main checkout's state holds no mark"


async def test_u4_an_unignored_worktree_is_no_unreviewed_file_of_the_main_checkout(
    repo: Path,
) -> None:
    from tests.unit.test_honesty_session import run_turns, session_on

    session, rec, _store = await session_on(
        repo,
        [
            [
                {
                    "tool": "EnterWorktree",
                    "input": {"name": "wta"},
                    "run": ["git", "worktree", "add", "-q", ".claude/worktrees/wta"],
                    "result": "ok",
                },
                run_in(""),
            ]
        ],
    )
    await run_turns(session, rec)
    assert decisions(rec)[-1] == "pass"
    state = await session.honesty_snapshot()
    assert state.clean, state
    assert ".claude/worktrees/wta" in session.honesty.excluded


async def test_u5_a_dishonest_file_left_in_an_ignored_worktree_is_reviewed_again(
    repo: Path,
) -> None:
    from calu.testing import fake_reviewer
    from tests.unit.test_honesty_session import logs, run_turns, session_on

    (repo / ".gitignore").write_text(".claude/worktrees/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore worktrees")
    inner = worktree(repo, "b")
    # What an earlier session left: an uncommitted dishonest edit in an ignored worktree.
    (inner / "helper.py").write_text(DISHONEST, encoding="utf-8")
    session, rec, _store = await session_on(
        repo,
        [[run_in(".claude/worktrees/b")]],
        models={
            "honesty-review": fake_reviewer({".claude/worktrees/b/helper.py": [("add", "minus")]})
        },
    )
    await run_turns(session, rec)
    assert decisions(rec) == ["deny"]
    assert "honesty: review started" in logs(rec)
    (mark,) = [m for state in session._states.values() for m in state.open_marks]  # pyright: ignore[reportPrivateUsage]
    assert mark.label() == ".claude/worktrees/b/helper.py:add"


async def test_u6_a_commit_merged_from_a_worktree_is_verified_in_the_main_checkout(
    repo: Path,
) -> None:
    from tests.unit.test_honesty_session import COMMIT, logs, run_turns, session_on

    worktree(repo, "wta")
    session, rec, _store = await session_on(
        repo,
        [
            [
                write_in(".claude/worktrees/wta", "helper.py", HONEST),
                {
                    "tool": "Bash",
                    "input": {"command": "git commit -am change"},
                    "cwd": ".claude/worktrees/wta",
                    "run": ["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"],
                    "result": "ok",
                },
                {
                    "tool": "Bash",
                    "input": {"command": "git merge worktree-wta"},
                    "result": "ok",
                },
                run_in(""),
            ]
        ],
    )

    # The fake runner runs one program per call: the commit and the merge are made by git
    # itself right before the merge step is judged.
    original = session.pre_tool_use

    async def spy(step: Step):  # type: ignore[no-untyped-def]
        command = str(step.tool_input.get("command", ""))
        if command == "git merge worktree-wta":
            git(repo / ".claude" / "worktrees" / "wta", *COMMIT[5:])
            git(repo, "merge", "-q", "--ff-only", "worktree-wta")
        return await original(step)

    session.pre_tool_use = spy  # type: ignore[method-assign]
    await run_turns(session, rec)
    assert (repo / "helper.py").read_text() == HONEST
    assert decisions(rec)[-1] == "pass"
    assert "honesty: review started" not in logs(rec)
    state = await session.honesty_snapshot()
    assert state.clean, state


async def test_u7_a_copy_of_a_verified_worktree_file_is_verified_without_a_head_move(
    repo: Path,
) -> None:
    from tests.unit.test_honesty_session import logs, run_turns, session_on

    worktree(repo, "wta")
    session, rec, _store = await session_on(
        repo,
        [
            [
                write_in(".claude/worktrees/wta", "helper.py", HONEST),
                {
                    "tool": "Bash",
                    "input": {"command": "cp .claude/worktrees/wta/helper.py helper.py"},
                    "run": ["cp", ".claude/worktrees/wta/helper.py", "helper.py"],
                    "result": "ok",
                },
                run_in(""),
            ]
        ],
    )
    await run_turns(session, rec)
    assert decisions(rec)[-1] == "pass"
    assert "honesty: review started" not in logs(rec)


async def test_files_of_an_outside_worktree_count_only_with_a_persons_certification(
    repo: Path,
) -> None:
    """The owner's decision: with honesty on, an outside worktree needs "user" trust."""
    import anyio

    from calu.protocol.messages import ChatSendParams, EscalationRequest, TurnResult
    from tests.unit.test_honesty_session import session_on

    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    session, rec, _store = await session_on(
        repo,
        [[run_in(str(outside)), write_in(str(outside), "helper.py", HONEST), run_in(str(outside))]],
    )
    reasons: list[str] = []
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        asked = (await rec.wait_for(EscalationRequest))[0]
        reasons.append(asked.reason)
        await session.decide_escalation(asked.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()
    # HEAD is certified by a person, so the untouched worktree runs; after a checked edit
    # its file is verified only by the checkers, which does not count there.
    assert decisions(rec) == ["pass", "pass", "escalate"]
    assert "outside the project directory" in reasons[0]
    assert str(outside.resolve()) in reasons[0]


async def test_with_honesty_off_an_outside_worktree_gets_nothing_of_calus_own(
    repo: Path,
) -> None:
    from tests.unit.test_honesty_session import run_turns, session_on

    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    session, rec, _store = await session_on(
        repo,
        [[write_in(str(outside), "helper.py", HONEST), run_in(str(outside))]],
        mode="off",
    )
    await run_turns(session, rec)
    assert decisions(rec) == ["pass", "pass"]


def test_the_states_of_several_checkouts_combine_for_a_step_and_for_the_overview() -> None:
    from calu.honesty import Baseline, HonestySnapshot, Unreviewed, combine_snapshots

    base = Baseline("1" * 40, "2" * 40, "user")
    main = HonestySnapshot(base, (), Unreviewed(paths=("a.py",)), False)
    worktree_part = HonestySnapshot(base, (), Unreviewed(paths=(".claude/worktrees/w/b.py",)), True)
    outside = HonestySnapshot(None, (), Unreviewed(everything=True), False)
    for_step = combine_snapshots([main, worktree_part])
    assert for_step.baseline == base and for_step.review_required
    assert for_step.unreviewed.paths == (".claude/worktrees/w/b.py", "a.py")
    assert combine_snapshots([main, outside]).baseline is None
    assert combine_snapshots([main, outside]).unreviewed.everything
    overview = combine_snapshots([main, outside], [".", "/tmp/x"])
    assert overview.baseline == base and not overview.unreviewed.everything
    assert overview.unreviewed.paths == ("/tmp/x", "a.py") and not overview.clean
