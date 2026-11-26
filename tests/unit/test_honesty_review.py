"""The honesty review (specification section 4): the plan, the reviewer agent, the session.

Three layers, all without API keys:

    the plan       `HonestyState.review_plan` on a real temporary git repository: which files,
                   diff or whole file, the whitespace-only shortcut, the cap
    the reviewer   `calu.checkers.honesty_review` with fake models: batches, bounded
                   concurrency, malformed answers, the timeout
    the session    `Session.honesty_review` with the scripted fake runner: the triggers, one
                   review at a time, no repetition while nothing changed, what is stored
"""

from pathlib import Path

import anyio
import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from calu import session as session_module
from calu.checkers import CodeMark, Decision
from calu.checkers.honesty_review import (
    FileVerdict,
    HonestyReviewer,
    ReviewAnswer,
    ReviewFailed,
    ReviewFinding,
    batch_prompt,
    batches,
    verdicts,
)
from calu.defaults.instructions import honesty_rules, review_instructions
from calu.honesty import (
    REVIEW_FILE_BYTES,
    REVIEW_READ_BYTES,
    SMALL_FILE_BYTES,
    Baseline,
    FileReview,
    Finding,
    HonestyState,
    Reviewed,
    ReviewFile,
    resolve_commit,
    whitespace_only,
)
from calu.protocol.messages import ChatSendParams, EscalationRequest, HonestyUpdate, TurnResult
from calu.testing import fake_reviewer, marking_checker
from tests.unit import test_honesty_session as base
from tests.unit.test_honesty_session import (
    CHANGED,
    COMMIT,
    PARSER,
    bash,
    checks,
    git,
    logs,
    run_turns,
    session_on,
    write,
)

pytestmark = pytest.mark.anyio

repo = base.repo
"""The fixture of `test_honesty_session`: a repository with `src/parser.py`, `README.md` and
an ignored `build/`."""


def trusted_state(root: Path) -> HonestyState:
    """Build a state whose baseline is the repository's HEAD, certified by a person.

    Args:
        root: The repository.

    Returns:
        The state.
    """
    state = HonestyState(root)
    head = resolve_commit(root, "HEAD")
    state.baseline = Baseline(head.commit_id, head.tree_id, "user")
    return state


def file(path: str, text: str = "x = 1\n") -> ReviewFile:
    """Build one file of a plan.

    Args:
        path: Its path.
        text: The text for the reviewer.

    Returns:
        The file, shown whole.
    """
    return ReviewFile(path, "0" * 64, text, whole=True)


# ------------------------------------------------------------------ the plan


def test_whitespace_only_ignores_line_ends_and_blank_lines_but_not_indentation() -> None:
    """The shortcut's definition: trailing whitespace, line endings, blank lines."""
    assert whitespace_only("a = 1  \r\n\nb = 2", "a = 1\nb = 2\n")
    assert not whitespace_only("if x:\n    y()\n", "if x:\ny()\n")
    assert not whitespace_only("a = 1\n", "a = 2\n")
    assert not whitespace_only("a b\n", "ab\n")


def test_the_plan_names_the_diff_the_whole_file_and_the_shortcut(repo: Path) -> None:
    """A changed file is shown as its diff, a new file whole; a whitespace-only change needs
    no reviewer; a file that is not text is skipped."""
    (repo / "setup.cfg").write_text("[tool]\nname = x\n", encoding="utf-8")
    git(repo, "add", "setup.cfg")
    git(repo, "commit", "-q", "-m", "cfg")
    state = trusted_state(repo)
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    (repo / "src" / "new.py").write_text("Y = 2\n", encoding="utf-8")
    (repo / "setup.cfg").write_text("[tool]   \nname = x\n\n\n", encoding="utf-8")
    (repo / "logo.bin").write_bytes(b"\xff\xfe\x00")
    (repo / "big.json").write_text("x" * (REVIEW_FILE_BYTES + 1), encoding="utf-8")
    (repo / "huge.json").write_text("x" * (REVIEW_READ_BYTES + 1), encoding="utf-8")

    plan = state.review_plan(300)
    assert plan.count == 6 and not plan.problem
    changed, new = sorted(plan.files, key=lambda one: one.whole)
    assert (changed.path, changed.whole) == ("src/parser.py", False)
    assert changed.text.startswith("--- a/src/parser.py\n+++ b/src/parser.py\n")
    assert '-    return sorted(line.split(","))' in changed.text
    assert (new.path, new.whole, new.text) == ("src/new.py", True, "Y = 2\n")
    assert changed.sha256 == state.content_sha256("src/parser.py")
    assert list(plan.shortcuts) == ["setup.cfg"] and plan.no_code == ()
    assert plan.shortcuts["setup.cfg"].sha256 == state.content_sha256("setup.cfg")
    assert dict(plan.skipped) == {
        "big.json": f"the file is larger than {REVIEW_FILE_BYTES} bytes",
        "huge.json": f"not a readable regular file of at most {REVIEW_READ_BYTES} bytes",
        "logo.bin": "not UTF-8 text",
    }
    assert set(plan.seen) == {one.path for one in plan.files} | set(plan.shortcuts) | set(
        dict(plan.skipped)
    )


