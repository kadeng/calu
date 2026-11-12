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
    assert with_keys[0].detail.startswith("anthropic, mistral ")
    # Present is not accepted (D131): the line says how to find out.
    assert "calu doctor --keys" in with_keys[0].detail


def test_the_credential_store_is_reported_without_decrypting_anything(
    store: CredentialStore,
) -> None:
    empty = doctor.credential_findings(store)
    assert [f.ok for f in empty] == [False, False]
    assert any(f.essential for f in empty)  # `calu run` needs the session credential

    store.save("session", "postgresql://localhost/calu", bytearray(b"password"))
    after = doctor.credential_findings(store)
    assert after[1].ok is True and after[1].detail == "stored"


def test_a_credential_without_a_password_is_reported(store: CredentialStore) -> None:
    """D104: the choice is legitimate, but it is invisible in the store, so `doctor` says it.

    It is a note for the narrow `operator` credential and a problem for `admin` or `session`,
    which can change the schema or start sessions.
    """
    store.save("operator", "postgresql://op@localhost/calu", None)
    (said,) = [f for f in doctor.credential_findings(store) if f.name == "credential protection"]
    assert said.ok is True, "the operator credential is what the switch is for"
    assert "operator stored without a password" in said.detail
    assert said.advice == ""

    store.save("admin", "postgresql://a@localhost/calu", None)
    (worse,) = [f for f in doctor.credential_findings(store) if f.name == "credential protection"]
    assert worse.ok is False
    assert "admin" in worse.advice and "with a password" in worse.advice

    # With a password on everything the report says nothing about protection at all.
    store.save("admin", "postgresql://a@localhost/calu", "pw")
    store.save("operator", "postgresql://op@localhost/calu", "pw")
    names = [f.name for f in doctor.credential_findings(store)]
    assert "credential protection" not in names


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


def test_the_web_extra_is_reported_but_never_essential() -> None:
    """An installation without the `web` extra has every other command (PLAN.md D41)."""
    findings = doctor.extra_findings()
    assert [f.name for f in findings] == ["web interface"]
    assert findings[0].essential is False
    # This environment has the extra or it does not; both answers must be a sentence that names
    # the fix when something is missing.
    if findings[0].ok:
        assert "installed" in findings[0].detail
    else:
        assert "calu[web]" in findings[0].advice
    assert doctor.essential_missing(findings) is False


