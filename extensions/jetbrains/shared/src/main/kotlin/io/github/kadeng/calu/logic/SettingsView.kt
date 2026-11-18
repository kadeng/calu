/**
 * Pure view logic of the settings dialog that is not about single setting rows (those are in
 * `SettingsModel.kt`): the model presets, the Setup card, the Honesty card, search highlighting
 * and the bookkeeping of optimistic saves.
 *
 *     ide.models.presets ──parseModels──► ModelsInfo
 *                        ──groupPresets──────► preset cards by provider
 *                        ──selectedPresetId──► which card is marked (all roles equal its model)
 *     a click on a preset ("one model for everything") ──modelsSetAllRequest──► ide.models.set_all
 *     a model of one role                              ──modelsSetRequest────► ide.models.set
 *     ide.status + problems ──setupRows──────► rows of the Setup card with fix buttons
 *     honesty.mode description ──modeSentences──► one sentence per mode
 *     search text ──highlightRanges(text)────► [start, end) ranges to mark in a label
 *
 *     user edit ──PendingEdits.begin──► optimistic value shown ──┬─ saved ──► settle(value)
 *                                                               └─ error ──► fail ──► roll back
 *
 * Port of `extensions/vscode/src/shared/settingsView.ts` and of the model requests of
 * `settingsMessages.ts` / `settingsPanel.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.ServerHelp
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

// ------------------------------------------------------------------ model presets

/**
 * One model preset (`ide.models.presets`.presets).
 *
 * @property id The preset's id, e.g. "default".
 * @property label The text of its card.
 * @property group The provider, e.g. "Anthropic"; "" if none.
 * @property model The model string it stores; "" for the preset that stores none (calu's
 *   default model).
 * @property note What the preset needs (which key or sign-in).
 */
data class ModelPreset(val id: String, val label: String, val group: String, val model: String, val note: String)

/**
 * A role that has a model (`ide.models.presets`.roles).
 *
 * @property key Key for `ide.models.set`.
 * @property label The role's name for the user.
 * @property model The stored model; "" means the default model.
 */
data class ModelRole(val key: String, val label: String, val model: String)

/**
 * The result of `ide.models.presets`.
 *
 * @property presets The presets in calu's order.
 * @property roles The roles with their stored models.
 * @property claudeModel Claude Code's own model (`claude.model`); `null` if none is stored.
 * @property claudeEffort Claude Code's effort (`claude.effort`); `null` if none is stored.
 * @property useBedrock `claude.use_bedrock`.
 * @property defaultModel The model a role without a stored model uses; "" if unknown.
 */
data class ModelsInfo(
    val presets: List<ModelPreset> = emptyList(),
    val roles: List<ModelRole> = emptyList(),
    val claudeModel: String? = null,
    val claudeEffort: String? = null,
    val useBedrock: Boolean = false,
    val defaultModel: String = "",
)

/**
 * Read the result of `ide.models.presets` defensively.
 *
 * @param result The result (untrusted), or `null`.
 * @return The models; presets without a non-empty string `id` and roles without a string `key`
 *   are dropped, missing texts are "".
 */
fun parseModels(result: JsonObject?): ModelsInfo {
    if (result == null) return ModelsInfo()
    val claude = result.obj("claude")
    return ModelsInfo(
        presets = result.arr("presets").mapNotNull { entry ->
            val o = entry.asObj() ?: return@mapNotNull null
            val id = o.str("id")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            ModelPreset(id, o.str("label") ?: id, o.str("group").orEmpty(), o.str("model").orEmpty(), o.str("note").orEmpty())
        },
        roles = result.arr("roles").mapNotNull { entry ->
            val o = entry.asObj() ?: return@mapNotNull null
            val key = o.str("key")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            ModelRole(key, o.str("label") ?: key, o.str("model").orEmpty())
        },
        claudeModel = claude?.str("model"),
        claudeEffort = claude?.str("effort"),
        useBedrock = claude?.bool("use_bedrock") == true,
        defaultModel = result.str("default_model").orEmpty(),
    )
}

/**
 * Presets of one provider, for one heading of the preset cards.
 *
 * @property group The provider, e.g. "Anthropic"; "Other" for presets without a group.
 * @property presets Its presets in calu's order.
 */
data class PresetGroup(val group: String, val presets: List<ModelPreset>)

