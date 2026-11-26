"""Every command of the full-screen client, in one table.

The command palette (`ctrl+p`), the slash commands of the composer, the footer and the help
screen all read `COMMANDS`, so a command has one name, one key and one description everywhere.
A command the attached session cannot carry out stays in the list and says why
(`Command.capability`, `Command.later`): nothing is silently missing.

A slash command is the word after `/` and the rest of the line; `CaluApp.run_command` receives
the command and that rest. The names of the line client (`calu.attach.HELP`) all exist here
with the same meaning; a test compares the two lists.

A slash word that is not calu's is Claude Code's: the composer sends the line to Claude as
chat text, and Claude Code runs it (agent/records/PLAN.md D248), as the editor plugins do.
`CLAUDE_COMMANDS` are the ones the completion list offers, marked as Claude Code's. calu's
own names do not clash with them: the conversation as the checkers see it is
`/checker-context`, so `/context` is Claude Code's table of the context window. `/usage` is
calu's usage panel, which shows Claude Code's own `/usage` text inside it.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from textual.command import DiscoveryHit, Hit, Hits, Provider

from calu.protocol.messages import SWITCHABLE_PERMISSION_MODES
from calu.tui.capabilities import Capabilities

if TYPE_CHECKING:
    from calu.tui.app import CaluApp


@dataclass(frozen=True)
class Command:
    """One thing the user can ask the client to do."""

    id: str
    title: str
    """What the palette, the slash command list and the help screen show: a few words."""
    key: str = ""
    """The key as shown to the user ("" if the command has none)."""
    slash: str = ""
    """The slash command with its arguments, e.g. `/deny [id] [note]` ("" if it has none)."""
    capability: str = ""
    """Key of the capability the session must offer (`calu.tui.capabilities`)."""
    later: str = ""
    """Non-empty for a command this client does not carry out yet: the sentence it shows."""
    group: str = "Session"
    """The heading the help screen lists the command under."""
    about: str = ""
    """One more sentence for the help screen and the palette's second line ("" if the title
    says it all)."""

    @property
    def name(self) -> str:
        """The slash command's word.

        Returns:
            `/deny` for `/deny [id] [note]`; "" without a slash command.
        """
        return self.slash.split(" ", 1)[0]

    def unavailable(self, caps: Capabilities) -> str | None:
        """Say why the command cannot be carried out now.

        Args:
            caps: What the attached session offers.

        Returns:
            None if it can, else the sentence to show.
        """
        if self.later:
            return self.later
        if self.capability:
            return caps.reason(self.capability)
        return None


COMMANDS: tuple[Command, ...] = (
    # ---- decisions
    Command(
        "allow",
        "Allow the step that waits",
        "a,a · alt+a",
        "/allow [id] [note]",
        group="Decisions",
        about="The plain letter needs a second press; any other key after it types both.",
    ),
    Command(
        "deny", "Deny the step that waits", "d,d · alt+d", "/deny [id] [note]", group="Decisions"
    ),
    Command("note", "Write a note for Claude, then decide", "n,n · alt+n", group="Decisions"),
    Command(
        "answer",
        "Answer a question of the agent",
        "1-9 · o,o · enter",
        "/answer [id] <text>",
        group="Decisions",
    ),
    Command(
        "detail",
        "Detail: everything the checker saw",
        "w,w · alt+w",
        "/why [id]",
        group="Decisions",
        about="Full screen; the step can be decided there, and the same key returns.",
    ),
    Command("next_decision", "Next open decision", "]", group="Decisions"),
    Command("previous_decision", "Previous open decision", "[", group="Decisions"),
    # ---- agents and messages
    Command("next_agent", "Next agent", "alt+→", group="Agents and messages"),
    Command("previous_agent", "Previous agent", "alt+←", group="Agents and messages"),
    Command(
        "pick_agent", "Switch agent by name", "ctrl+o", "/agent [name]", group="Agents and messages"
    ),
    Command(
        "cycle_mode",
        "Send mode: next turn, running turn, a background subagent",
        "tab",
        group="Agents and messages",
    ),
    Command(
        "send_other", "Send once with the other delivery", "ctrl+g", group="Agents and messages"
    ),
    Command(
        "inject", "Send into the running turn", "", "/inject <text>", group="Agents and messages"
    ),
    Command(
        "to",
        "Send to a running background subagent",
        "",
        "/to <agent> <text>",
        group="Agents and messages",
    ),
    Command(
        "side",
        "Side question",
        "f3",
        "/btw [question]",
        capability="side_question",
        group="Agents and messages",
        about="Asked beside the work; not part of the conversation.",
    ),
    Command(
        "comment",
        "Comment on lines of a project file",
        "",
        "/comment <path> <from> <to> <text>",
        group="Agents and messages",
    ),
    Command(
        "withdraw",
        "Withdraw or edit a queued message",
        capability="queue_withdraw",
        group="Agents and messages",
    ),
    Command(
        "stop_subagent",
        "Stop one subagent",
        capability="stop_subagent",
        group="Agents and messages",
    ),
    # ---- session
    Command("interrupt", "Interrupt the running turn", "esc esc", "/interrupt"),
    Command("model", "Choose the model", "", "/model [name]"),
    Command(
        "effort",
        "Choose the effort",
        "",
        "/effort [level]",
        about="A running session is stopped and resumed with it, after a yes.",
    ),
    Command(
        "restart",
        "Stop and resume with another model or effort",
        "",
        "/restart [model] [effort]",
    ),
    Command(
        "level",
        "Supervision level",
        "",
        "/level [name]",
        about="What waits for you besides the checkers' escalations.",
    ),
    Command(
        "mode",
        "Claude Code's permission mode",
        "",
        "/mode [mode]",
        capability="permission_mode",
        about="Claude Code's own Shift+Tab: default, acceptEdits or auto, while it runs.",
    ),
    Command("usage", "Usage: cost, context fill, usage limits", "", "/usage", capability="usage"),
    Command("state", "Session state", "", "/state"),
    Command("context", "The conversation as the checkers see it", "", "/checker-context"),
    Command(
        "checkers",
        "Checkers and their instructions",
        "",
        "/checkers",
        about="Read them; e edits and saves with this machine's calu login.",
    ),
    Command(
        "honesty",
        "Code honesty: state, or change the mode",
        "",
        "/honesty [off | track | enforce]",
        about="Mode, baseline, marked and unreviewed files; t r x m act on the page.",
    ),
    Command(
        "trust",
        "Certify a commit as honest",
        "",
        "/trust [worktree] [commit or branch]",
        about=(
            "Without an argument: HEAD of the main checkout. With a worktree name (Tab "
            "completes it) and a commit or branch: resolved in that worktree. Asks first; "
            "only y certifies."
        ),
    ),
    Command(
        "trust_file",
        "Certify one file as honest",
        "",
        "/trust-file <path>",
        about="The present content of the file. Asks first; only y certifies.",
    ),
    Command("honesty_review", "Let calu review the unreviewed files now", "", "/honesty-review"),
    Command("sessions", "Resume a stored conversation of this project", "", "/sessions"),
    Command("history", "Stored history of a session", "", "/history [id]"),
    Command("log", "The session's log", "", "/log"),
    Command("clear_mark", "Clear an honesty mark", "", "/clear-mark <id>"),
    Command(
        "rule",
        "Add a temporary rule for the checkers",
        "",
        "/rule [duration] [--project | --global] <text>",
        about=(
            "1m to 7d, default 1d; for this session unless --project or --global. Stored "
            "with this machine's calu login, never sent to Claude."
        ),
    ),
    Command("rules", "List the active temporary rules", "", "/rules"),
    Command("rule_remove", "Remove a temporary rule", "", "/rule-remove <id>"),
    Command(
        "start", "Start the session, or resume a stored one", "enter", "/start [resume id | new]"
    ),
    Command(
        "running",
        "The sessions that run, of every directory",
        "",
        "/running [text]",
        about="Reads the database; with a text only the sessions whose id, name or directory "
        "contains it. `/switch` attaches this client to one of them.",
    ),
    Command(
        "switch",
        "Attach this client to another running session",
        "",
        "/switch <the beginning of a session id or of its name>",
        about="The session you leave keeps running.",
    ),
    Command(
        "rename",
        "Name this session, for the lists of sessions",
        "",
        "/rename [name]",
        about="Stored with this machine's calu login; without a name the name is "
        "removed. `/running`, `calu sessions` and the web interface show it.",
    ),
    Command(
        "new",
        "New session in this directory, from calu serve",
        "",
        "/new [first message]",
        about="Needs a running `calu serve` and this machine's calu login. The client "
        "switches to the new session; this one keeps running.",
    ),
    Command(
        "fork",
        "Fork this conversation into a new session, from calu serve",
        "",
        "/fork [first message]",
        about="A copy of the conversation under a new session id; this one stays as it is.",
    ),
    Command(
        "rewind",
        "Undo: a new session that starts before a turn of this conversation",
        "",
        "/rewind <turn> [first message]",
        about="The new session knows the conversation up to the turn before; this one stays "
        "as it is, and files that later turns changed are not changed back.",
    ),
    Command("stop", "Stop the Claude session (calu keeps running)", "", "/stop"),
    Command("shutdown", "Shut down calu (asks for the word shutdown)", "", "/shutdown"),
    # ---- view
    Command(
        "scroll",
        "Scroll the transcript (the input keeps the focus)",
        "pgup pgdn shift+↑↓",
        group="View",
        about="Also the mouse wheel. New output does not move what is shown.",
    ),
    Command("follow", "Jump to the end and follow new output", "end · ctrl+end", group="View"),
    Command(
        "focus_transcript",
        "Move into the transcript",
        "ctrl+↑",
        group="View",
        about="j k move, enter expands, e next waiting step, D diff, / search, i agent "
        "task, t thinking, c copy.",
    ),
    Command(
        "thinking", "Show thinking in full or as one line each", "alt+t", "/thinking", group="View"
    ),
    Command(
        "panel",
        "Move into the side panel",
        "f2",
        group="View",
        about="j k move, enter opens; queue: c copy, r back to the input; held: s send, "
        "d delete; mark: x clear.",
    ),
    Command(
        "diff",
        "Full-screen diff of a step",
        "D (transcript)",
        "/diff",
        group="View",
        about="Side by side when wide; v and enter comment on lines.",
    ),
    Command(
        "git",
        "Open a terminal git program",
        "f4",
        "/git",
        group="View",
        about="In the project directory: gitui by default. CALU_GIT_TOOL or git_tool in "
        "tui.toml name another program. Without one, f4 offers to install gitui.",
    ),
    Command(
        "search",
        "Search the transcript",
        "ctrl+f",
        "/search [text]",
        group="View",
        about="As you type; enter older, ↓ newer. Also / in the transcript.",
    ),
    Command(
        "copy",
        "Copy a block, answer, command, result or diff",
        "alt+c",
        "/copy [answer | block | command | result | diff]",
        group="View",
        about="Without a word: the block under the cursor, or the last answer.",
    ),
    Command(
        "export", "Write the transcript to a Markdown file", "", "/export [file]", group="View"
    ),
    Command(
        "agent_info",
        "The task a subagent was given and what it returned",
        "i (transcript)",
        "/task [agent]",
        group="Agents and messages",
    ),
    Command(
        "hold",
        "Hold the input as a draft",
        "alt+h",
        "/hold [text]",
        group="Agents and messages",
        about="Kept in this client and sent when the session turns idle.",
    ),
    Command(
        "expand", "Show pasted texts in full in the input", "alt+e", group="Agents and messages"
    ),
    Command(
        "bell", "Bell when a step begins to wait: on or off", "", "/bell [on | off]", group="View"
    ),
    Command("theme", "Choose the colour theme", "", "/theme [name]", group="View"),
    Command("help", "Help: all keys and commands", "f1", "/help", group="View"),
    Command("detach", "Detach (the session keeps running)", "ctrl+d", "/quit", group="View"),
)
"""Every command, in the order the help screen lists them."""

ALIASES = {
    "/exit": "/quit",
    "/detail": "/why",
    "/side": "/btw",
    "/agents": "/agent",
    "/find": "/search",
    "/resume": "/sessions",
    "/lazygit": "/git",
    "/gitui": "/git",
}
"""Other words for a slash command."""

CLAUDE_GROUP = "Claude Code"
"""`Command.group` of the entries of `CLAUDE_COMMANDS`."""

CLAUDE_COMMANDS: tuple[Command, ...] = (
    Command(
        "claude",
        "Claude Code: summarize the conversation to free context",
        slash="/compact [what to keep]",
        group=CLAUDE_GROUP,
    ),
    Command(
        "claude",
        "Claude Code: forget the conversation (the transcript here stays)",
        slash="/clear",
        group=CLAUDE_GROUP,
    ),
    Command(
        "claude", "Claude Code: what fills the context window", slash="/context", group=CLAUDE_GROUP
    ),
    Command(
        "claude",
        "Claude Code: cost, durations and tokens of the session",
        slash="/cost",
        group=CLAUDE_GROUP,
    ),
)
"""Claude Code's commands the completion list offers; each one is sent as chat text, and
Claude Code runs it without the model (measured through calu, as for the editor plugins).
Any other slash word that is not calu's is sent the same way."""

