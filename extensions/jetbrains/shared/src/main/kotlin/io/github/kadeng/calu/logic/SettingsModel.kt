/**
 * Form model of calu's settings: turns the result of `ide.settings.get` into sections with one
 * control per setting, converts between a control's text and the JSON value that
 * `ide.settings.set` takes, and builds the bridge requests of the dialog. Pure.
 *
 *     ide.settings.get ──parseSettingRows──► rows ──buildSections(rows, search)──► sections ──► form
 *                      ──parseCheckers────► the checkers list
 *     user edits a control  ──settingEdit(row, input, project)──► ide.settings.set {key, value, project}
 *                                                                 or a message for the row
 *     "reset to default"    ──settingsUnsetRequest(key, project)──► ide.settings.unset {key, project}
 *     ide.status findings   ──setupItems(findings)──► Setup rows with fix actions
 *
 *     section "Claude"  (id "claude", description of the row `claude` of the kind "section")
 *       row  key "claude.effort"  label "Effort"  control SELECT  choices ["", "low", "high"]
 *            value "high"  defaultText "null"  modified true
 *
 * `project` is the scope: `null` for calu's global settings, else the name of a project that
 * has its own values (`ide.settings.get`.projects). The models card, the search highlighting,
 * the Setup card and the bookkeeping of optimistic saves are in `SettingsView.kt`.
 *
 * Port of `extensions/vscode/src/shared/settingsModel.ts` and of the requests of
 * `settingsMessages.ts` / `settingsPanel.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.CaluJsonPretty
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * One setting of calu as `ide.settings.get` lists it.
 *
 * @property key Dotted key, e.g. "claude.model".
 * @property type Type as text for the user, e.g. "true/false", "text (or empty)".
 * @property default The default as text for the user; `null` if calu sent another JSON value
 *   than a string (it is then in `defaultRaw`).
 * @property defaultRaw The row's `default` member as calu sent it; `null` if missing.
 * @property defaultValue The default as a JSON value (`default_value`); `null` if the member is
 *   missing, `JsonNull` if the default is "no value".
 * @property description What the setting does (plain sentences with the marks of `InlineText.kt`).
 * @property value What is stored in the asked scope, else what the scope inherits (the global
 *   value in a project, else the default), as a JSON value.
 * @property isSet True if a value is stored in the asked scope (the row is "modified").
 * @property inherits Where the value comes from when `isSet` is false: "global" or "default".
 * @property kind Control kind computed by calu: "bool", "choice" (with `choices`), "int",
 *   "number", "text", "model" (a model string), "markdown", "lines" (a list of strings), "json"
 *   (any other value), "section" (a nested group of settings without a control of its own).
 * @property choices Allowed values of a choice; empty otherwise.
 */
data class SettingRow(
    val key: String,
    val type: String = "",
    val default: String? = null,
    val defaultRaw: JsonElement? = null,
    val defaultValue: JsonElement? = null,
    val description: String = "",
    val value: JsonElement = JsonNull,
    val isSet: Boolean = false,
    val inherits: String = "default",
    val kind: String = "json",
    val choices: List<String> = emptyList(),
)

/**
 * Read the rows of `ide.settings.get` defensively.
 *
 * @param result The result of `ide.settings.get` (untrusted), or `null`.
 * @return One row per entry of `rows` that is an object with a non-empty string `key`, in
 *   calu's order; missing members get the defaults of `SettingRow`.
 */
fun parseSettingRows(result: JsonObject?): List<SettingRow> = result?.arr("rows").orEmpty().mapNotNull { entry ->
    val o = entry.asObj() ?: return@mapNotNull null
    val key = o.str("key")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
    SettingRow(
        key = key,
        type = o.str("type").orEmpty(),
        default = o.str("default"),
        defaultRaw = o["default"],
        defaultValue = o["default_value"],
        description = o.str("description").orEmpty(),
        value = o["value"] ?: JsonNull,
        isSet = o.bool("is_set"),
        inherits = if (o.str("inherits") == "global") "global" else "default",
        kind = o.str("kind").orEmpty(),
        choices = o.strings("choices"),
    )
}

/**
 * A checker definition as the settings list it.
 *
 * @property name The checker's name (what `act("editChecker", {name})` takes).
 * @property route The kind of step it judges: "command", "edit", "changeset", "subagent", ...
 * @property model Its model string; "" for the default model of the checkers.
 * @property enabled False if the checker is switched off.
 * @property version Version of its instructions.
 * @property requestLimit Most model requests of one check; `null` for the default.
 * @property outputTokensLimit Most output tokens of one check; `null` for the default.
 * @property maxTokens Most tokens of one answer; `null` for the default.
 */
