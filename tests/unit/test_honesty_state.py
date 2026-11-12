"""The honesty state: trusted commits, the baseline, verified contents, marks, the note.

Every test that reads git uses a real repository in a temporary directory. The database is
stood in for by `MemorySessionStore`; `tests/db/test_trust.py` covers the real tables.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from calu.checkers import CodeMark
from calu.config import Config
from calu.honesty import (
    FileReview,
    Finding,
    GitCommit,
    GitError,
    HonestySnapshot,
    HonestyState,
    Unreviewed,
    ancestors,
    content_ids,
    find_baseline,
    match_symbol,
    nearest_trusted,
    normal_symbol,
    resolve_commit,
)
from calu.store import MemorySessionStore

PARSER = '''\
class Parser:
    """Reads fields."""

    def fields(self):
        """Return the fields, sorted."""
        return []

    def run(self):
        """Run the parser."""


def run():
    """Run everything."""
'''


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


def commit(repo: Path, message: str = "change") -> GitCommit:
    """Commit everything in a test repository.

    Args:
        repo: The repository.
        message: The commit message.

    Returns:
        The new HEAD with its tree.
    """
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return GitCommit(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit: `src/parser.py`, `README.md`, and `build/` ignored.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The repository's directory.
    """
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "src" / "parser.py").write_text(PARSER, encoding="utf-8")
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    commit(root, "first")
    return root


def head(repo: Path) -> GitCommit:
    """Return HEAD of a test repository with its tree.

    Args:
        repo: The repository.

    Returns:
        The commit.
    """
    return GitCommit(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}"))


def trusted_state(repo: Path, max_open_marks: int = 3) -> HonestyState:
    """Return a state whose baseline is the repository's HEAD.

    Args:
        repo: The repository.
        max_open_marks: The threshold.

    Returns:
        The state.
    """
    state = HonestyState(repo, max_open_marks)
    state.baseline = head(repo)
    return state


def dirty(path: str, symbol: str = "", reason: str = "the docstring promises more") -> CodeMark:
    """Build a mark as a checker writes it.

    Args:
        path: The file.
        symbol: The symbol.
        reason: The reason.

    Returns:
        The mark.
    """
    return CodeMark(path=path, symbol=symbol, reason=reason)


# ------------------------------------------------------------------------------ the baseline


@pytest.mark.anyio
async def test_the_baseline_is_the_nearest_trusted_ancestor(repo: Path) -> None:
    """No trusted commit: no baseline. Then the nearest of two trusted ancestors wins."""
    store = MemorySessionStore()
    first = head(repo)
    (repo / "README.md").write_text("# two\n", encoding="utf-8")
    second = commit(repo, "second")
    (repo / "README.md").write_text("# three\n", encoding="utf-8")
    third = commit(repo, "third")

    assert await find_baseline(repo, store.trusted_among) is None

    await store.certify_commit(first.commit_id, first.tree_id, "user", "kai")
    assert await find_baseline(repo, store.trusted_among) == first
    await store.certify_commit(second.commit_id, second.tree_id, "session", "calu")
    assert await find_baseline(repo, store.trusted_among) == second
    await store.certify_commit(third.commit_id, third.tree_id, "user", "kai")
    assert await find_baseline(repo, store.trusted_among) == third


@pytest.mark.anyio
async def test_a_commit_with_a_trusted_tree_is_trusted(repo: Path) -> None:
    """Rewording a commit keeps its tree, so the reworded commit is still the baseline."""
    store = MemorySessionStore()
    before = head(repo)
    await store.certify_commit(before.commit_id, before.tree_id, "user", "kai")
    git(repo, "commit", "-q", "--amend", "-m", "first, reworded")
    after = head(repo)
    assert after.commit_id != before.commit_id and after.tree_id == before.tree_id
    assert await find_baseline(repo, store.trusted_among) == after


@pytest.mark.anyio
async def test_the_baseline_search_is_bounded(repo: Path, tmp_path: Path) -> None:
    """A trusted commit beyond the limit is not found, and a directory without git has none."""
    store = MemorySessionStore()
    first = head(repo)
    await store.certify_commit(first.commit_id, first.tree_id, "user", "kai")
    (repo / "README.md").write_text("# two\n", encoding="utf-8")
    commit(repo, "second")
    (repo / "README.md").write_text("# three\n", encoding="utf-8")
    commit(repo, "third")
    assert await find_baseline(repo, store.trusted_among, limit=2) is None
    assert await find_baseline(repo, store.trusted_among, limit=3) == first
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await find_baseline(plain, store.trusted_among) is None
    assert ancestors(plain) == []


