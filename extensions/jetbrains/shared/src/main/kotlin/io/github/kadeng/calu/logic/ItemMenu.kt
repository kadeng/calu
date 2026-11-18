/**
 * Pure model of the transcript's items and their context menus.
 *
 *     right-click on a tool row     -> menuFor(tool)                -> info rows + actions
 *     action chosen                 -> resolveItemAction(tool, action, folder) -> what to do, or an error
 *     click on a tool row           -> rowClick(tool)               -> open diff / file / tab, or expand
 *     a user message                -> userMessageMenu(state, item) -> Rewind, Fork from here, Copy
 *
 * The host decides from its own view state; paths never come from the surface.
 *
 * Port of `extensions/vscode/src/shared/itemMenu.ts`; `userMessageMenu` is the port of the
 * rules the VSCode webview applies when it draws a user message (`renderUser` in
 * `webview/items.ts`).
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.EditRange
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.UserSource
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAttached

/** Tools that change files and come with an `edit.diff` (the same set as `chat.EDIT_TOOLS`). */
val FILE_EDIT_TOOLS: Set<String> = setOf("Edit", "Write", "MultiEdit", "NotebookEdit")

/** Tools that read one file (`file_path` / `notebook_path`). */
val READ_TOOLS: Set<String> = setOf("Read", "NotebookRead")

/** Actions of a tool row's context menu (`ItemMenuAction.id`). */
object ItemActionId {
    /** Open the file a read tool call read, at the first line read. */
    const val OPEN_FILE: String = "openFile"

    /** Open the edited file at the edit's first line. */
    const val OPEN_FILE_AT_EDIT: String = "openFileAtEdit"

    /** Show the file's difference between git HEAD and the working tree. */
    const val SHOW_UNCOMMITTED_CHANGES: String = "showUncommittedChanges"

    /** Show the before/after diff of this edit. */
    const val SHOW_EDIT: String = "showEdit"

    /** Copy the file's path. */
    const val COPY_PATH: String = "copyPath"

    /** Copy the command of a Bash call. */
    const val COPY_COMMAND: String = "copyCommand"
}

/**
 * One action of a context menu.
 *
 * @property id One of `ItemActionId`.
 * @property label The menu entry's text.
 */
data class ItemMenuAction(val id: String, val label: String)

/**
 * The context menu of one tool row.
 *
 * @property kind "edit", "read" or "command".
 * @property info Information rows (label, value), shown above the actions.
 * @property actions Actions in display order.
 */
data class ItemMenu(val kind: String, val info: List<Pair<String, String>>, val actions: List<ItemMenuAction>)

/**
 * Classify a tool call.
 *
 * @param name The tool's name.
 * @param hasDiff True if the call has a diff (`ToolView.diffPath` is set).
 * @return "edit" for file-changing tools (or any tool with a diff), "read" for tools that read
 *   one file, "command" for Bash, else "other".
 */
fun toolKind(name: String, hasDiff: Boolean = false): String = when {
    name in FILE_EDIT_TOOLS || hasDiff -> "edit"
    name in READ_TOOLS -> "read"
    name == "Bash" -> "command"
    else -> "other"
}

/**
 * Classify the tool call of a tool view (see the other overload).
 *
 * @param tool The tool view.
 * @return "edit", "read", "command" or "other".
 */
fun toolKind(tool: ToolView): String = toolKind(tool.name, tool.diffPath != null)

/**
 * Shorten a commit id for display.
 *
 * @param head A commit id, or `null`.
 * @return The first 8 characters, or "unknown" for `null` and "".
 */
fun shortHead(head: String?): String = if (head.isNullOrEmpty()) "unknown" else head.take(8)

/**
 * Describe an edit's line range.
 *
 * @param range The range from `edit.diff`, or `null`.
 * @return "12-15", "12", "deleted before line 12" (end = start - 1), or "unknown".
 */
fun describeRange(range: EditRange?): String = when {
    range == null -> "unknown"
    range.end < range.start -> "deleted before line ${range.start}"
    range.end == range.start -> "${range.start}"
    else -> "${range.start}-${range.end}"
}

/**
 * Return the file path of an edit or read: the `edit.diff` path, else the tool input's
 * `file_path` / `notebook_path`.
 *
 * @param tool The tool view.
 * @return The path as calu reported it, or `null`.
 */
fun editPath(tool: ToolView): String? =
    tool.diffPath?.takeIf { it.isNotEmpty() } ?: tool.input.str("file_path") ?: tool.input.str("notebook_path")

