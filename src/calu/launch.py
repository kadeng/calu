"""`calu-claude`: starts Claude Code as another user with an environment built for it (D207).

calu may run as root while Claude Code runs as an unprivileged user (PLAN.md D206). The Agent
SDK starts one executable and appends its own arguments; its own `user` option changes the uid
only and passes calu's whole environment on (`spikes/RESULTS.md`). calu therefore names this
launcher as the executable:

    calu (root) ── Agent SDK ──► calu-claude --output-format stream-json ...
                                   │ reads the record in $CALU_LAUNCH
                                   │ setgroups, setgid, setuid; checks all three
                                   │ environment := kept variables + HOME, USER, LOGNAME
                                   └► execve(claude, the same arguments)

The launcher is a console script of calu's installation, next to `calu`; calu writes no file
for it. The record holds no secret. Started without root rights the launcher accepts only its
own uid and gid, so running it by hand gains nothing.
"""

import fnmatch
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError

LAUNCH_ENV = "CALU_LAUNCH"
"""Environment variable that holds the launch record (`LaunchSpec`, JSON)."""

SCRIPT = "calu-claude"
"""Name of the launcher's console script (`pyproject.toml`, `[project.scripts]`)."""

DEFAULT_KEEP = (
    "PATH",
    "LANG",
    "LC_*",
    "TZ",
    "TERM",
    "COLORTERM",
    "PWD",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "ANTHROPIC_*",
    "CLAUDE_*",
    "CALU_INSIDE_SESSION",
    "TRACEPARENT",
    "TRACESTATE",
)
"""Environment variables Claude Code keeps, as shell patterns: what a program needs to run
(path, locale, proxy, certificates), Claude Code's own settings and login variables, and what
the Agent SDK and calu set for it. Everything else of calu's environment is dropped, among it
database, AWS and `SUDO_*` variables and the checker keys."""

REFUSED = 126
"""Exit status of a launcher that did not start Claude Code."""


class LaunchError(Exception):
    """The launcher cannot start Claude Code as asked; the message says why."""


@dataclass(frozen=True)
class RunAs:
    """The user Claude Code runs as, when that is not the user calu runs as (D206).

    `groups` are the supplementary groups, `home` the directory Claude Code keeps its state
    in, `user` the login name or None for a uid without one.
    """

    uid: int
    gid: int
    home: str
    groups: tuple[int, ...] = ()
    user: str | None = None

    def spec(self, claude: str) -> LaunchSpec:
        """Return the launch record for this user.

        Args:
            claude: The path of the real Claude Code executable.

        Returns:
            The record, with the default set of kept environment variables.
        """
        return LaunchSpec(
            uid=self.uid,
            gid=self.gid,
            groups=self.groups,
            home=self.home,
            user=self.user,
            claude=claude,
        )