def test_nearest_trusted_reads_commit_and_tree_ids() -> None:
    """The first commit in the list that is trusted by either id is the baseline."""
    commits = [GitCommit("c3", "t3"), GitCommit("c2", "t2"), GitCommit("c1", "t1")]
    assert nearest_trusted(commits, {"c1"}, set()) == commits[2]
    assert nearest_trusted(commits, {"c1"}, {"t2"}) == commits[1]
    assert nearest_trusted(commits, set(), set()) is None


def test_a_branch_is_resolved_to_the_commit_it_points_at_now(repo: Path) -> None:
    """`resolve_commit` returns ids, so a later commit on the branch is not covered."""
    first = resolve_commit(repo, "main")
    assert first == head(repo)
    commit(repo, "second")
    assert resolve_commit(repo, "main") != first
    assert resolve_commit(repo, first.commit_id[:10]) == first
    for bad in ("", "no-such-branch", "--all", "main..HEAD"):
        with pytest.raises(GitError):
            resolve_commit(repo, bad)


# ------------------------------------------------------------------------ unreviewed files


def test_without_a_baseline_every_file_is_unreviewed(repo: Path) -> None:
    """The flag stands for all files; the state is not clean and the note says why."""
    state = HonestyState(repo)
    snapshot = state.snapshot()
    assert snapshot.unreviewed == Unreviewed(everything=True)
    assert not snapshot.clean
    assert "Baseline: none" in snapshot.note()


def test_a_fresh_checkout_of_the_baseline_is_clean(repo: Path) -> None:
    """Nothing differs from the baseline, so nothing is unreviewed."""
    snapshot = trusted_state(repo).snapshot()
    assert snapshot.unreviewed == Unreviewed()
    assert snapshot.clean


def test_a_file_changed_outside_a_checked_edit_is_unreviewed(repo: Path) -> None:
    """A changed tracked file and a new file count; an ignored and a deleted one do not."""
    state = trusted_state(repo)
    (repo / "src" / "parser.py").write_text(PARSER + "\nX = 1\n", encoding="utf-8")
    (repo / "src" / "new.py").write_text("Y = 2\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.py").write_text("Z = 3\n", encoding="utf-8")
    (repo / "README.md").unlink()
    snapshot = state.snapshot()
    assert snapshot.unreviewed.paths == ("src/new.py", "src/parser.py")
    assert not snapshot.clean


def test_a_checked_edit_verifies_its_file_and_a_later_change_invalidates_it(repo: Path) -> None:
    """Verified means: the content is the one that was judged."""
    state = trusted_state(repo)
    target = repo / "src" / "parser.py"
    assert state.is_reviewed("src/parser.py")
    target.write_text(PARSER + "\nX = 1\n", encoding="utf-8")
    assert state.record_edit("src/parser.py", basis_reviewed=True)
    assert state.snapshot().clean

    target.write_text(PARSER + "\nX = 2\n", encoding="utf-8")
    assert state.snapshot().unreviewed.paths == ("src/parser.py",)
    assert not state.is_reviewed("src/parser.py")

    # Back to the judged content, and back to the baseline's: both are reviewed contents.
    target.write_text(PARSER + "\nX = 1\n", encoding="utf-8")
    assert state.snapshot().clean
    target.write_text(PARSER, encoding="utf-8")
    assert state.snapshot().clean


def test_an_edit_of_unreviewed_content_does_not_verify_the_file(repo: Path) -> None:
    """A partial edit is judged by its diff, so the rest must have been reviewed before."""
    state = trusted_state(repo)
    target = repo / "src" / "parser.py"
    target.write_text(PARSER + "\nX = 1\n", encoding="utf-8")
    basis = state.is_reviewed("src/parser.py")
    assert not basis
    target.write_text(PARSER + "\nX = 1\nY = 2\n", encoding="utf-8")
    assert not state.record_edit("src/parser.py", basis_reviewed=basis)
    assert state.snapshot().unreviewed.paths == ("src/parser.py",)


