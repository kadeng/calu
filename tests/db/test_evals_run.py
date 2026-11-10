"""`calu evals` over the stored configuration: which checkers run, and the exit code.

The model calls themselves are covered by `tests/unit/test_evals.py` with a rule-based fake;
what is only reachable here is `run_evals`, which reads the checker definitions and the models
out of the database, and whose second return value is the exit code a scheduled run depends on.
"""

from pathlib import Path
from typing import Any

import pytest

from calu import admin, herd
from calu.checkers import CheckDecision, CodeMark, Decision
from calu.db import Db, connect
from calu.evals.cases import case_line, cases_from_marks, cases_to_yaml, load_cases
from calu.evals.cli import _rows_to_measure as cli_rows
from calu.evals.cli import collect_cases, run_evals
from calu.evals.runner import CaseResult, Report
from calu.setup import setup
from calu.store import ConfigStore, DatabaseSessionStore, SessionIdentity, StepRow

IDENTITY = SessionIdentity(hostname="box", os_user="u", directory="/w", pid=7, os="Linux")

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def _report(checker: Any, model: str, critical: int) -> Report:
    """Build a report with `critical` critical misses, without running a model.

    Args:
        checker: The checker the report is for.
        model: The model name it should claim.
        critical: How many cases should count as a critical miss.

    Returns:
        The report.
    """
    from calu.checkers.model import CheckDecision, Decision
    from calu.evals.cases import EvalCase

    results = []
    for index in range(critical):
        case = EvalCase(
            name=f"miss-{index}",
            checker=checker,
            tool_name="Bash",
            tool_input={"command": "x"},
            expected=[Decision.DENY],
        )
        # The checker let through what the case says must not pass: a critical miss.
        decision = CheckDecision(
            decision=Decision.ALLOW, reason="wrongly allowed", step_summary="Runs a command."
        )
        results.append(CaseResult(case=case, decision=decision))
    return Report(checker=checker, model=model, results=results)


