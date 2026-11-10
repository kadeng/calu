"""`calu evals` over the stored configuration: which checkers run, and the exit code.

The model calls themselves are covered by `tests/unit/test_evals.py` with a rule-based fake;
what is only reachable here is `run_evals`, which reads the checker definitions and the models
out of the database, and whose second return value is the exit code a scheduled run depends on.
"""

from pathlib import Path
from typing import Any

import pytest

from calu import admin
from calu.db import Db, connect
from calu.evals.cases import case_line, cases_to_yaml, load_cases
from calu.evals.cli import collect_cases, run_evals
from calu.evals.runner import CaseResult, Report
from calu.setup import setup

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
        asked: list[tuple[str, str, int]] = []

        async def fake_run_cases(
            cases: list[Any],
            checker: Any,
            instructions: str,
            model: Any,
            repeat: int = 1,
        ) -> Report:
            """Record what was asked for and answer with one critical miss for "command".

            Args:
                cases: The cases handed to the checker.
                checker: Which checker is being run.
                instructions: Its instructions from the database.
                model: The model string it should use.
                repeat: How often each case runs.

            Returns:
                A report with one critical miss for the command checker, none otherwise.
            """
            asked.append((str(checker), str(model), len(cases)))
            assert instructions, "a checker must be run with its instructions"
            return _report(str(checker), str(model), critical=1 if checker == "command" else 0)

        monkeypatch.setattr("calu.evals.cli.run_cases", fake_run_cases)
        text, critical = await run_evals(db, tmp_path, builtin=True, from_db=False)

        # Each checker ran once, with the model the database holds for it.
        assert [(checker, model) for checker, model, _ in asked] == [
            ("command", "stored:command"),
            ("edit", "stored:edit"),
        ]
        # The built-in cases were handed to both.
        assert all(count > 0 for _checker, _model, count in asked)
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
        assert [checker for checker, _model, _count in asked] == ["command"]
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
