"""The part of the Agent Client Protocol that calu's adapter speaks (PLAN.md D24, §6.1).

ACP (agentclientprotocol.com) is how an editor talks to a coding agent: JSON-RPC 2.0, the
editor is the *client*, the agent is the *server*, newline-delimited JSON over stdin and
stdout. One implementation reaches JetBrains Air, Zed, Neovim, Goose and every other ACP
client. The shapes here follow the published schema of protocol version 1 (schema.json, read
2026-11-09).

The wire uses camelCase keys, Python uses snake_case, so every model below generates camelCase
aliases and is serialized with `by_alias=True` (`wire`). Inbound models ignore unknown fields,
because the client may send more than this version knows.

What calu deliberately does **not** use (§6.1, points 3 and 4):

- the client's `fs/*` and `terminal/*` capabilities: Claude Code does its own file and command
  work inside the sandbox, and letting the editor read, write or run things on the agent's
  behalf would route tool calls around both the checkers and the sandbox;
- the `mcpServers` of `session/new`: an editor could otherwise add tools to the trusted zone.

Both are therefore reported as unsupported or ignored, never forwarded.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

PROTOCOL_VERSION = 1
"""The ACP major version this adapter implements; `initialize` echoes it."""

StopReason = Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]
"""Why a prompt turn ended (the result of `session/prompt`)."""

ToolKind = Literal[
    "read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"
]
"""How a client may present a tool call; "other" is the default."""

ToolCallStatus = Literal["pending", "in_progress", "completed", "failed"]
"""Life cycle of a tool call as the client shows it."""

PermissionKind = Literal["allow_once", "allow_always", "reject_once", "reject_always"]
"""The four option kinds of `session/request_permission`."""

TOOL_KINDS: dict[str, ToolKind] = {
    "Bash": "execute",
    "BashOutput": "execute",
    "KillShell": "execute",
    "Edit": "edit",
    "Write": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Read": "read",
    "NotebookRead": "read",
    "Glob": "search",
    "Grep": "search",
    "LS": "search",
    "WebFetch": "fetch",
    "WebSearch": "fetch",
    "Agent": "think",
    "Task": "think",
    "Skill": "think",
    "TodoWrite": "other",
}
"""Claude Code tool name -> ACP tool kind. Unknown names (MCP tools) become "other"."""

# JSON-RPC error codes: the three ACP defines next to the standard ones.
AUTH_REQUIRED = -32000
"""The client must call `authenticate` first."""
UNKNOWN_SESSION = -32002
"""The `sessionId` is not one this agent serves."""
INVALID_PARAMS = -32602
"""Standard JSON-RPC: the params do not fit the method."""
METHOD_NOT_FOUND = -32601
"""Standard JSON-RPC: this agent does not implement the method."""
INTERNAL_ERROR = -32603
"""Standard JSON-RPC: the handler failed."""
PARSE_ERROR = -32700
"""Standard JSON-RPC: the line was not JSON."""


class AcpModel(BaseModel):
    """Base of the ACP models: camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def wire(self) -> dict[str, Any]:
        """Serialize the model for the wire.

        Returns:
            The model as a dict with camelCase keys and without unset optional fields.
        """
        return self.model_dump(by_alias=True, exclude_none=True)


# ------------------------------------------------------------------ what the client sends


class FsCapabilities(AcpModel):
    """The client's file system capabilities. calu leaves both unused (see the module docstring)."""

    read_text_file: bool = False
    write_text_file: bool = False


class ConfigOptionCapabilities(AcpModel):
    """Which kinds of session config option the client understands.

    ACP marks support by the *presence* of an empty object, not by a flag, so `boolean` is None
    when the client said nothing and `{}` when it advertised support. An agent must not send a
    boolean option to a client that did not advertise it.
    """

    boolean: dict[str, Any] | None = None


class SessionCapabilities(AcpModel):
    """The client's session capabilities; calu reads only the config options."""

    config_options: ConfigOptionCapabilities | None = None


