"""Does an installed calu still find the files it ships with (PLAN.md D41)?

calu reads five kinds of data out of its own package at runtime: the schema migrations, the
default checker instructions, the default rules, the eval cases, and the web interface's
templates and static files. In a source checkout they are simply there, so a mistake in
`pyproject.toml` -- or a new data directory nobody declared -- is invisible until someone
installs the wheel.

These tests fail in the source tree too, but they are the cheap half of the guard: they go
through `importlib.resources` exactly as the shipped code does, assert every resource the code
names is present and non-empty, and assert that the set of data directories has not grown
beyond the ones listed here. A new directory makes `test_no_undeclared_data_directory` fail,
which is the reminder to check the built wheel (`uv build`, then list the archive).
"""

from importlib import resources
from pathlib import Path

import pytest

from calu.db.connection import migrations
from calu.db.local import compose_file
from calu.defaults import default_instructions
from calu.evals.cases import builtin_cases
from calu.setup import recommended_rules

CHECKERS = ["command", "edit", "changeset", "narrator", "subagent-result"]
"""The checker instruction files that `calu setup` installs."""

PAGES = [
    "base.html",
    "checker.html",
    "checkers.html",
    "escalation.html",
    "escalations.html",
    "fleet.html",
    "live.html",
    "rules.html",
    "session.html",
    "settings.html",
    "summary.html",
    "triage.html",
]
"""The templates `calu.web.app` renders; a missing one is a 500 on that page."""

ASSETS = ["calu.css", "calu.js", "live.js", "triage.js"]
"""The static files the pages reference."""

DATA_DIRECTORIES = {
    "calu.db": {"migrations"},
    "calu.defaults": {"checkers", "evals", "rules"},
    "calu.web": {"static", "templates"},
}
"""Every directory of non-Python files in the package, per package it belongs to."""


def test_the_migrations_are_readable_and_numbered_without_a_gap() -> None:
    """Every migration ships, carries SQL, and the versions run 1..n without a gap."""
    found = migrations()
    assert found, "no migration shipped with the package"
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    for migration in found:
        assert migration.sql.strip(), f"{migration.name} is empty"


@pytest.mark.parametrize("name", CHECKERS)
def test_each_default_checker_instruction_ships(name: str) -> None:
    """`calu setup` can install each checker because its instructions are in the package.

    Args:
        name: The checker whose default instructions must exist.
    """
    text = default_instructions(name)
    assert len(text) > 200, f"the default instructions of {name} look truncated"


def test_the_default_rules_and_eval_cases_ship() -> None:
    """The deny list and the eval set are readable out of the package."""
    assert recommended_rules(), "the recommended rule set is not in the package"
    assert builtin_cases(), "no eval case shipped with the package"


def test_the_local_compose_file_ships() -> None:
    """`calu db up` can start PostgreSQL because its compose file is in the package."""
    path = compose_file()
    assert path.is_file()
    assert "postgres" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", PAGES)
def test_each_page_template_ships(page: str) -> None:
    """Every template the web app renders is present and not empty.

    Args:
        page: The template file name.
    """
    path = Path(str(resources.files("calu.web").joinpath("templates").joinpath(page)))
    assert path.is_file(), f"{page} is missing from the package"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("asset", ASSETS)
def test_each_static_asset_ships(asset: str) -> None:
    """Every static file the pages reference is present and not empty.

    Args:
        asset: The static file name.
    """
    path = Path(str(resources.files("calu.web").joinpath("static").joinpath(asset)))
    assert path.is_file(), f"{asset} is missing from the package"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("package", sorted(DATA_DIRECTORIES))
def test_no_undeclared_data_directory(package: str) -> None:
    """A new directory of package data must be declared here and checked in a built wheel.

    Args:
        package: The package whose directories are compared with `DATA_DIRECTORIES`.
    """
    present = {
        entry.name
        for entry in resources.files(package).iterdir()
        if entry.is_dir() and entry.name != "__pycache__"
    }
    assert present == DATA_DIRECTORIES[package], (
        f"the data directories of {package} changed; check `uv build` ships the new one, "
        "then update DATA_DIRECTORIES"
    )


def test_the_package_says_what_it_is_and_where_it_lives() -> None:
    """The metadata is the PyPI page: without it a visitor gets no link and no platform.

    `importlib.metadata` reads it from the installed distribution, which is what a user gets,
    not from `pyproject.toml`.
    """
    from importlib.metadata import metadata

    found = metadata("calu")
    classifiers = found.get_all("Classifier") or []
    # Linux only, on the machine where Claude Code runs (REQUIREMENTS 3).
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Programming Language :: Python :: 3.14" in classifiers
    # The state has to be stated rather than implied by a version number.
    assert any(c.startswith("Development Status ::") for c in classifiers)
    # The package is fully typed and ships the marker that says so.
    assert "Typing :: Typed" in classifiers
    assert Path(str(resources.files("calu").joinpath("py.typed"))).is_file()

    urls = " ".join(found.get_all("Project-URL") or [])
    assert "github.com/kadeng/calu" in urls, "a visitor must be able to find the repository"
    assert "Issues" in urls
    assert found["License-Expression"] == "GPL-2.0-only" or "GPL-2.0" in str(found.get("License"))