/**
 * Group the presets by provider.
 *
 * @param presets The presets in calu's order.
 * @return The groups in the order their first preset appears; inside a group the presets keep
 *   their order.
 */
fun groupPresets(presets: List<ModelPreset>): List<PresetGroup> =
    presets.groupByTo(LinkedHashMap()) { it.group.ifEmpty { "Other" } }.map { (group, list) -> PresetGroup(group, list) }

/** What the roles' models have in common. */
sealed interface RolesState {
    /** There are no roles (e.g. no database login). */
    data object None : RolesState

    /**
     * Every role stores the same model.
     *
     * @property model The model; "" means none is stored (the default model).
     */
    data class Uniform(val model: String) : RolesState

    /** At least two roles store different models. */
    data object Mixed : RolesState
}

/**
 * Tell whether all roles use the same model.
 *
 * @param roles The roles of `ide.models.presets`.
 * @return See `RolesState`. Models are compared after trimming.
 */
fun rolesState(roles: List<ModelRole>): RolesState {
    if (roles.isEmpty()) return RolesState.None
    val first = roles[0].model.trim()
    return if (roles.all { it.model.trim() == first }) RolesState.Uniform(first) else RolesState.Mixed
}

/**
 * Find the preset that stores a given model.
 *
 * @param presets The presets.
 * @param model A model string; "" finds the preset that stores no model.
 * @return The first preset whose model equals `model` (trimmed), or `null`.
 */
fun presetForModel(presets: List<ModelPreset>, model: String): ModelPreset? = presets.firstOrNull { it.model == model.trim() }

/**
 * Decide which preset card is marked as the current state.
 *
 * Example: with one role that stores no model the result is the id of the default preset.
 *
 * @param presets The presets.
 * @param roles The roles with their stored models.
 * @return The id of the preset whose model every role stores (the default preset when no role
 *   stores a model); `null` if the roles differ, there are no roles, or their common model is
 *   not a preset (a custom model).
 */
fun selectedPresetId(presets: List<ModelPreset>, roles: List<ModelRole>): String? =
    (rolesState(roles) as? RolesState.Uniform)?.let { presetForModel(presets, it.model)?.id }

/**
 * Describe the model of one role for the "Per role" table.
 *
 * @param model The role's stored model ("" = none).
 * @param presets The presets (for their labels).
 * @param defaultModel The model a role without a stored model uses; "" if unknown.
 * @return The preset's label or the model string; for no stored model "default → <model>",
 *   or "default" if the default model is unknown.
 */
fun roleModelText(model: String, presets: List<ModelPreset>, defaultModel: String): String {
    val stored = model.trim()
    if (stored.isEmpty()) return if (defaultModel.isNotEmpty()) "default → $defaultModel" else "default"
    return presetForModel(presets, stored)?.label ?: stored
}

/**
 * Write the scope of a request.
 *
 * @param project The project's name; `null` or "" for calu's global settings.
 * @return The JSON value of the `project` member.
 */
private fun scopeOf(project: String?): JsonElement = if (project.isNullOrEmpty()) JsonNull else JsonPrimitive(project)

/**
 * Build the request that loads the presets and the roles of a scope.
 *
 * @param project The project's name; `null` for the global settings.
 * @return `ide.models.presets {project}`.
 */
fun modelsPresetsRequest(project: String?): BridgeRequest = BridgeRequest("ide.models.presets", buildJsonObject { put("project", scopeOf(project)) })

/**
 * Build the request "use one model for everything": every role gets the model. The dialog
 * loads the settings and the presets again afterwards.
 *
 * @param model A model string (a preset's `model`), or "default" for the preset that stores
 *   none; white space at its ends is dropped.
 * @param effort Effort of Claude Code's own model; `null` to leave it as it is.
 * @param project The scope; `null` for the global settings.
 * @return `ide.models.set_all {model, effort?, project}`; `null` if `model` is blank or a
 *   text is longer than `MAX_SETTING_NAME`.
 */
fun modelsSetAllRequest(model: String, effort: String?, project: String?): BridgeRequest? {
    val name = model.trim()
    if (name.isEmpty() || model.length > MAX_SETTING_NAME) return null
    if (effort != null && (effort.isBlank() || effort.length > MAX_SETTING_NAME)) return null
    if (!project.isNullOrEmpty() && (project.isBlank() || project.length > MAX_SETTING_NAME)) return null
    return BridgeRequest(
        "ide.models.set_all",
        buildJsonObject {
            put("model", JsonPrimitive(name))
            effort?.let { put("effort", JsonPrimitive(it)) }
            put("project", scopeOf(project))
        },
    )
}

