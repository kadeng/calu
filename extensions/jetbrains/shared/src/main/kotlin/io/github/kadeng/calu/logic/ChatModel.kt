/**
 * Pure helpers of a chat surface: everything a view decides before it builds components.
 *
 *     ViewState ──► tabList()          Main + one tab per subagent, with attention marks
 *               ──► usageMeter()       "42% · 84k" for the composer footer
 *               ──► otherOpenEscalations()  the strip above the input
 *     texts     ──► middleTruncate(), shortDirectory(), splitPath(), previewLines(), toolHead(),
 *                   splitByFiles(), turnLabel(), turnCost(), usageCostLine()
 *     links     ──► linkTarget()       whether a click on a Markdown link opens something
 *               ──► buildFileIndex() / lookupFile()   paths in Claude's text that tool calls named
 *
 * The session dropdown is in `SessionMenu.kt`, the setup cards are in `SetupCards.kt`, the image
 * checks in `Images.kt`.
 *
 * Port of `extensions/vscode/src/shared/chatModel.ts`. Where the original returns a webview
 * message, the port returns the message's `type` as an action name (see `CardAction`).
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.asArr
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.Item
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.RateLimit
import io.github.kadeng.calu.state.ToolFile
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.UsageView
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.own
import kotlinx.serialization.json.JsonObject
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale

// ------------------------------------------------------------------ texts

/**
 * Shorten a text in the middle, so that its start and its end stay readable (file paths).
 *
 * Example: `middleTruncate("/home/me/project/src/module/file.ts", 20) == "/home/me/…le/file.ts"`.
 *
 * @param text Any text.
 * @param max Maximum length of the result (values below 5 count as 5).
 * @return `text` if it fits, else its start, "…" and its end; the end gets the larger half.
 */
fun middleTruncate(text: String, max: Int): String {
    val limit = maxOf(5, max)
    if (text.length <= limit) return text
    val keep = limit - 1
    val head = keep / 2
    return "${text.substring(0, head)}…${text.substring(text.length - (keep - head))}"
}

private val HOME_PREFIX = Regex("^/(?:home|Users)/[^/]+(?=/|$)")

/**
 * Shorten a directory for a list: the home directory becomes "~", the rest is cut in the middle.
 *
 * @param directory An absolute path ("" allowed).
 * @param max Maximum length.
 * @return The short form, e.g. "~/projects/…/calu".
 */
fun shortDirectory(directory: String, max: Int = 40): String = middleTruncate(HOME_PREFIX.replaceFirst(directory, "~"), max)

/**
 * A path split into directory and file name.
 *
 * @property dir The directory including the trailing separator; "" if the path has none.
 * @property base The file name.
 */
data class PathParts(val dir: String, val base: String)

/**
 * Split a path into its directory and its file name, so that a narrow row can cut the directory
 * and still show the name.
 *
 * Example: `splitPath("src/app/main.py") == PathParts("src/app/", "main.py")`.
 *
 * @param path A path with "/" or "\" separators.
 * @return The two parts; joined they give `path`.
 */
fun splitPath(path: String): PathParts {
    val at = maxOf(path.lastIndexOf('/'), path.lastIndexOf('\\'))
    return PathParts(path.substring(0, at + 1), path.substring(at + 1))
}

private val WHITESPACE_RUN = Regex("\\s+")

/**
 * Put a text on one line (the `oneLine` of `chatModel.ts`; `oneLine` of `SessionPick.kt` cuts
 * one character earlier).
 *
 * @param text Any text.
 * @param limit Maximum length before the "…".
 * @return The text with runs of whitespace replaced by one space and without whitespace at its
 *   ends, cut after `limit` characters with "…" appended.
 */
fun flatLine(text: String, limit: Int = 160): String {
    val flat = text.replace(WHITESPACE_RUN, " ").trim()
    return if (flat.length <= limit) flat else "${flat.substring(0, limit)}…"
}

/**
 * A shortened text and whether lines were left out.
 *
 * @property text The kept lines.
 * @property truncated True if lines were left out.
 * @property lines Number of lines of the whole text.
 */
data class Preview(val text: String, val truncated: Boolean, val lines: Int)

/**
 * Keep the first or the last `count` lines of a text.
 *
 * Example: `previewLines("a\nb\nc", 2, tail = true) == Preview("b\nc", true, 3)`.
 *
 * @param text Any text ("\n" line breaks; a trailing newline does not count as a line).
 * @param count Maximum number of lines to keep (values below 1 count as 1).
 * @param tail False keeps the first lines (finished texts), true the last lines (while
 *   streaming, so the newest output stays visible).
 * @return The kept lines, `truncated` true if lines were left out, and the line count. A text
 *   that fits is returned unchanged (with its trailing newline).
 */
