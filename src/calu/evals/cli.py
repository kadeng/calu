"""`calu evals`: measure how well the configured checkers decide (ported from sitl).

    calu evals                        the built-in cases, with the checkers of the database
    calu evals --from-db              also the resolved escalations as cases
    calu evals cases/*.yaml           further case files
    calu evals --model mistral:...    one model for both checkers, instead of the configured
    calu evals --repeat 3             run every case three times (decision stability)

The checker instructions and models come from the database, so the command measures the
checkers as they are actually configured. The cases run in temporary directories; nothing of
the project is read or changed.
"""

from collections.abc import Sequence
from pathlib import Path

from calu.db import Db
from calu.evals.cases import CheckerName, EvalCase, builtin_cases, cases_from_escalations
from calu.evals.cases import load_cases as load_case_file
from calu.evals.runner import Report, run_cases
from calu.store import ConfigStore

CHECKERS: tuple[CheckerName, ...] = ("command", "edit")
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
        from_db: Also use the resolved escalations as cases.
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
    for checker in CHECKERS:
        definition = settings.checkers.get(checker)
        instructions = definition.instructions if definition else ""
        if not instructions:
            reports.append(Report(checker=checker, model="none"))
            continue
        configured = (
            settings.config.command_checker
            if checker == "command"
            else settings.config.edit_checker
        )
        reports.append(
            await run_cases(cases, checker, instructions, model or configured.model, repeat=repeat)
        )
    text = "\n\n".join(report.render() for report in reports)
    critical = sum(int(report.summary().get("critical_misses", 0)) for report in reports)
    return f"{text}\n\ncases: {len(cases)}, critical misses: {critical}", critical