data class CheckerRow(
    val name: String,
    val route: String = "",
    val model: String = "",
    val enabled: Boolean = true,
    val version: Int = 0,
    val requestLimit: Int? = null,
    val outputTokensLimit: Int? = null,
    val maxTokens: Int? = null,
)

/**
 * Read the checkers of `ide.settings.get` defensively.
 *
 * @param result The result of `ide.settings.get` (untrusted), or `null`.
 * @return One row per entry of `checkers` that is an object with a non-empty string `name`.
 */
fun parseCheckers(result: JsonObject?): List<CheckerRow> = result?.arr("checkers").orEmpty().mapNotNull { entry ->
    val o = entry.asObj() ?: return@mapNotNull null
    val name = o.str("name")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
    CheckerRow(
        name, o.str("route").orEmpty(), o.str("model").orEmpty(), o["enabled"] != JsonPrimitive(false), o.int("version") ?: 0,
        o.int("request_limit"), o.int("output_tokens_limit"), o.int("max_tokens"),
    )
}

/**
 * Read the names of the projects that can have their own values.
 *
 * @param result The result of `ide.settings.get` (untrusted), or `null`.
 * @return The strings of `projects`.
 */
fun parseSettingProjects(result: JsonObject?): List<String> = result?.strings("projects").orEmpty()

/** The control a setting is edited with. */
enum class ControlKind {
    /** A checkbox (boolean). */
    TOGGLE,

    /** One of `SettingsRowView.choices`. */
    SELECT,

    /** An integer or a decimal number. */
    NUMBER,

    /** One line. */
    TEXT,

    /** One line holding a model string (the dialog may offer the presets). */
    MODEL,

    /** A multi-line text. */
    MARKDOWN,

    /** A multi-line text, one entry per line. */
    LIST,

    /** A multi-line text holding any JSON value. */
    JSON,
}

/**
 * One setting as the form shows it.
 *
 * @property key Dotted key, e.g. "claude.model"; what `set` and `unset` take.
 * @property label Human label from the key's last segments, e.g. "Stream partial".
 * @property description What the setting does.
 * @property control The control it is edited with.
 * @property choices For `SELECT`: the allowed values. For a setting that may be empty, "" is
 *   the first.
 * @property integer True for `NUMBER` settings that take whole numbers only.
 * @property nullable True if the setting may be empty (`null`): an empty input then stores no value.
 * @property value The value in effect in the shown scope (a JSON value); `formatControlValue`
 *   turns it into the control's content.
 * @property defaultText The default as text.
 * @property defaultValue The default as a JSON value; `null` if calu did not send one.
 * @property modified True if a value is stored in the shown scope: show the marker and "reset
 *   to default".
 * @property inherits Where the value comes from when it is not modified: "global" (in a
 *   project) or "default".
 * @property type The type as text, for a tooltip.
 */
data class SettingsRowView(
    val key: String,
    val label: String,
    val description: String,
    val control: ControlKind,
    val choices: List<String>,
    val integer: Boolean,
    val nullable: Boolean,
    val value: JsonElement,
    val defaultText: String,
    val defaultValue: JsonElement?,
    val modified: Boolean,
    val inherits: String,
    val type: String,
)

/**
 * A group of settings: all keys with the same first segment.
 *
 * @property id The first key segment, e.g. "claude"; "general" for keys without a dot.
 * @property label Human label, e.g. "Claude".
 * @property description Description of the group (from its "section" row); "" if none.
 * @property rows The settings of the group in the order calu lists them (after filtering).
 * @property modifiedCount Number of `rows` that are modified.
 */
data class SettingsSection(val id: String, val label: String, val description: String, val rows: List<SettingsRowView>, val modifiedCount: Int)

/** Id of the section for keys without a dot. */
const val GENERAL_SECTION: String = "general"

/** Words that keep their spelling in labels. */
private val LABEL_WORDS: Map<String, String> = mapOf(
    "id" to "ID", "ids" to "IDs", "url" to "URL", "api" to "API", "aws" to "AWS", "sdk" to "SDK", "mcp" to "MCP", "acp" to "ACP",
    "cli" to "CLI", "usd" to "USD", "json" to "JSON", "tui" to "TUI", "s" to "seconds", "ms" to "milliseconds",
)