async def test_run_evals_uses_the_stored_checkers_and_reports_the_exit_code(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both checkers run with their stored instructions and model; misses become the exit code."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.set_setting(db, "command_checker.model", "stored:command")
        await admin.set_setting(db, "edit_checker.model", "stored:edit")
        await admin.set_setting(db, "changeset_checker.model", "stored:changeset")
        await admin.set_setting(db, "subagent_checker.model", "stored:subagent")
        asked: list[tuple[str, str, int]] = []
        budgets: dict[str, int | None] = {}

        async def fake_run_cases(
            cases: list[Any],
            checker: Any,
            row_name: str,
            instructions: str,
            model: Any,
            repeat: int = 1,
            request_limit: int | None = None,
            rules: Any = None,
        ) -> Report:
            """Record what was asked for and answer with one critical miss for "command".

            Args:
                cases: The cases handed to the checker.
                checker: Which kind of case is being run.
                row_name: Name of the checker definition being measured (D93).
                instructions: Its instructions from the database.
                model: The model string it should use.
                repeat: How often each case runs.
                request_limit: The configured request budget of this checker (D75).
                rules: The project's allow and deny lists, for the guarded misses (D76).

            Returns:
                A report with one critical miss for the command checker, none otherwise.
            """
            asked.append((str(row_name), str(model), len(cases)))
            budgets[str(row_name)] = request_limit
            assert rules is not None, "the report needs the project's rules to mark guarded misses"
            assert instructions, "a checker must be run with its instructions"
            return _report(str(row_name), str(model), critical=1 if checker == "command" else 0)

        monkeypatch.setattr("calu.evals.cli.run_cases", fake_run_cases)
        text, critical = await run_evals(db, tmp_path, builtin=True, from_db=False)

        # Each checker ran once, with the model the database holds for it.
        assert [(checker, model) for checker, model, _ in asked] == [
            ("command", "stored:command"),
            ("edit", "stored:edit"),
            ("changeset", "stored:changeset"),
            # Reported by row name, which is how an extra checker on a route is told apart
            # from the route's own (D93).
            ("subagent-result", "stored:subagent"),
        ]
        # The built-in cases were handed to each of them.
        assert all(count > 0 for _checker, _model, count in asked)
        # Each ran with its configured request budget: the changeset checker gets more,
        # because judging a whole change means reading files around the diff (D75).
        assert budgets["command"] == 5
        assert budgets["changeset"] == 8
        # One critical miss, which is both in the report and the exit code.
        assert critical == 1
        assert "critical misses: 1" in text
        assert "stored:command" in text

        # `--model` overrides every checker's model, which is how a one-model run is done.
        asked.clear()
        await run_evals(db, tmp_path, model="one:model", builtin=True, from_db=False)
        assert {model for _checker, model, _count in asked} == {"one:model"}

        # A checker that is switched off has no definition to run, so it is reported as
        # "none" rather than silently skipped -- `calu evals` must not look like it ran it.
        asked.clear()
        await admin.set_checker(db, "edit", enabled=False)
        text, _critical = await run_evals(db, tmp_path, builtin=True, from_db=False)
        assert [checker for checker, _model, _count in asked] == [
            "command",
            "changeset",
            "subagent-result",
        ]
        assert "none" in text
    finally:
        await conn.close()


async def test_the_sources_of_the_cases_are_added_in_order(db_url: str, tmp_path: Path) -> None:
    """Built-in, then files, then the escalations, and each source can be left out."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        case_file = tmp_path / "cases.yaml"
        case_file.write_text(
            "- name: from-a-file\n"
            "  checker: command\n"
            "  tool_name: Bash\n"
            "  tool_input: {command: ls}\n"
            "  expected: [PASS]\n",
            encoding="utf-8",
        )
        builtin_only = await collect_cases(db, (), from_db=False, builtin=True)
        assert builtin_only and all(c.source != "escalations" for c in builtin_only)

        with_file = await collect_cases(db, (str(case_file),), from_db=False, builtin=True)
        assert len(with_file) == len(builtin_only) + 1
        assert with_file[-1].name == "from-a-file"

        file_only = await collect_cases(db, (str(case_file),), from_db=False, builtin=False)
        assert [c.name for c in file_only] == ["from-a-file"]

        # The database has no resolved escalation, so that source adds nothing yet.
        assert await collect_cases(db, (), from_db=True, builtin=False) == []
    finally:
        await conn.close()


async def test_the_collected_cases_can_be_listed_and_kept(db_url: str, tmp_path: Path) -> None:
    """`--list` and `--export` are the replacement for a training mode (PLAN.md D55, Q9).

    A human already labelled every escalation they resolved. What was missing was a way to see
    those labels and keep them, so this checks both: the lines name the case, its checker and
    the acceptable decisions, and the exported YAML reads back as the same cases.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        cases = await collect_cases(db, (), from_db=False, builtin=True)
        assert cases, "the built-in set must not be empty"

        # The lines are what a person reads.
        line = case_line(cases[0])
        assert cases[0].name in line
        assert cases[0].checker in line
        assert cases[0].tool_name in line
        assert cases[0].expected[0].value in line

        # The export reads back as the same cases, so it can be committed and re-run.
        document = cases_to_yaml(cases)
        path = tmp_path / "kept.yaml"
        path.write_text(document, encoding="utf-8")
        again = load_cases(path)
        assert [c.name for c in again] == [c.name for c in cases]
        assert [c.expected for c in again] == [c.expected for c in cases]
        assert [c.tool_input for c in again] == [c.tool_input for c in cases]
        # `source` says where a case came from, so the file sets it rather than carrying it.
        assert "source:" not in document
        assert {c.source for c in again} == {"kept.yaml"}

        # An empty set is a document that still reads back.
        assert load_cases_text(cases_to_yaml([])) == []
    finally:
        await conn.close()


def load_cases_text(document: str) -> list:
    """Read cases from a YAML string, for the empty case.

    Args:
        document: The YAML document.

    Returns:
        The cases it holds.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cases.yaml"
        path.write_text(document, encoding="utf-8")
        return load_cases(path)


async def test_every_enabled_checker_is_measured_including_an_extra_one(db_url: str) -> None:
    """D93: a route carries several checkers, and each is a judgement of its own.

    `calu evals` measured one checker per route, so the honesty checker -- which sits beside
    the edit checker -- was never measured, and an eval case about it would have been judged by
    the edit checker instead and passed for the wrong reason.
    """
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        await admin.set_checker(db, "honesty", enabled=True)
        settings = await ConfigStore(db, Path("/w")).load()
        measured = [row for _kind, row in cli_rows(settings)]
        assert "honesty" in measured, "an enabled extra checker has to be measured"
        # Its kind is the one its route judges, so it gets the edit cases.
        kinds = {row: kind for kind, row in cli_rows(settings)}
        assert kinds["honesty"] == "edit"
        assert kinds["edit"] == "edit"
        # The built-in rows keep their order, so a report reads as before without extras.
        assert measured[:4] == ["command", "edit", "changeset", "subagent-result"]

        # A disabled checker is not measured at all: `ConfigStore` only loads enabled rows.
        await admin.set_checker(db, "honesty", enabled=False)
        again = await ConfigStore(db, Path("/w")).load()
        assert "honesty" not in [row for _kind, row in cli_rows(again)]
    finally:
        await conn.close()


async def test_an_overruled_mark_becomes_a_case_that_expects_no_mark(db_url: str) -> None:
    """D94: a mark a person overruled is the only honesty label that comes from real work."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(
                seq=1,
                tool_name="Write",
                tool_input={"file_path": "src/parser.py", "content": "x = 1\n"},
            )
        )
        await store.apply_honesty_marks(
            [
                CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies"),
                CodeMark(path="src/other.py", reason="this one is right"),
            ],
            [],
            step,
            None,
            "honesty",
        )
        marks = {m.path: m for m in await herd.honesty_marks(db)}
        # A person overrules one; the checker clears the other itself.
        await herd.clear_honesty_mark(db, marks["src/parser.py"].id, "kai")
        await store.apply_honesty_marks([], [CodeMark(path="src/other.py", reason="fixed")], step)

        cases = await cases_from_marks(db)
        assert [c.name for c in cases] == [f"overruled-mark-{marks['src/parser.py'].id}"], (
            "only the overruled mark is a case; one the checker cleared itself is not"
        )
        (case,) = cases
        assert case.expect_no_marks is True
        assert case.expected == [Decision.PASS]
        assert case.checker == "edit", "a Write is judged by an edit-route checker"
        assert case.tool_input["file_path"] == "src/parser.py"
        assert "src/parser.py:fields" in case.context
        assert "honesty" in case.context, "the case says whose finding was overruled"
        assert case.source == "honesty-marks"

        # And a checker that marks it again is counted as the mistake it is.
        wrong = CaseResult(
            case=case,
            decision=CheckDecision(
                decision=Decision.PASS,
                reason="",
                step_summary="s",
                marks_dirty=[CodeMark(path="src/parser.py", symbol="fields", reason="still")],
            ),
        )
        assert wrong.unwanted_mark
        report = Report(checker="honesty", model="test", results=[wrong])
        assert report.summary()["unwanted_marks"] == 1
        text = report.render()
        assert "unwanted marks 1" in text
        assert "[unwanted mark]" in text and "src/parser.py:fields" in text

        # A checker that records nothing is right, and nothing is reported.
        right = CaseResult(
            case=case,
            decision=CheckDecision(decision=Decision.PASS, reason="", step_summary="s"),
        )
        assert not right.unwanted_mark
        assert (
            Report(checker="honesty", model="test", results=[right]).summary()["unwanted_marks"]
            == 0
        )
    finally:
        await conn.close()


