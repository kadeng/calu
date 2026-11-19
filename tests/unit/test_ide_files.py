"""The rules of the editor's file synchronisation that need no file access (`calu.ide.files`)."""

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from calu.acp.peer import RpcError
from calu.ide import files
from calu.ide.files import (
    Change,
    ChangeLog,
    HashCache,
    Listing,
    TurnFiles,
    changed_paths,
    clean_path,
    decode_content,
    has_plain_name,
    int_param,
    is_excluded,
    listed_paths,
    parse_cursor,
    poll_interval,
    private_prefixes,
    session_prefixes,
    sha_param,
    status_paths,
    under,
)

SHA = hashlib.sha256(b"x").hexdigest()


def details(error: object) -> dict[str, Any]:
    """Return the `data` object of an error, which these tests expect to be there.

    Args:
        error: An `RpcError` or the editor's `Failed`.
    """
    found = getattr(error, "data", None)
    assert isinstance(found, dict), found
    return found


def reason_of(value: object, prefixes: tuple[str, ...] = ()) -> str:
    """Return the reason `clean_path` refuses a value with.

    Args:
        value: The request's value.
        prefixes: The excluded directories.
    """
    with pytest.raises(RpcError) as refused:
        clean_path(value, prefixes)
    assert refused.value.code == files.PATH_REFUSED
    return details(refused.value)["reason"]


# ------------------------------------------------------------------------------- paths


@pytest.mark.parametrize("path", ["a.py", "src/calu/a.py", "a b/ü.txt", ".github/x.yml", "a..b/c"])
def test_a_canonical_relative_path_is_returned_unchanged(path: str):
    assert clean_path(path) == path


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../a", "a/../b", "a/..", "..", "C:/a", "c:a", "C:\\a", "//host/share"],
)
def test_a_path_that_leaves_the_root_is_refused_as_outside(path: str):
    assert reason_of(path) in ("outside", "invalid")
    # Absolute paths, drive letters and parent segments name the reason "outside" unless
    # another rule (the backslash) already made the value invalid.
    if "\\" not in path:
        assert reason_of(path) == "outside"


@pytest.mark.parametrize(
    "path",
    [
        "",
        None,
        7,
        ["a"],
        "a\0b",
        "a\nb",
        "a\tb",
        "a\x7fb",
        "a\x85b",
        "a\\b",
        "a//b",
        "./a",
        "a/./b",
        "a/",
        "a/" * 3000,
        "x" * 300,
        "a/\udcff",
    ],
)
def test_a_malformed_path_is_refused_as_invalid(path: object):
    assert reason_of(path) == "invalid"


@pytest.mark.parametrize(
    "path",
    [
        ".git",
        ".git/config",
        "sub/.git/hooks/pre-commit",
        ".GIT/config",
        ".agent/traces/2026/x.md",
        "own/state/file",
        ".calu-upload-abc",
        "src/.calu-upload-abc",
    ],
)
def test_a_path_in_an_excluded_directory_is_refused(path: str):
    assert reason_of(path, (".agent/traces", "own/state")) == "excluded"


def test_excluded_prefixes_match_whole_segments_only():
    assert not is_excluded(".agent/traces-old/x", (".agent/traces",))
    assert not is_excluded(".gitignore", ())
    assert not is_excluded("a/.github/x", ())
    assert is_excluded("anything/at/all", ("",))


def test_names_the_protocol_cannot_carry_are_recognised():
    assert has_plain_name("src/a.py")
    for name in ("a\nb", "a\\b", "a/\udce9", "", "a//b", "x" * 256):
        assert not has_plain_name(name)


