"""Every text calu ships is known by its hash, also its earlier versions (PLAN.md D248)."""

import json
import pwd
import socket
from importlib import resources

import pytest

from calu.defaults.instructions import (
    SHIPPED_TEXTS,
    default_instructions,
    shipped_status,
    superseded_instructions,
    text_hash,
    without_test_triggers,
)
from calu.homes import short_path


def shipped_table() -> dict[str, list[list[str]]]:
    return json.loads(
        resources.files("calu.defaults").joinpath("checkers/shipped.json").read_text("utf-8")
    )


@pytest.mark.parametrize("name", SHIPPED_TEXTS)
def test_the_present_text_is_in_the_list_of_shipped_versions(name: str) -> None:
    """A changed default fails here until `scripts/shipped_instructions.py` ran again."""
    hashes = [row[0] for row in shipped_table()[name]]
    assert text_hash(default_instructions(name)) in hashes, (
        f"{name}.md changed: run `uv run python scripts/shipped_instructions.py`"
    )
    for earlier in superseded_instructions(name):
        assert text_hash(earlier) in hashes


def test_a_stored_text_is_current_outdated_or_edited() -> None:
    present = default_instructions("command")
    assert shipped_status("command", present).kind == "current"
    assert shipped_status("command", present.rstrip() + "\r\n\n").kind == "current"
    bare = shipped_status("command", without_test_triggers(present))
    assert bare.kind == "current" and bare.triggers_removed
    old = superseded_instructions("command")[-1]
    status = shipped_status("command", old)
    assert status.kind == "outdated" and status.present == present
    assert "older version calu shipped" in status.sentence("x", "`update`")
    assert status.diff(old).startswith("--- shipped/command.md")
    old_bare = shipped_status("command", without_test_triggers(old))
    if text_hash(old) != text_hash(without_test_triggers(old)):
        assert old_bare.kind == "outdated" and old_bare.triggers_removed
        assert old_bare.present == without_test_triggers(present)
    edited = shipped_status("command", present + "\nAlso: never touch prod.\n")
    assert edited.kind == "edited"
    assert "+Also: never touch prod." in edited.diff(present + "\nAlso: never touch prod.\n")
    assert "stays as it is" in edited.sentence("x", "`update`")


def test_a_home_directory_is_written_as_a_tilde() -> None:
    me = pwd.getpwuid(__import__("os").getuid())
    host = socket.gethostname()
    assert short_path(me.pw_dir + "/work/shop", me.pw_name, host) == "~/work/shop"
    assert short_path(me.pw_dir, me.pw_name, None) == "~"
    assert short_path("/srv/shop", me.pw_name, host) == "/srv/shop"
    # Another host: its user database is not readable, the usual place is assumed.
    assert short_path("/home/dev/p", "dev", "elsewhere.example") == "~/p"
    assert short_path("/root/p", "root", "elsewhere.example") == "~/p"
    assert short_path("/home/ann/p", "dev", "elsewhere.example") == "/home/ann/p"
    root = pwd.getpwnam("root").pw_dir
    if root not in ("/", "") and me.pw_name != "root":
        assert short_path(root + "/x", me.pw_name, host) == "~root/x"
    assert short_path("/home/devil/p", "dev", "elsewhere.example") == "/home/devil/p"
