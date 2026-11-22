/**
 * Pure helpers of the chat view state: the copy-on-write maps, the defensive readers for the
 * parts of an event (`parseQuestions`, `parseHonesty`, ...), and the functions a view calls to
 * describe the state (`honestyChip`, `sessionTitle`, `messageTarget`, ...).
 *
 *     event params (JsonObject, untrusted) ──parse*──► view types (ViewState.kt)
 *     ViewState ──honestyChip / messageTarget / openEscalationCount / ...──► texts and flags for a view
 *
 * Port of the helper functions of `extensions/vscode/src/shared/viewState.ts`. No function of
 * this file throws for any JSON value.
 */
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.CaluJsonPretty
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asArr
import io.github.kadeng.calu.json.asBool
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.asStr
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.jsString
import io.github.kadeng.calu.json.num
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

// ------------------------------------------------------------------ safe maps

/**
 * Read an entry of a map.
 *
 * @param map A map from the state.
 * @param key Any string, including "__proto__".
 * @return The value stored under `key`, or `null` if there is no entry.
 */
fun <T> own(map: Map<String, T>, key: String): T? = map[key]

/**
 * Return a copy of a map with one entry set.
 *
 * @param map The map (not modified).
 * @param key Any string, including "__proto__".
 * @param value The value.
 * @return The new map in the insertion order of `map`; a new key is the last entry, an existing
 *   key keeps its position.
 */
fun <T> withEntry(map: Map<String, T>, key: String, value: T): Map<String, T> {
    val copy = LinkedHashMap(map)
    copy[key] = value
    return copy
}

/**
 * Return a copy of a map without the given keys.
 *
 * @param map The map (not modified).
 * @param keys Keys to drop; missing keys are ignored.
 * @return The new map; the same object if no key was present.
 */
fun <T> withoutEntries(map: Map<String, T>, keys: Iterable<String>): Map<String, T> {
    val drop = keys.filterTo(HashSet()) { it in map }
    if (drop.isEmpty()) return map
    val copy = LinkedHashMap<String, T>()
    for ((k, v) in map) if (k !in drop) copy[k] = v
    return copy
}

// ------------------------------------------------------------------ small readers

/**
 * Return the message-list key for an `agent_id` field of an event.
 *
 * @param agentId The event's `agent_id` (missing, JSON null, "" or a non-string for the main
 *   agent).
 * @return `MAIN` or the id.
 */
fun agentKeyOf(agentId: JsonElement?): String = agentId.asStr()?.takeIf { it.isNotEmpty() } ?: MAIN

/**
 * Convert a member of unknown type to text as the TypeScript original's `String(value ?? '')`.
 *
 * @param value Any JSON value or `null` (a missing member).
 * @return "" for a missing member and for JSON null, else `value.jsString()`.
 */
internal fun textOrEmpty(value: JsonElement?): String =
    if (value == null || value is JsonNull) "" else value.jsString()

/**
 * Read a string member that must not be empty.
 *
 * @param o The object (untrusted).
 * @param key The member's name.
 * @return The member if it is a non-empty JSON string, else `null`.
 */
internal fun nonEmptyStr(o: JsonObject, key: String): String? = o.str(key)?.takeIf { it.isNotEmpty() }

/**
 * Cut a text to `limit` characters.
 *
 * @param text Any string.
 * @param limit Maximum length (UTF-16 code units, as `String.length`).
 * @return `text`, or its first `limit` characters followed by "\n… (N more characters)".
 */
fun truncate(text: String, limit: Int = MAX_STORED_TEXT): String =
    if (text.length <= limit) text else "${text.substring(0, limit)}\n… (${text.length - limit} more characters)"

/**
 * Recursively copy a JSON value with bounded strings (helper of `boundInput`).
 *
 * @param v Any JSON value.
 * @param depth Nesting depth (values other than strings deeper than 20 levels become "…").
 * @return The bounded copy.
 */
private fun boundValue(v: JsonElement, depth: Int): JsonElement {
    if (v is JsonPrimitive && v.isString) return JsonPrimitive(truncate(v.content))
    if (depth > 20) return JsonPrimitive("…")
    return when (v) {
        is JsonArray -> JsonArray(v.map { boundValue(it, depth + 1) })
        is JsonObject -> JsonObject(v.mapValues { boundValue(it.value, depth + 1) })
        else -> v
    }
}