def test_files_without_code_are_verified_without_a_model(repo: Path) -> None:
    """The owner's decision of 2026-11-20 (test plan L, open point 1): plain text, data and
    media files are verified unread, at any size; a script, an executable file, a program
    header and a configuration file stay with the reviewer, and so does a marked file."""
    state = trusted_state(repo)
    (repo / "out_b.txt").write_text("b\n", encoding="utf-8")
    (repo / "README.md").write_text("# project, changed\n", encoding="utf-8")
    (repo / "table.CSV").write_text("a,b\n1,2\n", encoding="utf-8")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    (repo / "photo.jpg").write_bytes(b"\xff\xd8" + b"\x00" * (REVIEW_READ_BYTES + 1))
    (repo / "run.txt").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (repo / "tool.txt").write_text("echo hi\n", encoding="utf-8")
    (repo / "tool.txt").chmod(0o755)
    (repo / "fake.png").write_bytes(b"\x7fELF\x02\x01\x01\x00\xff")
    (repo / "settings.json").write_text('{"hooks": ["x"]}\n', encoding="utf-8")
    (repo / "picture.svg").write_text("<svg><script>x()</script></svg>\n", encoding="utf-8")

    plan = state.review_plan(300)
    unread = {"logo.png", "out_b.txt", "photo.jpg", "table.CSV"}
    assert set(plan.no_code) == unread == set(plan.shortcuts)
    for key in unread:
        assert plan.shortcuts[key].sha256 == state.content_sha256(key), key
    assert {one.path for one in plan.files} == {
        "README.md",
        "run.txt",
        "tool.txt",
        "settings.json",
        "picture.svg",
    }
    assert dict(plan.skipped) == {"fake.png": "not UTF-8 text"}

    state.record([CodeMark(path="out_b.txt", symbol="", reason="x")], [], "edit")
    plan = state.review_plan(300)
    assert "out_b.txt" not in plan.no_code
    assert "out_b.txt" in {one.path for one in plan.files}, "a marked file goes to the reviewer"


def test_a_small_change_of_a_large_file_is_reviewed_as_its_diff(repo: Path) -> None:
    """The size limit is for the text the reviewer gets: a lock file of 240 kB with one new
    line is reviewed; the same file rewritten in every line is skipped (with a limit of 200 kB;
    the default, `REVIEW_FILE_BYTES`, takes the whole file)."""
    lock = "".join(f'name = "package-{number}"\n' for number in range(10_000))
    assert 200_000 < len(lock) < REVIEW_FILE_BYTES
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-q", "-m", "lock")
    state = trusted_state(repo)
    state.file_bytes = 200_000

    (repo / "uv.lock").write_text(lock + 'name = "one-more"\n', encoding="utf-8")
    plan = state.review_plan(300)
    (only,) = plan.files
    assert (only.path, only.whole, plan.skipped) == ("uv.lock", False, ())
    assert '+name = "one-more"' in only.text and len(only.text) < 1000
    assert only.sha256 == state.content_sha256("uv.lock")

    (repo / "uv.lock").write_text(lock.replace("package", "module"), encoding="utf-8")
    plan = state.review_plan(300)
    assert plan.files == () and plan.skipped == (
        ("uv.lock", "its change against the baseline is larger than 200000 bytes"),
    )
    state.file_bytes = REVIEW_FILE_BYTES
    (only,) = state.review_plan(300).files
    assert only.whole is False and only.content.startswith('name = "module-0"')


def test_a_change_after_a_verified_edit_is_shown_since_that_content(repo: Path) -> None:
    """The owner, 2026-11-26: reviewing a small change of a large file should not mean reading
    every change since the last trusted commit again. After a checked edit verified a
    content, the next review shows only the change since it; a new session (texts in memory
    only) falls back to the baseline."""
    lock = "".join(f'name = "package-{number}"\n' for number in range(10_000))
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-q", "-m", "lock")
    state = trusted_state(repo)

    rewritten = lock.replace("package", "module")  # every line changed since the baseline
    expected = state.expect_edit("uv.lock", "Write", {"content": rewritten})
    (repo / "uv.lock").write_text(rewritten, encoding="utf-8")
    assert state.record_edit("uv.lock", expected) is not None

    (repo / "uv.lock").write_text(rewritten + 'name = "one-more"\n', encoding="utf-8")
    (only,) = state.review_plan(300).files
    assert (only.whole, only.against) == (False, "verified")
    assert '+name = "one-more"' in only.text and len(only.text) < 2000
    assert only.text.count("\n ") == 20  # `context_lines` lines of context before the change
    assert "since its content was last verified" in batch_prompt([only], "abc")

    state.context_lines = 2
    (narrow,) = state.review_plan(300).files
    assert narrow.text.count("\n ") == 2

    restarted = trusted_state(repo)  # a new session: no texts, nothing verified
    (again,) = restarted.review_plan(300).files
    assert again.against == "baseline" and len(again.text) > 200_000


def test_a_file_in_the_place_of_a_symbolic_link_is_reviewed_whole(repo: Path) -> None:
    """The baseline records a link's target, not a text: no diff and no shortcut apply."""
    (repo / "link.py").symlink_to("src/parser.py")
    git(repo, "add", "link.py")
    git(repo, "commit", "-q", "-m", "link")
    state = trusted_state(repo)
    (repo / "link.py").unlink()
    (repo / "link.py").write_text("src/parser.py\n", encoding="utf-8")
    plan = state.review_plan(300)
    assert plan.shortcuts == {}
    assert [(one.path, one.whole) for one in plan.files] == [("link.py", True)]


def test_a_marked_file_is_reviewed_whole_with_its_marks_and_never_by_the_shortcut(
    repo: Path,
) -> None:
    """A file with an open mark goes to the reviewer also when its content is the baseline's."""
    state = trusted_state(repo)
    state.record([CodeMark(path="src/parser.py", symbol="fields", reason="unsorted")], [], "edit")
    plan = state.review_plan(300)
    (only,) = plan.files
    assert only.whole and only.text == PARSER
    assert [mark.symbol for mark in only.marks] == ["fields"]
    assert plan.shortcuts == {}


def test_more_files_than_the_limit_give_a_plan_that_reads_nothing(repo: Path) -> None:
    """The cap: the plan has the count and the reason, and no file."""
    state = trusted_state(repo)
    for number in range(3):
        (repo / f"new{number}.py").write_text("X = 1\n", encoding="utf-8")
    plan = state.review_plan(2)
    assert plan.count == 3 and plan.files == () and plan.shortcuts == {}
    assert plan.problem == (
        "3 files are unreviewed or marked, and one honesty review reads at most 2 "
        "(honesty.review_max_files)"
    )
    assert not state.review_plan(3).problem