/**
 * Build the request that applies a preset to every role.
 *
 * @param preset The chosen preset.
 * @param effort Effort of Claude Code's own model; `null` to leave it as it is.
 * @param project The scope; `null` for the global settings.
 * @return `ide.models.set_all` with the preset's model, or with "default" for the preset that
 *   stores none.
 */
fun presetRequest(preset: ModelPreset, effort: String?, project: String?): BridgeRequest? =
    modelsSetAllRequest(preset.model.ifEmpty { "default" }, effort, project)

/**
 * Build the request that sets the model of one role. The dialog loads the presets again
 * afterwards.
 *
 * @param role A `ModelRole.key`.
 * @param model The model string; "" stores none (the role then uses the default model).
 * @param project The scope; `null` for the global settings.
 * @return `ide.models.set {role, model, project}`; `null` if `role` is blank or a text is
 *   longer than `MAX_SETTING_NAME`.
 */
fun modelsSetRequest(role: String, model: String, project: String?): BridgeRequest? {
    if (role.isBlank() || role.length > MAX_SETTING_NAME || model.length > MAX_SETTING_NAME) return null
    if (!project.isNullOrEmpty() && (project.isBlank() || project.length > MAX_SETTING_NAME)) return null
    return BridgeRequest(
        "ide.models.set",
        buildJsonObject { put("role", JsonPrimitive(role)); put("model", JsonPrimitive(model.trim())); put("project", scopeOf(project)) },
    )
}

// ------------------------------------------------------------------ search

private val SEARCH_SPACES = Regex("\\s+")

/**
 * Split a search text into the words that are looked for in texts.
 *
 * @param search The search text of the dialog.
 * @return Its words in lower case, without filter words that start with "@" (`@modified`).
 */
fun searchWords(search: String): List<String> = search.lowercase().split(SEARCH_SPACES).filter { it.isNotEmpty() && !it.startsWith("@") }

/**
 * Find the parts of a text that a search matches, for highlighting.
 *
 * Example: `highlightRanges("Stream partial", "art str") == listOf(0 until 3, 8 until 11)`.
 *
 * @param text The text shown (a label, key or description).
 * @param search The search text; each word is looked for separately, ignoring case.
 * @return The ranges of all occurrences of all words, sorted by start, with overlapping and
 *   touching ranges merged. Empty for an empty search.
 */
fun highlightRanges(text: String, search: String): List<IntRange> {
    val hay = text.lowercase()
    val found = ArrayList<Pair<Int, Int>>()
    for (word in searchWords(search)) {
        var at = hay.indexOf(word)
        while (at != -1) {
            found.add(at to at + word.length)
            at = hay.indexOf(word, at + word.length)
        }
    }
    found.sortWith(compareBy({ it.first }, { it.second }))
    val merged = ArrayList<Pair<Int, Int>>()
    for (range in found) {
        val last = merged.lastOrNull()
        if (last != null && range.first <= last.second) merged[merged.size - 1] = last.first to maxOf(last.second, range.second)
        else merged.add(range)
    }
    return merged.map { it.first until it.second }
}

/**
 * Build the search text that `matchesSearch` gets from the search field and the filter chip.
 *
 * @param search The text of the search field.
 * @param onlyModified True if "Show only modified" is on.
 * @return `search` with single spaces, with the word "@modified" added if the chip is on and
 *   the text lacks it.
 */
fun effectiveSearch(search: String, onlyModified: Boolean): String {
    val words = search.split(SEARCH_SPACES).filter { it.isNotEmpty() }
    return (if (onlyModified && words.none { it.lowercase() == "@modified" }) words + "@modified" else words).joinToString(" ")
}

/**
 * Tell whether a card (Setup, Models, Checkers, Honesty) stays visible during a search.
 *
 * @param keywords Words that describe the card, e.g. "models presets anthropic bedrock".
 * @param search The effective search text (see `effectiveSearch`).
 * @return True for an empty search; false if the search holds a filter word ("@modified":
 *   cards have no modified state); else true if every word occurs in `keywords`.
 */
