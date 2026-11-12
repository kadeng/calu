"""Start-up warnings: sandbox tools, checker API keys, write sets; the sandbox settings."""

from pathlib import Path

import pytest

from calu import preflight
from calu.claude.sdk_runner import SdkRunner
from calu.config import (
    ChangesConfig,
    CheckerConfig,
    ClaudeConfig,
    ClockConfig,
    Config,
    ContextConfig,
    HonestyConfig,
    SandboxConfig,
)
from calu.herd import STALE_AFTER_S


def test_sandbox_settings_document_matches_the_spike():
    settings = SandboxConfig().settings()["sandbox"]
    assert settings["enabled"] is True
    assert settings["failIfUnavailable"] is True
    assert settings["allowUnsandboxedCommands"] is False
    # Claude Code must not approve sandboxed Bash itself: auto mode is the second opinion (D5).
    assert settings["autoAllowBashIfSandboxed"] is False
    assert "~/.config/calu" in settings["filesystem"]["denyRead"]
    assert "~/.claude/.credentials.json" in settings["filesystem"]["denyRead"]
    # No domain is allowed, so the sandbox blocks the network, and every list and switch of
    # the network section is written with its value.
    assert settings["network"] == {
        "allowedDomains": [],
        "deniedDomains": [],
        "allowUnixSockets": [],
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
    }
    assert settings["excludedCommands"] == []
    assert settings["filesystem"]["allowRead"] == []
    assert settings["filesystem"]["allowWrite"] == []
    assert settings["enableWeakerNestedSandbox"] is False
    assert settings["enableWeakerNetworkIsolation"] is False


def _dotted(document: dict, prefix: str = "") -> list[str]:
    """List the keys of a nested settings object as dotted paths."""
    keys: list[str] = []
    for key, value in document.items():
        if isinstance(value, dict):
            keys += _dotted(value, f"{prefix}{key}.")
        else:
            keys.append(f"{prefix}{key}")
    return keys


def test_the_settings_document_holds_exactly_the_keys_the_docstrings_name():
    """`SandboxConfig` and `settings()` refer to `PINNED_SANDBOX_KEYS`; this keeps that true."""
    from calu.config import PINNED_SANDBOX_KEYS

    for config in (
        SandboxConfig(),
        SandboxConfig(denied_domains=["x.example"], allow_unix_sockets=["/run/pg.sock"]),
    ):
        assert sorted(_dotted(config.settings()["sandbox"])) == sorted(PINNED_SANDBOX_KEYS)
    # Every key preflight treats as widening is one calu writes itself.
    widening = [name for name, *_ in preflight.SANDBOX_LIST_KEYS + preflight.SANDBOX_SWITCH_KEYS]
    assert set(widening) <= set(PINNED_SANDBOX_KEYS)
    written = SandboxConfig(allow_unix_sockets=["/run/pg.sock"]).settings()["sandbox"]
    assert written["network"]["allowUnixSockets"] == ["/run/pg.sock"]


def test_sandbox_settings_carry_network_and_exclusions():
    config = SandboxConfig(
        allowed_domains=["pypi.org"], excluded_commands=["docker"], weaker_nested_sandbox=True
    )
    settings = config.settings()["sandbox"]
    assert settings["network"]["allowedDomains"] == ["pypi.org"]
    assert settings["excludedCommands"] == ["docker"]
    assert settings["enableWeakerNestedSandbox"] is True


def test_the_runner_passes_the_sandbox_settings_to_claude_code(tmp_path: Path):
    runner = SdkRunner(tmp_path, ClaudeConfig())
    options = runner._options(None, None)  # type: ignore[arg-type]
    assert options.settings is not None and '"failIfUnavailable": true' in options.settings
    assert '"sandbox"' in options.settings