/**
 * Bound the size of a tool input for storage: every string value (also nested) is cut with
 * `truncate`. Non-object inputs become `{}`.
 *
 * @param input The tool input from calu (any JSON value or `null`).
 * @return A new object of the same shape with bounded strings.
 */
fun boundInput(input: JsonElement?): JsonObject =
    (input as? JsonObject)?.let { boundValue(it, 0) as JsonObject } ?: EMPTY_OBJECT

/**
 * Describe a tool input literally, for display next to an escalation (the user must see what
 * they approve, independent of calu's Markdown rendering).
 *
 * @param input The (bounded) tool input.
 * @return `(label, text)` pairs: "Command" (Bash `command`), "File" (`file_path` / `path` /
 *   `notebook_path`), always followed by "Input (JSON)" with the whole input pretty-printed
 *   (two-space indentation).
 */
fun literalToolInput(input: JsonObject): List<Pair<String, String>> {
    val parts = mutableListOf<Pair<String, String>>()
    input.str("command")?.let { parts += "Command" to it }
    for (key in listOf("file_path", "path", "notebook_path")) {
        input.str(key)?.let { parts += "File" to it }
    }
    parts += "Input (JSON)" to CaluJsonPretty.encodeToString(JsonObject.serializer(), input)
    return parts
}

// ------------------------------------------------------------------ questions

/**
 * Read the questions of an escalation (`escalation.request`.questions) defensively.
 *
 * @param raw The event's `questions` member (any JSON value or `null`).
 * @return One `QuestionView` per entry that is an object with a string `question`; options
 *   without a string `label` are dropped; texts are cut with `truncate`. Empty for a non-array.
 */
fun parseQuestions(raw: JsonElement?): List<QuestionView> =
    raw.asArr().mapNotNull { entry ->
        val q = entry.asObj() ?: return@mapNotNull null
        val question = q.str("question") ?: return@mapNotNull null
        val options = q.arr("options").mapNotNull { o ->
            val option = o.asObj() ?: return@mapNotNull null
            val label = option.str("label") ?: return@mapNotNull null
            QuestionOption(label = truncate(label), description = option.str("description")?.let { truncate(it) } ?: "")
        }
        QuestionView(
            question = truncate(question),
            header = q.str("header")?.let { truncate(it) } ?: "",
            options = options,
            multiSelect = q.bool("multiSelect"),
        )
    }

/** The form of the note calu sends for an answered question (see `answerFromNote`). */
private val ANSWER_NOTE = Regex("^The user answered your question: ([\\s\\S]*?)\\nThe AskUserQuestion call itself was not run;")

/**
 * Extract the user's answer from the note of an `escalation.resolved` event of a question.
 *
 * calu sends the text Claude receives: "The user answered your question: <answer>\nThe
 * AskUserQuestion call itself was not run; ..." (`ANSWER_TEMPLATE` in `calu.escalations`).
 *
 * @param note The event's note.
 * @return The answer without calu's frame; the whole note if it has another form.
 */
fun answerFromNote(note: String): String = ANSWER_NOTE.find(note)?.groupValues?.get(1) ?: note

/**
 * The user's choice for one question of an escalation (input of `formatAnswer`).
 *
 * @property labels The chosen option labels.
 * @property text A free text ("" if none).
 */
data class AnswerChoice(val labels: List<String>, val text: String)

/**
 * Build the answer text for the questions of an escalation from the user's choices.
 *
 * Example: for one question and the choice `AnswerChoice(listOf("A", "B"), "")` the result is
 * "A, B".
 *
 * @param questions The questions of the escalation, in order.
 * @param choices Per question (same index) the chosen option labels and/or a free text; a
 *   `null` or missing entry counts as no answer.
 * @return For one question the answer alone; for several, one line "<header or question>:
 *   <answer>" per question. Chosen labels are joined with ", "; a free text is appended after
 *   them. "" if nothing was chosen or written.
 */
fun formatAnswer(questions: List<QuestionView>, choices: List<AnswerChoice?>): String {
    val answers = questions.indices.map { i ->
        val c = choices.getOrNull(i)
        if (c == null) "" else (c.labels + c.text.trim()).filter { it != "" }.joinToString(", ")
    }
    if (answers.all { it == "" }) return ""
    if (questions.size == 1) return answers[0]
    return questions.mapIndexed { i, q -> "${q.header.ifEmpty { q.question }}: ${answers[i].ifEmpty { "(no answer)" }}" }
        .joinToString("\n")
}

// ------------------------------------------------------------------ honesty, limits, sessions