def test_without_a_baseline_the_plan_holds_every_file_git_lists_whole(repo: Path) -> None:
    """No baseline: every tracked and untracked file, whole; ignored files are left out."""
    state = HonestyState(repo)
    (repo / "build").mkdir()
    (repo / "build" / "out.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "notes.txt").write_text("todo\n", encoding="utf-8")
    plan = state.review_plan(300)
    assert plan.baseline is None
    assert [one.path for one in plan.files] == [".gitignore", "README.md", "src/parser.py"]
    assert all(one.whole for one in plan.files)
    assert plan.no_code == ("notes.txt",), "a file without code needs no reviewer"


def test_without_a_baseline_a_verified_file_is_not_part_of_the_next_plan(repo: Path) -> None:
    """No baseline, second review: the plan holds only the files that are not verified or
    carry a mark, and it counts the others instead of naming them as not judged."""
    state = HonestyState(repo)
    first = state.review_plan(300)
    assert first.count == 3 and first.verified_already == 0
    state.apply_review(
        {
            one.path: FileReview(
                one.sha256, (Finding("", "stale"),) if one.path == "README.md" else ()
            )
            for one in first.files
        }
    )
    second = state.review_plan(300)
    assert [one.path for one in second.files] == ["README.md"]
    assert (second.count, second.verified_already, second.skipped) == (1, 2, ())
    assert set(second.seen) == {"README.md"}


def test_a_file_whose_staged_content_alone_is_unreviewed_is_skipped(repo: Path) -> None:
    """The review reads working files; a content only the index holds stays unreviewed."""
    state = trusted_state(repo)
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    git(repo, "add", "src/parser.py")
    (repo / "src" / "parser.py").write_text(PARSER, encoding="utf-8")
    plan = state.review_plan(300)
    assert plan.files == ()
    assert plan.skipped == (
        (
            "src/parser.py",
            "the index holds an earlier content for it that nobody verified; "
            "`git add` of the file settles that",
        ),
    )


def test_the_last_review_covers_a_plan_only_with_the_same_contents_and_marks(
    repo: Path,
) -> None:
    """`Reviewed.covers`: true while a new review would read what the last one looked at;
    false after another content, another mark, a further file, or another baseline."""
    state = trusted_state(repo)
    (repo / "logo.bin").write_bytes(b"\xff\xfe\x00")
    plan = state.review_plan(300)
    assert plan.baseline is not None
    last = Reviewed(plan.baseline.commit_id, False, plan.seen)
    assert last.covers(state.review_plan(300))
    assert not Reviewed("", False, plan.seen).covers(plan)
    assert not Reviewed(plan.baseline.commit_id, True, plan.seen).covers(plan)
    assert not Reviewed(plan.baseline.commit_id, False, {}).covers(plan)

    (repo / "logo.bin").write_bytes(b"\xff\xfe\x01")
    assert not last.covers(state.review_plan(300))
    (repo / "logo.bin").write_bytes(b"\xff\xfe\x00")
    assert last.covers(state.review_plan(300))
    state.record([CodeMark(path="logo.bin", symbol="", reason="stale")], [], "edit")
    assert not last.covers(state.review_plan(300))
    state.clear(state.open_marks[0].id, "tester")
    (repo / "src" / "new.py").write_text("Y = 2\n", encoding="utf-8")
    assert not last.covers(state.review_plan(300))


def test_a_result_for_a_file_that_changed_meanwhile_is_dropped(repo: Path) -> None:
    """A review takes time: a result is applied only to the content and the marks it saw."""
    state = trusted_state(repo)
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    plan = state.review_plan(300)
    results = {one.path: FileReview(one.sha256, marks=()) for one in plan.files}

    (repo / "src" / "parser.py").write_text(CHANGED + "X = 1\n", encoding="utf-8")
    state.record([CodeMark(path="README.md", symbol="", reason="stale")], [], "command")
    changes = state.apply_current_review(results)
    assert changes.ignored == [] and changes.dropped == [
        ("README.md", "its marks changed while the review ran"),
        ("src/parser.py", "the file changed while the review ran"),
    ]
    assert state.seen_now(results) == {}
    assert state.verified == {} and len(state.open_marks) == 1

    plan = state.review_plan(300)
    results = {
        one.path: FileReview(one.sha256, marks=tuple(mark.id for mark in one.marks))
        for one in plan.files
    }
    done = state.apply_current_review(results)
    assert (done.ignored, done.dropped) == ([], [])
    assert state.seen_now(results) == {one.path: (one.sha256, ()) for one in plan.files}
    assert state.snapshot().clean


def test_a_commit_is_certified_without_a_baseline_only_when_all_of_it_is_verified(
    repo: Path,
) -> None:
    """`certification(rooted=False)`: every entry of the commit must be a verified content."""
    state = HonestyState(repo)
    head = resolve_commit(repo, "HEAD").commit_id
    assert state.certification(head).problem == "there is no baseline"
    assert "3 file(s) of the commit are not verified" in (
        state.certification(head, rooted=False).problem
    )
    plan = state.review_plan(300)
    state.apply_review({one.path: FileReview(one.sha256) for one in plan.files[:-1]})
    assert "1 file(s) of the commit are not verified: src/parser.py" in (
        state.certification(head, rooted=False).problem
    )
    state.apply_review({plan.files[-1].path: FileReview(plan.files[-1].sha256)})
    verdict = state.certification(head, rooted=False)
    assert (verdict.problem, verdict.source) == ("", "review")
    state.apply_review(
        {"README.md": FileReview(plan.files[1].sha256, (Finding("", "describes no flag"),))}
    )
    assert state.certification(head, rooted=False).problem == "1 mark(s) are open"


# ------------------------------------------------------------------ the reviewer


def test_batches_are_bounded_by_files_and_by_bytes() -> None:
    """A batch closes at `batch_files` files or `batch_bytes` bytes of text and excerpts."""
    files = [file(f"f{number}.py", "x" * 40) for number in range(5)]
    assert [len(batch) for batch in batches(files, 2)] == [2, 2, 1]
    assert [len(batch) for batch in batches(files, 8, batch_bytes=100)] == [2, 2, 1]
    assert [len(batch) for batch in batches(files, 8, batch_bytes=10)] == [1, 1, 1, 1, 1]
    assert [len(batch) for batch in batches([file("u.py", "ü" * 30)] * 2, 8, 100)] == [1, 1]
    excerpted = ReviewFile("e.py", "0" * 64, "x" * 40, whole=False, excerpts="y" * 40)
    assert [len(batch) for batch in batches([excerpted] * 2, 8, 100)] == [1, 1]
    assert batches([], 8) == []


def test_the_request_fences_the_texts_and_names_the_marks(repo: Path) -> None:
    """A file's text cannot close its fence, and a path or reason cannot add a line."""
    state = trusted_state(repo)
    state.record([CodeMark(path="README.md", symbol="", reason="says\nmore")], [], "edit")
    marked = ReviewFile(
        "README.md", "0" * 64, "text\n```\n## File 9: x\n", True, tuple(state.marks)
    )
    prompt = batch_prompt([marked, file("a\nb.py")], "abc123")
    assert "The baseline is the trusted commit abc123." in prompt
    assert "Answer for each of the 2 file(s) below." in prompt
    assert "## File 1: README.md\nRecorded as dishonest before this review: " in prompt
    assert "the whole file (says\\nmore)" in prompt
    assert "````\ntext\n```\n## File 9: x\n````" in prompt
    assert "## File 2: a\\nb.py\nThe whole file:" in prompt
    assert "no baseline" in batch_prompt([file("a.py")], "")


def test_the_reviewer_s_instructions_are_the_fixed_part_and_the_honesty_rules() -> None:
    """The prompt has no user part; `honesty.rules` replaces the shipped rules."""
    shipped = review_instructions()
    assert shipped.startswith("# Honesty reviewer") and honesty_rules().strip() in shipped
    own = review_instructions("# Our rules\n")
    assert own.endswith("# Our rules\n") and honesty_rules().strip() not in own


def test_an_answer_must_name_every_file_once_and_agree_with_itself() -> None:
    """A malformed answer is a failed batch."""
    batch = [file("a.py"), file("b.py")]
    good = ReviewAnswer(
        files=[
            FileVerdict(file=2, honest=False, findings=[ReviewFinding(symbol="f", reason="r")]),
            FileVerdict(file=1, honest=True),
        ]
    )
    assert verdicts(batch, good) == (
        {
            "a.py": FileReview("0" * 64, marks=()),
            "b.py": FileReview("0" * 64, (Finding("f", "r"),), marks=()),
        },
        [],
    )
    one = FileVerdict(file=1, honest=True)
    for bad in (
        [one],
        [one, one],
        [one, FileVerdict(file=3, honest=True)],
        [one, FileVerdict(file=2, honest=False)],
        [one, FileVerdict(file=2, honest=True, findings=[ReviewFinding(reason="r")])],
        [one, FileVerdict(file=2, honest=False, findings=[ReviewFinding(reason=" ")])],
    ):
        with pytest.raises(ReviewFailed):
            verdicts(batch, ReviewAnswer(files=bad))


def slow_reviewer(running: list[int], delay: float = 0.05, fail_on: str = "") -> Model:
    """Create a reviewer model that takes a while and records how many calls overlap.

    Args:
        running: Gets the number of calls in flight at the start of each call.
        delay: Seconds each call takes.
        fail_on: A text; a request that contains it raises an error.

    Returns:
        A model that answers "honest" for every file of a request.
    """
    active = 0

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Wait, then call every file of the request honest.

        Args:
            messages: The message history.
            info: Agent information; its first output tool receives the answer.

        Returns:
            A response that calls the output tool.

        Raises:
            RuntimeError: For a request that contains `fail_on`.
        """
        nonlocal active
        active += 1
        running.append(active)
        try:
            await anyio.sleep(delay)
        finally:
            active -= 1
        prompt = str(messages[-1])
        if fail_on and fail_on in prompt:
            raise RuntimeError("the model is down")
        count = prompt.count("## File ")
        files = [{"file": number, "honest": True} for number in range(1, count + 1)]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"files": files})])

    return FunctionModel(answer, model_name="slow-reviewer")


async def test_batches_run_at_the_same_time_up_to_the_limit(tmp_path: Path) -> None:
    """Six batches with a limit of two: never more than two model calls in flight."""
    running: list[int] = []
    progress: list[tuple[int, int]] = []

    async def note(done: int, total: int) -> None:
        """Record one progress call.

        Args:
            done: Finished batches.
            total: All batches.
        """
        progress.append((done, total))

    reviewer = HonestyReviewer(
        "judge", slow_reviewer(running), tmp_path, batch_files=1, concurrency=2
    )
    files = [file(f"f{number}.py") for number in range(6)]
    outcome = await reviewer.review(files, "abc", note)
    assert not outcome.problem and sorted(outcome.results) == [one.path for one in files]
    assert max(running) == 2 and len(running) == 6
    assert progress == [(number, 6) for number in range(1, 7)]
    assert (await reviewer.review([])).results == {}


async def test_one_failed_batch_fails_the_whole_review(tmp_path: Path) -> None:
    """A model error in one batch: no file of any batch gets a result."""
    reviewer = HonestyReviewer(
        "judge", slow_reviewer([], fail_on="f3.py"), tmp_path, batch_files=1, concurrency=1
    )
    outcome = await reviewer.review([file(f"f{number}.py") for number in range(5)])
    assert outcome.results == {}
    assert outcome.problem.startswith("The honesty review failed: batch 4 of 5 failed (")
    assert "the model is down" in outcome.problem


def whole_file_asking_reviewer(requests: list[str]) -> Model:
    """Create a reviewer model that asks to read every diff whole, and judges whole files.

    Args:
        requests: Gets the text of each request.

    Returns:
        A model that answers `needs_whole_file` for a file shown as a diff and "honest" for a
        file shown whole.
    """

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Answer per file of the request, by how it is shown.

        Args:
            messages: The message history.
            info: Agent information; its first output tool receives the answer.

        Returns:
            A response that calls the output tool.
        """
        prompt = str(messages[-1])
        requests.append(prompt)
        shown = prompt.split("## File ")[1:]
        files = [
            {"file": number, "honest": True, "needs_whole_file": "as a unified diff" in part}
            for number, part in enumerate(shown, start=1)
        ]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"files": files})])

    return FunctionModel(answer, model_name="whole-file-asking-reviewer")