fun cardMatches(keywords: String, search: String): Boolean {
    val all = search.lowercase().split(SEARCH_SPACES).filter { it.isNotEmpty() }
    if (all.isEmpty()) return true
    if (all.any { it.startsWith("@") }) return false
    val hay = keywords.lowercase()
    return all.all { hay.contains(it) }
}

/**
 * What the dialog's status text says about the saves.
 *
 * @property text The text; "" while there is nothing to say.
 * @property kind "saving" (a spinner), "saved" (a check mark), "error" (an error mark) or "idle".
 */
data class SaveSummary(val text: String, val kind: String)

/**
 * Summarise the saves of the settings dialog. Each setting has its own state (its row shows an
 * error or the stored value); this text is the summary and never says "not saved" for all of
 * them when only some failed.
 *
 *     a save is on its way                   -> "Saving…"
 *     all settled, some refused, some saved  -> "2 saved · 1 not saved"
 *     all settled, some refused              -> "1 setting not saved"
 *     all settled, saved a moment ago        -> "Saved"
 *     otherwise                              -> ""
 *
 * @param pending Number of settings whose save has no answer yet.
 * @param saved Number of settings saved in the last moment (the dialog forgets them after a
 *   few seconds).
 * @param failed Number of settings whose last save calu refused and that were not saved since.
 * @return The text and its kind.
 */
fun saveSummary(pending: Int, saved: Int, failed: Int): SaveSummary = when {
    pending > 0 -> SaveSummary("Saving…", "saving")
    failed > 0 && saved > 0 -> SaveSummary("$saved saved · $failed not saved", "error")
    failed > 0 -> SaveSummary("$failed setting${if (failed == 1) "" else "s"} not saved", "error")
    saved > 0 -> SaveSummary("Saved", "saved")
    else -> SaveSummary("", "idle")
}

private val BLANK_LINE = Regex("\\n\\s*\\n")
private val LINE_BREAK = Regex("\\s*\\n\\s*")

/**
 * Split a description into paragraphs.
 *
 * @param text The description of a setting (plain text; blank lines separate paragraphs).
 * @return The non-empty paragraphs with single line breaks replaced by spaces.
 */
fun paragraphs(text: String): List<String> = text.split(BLANK_LINE).map { it.replace(LINE_BREAK, " ").trim() }.filter { it.isNotEmpty() }

// ------------------------------------------------------------------ setup card

/**
 * One row of the Setup card.
 *
 * @property name Stable name, e.g. "claude", "database", "server".
 * @property title Text shown as the row's title, e.g. "Claude Code".
 * @property level "ok", "missing" or "note".
 * @property message What was found.
 * @property advice What to do about it; "" if nothing.
 * @property fixes Fix buttons, the most useful first; empty if the plugin has no fix.
 */
data class SetupRow(val name: String, val title: String, val level: String, val message: String, val advice: String, val fixes: List<SetupFix>)

/** Titles of well-known findings. */
private val SETUP_TITLES: Map<String, String> = mapOf(
    "calu" to "calu", "claude" to "Claude Code", "uv" to "uv", "git" to "git", "tmux" to "tmux", "screen" to "screen",
    "database" to "Database", "server" to "calu server",
)

private val CALU_PROBLEM = Regex("^calu:\\s*", RegexOption.IGNORE_CASE)

/**
 * Build the rows of the Setup card.
 *
 * @param status The result of `ide.status`; `null` if it failed.
 * @param serverHelp How to start the server; `null` if unknown.
 * @param problems Problem lines of the host; a line that starts with "calu:" says the bridge
 *   did not start.
 * @return With a status: one row per finding (see `setupItems`), then "database" (not
 *   reachable: level "missing" with the problem as message) and "server" (no running session
 *   and no finding named "server": level "note" with the fix "startServer" if a server
 *   command is known). Without a status: one row "calu" of level "missing" with the fixes
 *   "installCalu" and "restartBridge", whose message is the host's "calu:" problem line if
 *   there is one. A missing `claude` never gets an install fix.
 */