/**
 * Return the first line a read tool call read.
 *
 * @param tool The tool view.
 * @return The tool input's `offset` if it is a positive integer, else 1.
 */
fun readLine(tool: ToolView): Int = tool.input.int("offset")?.takeIf { it > 0 } ?: 1

/**
 * Build the context menu of a tool row.
 *
 * @param tool The tool view.
 * @return The menu for edits, reads and commands, or `null` for other tools.
 */
fun menuFor(tool: ToolView): ItemMenu? {
    val kind = toolKind(tool)
    val decision = tool.check?.decision ?: "not checked"
    return when (kind) {
        "edit" -> ItemMenu(
            kind,
            listOf(
                "File" to (editPath(tool) ?: "unknown"), "Tool" to tool.name, "Lines" to describeRange(tool.editRange),
                "Decision" to decision, "Git HEAD" to shortHead(tool.gitHead),
            ),
            listOfNotNull(
                ItemMenuAction(ItemActionId.OPEN_FILE_AT_EDIT, "Open file at edit"),
                ItemMenuAction(ItemActionId.SHOW_UNCOMMITTED_CHANGES, "Show uncommitted changes of this file"),
                ItemMenuAction(ItemActionId.SHOW_EDIT, "Show this edit").takeIf { tool.diffPath != null },
                ItemMenuAction(ItemActionId.COPY_PATH, "Copy path"),
            ),
        )
        "read" -> ItemMenu(
            kind,
            listOf("File" to (editPath(tool) ?: "unknown"), "Tool" to tool.name, "From line" to readLine(tool).toString(), "Decision" to decision),
            listOf(ItemMenuAction(ItemActionId.OPEN_FILE, "Open file"), ItemMenuAction(ItemActionId.COPY_PATH, "Copy path")),
        )
        "command" -> ItemMenu(
            kind,
            listOf("Command" to tool.input.str("command").orEmpty(), "Decision" to decision, "Git HEAD" to shortHead(tool.gitHead)),
            listOf(ItemMenuAction(ItemActionId.COPY_COMMAND, "Copy command")),
        )
        else -> null
    }
}

/**
 * Return the action that the clickable file name of a tool row runs.
 *
 * @param tool The tool view.
 * @return "openFileAtEdit" for edits, "openFile" for reads, `null` for other tools or a tool
 *   without a file path.
 */
fun fileLinkAction(tool: ToolView): String? {
    if (editPath(tool).isNullOrEmpty()) return null
    return when (toolKind(tool)) {
        "edit" -> ItemActionId.OPEN_FILE_AT_EDIT
        "read" -> ItemActionId.OPEN_FILE
        else -> null
    }
}

/** What the host does for a validated action. */
sealed interface ResolvedAction {
    /**
     * Open a project file, or its uncommitted changes, with the cursor on a line.
     *
     * @property action "openFileAtEdit" or "showUncommittedChanges".
     * @property path The path relative to the project directory, with "/" separators.
     * @property line The 1-based line.
     */
    data class OpenFile(val action: String, val path: String, val line: Int) : ResolvedAction

    /**
     * Show the before/after diff of an edit.
     *
     * @property toolUseId The edit's tool call.
     */
    data class ShowEdit(val toolUseId: String) : ResolvedAction

    /**
     * Copy a text to the clipboard.
     *
     * @property text The text.
     */
    data class Copy(val text: String) : ResolvedAction
}

/**
 * Validate a context-menu action against the host's state and turn it into a concrete task.
 *
 * Paths: the edit's path must be project-relative (as calu reports files inside the project
 * directory) or absolute inside `folder`; it must not contain "..". The line is the edit's
 * start line (1 if unknown), or for "openFile" the first line read.
 *
 * @param tool The tool view from the host's state (`null` if the id is unknown).
 * @param action The requested action (untrusted).
 * @param folder Absolute path of the project directory ("/" or "\" separators).
 * @return The task. "openFile" of a read is returned as `OpenFile("openFileAtEdit", ...)`.
 * @throws IllegalArgumentException if the tool is unknown, the action is not offered for it,
 *   or the path is outside the project directory; the message is a sentence for the user.
 */