async def test_a_diff_that_is_not_enough_is_reviewed_whole_in_a_second_round(
    tmp_path: Path,
) -> None:
    """The owner, 2026-11-26: a diff with context is often enough; where it is not, the
    reviewer reads the whole file, and only a whole file too large for it is not judged."""
    requests: list[str] = []
    reviewer = HonestyReviewer(
        "judge", whole_file_asking_reviewer(requests), tmp_path, file_bytes=1_000
    )
    small = ReviewFile("a.py", "1" * 64, "@@ -1 +1 @@\n-x\n+y\n", False, content="y = 1\n")
    large = ReviewFile("b.py", "2" * 64, "@@ -1 +1 @@\n-x\n+z\n", False, content="z" * 2_000)
    shown_whole = file("c.py")
    outcome = await reviewer.review([small, large, shown_whole], "abc")
    assert not outcome.problem
    assert sorted(outcome.results) == ["a.py", "c.py"]
    assert outcome.results["a.py"] == FileReview("1" * 64, marks=())
    assert outcome.unjudged == {
        "b.py": "the reviewer needed the whole file, which is larger than 1000 bytes"
    }
    assert len(requests) == 2
    second = requests[1]  # the request's repr: line breaks are written as \n
    assert "## File 1: a.py" in second and "The whole file:" in second and "y = 1" in second
    assert "b.py" not in second and "c.py" not in second