/**
 * Read a code honesty state (`honesty.update` params or the snapshot's `honesty`) defensively.
 *
 * @param raw Any JSON value or `null`.
 * @return A `HonestyInfo` with every field set (defaults for missing or mistyped ones; a mark
 *   needs a whole-number `id`), or `null` if `raw` is not an object.
 */
fun parseHonesty(raw: JsonElement?): HonestyInfo? {
    val p = raw.asObj() ?: return null
    val marks = p.arr("marks").mapNotNull { m ->
        val mark = m.asObj() ?: return@mapNotNull null
        val id = mark.int("id") ?: return@mapNotNull null
        HonestyMarkInfo(
            id = id,
            path = mark.str("path") ?: "",
            reason = truncate(mark.str("reason") ?: ""),
            symbol = mark.str("symbol") ?: "",
            openedBy = mark.str("opened_by"),
        )
    }
    return HonestyInfo(
        mode = p.str("mode") ?: "off",
        clean = p.bool("clean"),
        baseline = p.str("baseline"),
        baselineSource = p.str("baseline_source"),
        marks = marks,
        reviewRequired = p.bool("review_required"),
        reviewRunning = p.bool("review_running"),
        unreviewed = p.strings("unreviewed"),
        unreviewedAll = p.bool("unreviewed_all"),
        unreviewedCount = p.int("unreviewed_count") ?: 0,
    )
}

/**
 * The summary of a code honesty state for a header chip (result of `honestyChip`).
 *
 * @property kind "off" (mode off or unknown), "clean", "review" (a review runs) or "open"
 *   (marks, unreviewed files or a required review).
 * @property label A short text such as "Honesty: 2 marks, 3 unreviewed"; "" for "off".
 */
data class HonestyChip(val kind: String, val label: String)

/**
 * Summarise a code honesty state for a header chip.
 *
 * @param honesty The state, or `null`.
 * @return The chip, see `HonestyChip`.
 */
fun honestyChip(honesty: HonestyInfo?): HonestyChip {
    if (honesty == null || honesty.mode.isEmpty() || honesty.mode == "off") return HonestyChip("off", "")
    if (honesty.reviewRunning) return HonestyChip("review", "Honesty: review running")
    if (honesty.clean) return HonestyChip("clean", "Honesty: clean")
    val marks = honesty.marks.size
    val unreviewed = if (honesty.unreviewedAll) "all files" else "${honesty.unreviewedCount}"
    val parts = listOf(
        if (marks != 0) "$marks mark${if (marks == 1) "" else "s"}" else "",
        if (honesty.unreviewedAll || honesty.unreviewedCount > 0) "$unreviewed unreviewed" else "",
        if (honesty.baseline.isNullOrEmpty()) "no baseline" else "",
        if (honesty.reviewRequired) "review required" else "",
    ).filter { it.isNotEmpty() }
    return HonestyChip("open", "Honesty: ${parts.joinToString(", ").ifEmpty { "not clean" }}")
}

/**
 * Say that calu's settings store another honesty mode than the session uses.
 *
 * A session reads a changed `honesty.mode` by itself and reports it (`honesty.update`); the
 * two differ in the moment between, and for a session that does not read the change (it uses
 * another database, or its settings could not be reloaded).
 *
 * @param honesty The session's honesty state, or `null` (counts as mode "off").
 * @param stored `ViewState.honestyStored`.
 * @return "" if the stored mode is unknown or equal to the session's; else e.g.
 *   "stored: track".
 */
fun honestyStoredNote(honesty: HonestyInfo?, stored: String?): String {
    val mode = honesty?.mode?.takeIf { it.isNotEmpty() } ?: "off"
    if (stored.isNullOrEmpty() || stored == mode) return ""
    return "stored: $stored"
}

/**
 * Read a list of usage limits defensively.
 *
 * @param raw The `rate_limits` member of `usage.update`, the snapshot or `usage.get`.
 * @return The entries that are objects, with `type`, `status`, `utilization` and `resets_at`
 *   set (defaults "" / `null`). Empty for a non-array.
 */
fun parseRateLimits(raw: JsonElement?): List<RateLimit> =
    raw.asArr().mapNotNull { it.asObj() }.map { r ->
        RateLimit(
            type = r.str("type") ?: "",
            status = r.str("status") ?: "",
            utilization = r.num("utilization"),
            resetsAt = r.str("resets_at"),
            reportedAt = r.str("reported_at"),
        )
    }