private val WORD_SEPARATOR = Regex("[_-]+")

/**
 * Turn one key segment into words.
 *
 * @param segment A segment such as "stream_partial" or "timeout_s".
 * @return The words separated by spaces, lower case, with known abbreviations spelled as in
 *   `LABEL_WORDS`; e.g. "stream partial", "timeout seconds".
 */
private fun words(segment: String): String =
    segment.split(WORD_SEPARATOR).filter { it.isNotEmpty() }.joinToString(" ") { LABEL_WORDS[it.lowercase()] ?: it.lowercase() }

/**
 * Capitalise the first letter of a text.
 *
 * @param text Any string.
 * @return The text with its first character in upper case.
 */
private fun capitalise(text: String): String = if (text.isEmpty()) text else text.substring(0, 1).uppercase() + text.substring(1)

/**
 * Return the human label of a section.
 *
 * @param id The first key segment, e.g. "auto_escalate".
 * @return E.g. "Auto escalate"; "General" for `GENERAL_SECTION` and for "".
 */
fun sectionLabel(id: String): String = capitalise(words(id)).ifEmpty { "General" }

/**
 * Return the human label of a setting inside its section.
 *
 * Examples: `labelOf("claude.stream_partial") == "Stream partial"`,
 * `labelOf("checkers.command.request_limit") == "Command › request limit"`.
 *
 * @param key The dotted key.
 * @return The segments after the first as words, deeper levels joined with " › "; the only
 *   segment for a key without a dot.
 */
fun labelOf(key: String): String {
    val parts = key.split(".")
    return capitalise((if (parts.size > 1) parts.drop(1) else parts).joinToString(" › ") { words(it) })
}

/**
 * Return the section id of a key.
 *
 * @param key The dotted key.
 * @return The first segment, or `GENERAL_SECTION` for a key without a dot.
 */
fun sectionOf(key: String): String {
    val dot = key.indexOf('.')
    return if (dot > 0) key.substring(0, dot) else GENERAL_SECTION
}

/**
 * Choose the control of a row from the kind calu computed.
 *
 * @param kind The row's `kind`.
 * @param choices The row's `choices`.
 * @return The control; `JSON` for an unknown kind, so every setting stays editable. A "choice"
 *   without choices becomes `TEXT`.
 */
fun controlKind(kind: String, choices: List<String> = emptyList()): ControlKind = when (kind) {
    "bool" -> ControlKind.TOGGLE
    "choice" -> if (choices.isNotEmpty()) ControlKind.SELECT else ControlKind.TEXT
    "int", "number" -> ControlKind.NUMBER
    "text" -> ControlKind.TEXT
    "model" -> ControlKind.MODEL
    "markdown" -> ControlKind.MARKDOWN
    "lines" -> ControlKind.LIST
    else -> ControlKind.JSON
}

private val NULLABLE_TYPE = Regex("\\bor empty\\b|\\bNone\\b|\\bnull\\b|\\boptional\\b", RegexOption.IGNORE_CASE)

/**
 * Write a JSON value as compact JSON text.
 *
 * @param value The value.
 * @return The text, e.g. `{"A":"1"}`.
 */
private fun jsonText(value: JsonElement): String = CaluJson.encodeToString(JsonElement.serializer(), value)

/**
 * Build the view of one row.
 *
 * @param row A row of `ide.settings.get` whose kind is not "section".
 * @return The row as the form shows it.
 */
fun rowView(row: SettingRow): SettingsRowView {
    val control = controlKind(row.kind, row.choices)
    val nullable = NULLABLE_TYPE.containsMatchIn(row.type) || row.defaultValue is JsonNull
    val choices = if (control != ControlKind.SELECT) emptyList() else if (nullable && "" !in row.choices) listOf("") + row.choices else row.choices
    return SettingsRowView(
        key = row.key,
        label = labelOf(row.key),
        description = row.description,
        control = control,
        choices = choices,
        integer = row.kind == "int",
        nullable = nullable,
        value = row.value,
        defaultText = row.default ?: jsonText(row.defaultRaw ?: JsonNull),
        defaultValue = row.defaultValue,
        modified = row.isSet,
        inherits = if (row.inherits == "global") "global" else "default",
        type = row.type,
    )
}

private val SPACES = Regex("\\s+")

/**
 * Tell whether a row matches a search text.
 *
 * @param row The row.
 * @param search The search text; every word of it (split at white space) must occur in the
 *   key, the label or the description, ignoring case. The word "@modified" matches modified
 *   rows only.
 * @return True if the row matches; always true for an empty search.
 */