CLAUDE_WORD = re.compile(r"/[\w:.-]+")
"""A slash word that can name a command of Claude Code (built-in, a skill or a project's)."""


def is_claude_command(word: str) -> bool:
    """Tell whether a slash word goes to Claude Code instead of being one of calu's commands.

    Args:
        word: The first word of the input, e.g. `/compact`.

    Returns:
        True if it is no calu command or alias (`by_slash`) and looks like a command name
        (`CLAUDE_WORD`: letters, digits, `_`, `:`, `.`, `-`).
    """
    return by_slash(word) is None and CLAUDE_WORD.fullmatch(word) is not None


def by_slash(word: str) -> Command | None:
    """Find the command of a slash word.

    Args:
        word: The first word of the input, e.g. `/deny`.

    Returns:
        The command, or None for an unknown word.
    """
    word = ALIASES.get(word, word)
    return next((command for command in COMMANDS if command.name == word), None)


def completions(prefix: str) -> list[str]:
    """List the slash commands that start with a prefix.

    Args:
        prefix: What the user has typed, starting with `/`.

    Returns:
        The matching slash words in the order of `COMMANDS`, then those of `CLAUDE_COMMANDS`.
    """
    return [
        command.name
        for command in (*COMMANDS, *CLAUDE_COMMANDS)
        if command.name and command.name.startswith(prefix)
    ]


