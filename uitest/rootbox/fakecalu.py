"""`fakecalu`: the real `calu` command with a scripted runner in place of Claude Code.

For the root box (`uitest/README.md`, "The root box"). It calls `calu.cli.main` with the
arguments it was given, after replacing two names in `calu.cli`:

    calu.cli.serve_sessions ──► calu.serve.serve   with the scripted runner and fake checkers
    calu.cli.run_session    ──► calu.run.run_session with the same

Everything else is calu's own code path: the options (`--user`, `--from-sudo`, `--secure`,
`--insecure`), the credential store, the calu user password, `calu.asuser.resolve`, `prepare`
and `enter`, the sockets and the log file. So `fakecalu serve --user ada` as root runs sessions
whose files in the project are written with ada's file identity, without `claude`, an API
key or a network. The scripted conversations are those of `uitest/fake_server.py`
(`scenario: <name>` as the first message; anything else plays `tour`).

With `FAKECALU_SCRIPT=<file>` every session plays that script instead (JSON as
`calu.claude.FakeScript.load` reads it), for a test that needs its own steps, such as a
command whose `"writes"` create files in the project.

`--sudo` is not passed on to a fake: calu's `--sudo` runs the `calu` of its installation
again. The same command line is `sudo fakecalu serve --user "$USER" --from-sudo`, which is what
`calu serve --sudo` builds (`calu.asuser.sudo_command`).

`rootbox-install` writes the `fakecalu` command into `/usr/local/bin`, with the interpreter of
root's calu installation.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fake_server  # uitest/fake_server.py: the scenarios and their runner

import calu.cli
from calu.claude import FakeRunner
from calu.claude.fake_runner import FakeScript
from calu.run import run_session
from calu.serve import serve
from calu.testing import fake_models

SCRIPT_ENV = "FAKECALU_SCRIPT"
"""Environment variable that names a script file every session plays."""


def make_runner(workspace: Path, config: Any, hidden: set[str]) -> FakeRunner:
    """Create the scripted runner of one session.

    Args:
        workspace: The project directory of the session.
        config: Claude Code's settings for the session; passed on, unused by the runners.
        hidden: Names of environment variables hidden from Claude Code; passed on, unused.

    Returns:
        A runner for the script file `SCRIPT_ENV` names, else the scenario runner of
        `uitest/fake_server.py`.
    """
    chosen = os.environ.get(SCRIPT_ENV)
    if chosen:
        return FakeRunner(FakeScript.load(Path(chosen)), workspace)
    return fake_server.make_runner(workspace, config, hidden)


def main() -> int:
    """Run `calu` with the scripted runner and the fake checker models.

    Side effect: replaces `calu.cli.serve_sessions` and `calu.cli.run_session` for this
    process.

    Returns:
        The exit code of `calu.cli.main`.
    """
    fakes = {"runner_factory": make_runner, "checker_models": fake_models()}
    calu.cli.serve_sessions = functools.partial(serve, **fakes)
    calu.cli.run_session = functools.partial(run_session, **fakes)
    return calu.cli.main()


if __name__ == "__main__":
    sys.exit(main())