fun previewLines(text: String, count: Int, tail: Boolean): Preview {
    val lines = text.removeSuffix("\n").split("\n")
    val n = maxOf(1, count)
    if (lines.size <= n) return Preview(text, false, lines.size)
    val kept = if (tail) lines.takeLast(n) else lines.take(n)
    return Preview(kept.joinToString("\n"), true, lines.size)
}

/**
 * A tool input in one line.
 *
 * @property text The text for display.
 * @property isPath True if the text is a file path (the caller then cuts it in the middle
 *   instead of at the end).
 */
data class ToolHead(val text: String, val isPath: Boolean)

/**
 * Describe a tool input in one line: the command of Bash, the file of an edit or read, the
 * pattern of a search, the first question of `AskUserQuestion`, else the input as JSON.
 *
 * @param input The tool input (`ToolView.input`).
 * @param diffPath `ToolView.diffPath`, or `null`.
 * @return The line; `text` is "" for an empty input without a diff path.
 */
fun toolHead(input: JsonObject, diffPath: String? = null): ToolHead {
    input.str("command")?.let { return ToolHead(flatLine(it, 300), false) }
    for (key in listOf("file_path", "notebook_path", "path")) input.str(key)?.let { return ToolHead(it, true) }
    if (!diffPath.isNullOrEmpty()) return ToolHead(diffPath, true)
    for (key in listOf("description", "pattern", "query", "url", "skill", "prompt")) input.str(key)?.let { return ToolHead(flatLine(it, 300), false) }
    input["questions"].asArr().firstOrNull().asObj()?.str("question")?.let { return ToolHead(flatLine(it, 300), false) }
    return ToolHead(if (input.isEmpty()) "" else flatLine(CaluJson.encodeToString(JsonObject.serializer(), input), 300), false)
}

/**
 * Describe the input of a tool view in one line (see the other overload).
 *
 * @param tool The tool view.
 * @return The line.
 */
fun toolHead(tool: ToolView): ToolHead = toolHead(tool.input, tool.diffPath)

/**
 * A piece of a label: plain text, or a file link.
 *
 * @property text The piece's text.
 * @property file Index into the tool call's files (`ToolView.files`) for a link; `null` for
 *   plain text.
 */
data class LabelPart(val text: String, val file: Int? = null)

/**
 * Split a label into text and file links: every occurrence of a file's `raw` text (e.g. a path
 * argument of a command) becomes a link part.
 *
 * @param text The label.
 * @param files The tool call's files (`ToolView.files`); `null` for none.
 * @return The parts in order; their texts joined give `text`. At the same position the longer
 *   `raw` wins.
 */
fun splitByFiles(text: String, files: List<ToolFile>?): List<LabelPart> {
    val sorted = files.orEmpty().withIndex().filter { it.value.raw.isNotEmpty() }.sortedByDescending { it.value.raw.length }
    val parts = ArrayList<LabelPart>()
    var pos = 0
    while (pos < text.length) {
        var bestAt = -1
        var best: IndexedValue<ToolFile>? = null
        for (f in sorted) {
            val at = text.indexOf(f.value.raw, pos)
            if (at >= 0 && (best == null || at < bestAt)) { bestAt = at; best = f }
        }
        val found = best ?: break
        if (bestAt > pos) parts.add(LabelPart(text.substring(pos, bestAt)))
        parts.add(LabelPart(found.value.raw, found.index))
        pos = bestAt + found.value.raw.length
    }
    if (pos < text.length) parts.add(LabelPart(text.substring(pos)))
    return parts
}

/**
 * Name the look of a checker decision chip.
 *
 * @param decision The `decision` of a `check.result` ("PASS", "ALLOW", "DENY", "ESCALATE"; any
 *   letter case), or `null` while the step is not checked.
 * @return "pass" (a rule let it through), "allow", "deny", "escalate" or "pending".
 */
fun decisionKind(decision: String?): String = when (decision.orEmpty().uppercase(Locale.ROOT)) {
    "PASS" -> "pass"
    "ALLOW" -> "allow"
    "DENY" -> "deny"
    "ESCALATE" -> "escalate"
    else -> "pending"
}

/**
 * Tell whether a tool row shows the "error" chip next to the checker's decision.
 *
 *     result is no error                         -> no
 *     AskUserQuestion                            -> no (its "error" result carries the user's answer)
 *     the checker denied the call (DENY)         -> no (the DENY chip says why the call did not run)
 *     every other failed call                    -> yes
 *
 * @param tool The tool view.
 * @return True if the chip is shown.
 */