fun resolveItemAction(tool: ToolView?, action: String?, folder: String): ResolvedAction {
    requireNotNull(tool) { "This tool call is no longer in the chat." }
    val menu = menuFor(tool)
    require(menu != null && menu.actions.any { it.id == action }) { "This action is not available here." }
    return when (action) {
        ItemActionId.COPY_COMMAND -> ResolvedAction.Copy(tool.input.str("command").orEmpty())
        ItemActionId.SHOW_EDIT -> ResolvedAction.ShowEdit(tool.toolUseId)
        else -> {
            val rel = workspaceRelative(editPath(tool).orEmpty(), folder)
            when (action) {
                ItemActionId.COPY_PATH -> ResolvedAction.Copy(rel)
                ItemActionId.OPEN_FILE -> ResolvedAction.OpenFile(ItemActionId.OPEN_FILE_AT_EDIT, rel, readLine(tool))
                else -> ResolvedAction.OpenFile(action.orEmpty(), rel, tool.editRange?.start ?: 1)
            }
        }
    }
}

private val DRIVE_PATH = Regex("^[a-zA-Z]:/")
private val TRAILING_SLASHES = Regex("/+$")

/**
 * Convert a path from calu into a path relative to the project directory.
 *
 * @param path Project-relative, or absolute inside `folder`.
 * @param folder Absolute path of the project directory.
 * @return The relative path with "/" separators.
 * @throws IllegalArgumentException if the path is empty, outside `folder`, or contains "..".
 */
fun workspaceRelative(path: String, folder: String): String {
    var p = path.replace('\\', '/')
    val root = folder.replace('\\', '/').replace(TRAILING_SLASHES, "")
    if (p.startsWith("/") || DRIVE_PATH.containsMatchIn(p)) {
        // Windows drive letters differ in case between the IDE ("c:") and Claude ("C:").
        val windows = DRIVE_PATH.containsMatchIn(root)
        val inside = if (windows) p.lowercase().startsWith(root.lowercase() + "/") else p.startsWith("$root/")
        require(inside) { "The file is outside the workspace: $path" }
        p = p.substring(root.length + 1)
    }
    while (p.startsWith("./")) p = p.substring(2)
    require(p.isNotEmpty() && ".." !in p.split("/")) { "The file is outside the workspace: $path" }
    return p
}

// ------------------------------------------------------------------ what a click on a row does

/** What a click (or Enter) on a tool row does (spec section 11). `title` is the row's tooltip. */
sealed interface RowClick {
    /** The row's tooltip. */
    val title: String

    /**
     * Open the diff of this edit.
     *
     * @property title The tooltip.
     */
    data class OpenDiff(override val title: String) : RowClick

    /**
     * Open the file at a line, through the context-menu action `action`.
     *
     * @property action "openFileAtEdit" or "openFile" (`ItemActionId`).
     * @property line The 1-based line.
     * @property title The tooltip.
     */
    data class OpenFile(val action: String, val line: Int, override val title: String) : RowClick

    /**
     * Open the tab of the subagent that this call launched.
     *
     * @property tab The tab's key.
     * @property title The tooltip.
     */
    data class OpenTab(val tab: String, override val title: String) : RowClick

    /**
     * Show or hide the row's details in place.
     *
     * @property title The tooltip.
     */
    data class Expand(override val title: String) : RowClick
}

/**
 * Decide what a click on the file name inside a tool row does.
 *
 * @param tool The tool view.
 * @return The context-menu action that opens the file, the 1-based line the cursor is put on
 *   (the edit's first changed line, or the first line read; 1 if unknown) and the tooltip, e.g.
 *   "Open file at line 42"; `null` for tools without one file (see `fileLinkAction`).
 */
fun fileClick(tool: ToolView): RowClick.OpenFile? {
    val action = fileLinkAction(tool) ?: return null
    val line = if (action == ItemActionId.OPEN_FILE) readLine(tool) else tool.editRange?.start ?: 1
    return RowClick.OpenFile(action, line, "Open file at line $line")
}

/**
 * Decide what a click on the row of a tool call does.
 *
 *     edit with a recorded diff   -> OpenDiff      "Open diff"
 *     edit without a diff         -> OpenFile      "Open file at line N (no diff was recorded)"
 *     Read                        -> OpenFile      "Open file at line N"
 *     Agent / Task                -> OpenTab       "Open the subagent's tab"
 *     Bash and every other tool   -> Expand        "Show the command and its output" / "Show details"
 *
 * @param tool The tool view.
 * @return The action with its tooltip. The host may still find that it no longer holds the
 *   diff of an old edit; it then opens the file instead and says so.
 */
