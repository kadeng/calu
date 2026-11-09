"""Files that a tool call refers to, for links in the front ends (`tool.files` events).

    Bash command  -> its path arguments (parsed by `checkers.shell`) that are existing text files
    Skill call    -> the skill's SKILL.md (known once Claude Code inserts the skill text)

Only regular files with a known text extension (`TEXT_EXTENSIONS`) or name (`TEXT_FILE_NAMES`)
are linked, and none that the read deny lists match (`Rules.read_denied`), so a link never
offers to open e.g. a private key.
"""

from pathlib import Path

from calu.checkers.rules import Rules
from calu.checkers.shell import parse_command
from calu.protocol.messages import FileLink

# fmt: off
TEXT_EXTENSIONS = frozenset(
    {
        # documentation and plain text
        ".md", ".markdown", ".rst", ".txt", ".adoc", ".tex", ".bib", ".org",
        # logs and command output
        ".log", ".out", ".err",
        # data and configuration
        ".json", ".jsonc", ".json5", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".conf", ".properties", ".csv", ".tsv", ".xml",
        ".lock", ".sql", ".graphql", ".proto", ".tf", ".hcl", ".ipynb",
        # scripts and shells
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".pl", ".pm", ".rb", ".lua",
        ".r", ".jl", ".php", ".awk", ".sed",
        # Python, web
        ".py", ".pyi", ".pyx", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
        ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sass", ".less",
        # compiled languages
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".rs", ".go", ".java", ".kt",
        ".kts", ".scala", ".groovy", ".gradle", ".cs", ".fs", ".vb", ".swift", ".m", ".mm",
        ".dart", ".zig", ".nim", ".hs", ".ml", ".mli", ".ex", ".exs", ".erl", ".clj", ".elm",
        ".sol", ".v", ".sv", ".vhd", ".asm", ".s", ".cmake",
        # patches
        ".diff", ".patch",
    }
)
"""File extensions (lower case) that the front ends link; VSCode opens all of them as text."""

TEXT_FILE_NAMES = frozenset(
    {
        "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Jenkinsfile",
        "Vagrantfile", "Gemfile", "Rakefile", "Procfile", "Justfile", "justfile", "LICENSE",
        "README", "CHANGELOG", "CODEOWNERS", ".gitignore", ".gitattributes", ".dockerignore",
        ".editorconfig",
    }
)
"""File names without a text extension that the front ends link as well."""
# fmt: on


def is_text_file(path: Path) -> bool:
    """Tell whether `path` is an existing regular file of a linkable text type.

    Reads file metadata only (no content).

    Args:
        path: An absolute path.

    Returns:
        True if the file exists, is a regular file (after symlinks) and its name is in
        `TEXT_FILE_NAMES` or its (lower-case) suffix is in `TEXT_EXTENSIONS`.
    """
    if path.name not in TEXT_FILE_NAMES and path.suffix.lower() not in TEXT_EXTENSIONS:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def command_files(command: str, cwd: Path, rules: Rules) -> list[FileLink]:
    """Return the existing text files that a Bash command names, for inline links.

    Each path argument of the command (`checkers.shell.parse_command`, which also follows `cd`)
    is resolved; the first candidate that is a linkable text file (`is_text_file`) and not read
    denied (`Rules.read_denied`) becomes a link. A pytest node id (`tests/a.py::test_x`) links
    its file. Each file is listed once, in the order of the command.

    Args:
        command: The command line.
        cwd: The directory the command starts in.
        rules: The workspace's rules (for the read deny lists).

    Returns:
        The links: `raw` is the argument as written in the command, `path` the absolute file.

    Example:
        `command_files("wc -l docs/PLAN.md; git log", Path("/w"), rules)` gives
        `[FileLink(raw="docs/PLAN.md", path="/w/docs/PLAN.md")]` if that file exists.
    """
    links: list[FileLink] = []
    seen: set[Path] = set()
    for used in parse_command(command, cwd).paths:
        raw = used.raw
        candidates = list(used.paths)
        if "::" in raw:
            raw = raw.split("::", 1)[0]
            candidates = [None if p is None else _before_node_id(p) for p in candidates]
        for path in candidates:
            if path is None or path in seen or not is_text_file(path) or rules.read_denied(path):
                continue
            seen.add(path)
            links.append(FileLink(raw=raw, path=str(path)))
            break
    return links


def skill_file(base_directory: str) -> FileLink | None:
    """Return the link to a loaded skill's SKILL.md.

    Args:
        base_directory: The directory from the line "Base directory for this skill: <dir>"
            that Claude Code puts before the skill text.

    Returns:
        A link with `raw` = the skill directory's name and `path` = <dir>/SKILL.md, or None if
        that file does not exist.
    """
    path = Path(base_directory.strip()) / "SKILL.md"
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    return FileLink(raw=path.parent.name, path=str(path)) if exists else None


def _before_node_id(path: Path) -> Path:
    """Strip a pytest node id from a resolved path (`/w/tests/a.py::test_x` -> `/w/tests/a.py`).

    Args:
        path: A resolved path that may end in "::<node id>".

    Returns:
        The path up to the first "::".
    """
    return Path(str(path).split("::", 1)[0])
