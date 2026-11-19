"""calu as real root: the launcher, the file identity, the sockets, the place file (D206-D228)."""

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from calu import asuser, cli, launch, localsocket, logfile
from calu.credentials import CredentialStore
from calu.db import place
from calu.db.target import PasswordAuth, Target
from tests.root.box import AS_ROOT, OTHER_UID, THIRD_UID, WHY_NOT

pytestmark = [pytest.mark.root, pytest.mark.skipif(not AS_ROOT, reason=WHY_NOT)]


@pytest.fixture
def shared_dir():
    """A directory every user can enter (pytest's own temporary directory is root's alone)."""
    with tempfile.TemporaryDirectory(prefix="calu-root-test-") as name:
        path = Path(name)
        path.chmod(0o755)
        yield path


def as_user(uid: int, code: str) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet as another uid, with no supplementary groups.

    Args:
        uid: The uid, also used as the gid.
        code: The snippet.

    Returns:
        The finished process, with its output as text.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        user=uid,
        group=uid,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
    )


def test_the_launcher_starts_the_program_as_the_user_with_an_environment_built_for_it(
    shared_dir: Path,
) -> None:
    """D207: groups, gid and uid of the record; no variable of root but the kept ones."""
    recorder = shared_dir / "claude"
    recorder.write_text(
        "#!/bin/sh\n"
        'echo "uid=$(id -u) gid=$(id -g) groups=$(id -G) home=$HOME user=${USER-none} $*"\n'
        "env | cut -d= -f1 | sort | tr '\\n' ' '\n"
    )
    recorder.chmod(0o755)
    spec = launch.RunAs(uid=OTHER_UID, gid=OTHER_UID, home="/var/empty/home", groups=(4711,))
    environment = {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "ANTHROPIC_MODEL": "kept",
        "ANTHROPIC_API_KEY": "",  # hidden by calu: emptied, so left out here
        "MISTRAL_API_KEY": "dropped",
        "SUDO_UID": "0",
        "XDG_CONFIG_HOME": "/etc",
        "CLAUDE_CONFIG_DIR": "/root/.claude",
        launch.LAUNCH_ENV: spec.spec(str(recorder)).dump(),
    }
    done = subprocess.run(
        [str(launch.launcher_path()), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    first, names = done.stdout.splitlines()[:2]
    assert first == (
        f"uid={OTHER_UID} gid={OTHER_UID} groups={OTHER_UID} 4711 "
        "home=/var/empty/home user=none --version"
    )
    assert set(names.split()) - {"PWD", "SHLVL", "_", "OLDPWD"} == {
        "ANTHROPIC_MODEL",
        "HOME",
        "LANG",
        "PATH",
    }


def test_the_launcher_refuses_to_change_users_without_root(shared_dir: Path) -> None:
    """Run by hand as an ordinary user it gains nothing: only its own uid is accepted."""
    spec = launch.RunAs(uid=0, gid=0, home="/root").spec("/bin/true").dump()
    done = subprocess.run(
        [str(launch.launcher_path())],
        env={"PATH": os.environ["PATH"], launch.LAUNCH_ENV: spec},
        user=OTHER_UID,
        group=OTHER_UID,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == launch.REFUSED
    assert "only root can start Claude Code as uid 0" in done.stderr


def test_after_enter_calu_touches_files_as_the_user_and_stays_root(shared_dir: Path) -> None:
    """D211: files it creates are the user's, root's files are closed, `as_root` opens them."""
    project = shared_dir / "project"
    project.mkdir()
    os.chown(project, OTHER_UID, OTHER_UID)
    secret = shared_dir / "root-only"
    secret.write_text("root's")
    secret.chmod(0o600)
    state_root = shared_dir / "state-root"
    code = f"""
        import json, os, subprocess, sys
        from pathlib import Path
        from calu import asuser
        run_as = asuser.resolve("{OTHER_UID}:{OTHER_UID}", Path("{state_root}"))
        asuser.prepare(run_as, Path("{state_root}"))
        asuser.enter(run_as, Path("{state_root}"))
        made = Path("{project}") / "made-by-calu"
        made.write_text("x")
        try:
            Path("{secret}").read_text()
            direct = "read"
        except PermissionError:
            direct = "refused"
        child = subprocess.run(["id", "-u"], capture_output=True, text=True, **asuser.child())
        print(json.dumps({{
            "owner": made.stat().st_uid,
            "direct": direct,
            "as_root": asuser.as_root(Path("{secret}").read_text),
            "child": child.stdout.strip(),
            "process": [os.getuid(), os.geteuid()],
            "state": os.environ["XDG_STATE_HOME"],
        }}))
    """
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen == {
        "owner": OTHER_UID,
        "direct": "refused",
        "as_root": "root's",
        "child": str(OTHER_UID),
        "process": [0, 0],
        "state": str(state_root / "state" / str(OTHER_UID)),
    }
    for directory in (
        state_root / "state" / str(OTHER_UID),
        state_root / "claude" / str(OTHER_UID),
    ):
        status = directory.stat()
        assert (status.st_uid, status.st_gid) == (OTHER_UID, OTHER_UID)
        assert stat.S_IMODE(status.st_mode) == 0o700


def test_the_root_socket_is_roots_alone_and_another_user_cannot_connect(
    shared_dir: Path,
) -> None:
    """D215: socket 0600 in a directory 0700; the kernel names root as the peer."""
    path = localsocket.path_of("11111111-2222-3333-4444-555555555555", shared_dir / "sessions")
    # `bind` creates the two directories above the socket; the outer one must be root's.
    (shared_dir / "sessions").parent.chmod(0o755)
    listener = localsocket.bind(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == 0
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert localsocket.problems(path) == [] and localsocket.listed(path.parent) == [path]
        refused = as_user(
            THIRD_UID,
            f"""
            import socket
            client = socket.socket(socket.AF_UNIX)
            try:
                client.connect("{path}")
                print("connected")
            except OSError as exc:
                print(type(exc).__name__)
            """,
        )
        assert refused.stdout.strip() == "PermissionError", refused.stderr
        client = localsocket.connected(path)
        try:
            accepted, _ = listener.accept()
            assert localsocket.peer_uid(accepted) == 0
            accepted.close()
        finally:
            client.close()
        # A socket somebody else could reach is not listed and not connected to.
        path.parent.chmod(0o755)
        assert localsocket.listed(path.parent) == []
        with pytest.raises(localsocket.NotTrusted):
            localsocket.connected(path)
    finally:
        listener.close()


def test_a_socket_directory_of_another_user_is_refused(shared_dir: Path) -> None:
    """A `/run/calu` that somebody else owns could be swapped under the session."""
    outer = shared_dir / "calu"
    outer.mkdir()
    os.chown(outer, OTHER_UID, OTHER_UID)
    with pytest.raises(localsocket.NotTrusted, match=f"belongs to uid {OTHER_UID}"):
        localsocket.bind(localsocket.path_of("abc", outer / "sessions"))
    assert not (outer / "sessions" / "abc.sock").exists()


def test_the_place_file_is_read_by_every_user_and_trusted_only_from_root(
    shared_dir: Path,
) -> None:
    """D228: 0644 in a directory only root writes; a copy somebody else owns is refused."""
    path = shared_dir / "lib" / "database.json"
    owner = Target.parse(b"postgresql://calu_owner:owner-pw@127.0.0.1:5432/calu")
    place.write(owner, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and path.stat().st_uid == 0
    assert "owner-pw" not in path.read_text() and "calu_owner" not in path.read_text()
    reader = f"""
        from pathlib import Path
        from calu.db import place
        target = place.read(Path("{path}"))
        print(target.user, target.conninfo["port"], bool(getattr(target.auth, "password", "")))
    """
    seen = as_user(THIRD_UID, reader)
    assert seen.stdout.split() == ["calu_admin", "5432", "False"], seen.stderr
    place.write(owner, path, admin_password="generated")
    assert as_user(THIRD_UID, reader).stdout.split() == ["calu_admin", "5432", "True"]
    shared = place.read(path)
    assert isinstance(shared.auth, PasswordAuth) and shared.auth.password == "generated"
    # The same content in a file of another user: no client may believe it.
    os.chown(path, OTHER_UID, OTHER_UID)
    with pytest.raises(place.PlaceError, match=f"belongs to uid {OTHER_UID}"):
        place.read(path)


def test_the_log_file_and_the_credential_store_are_roots_alone(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D225, D228: the log 0600 in a 0700 directory; a store written by root 0600 in 0700."""
    monkeypatch.setenv("XDG_STATE_HOME", str(shared_dir / "var"))
    before = list(logging.getLogger().handlers)
    try:
        log = logfile.setup("serve")
    finally:
        for handler in [h for h in logging.getLogger().handlers if h not in before]:
            logging.getLogger().removeHandler(handler)
            handler.close()
        logging.captureWarnings(False)
    assert log is not None and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    store = CredentialStore(shared_dir / "etc" / "calu" / "credentials.json")
    store.save("session", bytearray(b"postgresql://calu_session@db"), None)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert asuser.only_root_writes(store.path)
    listing = as_user(THIRD_UID, f"import os; print(os.listdir('{store.path.parent}'))")
    assert "PermissionError" in listing.stderr
    # A store another user could have written is not one a session starts from unasked.
    store.path.parent.chmod(0o777)
    assert not asuser.only_root_writes(store.path)


def test_root_must_name_the_user_and_root_itself_only_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 26: Claude Code never runs as root by accident."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = CredentialStore(tmp_path / "config" / "calu" / "credentials.json")
    for command in (["run", str(tmp_path), "--batch", "-p", "x"], ["serve"]):
        assert cli.main(command, store_factory=lambda: store) == 1
        assert "say which user Claude Code runs as" in capsys.readouterr().err
    parser = cli.build_parser()
    assert cli._run_as(parser.parse_args(["serve", "--user", "root"])) is None
    assert cli._run_as(parser.parse_args(["serve", "--user", "0"])) is None
    with pytest.raises(cli.CliError, match="is not the user who ran sudo"):
        monkeypatch.setenv("SUDO_UID", str(THIRD_UID))
        cli._run_as(parser.parse_args(["serve", "--user", str(OTHER_UID), "--from-sudo"]))


def test_a_socket_of_the_session_user_is_private_to_that_user(shared_dir: Path) -> None:
    """D227: the `--insecure` socket of a root session is created as the user, 0600 in 0700."""
    root = shared_dir / f"calu-{OTHER_UID}" / "sessions"
    code = f"""
        import socket, stat
        from pathlib import Path
        from calu import asuser, localsocket
        run_as = asuser.resolve("{OTHER_UID}:{OTHER_UID}", Path("{shared_dir}/sr"))
        asuser.prepare(run_as, Path("{shared_dir}/sr"))
        asuser.enter(run_as, Path("{shared_dir}/sr"))
        path = localsocket.path_of("abc", Path("{root}"))
        listener = localsocket.bind(path, owner={OTHER_UID})
        print(path.stat().st_uid, oct(stat.S_IMODE(path.stat().st_mode)),
              oct(stat.S_IMODE(path.parent.stat().st_mode)), path.parent.parent.stat().st_uid)
    """
    shared_dir.chmod(0o1777)  # as /tmp: the user creates its own directory in it
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.stdout.split() == [str(OTHER_UID), "0o600", "0o700", str(OTHER_UID)], done.stderr


def test_prepare_takes_over_existing_directories_and_keeps_their_content(
    shared_dir: Path,
) -> None:
    """`asuser.prepare` on directories a former run or somebody else left behind.

    A state home of root's (0755) and a home of a third user (0777) both become the user's,
    0700, and keep what is in them; the files inside keep their owners. A named user's own
    home is not changed at all. Side effects: only below `shared_dir`.
    """
    state_root = shared_dir / "lib"
    bare = asuser.resolve(f"{OTHER_UID}:{OTHER_UID}", state_root)
    state, home = asuser.state_home(bare, state_root), Path(bare.home)
    for directory, owner, mode in ((state, 0, 0o755), (home, THIRD_UID, 0o777)):
        directory.mkdir(parents=True)
        (directory / "kept.txt").write_text("earlier run\n")
        os.chown(directory, owner, owner)
        directory.chmod(mode)
    asuser.prepare(bare, state_root)
    for directory in (state, home):
        status = directory.stat()
        assert (status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode)) == (
            OTHER_UID,
            OTHER_UID,
            0o700,
        )
        assert (directory / "kept.txt").read_text() == "earlier run\n"
        assert (directory / "kept.txt").stat().st_uid == 0
    # Done again on what it left: nothing changes, nothing is refused.
    asuser.prepare(bare, state_root)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700 and state.stat().st_uid == OTHER_UID
    # A named user keeps its home exactly as it is; only calu's state home is created.
    own_home = shared_dir / "home-of-a-name"
    own_home.mkdir()
    own_home.chmod(0o751)
    named = launch.RunAs(
        uid=OTHER_UID, gid=OTHER_UID, home=str(own_home), user="somebody", groups=()
    )
    asuser.prepare(named, state_root)
    assert own_home.stat().st_uid == 0 and stat.S_IMODE(own_home.stat().st_mode) == 0o751


def test_prepare_never_follows_a_link_where_a_directory_of_the_user_belongs(
    shared_dir: Path,
) -> None:
    """A symbolic link in place of the state home or a bare uid's home is refused.

    Following it would hand the link's target to the user (owner and mode 0700): with a link
    to a directory of root's, the user would own that directory afterwards.
    """
    state_root = shared_dir / "lib"
    bare = asuser.resolve(f"{OTHER_UID}:{OTHER_UID}", state_root)
    target = shared_dir / "roots-own"
    target.mkdir()
    target.chmod(0o755)
    for spot in (asuser.state_home(bare, state_root), Path(bare.home)):
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.symlink_to(target)
        with pytest.raises(asuser.UserError, match="is not a directory"):
            asuser.prepare(bare, state_root)
        status = target.stat()
        assert (status.st_uid, stat.S_IMODE(status.st_mode)) == (0, 0o755)
        spot.unlink()
        spot.mkdir()  # the next place is reached only once this one is a real directory


@pytest.mark.parametrize(
    ("uid", "gid", "groups"),
    [
        # The C library reads -1 (and its unsigned twin) as "leave it unchanged".
        (-1, -1, ()),
        (2**32 - 1, 2**32 - 1, ()),
        (OTHER_UID, -1, ()),
        (OTHER_UID, 2**32 - 1, ()),
        (OTHER_UID, OTHER_UID, (-1,)),
    ],
)
def test_the_launcher_as_root_refuses_a_record_whose_identity_cannot_be_taken(
    shared_dir: Path, uid: int, gid: int, groups: tuple[int, ...]
) -> None:
    """`launch.become` as real root: an identity the kernel does not give is never run.

    Each record names an id the system calls read as "unchanged", so a launcher that only
    trusted the calls would start Claude Code as root, or with root's group. The launcher must
    refuse (exit status `REFUSED`, one line on stderr) and start nothing.
    """
    started = shared_dir / "started"
    program = shared_dir / "claude"
    program.write_text(f"#!/bin/sh\nid >{started}\n")
    program.chmod(0o755)
    spec = launch.LaunchSpec(
        uid=uid, gid=gid, groups=groups, home="/var/empty", claude=str(program)
    ).dump()
    done = subprocess.run(
        [str(launch.launcher_path())],
        env={"PATH": os.environ["PATH"], launch.LAUNCH_ENV: spec},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == launch.REFUSED, done.stdout + done.stderr
    assert done.stderr.startswith(f"{launch.SCRIPT}: "), done.stderr
    assert len(done.stderr.splitlines()) == 1
    assert not started.exists(), started.read_text()