fun showsErrorChip(tool: ToolView): Boolean {
    if (tool.result?.isError != true) return false
    if (tool.name == "AskUserQuestion") return false
    return decisionKind(tool.check?.decision) != "deny"
}

/**
 * Word the outcome of a turn for its separator line.
 *
 *     success                         -> "turn done"
 *     interrupted                     -> "turn interrupted"
 *     any other subtype, or an error  -> "turn ended: <subtype, underscores as spaces>"
 *
 * @param subtype The turn item's `subtype` (of `turn.result`).
 * @param isError The turn item's `isError`.
 * @return The text.
 */
fun turnLabel(subtype: String, isError: Boolean): String {
    if (subtype == "interrupted") return "turn interrupted"
    if (!isError && (subtype == "success" || subtype == "")) return "turn done"
    return "turn ended: ${subtype.ifEmpty { "error" }.replace('_', ' ')}"
}

/**
 * Format an amount of US dollars with four decimals.
 *
 * @param usd The amount.
 * @return e.g. "0.0050" (decimal point, no grouping).
 */
private fun usd4(usd: Double): String = String.format(Locale.ROOT, "%.4f", usd)

/**
 * Word the cost on a turn's separator line. Claude Code reports the cost of the whole session
 * at the end of every turn; the turn's own cost is the difference to the turn before.
 *
 *     total 0.0050, turn 0.0003  -> "$0.0003 · session $0.0050"
 *     total 0.0027, turn unknown -> "session $0.0027"
 *     total unknown              -> ""
 *
 * @param costUsd The session's cost when the turn ended (`TurnItem.costUsd`), or `null`.
 * @param costDelta The cost of this turn alone (`TurnItem.costDelta`), or `null` if not known.
 * @return The text, "" without a cost. Amounts have four decimals.
 */
fun turnCost(costUsd: Double?, costDelta: Double? = null): String {
    if (costUsd == null) return ""
    val session = "session $" + usd4(costUsd)
    return if (costDelta != null) "$" + usd4(costDelta) + " · " + session else session
}

/**
 * Word the session line of the usage popover.
 *
 *     (0.005, 2) -> "Cost $0.0050 (Claude Code, whole session) · last turn: 2 model calls"
 *     (null, 0)  -> "Cost unknown"
 *
 * The checkers' models are billed to their own keys and are not in this number.
 *
 * @param costUsd The session's cost as Claude Code reported it last, or `null`.
 * @param numTurns `num_turns` of the last `turn.result`: the model calls of that turn; 0 if no
 *   turn ended yet.
 * @return The line.
 */
fun usageCostLine(costUsd: Double?, numTurns: Int): String {
    val cost = if (costUsd != null) "Cost $" + usd4(costUsd) + " (Claude Code, whole session)" else "Cost unknown"
    return if (numTurns > 0) "$cost · last turn: $numTurns model call${if (numTurns == 1) "" else "s"}" else cost
}

// ------------------------------------------------------------------ tabs

/**
 * One tab of the tab bar.
 *
 * @property key `MAIN` or the subagent's id.
 * @property label The tab's text.
 * @property status "" for Main, else the subagent's status ("running", "completed", "failed",
 *   "stopped").
 * @property background True for a background subagent.
 * @property openEscalations Number of open escalations in the tab.
 * @property attention True if a hidden tab has an open escalation or items the user has not seen.
 */
data class TabView(
    val key: String,
    val label: String,
    val status: String,
    val background: Boolean,
    val openEscalations: Int,
    val attention: Boolean,
)

/**
 * Build the tab bar: Main and one tab per subagent.
 *
 * @param state The view state.
 * @param active Key of the visible tab.
 * @param seen Per tab the id of the newest item the user has seen (missing = nothing).
 * @return The tabs in display order. `attention` is never set for the active tab. The open
 *   escalations of a session that ended (`transcriptEnded`) are not counted, and its running
 *   subagents show as "stopped".
 */