def test_a_large_marked_file_is_shown_as_its_diff_with_the_marked_definitions(
    repo: Path,
) -> None:
    """A mark on one function of a large file: the change and that function, not the file."""
    body = "".join(f"def helper_{number}():\n    return {number}\n\n\n" for number in range(1500))
    marked = 'class Parser:\n    def fields(self):\n        """Sorted."""\n        return []\n'
    source = body + marked
    assert len(source) > SMALL_FILE_BYTES
    (repo / "big.py").write_text(source, encoding="utf-8")
    git(repo, "add", "big.py")
    git(repo, "commit", "-q", "-m", "big")
    state = trusted_state(repo)
    (repo / "big.py").write_text(source.replace("return []", "return [1]"), encoding="utf-8")
    state.record(
        [CodeMark(path="big.py", symbol="Parser.fields()", reason="not sorted")], [], "edit"
    )
    (only,) = state.review_plan(300).files
    assert only.whole is False and "+        return [1]" in only.text
    assert only.excerpts.startswith("# lines ") and "def fields(self):" in only.excerpts
    assert "def helper_" not in only.excerpts
    assert "The marked definitions as they are now:" in batch_prompt([only], "abc")

    # A mark that names no definition, or one on the whole file: the whole file.
    state.record([CodeMark(path="big.py", symbol="", reason="all of it")], [], "edit")
    (whole,) = state.review_plan(300).files
    assert whole.whole and whole.excerpts == ""


async def test_a_review_over_its_time_fails(tmp_path: Path) -> None:
    """`honesty.review_timeout_s`: a review that takes longer verifies nothing."""
    reviewer = HonestyReviewer("judge", slow_reviewer([], delay=5), tmp_path, timeout_s=0.05)
    outcome = await reviewer.review([file("a.py")])
    assert outcome.results == {}
    assert "took longer than 0.05 seconds (honesty.review_timeout_s)" in outcome.problem


# ------------------------------------------------------------------ the session


async def test_a_whitespace_only_change_is_verified_without_a_model(repo: Path) -> None:
    """Section 4: the deterministic shortcut: no model call, and the command runs."""
    (repo / "src" / "parser.py").write_text(PARSER + "\n\n", encoding="utf-8")
    seen: list[str] = []
    session, rec, store = await session_on(
        repo, [[bash("make test")]], models={"honesty-review": fake_reviewer(seen=seen)}
    )
    await run_turns(session, rec)
    assert seen == []
    assert checks(rec)[0].decision is Decision.PASS
    assert [row.method for row in store.trusted_files.values()] == ["review"]
    assert "1 with a whitespace-only change" in logs(rec)


async def test_a_data_file_is_verified_without_a_model(repo: Path) -> None:
    """Test plan L, open point 1: a command wrote `out_b.txt`; the next code run needs no
    reviewer, and the log names the file type."""
    (repo / "out_b.txt").write_text("b\n", encoding="utf-8")
    seen: list[str] = []
    session, rec, store = await session_on(
        repo, [[bash("make test")]], models={"honesty-review": fake_reviewer(seen=seen)}
    )
    await run_turns(session, rec)
    assert seen == []
    assert checks(rec)[0].decision is Decision.PASS
    assert [row.method for row in store.trusted_files.values()] == ["review"]
    assert "1 without code (plain text, data, media)" in logs(rec)


