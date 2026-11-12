"""`calu evals`: measure how well the configured checkers decide (ported from sitl).

    calu evals                        the built-in cases, with the checkers of the database
    calu evals --from-db              also the resolved escalations as cases
    calu evals cases/*.yaml           further case files
    calu evals --model mistral:...    one model for every checker, instead of the configured
    calu evals --repeat 3             run every case three times (decision stability)

It measures all four checkers: the command and edit checkers on single steps, the changeset
checker on a whole commit (a case carries the `diff`, D75) and the subagent-result checker on
what a subagent handed back (a case carries the `prompt` and the `result`, D77). The checker
instructions and models come from the database, so the command measures the checkers as they
are actually configured. The cases run in temporary directories; nothing of
the project is read or changed, and the checker is told a plausible working directory rather
than that temporary one (PLAN.md D71).

**One run is not a measurement.** Measured with a 30B reasoning model, two runs of the same 25
cases against the same instructions gave 2 critical misses and none: the borderline cases are
where a checker is unstable, and they are the ones worth knowing about. Use `--repeat 3` or
more before concluding that a change to the instructions helped, and compare the critical
misses rather than the accuracy.
"""

from collections.abc import Sequence
from pathlib import Path

from calu.db import Db
from calu.evals.cases import (
    CheckerName,
    EvalCase,
    builtin_cases,
    cases_from_escalations,
)
from calu.evals.cases import load_cases as load_case_file
from calu.evals.runner import Report, run_cases
from calu.store import ConfigStore
from calu.store.config import ProjectSettings

CHECKERS: tuple[CheckerName, ...] = ("command", "edit", "changeset", "subagent")
"""The checkers `calu evals` measures, in the order it reports them."""


def _rows_to_measure(settings: ProjectSettings) -> list[tuple[CheckerName, str]]:
    """Return every enabled checker row to measure, with the kind of case it judges (D93).

    A route carries several checkers (D81) -- honesty beside the edit checker, a security
    checker beside the command checker -- and each is a separate judgement with its own
    instructions, so each is measured separately. The kind of case a row judges follows from
    its route, because that is what decides the shape of the input: a step, a whole commit or
    a subagent's result.

    The route's own row comes first in its group, so a report reads as before when no extra
    checker is configured.

    Args:
        settings: The project's settings, whose `checkers` holds the enabled definitions.

    Returns:
        (kind of case, row name) pairs: the four built-in rows in their usual order, then every
        other enabled row whose route is one the evaluation can run.
    """
    by_route: dict[str, CheckerName] = {
        "command": "command",
        "edit": "edit",
        "changeset": "changeset",
        "subagent": "subagent",
    }
    pairs: list[tuple[CheckerName, str]] = [(kind, ROW_NAMES[kind]) for kind in CHECKERS]
    for name, definition in sorted(settings.checkers.items()):
        if name in ROW_NAMES.values():
            continue
        kind = by_route.get(definition.route)
        if kind is not None:
            pairs.append((kind, name))
    return pairs


ROW_NAMES: dict[CheckerName, str] = {
    "command": "command",
    "edit": "edit",
    "changeset": "changeset",
    # The row is named after what it reviews, the route after where it sits (`calu.setup`).
    "subagent": "subagent-result",
}
"""Name of the `checker_types` row that holds each checker's instructions."""
"""The checkers an evaluation run measures."""


async def collect_cases(
    db: Db, files: Sequence[str] = (), from_db: bool = False, builtin: bool = True
) -> list[EvalCase]:
    """Collect the cases of all selected sources.

    Args:
        db: A connection, for the escalations.
        files: YAML case files to add.
        from_db: Also turn the resolved escalations of the database into cases.
        builtin: Include the built-in case set.

    Returns:
        The cases of every source, in the order built-in, files, database.

    Raises:
        OSError: If a case file cannot be read.
        pydantic.ValidationError: If a case file holds an invalid case.
        psycopg.Error: On database errors.
    """
    cases: list[EvalCase] = list(builtin_cases()) if builtin else []
    for name in files:
        cases += load_case_file(Path(name))
    if from_db:
        # Labels from real work: an escalation a human decided (D50). Overruled honesty marks
        # were a second source (D94) until the register became transient (D180).
        cases += await cases_from_escalations(db)
    return cases


async def run_evals(
    db: Db,
    workspace: Path,
    project: str | None = None,
    files: Sequence[str] = (),
    from_db: bool = False,
    builtin: bool = True,
    model: str | None = None,
    repeat: int = 1,
) -> tuple[str, int]:
    """Run the evaluation and return the report.

    Args:
        db: A connection that may read the configuration and the escalations.
        workspace: The project directory whose settings apply (for the rules and the models).
        project: Name of the project row whose settings apply, or None for the global ones.
        files: YAML case files to add to the built-in set.
        from_db: Also use the resolved escalations and the overruled honesty marks as cases.
        builtin: Include the built-in case set.
        model: One model for every checker, instead of the configured ones.
        repeat: How often each case is run.

    Returns:
        The report text and the number of critical misses (a non-zero exit code for scripts).

    Raises:
        OSError: If a case file cannot be read.
        pydantic.ValidationError: If a case file holds an invalid case, or the stored
            configuration is invalid.
        psycopg.Error: On database errors.
    """
    settings = await ConfigStore(db, workspace, project).load()
    cases = await collect_cases(db, files, from_db, builtin)
    reports: list[Report] = []
    for checker, row_name in _rows_to_measure(settings):
        definition = settings.checkers.get(row_name)
        if definition is None or not definition.instructions:
            # Reported as "none" rather than skipped: `calu evals` must not look as if it had
            # measured a checker that is switched off.
            reports.append(Report(checker=row_name, model="none"))
            continue
        instructions = definition.instructions
        configured = {
            "command": settings.config.command_checker,
            "edit": settings.config.edit_checker,
            "changeset": settings.config.changeset_checker,
            "subagent": settings.config.subagent_checker,
        }[checker]
        # Its own row's model and limits where it has them, because an extra checker on a
        # route (D81) is configured by its row and not by the route's `*_checker` settings.
        configured = definition.limits() if row_name not in ROW_NAMES.values() else configured
        reports.append(
            await run_cases(
                cases,
                checker,
                row_name,
                instructions,
                model or configured.model,
                repeat=repeat,
                # As configured: the changeset checker is given more requests than a step
                # checker, because it reads files around the diff (D75).
                request_limit=configured.request_limit,
                # The project's own lists, so that the report says which wrong answers this
                # project's rules stop before a checker is asked (D76).
                rules=settings.rules,
                # The configured effort, so the measured checker is the one a session runs --
                # but not under `--model`: an effort chosen for one model must not be applied to
                # another (D129, D135).
                effort=None if model else configured.effort,
            )
        )
    text = "\n\n".join(report.render() for report in reports)
    critical = sum(int(report.summary().get("critical_misses", 0)) for report in reports)
    return f"{text}\n\ncases: {len(cases)}, critical misses: {critical}", critical