/**
 * Read the running sessions of `ide.sessions` for the dropdown.
 *
 * @param raw The `running` member (any JSON value or `null`).
 * @return One `SessionEntry` per object with a string `id`, sessions of the workspace
 *   (`here`) first, otherwise in the given order. Empty for a non-array.
 */
fun parseSessions(raw: JsonElement?): List<SessionEntry> {
    val list = raw.asArr().mapNotNull { entry ->
        val s = entry.asObj() ?: return@mapNotNull null
        val id = s.str("id") ?: return@mapNotNull null
        SessionEntry(
            id = id,
            name = s.str("name") ?: "",
            directory = s.str("directory") ?: "",
            state = s.str("state") ?: "",
            openEscalations = s.int("open_escalations") ?: 0,
            here = s.bool("here"),
            stale = s.bool("stale"),
        )
    }
    return list.filter { it.here } + list.filter { !it.here }
}

/** Trailing path separators of a directory (see `sessionTitle`). */
private val TRAILING_SEPARATORS = Regex("[\\\\/]+$")

/** One path separator, "/" or "\" (see `sessionTitle`). */
private val SEPARATOR = Regex("[\\\\/]")

/**
 * Return the text that names a session in a list.
 *
 * @param id The session's id.
 * @param name The name given by the user ("" if none).
 * @param directory The directory the session works in ("" if unknown).
 * @return The trimmed name if it has one, else the last segment of the directory, else the
 *   first 8 characters of the id.
 */
fun sessionTitle(id: String, name: String, directory: String): String {
    if (name.trim().isNotEmpty()) return name.trim()
    val last = directory.replace(TRAILING_SEPARATORS, "").split(SEPARATOR).last()
    return last.ifEmpty { id.take(8) }
}

/**
 * Return the text that names a session in a list.
 *
 * @param session The session.
 * @return Its name if it has one, else the last segment of its directory, else the first 8
 *   characters of its id.
 */
fun sessionTitle(session: SessionEntry): String = sessionTitle(session.id, session.name, session.directory)

/**
 * Read the attached images of a user input.
 *
 * @param raw The event's `images` member (any JSON value or `null`).
 * @return One `ImageView` per entry with string `data` and `media_type`; data longer than
 *   `MAX_IMAGE_DATA` is dropped and the image marked `omitted`.
 */
internal fun parseImages(raw: JsonElement?): List<ImageView> =
    raw.asArr().mapNotNull { entry ->
        val i = entry.asObj() ?: return@mapNotNull null
        val data = i.str("data") ?: return@mapNotNull null
        val mediaType = i.str("media_type") ?: return@mapNotNull null
        if (data.length > MAX_IMAGE_DATA) {
            ImageView(mediaType = mediaType, data = "", omitted = true)
        } else {
            ImageView(mediaType = mediaType, data = data, omitted = false)
        }
    }

/**
 * Read one subagent of `agents.update` defensively.
 *
 * @param raw An entry of the event's `agents` (any JSON value).
 * @return The agent, or `null` if `raw` is not an object with a string `agent_id`. Optional
 *   members of another type than the protocol's are read as missing (`null`).
 */
internal fun parseAgent(raw: JsonElement?): AgentInfo? {
    val a = raw.asObj() ?: return null
    val agentId = a.str("agent_id") ?: return null
    return AgentInfo(
        agentId = agentId,
        background = a["background"].asBool(),
        description = a.str("description"),
        sdkAgentId = a.str("sdk_agent_id"),
        status = a.str("status"),
        subagentType = a.str("subagent_type"),
    )
}

/**
 * Read one setup hint of `ui.hints` defensively.
 *
 * @param raw An entry of the event's `hints` (any JSON value).
 * @return The hint, or `null` if `raw` is not an object with a string `id` and a string
 *   `message`. Other members that are not strings get their defaults (`level` "info", `action`
 *   and `actionLabel` "", the rest `null`).
 */
internal fun parseHint(raw: JsonElement?): SetupHint? {
    val h = raw.asObj() ?: return null
    val id = h.str("id") ?: return null
    val message = h.str("message") ?: return null
    return SetupHint(
        id = id,
        level = h.str("level") ?: "info",
        message = message,
        action = h.str("action") ?: "",
        actionLabel = h.str("actionLabel") ?: "",
        href = h.str("href"),
        short = h.str("short"),
        actionShort = h.str("actionShort"),
    )
}

// ------------------------------------------------------------------ checkers