async def test_the_gate_does_not_repeat_a_review_while_nothing_changed(repo: Path) -> None:
    """Section 4: the second command is denied with the findings of the first review; an
    edit of the file makes the next review run; a person's request always runs one."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    reviewer = fake_reviewer({"src/parser.py": [("fields", "unsorted")]}, seen=seen)
    session, rec, _store = await session_on(
        repo,
        [[bash("make test"), bash("make test"), write("src/parser.py", PARSER + "X = 1\n")]],
        models={"honesty-review": reviewer},
        max_open_marks=0,
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        await rec.wait_for(TurnResult)
        assert [check.decision for check in checks(rec)[:2]] == [Decision.DENY, Decision.DENY]
        assert len(seen) == 1, "the second command did not start a second review"
        assert await session.honesty_review() == ""
        assert len(seen) == 2, "the file was edited since, so the review runs again"
        assert await session.honesty_review() == ""
        assert len(seen) == 2
        assert (await session.request_review("tester")).ran
        assert len(seen) == 3
        await session.stop()


async def test_a_failed_review_is_repeated_by_the_gate_only_after_a_while(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed review judged nothing, so it is not kept as the last review: within
    `REVIEW_RETRY_S` the gate gets the failure again without a model call, afterwards the
    review runs again. A person's request runs one at any time."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    session, _rec, _store = await session_on(
        repo, [[]], models={"honesty-review": fake_reviewer(fails=True, seen=seen)}
    )
    first = await session.honesty_review()
    second = await session.honesty_review()
    assert first.startswith("The honesty review failed: batch 1 of 1 failed")
    assert second.startswith(first) and "`/honesty-review` runs one now" in second
    assert len(seen) == 1
    assert not (await session.request_review()).ran and len(seen) == 2
    assert len(seen) == 2
    monkeypatch.setattr(session_module, "REVIEW_RETRY_S", 0.0)
    assert await session.honesty_review() == first
    assert len(seen) == 3, "after the waiting time the gate's request runs the review again"


def rewriting_reviewer(path: Path, texts: list[str], seen: list[str]) -> Model:
    """Create a reviewer model during whose calls a file changes, as a step would change it.

    Args:
        path: The file to rewrite.
        texts: One content per call, used up from the front; no content is left for later
            calls, which change nothing.
        seen: Gets the request text of every call.

    Returns:
        A model that answers "honest" for every file of a request.
    """
    inner = fake_reviewer(seen=seen)
    assert isinstance(inner, FunctionModel) and inner.function is not None
    judge = inner.function

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Rewrite the file, then answer as `fake_reviewer` does.

        Args:
            messages: The message history.
            info: Agent information.

        Returns:
            The fake reviewer's response.
        """
        if texts:
            path.write_text(texts.pop(0), encoding="utf-8")
        response = judge(messages, info)
        assert isinstance(response, ModelResponse)
        return response

    return FunctionModel(answer, model_name="rewriting-reviewer")


async def test_a_file_that_changed_during_the_review_is_reviewed_by_the_same_request(
    repo: Path,
) -> None:
    """The result for a file that changed while the review ran is dropped, and the request
    runs a further review that reads the new content: the command runs."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    reviewer = rewriting_reviewer(repo / "src" / "parser.py", [CHANGED + "X = 1\n"], seen)
    session, rec, _store = await session_on(
        repo, [[bash("make test")]], models={"honesty-review": reviewer}
    )
    await run_turns(session, rec)
    assert len(seen) == 2 and "+X = 1" not in seen[0] and "+X = 1" in seen[1]
    assert checks(rec)[0].decision is Decision.PASS
    assert "0 file(s) verified, 0 finding(s), 0 file(s) not judged, 1 changed meanwhile" in (
        logs(rec)
    )
    assert (await session.honesty_snapshot()).clean


async def test_a_file_that_keeps_changing_is_not_covered_by_the_last_review(repo: Path) -> None:
    """A request runs at most `REVIEW_PASSES` reviews; its answer names the file that changed
    again, and the next request reviews it instead of giving the last answer again."""
    target = repo / "src" / "parser.py"
    target.write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    texts = [CHANGED + f"X = {number}\n" for number in range(session_module.REVIEW_PASSES)]
    session, _rec, _store = await session_on(
        repo, [[]], models={"honesty-review": rewriting_reviewer(target, texts, seen)}
    )
    problem = await session.honesty_review()
    assert len(seen) == session_module.REVIEW_PASSES
    assert problem == (
        "1 file(s) changed while the honesty review ran: src/parser.py (the file changed "
        "while the review ran). The next review reads them; a step that needs it starts one."
    )
    assert "/trust-file" not in problem
    assert not (await session.honesty_snapshot()).clean
    assert await session.honesty_review() == ""
    assert len(seen) == session_module.REVIEW_PASSES + 1
    assert (await session.honesty_snapshot()).clean


async def test_a_file_that_appeared_during_the_review_starts_the_next_one(repo: Path) -> None:
    """A file that became unreviewed while the review ran was not shown to the reviewer: the
    gate's next request runs a review of it."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    reviewer = rewriting_reviewer(repo / "src" / "new.py", ["Y = 2\n"], seen)
    session, _rec, _store = await session_on(repo, [[]], models={"honesty-review": reviewer})
    assert await session.honesty_review() == ""
    assert len(seen) == 1 and not (await session.honesty_snapshot()).clean
    assert await session.honesty_review() == ""
    assert len(seen) == 2 and "src/new.py" in seen[1]
    assert (await session.honesty_snapshot()).clean
    assert await session.honesty_review() == "" and len(seen) == 2


