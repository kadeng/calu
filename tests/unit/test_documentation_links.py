"""Every link between calu's own Markdown files points at something that exists.

A link to a file that was renamed, or to a heading that was rewritten, is documentation that
lies about itself -- and nothing says so: Markdown renders a dead anchor as an ordinary link
that quietly scrolls nowhere. One was found by hand on 2026-11-10 (the guide's own table of
contents still pointed at a section that had been renamed), which is one too many to keep
checking by hand.

Only relative links are checked. An `http(s)` link needs the network and would make the suite
depend on other people's servers.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DOCUMENTS = (
    "README.md",
    "CALU_GUIDE.md",
    "LICENSE.md",
    "AGENTS.md",
    "HANDOFF.md",
    "PLAN.md",
    "REQUIREMENTS.md",
    "docs/TESTING.md",
    "docs/CONFIGURATION.md",
    "src/calu/defaults/checkers/README.md",
)

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
"""An inline Markdown link; the group is its target."""


def anchors_of(text: str) -> set[str]:
    """Return the heading anchors a Markdown document offers.

    The rule is GitHub's: lower-case the heading, drop everything that is not a word
    character, a space or a hyphen, then replace spaces with hyphens. `## The models: Claude
    Code's account` therefore becomes `the-models-claude-codes-account`.

    Args:
        text: The document's source.

    Returns:
        The anchors, without the leading `#`.
    """
    found: set[str] = set()
    for line in text.splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            found.add(re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-"))
    return found


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_relative_link_and_anchor_resolves(name: str) -> None:
    """Check one document's links against the files and headings they name.

    Args:
        name: The document, relative to the repository root.
    """
    text = (ROOT / name).read_text(encoding="utf-8")
    known = {other: anchors_of((ROOT / other).read_text(encoding="utf-8")) for other in DOCUMENTS}
    broken: list[str] = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        path = ((ROOT / name).parent / file_part).resolve() if file_part else (ROOT / name)
        if not path.exists():
            broken.append(f"{target}: no such file")
            continue
        relative = str(path.relative_to(ROOT))
        if anchor and relative in known and anchor not in known[relative]:
            broken.append(f"{target}: {relative} has no heading with that anchor")
    assert broken == [], f"{name}:\n  " + "\n  ".join(broken)


def test_the_check_would_notice_a_dead_anchor() -> None:
    """The anchor rule has to match GitHub's, or the test above passes on anything."""
    anchors = anchors_of("# calu\n## The models: Claude Code's account, and the checkers' own\n")
    assert "the-models-claude-codes-account-and-the-checkers-own" in anchors
    assert "calu" in anchors
    assert "the-models" not in anchors


def test_every_module_is_in_the_contributors_table() -> None:
    """AGENTS.md lists what each module of `src/calu` is for; a module it omits is one nobody finds.

    Five were missing when this was first checked on 2026-11-11 -- among them `honesty.py`,
    the whole honesty register, two days after it was written. A table that rots silently is
    the same kind of untruth as a stale count, so it is checked the same way.
    """
    table = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    package = ROOT / "src" / "calu"
    missing = []
    for entry in sorted(package.iterdir()):
        if entry.name.startswith(("_", ".")) or entry.name == "py.typed":
            continue
        key = f"`src/calu/{entry.name}/`" if entry.is_dir() else f"`src/calu/{entry.name}`"
        if entry.is_dir() or entry.suffix == ".py":
            if key not in table:
                missing.append(key)
    assert missing == [], f"AGENTS.md does not say what these are for: {', '.join(missing)}"