fun tabList(state: ViewState, active: String, seen: Map<String, Int>): List<TabView> {
    val open = HashMap<String, Int>()
    val ended = transcriptEnded(state)
    for (e in state.escalations.values) if (!ended && e.status == EscalationStatus.OPEN) open[e.agentKey] = (open[e.agentKey] ?: 0) + 1
    fun tab(key: String, label: String, status: String, background: Boolean): TabView {
        val newest = own(state.items, key)?.lastOrNull()?.id ?: 0
        val escalations = open[key] ?: 0
        val unseen = newest > (seen[key] ?: 0)
        return TabView(key, label, status, background, escalations, key != active && (escalations > 0 || unseen))
    }
    return listOf(tab(MAIN, "Main", "", false)) + state.agents.map { a ->
        val label = a.description?.takeIf { it.isNotEmpty() } ?: a.subagentType?.takeIf { it.isNotEmpty() } ?: a.agentId.takeLast(6)
        val status = a.status ?: "running"
        tab(a.agentId, label, if (ended && status == "running") "stopped" else status, a.background == true)
    }
}

/**
 * Return the id of the newest item per tab (what "seen" is set to when everything counts as read).
 *
 * @param items `ViewState.items`.
 * @return Per tab key the id of its last item (tabs without items are left out).
 */
fun newestItemIds(items: Map<String, List<Item>>): Map<String, Int> =
    items.mapNotNull { (key, list) -> list.lastOrNull()?.let { key to it.id } }.toMap()

/**
 * An open escalation of a tab that is not visible, for the strip above the input.
 *
 * @property key The escalation's key in `ViewState.escalations`.
 * @property tab The tab that shows its card.
 * @property label e.g. "Main: Bash" or "tests: Edit".
 */
data class StripEntry(val key: String, val tab: String, val label: String)

/**
 * List the open escalations of the tabs that are not visible.
 *
 * @param state The view state.
 * @param active Key of the visible tab.
 * @return One entry per open escalation outside `active`, oldest first; none while the
 *   transcript is of a session that ended (`transcriptEnded`).
 */
fun otherOpenEscalations(state: ViewState, active: String): List<StripEntry> {
    fun name(tab: String): String =
        if (tab == MAIN) "Main" else state.agents.firstOrNull { it.agentId == tab }?.description?.takeIf { it.isNotEmpty() } ?: tab.takeLast(6)
    if (transcriptEnded(state)) return emptyList()
    return state.escalations.values
        .filter { it.status == EscalationStatus.OPEN && it.agentKey != active }
        .map { StripEntry(it.key, it.agentKey, "${name(it.agentKey)}: ${if (it.questions.isNotEmpty()) "question" else it.toolName}") }
}

// ------------------------------------------------------------------ usage

/**
 * Format a token count.
 *
 * @param tokens A number (negative values count as 0).
 * @return "950", "84k", "1.2M" (one decimal below 10 of a unit).
 */
fun formatTokens(tokens: Long): String {
    val n = maxOf(0L, tokens)
    if (n < 1000) return n.toString()
    val (value, unit) = if (n < 1_000_000) n / 1000.0 to "k" else n / 1_000_000.0 to "M"
    val text = if (value < 10) String.format(Locale.ROOT, "%.1f", value).removeSuffix(".0") else Math.round(value).toString()
    return text + unit
}

/**
 * What the context meter of the composer footer shows.
 *
 * @property percent 0..100, or `null` while the context is unknown.
 * @property label e.g. "42% · 84k"; "ctx –" while unknown.
 * @property title Tooltip, e.g. "Context: 84,000 of 200,000 tokens (42%). Click for limits and cost."
 * @property level "ok" below 70 %, "warn" below 90 %, else "high"; "unknown" without data.
 */
data class UsageMeter(val percent: Int?, val label: String, val title: String, val level: String)

/**
 * Summarise the context usage for the composer footer.
 *
 * @param usage `ViewState.usage`.
 * @param working True while Claude works on a turn: the number was read at the last turn end
 *   or at Claude Code's last usage report, and the tooltip says so.
 * @return The meter (see `UsageMeter`).
 */
fun usageMeter(usage: UsageView, working: Boolean = false): UsageMeter {
    val c = usage.context ?: return UsageMeter(null, "ctx –", "Context usage is not known yet. Click to ask.", "unknown")
    val percent = Math.round(c.percent).toInt().coerceIn(0, 100)
    val asOf = if (working) " As of the last turn or usage report; it is read again when the turn ends." else ""
    val tokens = String.format(Locale.US, "%,d", c.tokens)
    val max = String.format(Locale.US, "%,d", c.maxTokens)
    return UsageMeter(
        percent = percent,
        label = "$percent% · ${formatTokens(c.tokens.toLong())}",
        title = "Context: $tokens of $max tokens ($percent%).$asOf Click for limits and cost.",
        level = if (percent < 70) "ok" else if (percent < 90) "warn" else "high",
    )
}

private val RESET_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm", Locale.ROOT).withZone(ZoneOffset.UTC)

