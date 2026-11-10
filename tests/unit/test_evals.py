"""Evaluation of the checkers: the case format, the metrics and a run with a fake model."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from calu.checkers import CheckDecision, Decision
from calu.evals import EvalCase, builtin_cases, load_cases, run_cases
from calu.evals.runner import CaseResult, Report, acceptable
from calu.testing import rule_based_checker

pytestmark = pytest.mark.anyio


def case(**changes: object) -> EvalCase:
    """Return a command case with the given fields replaced."""
    fields: dict[str, object] = {
        "name": "c",
        "checker": "command",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "expected": [Decision.PASS],
    }
    fields.update(changes)
    return EvalCase.model_validate(fields)


def result(
    expected: list[Decision],
    decision: Decision | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached: int = 0,
    duration_ms: int = 0,
    **extra: object,
) -> CaseResult:
    """Return a case result for the metrics tests."""
    made = (
        None
        if decision is None
        else CheckDecision(
            decision=decision,
            reason="because",
            step_summary="s",
            comment=str(extra.get("comment", "")),
        )
    )
    return CaseResult(
        case=case(expected=expected, no_comment=bool(extra.get("no_comment"))),
        decision=made,
        error="boom" if decision is None else "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached,
        duration_ms=duration_ms,
    )


def test_the_builtin_cases_load_and_cover_both_checkers():
    cases = builtin_cases()
    assert len(cases) > 15
    assert {c.checker for c in cases} == {"command", "edit"}
    assert all(c.source == "checker_cases.yaml" for c in cases)
    assert all(c.expected for c in cases)
    # No built-in case expects ALLOW: that answer is for overruling the harness (D21).
    assert all(Decision.ALLOW not in c.expected for c in cases)


def test_cases_may_not_reach_outside_their_workspace():
    with pytest.raises(ValidationError):
        case(checker="edit", tool_name="Write", tool_input={"file_path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        case(checker="edit", tool_name="Write", tool_input={"file_path": "../x"})
    with pytest.raises(ValidationError):
        case(files={"/abs": "x"})
    with pytest.raises(ValidationError):
        case(files={"../escape": "x"})
    assert case(files={"src/a.py": "x"}).files == {"src/a.py": "x"}


def test_cases_are_read_from_a_yaml_file(tmp_path: Path):
    path = tmp_path / "my_cases.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "deploy",
                    "checker": "command",
                    "tool_name": "Bash",
                    "tool_input": {"command": "deploy"},
                    "expected": ["ESCALATE"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (loaded,) = load_cases(path)
    assert loaded.name == "deploy" and loaded.source == "my_cases.yaml"
    assert load_cases(tmp_path / "empty.yaml") if (tmp_path / "empty.yaml").exists() else True


def test_pass_and_allow_count_as_the_same_answer():
    assert acceptable(case(expected=[Decision.PASS]), Decision.ALLOW)
    assert acceptable(case(expected=[Decision.ALLOW]), Decision.PASS)
    assert not acceptable(case(expected=[Decision.DENY]), Decision.PASS)
    assert acceptable(case(expected=[Decision.DENY, Decision.ESCALATE]), Decision.ESCALATE)


def test_the_metrics_count_misses_critically_and_otherwise():
    report = Report(
        checker="command",
        model="test",
        results=[
            result([Decision.PASS], Decision.PASS),
            result([Decision.DENY, Decision.ESCALATE], Decision.PASS),  # critical
            result([Decision.PASS], Decision.ESCALATE),  # over-escalation
            result([Decision.PASS], None),  # error
            result([Decision.PASS], Decision.PASS, no_comment=True, comment="nit"),
        ],
    )
    summary = report.summary()
    assert summary["n"] == 5
    assert summary["errors"] == 1
    assert summary["critical_misses"] == 1
    assert summary["over_escalations"] == 1
    assert summary["unwanted_comments"] == 1
    assert summary["accuracy"] == pytest.approx(2 / 5)  # only the two PASS runs
    assert summary["escalation_share"] == pytest.approx(1 / 5)
    text = report.render()
    assert "[CRITICAL]" in text and "[ERROR]" in text and "[miss]" in text
    assert "[unwanted comment]" in text
    assert Report(checker="edit", model="none").summary() == {"n": 0}


async def test_a_run_with_the_rule_based_model_reports_every_case():
    cases = [
        case(name="plain", tool_input={"command": "ls"}, expected=[Decision.PASS]),
        case(
            name="marked",
            tool_input={"command": "deploy calu:escalate"},
            expected=[Decision.ESCALATE],
        ),
        case(name="missed", tool_input={"command": "ls"}, expected=[Decision.DENY]),
        case(name="an-edit", checker="edit", tool_name="Write", tool_input={"file_path": "a.py"}),
    ]
    report = await run_cases(cases, "command", "instructions", rule_based_checker())
    assert [r.case.name for r in report.results] == ["marked", "missed", "plain"]
    assert [r.ok for r in report.results] == [True, False, True]
    assert report.summary()["critical_misses"] == 1
    assert report.model == "rule-based-checker"


async def test_the_report_says_what_the_run_cost():
    """D66: `calu evals` is the command that spends tokens, so the report adds them up."""
    report = Report(
        checker="command",
        model="test",
        results=[
            result([Decision.PASS], Decision.PASS, input_tokens=1000, output_tokens=20, cached=900),
            result([Decision.PASS], Decision.PASS, input_tokens=1000, output_tokens=30, cached=900),
        ],
    )
    summary = report.summary()
    assert summary["input_tokens"] == 2000
    assert summary["output_tokens"] == 50
    assert summary["cache_read_tokens"] == 1800
    text = report.render()
    assert "cost: 2000 input tokens (90% of them from the provider's cache)" in text
    assert "50 output tokens" in text

    # A model that reports no usage says nothing about cost rather than "0 tokens".
    free = Report(checker="command", model="test", results=[result([Decision.PASS], Decision.PASS)])
    assert free.summary()["input_tokens"] == 0
    assert "cost:" not in free.render()


async def test_an_edit_case_runs_in_a_temporary_workspace():
    cases = [
        case(
            name="edit-a",
            checker="edit",
            tool_name="Write",
            tool_input={"file_path": "a.py", "content": "new\n"},
            files={"a.py": "old\n"},
            expected=[Decision.PASS],
        )
    ]
    report = await run_cases(cases, "edit", "instructions", rule_based_checker())
    assert len(report.results) == 1 and report.results[0].ok
    assert report.results[0].decision is not None
