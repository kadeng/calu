/**
 * Recording the user's own saves as diffs (agent/records/PLAN.md D253): the parts without the
 * IDE, shared with the tests. The backend's `HumanDiffSaveListener` uses them.
 *
 *     fileContentLoaded / fileContentReloaded / a save ──► SaveBases: base = the document's text
 *     beforeDocumentSaving ── gate.wanted()? ── before: the base (the text the buffer started
 *                             from; if unknown, the file as the VFS holds it), after: the
 *                             document's text
 *                          ── humanDiffParams ──► session.call human_diffs.add {path, before, after}
 *     answer {enabled: false} ──► gate.heard(false): nothing is sent for OFF_PAUSE_MS
 *
 * The session decides everything else (the setting, git-tracked files only, the file layout).
 *
 * The base is not the file on disk at the save: when Claude wrote the file while the buffer had
 * unsaved edits and the user kept them ("Keep Memory Changes", then a save that overwrites
 * Claude's version), the diff shows only the user's own edits (the owner's decision, D253).
 */
package io.github.kadeng.calu.logic

import java.util.Collections
import java.util.WeakHashMap
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull

/** Milliseconds the plugin sends nothing after the session said the feature is off. */
const val HUMAN_DIFF_OFF_PAUSE_MS: Long = 60_000

/** The longest text the plugin sends as `before` or `after` (the session's own limit). */
const val HUMAN_DIFF_MAX_CHARS: Int = 1_000_000

/** Whether saves are worth sending: not while the session said the feature is off. */
class HumanDiffGate {
    @Volatile private var offUntil = 0L

    /**
     * Tell whether a save should be sent now.
     *
     * @param attached whether a session is attached.
     * @param now the current time in milliseconds.
     * @return true if a session is attached and no "off" answer arrived in the last
     *   `HUMAN_DIFF_OFF_PAUSE_MS`.
     */
    fun wanted(attached: Boolean, now: Long): Boolean = attached && now >= offUntil

    /**
     * Take note of the session's answer.
     *
     * @param enabled `enabled` of the answer.
     * @param now the current time in milliseconds.
     */
    fun heard(enabled: Boolean, now: Long) {
        offUntil = if (enabled) 0L else now + HUMAN_DIFF_OFF_PAUSE_MS
    }
}

/**
 * The text each loaded document started from: its text when the IDE last loaded it, reloaded it
 * from disk or saved it, without the user's unsaved edits since. Thread-safe; the keys are held
 * weakly (a document the IDE drops is forgotten, and loading it again sets a new base).
 *
 * Example: `loaded(d, "v1")`; the user types; Claude writes "v2" on disk and the user keeps the
 * memory changes; `saving(d, "v1+mine")` returns "v1". Had the user loaded Claude's version
 * (`loaded(d, "v2")`), it would return "v2".
 *
 * @param K the document type (the IDE's `Document`; strings in tests).
 */
class SaveBases<K : Any> {
    private val texts: MutableMap<K, CharSequence> = Collections.synchronizedMap(WeakHashMap())

    /**
     * Take note of a load, a reload from disk, or a save that is not the user's: `text` becomes
     * the base.
     *
     * @param key the document.
     * @param text its text now; pass an immutable snapshot (`Document.immutableCharSequence`),
     *   which is kept without a copy.
     */
    fun loaded(key: K, text: CharSequence) {
        texts[key] = text
    }

    /**
     * Take note of a save of the user: return the base before it, and make `text` the new base.
     *
     * @param key the document.
     * @param text the text being saved (an immutable snapshot, as for `loaded`).
     * @return the base before the save, or null if the document was loaded before this tracker
     *   heard of it.
     */
    fun saving(key: K, text: CharSequence): CharSequence? = texts.put(key, text)
}

/**
 * Replace "\r\n" and lone "\r" by "\n".
 *
 * @param text any text.
 * @return the text with "\n" line ends only.
 */
fun normaliseLineEnds(text: String): String = text.replace("\r\n", "\n").replace('\r', '\n')

/**
 * Build the params of `human_diffs.add` for one save, or decide that it is not sent.
 *
 * @param root the project directory, absolute ("/" or "\" separators).
 * @param file the saved file, absolute.
 * @param before the text before the save, or null if unknown.
 * @param after the saved text.
 * @return the params `{path, before, after}`; null if the file is not inside `root`, `before`
 *   is unknown, a text is longer than `HUMAN_DIFF_MAX_CHARS`, or the texts are equal after
 *   normalising the line ends. Example: ("/w", "/w/doc/a.md", "a\r\n", "b\n") ->
 *   {path: "doc/a.md", before: "a\n", after: "b\n"}.
 */
fun humanDiffParams(root: String, file: String, before: String?, after: String): JsonObject? {
    if (before == null || before.length > HUMAN_DIFF_MAX_CHARS || after.length > HUMAN_DIFF_MAX_CHARS) return null
    val base = root.replace('\\', '/').trimEnd('/')
    val target = file.replace('\\', '/')
    if (!target.startsWith("$base/")) return null
    val path = target.substring(base.length + 1)
    if (path.isEmpty() || path.split('/').any { it == ".." || it.isEmpty() }) return null
    val old = normaliseLineEnds(before)
    val saved = normaliseLineEnds(after)
    if (old == saved) return null
    return buildJsonObject {
        put("path", JsonPrimitive(path))
        put("before", JsonPrimitive(old))
        put("after", JsonPrimitive(saved))
    }
}

/**
 * Read `enabled` from an answer of `human_diffs.add`.
 *
 * @param answer the answer.
 * @return its `enabled`; true if it is missing (an unknown answer does not pause the sending).
 */
fun humanDiffEnabled(answer: JsonElement?): Boolean =
    ((answer as? JsonObject)?.get("enabled") as? JsonPrimitive)?.booleanOrNull ?: true

/**
 * The offer of an `ide.settings.set` answer: a related switch to turn on as well (D253).
 *
 * @property key the dotted key of the related switch, e.g. "traces.enabled".
 * @property text the question to ask the user.
 */
data class EnableOffer(val key: String, val text: String)

/**
 * Read the offer from an `ide.settings.set` answer.
 *
 * @param answer the answer.
 * @return the offer, or null if the answer has none (or a malformed one).
 */
fun enableOffer(answer: JsonObject?): EnableOffer? {
    val offer = answer?.get("offer") as? JsonObject ?: return null
    val key = (offer["key"] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull ?: return null
    val text = (offer["text"] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull ?: return null
    return if (key.isEmpty()) null else EnableOffer(key, text)
}