/**
 * Tell whether a tool starts a subagent.
 *
 * @param name The tool name from `tool.use` (`null` if it is not a string).
 * @return True for "Agent" and "Task" (the older name of the same tool).
 */
fun isAgentLaunch(name: String?): Boolean = name == "Agent" || name == "Task"

/**
 * Return the route whose checkers judged a step, for *Edit checker instructions*. Checker
 * names are free (several checkers can serve one route), so the host looks the checkers up by
 * route.
 *
 * @param route The `route` of a `check.result`.
 * @return The route itself for the routes a checker judges ("command", "edit", "changeset",
 *   "subagent"); "" for the rules' routes ("allow", "deny", "escalate") and for "question".
 */
fun checkerRouteOf(route: String): String =
    if (route == "command" || route == "edit" || route == "changeset" || route == "subagent") route else ""

/**
 * Return the display name of the checker (or rule) behind a `check.result` route.
 *
 * @param route The event's `route`.
 * @return "Command checker", "Edit checker", "Changeset checker", "Subagent checker",
 *   "Question" (a question of the agent), "Recorded decision" for "stored" (a step taken from
 *   calu's record, which does not say who decided), or "Rules" for "allow", "deny" and "escalate".
 */
fun checkerLabel(route: String): String = when (route) {
    "command" -> "Command checker"
    "edit" -> "Edit checker"
    "changeset" -> "Changeset checker"
    "subagent" -> "Subagent checker"
    "question" -> "Question"
    "stored" -> "Recorded decision"
    else -> "Rules"
}

/**
 * Return the text a `check.result` contributes to the chat as a checker message.
 *
 * @param e The event params (untrusted).
 * @return The non-empty `comment` (a remark made while approving), else the non-empty `reason`
 *   of a DENY decision, else `null` (no chat message).
 */
fun checkerMessage(e: JsonObject): String? {
    val comment = e.str("comment")?.trim() ?: ""
    if (comment.isNotEmpty()) return comment
    val reason = e.str("reason")?.trim() ?: ""
    if (e.str("decision") == "DENY" && reason.isNotEmpty()) return reason
    return null
}

// ------------------------------------------------------------------ questions a view asks

/**
 * Who receives a chat message (result of `messageTarget`).
 *
 * @property to `MAIN` or the subagent's id; `null` if the tab's subagent cannot receive messages.
 * @property label Names the recipient, for the input's placeholder.
 * @property reason Set if `to` is `null`: why the subagent cannot receive messages.
 */
data class MessageTarget(val to: String?, val label: String, val reason: String? = null)

/**
 * Return who receives a chat message written while `tab` is shown: the agent of that tab.
 * Only the main agent and running background subagents can receive messages (calu rejects
 * others); an unknown tab key sends to the main agent.
 *
 * @param state The current state.
 * @param tab The visible tab: `MAIN`, an agent_id, or another tab key.
 * @return The recipient, or a target with `to == null` and a `reason` if the tab's subagent
 *   cannot receive messages.
 */
fun messageTarget(state: ViewState, tab: String): MessageTarget {
    val agent = state.agents.firstOrNull { it.agentId == tab }
        ?: return MessageTarget(to = MAIN, label = "Claude (main agent)")
    val label = "subagent \"${agent.description.orEmpty().ifEmpty { agent.agentId }}\""
    if ((agent.status ?: "running") != "running") {
        return MessageTarget(to = null, label = label, reason = "The $label has finished; switch to Main to write to Claude.")
    }
    if (agent.background != true) {
        return MessageTarget(
            to = null,
            label = label,
            reason = "The $label runs in the foreground and cannot receive messages; switch to Main to write to Claude.",
        )
    }
    return MessageTarget(to = agent.agentId, label = label)
}

/**
 * Count the open escalations (for the view's badge).
 *
 * @param state The current state.
 * @return The number of escalations with status "open".
 */
fun openEscalationCount(state: ViewState): Int = state.escalations.values.count { it.status == EscalationStatus.OPEN }

/**
 * Tell whether the chat can send requests to a session.
 *
 * @param state The current state.
 * @return True if the bridge runs and a session is attached (also while reconnecting).
 */
fun isAttached(state: ViewState): Boolean =
    state.bridge.status == BridgeStatus.RUNNING &&
        (state.link.status == LinkStatus.ATTACHED || state.link.status == LinkStatus.RECONNECTING)

/**
 * One answered side question (element of `sideHistory`).
 *
 * @property question The question text.
 * @property answer The Markdown answer.
 */
data class SideAnswer(val question: String, val answer: String)