/**
 * Read an ISO 8601 time.
 *
 * @param text e.g. "2026-11-18T14:30:00Z" or with an offset.
 * @return The instant, or `null` if the text is not such a time.
 */
private fun parseInstant(text: String): Instant? =
    runCatching { Instant.parse(text) }.getOrNull() ?: runCatching { OffsetDateTime.parse(text).toInstant() }.getOrNull()

/**
 * Describe one account limit for the usage popover.
 *
 * @param limit A rate limit of `ViewState.usage.rateLimits`.
 * @return e.g. "five hour: 63% used, allowed, resets 2026-11-18 14:30 UTC"; parts that are
 *   unknown are left out, and "limit: no data" is returned for an empty limit.
 */
fun rateLimitLine(limit: RateLimit): String {
    val parts = ArrayList<String>()
    limit.utilization?.let { parts.add("${Math.round(if (it <= 1) it * 100 else it)}% used") }
    if (limit.status.isNotEmpty()) parts.add(limit.status.replace('_', ' '))
    limit.resetsAt?.takeIf { it.isNotEmpty() }?.let(::parseInstant)?.let { parts.add("resets ${RESET_FORMAT.format(it)} UTC") }
    return "${limit.type.ifEmpty { "limit" }.replace('_', ' ')}: ${parts.joinToString(", ").ifEmpty { "no data" }}"
}

// ------------------------------------------------------------------ links and files

private val HTTP_LINK = Regex("^https?://", RegexOption.IGNORE_CASE)
private val ANY_SCHEME = Regex("^[a-z][a-z0-9+.-]*:", RegexOption.IGNORE_CASE)

/**
 * Decide what a click on a link of rendered Markdown opens. Links never navigate by themselves.
 *
 * @param href The link's target (untrusted).
 * @return The trimmed target for http(s) and relative links (the host opens a browser or a
 *   project file); `null` for an empty link, a fragment ("#…") and every other scheme.
 */
fun linkTarget(href: String?): String? {
    val target = href.orEmpty().trim()
    if (target.isEmpty() || target.startsWith("#")) return null
    if (HTTP_LINK.containsMatchIn(target)) return target
    if (ANY_SCHEME.containsMatchIn(target) || target.startsWith("//")) return null
    return target
}

/**
 * Lookup of the files that tool calls named: by absolute path, by `raw` text and by base name.
 *
 * @property paths From each key to the absolute path; `null` for a key that stands for several
 *   files (ambiguous).
 */
class FileIndex internal constructor(internal val paths: Map<String, String?>)

/**
 * A file that a text names.
 *
 * @property path The absolute path.
 * @property line The 1-based line, or `null` if the text gave none.
 */
data class FileRef(val path: String, val line: Int? = null)

private val PATH_SEPARATOR = Regex("[\\\\/]")

/**
 * Index the files that the tool calls of the state named (`tool.files`), so that a path in
 * Claude's text can be turned into a link.
 *
 * @param tools `ViewState.tools`.
 * @return The index: the absolute path, the `raw` text and the base name of every file map to
 *   its absolute path; a base name or raw text that stands for several files is ambiguous.
 */
fun buildFileIndex(tools: Map<String, ToolView>): FileIndex {
    val index = HashMap<String, String?>()
    fun add(key: String, path: String) {
        if (key.isEmpty()) return
        index[key] = if (!index.containsKey(key) || index[key] == path) path else null
    }
    for (tool in tools.values) {
        for (f in tool.files.orEmpty()) {
            add(f.path, f.path)
            add(f.raw, f.path)
            add(f.path.split(PATH_SEPARATOR).last(), f.path)
        }
    }
    return FileIndex(index)
}

private val FILE_WITH_LINE = Regex("^(.+?)(?::(\\d+))?(?::\\d+)?$")

/**
 * Look up a text that may name a file, with an optional ":line" suffix.
 *
 * @param index The index of `buildFileIndex`.
 * @param text e.g. "src/app.py", "app.py:42" or "/work/src/app.py:42:7".
 * @return The absolute path and the 1-based line (if given), or `null` if the text names no
 *   known file or an ambiguous one.
 */
fun lookupFile(index: FileIndex, text: String): FileRef? {
    val m = FILE_WITH_LINE.find(text.trim()) ?: return null
    val name = m.groupValues[1]
    val path = index.paths[name] ?: index.paths[name.removePrefix("./")] ?: return null
    val line = m.groupValues[2].takeIf { it.isNotEmpty() }?.toIntOrNull()
    return if (line != null && line >= 1) FileRef(path, line) else FileRef(path)
}
