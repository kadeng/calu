/**
 * The transcript as rows: what each item of the view state shows and what a click on it opens
 * (spec section 11, "direct manipulation"). Pure; a chat surface (native Swing now, an embedded
 * browser later) only draws `RowModel`s and sends their actions.
 *
 *     ViewState.items[tab] ──rowsOf──► [RowModel]      one per item, same order, same id
 *
 *     item kind     row shows                                       click opens
 *     user          the message (who wrote it, images counted)      -
 *     assistant     Markdown                                        -
 *     thinking      "Thinking", the text when expanded              expands
 *     tool (edit)   tool name, file name (link), decision chip      the diff at the changed line; link: the file
 *     tool (Read)   tool name, file name                            the file at the line read
 *     tool (other)  tool name, command or input summary, chip       expands command and output
 *     checker       checker name, decision, remark                  the checker's instructions
 *     escalation    a card (drawn by the surface from `escalation`)  Allow / Deny / Why?
 *     launch        the subagent's description                      the subagent's tab
 *     turn          how the turn ended (`turnLabel`), its cost and the session's (`turnCost`)   -
 *     notice, relay, divider   one line                             relay: the subagent's tab
 */
package io.github.kadeng.calu.chat

import io.github.kadeng.calu.json.CaluJsonPretty
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.neutraliseBidi
import io.github.kadeng.calu.logic.showsErrorChip
import io.github.kadeng.calu.logic.turnCost
import io.github.kadeng.calu.logic.turnLabel
import io.github.kadeng.calu.state.AssistantItem
import io.github.kadeng.calu.state.CheckerItem
import io.github.kadeng.calu.state.DividerItem
import io.github.kadeng.calu.state.EscalationItem
import io.github.kadeng.calu.state.EscalationView
import io.github.kadeng.calu.state.Item
import io.github.kadeng.calu.state.LaunchItem
import io.github.kadeng.calu.state.NoticeItem
import io.github.kadeng.calu.state.RelayItem
import io.github.kadeng.calu.state.ThinkingItem
import io.github.kadeng.calu.state.ToolItem
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.TurnItem
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.UserSource
import io.github.kadeng.calu.state.ViewState
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Tools whose row opens a diff. */
val EDIT_TOOLS: Set<String> = setOf("Edit", "Write", "MultiEdit", "NotebookEdit")

/**
 * Something a click does: an action of `CaluRemoteApi.act`, or "openTab" (handled by the surface).
 *
 * @property name The action's name.
 * @property args Its arguments.
 */
data class RowAction(val name: String, val args: JsonObject)

/**
 * One row of the transcript.
 *
 * @property id The item's id (unique in the state; a surface keeps one component per id).
 * @property kind The item's kind (`ItemKind`).
 * @property title The row's first words: tool name, checker name, "You", ... ; "" for none.
 * @property text The row's main text.
 * @property markdown True if `text` is Markdown, false for plain text.
 * @property chip A decision to show as a chip ("ALLOW", "DENY", "ESCALATE", "PASS"); "" for none.
 * @property chipKind "ok", "error", "warning" or "" (how to colour the chip).
 * @property detail Plain text shown when the row is expanded; "" if the row does not expand.
 * @property action What a click on the row does; `null` to expand (if `detail`) or nothing.
 * @property link What a click on `linkText` does; `null` for no link.
 * @property linkText The link's text (a file name); "" for none.
 * @property tooltip Names the row's action, e.g. "Open diff".
 * @property streaming True while the text still grows.
 * @property dim True for rows drawn less prominently (recorded history, system messages).
 * @property escalation The escalation of an "escalation" row; `null` otherwise.
 * @property failed True for a tool call that ended with an error which the decision chip does
 *   not explain already (`showsErrorChip`); drawn as a second chip.
 */
