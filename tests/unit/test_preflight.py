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


def test_the_three_network_policies_are_expressed_in_the_sandbox_settings() -> None:
    """D39: measured against the real CLI — no network key, an allow list, or the wildcard."""
    configured = SandboxConfig(allowed_domains=["pypi.org"], denied_domains=["evil.example"])

    nothing = configured.with_policy("none").settings()["sandbox"]
    assert "allowedDomains" not in nothing.get("network", {})
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
