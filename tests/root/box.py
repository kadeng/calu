"""Whether the root-only tests run here, and which users they use (see `tests.root`)."""

import os

AS_ROOT = os.geteuid() == 0 and os.environ.get("CALU_ROOT_TESTS") == "1"
"""Whether the tests of this package run: as root, and asked for."""

WHY_NOT = "needs root and CALU_ROOT_TESTS=1 (run in the root box: uitest/rootbox)"
"""The reason shown for a skipped test."""

OTHER_UID = int(os.environ.get("CALU_ROOT_TEST_UID", "1000"))
"""The unprivileged uid Claude Code stands in as; the root box's `ada`."""

THIRD_UID = int(os.environ.get("CALU_ROOT_TEST_THIRD_UID", "1002"))
"""Another user of the machine, who must reach nothing; the root box's `carol`."""
