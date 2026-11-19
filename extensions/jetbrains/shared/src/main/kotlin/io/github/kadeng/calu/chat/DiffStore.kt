package io.github.kadeng.calu.chat

import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** Most diffs kept; the oldest are dropped (the VSCode extension keeps the same number). */
const val MAX_DIFFS: Int = 300

/**
 * Before and after of one edit, as calu sent it with `edit.diff` when it checked the edit.
 *
 * @property toolUseId The tool call that made the edit.
 * @property path The file: relative to the session's directory, or absolute outside it.
 * @property before The file's content before the edit ("" for a new file).
 * @property after The file's content after the edit.
 * @property startLine First changed line in `after` (1-based), or `null` if unknown.
 */
@Serializable
data class DiffData(val toolUseId: String, val path: String, val before: String, val after: String, val startLine: Int? = null)

/**
 * Keeps the newest `MAX_DIFFS` diffs of the attached session by tool call id. Thread-safe.
 * The diffs are not part of the view state: they can be large, and only a click needs them.
 */
class DiffStore {
    private val diffs = LinkedHashMap<String, DiffData>()

    /**
     * Remember the diff of an `edit.diff` event; an event without string `tool_use_id`, `path`,
     * `before` and `after` is ignored. A second diff of the same call replaces the first.
     *
     * @param params The event's params.
     */
    @Synchronized
    fun record(params: JsonObject) {
        val id = params.str("tool_use_id") ?: return
        val diff = DiffData(id, params.str("path") ?: return, params.str("before") ?: return, params.str("after") ?: return, params.int("start_line"))
        diffs.remove(id)
        diffs[id] = diff
        while (diffs.size > MAX_DIFFS) diffs.remove(diffs.keys.first())
    }

    /**
     * Look a diff up.
     *
     * @param toolUseId The tool call's id.
     * @return The diff, or `null` if none is held (never recorded, or dropped as too old).
     */
    @Synchronized
    fun get(toolUseId: String): DiffData? = diffs[toolUseId]

    /**
     * Find the newest held diff one of whose sides has exactly this text. The comment action
     * of a diff in remote development uses it: there the client's diff editor knows its text
     * but not which file it shows.
     *
     * @param text The whole text of the editor the selection was made in.
     * @return The diff and the side ("after" is preferred when both sides are equal), or
     *   `null` if no held diff has that text on either side.
     */
    @Synchronized
    fun findByText(text: String): Pair<DiffData, String>? {
        for (diff in diffs.values.reversed()) {
            if (diff.after == text) return diff to "after"
            if (diff.before == text) return diff to "before"
        }
        return null
    }

    /** Forget every diff (another session is attached). */
    @Synchronized
    fun clear() = diffs.clear()
}