fun matchesSearch(row: SettingsRowView, search: String): Boolean {
    val needles = search.lowercase().split(SPACES).filter { it.isNotEmpty() }
    if (needles.isEmpty()) return true
    val hay = "${row.key} ${row.label} ${row.description}".lowercase()
    return needles.all { if (it == "@modified") row.modified else hay.contains(it) }
}

/**
 * Group the rows of `ide.settings.get` into sections.
 *
 * @param rows The rows, in calu's order. Rows of the kind "section" are not shown as
 *   settings; the description of a first-level one describes its section.
 * @param search Optional search text (see `matchesSearch`).
 * @return The sections in the order their first row appears, without sections that have no
 *   matching row.
 */
fun buildSections(rows: List<SettingRow>, search: String = ""): List<SettingsSection> {
    val sections = LinkedHashMap<String, MutableList<SettingsRowView>>()
    val descriptions = HashMap<String, String>()
    for (row in rows) {
        if (row.key.isEmpty()) continue
        if (row.kind == "section") {
            if ('.' !in row.key) descriptions[row.key] = row.description
            continue
        }
        val view = rowView(row)
        if (matchesSearch(view, search)) sections.getOrPut(sectionOf(row.key)) { ArrayList() }.add(view)
    }
    return sections.map { (id, list) -> SettingsSection(id, sectionLabel(id), descriptions[id].orEmpty(), list, list.count { it.modified }) }
}

/**
 * Return a JSON value as the text a person would write for it.
 *
 * @param value The value.
 * @return The content of a string, else the compact JSON text.
 */
private fun plainText(value: JsonElement): String = if (value is JsonPrimitive && value.isString) value.content else jsonText(value)

/**
 * Format a setting's value for its control.
 *
 * @param control The control.
 * @param value The JSON value; `null` stands for a missing value.
 * @return For `TOGGLE`: "true" or "false". For `LIST`: one entry per line. For `JSON`:
 *   pretty-printed JSON with two spaces ("" for a missing value, "null" for JSON null).
 *   Otherwise the value as text; "" for JSON null and a missing value.
 */
fun formatControlValue(control: ControlKind, value: JsonElement?): String {
    if (control == ControlKind.TOGGLE) return if (value == JsonPrimitive(true)) "true" else "false"
    if (control == ControlKind.JSON) return if (value == null) "" else CaluJsonPretty.encodeToString(JsonElement.serializer(), value)
    if (value == null || value is JsonNull) return ""
    if (control == ControlKind.LIST) return if (value is JsonArray) value.joinToString("\n") { plainText(it) } else plainText(value)
    return plainText(value)
}

/** Result of `parseControlValue`. */
sealed interface ParsedValue {
    /**
     * The input is a value.
     *
     * @property value The JSON value for `ide.settings.set`.
     */
    data class Ok(val value: JsonElement) : ParsedValue

    /**
     * The input is no value of the setting.
     *
     * @property message What to enter instead, as a sentence for the row.
     */
    data class Error(val message: String) : ParsedValue
}

private val NUMBER_TEXT = Regex("^[+-]?(\\d+\\.?\\d*|\\.\\d+)([eE][+-]?\\d+)?$")

/**
 * Convert what the user entered into the JSON value for `ide.settings.set`.
 *
 * This is a first check for an immediate message; calu validates the value again and its
 * message is shown the same way.
 *
 * @param row The row's view (`control`, `choices`, `integer`, `nullable`).
 * @param input The control's content; for `TOGGLE` the text "true" or "false".
 * @return `Ok`: a boolean (`TOGGLE`), a number (`NUMBER`; a whole number is written without a
 *   decimal point), a list of the non-empty trimmed lines (`LIST`), the parsed JSON (`JSON`),
 *   the text as entered (`MARKDOWN`), else the trimmed text; JSON null for an empty input of a
 *   nullable setting. `Error` for a number that is not one, a decimal where a whole number is
 *   needed, a value outside `choices`, invalid JSON, or an empty input of a `NUMBER` or `JSON`
 *   setting that cannot be empty.
 */
