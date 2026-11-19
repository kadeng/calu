"""The projects directory of `calu serve` and the relay port derived from it (PLAN D243)."""

import os
from pathlib import Path

import pytest

from calu import projects_dir
from calu.projects_dir import (
    RELAY_PORT_FIRST,
    RELAY_PORT_LAST,
    RESERVED_PORTS,
    NotServed,
    ProjectsDirRefused,
    check_projects_dir,
    refusal,
    relay_port,
    relay_ports,
    served_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/etc",
        "/etc/nginx",
        "/proc/1",
        "/sys",
        "/dev",
        "/boot",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/usr",
        "/usr/lib/python3",
        "/usr/local/src",
        "/usr/share",
        "/var/lib/calu",
        "/var/log",
        "/var/cache/apt",
        "/var/spool",
        "/run/user/1000",
        "/System/Library",
        "/Library/Application Support",
        "/Applications",
        "/private/etc",
        "/private/var/db",
        "/home/ada/.config",
        "/home/ada/.local/share/projects",
        "/home/.hidden/src",
    ],
)
def test_system_hidden_and_root_directories_are_refused(path: str) -> None:
    assert refusal(Path(path), claude_as_root=False) is not None


@pytest.mark.parametrize(
    "path", ["/home/ada", "/home/ada/src", "/srv/projects", "/opt/work", "/tmp/x", "/var/www"]
)
def test_ordinary_directories_are_served(path: str) -> None:
    assert refusal(Path(path), claude_as_root=False) is None


def test_roots_home_only_when_claude_code_runs_as_root() -> None:
    assert "root's home" in (refusal(Path("/root/src"), claude_as_root=False) or "")
    assert "root's home" in (refusal(Path("/var/root"), claude_as_root=False) or "")
    assert refusal(Path("/root/src"), claude_as_root=True) is None
    # A system directory stays refused also for root.
    assert refusal(Path("/etc"), claude_as_root=True) is not None


def test_a_directory_with_a_dot_inside_its_name_is_not_hidden() -> None:
    assert refusal(Path("/home/ada/my.project"), claude_as_root=False) is None


def test_check_resolves_symbolic_links_first(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert check_projects_dir(tmp_path / "src") == (tmp_path / "src").resolve()
    (tmp_path / "to-etc").symlink_to("/etc")
    with pytest.raises(ProjectsDirRefused, match=r"to-etc \(/etc\): it is inside /etc"):
        check_projects_dir(tmp_path / "to-etc")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").symlink_to(tmp_path / ".hidden")
    with pytest.raises(ProjectsDirRefused, match="hidden"):
        check_projects_dir(tmp_path / "visible")
    with pytest.raises(ProjectsDirRefused, match="--projects DIR"):
        check_projects_dir(tmp_path / ".hidden")
    (tmp_path / "to-root").symlink_to("/")
    with pytest.raises(ProjectsDirRefused, match="the root directory"):
        check_projects_dir(tmp_path / "to-root")
    with pytest.raises(ProjectsDirRefused, match="is not a directory"):
        check_projects_dir(tmp_path / "missing")


def test_check_takes_a_relative_path_from_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path / "src")
    assert check_projects_dir(".") == (tmp_path / "src").resolve()


def test_served_path_accepts_the_directory_and_below_and_refuses_the_rest(tmp_path: Path) -> None:
    projects = (tmp_path / "projects").resolve()
    (projects / "app" / "sub").mkdir(parents=True)
    assert served_path(projects, str(projects), "h") == projects
    assert served_path(projects, str(projects / "app" / "sub"), "h") == projects / "app" / "sub"
    # Not yet existing, but inside: the caller checks for a directory.
    assert served_path(projects, str(projects / "new"), "h") == projects / "new"
    with pytest.raises(NotServed, match="the projects directory this `calu serve` on h serves"):
        served_path(projects, str(tmp_path), "h")
    with pytest.raises(NotServed, match="is not in"):
        served_path(projects, str(projects / ".." / "elsewhere"), "h")
    with pytest.raises(NotServed, match="is not in"):
        served_path(projects, str(tmp_path / "projects-other"), "h")
    (projects / "app" / "escape").symlink_to("/etc")
    with pytest.raises(NotServed, match="is not in"):
        served_path(projects, str(projects / "app" / "escape"), "h")
    with pytest.raises(NotServed, match=r"hidden directory \.venv"):
        served_path(projects, str(projects / "app" / ".venv" / "x"), "h")
    with pytest.raises(NotServed, match="no absolute path"):
        served_path(projects, "app", "h")


def test_the_relay_port_is_stable_in_range_and_skips_reserved_ports() -> None:
    one = Path("/home/ada/src")
    assert relay_port(one) == relay_port(Path("/home/ada/src"))
    ports = relay_ports(one)
    assert ports[0] == relay_port(one)
    assert (
        len(ports)
        == len(set(ports))
        == RELAY_PORT_LAST - RELAY_PORT_FIRST + 1 - len(RESERVED_PORTS)
    )
    assert all(RELAY_PORT_FIRST <= port <= RELAY_PORT_LAST for port in ports)
    assert not RESERVED_PORTS & set(ports)
    # The sequence after the first port is the range in order, wrapping around.
    following = [port for port in ports if port > ports[0]]
    assert following == sorted(following)
    # Different directories mostly get different ports.
    many = {relay_port(Path(f"/home/ada/src/p{index}")) for index in range(40)}
    assert len(many) > 25


def test_the_relay_port_does_not_depend_on_the_process(tmp_path: Path) -> None:
    """A fixed value: the port is a pure function of the path, so a restart keeps it."""
    # sha256(b"/home/ada/src") as a number mod 189, plus 8811, moved past a reserved port.
    import hashlib

    digest = int.from_bytes(hashlib.sha256(os.fsencode("/home/ada/src")).digest(), "big")
    expected = RELAY_PORT_FIRST + digest % 189
    while expected in RESERVED_PORTS:
        expected = expected + 1 if expected < RELAY_PORT_LAST else RELAY_PORT_FIRST
    assert relay_port(Path("/home/ada/src")) == expected
    assert projects_dir.relay_ports(tmp_path)[0] == relay_port(tmp_path)


def test_calu_serve_refuses_the_directory_before_it_asks_for_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from calu import cli

    parser = cli.build_parser()

    def asked(_store: object) -> bytearray:
        raise AssertionError("no password may be asked for a refused directory")

    monkeypatch.setattr(cli, "_session_login", asked)
    with pytest.raises(cli.CliError, match="it is inside /etc, a directory of the operating"):
        cli.cmd_serve(parser.parse_args(["serve", "--projects", "/etc"]), None)  # type: ignore[arg-type]
    (tmp_path / ".work").mkdir()
    monkeypatch.chdir(tmp_path / ".work")
    with pytest.raises(cli.CliError, match=r"hidden or inside a hidden directory \(\.work\)"):
        cli.cmd_serve(parser.parse_args(["serve"]), None)  # type: ignore[arg-type]
    assert parser.parse_args(["serve", "--projects", "/srv/p"]).projects == "/srv/p"
