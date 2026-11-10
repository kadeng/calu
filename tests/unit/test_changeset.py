"""The changeset checker: recognising a commit, collecting its diff, and routing to it."""

import subprocess
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from calu.checkers import Route
from calu.checkers.changeset import changeset, commit_options, is_commit
from calu.protocol.messages import (
    ChatSendParams,
    CheckResult,
    EscalationRequest,
    ToolResult,
    TurnResult,
)
from tests.unit.helpers import default_settings, make_session

pytestmark = pytest.mark.anyio


def repository(tmp_path: Path) -> Path:
    """Create a git repository with one commit and a staged change, inside `tmp_path`."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "first")
    (root / "app.py").write_text("def hello():\n    return 2\n", encoding="utf-8")
    (root / "new.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    return root


def git(workspace: Path, *arguments: str) -> None:
    """Run a git command in the workspace."""
    subprocess.run(["git", "-C", str(workspace), *arguments], check=True)


def test_a_commit_is_recognised_in_its_common_spellings(tmp_path: Path):
    assert is_commit("git commit -m 'x'", tmp_path)
    assert is_commit("git commit", tmp_path)
    assert is_commit("git add -A && git commit -m 'x'", tmp_path)
    assert is_commit("git -C /w commit -m 'x'", tmp_path)
    assert is_commit("git commit -m \"$(cat <<'EOF'\nmessage\nEOF\n)\"", tmp_path)
    assert not is_commit("git status", tmp_path)
    assert not is_commit("git log --oneline", tmp_path)
    assert not is_commit("echo git commit", tmp_path)  # the word, not the command
    assert not is_commit("git committed-typo", tmp_path)


def test_the_options_that_change_the_diff_are_read():
    assert commit_options("git commit -m x") == (False, False)
    assert commit_options("git commit -am x") == (True, False)
    assert commit_options("git commit -a -m x") == (True, False)
    assert commit_options("git commit --all -m x") == (True, False)
    assert commit_options("git commit --amend --no-edit") == (False, True)
    assert commit_options("git commit -a --amend") == (True, True)


async def test_the_changeset_is_the_staged_diff(tmp_path: Path):
    root = repository(tmp_path)
    staged = await changeset(root, "git commit -m 'second'")
    assert "app.py" in staged and "-    return 1" in staged and "+    return 2" in staged
    assert "new.py" not in staged  # untracked, so not part of this commit
    # `-a` adds the tracked changes that were not staged; here there are none beyond app.py.
    everything = await changeset(root, "git commit -am 'second'")
    assert "app.py" in everything


async def test_an_empty_or_unavailable_changeset_says_so(tmp_path: Path):
    root = repository(tmp_path)
    git(root, "reset", "-q")
    assert await changeset(root, "git commit -m x") == ""
    assert (await changeset(tmp_path / "nowhere", "git commit -m x")).startswith("(")
    plain = tmp_path / "plain"  # a directory outside any repository
    plain.mkdir()
    assert (await changeset(plain, "git commit -m x")).startswith("(")


async def test_a_commit_goes_to_the_changeset_checker_with_its_diff(tmp_path: Path):
    root = repository(tmp_path)
    session, rec, store = make_session(
        root, [[{"tool": "Bash", "input": {"command": "git commit -m 'second'"}, "result": "ok"}]]
    )
    seen: list[str] = []
    # A route carries a list of checkers (D81); the route's own is the first.
    (checker,) = session.checkers[Route.CHANGESET]
    original = checker.build_prompt  # keep the fake model, but record the prompt it gets

    def build_prompt(step, context, extra=""):
        """Record the extra block and build the prompt as usual."""
        seen.append(extra)
        return original(step, context, extra)

    checker.build_prompt = build_prompt  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="commit it"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (check,) = rec.of(CheckResult)
    assert check.route == "changeset" and check.decision == "PASS"
    assert seen and "# Changeset of this commit" in seen[0]
    assert "+    return 2" in seen[0]
    (step,) = store.steps.values()
    assert step.checker_runs[0].checker_name == "changeset"


async def test_a_denied_changeset_stops_the_commit(tmp_path: Path):
    root = repository(tmp_path)
    # The fake checker answers by markers in the step, so the command carries one.
    session, rec, _ = make_session(
        root,
        [[{"tool": "Bash", "input": {"command": "git commit -m 'calu:deny me'"}, "result": "x"}]],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="commit it"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (check,) = rec.of(CheckResult)
    assert check.route == "changeset" and check.decision == "DENY"
    (result,) = rec.of(ToolResult)
    assert result.is_error


async def test_without_a_changeset_checker_a_commit_goes_to_the_command_checker(tmp_path: Path):
    root = repository(tmp_path)
    settings = default_settings(root)
    without = {name: d for name, d in settings.checkers.items() if name != "changeset"}
    session, rec, _ = make_session(
        root,
        [[{"tool": "Bash", "input": {"command": "git commit -m 'second'"}, "result": "ok"}]],
        settings=replace(settings, checkers=without),
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="commit it"))
        await rec.wait_for(TurnResult)
        await session.stop()
    (check,) = rec.of(CheckResult)
    assert check.route == "command"


async def test_an_escalated_commit_carries_its_changeset_for_the_operator(tmp_path: Path):
    root = repository(tmp_path)
    session, rec, store = make_session(
        root,
        [
            [
                {
                    "tool": "Bash",
                    "input": {"command": "git commit -m 'calu:escalate this'"},
                    "result": "ok",
                }
            ]
        ],
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="commit it"))
        request = (await rec.wait_for(EscalationRequest))[0]
        stored = store.contexts[int(request.escalation_id)]
        assert "# Changeset of this commit" in stored["changeset"]
        assert "+    return 2" in stored["changeset"]
        await session.decide_escalation(request.escalation_id, "deny", "not yet")
        await rec.wait_for(TurnResult)
        await session.stop()
