"""The log of `calu run` and `calu serve`: tracebacks in a file, one line on stderr (D225)."""

import io
import logging
import stat
from pathlib import Path

import pytest

from calu import cli, logfile
from calu.credentials import WrongPassword


@pytest.fixture
def clean_root():
    """Remove the handlers `logfile.setup` adds, after the test."""
    yield
    for logger in (logging.getLogger(), logging.getLogger(logfile.REFUSALS_LOGGER)):
        for handler in [h for h in logger.handlers if getattr(h, logfile.CALU_HANDLER, False)]:
            logger.removeHandler(handler)
            handler.close()
    logging.getLogger(logfile.REFUSALS_LOGGER).propagate = True
    logging.captureWarnings(False)


def test_a_traceback_goes_to_the_file_and_one_line_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_root: None
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    err = io.StringIO()
    path = tmp_path / "calu" / "log" / "run.log"
    assert logfile.setup("run", err) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    log = logging.getLogger("calu.test")
    log.info("only in the file")
    try:
        raise ExceptionGroup("unhandled errors", [ValueError("bad\nvalue")])
    except ExceptionGroup:
        log.exception("the step\nfailed")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert err.getvalue().splitlines() == [
        f"calu: the step failed (ValueError: bad value; details in {path})"
    ]
    written = path.read_text()
    assert "only in the file" in written
    assert "Traceback (most recent call last)" in written and "ValueError: bad" in written


def test_a_second_setup_replaces_its_own_handlers_and_keeps_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_root: None
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    other = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(other)
    try:
        logfile.setup("serve", io.StringIO())
        logfile.setup("serve", io.StringIO())
        ours = [h for h in root.handlers if getattr(h, logfile.CALU_HANDLER, False)]
        assert len(ours) == 2
        refusals = logging.getLogger(logfile.REFUSALS_LOGGER).handlers
        assert len(refusals) == 1 and logfile.refusals_file() == logfile.refusals_path("serve")
        assert other in root.handlers
    finally:
        root.removeHandler(other)


def test_without_a_log_file_errors_still_reach_stderr_as_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_root: None
) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    err = io.StringIO()
    assert logfile.setup("run", err) is None
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("calu.test").exception("it broke")
    lines = err.getvalue().splitlines()
    assert lines[0].startswith("calu: no log file (")
    assert lines[1] == "calu: it broke (RuntimeError: boom)"


def test_an_unexpected_error_of_calu_run_is_one_line_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clean_root: None,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def broken(_args, _store) -> None:
        """Stands in for `cmd_run` and fails the way a bug would."""
        raise RuntimeError("first line\nsecond line")

    monkeypatch.setattr(cli, "cmd_run", broken)
    assert cli.main(["run", str(tmp_path)], store_factory=lambda: None) == 1  # type: ignore[arg-type]
    for handler in logging.getLogger().handlers:
        handler.flush()
    path = tmp_path / "calu" / "log" / "run.log"
    err = capsys.readouterr().err.splitlines()
    assert err == [f"calu: RuntimeError: first line second line; details in {path}"]
    assert "Traceback (most recent call last)" in path.read_text()


def test_an_error_calu_reports_itself_is_not_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clean_root: None,
) -> None:
    """The refusal of `--sudo` ends with the install command: the root box cut it at 300."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    hint = "install calu for root: " + "x" * 400 + " END"

    def refuses(_args, _store) -> None:
        """Stands in for `cmd_run` and refuses with a long message."""
        raise cli.CliError(hint)

    monkeypatch.setattr(cli, "cmd_run", refuses)
    assert cli.main(["run", str(tmp_path)], store_factory=lambda: None) == 1  # type: ignore[arg-type]
    assert capsys.readouterr().err.splitlines() == [f"calu: {hint}"]


def test_a_wrong_session_password_is_said_in_words(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clean_root: None,
) -> None:
    """It used to print `calu: session`: the exception carries the credential's name only."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def wrong(_args, _store) -> None:
        """Stands in for `cmd_serve` at a wrong session password."""
        raise WrongPassword("session")

    monkeypatch.setattr(cli, "cmd_serve", wrong)
    assert cli.main(["serve"], store_factory=lambda: None) == 1  # type: ignore[arg-type]
    assert capsys.readouterr().err.splitlines() == [
        "calu: wrong password for the credential 'session'"
    ]


def test_a_command_that_elevates_through_sudo_opens_no_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clean_root: None,
) -> None:
    """`calu serve --sudo` as a user: the log is root's, the user's home gets no file."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)

    def refused(_args, _store) -> None:
        """Stands in for `cmd_serve` when the installation check refuses the elevation."""
        raise cli.CliError("--sudo would run this installation of calu as root")

    monkeypatch.setattr(cli, "cmd_serve", refused)
    assert cli.main(["serve", "--sudo"], store_factory=lambda: None) == 1  # type: ignore[arg-type]
    assert not (tmp_path / "calu").exists()
    assert capsys.readouterr().err.startswith("calu: --sudo would run")


def test_serve_passes_secure_on_to_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_root: None
) -> None:
    """D226: `calu serve --secure` makes every client of its sessions show a token."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    seen = []

    async def fake_serve(url, options, user=None) -> int:
        """Stands in for `calu.serve.serve` and records its options."""
        seen.append(options)
        return 0

    monkeypatch.setattr(cli, "_session_login", lambda store: bytearray(b"postgresql://s@db"))
    monkeypatch.setattr(cli, "serve_sessions", fake_serve)
    assert cli.main(["serve", "--secure"], store_factory=lambda: None) == 0  # type: ignore[arg-type]
    assert cli.main(["serve"], store_factory=lambda: None) == 0  # type: ignore[arg-type]
    assert cli.main(["serve", "--insecure"], store_factory=lambda: None) == 0  # type: ignore[arg-type]
    assert [options.secure for options in seen] == [True, False, False]
    assert [options.insecure for options in seen] == [False, False, True]  # D227
    assert cli.main(["serve", "--secure", "--insecure"], store_factory=lambda: None) == 1  # type: ignore[arg-type]
    assert len(seen) == 3, "the two together start no server"