class ClientCapabilities(AcpModel):
    """What the client offers the agent. calu uses none of it."""

    fs: FsCapabilities = Field(default_factory=FsCapabilities)
    terminal: bool = False
    session: SessionCapabilities | None = None

    @property
    def boolean_config_options(self) -> bool:
        """Whether the client advertised boolean session config options.

        Returns:
            True if `session.configOptions.boolean` is present, so that calu may offer its
            auto-escalation switches as one boolean option each instead of only as a mode.
        """
        return (
            self.session is not None
            and self.session.config_options is not None
            and (self.session.config_options.boolean is not None)
        )


class InitializeParams(AcpModel):
    """Params of `initialize`: the client's protocol version and capabilities."""

    protocol_version: int = PROTOCOL_VERSION
    client_capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)
    client_info: dict[str, Any] | None = None


class NewSessionParams(AcpModel):
    """Params of `session/new`.

    `cwd` is the absolute directory the editor has open; calu checks it against the workspace of
    the session it is attached to. `mcp_servers` is required by the protocol and ignored here.
    """

    cwd: str
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    additional_directories: list[str] = Field(default_factory=list)


class LoadSessionParams(NewSessionParams):
    """Params of `session/load`: like `session/new`, plus the session to load."""

    session_id: str


class PromptParams(AcpModel):
    """Params of `session/prompt`: the session and the user's message as content blocks.

    The blocks stay untyped dicts, because the protocol has five variants and more may come;
    `prompt_text` turns them into the text calu sends to Claude.
    """

    session_id: str
    prompt: list[dict[str, Any]] = Field(default_factory=list)


class SetModeParams(AcpModel):
    """Params of `session/set_mode`: the session and the mode the client picked."""

    session_id: str
    mode_id: str


class SetConfigOptionParams(AcpModel):
    """Params of `session/set_config_option`: which option the client set, and to what.

    The request also carries `type` for a boolean option; calu ignores it, because the Python
    type of `value` already says which option kind arrived.
    """

    session_id: str
    config_id: str
    value: bool | str


class SessionIdParams(AcpModel):
    """Params of the methods that only name a session (`session/cancel`, `session/close`)."""

    session_id: str


class AuthenticateParams(AcpModel):
    """Params of `authenticate`: which advertised method the client chose."""

    method_id: str = ""


# ------------------------------------------------------------------ what the agent answers


def initialize_result(name: str, version: str) -> dict[str, Any]:
    """Build the result of `initialize`.

    `loadSession` is advertised, because calu can replay a conversation from the database
    (`session.history`). The prompt capabilities stay at their defaults, so the client sends
    only the baseline `text` and `resource_link` blocks, plus `resource` for embedded context.

    Args:
        name: Name of this agent, as the client shows it.
        version: Version of the adapter.

    Returns:
        The `initialize` result, ready to send.
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            "mcpCapabilities": {"http": False, "sse": False},
        },
        "agentInfo": {"name": name, "version": version},
        "authMethods": [],
    }


def prompt_text(blocks: list[dict[str, Any]]) -> str:
    """Turn the content blocks of `session/prompt` into the text calu sends to Claude.

    Understood: "text" (its text), "resource" (the embedded text, with its uri as a heading) and
    "resource_link" (the uri). An "image" or "audio" block is named but not passed on, because
    the adapter does not advertise those capabilities; an unknown type is named by its type, so
    that Claude is told something arrived rather than silently losing it.

    Args:
        blocks: The `prompt` array as the client sent it.

    Returns:
        The blocks as one text, parts separated by blank lines ("" if nothing was readable).
    """
    parts: list[str] = []
    for block in blocks:
        kind = str(block.get("type", ""))
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            resource = block.get("resource") or {}
            uri = str(resource.get("uri", "")) if isinstance(resource, dict) else ""
            text = str(resource.get("text", "")) if isinstance(resource, dict) else ""
            parts.append(f"{uri}:\n\n{text}" if text else uri)
        elif kind == "resource_link":
            parts.append(str(block.get("uri", "")))
        else:
            parts.append(f"[a {kind or 'nameless'} content block, which calu does not accept]")
    return "\n\n".join(part for part in parts if part)


def text_block(text: str) -> dict[str, Any]:
    """Build a "text" content block.

    Args:
        text: The text.

    Returns:
        The content block.
    """
    return {"type": "text", "text": text}


def message_chunk(
    kind: Literal["agent_message_chunk", "agent_thought_chunk", "user_message_chunk"],
    text: str,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build a message chunk update (`session/update`).

    Args:
        kind: Which chunk variant: the agent's answer, its thinking, or a user message.
        text: The text of this chunk.
        message_id: Id that ties chunks of one message together; omitted if None.

    Returns:
        The `update` object of a `session/update` notification.
    """
    update: dict[str, Any] = {"sessionUpdate": kind, "content": text_block(text)}
    if message_id:
        update["messageId"] = message_id
    return update


