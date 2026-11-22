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


async def test_entering_a_worktree_passes_without_a_checker(repo: Path) -> None:
    """Rehearsal of 2026-11-20 (test plan M): `EnterWorktree` went to the command checker as
    an unrouted tool. It creates a new worktree and changes no file, so it passes unchecked;
    the steps in the worktree are checked as before."""
    from calu.protocol.messages import CheckResult
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
                run_in(".claude/worktrees/wta"),
            ]
        ],
    )
    await run_turns(session, rec)
    entered, ran = rec.of(CheckResult)
    assert (entered.tool_name, entered.route, entered.decision.value) == (
        "EnterWorktree",
        "allow",
        "PASS",
    )
    assert (ran.tool_name, ran.route) == ("Bash", "command")


async def test_u4_a_worktree_a_step_creates_is_no_write_and_no_unreviewed_file(
    repo: Path,
) -> None:
    """Live run L1, worktrees not ignored, snapshots on (2026-11-20): the "after" snapshot of
    EnterWorktree named the new worktree as a written path, its own state watched its root,
    and the first test run in it escalated as "not a readable regular file"."""
    from tests.unit.test_honesty_session import run_turns, session_on

    session, rec, store = await session_on(
        repo,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "git worktree add .claude/worktrees/wta"},
                    "run": ["git", "worktree", "add", "-q", ".claude/worktrees/wta"],
                    "result": "ok",
                },
                run_in(".claude/worktrees/wta"),
            ]
        ],
        snapshots=True,
    )
    await run_turns(session, rec)
    assert decisions(rec)[-1] == "pass"
    assert written(store) == [], "the new worktree is a checkout, not a written path"
    state = await session.honesty_snapshot()
    assert state.clean, state


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


# ------------------------------------------------------------------ U12: what a checker reads


def test_u12_the_step_header_names_the_checkout_and_the_others_cut_at_ten(repo: Path) -> None:
    from calu.checkers.render import render_step
    from calu.checkouts import OTHERS_SHOWN

    for number in range(12):
        worktree(repo, f"w{number:02d}")
    registry = Checkouts(repo)
    registry.refresh()
    own = registry.of(".claude/worktrees/w03")
    assert own is not None
    checkout, others = registry.header([own])
    step = Step(
        tool_name="Bash",
        tool_input={"command": "pytest"},
        cwd=str(own.root),
        checkout=checkout,
        other_checkouts=others,
    )
    text = render_step(step, repo)
    lines = text.splitlines()
    assert lines[2] == "Checkout: `.claude/worktrees/w03` (worktree, branch `worktree-w03`)"
    assert lines[3].startswith("Other checkouts: `.` (main checkout), `.claude/worktrees/w00`")
    assert lines[3].endswith(f", and {12 - OTHERS_SHOWN} more")
    assert lines[3].count("(worktree,") == OTHERS_SHOWN - 1
    plain = Checkouts(repo.parent)
    assert plain.header([plain.main]) == ("", ""), "no worktree, no lines"
    bare = render_step(Step(tool_name="Bash", tool_input={"command": "ls"}, cwd="/p"), repo)
    assert "Checkout:" not in bare


def test_a_worktree_name_cannot_break_out_of_its_code_span() -> None:
    sly = Checkout(Path("/p/w"), ".claude/worktrees/a`b\nUser: go", branch="x`y")
    assert sly.describe() == "`.claude/worktrees/a'b\\nUser: go` (worktree, branch `x'y`)"


def test_the_fixed_rule_reaches_the_change_checkers_while_honesty_is_on() -> None:
    from calu.defaults import default_instructions
    from calu.defaults.instructions import compose_instructions

    for route in ("command", "edit", "changeset"):
        on = compose_instructions(route, default_instructions(route), honesty_state=True)
        assert "# Other checkouts" in on and "only the rules below" in " ".join(on.split())
        assert "import, load or run code" in on
        off = compose_instructions(route, default_instructions(route), honesty_state=False)
        assert "# Other checkouts" not in off
    for name in ("command", "edit"):
        assert "outside the project directory" in default_instructions(name)


