"""`calu doctor`: what it finds, and what it calls essential."""

import os
import subprocess
from pathlib import Path

import pytest

from calu import cli, doctor
from calu.credentials import CredentialStore
from calu.db import place as db_place
from calu.db.target import Target
from calu.doctor import Finding
from calu.launch import RunAs


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
    assert "calu setup" in found[0].advice


def test_the_web_packages_are_reported_but_never_essential() -> None:
    """An installation whose web packages are missing has every other command."""
    findings = doctor.extra_findings()
    assert [f.name for f in findings] == ["web interface"]
    assert findings[0].essential is False
    # Both answers must be a sentence that names the fix when something is missing.
    if findings[0].ok:
        assert "installed" in findings[0].detail
    else:
        assert "uv tool install --reinstall calu" in findings[0].advice
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
    monkeypatch.setattr(doctor, "namespace_findings", list)
    assert doctor.self_approval_findings(store) == []
    # Both programs installed, and the kernel refuses the namespaces (a plain container):
    # the sandbox cannot run there either, which the root box showed.
    refused = [doctor.Finding("sandbox start", False, "bubblewrap cannot create a sandbox")]
    monkeypatch.setattr(doctor, "namespace_findings", lambda: refused)
    (finding,) = doctor.self_approval_findings(store)
    assert "cannot create a sandbox" in finding.detail
    # A store only root can read is out of reach of Claude Code's user.
    monkeypatch.setattr(doctor.asuser, "only_root_writes", lambda path: True)
    assert doctor.self_approval_findings(store) == []


def test_doctor_states_the_permission_mode_as_information(tmp_path: Path) -> None:
    import json

    home, workspace = tmp_path / "home", tmp_path / "ws"
    (home / ".claude").mkdir(parents=True)
    (workspace / ".claude").mkdir(parents=True)
    (finding,) = doctor.permission_mode_findings(home=home)
    assert finding.name == "permission mode" and finding.ok
    assert '"auto"' in finding.detail and "calu's default" in finding.detail
    # The project's file has precedence over the user's; another mode is the user's choice,
    # stated as information (D241), not as something to fix.
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}})
    )
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "plan"}})
    )
    (finding,) = doctor.permission_mode_findings(workspace, home)
    assert finding.ok and '"plan"' in finding.detail and "classifier is off" in finding.detail
    assert finding.line().startswith("ok")


def test_the_local_findings_hold_the_permission_mode_line(store: CredentialStore) -> None:
    assert "permission mode" in [finding.name for finding in doctor.local_findings(store)]


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


def test_the_claude_cli_runs_as_the_user_calu_works_for(tmp_path: Path, monkeypatch) -> None:
    """Under `--user` the CLI runs in the project as that user, without root's keys."""
    import subprocess

    from calu import asuser
    from calu.launch import RunAs

    seen: list[dict[str, object]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        """Record the options and answer like the CLI.

        Args:
            command: The command line.
            **options: The keyword arguments of `subprocess.run`.

        Returns:
            A finished process that printed a version and one JSON line.
        """
        seen.append(options)
        return subprocess.CompletedProcess(command, 0, '2.1.319\n{"enabled": true}\n', "")

    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.subprocess, "run", run)
    monkeypatch.setenv("MISTRAL_API_KEY", "secret")
    monkeypatch.setattr(asuser, "_active", RunAs(uid=1000, gid=1000, home="/home/ada"))
    assert doctor.claude_version()[0] is True
    assert doctor.sandbox_posture(tmp_path) == {"enabled": True}
    for options in seen:
        assert options["user"] == 1000 and options["group"] == 1000
        env = options["env"]
        assert isinstance(env, dict) and "MISTRAL_API_KEY" not in env
        assert env["HOME"] == "/home/ada"
    # Without a switch nothing changes: no user, no environment of its own.
    monkeypatch.setattr(asuser, "_active", None)
    seen.clear()
    doctor.claude_version()
    assert "user" not in seen[0] and "env" not in seen[0]


