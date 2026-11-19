"""What `calu` imports before a command runs, and how long `calu --version` takes.

Every command starts by importing `calu.cli`. The checkers (Pydantic AI with its provider
SDKs) and the Agent SDK (with `mcp`, Starlette and uvicorn) take about two seconds to import;
a command that runs no checker and starts no Claude Code must not pay for them. Four reports
of the test campaign named the wait (`calu --version`, `calu attach`, `calu ide`).

Each case runs in a process of its own, because the test process has imported everything.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HEAVY = (
    "pydantic_ai",
    "claude_agent_sdk",
    "mcp",
    "botocore",
    "mistralai",
    "textual",
    "starlette",
    "uvicorn",
)
"""Top-level packages no client command needs before it has decided what to do."""

REPORT = f"print(json.dumps([name for name in {HEAVY!r} if name in sys.modules]))"


def loaded(code: str) -> list[str]:
    """Run `code` in a fresh interpreter and return the heavy packages it imported.

    Args:
        code: Python statements; `sys`, `json`, `io` and `contextlib` are imported for them.

    Returns:
        The names of `HEAVY` that are in `sys.modules` afterwards.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import contextlib, io, json, sys\n{code}\n{REPORT}"],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_importing_the_command_line_imports_no_checker_and_no_agent_sdk() -> None:
    assert loaded("import calu.cli") == []


def test_building_the_parser_imports_no_checker_and_no_agent_sdk() -> None:
    assert loaded("import calu.cli\ncalu.cli.build_parser()") == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["--help"],
        ["sessions", "--help"],
        ["attach", "--help"],
        ["start", "--help"],
        ["protocol"],
    ],
)
def test_a_command_that_runs_no_session_imports_none_of_them(argv: list[str]) -> None:
    code = (
        "import calu.cli\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    try:\n"
        f"        calu.cli.main({argv!r})\n"
        "    except SystemExit:\n"
        "        pass"
    )
    assert loaded(code) == []


@pytest.mark.parametrize(
    "modules",
    [
        # What the client commands use once they run: the line client, the fleet commands, the
        # editor bridge, the ACP adapter, `calu start`, `calu doctor`.
        "calu.attach, calu.protocol.client, calu.herd, calu.session_requests",
        "calu.ide.serve, calu.ide.bridge, calu.ide.database, calu.acp.serve, calu.acp.adapter",
        "calu.doctor, calu.preflight, calu.setup, calu.admin, calu.config, calu.model_choice",
        "calu.tui.start, calu.run, calu.serve, calu.launch",
    ],
)
def test_the_modules_of_the_client_commands_import_none_of_them(modules: str) -> None:
    assert loaded(f"import {modules}") == []


def test_the_full_screen_client_and_the_web_interface_import_no_checker() -> None:
    """They may import their own toolkit (Textual, Starlette), and nothing of the checkers."""
    tui = loaded("import calu.tui.app, calu.tui.connection, calu.tui.adminaccess")
    assert set(tui) <= {"textual"}
    web = loaded("import calu.web.app, calu.web.database, calu.lightcycles.server")
    assert set(web) <= {"starlette", "uvicorn"}


def test_locating_claude_code_does_not_import_the_agent_sdk() -> None:
    """`calu doctor` and `calu ide` ask where the bundled `claude` is."""
    code = (
        "import calu.launch\n"
        "try:\n"
        "    calu.launch.claude_path(None)\n"
        "except calu.launch.LaunchError:\n"
        "    pass"
    )
    assert loaded(code) == []


def test_the_lazy_names_of_the_packages_still_resolve() -> None:
    """`from calu.checkers import Checker` works, and imports Pydantic AI only then."""
    code = (
        "import calu.checkers, calu.context, calu.evals\n"
        "assert 'pydantic_ai' not in sys.modules\n"
        "from calu.checkers import Checker, ResultChecker, CheckContext\n"
        "from calu.context import ContextBuilder\n"
        "from calu.evals import run_cases\n"
        "assert 'pydantic_ai' in sys.modules\n"
        "try:\n"
        "    calu.checkers.no_such_name\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('a missing name did not raise')"
    )
    assert "pydantic_ai" in loaded(code)


@pytest.mark.slow
def test_calu_version_answers_quickly() -> None:
    """`calu --version` is faster than a process that only imports what it used to import.

    Before the imports were made lazy, `calu --version` imported Pydantic AI with the Mistral
    and Bedrock models and the Agent SDK (1.9 s on the machine that now needs 0.5 s; those
    imports alone take 1.5 s there). The comparison holds on a slow or busy machine, where a
    bound in seconds alone would not; the runs alternate, best of three.
    """
    calu = Path(sys.executable).parent / "calu"
    version = [str(calu), "--version"] if calu.exists() else [sys.executable, "-m", "calu"]
    if not calu.exists():
        version.append("--version")
    checkers = [
        sys.executable,
        "-c",
        "import pydantic_ai, pydantic_ai.models.bedrock, pydantic_ai.models.mistral, "
        "claude_agent_sdk",
    ]
    best = {"version": float("inf"), "checkers": float("inf")}
    for _ in range(3):
        for name, command in (("version", version), ("checkers", checkers)):
            started = time.perf_counter()
            done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
            best[name] = min(best[name], time.perf_counter() - started)
            assert done.returncode == 0, done.stderr
            assert name != "version" or done.stdout.startswith("calu ")
    assert best["version"] < best["checkers"], best
    assert best["version"] < 5.0, f"`calu --version` took {best['version']:.2f} s"