def test_a_new_untracked_file_is_unreviewed_until_verified(repo: Path) -> None:
    """A file that does not exist is a reviewed basis; writing it whole verifies it."""
    state = trusted_state(repo)
    assert state.is_reviewed("src/new.py")
    (repo / "src" / "new.py").write_text("Y = 2\n", encoding="utf-8")
    assert state.snapshot().unreviewed.paths == ("src/new.py",)
    assert state.record_edit(repo / "src" / "new.py", basis_reviewed=True)
    assert state.snapshot().clean


def test_an_ignored_file_is_tracked_only_if_the_session_wrote_it(repo: Path) -> None:
    """calu does not scan ignored directories; it watches what it was told about."""
    state = trusted_state(repo)
    (repo / "build").mkdir()
    (repo / "build" / "written.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "build" / "other.py").write_text("B = 1\n", encoding="utf-8")
    assert state.snapshot().clean

    state.watch("build/written.py")
    assert state.snapshot().unreviewed.paths == ("build/written.py",)
    assert state.record_edit("build/written.py", basis_reviewed=True)
    assert state.snapshot().clean
    (repo / "build" / "written.py").write_text("A = 2\n", encoding="utf-8")
    assert state.snapshot().unreviewed.paths == ("build/written.py",)
    (repo / "build" / "written.py").unlink()
    assert state.snapshot().clean


def test_a_watched_file_outside_the_repository_is_named_by_its_absolute_path(
    repo: Path, tmp_path: Path
) -> None:
    """A file outside the workspace that the session wrote is tracked like any other."""
    state = trusted_state(repo)
    outside = tmp_path / "notes.txt"
    outside.write_text("one\n", encoding="utf-8")
    key = state.watch(outside)
    assert key == outside.resolve().as_posix()
    assert state.snapshot().unreviewed.paths == (key,)
    assert state.record_edit(outside, basis_reviewed=True)
    assert state.snapshot().clean


def test_a_stored_hash_verifies_a_file_after_a_restart_until_its_content_changes(
    repo: Path,
) -> None:
    """`trusted_files` holds a content hash; a fresh state knows nothing else."""
    script = repo / "build" / "gen.py"
    script.parent.mkdir()
    script.write_text("print('x')\n", encoding="utf-8")
    ids = content_ids(script)
    assert ids is not None
    assert ids.sha256 == hashlib.sha256(b"print('x')\n").hexdigest()

    restarted = trusted_state(repo)
    restarted.watch("build/gen.py")
    assert restarted.snapshot().unreviewed.paths == ("build/gen.py",)
    restarted.trusted_hashes = frozenset({ids.sha256})
    assert restarted.snapshot().clean
    assert restarted.is_reviewed("build/gen.py")

    script.write_text("print('y')\n", encoding="utf-8")
    assert restarted.snapshot().unreviewed.paths == ("build/gen.py",)


def test_a_stored_hash_also_verifies_a_changed_tracked_file(repo: Path) -> None:
    """The hash stands for the content wherever it lies."""
    state = trusted_state(repo)
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    assert state.snapshot().unreviewed.paths == ("README.md",)
    state.trusted_hashes = frozenset({hashlib.sha256(b"# changed\n").hexdigest()})
    assert state.snapshot().clean


def test_a_commit_since_the_baseline_is_compared_with_the_baseline(repo: Path) -> None:
    """Unreviewed is relative to the trusted commit, not to HEAD."""
    state = trusted_state(repo)
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    commit(repo, "not certified")
    assert state.snapshot().unreviewed.paths == ("README.md",)


def test_a_workspace_below_the_repository_top_sees_the_whole_repository(repo: Path) -> None:
    """Paths stay relative to the workspace; a file above it is named absolutely."""
    state = HonestyState(repo / "src")
    state.baseline = head(repo)
    (repo / "src" / "parser.py").write_text(PARSER + "\nX = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    assert state.snapshot().unreviewed.paths == tuple(
        sorted(["parser.py", (repo / "README.md").resolve().as_posix()])
    )
    assert state.record_edit("parser.py", basis_reviewed=True)
    assert state.snapshot().unreviewed.paths == ((repo / "README.md").resolve().as_posix(),)


def test_git_failing_counts_as_everything_unreviewed(tmp_path: Path) -> None:
    """A baseline that git cannot list verifies nothing."""
    plain = tmp_path / "plain"
    plain.mkdir()
    state = HonestyState(plain)
    state.baseline = GitCommit("a" * 40, "b" * 40)
    found = state.unreviewed()
    assert found.everything and found.problem
    assert "could not be compared" in state.snapshot().note()
    assert state.uncertified("c" * 40).startswith("git could not list")


def test_a_symbolic_link_is_hashed_as_a_link(repo: Path) -> None:
    """The content of a link is its target path, as git records it."""
    state = trusted_state(repo)
    (repo / "link.md").symlink_to("README.md")
    assert state.snapshot().unreviewed.paths == ("link.md",)
    commit(repo, "with the link")
    state.baseline = head(repo)
    assert state.snapshot().clean


# ------------------------------------------------------------------------------------ marks


def test_the_fourth_open_mark_requires_a_review_and_clean_marks_are_then_ignored(
    repo: Path,
) -> None:
    """Three marks are within the default threshold; a fourth one is not."""
    state = trusted_state(repo)
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (repo / name).write_text("x = 1\n", encoding="utf-8")
    changes = state.record([dirty("a.py"), dirty("b.py"), dirty("c.py")], [], "edit")
    assert len(changes.opened) == 3 and not changes.review_required
    assert state.dishonest_files() == {"a.py", "b.py", "c.py"}

    changes = state.record([dirty("d.py")], [], "edit")
    assert changes.review_required and state.review_required

    changes = state.record([], [dirty("a.py")], "edit", edited={"a.py"})
    assert not changes.closed
    assert "a review is required" in changes.ignored[0]
    assert len(state.open_marks) == 4

    # A person's override closes a mark; the review stays required.
    assert state.clear(state.open_marks[0].id, "kai") is not None
    assert state.review_required and "Review required: yes" in state.snapshot().note()


def test_a_passing_review_clears_the_marks_and_the_requirement(repo: Path) -> None:
    """Findings replace a file's marks; no open mark left means no review is required."""
    state = trusted_state(repo, max_open_marks=0)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    state.record([dirty("a.py"), dirty("b.py")], [], "edit")
    assert state.review_required
    sha_a, sha_b = state.content_sha256("a.py"), state.content_sha256("b.py")
    assert sha_a and sha_b

    changes = state.apply_review(
        {"a.py": FileReview(sha_a), "b.py": FileReview(sha_b, (Finding("", "still vague"),))}
    )
    assert [mark.label() for mark in state.open_marks] == ["b.py"]
    assert state.open_marks[0].reason == "still vague"
    assert changes.review_required and len(changes.closed) == 2

    changes = state.apply_review({"b.py": FileReview(sha_b)})
    assert not state.open_marks and not state.review_required
    assert state.snapshot().clean


def test_a_review_verifies_the_content_it_read(repo: Path) -> None:
    """A file that changed after the review read it stays unreviewed."""
    state = trusted_state(repo)
    target = repo / "README.md"
    target.write_text("# reviewed\n", encoding="utf-8")
    read = state.content_sha256("README.md")
    assert read
    target.write_text("# changed again\n", encoding="utf-8")
    state.apply_review({"README.md": FileReview(read)})
    assert state.snapshot().unreviewed.paths == ("README.md",)
    target.write_text("# reviewed\n", encoding="utf-8")
    assert state.snapshot().clean

    changes = state.apply_review({"README.md": FileReview("not a hash")})
    assert changes.ignored == ["README.md: the review named no content hash"]


def test_a_clean_mark_needs_a_step_that_edits_the_file(repo: Path) -> None:
    """A command's checker, or an edit of another file, does not close a mark."""
    state = trusted_state(repo)
    state.record([dirty("src/parser.py", "Parser.fields")], [], "edit")
    clean = [dirty("src/parser.py", "Parser.fields")]

    assert not state.record([], clean, "command").closed
    changes = state.record([], clean, "edit", edited={"README.md"})
    assert "does not edit that file" in changes.ignored[0]
    assert state.record([], clean, "edit", edited={"src/parser.py"}).closed
    assert not state.open_marks


def test_marks_are_matched_by_normalised_symbol(repo: Path) -> None:
    """`fields()` and the bare `fields` mean `Parser.fields`; an ambiguous bare name does not."""
    state = trusted_state(repo)
    opened = state.record(
        [dirty("src/parser.py", "fields()"), dirty("src/parser.py", "Parser.run")], [], "edit"
    ).opened
    assert [mark.symbol for mark in opened] == ["Parser.fields", "Parser.run"]

    # The same spot again refreshes the reason instead of adding a mark.
    state.record([dirty("src/parser.py", "Parser.fields", "new reason")], [], "edit")
    assert state.open_marks[0].reason == "new reason"
    assert len(state.open_marks) == 2

    edited = {"src/parser.py"}
    assert state.record([], [dirty("src/parser.py", "fields")], "edit", edited).closed
    # `run` is both a function and `Parser.run`; the bare name is the function, not the mark.
    changes = state.record([], [dirty("src/parser.py", "run")], "edit", edited)
    assert not changes.closed and "no open mark matches" in changes.ignored[0]
    assert state.record([], [dirty("src/parser.py", "Parser.run()")], "edit", edited).closed


def test_a_symbol_calu_does_not_know_marks_the_whole_file(repo: Path) -> None:
    """The finding is kept; the symbol as written moves into the reason."""
    state = trusted_state(repo)
    (mark,) = state.record([dirty("src/parser.py", "Parser.missing")], [], "edit").opened
    assert mark.symbol == "" and mark.reason.startswith("Parser.missing: ")
    # A file that is no Python keeps the symbol as written.
    (other,) = state.record([dirty("README.md", "Usage")], [], "edit").opened
    assert other.symbol == "Usage"


def test_a_mark_with_a_path_outside_the_project_is_ignored(repo: Path) -> None:
    """Paths come from a model's answer and are checked before they are used."""
    state = trusted_state(repo)
    changes = state.record(
        [dirty("../other/x.py"), dirty("/etc/passwd"), dirty("src/none.py")], [], "edit"
    )
    assert not changes.opened and len(changes.ignored) == 3


def test_the_same_spot_named_dirty_and_clean_stays_open(repo: Path) -> None:
    """Clean marks are applied first, so a contradictory answer leaves the mark."""
    state = trusted_state(repo)
    state.record([dirty("README.md")], [], "edit")
    state.record([dirty("README.md")], [dirty("README.md")], "edit", edited={"README.md"})
    assert [mark.label() for mark in state.open_marks] == ["README.md"]


def test_marks_whose_code_is_gone_are_closed_by_calu(repo: Path) -> None:
    """A deleted file and a removed symbol close their marks; a file that does not parse keeps
    them."""
    state = trusted_state(repo)
    (repo / "gone.py").write_text("x = 1\n", encoding="utf-8")
    state.record(
        [dirty("gone.py"), dirty("src/parser.py", "Parser.fields"), dirty("src/parser.py", "run")],
        [],
        "edit",
    )
    assert state.close_stale() == []

    (repo / "gone.py").unlink()
    (repo / "src" / "parser.py").write_text("def run(:\n", encoding="utf-8")
    assert [mark.path for mark in state.close_stale()] == ["gone.py"]

    (repo / "src" / "parser.py").write_text("def run():\n    pass\n", encoding="utf-8")
    (closed,) = state.close_stale()
    assert closed.symbol == "Parser.fields" and closed.closed_by == "calu: the symbol is gone"
    assert [mark.symbol for mark in state.open_marks] == ["run"]


def test_a_marked_file_is_dishonest_whatever_was_verified(repo: Path) -> None:
    """An open mark alone makes the state not clean."""
    state = trusted_state(repo)
    (repo / "README.md").write_text("# edited\n", encoding="utf-8")
    assert state.record_edit("README.md", basis_reviewed=True)
    state.record([dirty("README.md")], [], "edit")
    snapshot = state.snapshot()
    assert not snapshot.clean and not snapshot.unreviewed
    state.record([], [dirty("README.md")], "edit", edited={"README.md"})
    assert state.snapshot().clean


def test_symbol_helpers() -> None:
    """The two pure helpers behind the matching."""
    assert normal_symbol(" Parser.fields() ") == "Parser.fields"
    assert match_symbol("fields", ["Parser", "Parser.fields"]) == "Parser.fields"
    assert match_symbol("run", ["A.run", "B.run"]) is None
    assert match_symbol("", ["", "x"]) == ""
    assert match_symbol("", ["x"]) is None


# ------------------------------------------------------------------------------- the note


def test_the_note_names_the_baseline_the_marks_and_the_unreviewed_files(repo: Path) -> None:
    """The step's own marks come first; of many files twenty are named and all are counted."""
    state = trusted_state(repo)
    for index in range(25):
        (repo / f"f{index:02}.txt").write_text("x\n", encoding="utf-8")
    state.record(
        [dirty("README.md", reason="says nothing"), dirty("src/parser.py", "run", "does more")],
        [],
        "edit",
    )
    note = state.snapshot().note(own_paths={"src/parser.py"})
    lines = note.splitlines()
    assert lines[1] == f"Baseline: trusted commit {head(repo).commit_id[:12]}."
    assert lines[2] == "Open marks on this step's files: src/parser.py:run (does more)."
    assert lines[3] == "Open marks on other files: README.md (says nothing)."
    assert lines[4].startswith("Unreviewed files (25): f00.txt, f01.txt")
    assert "f19.txt, and 5 more." in lines[4] and "f20.txt" not in lines[4]
    assert lines[5] == "Review required: no."


def test_the_note_of_a_clean_state_says_so() -> None:
    """A checker is told the state also when there is nothing to distrust."""
    snapshot = HonestySnapshot(GitCommit("a" * 40, "b" * 40), (), Unreviewed(), False)
    assert snapshot.clean
    assert snapshot.note().splitlines()[1:] == [
        "Baseline: trusted commit aaaaaaaaaaaa.",
        "Open marks: none.",
        "Unreviewed files: none.",
        "Review required: no.",
    ]


# -------------------------------------------------------------------------- certification


def test_a_commit_of_verified_content_may_be_certified(repo: Path) -> None:
    """Every file the commit changes has the content a checker judged."""
    state = trusted_state(repo)
    (repo / "README.md").write_text("# edited\n", encoding="utf-8")
    state.record_edit("README.md", basis_reviewed=True)
    new = commit(repo, "edited")
    assert state.uncertified(new.commit_id) == ""


def test_a_commit_is_judged_by_what_it_records(repo: Path) -> None:
    """The committed content counts, not the working file at the time of the question."""
    state = trusted_state(repo)
    target = repo / "README.md"
    target.write_text("# staged earlier\n", encoding="utf-8")
    git(repo, "add", "README.md")
    target.write_text("# edited\n", encoding="utf-8")
    state.record_edit("README.md", basis_reviewed=True)
    assert state.snapshot().clean
    git(repo, "commit", "-q", "-m", "records the staged content")
    assert state.uncertified(head(repo).commit_id) == (
        "1 file(s) of the commit are not verified: README.md"
    )


def test_nothing_is_certified_without_a_baseline_or_with_open_marks(repo: Path) -> None:
    """Each reason is named; a malformed id is refused."""
    state = HonestyState(repo)
    here = head(repo).commit_id
    assert state.uncertified(here) == "there is no baseline"
    state.baseline = head(repo)
    assert state.uncertified(here) == ""
    assert state.uncertified("HEAD") == "the commit id is not a full commit id"
    state.record([dirty("README.md")], [], "edit")
    assert state.uncertified(here) == "1 mark(s) are open"
    state.clear(state.open_marks[0].id, "kai")
    state.review_required = True
    assert state.uncertified(here) == "an honesty review is required"


def test_a_commit_that_only_deletes_may_be_certified(repo: Path) -> None:
    """A deleted file leaves nothing to verify."""
    state = trusted_state(repo)
    (repo / "README.md").unlink()
    new = commit(repo, "removes the readme")
    assert state.uncertified(new.commit_id) == ""


# ---------------------------------------------------------------- settings and the test store


def test_the_honesty_settings_have_the_specified_defaults() -> None:
    """Section 9 of the specification."""
    honesty = Config().honesty
    assert honesty.mode == "off"
    assert honesty.max_open_marks == 3
    assert honesty.review_max_files == 300
    assert honesty.reviewer_model == ""
    assert honesty.review_batch_files >= 1 and honesty.review_concurrency >= 1
    assert honesty.reviewer_request_limit > Config().command_checker.request_limit


@pytest.mark.anyio
async def test_the_memory_store_keeps_certifications() -> None:
    """The stand-in behaves like the database functions: first certification wins."""
    store = MemorySessionStore()
    assert await store.certify_commit("c1", "t1", "session", "calu")
    assert not await store.certify_commit("c1", "t1", "user", "kai")
    assert store.trusted_commits["c1"].method == "session"
    assert await store.trusted_among(["c1", "c2"], ["t1", "t9"]) == (
        frozenset({"c1"}),
        frozenset({"t1"}),
    )
    with pytest.raises(ValueError, match="who certified"):
        await store.certify_commit("c2", "t2", "user", "")
    assert await store.trust_file("f" * 64, "/tmp/x", "kai")
    assert not await store.trust_file("f" * 64, "/tmp/y", "kai")
    assert await store.trusted_file_hashes() == frozenset({"f" * 64})
