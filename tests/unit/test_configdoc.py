"""The configuration reference is generated, and the committed file must match the model."""

from pathlib import Path

from pydantic import BaseModel

from calu.config import Config
from calu.configdoc import SKIP, configuration_markdown, default_of, type_name

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "CONFIGURATION.md"


def test_the_committed_reference_matches_the_configuration_model() -> None:
    """Regenerate `docs/CONFIGURATION.md`; a difference means someone forgot to run it.

    The fix is one command:

        uv run python -c "from pathlib import Path; from calu.configdoc import \
configuration_markdown; Path('docs/CONFIGURATION.md').write_text(configuration_markdown())"
    """
    assert REFERENCE.is_file(), "docs/CONFIGURATION.md is missing"
    assert REFERENCE.read_text(encoding="utf-8") == configuration_markdown(), (
        "docs/CONFIGURATION.md is out of date; regenerate it (see this test's docstring)"
    )


def test_every_setting_is_documented_and_listed() -> None:
    """A field without a description would be a setting nobody can look up."""
    reference = configuration_markdown()

    def walk(model: type[BaseModel], prefix: str = "") -> list[str]:
        keys: list[str] = []
        for name, info in model.model_fields.items():
            key = f"{prefix}{name}"
            if key in SKIP:
                continue
            assert info.description, f"{key} has no docstring under it"
            keys.append(key)
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                keys += walk(annotation, f"{key}.")
        return keys

    for key in walk(Config):
        assert f"| `{key}` |" in reference, f"{key} is not in the reference"


def test_a_value_is_rendered_the_way_it_is_typed() -> None:
    """The type and default columns have to be copy-pasteable, not Python repr."""
    assert type_name(bool) == "bool"
    assert type_name(list[str]) == "list of text"
    assert type_name(str | None) == "text (or empty)"
    fields = Config.model_fields
    assert default_of(fields["check_subagent_results"]) == "`true`"
    assert default_of(fields["claude"]) == "see below"
    assert default_of(fields["tool_routes"]) == "(empty)"
