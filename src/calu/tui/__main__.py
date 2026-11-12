"""The full-screen client on a scripted session: `python -m calu.tui [scenario]`.

No calu process, no Claude and no database are involved: the client is attached to
`calu.tui.fake.FakeSession`, which first replays the scenario as history and then answers the
requests the client sends (a message is queued, a decision resolves the escalation, a side
question gets a scripted answer). For looking at the client and for work on it.
"""

import sys
from pathlib import Path

from calu.tui.app import CaluApp
from calu.tui.fake import FakeAdmin, FakeSession, fake_connector
from calu.tui.scenarios import SCENARIOS, preload


def main(arguments: list[str]) -> int:
    """Run the client on a scenario.

    Args:
        arguments: The command line without the program name: optionally a scenario name.

    Returns:
        0, or 2 for an unknown scenario (its names are printed).
    """
    name = arguments[0] if arguments else "showcase"
    if name not in SCENARIOS:
        print(f"scenarios: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2
    session = FakeSession(workspace=str(Path.cwd()))  # so that f4 and @ work on real files
    preload(session, SCENARIOS[name]())
    CaluApp(fake_connector(session), admin=FakeAdmin(session)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