fun setupRows(status: JsonObject?, serverHelp: ServerHelp?, problems: List<String> = emptyList()): List<SetupRow> {
    if (status == null) {
        val line = problems.firstOrNull { CALU_PROBLEM.containsMatchIn(it) }
        return listOf(
            SetupRow(
                "calu", "calu", "missing",
                line?.replaceFirst(CALU_PROBLEM, "") ?: "calu did not answer, so the setup could not be checked.",
                "Install calu, or start its bridge again if it is installed.",
                listOf(SetupFix("installCalu", "Install calu"), SetupFix("restartBridge", "Start again")),
            ),
        )
    }
    val rows = setupItems(status.arr("findings")).mapTo(ArrayList()) {
        SetupRow(it.name, SETUP_TITLES[it.name] ?: it.name, it.level, it.message, it.advice, listOfNotNull(it.fix))
    }
    val names = rows.map { it.name }.toSet()
    val database = status.obj("database")
    if (database != null && "database" !in names) {
        val reachable = database.bool("reachable")
        val place = database.str("place")?.takeIf { it.isNotEmpty() }?.let { " ($it)" }.orEmpty()
        rows.add(
            SetupRow(
                "database", "Database", if (reachable) "ok" else "missing",
                if (reachable) "Reachable$place." else database.str("problem")?.takeIf { it.isNotEmpty() } ?: "Not reachable$place.",
                "", emptyList(),
            ),
        )
    }
    val server = status.obj("server")
    if (server != null && "server" !in names) {
        val running = server.int("sessions_running") ?: 0
        rows.add(
            SetupRow(
                "server", "calu server", if (running > 0) "ok" else "note",
                if (running > 0) "$running session${if (running == 1) "" else "s"} running on this machine." else "No session is running on this machine.",
                if (running > 0) "" else "New sessions need a running calu server; the command is below.",
                if (running > 0 || serverHelp == null) emptyList() else listOf(SetupFix("startServer", "Open terminal with the server command")),
            ),
        )
    }
    return rows
}

/**
 * Summary of the Setup card.
 *
 * @property ok True if no row is "missing": the card collapses to one line.
 * @property text The one-line text, e.g. "Everything is in place".
 */
data class SetupSummary(val ok: Boolean, val text: String)

/**
 * Summarise the Setup card in one line.
 *
 * @param rows The rows of `setupRows`.
 * @return `ok` is true if no row has the level "missing". The text is "Everything is in
 *   place" (with the number of notes if there are any), or how many things are missing.
 */
fun setupSummary(rows: List<SetupRow>): SetupSummary {
    val missing = rows.count { it.level == "missing" }
    val notes = rows.count { it.level == "note" }
    if (missing > 0) return SetupSummary(false, "$missing thing${if (missing == 1) " needs" else "s need"} attention")
    return SetupSummary(true, if (notes > 0) "Everything is in place ($notes note${if (notes == 1) "" else "s"})" else "Everything is in place")
}

// ------------------------------------------------------------------ honesty card

private val TRAILING_DEFAULT = Regex("\\s*Default \\S+\\.?\\s*$")

/**
 * Split the description of a choice setting into one text per choice.
 *
 * The description of `honesty.mode` has the form "`off`: text. `track`: text. `enforce`: text.
 * Default `off`." (with or without the backticks).
 *
 * Example: `modeSentences("`a`: first. `b`: second. Default `a`.", listOf("a", "b"))` is
 * `{a: "first.", b: "second."}`.
 *
 * @param description The setting's description.
 * @param choices The allowed values, e.g. `["off", "track", "enforce"]`.
 * @return For each choice whose marker "<choice>:" occurs in the description (at the start or
 *   after white space, optionally in backticks): the text up to the next choice's marker,
 *   without backticks and without a trailing "Default <value>." sentence. Choices without a
 *   marker are absent from the result. The entries are in the order of the markers.
 */
fun modeSentences(description: String, choices: List<String>): Map<String, String> {
    data class Mark(val choice: String, val start: Int, val end: Int)
    val marks = choices.filter { it.isNotEmpty() }.mapNotNull { choice ->
        val match = Regex("(^|\\s)`?${Regex.escape(choice)}`?:\\s").find(description) ?: return@mapNotNull null
        Mark(choice, match.range.first + match.groupValues[1].length, match.range.last + 1)
    }.sortedBy { it.start }
    val result = LinkedHashMap<String, String>()
    marks.forEachIndexed { index, mark ->
        val stop = if (index + 1 < marks.size) marks[index + 1].start else description.length
        result[mark.choice] = description.substring(mark.end, stop).replace("`", "").replace(SEARCH_SPACES, " ").replace(TRAILING_DEFAULT, "").trim()
    }
    return result
}

// ------------------------------------------------------------------ optimistic saves

