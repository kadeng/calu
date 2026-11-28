"""The folder part of the web interface's "new session" dialog (`calu.web.start`, D270).

Works in a temporary projects directory; `git` is a fake that records its arguments and makes
the folder a clone would make. Nothing else is touched, nothing is printed.
"""

import subprocess
from pathlib import Path

import pytest

from calu.web import start


def projects_dir(tmp_path: Path) -> Path:
    """Make a projects directory with two folders and a hidden one.

    Args:
        tmp_path: The test's directory.

    Returns:
        The resolved projects directory.
    """
    root = (tmp_path / "projects").resolve()
    for name in ("shop", "api", ".cache", "shop/web"):
        (root / name).mkdir(parents=True)
    return root


def test_the_dialog_lists_the_visible_folders_of_one_level(tmp_path: Path) -> None:
    """Hidden folders are not offered (a server starts no session there), and nothing
    outside the projects directory can be listed."""
    root = projects_dir(tmp_path)
    assert start.listing(root, "") == ["api", "shop"]
    assert start.listing(root, "shop") == ["web"]
    with pytest.raises(start.StartRefused, match="not in"):
        start.listing(root, "../..")
    with pytest.raises(start.StartRefused, match="hidden"):
        start.listing(root, ".cache")


def test_a_folder_is_chosen_made_or_refused(tmp_path: Path) -> None:
    """No name and no address: the chosen folder itself. A name: a new, empty folder in
    it. A name with a path separator, a leading dot or dash, or one that exists, is refused."""
    root = projects_dir(tmp_path)
    assert start.prepare(root, "shop", "", "") == root / "shop"
    made = start.prepare(root, "shop", "new-thing", "")
    assert made == root / "shop" / "new-thing" and made.is_dir()
    for bad in ("../escape", ".hidden", "-x", "a/b"):
        with pytest.raises(start.StartRefused):
            start.prepare(root, "", bad, "")
    with pytest.raises(start.StartRefused, match="exists already"):
        start.prepare(root, "", "api", "")
    with pytest.raises(start.StartRefused, match="no folder"):
        start.prepare(root, "missing", "", "")


def test_only_https_ssh_and_scp_like_addresses_are_cloned() -> None:
    """git's other transports include `ext::`, which runs a command; an address that starts
    with a dash would be read as an option."""
    assert start.clone_refusal("https://github.com/kadeng/calu.git") is None
    assert start.clone_refusal("ssh://git@host/repo.git") is None
    assert start.clone_refusal("git@github.com:kadeng/calu.git") is None
    for bad in (
        "ext::sh -c touch% /tmp/x",
        "--upload-pack=x",
        "file:///etc",
        "/srv/repo",
        "http://x",
    ):
        assert start.clone_refusal(bad) is not None, bad
    assert start.repository_name("https://github.com/kadeng/calu.git") == "calu"
    assert start.repository_name("git@github.com:kadeng/calu") == "calu"


def test_a_clone_runs_git_with_only_safe_transports_and_no_prompt(tmp_path: Path) -> None:
    """The clone gets the repository's name as its folder unless a name is given.

    It runs with https and ssh as the only transports, with no terminal prompt, and with ssh
    in batch mode. A failed clone is a refusal that carries git's last line.
    """
    root = projects_dir(tmp_path)
    seen: list[dict[str, object]] = []

    def git(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        seen.append({"command": command, **options})
        Path(command[-1]).mkdir()
        return subprocess.CompletedProcess(command, 0, "", "")

    made = start.prepare(root, "shop", "", "https://example.org/team/tool.git", git)
    assert made == root / "shop" / "tool"
    command = seen[0]["command"]
    assert isinstance(command, list)
    transports = [
        "protocol.allow=never",
        "protocol.https.allow=always",
        "protocol.ssh.allow=always",
    ]
    assert command[0] == "git" and command[2:7:2] == transports
    assert command[-3:] == ["--", "https://example.org/team/tool.git", str(made)]
    env = seen[0]["env"]
    assert isinstance(env, dict)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert seen[0]["timeout"] == start.CLONE_TIMEOUT_S

    def failing(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 128, "", "Cloning...\nfatal: not found")

    with pytest.raises(start.StartRefused, match="git clone failed: fatal: not found"):
        start.prepare(root, "", "other", "https://example.org/missing.git", failing)
    with pytest.raises(start.StartRefused, match="https://, ssh://"):
        start.prepare(root, "", "", "file:///srv/x.git", git)


def test_a_conversation_is_continued_only_in_its_own_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from calu.claude.transcripts import Transcript

    root = projects_dir(tmp_path)
    known = "0b2a6a8e-3c1d-4f5e-9a7b-1c2d3e4f5a6b"
    seen: list[Path] = []

    def kept(directory: Path) -> list[Transcript]:
        """Stand in for Claude Code's transcripts: one conversation, in `shop` only.

        Args:
            directory: The folder asked about.

        Returns:
            Its conversations.
        """
        seen.append(directory)
        if directory != root / "shop":
            return []
        return [Transcript(known, datetime.now(UTC), "fix the cart")]

    monkeypatch.setattr(start, "transcripts", kept)
    assert start.check_resume(root, "shop", "", "", known.upper()) == known
    with pytest.raises(start.StartRefused, match="keeps no conversation"):
        start.check_resume(root, "api", "", "", known)
    with pytest.raises(start.StartRefused, match="no Claude session id"):
        start.check_resume(root, "shop", "", "", "not-an-id")
    with pytest.raises(start.StartRefused, match="leave the new folder"):
        start.check_resume(root, "shop", "new", "", known)
    with pytest.raises(start.StartRefused, match="leave the new folder"):
        start.check_resume(root, "shop", "", "https://example.org/x.git", known)
    with pytest.raises(start.StartRefused):
        start.check_resume(root, ".cache", "", "", known)  # a hidden folder is never served
    assert seen == [root / "shop", root / "api"]


def test_a_conversation_is_described_in_one_line() -> None:
    from calu import herd
    from calu.web.app import _conversation_label

    row = herd.ConversationRow(
        claude_session_id="0b2a6a8e-3c1d-4f5e-9a7b-1c2d3e4f5a6b",
        age_s=7200,
        last_input="fix the\ncart " + "x" * 100,
        turns=3,
        running=False,
        outside=True,
    )
    label = _conversation_label(row)
    assert label.startswith(f"{herd.age_text(7200)} ago · started with claude · fix the cart ")
    assert len(label.split(" · ")[-1]) == 80 and label.endswith("…")
    running = herd.ConversationRow("id", 5, "", 1, True)
    assert _conversation_label(running) == f"{herd.age_text(5)} ago · running · (no message)"


def test_a_folder_with_a_nul_character_is_refused(tmp_path: Path) -> None:
    with pytest.raises(start.StartRefused, match="NUL"):
        start.folder(projects_dir(tmp_path), "shop\0x")
