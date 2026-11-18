"""The log of `calu run` and `calu serve`: tracebacks in a file, one line on stderr (D225)."""

import io
import logging
import stat
from pathlib import Path

import pytest

from calu import cli, logfile


@pytest.fixture
def clean_root():
    """Remove the handlers `logfile.setup` adds, after the test."""
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, logfile.CALU_HANDLER, False)]:
        root.removeHandler(handler)
        handler.close()
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