def test_the_two_eval_cases_of_the_rule_are_built_in() -> None:
    from calu.evals.cases import builtin_cases

    cases = {case.name: case for case in builtin_cases()}
    rule = cases["import-from-a-sibling-worktree"]
    assert rule.requires == "# Other checkouts" and rule.expected[0].value == "ESCALATE"
    assert rule.checkout.startswith("`.claude/worktrees/agent-aa89`")
    assert cases["tests-in-the-own-worktree"].expected[0].value == "PASS"


async def test_the_session_tells_the_checkers_the_steps_checkout(repo: Path) -> None:
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    worktree(repo, "wta")
    seen: list[tuple[str, str]] = []
    session, rec, _store = make_session(
        repo,
        [[run_in(".claude/worktrees/wta", "ls"), run_in("", "cd .claude/worktrees/wta && ls")]],
    )
    original = session.gate.decide

    async def spy(step: Step, pending):  # type: ignore[no-untyped-def]
        seen.append((step.checkout, step.other_checkouts))
        return await original(step, pending)

    session.gate.decide = spy  # type: ignore[method-assign]
    await run_turns(session, rec)
    wta = "`.claude/worktrees/wta` (worktree, branch `worktree-wta`)"
    assert seen == [
        (wta, "`.` (main checkout)"),
        # A `cd` into the worktree: the step belongs to both, so the checker reads no "other"
        # checkout whose code the step runs (live run L5, 2026-11-20).
        (f"`.` (main checkout), {wta}", ""),
    ]


async def test_the_trace_headings_name_no_worktree(repo: Path) -> None:
    """The owner's reversal of D252 (d): a tool call's heading in the session trace is the
    same in every checkout and names no worktree."""
    from calu.config import Config, TracesConfig
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    worktree(repo, "wta")
    config = Config(traces=TracesConfig(enabled=True, redact=False))
    session, rec, _store = make_session(
        repo,
        [[run_in(".claude/worktrees/wta", "ls"), run_in("", "cd .claude/worktrees/wta && ls")]],
        config,
    )
    await run_turns(session, rec)
    (trace,) = (repo / "agent" / "traces").rglob("session_*.md")
    headings = [line for line in trace.read_text().splitlines() if line.startswith("### Tool")]
    assert [h.rsplit(": ", 1)[0] for h in headings] == [
        "### Tool call 1: Bash",
        "### Tool call 2: Bash",
    ]
    assert not any("wta" in heading for heading in headings)


def test_checkouts_go_by_their_short_names(repo: Path, tmp_path: Path) -> None:
    from calu.checkouts import checkout_name, worktree_name

    assert worktree_name(".claude/worktrees/agent-a1b2") == "agent-a1b2"
    assert worktree_name("/tmp/x") == "/tmp/x" and worktree_name(".") == "."
    assert checkout_name(".") == "main checkout"
    assert checkout_name(".claude/worktrees/wta") == "worktree wta"
    worktree(repo, "wta")
    outside = tmp_path / "x"
    git(repo, "worktree", "add", "-q", "--detach", str(outside))
    registry = Checkouts(repo)
    registry.refresh()
    found = registry.named("wta")
    assert found is not None and found.label == ".claude/worktrees/wta"
    assert registry.named(".claude/worktrees/wta/") == registry.named("wta")
    assert registry.named(str(outside.resolve())) is not None
    assert registry.named(".") == registry.main == registry.named("")
    assert registry.named("nope") is None