def test_missing_sandbox_tools_are_reported(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    (warning,) = preflight.sandbox_warnings(Config())
    assert "bwrap and socat" in warning and "refuses to start" in warning
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert preflight.sandbox_warnings(Config()) == []


def test_a_disabled_sandbox_is_reported_as_well():
    config = Config(claude=ClaudeConfig(sandbox=SandboxConfig(enabled=False)))
    (warning,) = preflight.sandbox_warnings(config)
    assert "sandbox for Bash commands is off" in warning


def test_missing_api_keys_name_the_roles_and_the_env_file():
    config = Config(
        command_checker=CheckerConfig(model="mistral:mistral-small-2603"),
        edit_checker=CheckerConfig(model="fireworks:accounts/x/models/y"),
        changeset_checker=CheckerConfig(model="fireworks:accounts/x/models/y"),
        subagent_checker=CheckerConfig(model="fireworks:accounts/x/models/y"),
        context=ContextConfig(
            narrate=False,
            narrator_model="fireworks:accounts/x/models/y",
            summarizer_model="mistral:ministral-3b-latest",
        ),
        check_subagent_results=False,
    )
    warnings = preflight.key_warnings(config, environ={})
    assert len(warnings) == 2
    mistral = next(w for w in warnings if "MISTRAL_API_KEY" in w)
    assert "command checker" in mistral and "summarizer" in mistral
    assert ".env" in mistral
    assert any("FIREWORKS_API_KEY" in w for w in warnings)
    assert (
        preflight.key_warnings(config, environ={"MISTRAL_API_KEY": "x", "FIREWORKS_API_KEY": "y"})
        == []
    )


def test_models_without_a_key_variable_are_not_checked():
    config = Config().with_model("bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert preflight.key_warnings(config, environ={}) == []
    assert "narrator" in preflight.configured_models(config)


def test_a_review_without_a_snapshot_is_reported() -> None:
    """D31: the review needs the snapshot that measures what a command wrote."""
    assert preflight.change_warnings(Config()) == []
    on = Config(changes=ChangesConfig(snapshot=True, review=True))
    assert preflight.change_warnings(on) == []
    broken = Config(changes=ChangesConfig(snapshot=False, review=True))
    assert len(preflight.change_warnings(broken)) == 1
    assert "needs the snapshot" in preflight.change_warnings(broken)[0]
    assert preflight.change_warnings(broken)[0] in preflight.warnings(broken, {})


def test_the_three_network_policies_are_expressed_in_the_sandbox_settings() -> None:
    """D39: measured against the real CLI — an empty allow list, a list, or the wildcard."""
    configured = SandboxConfig(allowed_domains=["pypi.org"], denied_domains=["evil.example"])

    nothing = configured.with_policy("none").settings()["sandbox"]
    assert nothing["network"]["allowedDomains"] == []
    assert nothing["network"]["deniedDomains"] == ["evil.example"]  # the deny list is kept

    only_listed = configured.with_policy("proxy").settings()["sandbox"]
    assert only_listed["network"]["allowedDomains"] == ["pypi.org"]

    everything = configured.with_policy("full").settings()["sandbox"]
    assert everything["network"]["allowedDomains"] == ["*"]
    # The deny list still wins over the wildcard, which is "everything except these".
    assert everything["network"]["deniedDomains"] == ["evil.example"]


def test_an_open_network_is_reported_at_every_start() -> None:
    open_network = Config(
        claude=ClaudeConfig(sandbox=SandboxConfig(allowed_domains=["*"], denied_domains=["x.io"]))
    )
    said = preflight.network_warnings(open_network)
    assert len(said) == 1
    assert "every domain" in said[0] and "except x.io" in said[0]
    assert said[0] in preflight.warnings(open_network, {})

    # A narrow policy says nothing: it is neither risky nor surprising.
    narrow = Config(claude=ClaudeConfig(sandbox=SandboxConfig(allowed_domains=["pypi.org"])))
    assert preflight.network_warnings(narrow) == []

    # A closed network is the safe end, but it explains every failing download, so it is said
    # once at the start rather than left to be guessed from a stuck install (D45).
    closed = preflight.network_warnings(Config())
    assert len(closed) == 1
    assert "blocks every outgoing connection" in closed[0]
    assert "calu config add claude.sandbox.allowed_domains" in closed[0]
    disabled = Config(
        claude=ClaudeConfig(sandbox=SandboxConfig(enabled=False, allowed_domains=["*"]))
    )
    assert preflight.network_warnings(disabled) == []


def test_a_missing_key_says_what_it_costs_per_role() -> None:
    """The consequence differs by role, and the warning is the first thing a new user reads.

    A checker that cannot answer escalates its step; a narrator or a summarizer that cannot
    answer only leaves the checkers with less context. Saying "every check fails and escalates"
    for all six roles was wrong for two of them.
    """
    # Checkers, narrator and summarizer all on the same provider: both consequences.
    everywhere = Config().with_model("mistral:mistral-small-2603")
    both = [w for w in preflight.key_warnings(everywhere, {}) if "MISTRAL" in w]
    assert len(both) == 1
    assert "every check of these escalates to you" in both[0]
    assert "the narration and the folded summary stay empty" in both[0]

    # Only the checkers on that provider: nothing about narration.
    elsewhere = everywhere.model_copy(
        update={"context": ContextConfig(narrate=False, summarizer_model="anthropic:x")}
    )
    checkers = [w for w in preflight.key_warnings(elsewhere, {}) if "MISTRAL" in w]
    assert "every check of these escalates" in checkers[0]
    assert "empty" not in checkers[0]

    # One of the two: the verb agrees with it.
    summary_only = Config(
        context=ContextConfig(narrate=False, summarizer_model="mistral:ministral-3b-latest")
    )
    one = [w for w in preflight.key_warnings(summary_only, {}) if "MISTRAL" in w]
    assert "the folded summary stays empty" in one[0]
    assert "the narration" not in one[0]

    # With the key set there is no warning at all. The default model needs Anthropic's key.
    assert preflight.key_warnings(everywhere, {"MISTRAL_API_KEY": "k"}) == []
    assert preflight.key_warnings(Config(), {"ANTHROPIC_API_KEY": "k"}) == []
    (missing,) = preflight.key_warnings(Config(), {})
    assert "ANTHROPIC_API_KEY is not set" in missing


def test_no_clock_interval_makes_a_session_look_stale():
    """sd4: the heartbeat has its own fixed interval, so no clock setting is warned about."""
    for interval in (0.0, 10.0, STALE_AFTER_S, 10 * STALE_AFTER_S):
        config = Config(clock=ClockConfig(check_interval_s=interval))
        assert preflight.heartbeat_warnings(config) == []
        assert not any("stale" in said for said in preflight.warnings(config, {}))


def test_honesty_switched_on_by_halves_is_warned_about():
    """D90: each half without the other is silent, which is the worst misconfiguration."""
    off = Config()
    assert preflight.honesty_warnings(off, []) == [], "both off is the default, not a problem"
    assert preflight.honesty_warnings(off, ["command", "edit"]) == []

    # A mode without the checker: nothing records, so the register stays empty.
    enforcing = Config(honesty=HonestyConfig(mode="enforce"))
    (said,) = preflight.honesty_warnings(enforcing, ["command", "edit"])
    assert "honesty.mode is 'enforce'" in said
    assert "the honesty checker is disabled" in said
    assert "calu checker set honesty --enable" in said

    # The checker without a mode: a model call per edit whose answer is thrown away.
    (wasted,) = preflight.honesty_warnings(off, ["command", "edit", "honesty"])
    assert "thrown away" in wasted
    assert "'track'" in wasted

    # The two agreeing is quiet, in both directions.
    assert preflight.honesty_warnings(enforcing, ["edit", "honesty"]) == []
    tracking = Config(honesty=HonestyConfig(mode="track"))
    assert preflight.honesty_warnings(tracking, ["honesty"]) == []

    # And it reaches the start-up warnings, which is where a user sees it.
    assert said in preflight.warnings(enforcing, {}, ["command", "edit"])


def test_a_checker_on_the_machine_s_claude_account_is_reported_once():
    """D117: `claude:` needs no key and gets no tools, and neither is visible in the config."""
    assert preflight.cli_model_warnings(Config()) == []

    one = Config(command_checker=CheckerConfig(model="claude:haiku"))
    (said,) = preflight.cli_model_warnings(one)
    assert "command checker" in said
    assert "needs no API key" in said and "cannot read files" in said
    assert said in preflight.warnings(one, {"MISTRAL_API_KEY": "k"})

    # Several roles, one warning: the choice was deliberate and does not need repeating.
    both = Config(
        command_checker=CheckerConfig(model="claude:haiku"),
        edit_checker=CheckerConfig(model="claude:sonnet"),
    )
    (together,) = preflight.cli_model_warnings(both)
    assert "command checker, edit checker" in together

    # And it costs no key warning, because the account is not an API provider.
    assert preflight.key_warnings(Config().with_model("claude:haiku"), environ={}) == []


def test_bedrock_checkers_without_aws_credentials_are_warned_about():
    """D132: `key_warnings` cannot cover Bedrock, which has no single key variable."""
    on_bedrock = Config().with_model("bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0")

    (said,) = preflight.bedrock_warnings(on_bedrock, credentials=lambda: None)
    assert "run on AWS Bedrock" in said and "no AWS credentials were found" in said
    assert "command checker" in said and "narrator" in said
    assert "calu doctor --keys" in said and "calu config models auto" in said

    # With credentials nothing is said -- and nothing is said when no role is on Bedrock.
    assert preflight.bedrock_warnings(on_bedrock, credentials=lambda: object()) == []
    assert preflight.bedrock_warnings(Config(), credentials=lambda: None) == []

    # A credential chain that raises (a broken profile file) counts as none, not as a crash.
    def broken() -> object:
        raise ValueError("unparseable ~/.aws/config")

    assert preflight.bedrock_warnings(on_bedrock, credentials=broken)


def test_the_users_own_claude_setup_that_a_session_leaves_out_is_named(tmp_path: Path) -> None:
    """D144 (Q21): hooks, CLAUDE.md, skills and the local settings were left out silently."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("be brief\n", encoding="utf-8")
    (home / ".claude" / "settings.json").write_text(
        '{"hooks": {"PreToolUse": []}, "enabledPlugins": {"x": true}, "theme": "dark"}',
        encoding="utf-8",
    )
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")

    narrowed = Config.model_validate({"claude": {"setting_sources": ["project"]}})
    (said,) = preflight.setting_source_warnings(narrowed, home, project)
    assert "~/.claude/settings.json (hooks, enabledPlugins)" in said, "theme does not matter"
    assert "~/.claude/CLAUDE.md" in said and "~/.claude/skills/" in said
    assert ".claude/settings.local.json" in said
    assert "calu config add claude.setting_sources user" in said

    everything = Config.model_validate(
        {"claude": {"setting_sources": ["user", "project", "local"]}}
    )
    assert preflight.setting_source_warnings(everything, home, project) == []
    # Nothing of the user's: nothing to say. A broken settings file is skipped, not fatal.
    (tmp_path / "empty" / ".claude").mkdir(parents=True)
    (tmp_path / "empty" / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
    assert preflight.setting_source_warnings(narrowed, tmp_path / "empty", None) == []
    # D178: by default every source loads, so nothing is left out to name.
    assert preflight.setting_source_warnings(Config(), home, project) == []


def test_a_password_free_credential_without_a_sandbox_is_named(tmp_path: Path) -> None:
    """D162: a credential stored without a password is named when no working sandbox keeps
    it from the programs Claude runs, because such a credential can resolve escalations."""
    from calu.credentials import CredentialStore, KdfParams

    store = CredentialStore(
        tmp_path / "credentials.json", KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    )
    store.save("operator", "postgresql://op@localhost/calu", None)
    off = Config.model_validate({"claude": {"sandbox": {"enabled": False}}})
    (said,) = preflight.self_approval_warnings(off, store)
    assert "operator" in said and "calu credential set operator" in said

    enforced = Config.model_validate(
        {"claude": {"sandbox": {"enabled": True, "fail_if_unavailable": True}}}
    )
    assert preflight.self_approval_warnings(enforced, store) == [], "a working sandbox: fine"

    store.save("operator", "postgresql://op@localhost/calu", "pw")
    assert preflight.self_approval_warnings(off, store) == [], "a password: fine"


# ---------------------------------------------------------------- cs1: calu's own directories


def test_calus_own_directories_follow_the_xdg_variables(tmp_path: Path, monkeypatch) -> None:
    """The credential store and the env file move with XDG_CONFIG_HOME; so must the deny list."""
    from calu import changes, credentials
    from calu.config import calu_private_paths, user_env_file

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    denied = SandboxConfig().settings()["sandbox"]["filesystem"]["denyRead"]
    assert str(credentials.default_path().parent) in denied
    assert str(user_env_file().parent) in denied
    assert str(changes.default_root().parent) in denied
    # The literal defaults stay, in case the variable is set for calu only.
    assert "~/.config/calu" in denied and "~/.local/state/calu" in denied
    assert calu_private_paths() == denied[: len(calu_private_paths())]


def test_calus_own_directories_cannot_be_removed_from_the_deny_list(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    for configured in ([], ["~/.ssh"]):
        denied = SandboxConfig(deny_read=configured).settings()["sandbox"]["filesystem"]["denyRead"]
        assert denied == ["~/.config/calu", "~/.local/state/calu", *configured]


def test_a_config_home_below_the_home_directory_is_written_with_a_tilde(monkeypatch) -> None:
    from calu.config import calu_private_paths

    environ = {"XDG_CONFIG_HOME": str(Path.home() / ".cfg")}
    assert calu_private_paths(environ) == ["~/.config/calu", "~/.local/state/calu", "~/.cfg/calu"]


# ---------------------------------------------------------------- cs0: the settings files


def test_sandbox_entries_of_the_settings_files_are_named(tmp_path: Path) -> None:
    import json

    home, workspace = tmp_path / "home", tmp_path / "ws"
    (home / ".claude").mkdir(parents=True)
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.local.json").write_text(
        json.dumps(
            {
                "sandbox": {
                    "filesystem": {"allowRead": ["~/.config/calu/.env"]},
                    "excludedCommands": ["python3", "docker"],
                    "network": {"allowedDomains": ["pypi.org", "evil.example"]},
                    "allowUnsandboxedCommands": True,
                }
            }
        )
    )
    (workspace / ".claude" / "settings.json").write_text('{"sandbox": {"enabled": false}}')
    (home / ".claude" / "settings.json").write_text('{"sandbox": {"enabled": true}}')
    sandbox = SandboxConfig(allowed_domains=["pypi.org"], excluded_commands=["docker"])
    config = Config(claude=ClaudeConfig(sandbox=sandbox))

    project, local = preflight.settings_file_sandbox_warnings(config, home, workspace)
    assert project.startswith(".claude/settings.json") and "enabled: false" in project
    assert local.startswith(".claude/settings.local.json")
    assert "filesystem.allowRead: ~/.config/calu/.env" in local
    assert "excludedCommands: python3;" in local  # docker is in calu's own list
    assert "network.allowedDomains: evil.example;" in local
    assert "allowUnsandboxedCommands: true" in local
    assert local in preflight.warnings(config, {}, workspace=workspace)

    # A source calu does not load cannot widen anything, and neither can a file that says
    # nothing beyond calu's own configuration.
    narrowed = Config(claude=ClaudeConfig(sandbox=sandbox, setting_sources=["user"]))
    assert preflight.settings_file_sandbox_warnings(narrowed, home, workspace) == []
    everything = Config(
        claude=ClaudeConfig(
            sandbox=SandboxConfig(
                allowed_domains=["*"],
                excluded_commands=["python3", "docker"],
                allow_unsandboxed_commands=True,
            ),
            setting_sources=["local"],
        )
    )
    (only,) = preflight.settings_file_sandbox_warnings(everything, home, workspace)
    assert "allowedDomains" not in only and "excludedCommands" not in only
    assert "allowUnsandboxedCommands" not in only and "filesystem.allowRead" in only


def test_a_broken_settings_file_is_reported_not_taken_for_empty(tmp_path: Path) -> None:
    """calu cannot say what a file it cannot read sets, so each such file is named."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    (tmp_path / ".claude" / "settings.local.json").write_text('{"sandbox": ["x"]}')
    found = preflight.settings_file_sandbox_warnings(Config(), tmp_path, tmp_path)
    assert any("cannot read it" in line for line in found)
    assert any("not an object" in line for line in found)


# ---------------------------------------------------------------- ci1: keys calu cannot hide


def test_the_key_variable_of_a_provider_is_taken_from_pydantic_ai() -> None:
    from calu.config import provider_key_variables

    assert provider_key_variables("xai") == ("XAI_API_KEY",)
    assert provider_key_variables("huggingface") == ("HF_TOKEN",)
    assert provider_key_variables("heroku") == ("HEROKU_INFERENCE_KEY",)
    assert "AZURE_OPENAI_API_KEY" in (provider_key_variables("azure") or ())
    assert "VERCEL_AI_GATEWAY_API_KEY" in (provider_key_variables("vercel") or ())
    assert provider_key_variables("openai-chat") == ("OPENAI_API_KEY",)
    assert provider_key_variables("bedrock") == ()
    assert provider_key_variables("no-such-provider") is None


def test_a_key_claude_code_would_inherit_is_named_by_its_real_variable(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    config = Config().with_model("xai:grok-4")
    exported = {"XAI_API_KEY": "k"}
    (warning,) = preflight.unknown_provider_warnings(config, env_file, exported)
    assert "'xai'" in warning and "`calu config add hidden_env XAI_API_KEY`" in warning
    assert "XAI_API_KEY is set" in warning
    # The name the prefix would suggest does not hide the key, and so does not end the notice.
    guessed = config.model_copy(update={"hidden_env": ["XAI_API_KEY_GUESS", "GROK_API_KEY"]})
    assert preflight.unknown_provider_warnings(guessed, env_file, exported) != []
    # Named in the setting, or kept in calu's env file: the runner hides it.
    listed = config.model_copy(update={"hidden_env": ["XAI_API_KEY"]})
    assert preflight.unknown_provider_warnings(listed, env_file, exported) == []
    env_file.write_text("XAI_API_KEY=k\n")
    assert preflight.unknown_provider_warnings(config, env_file, exported) == []
    # A key that is not in calu's environment is not inherited by anything.
    assert preflight.unknown_provider_warnings(config, tmp_path / "none", {}) == []
    hf = Config().with_model("huggingface:some/model")
    (warning,) = preflight.unknown_provider_warnings(hf, tmp_path / "none", {"HF_TOKEN": "t"})
    assert "calu config add hidden_env HF_TOKEN" in warning and "HUGGINGFACE" not in warning


def test_providers_calu_hides_by_itself_give_no_notice(tmp_path: Path) -> None:
    exported = {"OPENAI_API_KEY": "k", "MISTRAL_API_KEY": "k"}
    for model in (
        "mistral:m",
        "openai:gpt",
        "openai-chat:gpt",
        "openai-responses:gpt",
        "bedrock:m",
        "claude:haiku",
        "test:model",
        "ollama:m",
    ):
        config = Config().with_model(model)
        assert preflight.unknown_provider_warnings(config, tmp_path / "x", exported) == [], model


def test_a_provider_calu_cannot_look_up_is_named_without_a_guessed_variable(
    tmp_path: Path,
) -> None:
    config = Config().with_model("acme:model-1")
    (warning,) = preflight.unknown_provider_warnings(config, tmp_path / ".env", {})
    assert "'acme'" in warning and "<VARIABLE>" in warning
    assert "ACME_API_KEY" not in warning
    # Nothing calu can check shows that the right variable is hidden, so the notice stays.
    listed = config.model_copy(update={"hidden_env": ["ACME_API_KEY"]})
    assert preflight.unknown_provider_warnings(listed, tmp_path / ".env", {}) != []


def test_a_name_from_hidden_env_is_blanked_for_claude_code(tmp_path: Path) -> None:
    """What `calu run` builds: the loaded secrets plus the `hidden_env` setting."""
    config = Config(hidden_env=["OPENROUTER_API_KEY"])
    runner = SdkRunner(tmp_path, config.claude, {"MISTRAL_API_KEY"} | set(config.hidden_env))
    assert runner._claude_env() == {"MISTRAL_API_KEY": "", "OPENROUTER_API_KEY": ""}


# ---------------------------------------------------------------- round 2: settings files


def _settings_files(tmp_path: Path, **files: dict) -> tuple[Path, Path]:
    """Write settings files and return (home, workspace).

    Args:
        tmp_path: Where to put them.
        **files: "user", "project" or "local" -> the `sandbox` object of that file.
    """
    import json

    home, workspace = tmp_path / "home", tmp_path / "ws"
    (home / ".claude").mkdir(parents=True)
    (workspace / ".claude").mkdir(parents=True)
    paths = {
        "user": home / ".claude" / "settings.json",
        "project": workspace / ".claude" / "settings.json",
        "local": workspace / ".claude" / "settings.local.json",
    }
    for scope, sandbox in files.items():
        paths[scope].write_text(json.dumps({"sandbox": sandbox}))
    return home, workspace


def test_a_session_does_not_start_on_sandbox_entries_calu_did_not_configure(
    tmp_path: Path,
) -> None:
    home, workspace = _settings_files(
        tmp_path, user={"excludedCommands": ["docker"]}, project={"enabled": True}
    )
    (blocker,) = preflight.settings_file_sandbox_blockers(Config(), home, workspace)
    assert blocker.startswith("~/.claude/settings.json") and "excludedCommands: docker" in blocker
    assert "no session starts" in blocker and "fail_if_unavailable" in blocker
    # The same sentence is a start-up warning, so the reason is read before the refusal.
    assert preflight.settings_file_sandbox_warnings(Config(), home, workspace) == [blocker]
    # What calu's own configuration also says is not beyond it.
    own = Config.model_validate({"claude": {"sandbox": {"excluded_commands": ["docker"]}}})
    assert preflight.settings_file_sandbox_blockers(own, home, workspace) == []
    assert preflight.settings_file_sandbox_blockers(own.claude, home, workspace) == []
    # A source that is not loaded is not read by Claude Code either.
    narrowed = Config.model_validate({"claude": {"setting_sources": ["project", "local"]}})
    assert preflight.settings_file_sandbox_blockers(narrowed, home, workspace) == []


def test_without_fail_if_unavailable_the_entries_are_a_warning(tmp_path: Path) -> None:
    home, workspace = _settings_files(tmp_path, local={"filesystem": {"allowWrite": ["/etc"]}})
    lenient = Config.model_validate({"claude": {"sandbox": {"fail_if_unavailable": False}}})
    assert preflight.settings_file_sandbox_blockers(lenient, home, workspace) == []
    (warning,) = preflight.settings_file_sandbox_warnings(lenient, home, workspace)
    assert "filesystem.allowWrite: /etc" in warning and "runs anyway" in warning
    # With the sandbox off, calu asks for none, and there is nothing to compare with.
    off = Config.model_validate({"claude": {"sandbox": {"enabled": False}}})
    assert preflight.settings_file_sandbox_warnings(off, home, workspace) == []


def test_every_sandbox_key_that_weakens_is_named(tmp_path: Path) -> None:
    home, workspace = _settings_files(
        tmp_path,
        project={
            "failIfUnavailable": False,
            "autoAllowBashIfSandboxed": True,
            "enableWeakerNetworkIsolation": True,
            "ignoreViolations": {"*": ["/etc"]},
            "network": {"httpProxyPort": 8080, "socksProxyPort": 1080, "allowLocalBinding": True},
            "filesystem": {"allowWrite": ["~/.ssh"]},
        },
    )
    ((label, entries),) = preflight.settings_file_sandbox_findings(Config(), home, workspace)
    assert label == ".claude/settings.json"
    names = sorted(entry.partition(":")[0] for entry in entries)
    assert names == [
        "autoAllowBashIfSandboxed",
        "enableWeakerNetworkIsolation",
        "failIfUnavailable",
        "filesystem.allowWrite",
        "ignoreViolations",
        "network.allowLocalBinding",
        "network.httpProxyPort",
        "network.socksProxyPort",
    ]
    # A file that only tightens the sandbox, or repeats calu's values, holds no finding.
    home, workspace = _settings_files(
        tmp_path / "tight",
        project={
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"denyRead": ["~/secrets"], "allowRead": []},
            "network": {"allowedDomains": [], "deniedDomains": ["x.example"]},
        },
    )
    assert preflight.settings_file_sandbox_findings(Config(), home, workspace) == []


# ---------------------------------------------------------------- round 2: the permission mode


def test_the_start_says_whether_auto_modes_classifier_is_active(tmp_path: Path) -> None:
    import json

    home, workspace = _settings_files(tmp_path)
    active, line = preflight.permission_mode_line(Config(), home, workspace)
    assert not active and "NOT active" in line and '"default"' in line
    assert "runs on their word alone" in line
    assert preflight.permission_mode_warnings(Config(), home, workspace) == [line]

    required = Config.model_validate({"claude": {"require_auto_mode": True}})
    _, line = preflight.permission_mode_line(required, home, workspace)
    assert "NOT active" in line and "waits for a human" in line

    configured = Config.model_validate({"claude": {"permission_mode": "auto"}})
    active, line = preflight.permission_mode_line(configured, home, workspace)
    assert active and "claude.permission_mode" in line
    assert preflight.permission_mode_warnings(configured, home, workspace) == []

    # Unset in calu: Claude Code's own settings decide, the nearest file first.
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}})
    )
    active, line = preflight.permission_mode_line(Config(), home, workspace)
    assert active and "~/.claude/settings.json" in line
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits"}})
    )
    active, line = preflight.permission_mode_line(Config(), home, workspace)
    assert not active and '"acceptEdits"' in line and ".claude/settings.json" in line
    # A source that is not loaded does not count.
    user_only = Config.model_validate({"claude": {"setting_sources": ["user"]}})
    assert preflight.permission_mode_line(user_only, home, workspace)[0]
    # calu's own setting has the last word.
    plan = Config.model_validate({"claude": {"permission_mode": "plan"}})
    assert not preflight.permission_mode_line(plan, home, workspace)[0]


