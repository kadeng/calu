"""Routing of tool calls to checkers.

Each tool name maps to a `Route`:

    Bash                                  -> COMMAND  (command checker)
    Edit, Write, MultiEdit, NotebookEdit  -> EDIT     (edit checker)
    AskUserQuestion                       -> QUESTION (the human answers it; PLAN.md D35)
    read-only built-in tools              -> ALLOW    (no check)
    anything else (WebFetch, MCP tools…)  -> `default` (COMMAND unless configured otherwise)

The table can be overridden per tool name, for example to escalate all calls of one MCP tool.
The user's allow and deny lists (`calu.checkers.rules`) are applied before the
route: they can escalate a call, let a Bash call run without a check, or send a read of an
unlisted path to the command checker. A Bash call that creates a git commit goes to CHANGESET
instead of COMMAND, because the interesting part of a commit is the change, not the command
(`calu.checkers.changeset`, PLAN.md D26). `AskUserQuestion` goes to QUESTION: no checker judges
it, because only the human knows the answer, and the answer reaches Claude as the message of the
refusal that keeps the call from running (D35).
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Route(StrEnum):
    """What happens to a tool call before it runs."""

    COMMAND = "command"  # reviewed by the command checker
    EDIT = "edit"  # reviewed by the edit checker
    CHANGESET = "changeset"  # a commit: the changeset checker reviews the whole change
    ALLOW = "allow"  # runs without a check
    ESCALATE = "escalate"  # always asks the human
    SUBAGENT = "subagent"  # a subagent's final result, reviewed by the result checker
    QUESTION = "question"  # the agent asks the human; the answer goes back as the result
    DENY = "deny"  # never runs


EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "NotebookRead",
        "TodoWrite",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
        "Agent",  # starting a subagent; the subagent's own tool calls are checked
        "Task",  # older name of Agent
        "SendMessage",
        "Skill",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
    }
)
"""Tools that run without a check.

Most of them read. The four that do not are deliberate, and each is safe for the same reason --
whatever they set in motion is itself checked:

* `TodoWrite`, `TaskCreate`, `TaskUpdate` write Claude Code's own plan and task state, which
  never reaches the project or the shell.
* `Agent` (and its older name `Task`) starts a subagent. Every tool call that subagent makes
  goes through the same gate, and what it hands back is reviewed by the subagent result checker,
  so starting one buys no unchecked work.
* `SendMessage` is how the main agent relays a client's message to one of its subagents
  (`calu.protocol.messages`, `chat.send {to: <agent id>}`), so checking it would put a checker
  call and a possible denial in the way of calu's own relay. The free route holds **only while
  the message stays inside the session**: Claude Code's `SendMessage` also reaches other
  sessions on the machine, whose steps no checker of this session sees. The session therefore
  compares the `to` value with its own subagents and sends a message to anything else to the
  command checker (`Session._own_agent`, PLAN.md D69). calu's own relay always matches,
  because the id it tells the main agent to use comes from that same registry.
* `Skill` loads instructions. The tool calls the skill then asks for are checked like any other.

Everything that leaves the machine (`WebFetch`, `WebSearch`) or that calu does not know is
deliberately absent: it falls to `ToolPolicy.default`, which is the command checker. A route of
this table is a ceiling, not a promise: the session may still send a call to a checker that the
table would let through, which is what it does for a read outside the allow list and for a
`SendMessage` that leaves the session.
"""


class ToolPolicy(BaseModel):
    """Maps tool names to routes.

    `overrides` take precedence over the built-in table; `default` applies to tools that the
    table does not know.
    """

    overrides: dict[str, Route] = Field(default_factory=dict)
    default: Route = Route.COMMAND

    def route(self, tool_name: str) -> Route:
        """Return the route for a call of `tool_name`.

        Args:
            tool_name: Name of the tool as Claude Code reports it (e.g. "Bash", "mcp__x__y").

        Returns:
            The route from `overrides`, else from the built-in table, else `default`.
        """
        if tool_name in self.overrides:
            return self.overrides[tool_name]
        if tool_name == "Bash":
            return Route.COMMAND
        if tool_name in EDIT_TOOLS:
            return Route.EDIT
        if tool_name == "AskUserQuestion":
            return Route.QUESTION
        if tool_name in READ_ONLY_TOOLS:
            return Route.ALLOW
        return self.default


KNOWN_TOOLS = (
    frozenset({"Bash", "AskUserQuestion", "WebFetch", "WebSearch"}) | EDIT_TOOLS | (READ_ONLY_TOOLS)
)
"""Tools whose route was chosen on purpose. `WebFetch` and `WebSearch` are here although they
fall to the default route, because that is deliberate; every other tool reaches the default
route only because nobody looked at it (`unknown_tools`, D154)."""


def unknown_tools(offered: list[str], configured: dict[str, Route] | None = None) -> list[str]:
    """Return the tools Claude Code offers that no route was chosen for (PLAN.md D154).

    Claude Code adds tools with its own updates (2.1.319 offers `RemoteTrigger`, `CronCreate`,
    `Artifact`, `Workflow` and others). Such a tool goes to `default_route` -- the command
    checker unless configured otherwise -- so it is not unchecked, but some of them reach
    outside the session (a cloud agent, a published page), and the user should hear that they
    exist. MCP tools (`mcp__...`) are left out: they are the user's own, and going to the
    default route is their documented behaviour.

    Args:
        offered: The tool names of the CLI's init message.
        configured: `tool_routes`, whose names count as chosen.

    Returns:
        The unknown names, sorted.
    """
    chosen = KNOWN_TOOLS | set(configured or {})
    return sorted(name for name in offered if name not in chosen and not name.startswith("mcp__"))