async def test_a_review_nobody_asked_for_does_not_run_without_a_baseline(repo: Path) -> None:
    """Section 5: without a baseline only a person's request starts a review; the gate's
    request calls no model and certifies no commit."""
    seen: list[str] = []
    session, _rec, store = await session_on(
        repo, [[]], trusted=False, models={"honesty-review": fake_reviewer(seen=seen)}
    )
    problem = await session.honesty_review()
    assert problem.startswith("The honesty review did not run: there is no baseline")
    assert seen == [] and store.trusted_commits == {} and store.trusted_files == {}
    assert session.honesty.verified == {}


async def test_a_second_review_without_a_baseline_names_no_verified_file(repo: Path) -> None:
    """No baseline: the first review finds one file dishonest; the second finds it honest,
    judges only that file, reports no problem, and HEAD is certified."""
    seen: list[str] = []
    session, _rec, store = await session_on(
        repo,
        [[]],
        trusted=False,
        models={
            "honesty-review": fake_reviewer({"README.md": [("", "says nothing true")]}, seen=seen)
        },
    )
    first = await session.request_review("tester")
    assert (first.ran, first.problem) == (True, "") and store.trusted_commits == {}
    session._models["honesty-review"] = fake_reviewer(seen=seen)
    second = await session.request_review("tester")
    assert (second.ran, second.problem) == (True, "")
    assert seen[1].count("## File ") == 1 and "README.md" in seen[1]
    assert list(store.trusted_commits) == [git(repo, "rev-parse", "HEAD")]
    assert (await session.honesty_snapshot()).clean


async def test_a_small_change_of_a_large_file_does_not_stop_a_command(repo: Path) -> None:
    """A committed lock file above the reviewer's text limit gets one more line: the review
    reads the diff, and the command runs."""
    lock = "".join(f'name = "package-{number}"\n' for number in range(10_000))
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-q", "-m", "lock")
    (repo / "uv.lock").write_text(lock + 'name = "one-more"\n', encoding="utf-8")
    seen: list[str] = []
    session, rec, _store = await session_on(
        repo, [[bash("make test")]], models={"honesty-review": fake_reviewer(seen=seen)}
    )
    await run_turns(session, rec)
    assert len(seen) == 1 and len(seen[0]) < 5000 and '+name = "one-more"' in seen[0]
    assert checks(rec)[0].decision is Decision.PASS


async def test_only_one_review_runs_at_a_time(repo: Path) -> None:
    """Section 4: a second request waits for the running review and does not start another."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    running: list[int] = []
    session, _rec, _store = await session_on(
        repo, [[]], models={"honesty-review": slow_reviewer(running, delay=0.2)}
    )
    results: list[bool] = []

    async def ask() -> None:
        """Ask for a review as a person does and keep whether it ran."""
        results.append((await session.request_review()).ran)

    async with anyio.create_task_group() as tg:
        tg.start_soon(ask)
        await anyio.sleep(0.05)
        tg.start_soon(ask)
        tg.start_soon(session.honesty_review)
    assert running == [1] and results == [True, True]
    assert (await session.honesty_snapshot()).clean


async def test_a_passing_review_clears_review_required(repo: Path) -> None:
    """Section 3: with more marks than allowed only a review clears them; it reads the marked
    files, finds them honest, and the command runs."""
    session, rec, _store = await session_on(repo, [[bash("make test")]], max_open_marks=1)
    session.honesty.record(
        [
            CodeMark(path="src/parser.py", symbol="fields", reason="unsorted"),
            CodeMark(path="README.md", symbol="", reason="names a flag that is gone"),
        ],
        [],
        "edit",
    )
    assert session.honesty.review_required
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.PASS
    assert not session.honesty.review_required and not session.honesty.open_marks
    assert rec.of(HonestyUpdate)[-1].clean


async def test_a_review_that_leaves_findings_within_the_threshold_ends_the_requirement(
    repo: Path,
) -> None:
    """Section 4: findings replace the file's marks; afterwards a review is required only
    while more marks are open than `honesty.max_open_marks` (the owner, 2026-11-26: before,
    one finding left kept the requirement, and the next command started a full review
    again). The command is denied with the remaining mark, which an edit settles."""
    reviewer = fake_reviewer({"README.md": [("", "names a flag that is gone")]})
    session, rec, _store = await session_on(
        repo, [[bash("make test")]], models={"honesty-review": reviewer}, max_open_marks=1
    )
    session.honesty.record(
        [
            CodeMark(path="src/parser.py", symbol="fields", reason="unsorted"),
            CodeMark(path="README.md", symbol="", reason="old reason"),
        ],
        [],
        "edit",
    )
    assert session.honesty.review_required  # before the review: two marks, more than one
    await run_turns(session, rec)  # the command starts the review, which leaves one mark
    assert checks(rec)[0].decision is Decision.DENY
    assert "README.md (names a flag that is gone)" in checks(rec)[0].reason
    assert [mark.label() for mark in session.honesty.open_marks] == ["README.md"]
    assert not session.honesty.review_required


async def test_a_review_without_a_baseline_certifies_head_when_all_of_it_is_verified(
    repo: Path,
) -> None:
    """Sections 4 and 5: a person asks for a review of a repository without a trusted commit;
    every file is judged whole, and HEAD is certified with the source "review" because calu
    finds every content it records verified."""
    seen: list[str] = []
    session, rec, store = await session_on(
        repo, [[]], trusted=False, models={"honesty-review": fake_reviewer(seen=seen)}
    )
    done = await session.request_review("tester")
    assert done.ran and not done.problem
    assert "There is no baseline" in seen[0] and seen[0].count("The whole file:") == 3
    head = git(repo, "rev-parse", "HEAD")
    row = store.trusted_commits[head]
    assert (row.method, row.certified_by) == ("review", "calu honesty review")
    update = rec.of(HonestyUpdate)[-1]
    assert (update.baseline, update.baseline_source, update.clean) == (head, "review", True)


async def test_a_review_without_a_baseline_does_not_certify_a_head_with_other_contents(
    repo: Path,
) -> None:
    """HEAD is not certified while a working file differs from what HEAD records, and not
    when the review has findings."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, rec, store = await session_on(repo, [[]], trusted=False)
    assert (await session.request_review()).ran
    assert store.trusted_commits == {}
    assert "is not certified after the review: 1 file(s) of the commit are not verified" in (
        logs(rec)
    )

    (repo / "src" / "parser.py").write_text(PARSER, encoding="utf-8")
    reviewer = fake_reviewer({"README.md": [("", "says nothing true")]})
    session, rec, store = await session_on(
        repo, [[]], trusted=False, models={"honesty-review": reviewer}
    )
    assert (await session.request_review()).ran
    assert store.trusted_commits == {}
    assert rec.of(HonestyUpdate)[-1].baseline is None