def test_the_mode_line_is_among_the_start_up_warnings(tmp_path: Path) -> None:
    plan = Config.model_validate({"claude": {"permission_mode": "plan"}})
    said = [w for w in preflight.warnings(plan, {}, workspace=tmp_path) if "classifier" in w]
    assert len(said) == 1 and "NOT active" in said[0]
    auto = Config.model_validate({"claude": {"permission_mode": "auto"}})
    assert not any("NOT active" in w for w in preflight.warnings(auto, {}, workspace=tmp_path))


# ---------------------------------------------------------------- round 2: calu's directories


def test_a_relative_config_home_is_denied_by_its_absolute_path(tmp_path: Path, monkeypatch) -> None:
    from calu.config import calu_private_paths
    from calu.paths import calu_private_dirs

    monkeypatch.chdir(tmp_path)
    paths = calu_private_paths({"XDG_CONFIG_HOME": "cfg"})
    assert str(tmp_path / "cfg" / "calu") in paths
    assert all(path.startswith(("~/", "/")) for path in paths)
    dotted = calu_private_dirs({"XDG_STATE_HOME": str(tmp_path / "a" / ".." / "state")})
    assert tmp_path / "state" / "calu" in dotted
    assert not any(".." in directory.parts for directory in dotted)


def test_a_linked_config_home_is_denied_under_both_names(tmp_path: Path) -> None:
    from calu.config import calu_private_paths
    from calu.paths import calu_private_dir_of

    real, link = tmp_path / "real", tmp_path / "link"
    (real / "calu").mkdir(parents=True)
    link.symlink_to(real)
    environ = {"XDG_CONFIG_HOME": str(link)}
    paths = calu_private_paths(environ)
    assert str(link / "calu") in paths and str(real / "calu") in paths
    for path in (link / "calu" / ".env", real / "calu" / "credentials.json", real / "calu"):
        assert calu_private_dir_of(path, environ) is not None, path
    assert calu_private_dir_of(tmp_path / "real" / "calumet" / "x", environ) is None
    assert calu_private_dir_of(tmp_path / "elsewhere", environ) is None