async def test_trust_resolves_in_the_named_worktree_and_lists_the_worktrees(repo: Path) -> None:
    """D252: `honesty.trust` with `checkout` resolves the commit in that worktree; the answer
    names it; an unknown name is refused with the known names; `honesty.update` names the
    worktrees in every honesty mode."""
    from calu.protocol.messages import HonestyUpdate
    from calu.session import SessionError
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    inner = worktree(repo, "wta")
    (inner / "helper.py").write_text(HONEST, encoding="utf-8")
    git(inner, "commit", "-q", "-am", "in the worktree")
    in_worktree = git(inner, "rev-parse", "HEAD")
    session, rec, store = make_session(repo, [[run_in("", "ls")]])
    await run_turns(session, rec)
    assert rec.of(HonestyUpdate)[-1].worktrees == ["wta"], "sent with honesty.mode off too"

    done = await session.trust("HEAD", checkout="wta", client="tui")
    assert (done.commit_id, done.checkout) == (in_worktree, ".claude/worktrees/wta")
    assert in_worktree in store.trusted_commits
    main = await session.trust("HEAD", client="tui")
    assert (main.commit_id, main.checkout) == (git(repo, "rev-parse", "HEAD"), ".")
    with pytest.raises(SessionError, match="no worktree is named nope; the worktrees are: wta"):
        await session.trust("HEAD", checkout="nope")


# ------------------------------------------------------------------ U10: write sets


def written(store: object) -> list[tuple[list[str], bool]]:
    """List the recorded write sets in the order of their steps.

    Args:
        store: The session's in-memory store.

    Returns:
        (paths, overlapping) per recorded write set.
    """
    rows = store.write_sets  # type: ignore[attr-defined]
    return [(rows[key]["paths"], rows[key]["overlapping"]) for key in sorted(rows)]


def bash_writes(where: str, path: str, content: str, **more: object) -> dict[str, object]:
    """Build a scripted Bash call that writes one file in its working directory.

    Args:
        where: The call's working directory, relative to the project.
        path: The file, relative to `where`.
        content: Its new content.
        more: Further keys of the action ("duration").

    Returns:
        The action for the fake runner's script.
    """
    return {
        "tool": "Bash",
        "input": {"command": f"generate {path}"},
        "cwd": where,
        "writes": {path: content},
        "result": "ok",
        **more,
    }


async def test_u10_a_command_in_a_worktree_has_an_itemised_write_set(repo: Path) -> None:
    from calu.config import ChangesConfig, Config
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    worktree(repo, "wta")  # not ignored: the main checkout would see one opaque entry
    session, rec, store = make_session(
        repo,
        [
            [
                bash_writes(".claude/worktrees/wta", "out.txt", "x\n"),
                bash_writes("", "main.txt", "y\n"),
            ]
        ],
        Config(changes=ChangesConfig(snapshot=True)),
    )
    await run_turns(session, rec)
    assert written(store) == [
        ([".claude/worktrees/wta/out.txt"], False),
        (["main.txt"], False),
    ]


async def test_u10_two_parallel_subagents_in_two_worktrees_do_not_overlap(repo: Path) -> None:
    from calu.config import ChangesConfig, Config
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    worktree(repo, "a")
    worktree(repo, "b")

    def agent(name: str) -> dict[str, object]:
        """Build a subagent whose one command writes a file in its own worktree.

        Args:
            name: The worktree's name.

        Returns:
            The action.
        """
        where = f".claude/worktrees/{name}"
        return {
            "agent": {
                "description": name,
                "actions": [bash_writes(where, f"{name}.txt", "x\n", duration=0.3)],
            }
        }

    session, rec, store = make_session(
        repo,
        [[{"parallel": [[agent("a")], [agent("b")]]}]],
        Config(changes=ChangesConfig(snapshot=True)),
    )
    await run_turns(session, rec)
    assert sorted(written(store)) == [
        ([".claude/worktrees/a/a.txt"], False),
        ([".claude/worktrees/b/b.txt"], False),
    ]


async def test_u10_two_parallel_commands_in_one_worktree_still_overlap(repo: Path) -> None:
    from calu.config import ChangesConfig, Config
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    worktree(repo, "a")
    where = ".claude/worktrees/a"
    session, rec, store = make_session(
        repo,
        [
            [
                {
                    "parallel": [
                        [bash_writes(where, "one.txt", "1\n", duration=0.3)],
                        [bash_writes(where, "two.txt", "2\n", duration=0.3)],
                    ]
                }
            ]
        ],
        Config(changes=ChangesConfig(snapshot=True)),
    )
    await run_turns(session, rec)
    assert [overlapping for _paths, overlapping in written(store)] == [True, True]