ARGUMENTS: dict[str, Sequence[str]] = {"/mode": SWITCHABLE_PERMISSION_MODES}
"""Slash words whose argument is one of a fixed list, which Tab completes."""


def argument_completions(text: str, known: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """List the arguments of a slash command that start like the typed one.

    Args:
        text: The composer's text, e.g. `/mode ac`.
        known: Arguments that the session names, by slash word, in addition to `ARGUMENTS`:
            `{"/trust": <the worktree names>}` completes the first argument of `/trust`.

    Returns:
        The matching arguments in the order of `ARGUMENTS` (or of `known`); empty if the
        command takes no argument from a list or the text has more than one argument.

    Example:
        `argument_completions("/mode a")` is `["acceptEdits", "auto"]`;
        `argument_completions("/trust ag", {"/trust": ["agent-a1", "wta"]})` is `["agent-a1"]`.
    """
    word, _, typed = text.partition(" ")
    choices = ARGUMENTS.get(word)
    if choices is None and known is not None:
        choices = known.get(word) or None
    if choices is None or " " in typed:
        return []
    return [choice for choice in choices if choice.lower().startswith(typed.lower())]


def slash_names() -> Sequence[str]:
    """List every slash word the client understands.

    Returns:
        The words of `COMMANDS` and of `ALIASES`.
    """
    return [*[command.name for command in COMMANDS if command.name], *ALIASES]


class CaluCommands(Provider):
    """The command palette's source: `COMMANDS`, each with its key and whether it is available."""

    def _entry(self, command: Command) -> tuple[str, str]:
        """Build what the palette shows for a command.

        Args:
            command: The command.

        Returns:
            `(display text, help text)`: the title with the key; and the slash command with
            the command's `about`, or the sentence that says why it is not available.
        """
        app: CaluApp = self.app  # type: ignore[assignment]
        reason = command.unavailable(app.capabilities)
        title = command.title + (f"  ({command.key})" if command.key else "")
        if reason:
            return f"{title}  [not available]", reason
        return title, " · ".join(part for part in (command.slash, command.about) if part)

    async def discover(self) -> Hits:
        """List every command when the palette opens without a query.

        Yields:
            One entry per command.
        """
        app: CaluApp = self.app  # type: ignore[assignment]
        for command in COMMANDS:
            display, help_text = self._entry(command)
            yield DiscoveryHit(display, partial(app.run_command, command.id, ""), help=help_text)

    async def search(self, query: str) -> Hits:
        """Find the commands that match what the user typed.

        Args:
            query: The text typed into the palette.

        Yields:
            The matching commands, best first.
        """
        app: CaluApp = self.app  # type: ignore[assignment]
        matcher = self.matcher(query)
        for command in COMMANDS:
            display, help_text = self._entry(command)
            score = matcher.match(f"{display} {command.slash}")
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    partial(app.run_command, command.id, ""),
                    help=help_text,
                )
