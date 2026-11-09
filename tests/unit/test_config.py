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
