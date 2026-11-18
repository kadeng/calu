/**
 * Markdown texts of the two read-only documents: the checker context (`context.get`) and the
 * context of one escalation (`escalation.context`, "Why?"). Pure. Ported from
 * `extensions/vscode/src/shared/contextText.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJsonPretty
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.asStr
import io.github.kadeng.calu.json.long
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.objOrEmpty
import io.github.kadeng.calu.json.str
import kotlinx.serialization.json.JsonObject

/**
 * Session events after which the checker context may have changed: a view that shows the
 * document of `formatCheckerContext` loads `context.get` again when one of them arrives.
 */
val CONTEXT_EVENTS: Set<String> = setOf("check.result", "turn.result", "user.input", "session.state")

private val FENCE = Regex("^(`{3,}|~{3,})")
private val HEADING = Regex("^#{1,5} ")
private val TICKS = Regex("`+")

/**
 * Put every Markdown heading outside code fences one level lower, so that a quoted text fits
 * under the document's own headings.
 *
 * @param text Markdown.
 * @return The text with "#" put in front of each heading line of level 1 to 5 outside fences.
 */
fun demoteHeadings(text: String): String {
    var fence: String? = null
    return text.split("\n").joinToString("\n") { line ->
        val marker = FENCE.find(line)?.groupValues?.get(1)
        val open = fence
        if (marker != null && (open == null || marker.startsWith(open))) {
            fence = if (open != null) null else marker
            line
        } else if (fence == null && HEADING.containsMatchIn(line)) {
            "#$line"
        } else {
            line
        }
    }
}

/**
 * Put a text into a code fence that the text cannot close.
 *
 * @param text Any text.
 * @param language The fence's language tag; "" for none.
 * @return The fenced text; the fence has one backtick more than the longest run in the text (at least three).
 */
fun fenced(text: String, language: String = ""): String {
    val longest = maxOf(2, TICKS.findAll(text).maxOfOrNull { it.value.length } ?: 0)
    val fence = "`".repeat(longest + 1)
    return "$fence$language\n$text\n$fence"
}

/**
 * Build the checker context document.
 *
 * @param context The result of `context.get` (untrusted).
 * @param loadedAt The time of loading as text, e.g. "14:03:10".
 * @return Markdown with the folded summary and the recent turns as the checkers see them.
 */
fun formatCheckerContext(context: JsonObject, loadedAt: String): String {
    val summary = context.str("chat_summary")?.trim().orEmpty()
    val recent = context.str("recent")?.trim().orEmpty()
    return listOf(
        "# Checker context", "",
        "The conversation as the checkers see it, loaded $loadedAt. Read-only. Not shown: the checker",
        "instructions and the step being checked.", "",
        "# Folded summary", "", if (summary.isNotEmpty()) demoteHeadings(summary) else "_Nothing folded yet._", "",
        "# Recent turns", "", if (recent.isNotEmpty()) demoteHeadings(recent) else "_No turns yet._", "",
    ).joinToString("\n")
}

/**
 * Build the document of one escalation: what the checker knew and said when the step was checked.
 *
 * @param context The result of `escalation.context` (untrusted).
 * @return Markdown: state, reason, the step, the checker runs, the files before the step, and
 *   the summary and recent turns of that moment.
 */
fun formatEscalationContext(context: JsonObject): String {
    val lines = ArrayList<String>()
    lines += "# Escalation ${context.str("escalation_id").orEmpty()}".trimEnd()
    lines += ""
    val by = context.str("resolved_by")?.takeIf { it.isNotEmpty() }?.let { " by $it" }.orEmpty()
    lines += "- **State:** ${context.str("state") ?: "unknown"}$by"
    context.str("tool_name")?.takeIf { it.isNotEmpty() }?.let { lines += "- **Tool:** $it" }
    context.str("agent_id")?.takeIf { it.isNotEmpty() }?.let { lines += "- **Subagent:** $it" }
    context.str("note")?.takeIf { it.isNotEmpty() }?.let { lines += "- **Note:** $it" }
    lines += listOf("", "# Reason", "", context.str("reason")?.trim()?.takeIf { it.isNotEmpty() } ?: "_No reason was stored._", "", "# Step", "")
    context.str("rendered")?.trim()?.takeIf { it.isNotEmpty() }?.let { lines += demoteHeadings(it); lines += "" }
    lines += fenced(CaluJsonPretty.encodeToString(JsonObject.serializer(), context.objOrEmpty("tool_input")), "json")
    lines += listOf("", "# Checker runs", "")
    val runs = context.arr("checker_runs").mapNotNull { it.asObj() }
    if (runs.isEmpty()) lines += listOf("_No checker ran (a rule or a setting escalated the step)._", "")
    for (run in runs) {
        val tokens = listOfNotNull(
            run.long("input_tokens")?.let { "$it in" }, run.long("cache_read_tokens")?.let { "$it cached" }, run.long("output_tokens")?.let { "$it out" },
        ).joinToString(", ")
        val facts = listOf(run.str("model").orEmpty(), tokens, run.long("duration_ms")?.let { "$it ms" }.orEmpty()).filter { it.isNotEmpty() }
        lines += listOf("## ${run.str("checker_name").orEmpty()} (version ${run.long("checker_version") ?: "?"}): ${run.str("decision").orEmpty()}", "", facts.joinToString(" · "), "")
        run.str("reason")?.trim()?.takeIf { it.isNotEmpty() }?.let { lines += it; lines += "" }
    }
    val files = context.obj("files")?.entries.orEmpty()
    if (files.isNotEmpty()) {
        lines += listOf("# Files before the step", "")
        for ((path, text) in files) lines += listOf("## $path", "", fenced(text.asStr().orEmpty()), "")
    }
    lines += listOf("# Folded summary at that moment", "", context.str("chat_summary")?.trim()?.takeIf { it.isNotEmpty() }?.let(::demoteHeadings) ?: "_Nothing was folded._", "")
    lines += listOf("# Recent turns at that moment", "", context.str("recent")?.trim()?.takeIf { it.isNotEmpty() }?.let(::demoteHeadings) ?: "_No turns._", "")
    return lines.joinToString("\n")
}
