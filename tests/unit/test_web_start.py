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
    """The clone gets the repository's name as its folder unless a name is given, runs with
    https and ssh as the only transports, no terminal prompt and ssh in batch mode; a failed
    clone is a refusal with git's last line."""
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