data class RowModel(
    val id: Int,
    val kind: String,
    val title: String = "",
    val text: String = "",
    val markdown: Boolean = false,
    val chip: String = "",
    val chipKind: String = "",
    val detail: String = "",
    val action: RowAction? = null,
    val link: RowAction? = null,
    val linkText: String = "",
    val tooltip: String = "",
    val streaming: Boolean = false,
    val dim: Boolean = false,
    val escalation: EscalationView? = null,
    val failed: Boolean = false,
)

/**
 * Build the rows of one tab.
 *
 * @param state The view state.
 * @param tab The tab's key: "main" or a subagent's id.
 * @return One row per item of the tab, in order; empty for an unknown tab.
 */
fun rowsOf(state: ViewState, tab: String): List<RowModel> = state.items[tab].orEmpty().map { rowOf(state, it) }

/**
 * Colour class of a decision.
 *
 * @param decision "PASS", "ALLOW", "DENY", "ESCALATE" or another text.
 * @return "ok" for PASS and ALLOW, "error" for DENY, "warning" for ESCALATE, else "".
 */
fun chipKindOf(decision: String): String = when (decision) {
    "PASS", "ALLOW", "ACCEPT" -> "ok"
    "DENY" -> "error"
    "ESCALATE" -> "warning"
    else -> ""
}

/**
 * Build the row of one item.
 *
 * @param state The view state (for the tool call or escalation the item refers to).
 * @param item The item.
 * @return The row. Its texts (`title`, `text`, `detail`, `linkText`, `tooltip`) are passed
 *   through `neutraliseBidi`, so a row cannot be drawn in another order than it is stored in;
 *   the `escalation` of an escalation row is the state's own object and is not changed (the
 *   surface that draws the card calls `neutraliseBidi` on what it shows).
 */
fun rowOf(state: ViewState, item: Item): RowModel = rawRowOf(state, item).let { r ->
    r.copy(
        title = neutraliseBidi(r.title), text = neutraliseBidi(r.text), detail = neutraliseBidi(r.detail),
        linkText = neutraliseBidi(r.linkText), tooltip = neutraliseBidi(r.tooltip),
    )
}

/**
 * Build the row of one item with its texts as calu sent them.
 *
 * @param state The view state.
 * @param item The item.
 * @return The row, before `neutraliseBidi`.
 */
private fun rawRowOf(state: ViewState, item: Item): RowModel = when (item) {
    is UserItem -> RowModel(
        item.id, item.kind,
        title = when (item.source) {
            UserSource.USER -> "You"
            UserSource.CROSS_SESSION -> "Another session"
            else -> "System"
        },
        text = item.text + if (item.images.isNotEmpty()) "\n[${item.images.size} image${if (item.images.size == 1) "" else "s"}]" else "",
        dim = item.source != UserSource.USER || item.stored == true,
    )
    is AssistantItem -> RowModel(item.id, item.kind, text = item.text, markdown = true, streaming = item.streaming == true, dim = item.stored == true)
    is ThinkingItem -> RowModel(item.id, item.kind, title = "Thinking", detail = item.text, streaming = item.streaming == true, dim = true)
    is ToolItem -> state.tools[item.toolUseId]?.let { toolRow(item.id, it) } ?: RowModel(item.id, item.kind, title = "Tool call")
    is CheckerItem -> RowModel(
        item.id, item.kind, title = item.label, text = item.text, chip = item.decision, chipKind = chipKindOf(item.decision),
        action = item.checkerRoute.takeIf { it.isNotEmpty() }?.let { RowAction("editCheckerRoute", buildJsonObject { put("route", JsonPrimitive(it)) }) },
        tooltip = if (item.checkerRoute.isNotEmpty()) "Open this checker's instructions" else "",
    )
    is EscalationItem -> RowModel(item.id, item.kind, escalation = state.escalations[item.escalationKey])
    is TurnItem -> RowModel(
        item.id, item.kind,
        text = listOfNotNull(
            if (item.stored == true) "end of the turn" else turnLabel(item.subtype, item.isError),
            turnCost(item.costUsd, item.costDelta).takeIf { it.isNotEmpty() },
            item.backgroundTasks.takeIf { it > 0 }?.let { "$it background task${if (it == 1) "" else "s"} running" },
        ).joinToString(" · "),
        chipKind = if (item.isError) "error" else "",
        dim = true,
    )
    is LaunchItem -> RowModel(
        item.id, item.kind,
        title = "Subagent" + (item.subagentType?.let { " ($it)" } ?: "") + if (item.background) ", background" else "",
        text = item.description, detail = item.prompt,
        action = RowAction("openTab", buildJsonObject { put("tab", JsonPrimitive(item.toolUseId)) }),
        tooltip = "Open the subagent's tab",
    )
    is NoticeItem -> RowModel(item.id, item.kind, text = item.text, chipKind = if (item.level == "info") "" else item.level)
    is RelayItem -> RowModel(
        item.id, item.kind, text = "Message sent to a subagent", dim = true,
        action = RowAction("openTab", buildJsonObject { put("tab", JsonPrimitive(item.to)) }), tooltip = "Open the subagent's tab",
    )
    is DividerItem -> RowModel(item.id, item.kind, text = item.text, dim = true)
}

