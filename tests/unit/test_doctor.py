"""`calu doctor`: what it finds, and what it calls essential."""

from pathlib import Path

import pytest

from calu import doctor
from calu.credentials import CredentialStore
from calu.doctor import Finding


def test_a_finding_renders_its_advice_only_when_something_is_wrong() -> None:
    good = Finding("git", True, "/usr/bin/git", "apt install git")
    assert good.line() == "ok      git: /usr/bin/git"

    bad = Finding("socat", False, "not on PATH", "apt install socat")
    assert "note" in bad.line() and "apt install socat" in bad.line()

    essential = Finding("claude", False, "not on PATH", "install it", essential=True)
    assert essential.line().startswith("MISSING")


def test_the_report_names_what_a_session_cannot_run_without() -> None:
    findings = [
        Finding("claude", True, "2.1.319"),
        Finding("socat", False, "not on PATH", "apt install socat"),
        Finding("credential 'session'", False, "not stored", "run calu setup", essential=True),
    ]
    shown = doctor.report(findings)
    assert "a session cannot run without credential 'session'" in shown
    assert doctor.essential_missing(findings) is True

    fine = [Finding("claude", True, "2.1.319")]
    assert "everything in place" in doctor.report(fine)
    assert doctor.essential_missing(fine) is False

    with_notes = [Finding("claude", True, "x"), Finding("socat", False, "no", "install it")]
    assert "1 note(s)" in doctor.report(with_notes)
    assert doctor.essential_missing(with_notes) is False


def test_the_checker_keys_are_reported_by_provider() -> None:
    none_set = doctor.key_findings([])
    assert none_set[0].ok is False
    assert "none of the known providers" in none_set[0].detail
    assert "ANTHROPIC_API_KEY" in none_set[0].advice

    with_keys = doctor.key_findings(["ANTHROPIC_API_KEY", "MISTRAL_API_KEY"])
    assert with_keys[0].ok is True
    assert with_keys[0].detail == "anthropic, mistral"


def test_the_credential_store_is_reported_without_decrypting_anything(
    store: CredentialStore,
) -> None:
    empty = doctor.credential_findings(store)
    assert [f.ok for f in empty] == [False, False]
    assert any(f.essential for f in empty)  # `calu run` needs the session credential

    store.save("session", "postgresql://localhost/calu", bytearray(b"password"))
    after = doctor.credential_findings(store)
    assert after[1].ok is True and after[1].detail == "stored"


def test_a_workspace_is_checked_for_being_a_git_repository(tmp_path: Path) -> None:
    plain = doctor.workspace_findings(tmp_path)
    assert plain[0].ok is True  # the directory exists
    assert plain[1].ok is False and "no .git" in plain[1].detail

    (tmp_path / ".git").mkdir()
    assert doctor.workspace_findings(tmp_path)[1].ok is True

    missing = doctor.workspace_findings(tmp_path / "nope")
    assert missing[0].ok is False and missing[0].essential


def test_the_local_findings_cover_the_four_groups(store: CredentialStore) -> None:
    names = [finding.name for finding in doctor.local_findings(store)]
    assert names[:4] == ["claude", "bwrap", "socat", "git"]
    assert "credential 'admin'" in names and "checker keys" in names
    assert "snapshot directory" in names


@pytest.mark.anyio
async def test_a_database_that_does_not_answer_is_one_finding() -> None:
    found = await doctor.database_findings("postgresql://127.0.0.1:1/nothing")
    assert len(found) == 1
    assert found[0].name == "database" and found[0].ok is False and found[0].essential
    assert "calu db up" in found[0].advice
