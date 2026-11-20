"""The library of checker instruction templates shipped in `calu/defaults/templates/`.

A template is an example text for one checker type, which the user loads into a checker
(`calu checker template use NAME`) and adapts. `calu setup` loads none of them. Each file is
one template; its name is the file name without `.md`:

    ---
    checker: subagent-result          the checker type the template is written for
    summary: one line for the list    what the rules say
    ---
    # Review results                  the text `use` stores (everything after the header)
    ...

`README.md` in the directory is the index for readers and is no template. The texts are in
the public domain, like the shipped checker instructions (LICENSE.md; agent/records/PLAN.md
D250).
"""

from dataclasses import dataclass
from importlib import resources

HEADER_LINE = "---"
"""The line that opens and closes a template's header."""


class TemplateError(Exception):
    """A template does not exist or its file is malformed; the message says which."""


@dataclass(frozen=True)
class Template:
    """One template of checker instructions."""

    name: str
    """The file name without `.md`, e.g. "review-findings"."""
    checker: str
    """The checker type the template is written for, e.g. "subagent-result"."""
    summary: str
    """One line: what the rules say."""
    text: str
    """The instructions `use` stores, ending with one newline."""


def parse(name: str, content: str) -> Template:
    """Read a template file's header and text.

    Args:
        name: The template's name.
        content: The file's content.

    Returns:
        The template; its text without the header, without leading blank lines, ending with
        exactly one newline.

    Raises:
        TemplateError: If the header is missing or unclosed, names no checker or no summary,
            or the text is empty.

    Example:
        A file with the lines "---", "checker: edit", "summary: s", "---", "# Rules" gives
        checker "edit" and the text "# Rules" with a newline.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != HEADER_LINE:
        raise TemplateError(f"template {name!r} does not start with a {HEADER_LINE} header")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == HEADER_LINE)
    except StopIteration:
        raise TemplateError(f"template {name!r}: the header is not closed") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    checker, summary = fields.get("checker", ""), fields.get("summary", "")
    if not checker or not summary:
        raise TemplateError(f"template {name!r}: the header needs a checker and a summary")
    text = "\n".join(lines[end + 1 :]).strip("\n")
    if not text.strip():
        raise TemplateError(f"template {name!r} has no text")
    return Template(name, checker, summary, text + "\n")


def templates() -> list[Template]:
    """Read every template shipped with calu.

    Returns:
        The templates in name order.

    Raises:
        TemplateError: If a shipped file is malformed (a defect of calu, caught by its tests).
    """
    folder = resources.files("calu.defaults").joinpath("templates")
    found = [
        parse(entry.name.removesuffix(".md"), entry.read_text(encoding="utf-8"))
        for entry in folder.iterdir()
        if entry.name.endswith(".md") and entry.name != "README.md"
    ]
    return sorted(found, key=lambda template: template.name)


def template(name: str) -> Template:
    """Return one shipped template by name.

    Args:
        name: The template's name, with or without `.md`.

    Returns:
        The template.

    Raises:
        TemplateError: If calu ships no template of that name; the message lists the names.
    """
    wanted = name.removesuffix(".md")
    shipped = templates()
    for found in shipped:
        if found.name == wanted:
            return found
    names = ", ".join(found.name for found in shipped)
    raise TemplateError(f"calu ships no template named {name!r}; the templates are: {names}")


def matching(text: str) -> Template | None:
    """Find the template whose text a stored instruction text is, unchanged.

    Args:
        text: A stored instruction text.

    Returns:
        The template, or None if the text is no template's (surrounding blank lines and
        trailing spaces do not count).
    """
    wanted = _normal(text)
    return next((found for found in templates() if _normal(found.text) == wanted), None)


def _normal(text: str) -> str:
    """Return a text without trailing spaces on its lines and without surrounding blank lines.

    Args:
        text: Any text.

    Returns:
        The text in that form, for comparisons.
    """
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines())