/**
 * Build the row of a tool call.
 *
 * @param id The item's id.
 * @param tool The tool call.
 * @return The row (see the table in the file comment).
 */
private fun toolRow(id: Int, tool: ToolView): RowModel {
    val path = tool.diffPath ?: tool.input.str("file_path") ?: tool.input.str("notebook_path") ?: tool.input.str("path")
    val check = tool.check
    val result = tool.result
    val chip = check?.decision.orEmpty()
    val why = listOfNotNull(
        check?.summary?.takeIf { it.isNotEmpty() },
        check?.reason?.takeIf { it.isNotEmpty() }?.let { "Reason: $it" },
        check?.comment?.takeIf { it.isNotEmpty() }?.let { "Remark: $it" },
    )
    val output = result?.let { (if (it.isError) "Failed:\n" else "Output:\n") + it.content }
    val base = RowModel(id, "tool", title = tool.name, chip = chip, chipKind = chipKindOf(chip), failed = showsErrorChip(tool), dim = tool.stored != null)
    val fileArgs = { line: Int? -> buildJsonObject { put("path", JsonPrimitive(path ?: "")); line?.let { put("line", JsonPrimitive(it)) } } }
    if (tool.name in EDIT_TOOLS && path != null) {
        val line = tool.editRange?.start
        val diff = tool.diffPath != null
        return base.copy(
            text = check?.summary.orEmpty(),
            detail = (why + listOfNotNull(output)).joinToString("\n\n"),
            linkText = path.substringAfterLast('/'),
            link = RowAction("openFile", fileArgs(line)),
            action = if (diff) RowAction("openDiff", buildJsonObject { put("tool_use_id", JsonPrimitive(tool.toolUseId)) }) else RowAction("openFile", fileArgs(line)),
            tooltip = if (diff) "Open diff" else "Open file" + (line?.let { " at line $it" } ?: "") + " (no diff is held for this edit)",
        )
    }
    if (tool.name == "Read" && path != null) {
        val line = tool.input.int("offset")
        return base.copy(
            text = check?.summary.orEmpty(), linkText = path.substringAfterLast('/'), link = RowAction("openFile", fileArgs(line)),
            action = RowAction("openFile", fileArgs(line)), tooltip = "Open file" + (line?.let { " at line $it" } ?: ""),
        )
    }
    val command = tool.input.str("command")
    val summary = command ?: tool.input.str("description") ?: tool.input.str("pattern") ?: tool.input.str("query") ?: tool.input.str("url") ?: ""
    val input = command ?: if (tool.input.isEmpty()) "" else CaluJsonPretty.encodeToString(JsonObject.serializer(), tool.input)
    return base.copy(
        // The row names what runs (the command); what the checker said about it is in the detail.
        text = summary.lineSequence().firstOrNull().orEmpty(),
        detail = (listOf(input).filter { it.isNotEmpty() } + why + listOfNotNull(output)).joinToString("\n\n"),
        tooltip = "Show the command and its output",
    )
}