fun rowClick(tool: ToolView): RowClick {
    if (tool.name == "Agent" || tool.name == "Task") return RowClick.OpenTab(tool.toolUseId, "Open the subagent's tab")
    val kind = toolKind(tool)
    val file = fileClick(tool)
    if (kind == "edit" && tool.diffPath != null) return RowClick.OpenDiff("Open diff")
    if (kind == "edit" && file != null) return file.copy(title = "${file.title} (no diff was recorded)")
    if (kind == "read" && file != null) return file
    return RowClick.Expand(if (kind == "command") "Show the command and its output" else "Show details")
}

/**
 * Find the first line in which two texts differ (for revealing a diff at its first change).
 *
 * @param before The text before the edit.
 * @param after The text after the edit.
 * @return The 1-based number of the first line that differs or exists in only one text; 1 if
 *   the texts are equal.
 */
fun firstChangedLine(before: String, after: String): Int {
    val a = before.split("\n")
    val b = after.split("\n")
    val shared = minOf(a.size, b.size)
    for (i in 0 until shared) if (a[i] != b[i]) return i + 1
    return if (a.size == b.size) 1 else shared + 1
}

// ------------------------------------------------------------------ user messages

/** Title of the confirmation that "Rewind" asks before it starts the new session. */
const val REWIND_CONFIRM_TITLE: String = "Rewind to before this message?"

/** Text of the confirmation that "Rewind" asks. */
const val REWIND_CONFIRM_DETAIL: String =
    "A new session starts that continues the conversation from before that turn. Files are not " +
        "reverted: what Claude changed since then stays on disk. The current session keeps running; " +
        "you can switch back to it in the session menu."

/** Why the first message of a conversation offers no rewind or fork. */
const val FIRST_MESSAGE_NOTE: String =
    "This is the first message of the conversation: nothing is before it. Start a new session instead."

/** The line under a message the user sent to a subagent. */
const val RELAYED_MESSAGE_NOTE: String = "Sent to this subagent; the main agent relays it."

/**
 * One action of a user message.
 *
 * @property id "rewind" (`act("rewind", {turn, uuid?})` after a confirmation), "forkAt"
 *   (`act("forkAt", {turn, uuid?})`) or "copy" (the surface copies the message's text).
 * @property label The action's short text.
 * @property title The tooltip; for a disabled action it says why.
 * @property enabled False if the action cannot run now.
 */
data class UserMessageAction(val id: String, val label: String, val title: String, val enabled: Boolean)

/**
 * What a user message offers.
 *
 * @property actions The actions in display order. A message that is not the user's own (another
 *   session's, calu's) and a message sent to a subagent have only "copy".
 * @property note A line shown under the message instead of rewind and fork; `null` for none.
 * @property turn The turn to pass to `rewind` / `forkAt` (`UserItem.turn`).
 * @property uuid Claude Code's id of the message to pass along; `null` if unknown.
 */
data class UserMessageMenu(val actions: List<UserMessageAction>, val note: String?, val turn: Int, val uuid: String?)

/**
 * Decide what a user message of the transcript offers.
 *
 *     the user's own message in Main     Rewind · Fork from here · Copy
 *       no session attached              Rewind and Fork disabled
 *       first message of the conversation (turnBase known, turn <= 1)   disabled, the tooltip says why
 *     a message sent to a subagent       Copy, and the note that the main agent relays it
 *     another session's / a system message   Copy
 *
 * "Copy" is the plugin's own addition; the VSCode extension has no such button.
 *
 * @param state The view state.
 * @param item The user message.
 * @return The menu.
 */
fun userMessageMenu(state: ViewState, item: UserItem): UserMessageMenu {
    val copy = UserMessageAction("copy", "Copy", "Copy the message's text", item.text.isNotEmpty())
    if (item.source != UserSource.USER) return UserMessageMenu(listOf(copy), null, item.turn, item.uuid)
    // A message for a subagent is relayed in a turn of the main agent: nothing to rewind to here.
    if (item.to != null) return UserMessageMenu(listOf(copy), RELAYED_MESSAGE_NOTE, item.turn, item.uuid)
    // The first message of the conversation has nothing before it to go back to.
    val first = state.turnBase != null && item.turn <= 1
    val enabled = isAttached(state) && !first
    val rewind = "Rewind to before this message: a new session continues from before this turn (files are not reverted)"
    val fork = "Fork from here: a new session continues from before this turn; this one keeps running"
    return UserMessageMenu(
        listOf(
            UserMessageAction("rewind", "Rewind to before this message", if (first) FIRST_MESSAGE_NOTE else rewind, enabled),
            UserMessageAction("forkAt", "Fork from here", if (first) FIRST_MESSAGE_NOTE else fork, enabled),
            copy,
        ),
        null, item.turn, item.uuid,
    )
}