/**
 * Return the side questions that were answered, as the `history` of the next `side.ask`.
 *
 * @param state The current state.
 * @return `(question, answer)` pairs, oldest first, at most `MAX_SIDE_EXCHANGES`.
 */
fun sideHistory(state: ViewState): List<SideAnswer> =
    state.side
        .mapNotNull { s -> s.answer?.takeIf { it != "" }?.let { SideAnswer(s.question, it) } }
        .takeLast(MAX_SIDE_EXCHANGES)

/**
 * Tell whether a file path came from calu as a file link of a tool call, so the plugin may open
 * it when the view asks (`openFile`).
 *
 * @param state The plugin's state.
 * @param path An absolute path from the view (untrusted).
 * @return True if some tool call of the state lists exactly this path in its `files`.
 */
fun isKnownFile(state: ViewState, path: String): Boolean =
    state.tools.values.any { tool -> tool.files?.any { it.path == path } == true }

/**
 * Read the honesty checker's result from `check.result` params defensively (port of
 * `parseHonestyResult` in `viewState.ts`).
 *
 * @param p The params of a `check.result` event (untrusted).
 * @return `null` if the event carries no honesty result (no `honesty` entries and no
 *   `honesty_escalation`). Otherwise one `HonestyFileView` per well-formed entry (an entry
 *   without a string `path` is dropped; texts are cut by `truncate`; a unit counts only if its
 *   `honest` is false) and the escalation reason.
 */
fun parseHonestyResult(p: JsonObject): HonestyResultView? {
    fun text(o: JsonObject, key: String): String = o.str(key)?.let { truncate(it) } ?: ""
    val files = p.arr("honesty").mapNotNull { it.asObj() }.mapNotNull { f ->
        val path = f.str("path") ?: return@mapNotNull null
        HonestyFileView(
            path = path,
            honest = f.str("verdict") != "DISHONEST",
            reason = text(f, "reason"),
            dishonestUnits = f.arr("units").mapNotNull { it.asObj() }
                .filter { it["honest"] == kotlinx.serialization.json.JsonPrimitive(false) }
                .map { HonestyUnitView(text(it, "symbol"), text(it, "reason")) },
        )
    }
    val escalation = text(p, "honesty_escalation")
    if (files.isEmpty() && escalation.isEmpty()) return null
    return HonestyResultView(files, escalation)
}

/**
 * One line of the per-file list a checked step shows for the honesty checker.
 *
 * @property text "greeter.py: HONEST", "greeter.py: DISHONEST. <reason>", or for the checker's
 *   question "Honesty checker asks a person: <reason>".
 * @property level "ok" for an honest file, "error" for a dishonest one, "warning" for the question.
 * @property tooltip The file's whole path, or "" for the question.
 */
data class HonestyLine(val text: String, val level: String, val tooltip: String = "")

/**
 * Build the per-file list of a step's honesty result. No side effects.
 *
 * @param honesty The step's honesty result, or `null`.
 * @return One line per judged file (the file's name, HONEST or DISHONEST with the reason), then
 *   the checker's question if it asked for a person; empty for `null`. The words PASS and ALLOW
 *   never occur: the honesty checker gives a status, not a decision.
 */
fun honestyLines(honesty: HonestyResultView?): List<HonestyLine> {
    if (honesty == null) return emptyList()
    val files = honesty.files.map { f ->
        val name = f.path.substringAfterLast('/')
        if (f.honest) HonestyLine("$name: HONEST", "ok", f.path)
        else HonestyLine("$name: DISHONEST" + (if (f.reason.isNotEmpty()) ". ${f.reason}" else ""), "error", f.path)
    }
    return files + listOfNotNull(honesty.escalation.takeIf { it.isNotEmpty() }?.let { HonestyLine("Honesty checker asks a person: $it", "warning") })
}

/**
 * Write the dishonest units of a step's honesty result for the row's details.
 *
 * @param honesty The step's honesty result, or `null`.
 * @return "" if no file has a dishonest unit; else "Honesty checker, dishonest units:" and one
 *   line per unit: "<path> · <symbol>: <reason>".
 */
fun honestyUnitsText(honesty: HonestyResultView?): String {
    val units = honesty?.files.orEmpty().flatMap { f -> f.dishonestUnits.map { u -> "${f.path} · ${u.symbol}: ${u.reason}" } }
    return if (units.isEmpty()) "" else "Honesty checker, dishonest units:\n" + units.joinToString("\n")
}