def tool_call(
    tool_call_id: str,
    title: str,
    tool_name: str,
    status: ToolCallStatus = "pending",
    raw_input: dict[str, Any] | None = None,
    locations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a `tool_call` update, which announces a tool call to the client.

    Args:
        tool_call_id: Claude's id of the call; the client uses it to match later updates.
        title: One line the client shows for the call.
        tool_name: Claude Code's tool name, mapped to an ACP kind through `TOOL_KINDS`.
        status: Life-cycle state, "pending" while the checkers still decide.
        raw_input: The tool input, for a client that wants to show the details.
        locations: Absolute paths the call refers to, so the editor can follow along.

    Returns:
        The `update` object of a `session/update` notification.
    """
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": title,
        "name": tool_name,
        "kind": TOOL_KINDS.get(tool_name, "other"),
        "status": status,
    }
    if raw_input is not None:
        update["rawInput"] = raw_input
    if locations:
        update["locations"] = [{"path": path} for path in locations]
    return update


def tool_call_update(
    tool_call_id: str,
    *,
    title: str | None = None,
    status: ToolCallStatus | None = None,
    content: list[dict[str, Any]] | None = None,
    raw_output: Any | None = None,
) -> dict[str, Any]:
    """Build a `tool_call_update`, which changes a tool call the client already knows.

    Fields that are None are left out, and the client keeps its current value for them.

    Args:
        tool_call_id: The call to update.
        title: New title, e.g. the checker's summary of what the step does.
        status: New life-cycle state.
        content: Replaces the call's content (text blocks, diffs).
        raw_output: The tool's result, for clients that show it.

    Returns:
        The `update` object of a `session/update` notification.
    """
    update: dict[str, Any] = {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id}
    if title is not None:
        update["title"] = title
    if status is not None:
        update["status"] = status
    if content is not None:
        update["content"] = content
    if raw_output is not None:
        update["rawOutput"] = raw_output
    return update


def text_content(text: str) -> dict[str, Any]:
    """Wrap text as tool call content.

    Args:
        text: The text.

    Returns:
        A "content" entry for a tool call's `content` list.
    """
    return {"type": "content", "content": text_block(text)}


def diff_content(path: str, before: str, after: str) -> dict[str, Any]:
    """Build the diff content of an edit, which clients show as a diff view.

    Args:
        path: Absolute path of the edited file (the protocol requires an absolute path).
        before: The file's content before the edit; "" is sent as null for a new file.
        after: The file's content after the edit.

    Returns:
        A "diff" entry for a tool call's `content` list.
    """
    return {"type": "diff", "path": path, "oldText": before or None, "newText": after}


def permission_options() -> list[dict[str, Any]]:
    """Build the four options of a `session/request_permission` request.

    calu offers all four kinds. "Always" is answered like "once" and recorded in the note of the
    decision, because turning it into a rule needs the admin credential that a client does not
    have (`calu rules add`, §6.2); the protocol leaves the scope of "always" to the agent.

    Returns:
        The `options` array, in the order a client should show it.
    """
    return [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "allow-always", "name": "Allow (remember)", "kind": "allow_always"},
        {"optionId": "reject-once", "name": "Deny", "kind": "reject_once"},
        {"optionId": "reject-always", "name": "Deny (remember)", "kind": "reject_always"},
    ]
