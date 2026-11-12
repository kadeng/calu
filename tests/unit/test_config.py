import pydantic
import pytest

from calu.config import Config, build_config, merge_settings


def test_merge_is_deep_for_dicts_and_replaces_other_values() -> None:
    base = {"claude": {"model": "opus", "effort": "low"}, "context": {"files": ["A"]}}
    merged = merge_settings(base, {"claude": {"model": "sonnet"}, "context": {"files": ["B"]}})
    assert merged == {"claude": {"model": "sonnet", "effort": "low"}, "context": {"files": ["B"]}}
    assert base["claude"]["model"] == "opus"


def test_layers_and_checker_rows_build_the_config() -> None:
    config = build_config(
        [{"claude": {"model": "opus", "use_bedrock": True}}, {"claude": {"model": "sonnet"}}],
        [
            ("command", "fireworks:x", 2, 1000, 500),
            ("narrator", "mistral:small", 3, 8000, 4000),
            ("custom", "ignored:y", 1, 1, 1),
        ],
    )
    assert config.claude.model == "sonnet" and config.claude.use_bedrock
    assert config.command_checker.model == "fireworks:x"
    assert config.command_checker.max_tokens == 500
    assert config.edit_checker == Config().edit_checker
    assert config.context.narrator_model == "mistral:small"


def test_the_permission_mode_is_left_to_claude_code() -> None:
    assert build_config([]).claude.permission_mode is None


def test_invalid_values_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        build_config([{"context": {"fold_every": 1}}])


def test_the_configuration_wins_over_a_checker_definition() -> None:
    """`calu config set command_checker.model` has to beat the definition's own model.

    Both layers carry a model: the checker definition (`calu checker set --model`) and the
    configuration (`calu config set`, the web interface's settings form). The configuration is
    the layer a project overrides, so it wins, and the definition's value is the default for
    what nobody configured. Until 2026-11-10 it was the other way round and the documented
    `calu config set command_checker.model` silently did nothing (PLAN.md D51).
    """
    rows = [
        ("command", "definition:command", 7, 700, 70),
        ("edit", "definition:edit", 8, 800, 80),
        ("narrator", "definition:narrator", 1, 100, 10),
    ]
    # Nothing configured: the definition decides.
    plain = build_config([], rows)
    assert plain.command_checker.model == "definition:command"
    assert plain.command_checker.request_limit == 7
    assert plain.context.narrator_model == "definition:narrator"

    # Configured globally: the configuration decides, field by field.
    configured = build_config([{"command_checker": {"model": "configured:command"}}], rows)
    assert configured.command_checker.model == "configured:command"
    assert configured.command_checker.request_limit == 7  # not configured, so the definition's
    assert configured.edit_checker.model == "definition:edit"  # untouched

    # A project overrides the global layer, which overrides the definition.
    layered = build_config(
        [
            {"command_checker": {"model": "global:command", "max_tokens": 1}},
            {"command_checker": {"model": "project:command"}},
        ],
        rows,
    )
    assert layered.command_checker.model == "project:command"
    assert layered.command_checker.max_tokens == 1


def test_hidden_env_holds_variable_names_only() -> None:
    assert Config(hidden_env=["XAI_API_KEY", "HF_TOKEN"]).hidden_env == ["XAI_API_KEY", "HF_TOKEN"]
    for entry in ("A=B", "", "MY KEY", "A\0B"):
        with pytest.raises(pydantic.ValidationError, match="hidden_env"):
            Config(hidden_env=[entry])
        with pytest.raises(pydantic.ValidationError):
            build_config([{"hidden_env": [entry]}])
