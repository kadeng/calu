"""The definitions an edit changes, and how an edit step shows their contract."""

from pathlib import Path

from calu.checkers.model import Step
from calu.checkers.render import render_step
from calu.checkers.symbols import changed_definitions

BEFORE = '''"""A module."""


class Parser:
    """Reads fields."""

    def parse(self, text: str) -> list[str]:
        """Return the fields of `text`, sorted.

        Args:
            text: One record.
        """
        return sorted(text.split(","))

    def count(self, text: str) -> int:
        """Return the number of fields."""
        return len(self.parse(text))


def helper(value):
    return value + 1
'''


def changed(after: str, path: str = "src/parser.py") -> dict[str, str]:
    """Return the changed definitions of BEFORE -> `after` as name -> what changed."""
    return {item.name: item.what_changed for item in changed_definitions(BEFORE, after, path)}


def test_a_changed_body_is_named_with_the_parts_that_did_not_change() -> None:
    """The case honesty is about: the code changes and the text that describes it does not."""
    after = BEFORE.replace('return sorted(text.split(","))', 'return text.split(",")')
    assert changed(after) == {
        "Parser.parse": "the body changed; the signature and docstring did not"
    }


def test_each_part_of_a_definition_is_told_apart() -> None:
    after = BEFORE.replace(
        "def count(self, text: str) -> int:", "def count(self, line: str) -> int:"
    )
    after = after.replace("return len(self.parse(text))", "return len(self.parse(line))")
    assert changed(after) == {
        "Parser.count": "the signature and body changed; the docstring did not"
    }
    after = BEFORE.replace("Return the number of fields.", "Return how many fields there are.")
    assert changed(after) == {
        "Parser.count": "the docstring changed; the signature and body did not"
    }
    after = BEFORE.replace('"""Reads fields."""', '"""Reads and counts fields."""')
    assert changed(after) == {"Parser": "the docstring changed; the class line and body did not"}


def test_a_definition_without_a_docstring_says_so() -> None:
    after = BEFORE.replace("return value + 1", "return value + 2")
    assert changed(after) == {
        "helper": "the body changed; the signature did not; it has no docstring"
    }


def test_formatting_comments_and_new_definitions_are_not_changes() -> None:
    """Only a definition that existed before and now says or does something else counts."""
    after = BEFORE.replace(
        'return sorted(text.split(","))', 'return sorted(  text.split(",")  )  # x'
    )
    assert changed(after) == {}
    after = BEFORE + "\n\ndef added(value):\n    return value\n"
    assert changed(after) == {}


def test_other_files_and_broken_python_yield_nothing() -> None:
    after = BEFORE.replace("value + 1", "value + 2")
    assert changed(after, "src/parser.ts") == {}
    assert changed(BEFORE + "\ndef broken(:\n") == {}


def test_an_edit_step_shows_the_contract_of_the_function_it_changes(tmp_path: Path) -> None:
    """The docstring is outside the diff's context; the checker gets it without a tool call."""
    body = "\n".join(f"        step_{n} = {n}" for n in range(12))
    source = BEFORE.replace(
        '        return sorted(text.split(","))',
        body + '\n        return sorted(text.split(","))',
    )
    (tmp_path / "parser.py").write_text(source, encoding="utf-8")
    step = Step(
        tool_name="Edit",
        tool_input={
            "file_path": "parser.py",
            "old_string": 'return sorted(text.split(","))',
            "new_string": 'return text.split(",")',
        },
        cwd=str(tmp_path),
    )
    shown = render_step(step, tmp_path)
    diff, _, contracts = shown.partition("Definitions this edit changes")
    assert "sorted." not in diff, "the docstring is not part of the diff"
    assert "`Parser.parse`, line 7: the body changed" in contracts
    assert "def parse(self, text: str) -> list[str]:" in contracts
    assert "Return the fields of `text`, sorted." in contracts


def test_a_new_file_and_a_text_file_get_no_contract_section(tmp_path: Path) -> None:
    for name, content in (("new.py", "def f():\n    return 1\n"), ("notes.md", "# Notes\n")):
        step = Step(
            tool_name="Write", tool_input={"file_path": name, "content": content}, cwd=str(tmp_path)
        )
        assert "Definitions this edit changes" not in render_step(step, tmp_path)