/**
 * What a row showed before an edit: the state to return to when the edit is refused.
 *
 * @property value The row's value.
 * @property isSet `isSet` of the row.
 */
data class RowState(val value: JsonElement, val isSet: Boolean)

/**
 * Bookkeeping of edits that were sent to the bridge and not answered yet. Not thread-safe:
 * the dialog calls it from one thread (the EDT).
 *
 * The dialog shows an edit at once (optimistic). Per key this class remembers the state the
 * bridge last confirmed and how many requests are open:
 *
 *     begin(key, shownBefore, optimistic)   open += 1; confirmed = shownBefore (first edit only)
 *     settle(key, stateFromBridge)          open -= 1; confirmed = stateFromBridge
 *     fail(key)                             open -= 1; returns confirmed when open reaches 0
 *
 * Invariant: `isPending(key)` is true exactly while more `begin` than `settle` + `fail` calls
 * were made for the key.
 */
class PendingEdits {
    /**
     * The open edits of one key.
     *
     * @property open Number of requests without an answer.
     * @property confirmed The state the bridge last confirmed.
     * @property optimistic The state of the latest edit.
     */
    private class Entry(var open: Int, var confirmed: RowState, var optimistic: RowState)

    private val entries = HashMap<String, Entry>()

    /**
     * Note that an edit was sent.
     *
     * @param key The setting.
     * @param before What the row showed before the edit.
     * @param optimistic What the row shows now.
     */
    fun begin(key: String, before: RowState, optimistic: RowState) {
        val entry = entries[key]
        if (entry != null) {
            entry.open += 1
            entry.optimistic = optimistic
        } else {
            entries[key] = Entry(1, before, optimistic)
        }
    }

    /**
     * Note that the bridge stored an edit.
     *
     * @param key The setting.
     * @param stored The state the bridge reported.
     * @return The state the row must show: `stored` if no other edit of the key is open, else
     *   the optimistic state of the latest edit. `stored` also for a key without an open edit.
     */
    fun settle(key: String, stored: RowState): RowState {
        val entry = entries[key] ?: return stored
        entry.open -= 1
        entry.confirmed = stored
        if (entry.open > 0) return entry.optimistic
        entries.remove(key)
        return stored
    }

    /**
     * Note that the bridge refused an edit.
     *
     * @param key The setting.
     * @return The state to roll back to (the last confirmed one) if no other edit of the key is
     *   open; `null` if another edit is still open or the key has no open edit.
     */
    fun fail(key: String): RowState? {
        val entry = entries[key] ?: return null
        entry.open -= 1
        if (entry.open > 0) return null
        entries.remove(key)
        return entry.confirmed
    }

    /**
     * Return the optimistic state of a key, for re-applying it to freshly loaded data.
     *
     * @param key The setting.
     * @return The state of the latest open edit, or `null` if none is open.
     */
    fun optimistic(key: String): RowState? = entries[key]?.optimistic

    /**
     * Tell whether an edit of a key is open.
     *
     * @param key The setting.
     * @return True while a `begin` of the key has no `settle` or `fail` yet.
     */
    fun isPending(key: String): Boolean = entries.containsKey(key)

    /** Number of keys with an open edit. */
    val size: Int get() = entries.size

    /** Forget all open edits (after a scope switch: their answers belong to the old scope). */
    fun clear() = entries.clear()
}

/**
 * Return the rows with one row's state replaced.
 *
 * @param rows The rows of `ide.settings.get`.
 * @param key The setting to change.
 * @param state The new value and `isSet`.
 * @return A new list; the row with `key` is a copy with `value` and `isSet` from `state`, all
 *   other rows are the same objects.
 */
fun withRowState(rows: List<SettingRow>, key: String, state: RowState): List<SettingRow> =
    rows.map { if (it.key == key) it.copy(value = state.value, isSet = state.isSet) else it }

/**
 * Compare two JSON values.
 *
 * @param a A JSON value; `null` counts as JSON null.
 * @param b A JSON value; `null` counts as JSON null.
 * @return True if both serialise to the same JSON text (object members in the same order).
 */
fun sameJson(a: JsonElement?, b: JsonElement?): Boolean =
    CaluJson.encodeToString(JsonElement.serializer(), a ?: JsonNull) == CaluJson.encodeToString(JsonElement.serializer(), b ?: JsonNull)