async def test_without_a_baseline_the_gate_asks_a_person_and_runs_no_review(repo: Path) -> None:
    """Section 5: no baseline escalates at once; the review runs only when a person asks."""
    seen: list[str] = []
    session, rec, _store = await session_on(
        repo,
        [[bash("make test")]],
        trusted=False,
        models={"honesty-review": fake_reviewer(seen=seen)},
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "/honesty-review" in request.reason and seen == []
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_where_a_review_is_the_least_sufficient_trust_a_checked_edit_is_reviewed(
    repo: Path,
) -> None:
    """Section 13: with `sufficient_trust = review` the edit check does not verify the file;
    the review before the commit does, and the commit is certified as "review"."""
    seen: list[str] = []
    session, rec, store = await session_on(
        repo,
        [
            [
                write("src/parser.py", PARSER + "X = 1\n"),
                bash("git commit -m second", run=["git", "add", "-A"]),
                bash("git commit -m second", run=COMMIT),
            ]
        ],
        models={"honesty-review": fake_reviewer(seen=seen)},
        sufficient_trust="review",
    )
    await run_turns(session, rec)
    assert len(seen) == 1 and "+X = 1" in seen[0]
    head = git(repo, "rev-parse", "HEAD")
    assert store.trusted_commits[head].method == "review"
    assert [row.method for row in store.trusted_files.values()] == ["review"]


async def test_a_file_the_review_cannot_read_as_text_waits_for_a_person(repo: Path) -> None:
    """A file that is not text is not judged: the step is escalated and names it."""
    (repo / "logo.bin").write_bytes(b"\xff\xfe\x00")
    session, rec, _store = await session_on(repo, [[bash("make test")]])
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        await session.send_chat(ChatSendParams(text="go"))
        request = (await rec.wait_for(EscalationRequest))[0]
        assert "could not judge 1 file(s): logo.bin (not UTF-8 text)" in request.reason
        assert "/trust-file PATH" in request.reason
        await session.decide_escalation(request.escalation_id, "deny", "no")
        await rec.wait_for(TurnResult)
        await session.stop()


async def test_progress_of_a_review_reaches_the_clients(repo: Path) -> None:
    """Section 4: clients hear that a review runs (`review_running`), each batch, and its end."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    (repo / "src" / "new.py").write_text("Y = 2\n", encoding="utf-8")
    session, rec, _store = await session_on(repo, [[]], review_batch_files=1)
    assert (await session.request_review("tester")).ran
    running = [update.review_running for update in rec.of(HonestyUpdate)]
    assert True in running and running[-1] is False
    text = logs(rec)
    assert "review started: 2 file(s) to judge (2 by the reviewer in 2 batch(es)" in text
    assert "review: batch 1 of 2 answered" in text and "review: batch 2 of 2 answered" in text
    assert "review finished: 2 file(s) verified, 0 finding(s), 0 file(s) not judged" in text


async def test_track_mode_runs_a_review_only_when_a_person_asks(repo: Path) -> None:
    """Section 5: `track` blocks nothing and starts no review by itself."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    seen: list[str] = []
    session, rec, _store = await session_on(
        repo,
        [[bash("make test")]],
        mode="track",
        models={
            "honesty-review": fake_reviewer(seen=seen),
            "command": marking_checker(executes=("make test",)),
        },
    )
    await run_turns(session, rec)
    assert checks(rec)[0].decision is Decision.PASS and seen == []
    assert (await session.request_review()).ran and len(seen) == 1


async def test_a_review_goes_on_when_its_caller_stops_waiting(repo: Path) -> None:
    """The gate waits a bounded time: the step is told that the review still runs, a second
    caller joins the same review, and the result is applied when the review ends."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    running: list[int] = []
    session, _rec, store = await session_on(
        repo, [[]], models={"honesty-review": slow_reviewer(running, delay=0.3)}
    )
    async with anyio.create_task_group() as tg:
        await session.start(tg)
        first = await session.honesty_review(wait_s=0.02)
        assert first.startswith("The honesty review is still running.")
        assert store.trusted_files == {}
        assert await session.honesty_review() == "", "the second caller waited for it"
        assert running == [1]
        assert (await session.honesty_snapshot()).clean
        await session.stop()
    assert [row.method for row in store.trusted_files.values()] == ["review"]


async def test_a_running_review_ends_with_the_session_and_verifies_nothing(repo: Path) -> None:
    """Stopping the session ends the review in flight; nothing of it is applied."""
    (repo / "src" / "parser.py").write_text(CHANGED, encoding="utf-8")
    session, _rec, store = await session_on(
        repo, [[]], models={"honesty-review": slow_reviewer([], delay=30)}
    )
    answers: list[str] = []

    async def ask() -> None:
        """Ask for the review as the gate does and keep the answer."""
        answers.append(await session.honesty_review())

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await session.start(tg)
            tg.start_soon(ask)
            await anyio.sleep(0.1)
            await session.stop()
    assert answers == ["The honesty review was stopped with the session."]
    assert store.trusted_files == {} and session.honesty.verified == {}