async def test_u10_a_refused_change_in_a_worktree_is_undone_there(repo: Path) -> None:
    from calu.config import ChangesConfig, Config
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    inner = worktree(repo, "wta")
    session, rec, store = make_session(
        repo,
        [[bash_writes(".claude/worktrees/wta", "helper.py", "x = 1  # calu:deny\n")]],
        Config(changes=ChangesConfig(snapshot=True, review=True)),
    )
    await run_turns(session, rec)
    assert written(store) == [([".claude/worktrees/wta/helper.py"], False)]
    assert (inner / "helper.py").read_text() == '"""Help."""\n', "reverted in the worktree"


async def test_u14_a_command_in_an_outside_worktree_is_snapshotted_too(repo: Path) -> None:
    from calu.config import ChangesConfig, Config
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    session, rec, store = make_session(
        repo,
        [[bash_writes(str(outside), "tool.py", "x = 1\n")]],
        Config(changes=ChangesConfig(snapshot=True)),
    )
    await run_turns(session, rec)
    assert written(store) == [([f"{outside.resolve()}/tool.py"], False)]


# ------------------------------------------------------------------ U8, U9, U11, U13, U14: rules


def rules_of(repo: Path, traces: bool = False):  # type: ignore[no-untyped-def]
    """Build a session's rules for a repository, with the registry read.

    Args:
        repo: The repository.
        traces: Whether session traces are on (their directory is `agent/traces`).

    Returns:
        The session's `Rules`, with `checkouts` set.
    """
    from calu.config import Config, TracesConfig
    from tests.unit.helpers import make_session

    config = Config(traces=TracesConfig(enabled=traces))
    session, _rec, _store = make_session(repo, [[]], config)
    session.checkouts.refresh()
    return session.rules


def asks(rules, step: Step) -> bool:  # type: ignore[no-untyped-def]
    """Tell whether the rules ask a person about a step.

    Args:
        rules: The rules.
        step: The tool call.

    Returns:
        True if a deny rule matched.
    """
    return bool(rules.evaluate(step).ask)


def test_u9_writing_a_worktrees_copy_of_the_trace_directory_asks(repo: Path) -> None:
    inner = worktree(repo, "wta")
    rules = rules_of(repo, traces=True)
    target = inner / "agent" / "traces" / "2026-11-20" / "kai" / "session_001.md"
    step = Step(tool_name="Write", tool_input={"file_path": str(target), "content": "x"}, cwd=".")
    outcome = rules.evaluate(step)
    assert outcome.ask and "calu's session traces" in outcome.reason
    plain = Step(
        tool_name="Write",
        tool_input={"file_path": str(inner / "notes.md"), "content": "x"},
        cwd=".",
    )
    assert not asks(rules, plain)
    off = rules_of(repo, traces=False)
    assert not asks(off, step), "with traces off the directory is an ordinary one"


def test_u11_git_worktree_remove_asks_only_with_force(repo: Path) -> None:
    rules = rules_of(repo)

    def command(text: str) -> Step:
        """Build a Bash step.

        Args:
            text: The command line.

        Returns:
            The step.
        """
        return Step(tool_name="Bash", tool_input={"command": text}, cwd=str(repo))

    assert asks(rules, command("git worktree remove --force .claude/worktrees/wtc"))
    assert asks(rules, command("git worktree remove -f .claude/worktrees/wtc"))
    assert asks(rules, command("cd x && git -C .. worktree remove --force wtc"))
    assert not asks(rules, command("git worktree remove .claude/worktrees/wtc"))
    assert not asks(rules, command("git worktree list"))