async def test_a_mark_case_carries_the_file_as_it_was(db_url: str) -> None:
    """D95: a case about an edit can only be reproduced with the file the edit applies to."""
    conn = await connect(db_url)
    try:
        await setup(conn, db_url, "test:model")
        db = Db(conn)
        store = DatabaseSessionStore(db)
        await store.start(IDENTITY)
        step = await store.add_step(
            StepRow(
                seq=1,
                tool_name="Edit",
                tool_input={
                    "file_path": "src/parser.py",
                    "old_string": "return 1",
                    "new_string": "return 2",
                },
            )
        )
        before = "def fields():\n    return 1\n"
        await store.apply_honesty_marks(
            [CodeMark(path="src/parser.py", symbol="fields", reason="the docstring lies")],
            [],
            step,
            None,
            "honesty",
            {"src/parser.py": before},
        )
        (mark,) = await herd.honesty_marks(db)
        assert mark.files == {"src/parser.py": before}, "the register keeps what was judged"

        # A repeated mark on the same spot refreshes the reason and keeps the first snapshot:
        # that is the state the checker judged when it opened the mark.
        await store.apply_honesty_marks(
            [CodeMark(path="src/parser.py", symbol="fields", reason="still wrong")],
            [],
            step,
            None,
            "honesty",
            {"src/parser.py": "something else entirely\n"},
        )
        (again,) = await herd.honesty_marks(db)
        assert again.reason == "still wrong"
        assert again.files == {"src/parser.py": before}, "the first snapshot is the one judged"

        await herd.clear_honesty_mark(db, mark.id, "kai")
        (case,) = await cases_from_marks(db)
        # The case can be reproduced: the file is there, so the edit applies and the checker
        # sees a diff instead of raw JSON.
        assert case.files == {"src/parser.py": before}
        assert case.tool_input["old_string"] == "return 1"
    finally:
        await conn.close()
