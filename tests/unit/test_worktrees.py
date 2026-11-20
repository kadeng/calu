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