def test_calus_own_directories_inside_the_project_are_prefixes(tmp_path: Path):
    root = tmp_path / "project"
    environ = {"XDG_CONFIG_HOME": str(root / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    assert private_prefixes(root, environ) == ["cfg/calu"]
    # A project inside one of calu's directories is excluded as a whole.
    inside = tmp_path / "state" / "calu" / "snapshots"
    assert private_prefixes(inside, environ) == [""]


def test_the_directories_a_session_names_are_checked_like_paths():
    assert session_prefixes([".agent/traces/", "../x", "/abs", 5, "ok/dir"]) == (
        ".agent/traces",
        "ok/dir",
    )
    assert session_prefixes("no list") == ()
    assert session_prefixes(None) == ()


# -------------------------------------------------------------------------- parameters


def test_a_hash_parameter_must_be_present_and_well_formed():
    assert sha_param({"base_sha256": SHA}, "base_sha256", nullable=True) == SHA
    assert sha_param({"base_sha256": None}, "base_sha256", nullable=True) is None
    for params in ({}, {"base_sha256": SHA.upper()}, {"base_sha256": SHA[:-1]}, {"base_sha256": 1}):
        with pytest.raises(RpcError) as bad:
            sha_param(params, "base_sha256", nullable=True)
        assert bad.value.code == files.INVALID_PARAMS
    with pytest.raises(RpcError):
        sha_param({"base_sha256": None}, "base_sha256", nullable=False)


def test_an_integer_parameter_is_bounded():
    assert int_param({}, "mode", None, 0, 0o777) is None
    assert int_param({"mode": 0o755}, "mode", None, 0, 0o777) == 0o755
    for value in (-1, 0o1000, 0o4755, True, "420", 1.5):
        with pytest.raises(RpcError) as bad:
            int_param({"mode": value}, "mode", None, 0, 0o777)
        assert bad.value.code == files.INVALID_PARAMS


def test_content_is_size_checked_before_it_is_decoded_and_must_be_canonical_base64():
    assert decode_content(base64.b64encode(b"\x00\xffhello").decode()) == b"\x00\xffhello"
    assert decode_content("") == b""
    whole = base64.b64encode(b"x" * 300).decode()
    assert decode_content(whole, limit=300) == b"x" * 300
    with pytest.raises(RpcError) as large:
        decode_content(base64.b64encode(b"x" * 301).decode(), limit=300)
    assert large.value.code == files.TOO_LARGE
    assert details(large.value)["limit"] == 300 and details(large.value)["size"] >= 300
    for bad in ("not base64!", "YQ", "YQ==\n", "YQ=a", 5, None, b"YQ=="):
        with pytest.raises(RpcError) as refused:
            decode_content(bad)
        assert refused.value.code == files.INVALID_PARAMS


def test_the_request_limit_is_one_mebibyte():
    assert files.REQUEST_BYTES == 1024 * 1024
    assert decode_content(base64.b64encode(bytes(files.REQUEST_BYTES)).decode())
    with pytest.raises(RpcError) as large:
        decode_content("A" * (4 * ((files.REQUEST_BYTES + 2) // 3) + 4))
    assert large.value.code == files.TOO_LARGE


# -------------------------------------------------------------------------- hash cache


def test_the_hash_cache_answers_only_for_the_stored_signature():
    now = 10_000_000_000
    cache = HashCache(racy_ns=2_000_000_000, clock=lambda: now)
    old = (3, now - 5_000_000_000, 11)
    cache.put("a", old, SHA)
    assert cache.get("a", old) == SHA
    assert cache.get("a", (4, old[1], 11)) is None
    assert cache.get("a", (3, old[1] + 1, 11)) is None
    assert cache.get("a", (3, old[1], 12)) is None
    assert cache.get("b", old) is None
    cache.forget("a")
    assert cache.get("a", old) is None and cache.last("a") is None


def test_the_hash_of_a_file_written_a_moment_ago_is_not_kept():
    now = 10_000_000_000
    cache = HashCache(racy_ns=2_000_000_000, clock=lambda: now)
    fresh = (3, now - 1_000_000, 11)
    cache.put("a", fresh, SHA)
    assert cache.get("a", fresh) is None, "the same tick could hold another write"
    assert cache.last("a") == SHA
    assert cache.seen("a", fresh) == SHA and cache.seen("a", (4, fresh[1], 11)) is None


# ---------------------------------------------------------------------------- listings


def test_git_listings_are_parsed():
    assert listed_paths(b"b.py\0a/c.py\0nested/\0b.py\0") == ["a/c.py", "b.py", "nested"]
    assert listed_paths(b"") == []
    status = b"\0".join(
        [
            b"# branch.oid abc",
            b"1 .M N... 100644 100644 100644 aaaa bbbb src/a b.py",
            b"1 .D N... 100644 100644 000000 aaaa bbbb gone.py",
            b"2 R. N... 100644 100644 100644 aaaa bbbb R100 new name.py",
            b"old name.py",
            b"u UU N... 100644 100644 100644 100644 aaaa bbbb cccc conflict.py",
            b"? untracked dir/x.txt",
            b"! ignored.txt",
            b"",
        ]
    )
    assert status_paths(status) == [
        "conflict.py",
        "gone.py",
        "new name.py",
        "old name.py",
        "src/a b.py",
        "untracked dir/x.txt",
    ]
    # In a subdirectory of the repository the paths become relative to it.
    assert status_paths(status, "src/") == ["a b.py"]


def test_a_selection_covers_files_and_directories():
    assert under("src/a.py", ["src"]) and under("src/a.py", ["src/a.py"])
    assert not under("src2/a.py", ["src"]) and not under("a.py", [])


def test_a_listing_is_cut_into_pages_and_a_cursor_names_the_next():
    listing = Listing(head=None, git=False, paths=[f"f{i:05}" for i in range(4500)], missing=[])
    first, cursor = listing.page(0)
    assert len(first) == files.PAGE_ENTRIES and cursor == f"{listing.id}:2000"
    assert parse_cursor(cursor) == (listing.id, 2000)
    second, cursor = listing.page(2000)
    third, last = listing.page(4000)
    assert len(second) == 2000 and len(third) == 500 and last is None
    assert first + second + third == listing.paths
    # Long paths end a page early, so that a response stays below the line limit.
    long = Listing(head=None, git=False, paths=["d/" + "x" * 4000] * 500, missing=[])
    page, more = long.page(0)
    assert len(page) < 100 and more is not None
    for bad in (None, 5, "", "abc", "abc:", "abc:-1", ":5", "abc:1e3"):
        with pytest.raises(RpcError):
            parse_cursor(bad)


# -------------------------------------------------------------------------- change log


def test_changes_of_one_path_merge_and_get_one_sequence_number_per_notification():
    log = ChangeLog(batch=2)
    assert not log and log.cut() == []
    log.add(Change("a", "h0", "h1", "ide"))
    log.add(Change("a", "h1", "h2", "agent"))
    log.add(Change("b", None, "h9", "other"))
    log.add(Change("c", "h3", None, "other"))
    notes = log.cut()
    assert [note["seq"] for note in notes] == [1, 2] and {n["epoch"] for n in notes} == {log.epoch}
    assert notes[0]["changes"] == [
        {
            "path": "a",
            "before_sha256": "h0",
            "after_sha256": "h2",
            "deleted": False,
            "origin": "agent",
        },
        {
            "path": "b",
            "before_sha256": None,
            "after_sha256": "h9",
            "deleted": False,
            "origin": "other",
        },
    ]
    assert notes[1]["changes"][0]["deleted"] is True
    assert log.seq == 2 and not log


def test_a_file_back_at_its_known_content_is_not_reported():
    log = ChangeLog()
    log.add(Change("a", "h0", "h1", "other"))
    log.add(Change("a", "h1", "h0", "other"))
    assert log.cut() == []
    # Created and deleted again: the content before is unknown, so it is reported.
    log.add(Change("b", None, "h1", "other"))
    log.add(Change("b", "h1", None, "other"))
    assert [c["path"] for c in log.cut()[0]["changes"]] == ["b"]


def test_a_skipped_path_replaces_its_waiting_change_and_shares_the_batch():
    log = ChangeLog(batch=2)
    log.add(Change("big", "h0", "h1", "other"))
    log.skip("big", "too-large")  # the file grew above the limit: the later fact wins
    log.skip("pipe", "not-regular")
    log.add(Change("a", None, "h2", "other"))
    assert bool(log)
    notes = log.cut()
    assert [(n["seq"], [c["path"] for c in n["changes"]], n["skipped"]) for n in notes] == [
        (1, ["a"], [{"path": "big", "reason": "too-large"}]),
        (2, [], [{"path": "pipe", "reason": "not-regular"}]),
    ]
    log.skip("x", "symlink")
    log.add(Change("x", None, "h3", "other"))  # a regular file again
    assert [n["skipped"] for n in log.cut()] == [[]]


def test_a_replay_is_complete_only_for_the_own_epoch_without_a_dropped_notification():
    log = ChangeLog(kept=3)
    for index in range(5):
        log.add(Change(f"f{index}", None, "h", "other"))
        log.cut()
    assert log.since(None, None) == []
    assert [note["seq"] for note in log.since(3, log.epoch) or []] == [4, 5]
    assert log.since(5, log.epoch) == []
    assert log.since(1, log.epoch) is None, "notification 2 was dropped"
    assert log.since(3, "another epoch") is None
    assert log.since(3, None) is None and log.since(None, log.epoch) is None
    assert log.since(9, log.epoch) is None
    log.gap()
    assert log.since(5, log.epoch) is None, "nothing was watched after the gap"
    log.add(Change("late", None, "h", "other"))
    log.cut()
    assert [note["seq"] for note in log.since(6, log.epoch) or []] == []


# --------------------------------------------------------------------- the turn's files


def test_the_files_of_the_running_turn_are_held_until_it_ends(tmp_path: Path):
    turn = TurnFiles(tmp_path)
    turn.attach("s1", "idle", [])
    assert turn.event("session.state", {"state": "running"}) is False
    turn.event("edit.diff", {"tool_use_id": "t1", "path": "src/a.py", "before": "", "after": "x"})
    turn.event(
        "tool.use",
        {"tool_use_id": "t2", "name": "Write", "input": {"file_path": str(tmp_path / "b.py")}},
    )
    turn.event("tool.use", {"tool_use_id": "t3", "name": "Bash", "input": {"command": "make"}})
    turn.event("tool.files", {"tool_use_id": "t3", "files": [{"raw": "c", "path": "c.txt"}]})
    turn.event("tool.use", {"tool_use_id": "t4", "name": "Read", "input": {"file_path": "r.py"}})
    assert [turn.held(p) for p in ("src/a.py", "b.py", "c.txt", "r.py", "other.py")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    for state in ("checking", "tool_call", "escalated"):
        turn.event("session.state", {"state": state})
        assert turn.held("src/a.py")
    assert turn.event("turn.result", {}) is True
    assert not turn.held("src/a.py")
    turn.event("edit.diff", {"tool_use_id": "t5", "path": "src/a.py"})
    assert turn.held("src/a.py")
    assert turn.event("session.state", {"state": "idle"}) is True
    assert not turn.held("src/a.py")
    # A path outside the workspace is not a file of the project.
    turn.event("session.state", {"state": "running"})
    turn.event("edit.diff", {"tool_use_id": "t6", "path": "/etc/hosts"})
    turn.event("edit.diff", {"tool_use_id": "t6", "path": "../up.py"})
    assert not turn.held("etc/hosts") and not turn.held("up.py")
    turn.detach()
    assert turn.session_id == "" and turn.state == "stopped"


def test_an_attach_replays_the_events_and_ends_with_the_snapshots_state(tmp_path: Path):
    turn = TurnFiles(tmp_path)
    events = [
        {"method": "session.state", "params": {"state": "running"}},
        {"method": "edit.diff", "params": {"tool_use_id": "t1", "path": "a.py"}},
    ]
    turn.attach("s1", "tool_call", events)
    assert turn.session_id == "s1" and turn.held("a.py")
    turn.attach("s2", "idle", events)
    assert not turn.held("a.py")


def test_the_hints_attribute_changes_to_the_agent(tmp_path: Path):
    turn = TurnFiles(tmp_path)
    turn.attach("s1", "running", [])
    assert turn.take_hints().origin("a.py") == "other"
    turn.event("edit.diff", {"tool_use_id": "t1", "path": "a.py"})
    turn.take_hints()  # a scan before the edit ran
    assert turn.event("tool.result", {"tool_use_id": "t1"}) is True
    hints = turn.take_hints()
    assert hints.origin("a.py") == "agent" and hints.origin("b.py") == "other"
    # While a command runs, and in the look after it, every change is the agent's.
    turn.event("tool.use", {"tool_use_id": "t2", "name": "Bash", "input": {}})
    assert turn.take_hints().origin("anything") == "agent"
    turn.event("tool.result", {"tool_use_id": "t2"})
    assert turn.take_hints().origin("anything") == "agent"
    assert turn.take_hints().origin("anything") == "other"


# ------------------------------------------------------------------------------- scans


def test_two_scans_are_compared_by_signature():
    known = {"same": (1, 1, 1), "touched": (1, 1, 2), "gone": (1, 1, 3)}
    current = {"same": (1, 1, 1), "touched": (1, 2, 2), "new": (5, 5, 5)}
    assert changed_paths(known, current) == (["new", "touched"], ["gone"])


def test_a_slow_scan_lengthens_the_wait_within_bounds():
    waits = [poll_interval(0.01, 2.0), poll_interval(0.5, 2.0), poll_interval(60.0, 2.0)]
    assert waits == pytest.approx([2.0, 10.0, 30.0])
    assert poll_interval(0.0, 0.05) == pytest.approx(0.05)


def test_the_capabilities_name_what_the_specification_defines():
    assert files.CAPABILITIES == ("cwd", "ping", "paths", "fs", "fs.watch")
    assert [files.PATH_REFUSED, files.NO_MANIFEST] == [-32030, -32039]