def test_doctor_finds_the_claude_a_session_would_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check uses the session's lookup: the executable bundled with the Agent SDK counts,
    also when no `claude` is on PATH (the bridge of an editor has no ~/.local/bin on PATH)."""
    from importlib.machinery import ModuleSpec

    from calu import doctor, launch

    package = tmp_path / "claude_agent_sdk"
    (package / "_bundled").mkdir(parents=True)
    # The SDK is located, not imported (`calu.launch.claude_path`).
    spec = ModuleSpec("claude_agent_sdk", None, origin=str(package / "__init__.py"))
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: spec)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.claude_executable() is None
    answered, detail = doctor.claude_version()
    assert not answered and "not on PATH" in detail and "bundled" in detail

    bundled = package / "_bundled" / "claude"
    bundled.write_text("#!/bin/sh\necho '2.1.327 (Claude Code)'\n", encoding="utf-8")
    bundled.chmod(0o755)
    assert doctor.claude_executable() == str(bundled)
    assert doctor.claude_version() == (True, f"2.1.327 (Claude Code) ({bundled})")


def test_a_store_only_root_reads_is_not_called_the_wrong_place(
    store: CredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `sudo calu setup --no-password` the report called `/etc/calu` a wrong choice."""
    store.save("admin", bytearray(b"postgresql://owner@db"), None)
    store.save("session", bytearray(b"postgresql://session@db"), None)
    risky = doctor.credential_findings(store)[-1]
    assert not risky.ok and "wrong entry" in risky.advice
    monkeypatch.setattr(doctor.asuser, "only_root_writes", lambda path: True)
    stated = doctor.credential_findings(store)[-1]
    assert stated.ok and "only root can read" in stated.detail and stated.advice == ""


def test_the_place_file_is_reported_with_what_it_gives_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D228: a password in the place file is readable by every user of the machine."""
    path = tmp_path / "database.json"
    monkeypatch.setenv(db_place.PATH_ENV, str(path))
    own = os.geteuid()
    assert doctor.place_findings(own) == []
    owner = Target.parse(b"postgresql://calu_owner:pw@127.0.0.1:5433/calu")
    db_place.write(owner, path)
    (plain,) = doctor.place_findings(own)
    assert plain.ok and "holds no secret" in plain.detail and "5433" in plain.detail
    db_place.write(owner, path, admin_password="generated")
    (published,) = doctor.place_findings(own)
    assert not published.ok and published.name == "settings login"
    assert "generated" not in published.detail + published.advice
    assert "every user and every program" in published.detail
    # A file that is not root's is not trusted, and the report says that clients skip it.
    (distrusted,) = doctor.place_findings(own + 1)
    assert not distrusted.ok and "clients do not use it" in distrusted.detail


def test_bubblewrap_that_cannot_create_a_namespace_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a plain container `bwrap` is on PATH and every sandboxed command fails."""
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    answers = [
        subprocess.CompletedProcess([], 1, "", "bwrap: Creating new namespace failed\n"),
        subprocess.CompletedProcess([], 0, "", ""),
    ]
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: answers.pop(0))
    (failed,) = doctor.namespace_findings()
    assert not failed.ok and not failed.essential
    assert "Creating new namespace failed" in failed.detail
    assert doctor.namespace_findings() == []


def test_the_user_side_of_a_root_calu_is_reported_without_creating_anything(
    tmp_path: Path,
) -> None:
    """`calu doctor --user`: the user, its home, the installation and the launcher."""
    bare = RunAs(uid=4242, gid=4243, home=str(tmp_path / "claude" / "4242"))
    user, state, install, _launcher = doctor.user_findings(bare, tmp_path)
    assert user.ok and "uid 4242" in user.detail and "creates at the first run" in user.detail
    assert str(tmp_path / "state" / "4242") in state.detail
    assert not (tmp_path / "state").exists() and not (tmp_path / "claude").exists()
    # The tests' installation belongs to the tests' user: that is what `--sudo` refuses.
    assert not install.ok and "sudo env UV_TOOL_DIR=/usr/local/lib/calu" in install.advice
    named = RunAs(uid=4242, gid=4243, home=str(tmp_path / "nobody-home"), user="nobody-here")
    missing = doctor.user_findings(named, tmp_path)[0]
    assert not missing.ok and missing.essential and "does not exist" in missing.detail


def test_doctor_as_a_user_refuses_another_user(
    store: CredentialStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--user` is root's option, as for `calu run`."""
    assert cli.main(["doctor", "--user", "0"], store_factory=lambda: store) == 1
    assert "--user needs calu to run as root" in capsys.readouterr().err


def test_doctor_reports_the_sandbox_off_recommendation_unless_it_is_met() -> None:
    """Requirement 42, judged as at a session's start; only with `--db`, which reads the
    setting."""
    from calu.preflight import SANDBOX_OFF_ADVICE, LoginProtection

    unknown = LoginProtection()
    assert doctor.sandbox_off_findings(None, unknown) == []
    (on,) = doctor.sandbox_off_findings(True, unknown)
    assert on.ok and on.detail.startswith("on")
    (off,) = doctor.sandbox_off_findings(False, unknown)
    assert not off.ok and not off.essential and off.advice == SANDBOX_OFF_ADVICE
    (met,) = doctor.sandbox_off_findings(False, LoginProtection(True, None, False))
    assert met.ok and met.detail.startswith("off;")
