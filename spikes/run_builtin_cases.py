"""Run the whole built-in evaluation set against a real checker model, both checkers.

What `calu evals` does, without a database: it reads the instructions from
`src/calu/defaults/checkers/` and the cases from `src/calu/defaults/evals/`, so it measures what
calu *ships* rather than what one machine has configured. Used for PLAN.md D73 and to be re-run
after every change to the default instructions.

    FIREWORKS_API_KEY=... uv run python spikes/run_builtin_cases.py

The key must not carry a carriage return (D70); a `.env` written on Windows gives one to a shell
that sources it. `SITL_TEST_CHECKER_MODEL` picks another model. Expect roughly 250k input and
50k output tokens per run with `repeat=3`, and about two minutes.

One run is not a measurement: two runs of the same instructions have differed by two critical
misses. Run it twice and compare the critical misses, not the accuracy.
"""

import os
from pathlib import Path

import anyio

from calu.evals.cases import builtin_cases
from calu.evals.runner import run_cases

MODEL = os.environ.get(
    "SITL_TEST_CHECKER_MODEL",
    "fireworks:accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
)


async def main() -> None:
    cases = builtin_cases()
    print(f"{len(cases)} cases, model {MODEL}\n")
    for checker, instructions_file in (
        ("command", "command.md"),
        ("edit", "edit.md"),
        ("changeset", "changeset.md"),
        ("subagent", "subagent-result.md"),
    ):
        text = Path(f"src/calu/defaults/checkers/{instructions_file}").read_text(encoding="utf-8")
        limits = {"changeset": 8}.get(checker)
        report = await run_cases(
            cases,
            checker,
            checker,
            text,
            MODEL,
            repeat=3,
            max_concurrency=4,
            request_limit=limits,
        )
        print(report.render())
        print()


anyio.run(main)
