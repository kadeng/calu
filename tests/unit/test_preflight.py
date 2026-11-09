"""Start-up warnings: sandbox tools, checker API keys, write sets; the sandbox settings."""

from pathlib import Path

from calu import preflight
from calu.claude.sdk_runner import SdkRunner
from calu.config import (
    ChangesConfig,
    CheckerConfig,
    ClaudeConfig,
    Config,
    ContextConfig,
    SandboxConfig,
)


def test_sandbox_settings_document_matches_the_spike():
    settings = SandboxConfig().settings()["sandbox"]
    assert settings["enabled"] is True
    assert settings["failIfUnavailable"] is True
    assert settings["allowUnsandboxedCommands"] is False
    # Claude Code must not approve sandboxed Bash itself: auto mode is the second opinion (D5).
    assert settings["autoAllowBashIfSandboxed"] is False
    assert "~/.config/calu" in settings["filesystem"]["denyRead"]
    assert "~/.claude/.credentials.json" in settings["filesystem"]["denyRead"]
    assert "network" not in settings  # no allowed domains: the sandbox blocks the network
    assert "excludedCommands" not in settings


def test_sandbox_settings_carry_network_and_exclusions():
    config = SandboxConfig(
        allowed_domains=["pypi.org"], excluded_commands=["docker"], weaker_nested_sandbox=True
    )
    settings = config.settings()["sandbox"]
    assert settings["network"] == {"allowedDomains": ["pypi.org"]}
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