class LaunchSpec(BaseModel):
    """Who Claude Code runs as, and with which environment.

    `groups` are the supplementary groups; an empty tuple leaves the process with none.
    `user` is the login name where the uid has one (it becomes `USER` and `LOGNAME`), None for
    a uid without a name. `home` becomes `HOME`: Claude Code keeps its login and its
    conversations below it. `claude` is the path of the real Claude Code executable. `keep`
    holds shell patterns of the variable names to take over from the launcher's environment.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uid: int
    gid: int
    groups: tuple[int, ...] = ()
    home: str
    user: str | None = None
    claude: str
    keep: tuple[str, ...] = DEFAULT_KEEP

    def dump(self) -> str:
        """Return the record as the text `CALU_LAUNCH` carries.

        Returns:
            The JSON document `parse` reads.
        """
        return self.model_dump_json()

    @classmethod
    def parse(cls, text: str) -> LaunchSpec:
        """Read a record.

        Args:
            text: What `dump` returned.

        Returns:
            The record.

        Raises:
            LaunchError: If the text is empty or no valid record.
        """
        if not text:
            raise LaunchError(
                f"${LAUNCH_ENV} is not set: `{SCRIPT}` is started by calu, not by hand"
            )
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise LaunchError(f"${LAUNCH_ENV} holds no valid launch record: {exc}") from exc


def environment(spec: LaunchSpec, given: Mapping[str, str]) -> dict[str, str]:
    """Build Claude Code's environment from an empty one.

        result = {name: value in `given` | name matches one of spec.keep, name != CALU_LAUNCH}
                 + HOME = spec.home
                 + USER, LOGNAME = spec.user        (only if the uid has a name)

    Args:
        spec: The launch record.
        given: The environment the launcher was started with.

    Returns:
        The environment for the Claude Code process. `USER`, `LOGNAME` and
        `CLAUDE_CONFIG_DIR` of `given` never survive: they would name the user calu runs as,
        or that user's directory.
    """
    result = {
        name: value
        for name, value in given.items()
        if name != LAUNCH_ENV and any(fnmatch.fnmatchcase(name, pattern) for pattern in spec.keep)
    }
    result["HOME"] = spec.home
    # Claude Code's configuration directory is `<home>/.claude` for this user; a value from
    # calu's environment would name the directory of the user calu runs as.
    result.pop("CLAUDE_CONFIG_DIR", None)
    for name in ("USER", "LOGNAME"):
        result.pop(name, None)
        if spec.user is not None:
            result[name] = spec.user
    return result


def become(spec: LaunchSpec) -> None:
    """Change this process to the record's user: groups, then gid, then uid.

    The order matters: after the uid is given up, the process may change neither its groups
    nor its gid. Side effect: the change cannot be undone by this process.

    Args:
        spec: The launch record.

    Raises:
        LaunchError: If the process is not root and the record names another uid or gid than
            its own, or if the identity after the change is not exactly the record's.
    """
    if os.geteuid() != 0:
        if (spec.uid, spec.gid) != (os.getuid(), os.getgid()):
            raise LaunchError(
                f"only root can start Claude Code as uid {spec.uid}, gid {spec.gid}; this "
                f"process is uid {os.getuid()}, gid {os.getgid()}"
            )
        return
    try:
        os.setgroups(list(spec.groups))
        os.setresgid(spec.gid, spec.gid, spec.gid)
        os.setresuid(spec.uid, spec.uid, spec.uid)
    except OSError as exc:
        raise LaunchError(f"changing to uid {spec.uid}, gid {spec.gid} failed: {exc}") from exc
    now = (os.getresuid(), os.getresgid(), sorted(os.getgroups()))
    wanted = ((spec.uid,) * 3, (spec.gid,) * 3, sorted(set(spec.groups)))
    if now != wanted:
        raise LaunchError(f"the process identity is {now} after the change, not {wanted}")


def launcher_path() -> Path:
    """Return the launcher's console script of this installation.

    Returns:
        `<directory of the running Python>/calu-claude`: where the installer puts console
        scripts, for a virtual environment and for `uv tool install` alike.

    Raises:
        LaunchError: If the script does not exist there (an installation without console
            scripts, such as a single-file binary).
    """
    path = Path(sys.executable).parent / SCRIPT
    if not path.is_file():
        raise LaunchError(
            f"{path} does not exist: this installation of calu has no `{SCRIPT}` launcher, "
            "which starting Claude Code as another user needs"
        )
    return path


def claude_path(configured: str | None) -> str:
    """Return the Claude Code executable calu starts.

    Args:
        configured: `claude.cli_path` of the configuration, or None.

    Returns:
        `configured` if set; else the executable bundled with the Agent SDK; else the `claude`
        found on `PATH`.

    Raises:
        LaunchError: If none of the three exists.
    """
    if configured:
        return configured
    # Imported here: the launcher itself never needs the SDK and should start fast.
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("claude")
    if found is None:
        raise LaunchError("no Claude Code executable was found (set claude.cli_path)")
    return found


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> NoReturn:
    """Become the record's user and replace this process with Claude Code.

    Side effects: changes the process's groups, gid and uid; on success the process image is
    replaced and this function does not return. On a refusal it prints one line to stderr.

    Args:
        argv: The arguments for Claude Code; None uses the command line.
        environ: The launcher's environment; None uses the process's.

    Raises:
        SystemExit: With status `REFUSED` when there is no valid record, the user change is
            not possible, or Claude Code cannot be executed.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    given = dict(os.environ if environ is None else environ)
    try:
        spec = LaunchSpec.parse(given.get(LAUNCH_ENV, ""))
        env = environment(spec, given)
        become(spec)
        os.execve(spec.claude, [spec.claude, *arguments], env)
    except LaunchError as exc:
        print(f"{SCRIPT}: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"{SCRIPT}: cannot execute Claude Code: {exc}", file=sys.stderr)
    raise SystemExit(REFUSED)