fun parseControlValue(row: SettingsRowView, input: String): ParsedValue {
    if (row.control == ControlKind.TOGGLE) return ParsedValue.Ok(JsonPrimitive(input == "true"))
    val trimmed = input.trim()
    if (trimmed.isEmpty() && row.nullable && row.control != ControlKind.LIST && row.control != ControlKind.JSON) return ParsedValue.Ok(JsonNull)
    return when (row.control) {
        ControlKind.NUMBER -> {
            if (trimmed.isEmpty()) return ParsedValue.Error("Enter a number.")
            val n = trimmed.takeIf { NUMBER_TEXT.matches(it) }?.toDoubleOrNull()?.takeIf { it.isFinite() }
                ?: return ParsedValue.Error("\"$trimmed\" is not a number.")
            val whole = n == Math.rint(n) && Math.abs(n) < 9.0e15
            if (row.integer && !whole) return ParsedValue.Error("Enter a whole number.")
            ParsedValue.Ok(if (whole) JsonPrimitive(n.toLong()) else JsonPrimitive(n))
        }
        ControlKind.SELECT ->
            if (trimmed in row.choices) ParsedValue.Ok(JsonPrimitive(trimmed))
            else ParsedValue.Error("Choose one of: ${row.choices.filter { it.isNotEmpty() }.joinToString(", ")}.")
        ControlKind.LIST -> ParsedValue.Ok(JsonArray(input.split("\n").map { it.trim() }.filter { it.isNotEmpty() }.map { JsonPrimitive(it) }))
        ControlKind.JSON -> {
            if (trimmed.isEmpty()) return if (row.nullable) ParsedValue.Ok(JsonNull) else ParsedValue.Error("Enter a JSON value.")
            try {
                ParsedValue.Ok(CaluJson.parseToJsonElement(trimmed))
            } catch (error: kotlinx.serialization.SerializationException) {
                ParsedValue.Error("Not valid JSON: ${error.message.orEmpty().lineSequence().firstOrNull().orEmpty()}")
            }
        }
        ControlKind.MARKDOWN -> ParsedValue.Ok(JsonPrimitive(input))
        else -> ParsedValue.Ok(JsonPrimitive(trimmed))
    }
}

/**
 * Convert the state of a checkbox into the JSON value for `ide.settings.set`.
 *
 * @param row The row's view (unused beyond its kind; present so both overloads read alike).
 * @param input The checkbox's state.
 * @return `Ok` with the boolean.
 */
@Suppress("UNUSED_PARAMETER")
fun parseControlValue(row: SettingsRowView, input: Boolean): ParsedValue = ParsedValue.Ok(JsonPrimitive(input))

// ------------------------------------------------------------------ requests

/** Longest key, role, model or project name a request takes, in characters. */
const val MAX_SETTING_NAME: Int = 300

/** Longest JSON text of a setting value a request takes, in characters. */
const val MAX_VALUE_JSON: Int = 2_000_000

/**
 * One request to the bridge (`CaluRemoteApi.call(projectId, method, paramsJson, session = false)`).
 *
 * @property method The bridge method, e.g. "ide.settings.set".
 * @property params Its params.
 */
data class BridgeRequest(val method: String, val params: JsonObject)

/** What an edit of a control leads to. */
sealed interface SettingEdit {
    /**
     * Send this request; the result's `value` is the value after calu's validation.
     *
     * @property request The `ide.settings.set` request.
     * @property value The value that is sent (to show at once, before the answer).
     */
    data class Send(val request: BridgeRequest, val value: JsonElement) : SettingEdit

    /**
     * Send nothing and show a message at the row.
     *
     * @property message The message.
     */
    data class Refused(val message: String) : SettingEdit
}

/**
 * Tell whether a text is a usable key, role, model or project name of a request.
 *
 * @param text The text.
 * @return True if it holds a non-blank character and has at most `MAX_SETTING_NAME` characters.
 */
private fun isName(text: String): Boolean = text.isNotBlank() && text.length <= MAX_SETTING_NAME

/**
 * Write the scope of a request.
 *
 * @param project The project's name; `null` or "" for calu's global settings.
 * @return The JSON value of the `project` member.
 */
private fun scope(project: String?): JsonElement = if (project.isNullOrEmpty()) JsonNull else JsonPrimitive(project)

/**
 * Build the request that loads the settings of a scope.
 *
 * @param project The project's name; `null` for the global settings.
 * @return `ide.settings.get {project}`.
 */
fun settingsGetRequest(project: String?): BridgeRequest = BridgeRequest("ide.settings.get", buildJsonObject { put("project", scope(project)) })