def test_a_settings_file_calu_cannot_judge_is_a_finding(tmp_path: Path) -> None:
    """The start is refused when calu cannot say what a loaded settings file does to the sandbox.

    That covers a file calu cannot read as JSON, a sandbox key calu does not know, and a value
    of an unexpected type; a missing file and a file without a `sandbox` object hold nothing.
    """
    home, workspace = tmp_path / "home", tmp_path / "work"
    (home / ".claude").mkdir(parents=True)
    (workspace / ".claude").mkdir(parents=True)

    def findings() -> dict[str, list[str]]:
        """Return the findings by file label."""
        return dict(preflight.settings_file_sandbox_findings(Config(), home, workspace))

    assert findings() == {}
    user = home / ".claude" / "settings.json"
    user.write_text('{"model": "opus"}', encoding="utf-8")
    assert findings() == {}

    user.write_text('{"sandbox": {"excludedCommands": ["docker"]},}', encoding="utf-8")
    assert "cannot read it" in findings()["~/.claude/settings.json"][0]

    project = workspace / ".claude" / "settings.json"
    user.write_text("{}", encoding="utf-8")
    project.write_text(
        '{"sandbox": {"someFutureKey": true, "network": {"other": 1}, "enabled": 0,'
        ' "excludedCommands": "docker"}}',
        encoding="utf-8",
    )
    entries = " | ".join(findings()[".claude/settings.json"])
    for expected in ("someFutureKey", "network.other", "enabled: 0", "excludedCommands"):
        assert expected in entries


def test_the_users_settings_file_is_found_where_claude_code_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `CLAUDE_CONFIG_DIR` set, the user's settings file is the one in that directory."""
    relocated = tmp_path / "cfg"
    relocated.mkdir()
    (relocated / "settings.json").write_text('{"sandbox": {"enabled": false}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
    ((label, entries),) = preflight.settings_file_sandbox_findings(Config())
    assert label == "$CLAUDE_CONFIG_DIR/settings.json" and "enabled: false" in entries[0]