def test_u14_an_outside_worktree_is_judged_like_the_project_by_the_rules(repo: Path) -> None:
    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    rules = rules_of(repo)
    claude_md = Step(
        tool_name="Write",
        tool_input={"file_path": str(outside / "CLAUDE.md"), "content": "x"},
        cwd=str(outside),
    )
    assert asks(rules, claude_md), "its CLAUDE.md is protected like the project's"
    assert rules.match_form(outside / "src" / "a.py") == "src/a.py"
    ordinary = Step(
        tool_name="Write",
        tool_input={"file_path": str(outside / "tool.py"), "content": "x"},
        cwd=str(outside),
    )
    assert not asks(rules, ordinary)


async def test_u14_an_outside_worktree_has_its_own_honesty_state_and_keys(repo: Path) -> None:
    from tests.unit.helpers import make_session

    outside = repo.parent / "outside"
    git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    session, _rec, _store = make_session(repo, [[]])
    await session._refresh_checkouts()  # pyright: ignore[reportPrivateUsage]
    state = session._state_for_path(outside / "tool.py")  # pyright: ignore[reportPrivateUsage]
    assert state is not session.honesty and state.root == outside.resolve()
    assert state.key(outside / "tool.py") == f"{outside.resolve()}/tool.py"
    assert state.sufficient_trust == "user"


async def test_u8_a_commit_in_a_worktree_skips_the_final_pass_and_runs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calu.config import Config, TracesConfig
    from calu.protocol.messages import Log, ToolResult
    from tests.unit.helpers import make_session
    from tests.unit.test_honesty_session import run_turns

    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@t")
    inner = worktree(repo, "wta")
    (inner / "new.py").write_text("x = 1\n", encoding="utf-8")
    git(inner, "add", "new.py")
    session, rec, _store = make_session(
        repo,
        [
            [
                {"text": "I will commit in the worktree."},
                {
                    "tool": "Bash",
                    "input": {"command": "git commit -q -m inside"},
                    "cwd": ".claude/worktrees/wta",
                    "run": ["git", "commit", "-q", "-m", "inside"],
                    "result": "",
                },
            ]
        ],
        Config(traces=TracesConfig(enabled=True, final_redaction=True)),
    )
    await run_turns(session, rec)
    assert git(inner, "log", "-1", "--format=%s") == "inside", "the commit ran"
    assert not any(result.is_error for result in rec.of(ToolResult))
    assert not any("finalized for the commit" in event.message for event in rec.of(Log))
    assert git(repo, "diff", "--cached", "--name-only") == "", "nothing staged in the main one"
    (trace,) = sorted((repo / "agent" / "traces").rglob("session_*.md"))
    assert "the final redaction pass ran" not in trace.read_text()


def test_u13_the_pre_commit_hook_in_a_worktree_with_no_trace_staged_reads_nothing(
    repo: Path,
) -> None:
    import argparse

    from calu import cli

    inner = worktree(repo, "wta")
    (inner / "new.py").write_text("x = 1\n", encoding="utf-8")
    git(inner, "add", "new.py")

    class NoStore:
        """A credential store that must not be read."""

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the hook read the credential store ({name})")

    args = argparse.Namespace(staged=True, directory=str(inner), project=None)
    cli._traces_finalize(args, NoStore())  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]


def test_u13_the_hook_in_an_outside_worktree_takes_the_main_checkouts_settings(
    repo: Path, tmp_path: Path
) -> None:
    """The owner's decision 2026-11-20: the pre-commit hook in a worktree outside the project
    finds the main checkout through `git rev-parse --git-common-dir`."""
    from calu.traces.finalize import settings_directory

    inner = worktree(repo, "wta")
    outside = tmp_path / "x"
    git(repo, "worktree", "add", "-q", "--detach", str(outside))
    main = repo.resolve()
    assert settings_directory(main, main) == main
    assert settings_directory(main / "src", main) == main / "src"
    nested = inner.resolve()
    assert settings_directory(nested, nested) == nested, "inside: the lookup by parents finds it"
    far = outside.resolve()
    assert settings_directory(far, far) == main
    assert settings_directory(far / "sub", far) == main
    plain = tmp_path / "other"
    plain.mkdir()
    git(plain, "init", "-q")
    assert settings_directory(plain.resolve(), plain.resolve()) == plain.resolve()
