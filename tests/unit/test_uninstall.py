"""`calu uninstall` (D267): what it plans, what it refuses, what it removes.

Every test works in a temporary directory with a fake docker (`FakeDocker`) and, where
processes matter, a fake /proc; nothing of the machine running the tests is looked at or
removed. Nothing is printed except by `cli.main`, whose output is captured.
"""

import os
import subprocess
from pathlib import Path

import pytest

from calu import cli, machine, uninstall
from calu.credentials import CredentialStore


class FakeDocker:
    """Answers the docker commands `calu.uninstall` runs, and records them."""

    def __init__(self, container: bool = False, volume: bool = False, mount: str = "") -> None:
        """Describe the Docker state.

        Args:
            container: Whether the compose project `calu-db` has a container.
            volume: Whether the named volume `calu-db_data` exists.
            mount: The bind source of the container's data mount ("" for none).
        """
        self.container, self.volume, self.mount = container, volume, mount
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Answer one command.

        Args:
            args: The command.

        Returns:
            A finished process with the output docker would give.
        """
        self.calls.append(args)
        out, code = "", 0
        if args[:3] == ["docker", "ps", "-a"]:
            out = "abc123\n" if self.container else ""
        elif args[:2] == ["docker", "inspect"]:
            out = f'[{{"Type":"bind","Source":"{self.mount}","Destination":"/var/lib/postgresql"}}]'
        elif args[:3] == ["docker", "volume", "inspect"]:
            code = 0 if self.volume else 1
        elif args[:2] == ["docker", "run"]:
            data = Path(args[args.index("-v") + 1].split(":")[0])
            for child in data.iterdir():
                child.unlink()
        return subprocess.CompletedProcess(args, code, out, "")


def no_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """A machine without Docker.

    Args:
        args: The command.

    Raises:
        FileNotFoundError: Always.
    """
    raise FileNotFoundError("docker")


def user_install(home: Path) -> Path:
    """Create a user's uv tool installation of calu with its command.

    Args:
        home: The home directory.

    Returns:
        The `calu/__init__.py` inside it.
    """
    env = home / ".local" / "share" / "uv" / "tools" / "calu"
    module = env / "lib" / "python3.14" / "site-packages" / "calu" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    command = home / ".local" / "bin" / "calu"
    command.parent.mkdir(parents=True)
    command.symlink_to(env / "bin" / "calu")
    (env / "uv-receipt.toml").write_text(
        f'[tool]\nentrypoints = [{{ name = "calu", install-path = "{command}" }}]\n'
    )
    return module


def test_a_users_run_plans_its_own_files_database_and_installation(tmp_path: Path) -> None:
    """As a user: the configuration and state directories, the local database whose files
    the user owns (container, data directory), and the uv tool installation with its
    command; prerequisites and projects are named as kept."""
    home = tmp_path / "home"
    module = user_install(home)
    (home / ".config" / "calu").mkdir(parents=True)
    data = home / ".local" / "state" / "calu" / "db"
    (data / "pg").mkdir(parents=True)
    docker = FakeDocker(container=True, mount=str(data / "pg"))

    chosen = uninstall.plan(os.geteuid() or 1000, {"HOME": str(home)}, module, docker)

    targets = [item.target for item in chosen.items]
    assert targets[:2] == ["calu-db", str(data)]
    assert str(home / ".config" / "calu") in targets
    assert str(home / ".local" / "state" / "calu") in targets
    assert str(home / ".local" / "bin" / "calu") in targets
    assert str(home / ".local" / "share" / "uv" / "tools" / "calu") in targets
    assert any(note.startswith("prerequisites (uv, Docker") for note in chosen.notes)
    assert any("what calu wrote into your projects" in note for note in chosen.notes)


def test_a_users_run_leaves_roots_database_and_says_who_removes_it(tmp_path: Path) -> None:
    """The Docker daemon is shared: a database whose data directory the user does not own
    is root's, and a user's run names `sudo calu uninstall` for it instead of removing it."""
    if os.geteuid() == 0:
        pytest.skip("needs a user that is not root")
    home = tmp_path / "home"
    module = user_install(home)
    docker = FakeDocker(container=True, mount="/")  # "/" belongs to root

    chosen = uninstall.plan(os.geteuid(), {"HOME": str(home)}, module, docker)

    assert not [item for item in chosen.items if item.kind in ("container", "data", "volume")]
    assert any("is root's" in note and "sudo calu uninstall" in note for note in chosen.notes)


def test_a_checkout_is_not_removed_and_roots_installation_is_named(tmp_path: Path) -> None:
    """calu run from a checkout (no uv receipt) is left to whoever installed it; a root
    installation next to it is named with the command that removes it."""
    home = tmp_path / "home"
    (home / ".config" / "calu").mkdir(parents=True)
    module = tmp_path / "checkout" / "src" / "calu" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    root_home = tmp_path / "usr-local-lib-calu"
    (root_home / "calu").mkdir(parents=True)

    chosen = uninstall.plan(1000, {"HOME": str(home)}, module, no_docker, root_home)

    assert [item.target for item in chosen.items] == [str(home / ".config" / "calu")]
    assert any("no uv tool installation" in note for note in chosen.notes)
    assert any(str(root_home) in note and "sudo calu uninstall" in note for note in chosen.notes)
    assert any("Docker is not installed" in note for note in chosen.notes)


def test_the_confirmation_must_be_the_phrase_and_typed_by_hand() -> None:
    """The phrase, exactly; an answer in under `MIN_SECONDS` is refused as automated; the
    end of the input is no confirmation."""
    times = iter([0.0, 3.0])
    uninstall.confirm(lambda _p: "remove calu", lambda: next(times))

    times = iter([0.0, 3.0])
    with pytest.raises(uninstall.NotConfirmed, match='not "remove calu"'):
        uninstall.confirm(lambda _p: "remove", lambda: next(times))

    times = iter([0.0, 1.9])
    with pytest.raises(uninstall.NotConfirmed, match="looks automated; nothing was removed"):
        uninstall.confirm(lambda _p: "remove calu", lambda: next(times))

    def ended(_prompt: str) -> str:
        raise EOFError

    with pytest.raises(uninstall.NotConfirmed, match="no confirmation was given"):
        uninstall.confirm(ended)


def test_removal_runs_the_database_first_and_reports_each_item(tmp_path: Path) -> None:
    """The container goes with `docker compose down --volumes`, the database files that the
    container's user owns go through one run of the postgres image, then the directories;
    a failure is reported and the other items still go."""
    data = tmp_path / "db"
    (data / "pg").mkdir(parents=True)
    (data / "pg" / "PG_VERSION").write_text("18")
    config = tmp_path / "config"
    config.mkdir()
    chosen = uninstall.Plan(
        [
            uninstall.Item("path", str(config), "logins"),
            uninstall.Item("data", str(data), "files"),
            uninstall.Item("container", "calu-db", "container"),
        ]
    )
    docker = FakeDocker()
    said: list[str] = []

    failed = uninstall.remove(
        chosen, tmp_path / "compose.yml", "postgres:18", 1000, docker, said.append
    )

    assert failed == []
    assert said == ["removed: calu-db", f"removed: {data}", f"removed: {config}"]
    assert docker.calls[0][:2] == ["docker", "compose"] and "--volumes" in docker.calls[0]
    assert docker.calls[1][:2] == ["docker", "run"] and "postgres:18" in docker.calls[1]
    assert not data.exists() and not config.exists()


def test_running_calu_processes_are_found_but_not_this_one(tmp_path: Path) -> None:
    """`calu serve` and an editor's `calu ide` count; this process, its parent (sudo) and
    other programs do not."""
    proc = tmp_path / "proc"

    def process(pid: int, parent: int, *argv: str) -> None:
        entry = proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "stat").write_text(f"{pid} (x) S {parent} 0 0")
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    process(10, 1, "/usr/local/bin/calu", "serve", "--sudo")
    process(11, 1, "/home/u/.local/bin/calu", "ide")
    process(12, 1, "vim", "calu")
    process(20, 19, "/usr/local/bin/calu", "uninstall")
    process(19, 1, "sudo", "calu", "run")  # the parent of this process: left out

    found = machine.calu_processes(0, 20, proc)

    assert sorted(found) == [
        (10, "/usr/local/bin/calu serve --sudo"),
        (11, "/home/u/.local/bin/calu ide"),
    ]


def test_calu_uninstall_dry_run_lists_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`calu uninstall --dry-run` prints the plan and the kept things and touches nothing."""
    home = tmp_path / "home"
    (home / ".config" / "calu").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setattr(uninstall, "run_command", no_docker)
    store = CredentialStore(tmp_path / "store.json")

    assert cli.main(["uninstall", "--dry-run"], lambda: store) == 0

    said = capsys.readouterr().out
    assert f"{home / '.config' / 'calu'}: your calu logins and checker keys" in said
    assert "(--dry-run: nothing was removed)" in said
    assert (home / ".config" / "calu").is_dir()
