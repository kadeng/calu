"""The final redaction pass before a commit and the pre-commit hook (PLAN.md D246)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu.defaults.instructions import final_redaction_instructions
from calu.testing import _last_prompt, excerpt_of
from calu.traces import finalize as fin
from calu.traces import hook
from calu.traces.redact import Secrets

pytestmark = pytest.mark.anyio

EMAIL = re.compile(r"[a-z]+@example\.org")

HEADER = "# calu session trace\n\n- calu session: s1\n\n---\n\n"


@pytest.fixture(autouse=True)
def quiet_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep git away from the host's configuration and from an index set outside the test."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def git(repo: Path, *args: str) -> str:
    """Run git in the test repository and return its output."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path: Path) -> Path:
    """Create a repository with one commit that holds no trace."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "start")
    return repo


TRACE = "agent/traces/2026-11-19/kai/session_001.md"


class Agent:
    """A stand-in redaction agent: replaces e-mail addresses and records what it read."""

    def __init__(self, answer=None, fail: Exception | None = None) -> None:
        self.read: list[str] = []
        self.answer = answer
        self.fail = fail

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            prompt = _last_prompt(messages)
            excerpt = excerpt_of(prompt)
            self.read.append(excerpt)
            if self.fail is not None:
                raise self.fail
            text = self.answer(excerpt) if self.answer else EMAIL.sub("[redacted: e-mail]", excerpt)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": text})])

        return FunctionModel(respond, model_name="fake-final")

    def redactor(self):
        return fin.final_redactor(final_redaction_instructions(), self.model(), 30)


def write(repo: Path, text: str) -> Path:
    path = repo / TRACE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


async def test_a_first_commit_redacts_the_whole_new_trace(tmp_path: Path):
    repo = make_repo(tmp_path)
    path = write(repo, HEADER + "### User\n\n> mail jane@example.org\n\n")
    git(repo, "add", TRACE)
    agent = Agent()

    outcome = await fin.finalize(repo, "staged", agent.redactor(), Secrets())

    assert not outcome.failures
    (result,) = outcome.results
    assert result.staged and result.redacted_chars > 0
    assert [r.strip() for r in agent.read] == [HEADER + "### User\n\n> mail jane@example.org"]
    staged = git(repo, "show", f":{TRACE}")
    assert "jane@example.org" not in staged and "[redacted: e-mail]" in staged
    assert staged == path.read_text()  # the file on disk follows the index
    assert fin.finalized_end(staged) == len(staged)  # the marker closes the file
    assert "the final redaction pass ran up to this line" in staged.splitlines()[-2]


async def test_a_second_commit_redacts_only_the_appended_part(tmp_path: Path):
    repo = make_repo(tmp_path)
    # A committed part that the stand-in would change if it read it again.
    committed = HEADER + "### User\n\n> keep bob@example.org\n\n"
    path = write(repo, committed)
    git(repo, "add", TRACE)
    git(repo, "commit", "-q", "-m", "trace")
    with path.open("a") as file:
        file.write("### Claude\n\n> wrote to ann@example.org\n\n")
    agent = Agent()

    outcome = await fin.finalize(repo, "worktree", agent.redactor(), Secrets(), staging=True)

    assert not outcome.failures
    assert [r.strip() for r in agent.read] == ["### Claude\n\n> wrote to ann@example.org"]
    text = path.read_text()
    assert text.startswith(committed)  # the committed part is untouched
    assert "ann@example.org" not in text and "bob@example.org" in text
    assert git(repo, "show", f":{TRACE}") == text
    # Nothing is left for a second pass before the commit (the hook after Claude's pass).
    assert fin.plans(repo, "staged") == []
    assert fin.plans(repo, "worktree") == []
    git(repo, "commit", "-q", "-m", "more")
    with path.open("a") as file:
        file.write("### User\n\n> next\n\n")
    (again,) = fin.plans(repo, "worktree")
    assert [s.text for s in again.segments if s.redact] == ["### User\n\n> next\n\n"]


async def test_an_unusable_answer_withholds_the_part_and_the_raw_text_is_gone(tmp_path: Path):
    repo = make_repo(tmp_path)
    write(repo, HEADER + "> secret plan of carl@example.org\n\n")
    agent = Agent(answer=lambda excerpt: excerpt * 10)  # longer than any redaction

    outcome = await fin.finalize(repo, "worktree", agent.redactor(), Secrets(), staging=True)

    (result,) = outcome.results
    assert result.withheld == 1
    staged = git(repo, "show", f":{TRACE}")
    assert "carl@example.org" not in staged and fin.WITHHELD_PART in staged
    assert fin.finalized_end(staged) == len(staged)


async def test_an_unreachable_agent_changes_nothing_and_reports_the_file(tmp_path: Path):
    repo = make_repo(tmp_path)
    text = HEADER + "> hello dora@example.org\n\n"
    path = write(repo, text)
    git(repo, "add", TRACE)
    before = git(repo, "show", f":{TRACE}")
    agent = Agent(fail=ConnectionError("no route"))

    outcome = await fin.finalize(repo, "staged", agent.redactor(), Secrets())

    assert outcome.results == []
    ((failed, reason),) = outcome.failures
    assert failed == TRACE and "ConnectionError" in reason
    assert path.read_text() == text and git(repo, "show", f":{TRACE}") == before


async def test_without_the_agent_only_stage_one_runs(tmp_path: Path):
    repo = make_repo(tmp_path)
    write(repo, HEADER + "key AKIAIOSFODNN7EXAMPLE\n\n")
    outcome = await fin.finalize(repo, "worktree", None, Secrets(), staging=False)
    (result,) = outcome.results
    assert not result.staged
    text = (repo / TRACE).read_text()
    assert "AKIA" not in text and fin.finalized_end(text) == len(text)
    assert git(repo, "status", "--porcelain").startswith("??")  # not staged


async def test_a_block_appended_while_the_pass_ran_is_kept_after_it(tmp_path: Path):
    repo = make_repo(tmp_path)
    path = write(repo, HEADER + "> one eve@example.org\n\n")
    (file,) = fin.plans(repo, "worktree")
    text, _, _ = await fin.finalized_text(file, Agent().redactor(), Secrets())
    with path.open("a") as handle:  # the session's writer appends meanwhile
        handle.write("> two\n\n")

    fin.apply(repo, file, text, "worktree", staging=True)

    assert path.read_text() == text + "> two\n\n"
    assert git(repo, "show", f":{TRACE}") == text  # the commit records the finalized part


async def test_a_file_changed_otherwise_while_the_pass_ran_is_refused(tmp_path: Path):
    repo = make_repo(tmp_path)
    path = write(repo, HEADER + "> one\n\n")
    (file,) = fin.plans(repo, "worktree")
    text, _, _ = await fin.finalized_text(file, None, Secrets())
    path.write_text(HEADER + "> edited\n\n")
    with pytest.raises(fin.FinalizeError, match="changed while"):
        fin.apply(repo, file, text, "worktree", staging=True)
    assert path.read_text() == HEADER + "> edited\n\n"


def test_a_file_edited_by_hand_is_redacted_where_it_differs():
    base = HEADER + "> a\n> b\n> c\n\n"
    current = HEADER + "> a\n> B fay@example.org\n> c\n\n> d\n\n"
    segments = fin.segments_of(base, current)
    assert "".join(s.text for s in segments) == current
    assert [s.text for s in segments if s.redact] == ["> B fay@example.org\n", "> d\n\n"]


def test_a_copied_or_edited_marker_does_not_count():
    body = HEADER + "> a\n\n"
    marked = body + fin.marker_for(body)
    assert fin.finalized_end(marked) == len(marked)
    assert fin.finalized_end("> other\n\n" + fin.marker_for(body)) == 0
    assert fin.segments_of(None, marked + "> b\n\n")[-1].text == "> b\n\n"


async def test_files_that_are_no_trace_are_left_alone(tmp_path: Path):
    repo = make_repo(tmp_path)
    other = repo / "notes" / "session_x.md"
    other.parent.mkdir()
    other.write_text("# my notes\n")
    git(repo, "add", "notes")
    assert fin.plans(repo, "staged") == []


# ---------------------------------------------------------------------- the hook


def test_the_hook_is_installed_refused_chained_and_removed(tmp_path: Path):
    repo = make_repo(tmp_path)
    path = hook.hook_path(repo)
    assert "installed" in hook.install(repo, "/opt/calu/bin/calu")
    assert hook.is_calus(path) and os.access(path, os.X_OK)
    assert "'/opt/calu/bin/calu'" not in path.read_text()  # a plain path needs no quotes
    hook.install(repo, "/opt/calu/bin/calu")  # again: replaced, no error
    assert "removed" in hook.remove(repo)
    assert not path.exists()

    path.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(hook.HookError, match="did not write"):
        hook.install(repo, "/opt/calu/bin/calu")
    assert path.read_text() == "#!/bin/sh\necho mine\n"
    with pytest.raises(hook.HookError, match="did not write"):
        hook.remove(repo)

    assert "runs after it" in hook.install(repo, "/opt/calu/bin/calu", chain=True)
    assert path.with_name(hook.CHAINED).read_text() == "#!/bin/sh\necho mine\n"
    hook.remove(repo)
    assert path.read_text() == "#!/bin/sh\necho mine\n"
    assert not path.with_name(hook.CHAINED).exists()


def test_the_hook_follows_core_hooks_path(tmp_path: Path):
    repo = make_repo(tmp_path)
    git(repo, "config", "core.hooksPath", ".githooks")
    hook.install(repo, "calu")
    assert (repo / ".githooks" / "pre-commit").is_file()


def calu_command(tmp_path: Path) -> str:
    """Write a `calu` that runs this checkout's calu with this interpreter."""
    script = tmp_path / "bin" / "calu"
    script.parent.mkdir()
    script.write_text(f'#!/bin/sh\nexec {sys.executable} -m calu "$@"\n')
    script.chmod(0o755)
    return str(script)


@pytest.mark.slow
async def test_the_hook_refuses_an_unfinalized_trace_and_passes_a_finalized_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A commit outside a session: no database login here, so the pass cannot run.

    The test runs `git commit` in a temporary repository below `tmp_path`. The installed hook
    starts the `calu` command in a subprocess. The XDG directories are set below `tmp_path`,
    and `CALU_PLACE_FILE` names a file that does not exist.
    """
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("CALU_PLACE_FILE", "/nonexistent")
    repo = make_repo(tmp_path)
    hook.install(repo, calu_command(tmp_path))
    write(repo, HEADER + "> hi gus@example.org\n\n")
    git(repo, "add", TRACE)

    refused = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "t"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert refused.returncode != 0
    assert "commit is refused" in refused.stderr and "--no-verify" in refused.stderr
    assert git(repo, "log", "--oneline").count("\n") == 1

    # Finalized by hand (here with the Python API and no agent), the hook has nothing to do
    # and needs no login.
    await fin.finalize(repo, "staged", None, Secrets())
    git(repo, "commit", "-q", "-m", "t")
    assert fin.finalized_end(git(repo, "show", f"HEAD:{TRACE}")) > 0
