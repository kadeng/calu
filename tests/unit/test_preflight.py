"""Start-up warnings: sandbox tools, checker API keys, write sets; the sandbox settings."""

from pathlib import Path

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
    # Every key that could widen the sandbox is written, also when empty (finding cs0): a key
    # calu left out would be one that only Claude Code's settings files define.
    assert settings["network"] == {
        "allowedDomains": [],  # no allowed domains: the sandbox blocks the network
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
    }
    assert settings["excludedCommands"] == []
    assert settings["filesystem"]["allowRead"] == []
    assert settings["enableWeakerNestedSandbox"] is False


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
        context=ContextConfig(narrate=False, summarizer_model="mistral:ministral-3b-latest"),
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
    both = [w for w in preflight.key_warnings(Config(), {}) if "MISTRAL" in w]
    assert len(both) == 1
    assert "every check of these escalates to you" in both[0]
    assert "the narration and the folded summary stay empty" in both[0]

    # Only the checkers on that provider: nothing about narration.
    elsewhere = Config(context=ContextConfig(narrate=False, summarizer_model="anthropic:x"))
    checkers = [w for w in preflight.key_warnings(elsewhere, {}) if "MISTRAL" in w]
    assert "every check of these escalates" in checkers[0]
    assert "empty" not in checkers[0]

    # One of the two: the verb agrees with it.
    summary_only = Config(context=ContextConfig(narrate=False))
    one = [w for w in preflight.key_warnings(summary_only, {}) if "MISTRAL" in w]
    assert "the folded summary stays empty" in one[0]
    assert "the narration" not in one[0]

    # With the key set there is no warning at all.
    assert preflight.key_warnings(Config(), {"MISTRAL_API_KEY": "k"}) == []


def test_an_interval_that_outlives_the_stale_threshold_is_warned_about():
    """D67: the heartbeat shares the clock interval, and a herd judges staleness by it."""
    assert preflight.heartbeat_warnings(Config()) == []
    # Switched off on purpose: the row keeps the stamp of its insert, so nothing is claimed.
    assert preflight.heartbeat_warnings(Config(clock=ClockConfig(check_interval_s=0))) == []

    too_long = Config(clock=ClockConfig(check_interval_s=STALE_AFTER_S))
    (said,) = preflight.heartbeat_warnings(too_long)
    assert "will call this session stale although it is running" in said
    assert f"{STALE_AFTER_S:g} s" in said
    # It reaches the start-up warnings, which is where a user sees it.
    assert said in preflight.warnings(too_long, {})


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
    """D162: without a working sandbox, a credential stored without a password is readable by
    any program Claude runs; a one-line script could resolve its own escalations."""
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


def test_a_broken_settings_file_gives_no_sandbox_warning(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    (tmp_path / ".claude" / "settings.local.json").write_text('{"sandbox": ["x"]}')
    assert preflight.settings_file_sandbox_warnings(Config(), tmp_path, tmp_path) == []


# ---------------------------------------------------------------- ci1: keys calu cannot hide


def test_a_provider_with_an_unknown_key_variable_is_named(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    config = Config().with_model("openrouter:some/model")
    (warning,) = preflight.unknown_provider_warnings(config, env_file)
    assert "'openrouter'" in warning and "calu config add hidden_env OPENROUTER_API_KEY" in warning
    # Named in the setting, or kept in calu's env file: the runner hides it.
    listed = config.model_copy(update={"hidden_env": ["OPENROUTER_API_KEY"]})
    assert preflight.unknown_provider_warnings(listed, env_file) == []
    env_file.write_text("OPENROUTER_API_KEY=k\n")
    assert preflight.unknown_provider_warnings(config, env_file) == []
    # Providers calu knows, and those without a key variable, are not named.
    for model in ("mistral:m", "bedrock:m", "claude:haiku", "test:model", "ollama:m"):
        assert preflight.unknown_provider_warnings(Config().with_model(model), tmp_path / "x") == []


def test_a_name_from_hidden_env_is_blanked_for_claude_code(tmp_path: Path) -> None:
    """What `calu run` builds: the loaded secrets plus the `hidden_env` setting."""
    config = Config(hidden_env=["OPENROUTER_API_KEY"])
    runner = SdkRunner(tmp_path, config.claude, {"MISTRAL_API_KEY"} | set(config.hidden_env))
    assert runner._claude_env() == {"MISTRAL_API_KEY": "", "OPENROUTER_API_KEY": ""}