def test_the_sandbox_posture_is_only_reported_when_it_says_something(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI's posture is about the project's settings, so silence must stay silent."""
    # Nothing to report: no finding at all, rather than a claim calu cannot stand behind.
    monkeypatch.setattr(
        doctor,
        "sandbox_posture",
        lambda _workspace: {"enabled": False, "unavailableReason": None, "policyLocked": False},
    )
    assert doctor.posture_findings(tmp_path) == []

    # The CLI could not answer at all (no CLI, a timeout, not JSON).
    monkeypatch.setattr(doctor, "sandbox_posture", lambda _workspace: None)
    assert doctor.posture_findings(tmp_path) == []

    # A reason against building one is worth saying, with only its first clause.
    monkeypatch.setattr(
        doctor,
        "sandbox_posture",
        lambda _workspace: {
            "unavailableReason": "socat not installed · install missing tools",
            "policyLocked": False,
        },
    )
    found = doctor.posture_findings(tmp_path)
    assert [f.name for f in found] == ["sandbox here"]
    assert found[0].detail == "socat not installed"
    assert found[0].ok is False

    # A locked managed policy is the one thing nothing else sees.
    monkeypatch.setattr(
        doctor,
        "sandbox_posture",
        lambda _workspace: {"unavailableReason": None, "policyLocked": True},
    )
    found = doctor.posture_findings(tmp_path)
    assert [f.name for f in found] == ["sandbox policy"]
    assert "may not win" in found[0].advice
    # Never essential: a session still starts, it is the isolation that is in doubt.
    assert doctor.essential_missing(found) is False


def test_a_key_with_a_stray_carriage_return_is_named() -> None:
    """D70: the provider answers such a key with a bare "Connection error."."""
    assert doctor.key_value_findings({"FIREWORKS_API_KEY": "fw_abc"}) == []
    assert doctor.key_value_findings({}) == []

    (broken,) = doctor.key_value_findings({"FIREWORKS_API_KEY": "fw_abc\r"})
    assert broken.ok is False
    assert broken.essential is False, "the session runs; its checks are what fail"
    assert "carriage return" in broken.detail
    assert "Connection error." in broken.advice
    assert "FIREWORKS_API_KEY" in broken.name

    (spaced,) = doctor.key_value_findings({"MISTRAL_API_KEY": " ms_x"})
    assert "whitespace" in spaced.detail

    # It is part of the local report, which is what `calu doctor` prints.
    report = doctor.report(doctor.key_value_findings({"MISTRAL_API_KEY": "ms_x\r\n"}))
    assert "carriage return" in report


def test_a_key_that_is_set_but_refused_is_found_before_it_escalates_every_step() -> None:
    """D131: presence is not validity -- an expired Mistral key passed the old check."""
    import httpx

    seen: list[tuple[str, dict[str, str]]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, dict(request.headers)))
        if request.url.host == "api.eu.mistral.ai":
            return httpx.Response(401, json={"detail": "Your API key expired on 2026-11-08."})
        if request.url.host == "api.fireworks.ai":
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(provider))
    environ = {
        "ANTHROPIC_API_KEY": "sk-ant-good",
        "FIREWORKS_API_KEY": "fw-unreachable",
        "MISTRAL_API_KEY": "m-expired",
    }
    found = {f.name: f for f in doctor.key_validity_findings(environ, client)}

    assert found["anthropic key"].ok and "accepted" in found["anthropic key"].detail
    mistral = found["mistral key"]
    assert not mistral.ok
    assert "expired on 2026-11-08" in mistral.detail, "the provider's own reason is shown"
    assert "calu config models auto" in mistral.advice
    # Unreachable is not called a bad key: the network decides that one.
    assert found["fireworks key"].ok and "not checked" in found["fireworks key"].detail
    assert "openai key" not in found, "a provider without a key is not asked at all"

    # Each key went to its own provider and nowhere else.
    for host, headers in seen:
        carried = headers.get("x-api-key", "") + headers.get("authorization", "")
        if host == "api.anthropic.com":
            assert "sk-ant-good" in carried and "m-expired" not in carried
        if host == "api.eu.mistral.ai":
            assert "m-expired" in carried and "sk-ant-good" not in carried


def test_aws_credentials_are_shown_to_sts_when_boto3_finds_any() -> None:
    """D132: the Bedrock counterpart of the provider key check."""

    class Client:
        def __init__(self, refuse: bool) -> None:
            self.refuse = refuse

        def get_caller_identity(self) -> dict[str, str]:
            if self.refuse:
                raise RuntimeError(
                    "ExpiredToken: The security token included in the request is expired"
                )
            return {"Account": "123456789012"}

    class Session:
        def __init__(self, credentials: bool, refuse: bool = False) -> None:
            self.credentials, self.refuse = credentials, refuse

        def get_credentials(self) -> object | None:
            return object() if self.credentials else None

        def client(self, _name: str) -> Client:
            return Client(self.refuse)

    (ok,) = doctor.aws_findings(lambda: Session(True))
    assert ok.ok and "...9012" in ok.detail and "123456789012" not in ok.detail

    (refused,) = doctor.aws_findings(lambda: Session(True, refuse=True))
    assert not refused.ok and "ExpiredToken" in refused.detail
    assert "aws sso login" in refused.advice

    assert doctor.aws_findings(lambda: Session(False)) == [], "no AWS, no finding"


def test_a_password_free_credential_where_no_sandbox_can_run_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D164: doctor's view of D162 -- it cannot read the setting, only whether bwrap and socat
    are there."""
    from calu import doctor
    from calu.credentials import CredentialStore, KdfParams

    store = CredentialStore(
        tmp_path / "c.json", KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    )
    store.save("operator", "postgresql://op@localhost/calu", None)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "socat" else "/x")
    (finding,) = doctor.self_approval_findings(store)
    assert not finding.ok and "socat missing" in finding.detail
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    assert doctor.self_approval_findings(store) == []


def test_doctor_says_whether_auto_modes_classifier_would_be_active(tmp_path: Path) -> None:
    import json

    home, workspace = tmp_path / "home", tmp_path / "ws"
    (home / ".claude").mkdir(parents=True)
    (workspace / ".claude").mkdir(parents=True)
    (finding,) = doctor.permission_mode_findings(home=home)
    assert finding.name == "auto mode" and not finding.ok
    assert "NOT active" in finding.detail and "claude.permission_mode auto" in finding.advice
    assert "NOT active" in finding.line()

    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}})
    )
    (finding,) = doctor.permission_mode_findings(home=home)
    assert finding.ok and "expected to be active" in finding.line()
    # The project's file has precedence over the user's.
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "plan"}})
    )
    (finding,) = doctor.permission_mode_findings(workspace, home)
    assert not finding.ok and '"plan"' in finding.detail


def test_the_local_findings_hold_the_auto_mode_line(store: CredentialStore) -> None:
    assert "auto mode" in [finding.name for finding in doctor.local_findings(store)]


def test_doctor_names_sandbox_entries_a_session_would_not_start_with(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    assert doctor.settings_file_findings(workspace, home) == []
    (workspace / ".claude" / "settings.json").write_text(
        '{"sandbox": {"excludedCommands": ["docker"]}}'
    )
    (finding,) = doctor.settings_file_findings(workspace, home)
    assert not finding.ok and "excludedCommands: docker" in finding.detail
    assert "does not start" in finding.advice