/**
 * Build the request that stores a value.
 *
 * @param key The setting's dotted key.
 * @param value The JSON value (JSON null stores "no value" for a nullable setting).
 * @param project The scope; `null` for the global settings.
 * @return `ide.settings.set {key, value, project}`; `null` if `key` or `project` is no usable
 *   name or the value's JSON text is longer than `MAX_VALUE_JSON`.
 */
fun settingsSetRequest(key: String, value: JsonElement, project: String?): BridgeRequest? {
    if (!isName(key) || (!project.isNullOrEmpty() && !isName(project)) || jsonText(value).length > MAX_VALUE_JSON) return null
    return BridgeRequest("ide.settings.set", buildJsonObject { put("key", JsonPrimitive(key)); put("value", value); put("project", scope(project)) })
}

/**
 * Build the request that removes the stored value of a setting ("reset to default"; in a
 * project the setting then inherits the global value). The dialog loads the settings again
 * afterwards.
 *
 * @param key The setting's dotted key.
 * @param project The scope; `null` for the global settings.
 * @return `ide.settings.unset {key, project}`; `null` if `key` or `project` is no usable name.
 */
fun settingsUnsetRequest(key: String, project: String?): BridgeRequest? {
    if (!isName(key) || (!project.isNullOrEmpty() && !isName(project))) return null
    return BridgeRequest("ide.settings.unset", buildJsonObject { put("key", JsonPrimitive(key)); put("project", scope(project)) })
}

/**
 * Turn an edit of a control into the request that stores it.
 *
 * @param row The edited row.
 * @param input The control's content (for `TOGGLE`: "true" or "false").
 * @param project The scope; `null` for the global settings.
 * @return `Send` with `ide.settings.set`, or `Refused` with the message of `parseControlValue`
 *   (or that the value is too large).
 */
fun settingEdit(row: SettingsRowView, input: String, project: String?): SettingEdit = when (val parsed = parseControlValue(row, input)) {
    is ParsedValue.Error -> SettingEdit.Refused(parsed.message)
    is ParsedValue.Ok -> settingsSetRequest(row.key, parsed.value, project)?.let { SettingEdit.Send(it, parsed.value) }
        ?: SettingEdit.Refused("The value is too large to store.")
}

/**
 * Turn a click on a checkbox into the request that stores it.
 *
 * @param row The edited row.
 * @param input The checkbox's new state.
 * @param project The scope; `null` for the global settings.
 * @return See the other overload.
 */
fun settingEdit(row: SettingsRowView, input: Boolean, project: String?): SettingEdit = settingEdit(row, if (input) "true" else "false", project)

// ------------------------------------------------------------------ setup items

/** Fix actions the Setup card can offer. */
val FIX_ACTIONS: List<String> =
    listOf("installCalu", "updateCalu", "startServer", "copyServerCommand", "copyRootInstall", "openClaudeDocs", "restartBridge")

/**
 * A fix button.
 *
 * @property action One of `FIX_ACTIONS`.
 * @property label The button's text.
 */
data class SetupFix(val action: String, val label: String)

/**
 * One finding of `ide.status` as the Setup card lists it.
 *
 * @property name What was checked, e.g. "claude", "uv", "git", "tmux".
 * @property level "ok", "missing" or "note".
 * @property message What was found.
 * @property advice What to do about it; "" if nothing.
 * @property fix The fix button of the row; `null` if the plugin has no fix for it.
 */
data class SetupItem(val name: String, val level: String, val message: String, val advice: String, val fix: SetupFix? = null)

/**
 * Turn the findings of `ide.status` into the rows of the Setup card.
 *
 * @param findings The `findings` of `ide.status` (untrusted: entries without a string `name`
 *   are dropped).
 * @return One item per finding in the given order. A missing `claude` gets the fix "How to
 *   install Claude Code" (the plugin never installs it); a finding named `calu` that is not ok
 *   gets "Update calu"; other findings get no button.
 */
fun setupItems(findings: List<JsonElement>?): List<SetupItem> = findings.orEmpty().mapNotNull { entry ->
    val f = entry.asObj() ?: return@mapNotNull null
    val name = f.str("name") ?: return@mapNotNull null
    val level = f.str("level").let { if (it == "missing" || it == "note") it else "ok" }
    val fix = when {
        level == "ok" -> null
        name == "claude" -> SetupFix("openClaudeDocs", "How to install Claude Code")
        name == "calu" -> SetupFix("updateCalu", "Update calu")
        else -> null
    }
    SetupItem(name, level, f.str("message").orEmpty(), f.str("advice").orEmpty(), fix)
}
