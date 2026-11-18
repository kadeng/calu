/**
 * Model of the session chooser (no IntelliJ Platform import): which entries it shows, in which
 * order, and what choosing one does.
 *
 *     ide.sessions ──buildSessionPicks──►  New session…
 *                                          ── Running in this workspace ──   -> ide.attach
 *                                          ── Other running sessions ──      -> ide.attach
 *                                          ── Stored conversations ──        -> ide.start resume | fork
 *
 * The `ide.sessions` result comes from the bridge and is read defensively: a member that is
 * missing or has another JSON type never raises an exception.
 *
 * Ported from `extensions/vscode/src/shared/sessionPick.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.jsTruthy
import io.github.kadeng.calu.json.num
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.sessionTitle
import kotlinx.serialization.json.JsonObject
import kotlin.math.floor

/** Most characters of a session's name (calu's limit). */
const val MAX_SESSION_NAME: Int = 80

/** The characters JavaScript's `\s` and `String.trim` treat as white space. */
private const val JS_SPACE = "\\s\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"

/** Matches one run of white space (see `oneLine`). */
private val SPACE_RUN = Regex("[$JS_SPACE]+")

/** Matches the white space at the start and at the end of a text (see `jsTrim`). */
private val OUTER_SPACE = Regex("^[$JS_SPACE]+|[$JS_SPACE]+$")

/**
 * Remove leading and trailing white space as JavaScript's `String.trim` does.
 *
 * @param text Any text.
 * @return The text without the white space at its start and end.
 */
private fun jsTrim(text: String): String = text.replace(OUTER_SPACE, "")

/** What choosing an entry of the session chooser does. */
sealed interface SessionChoice {
    /** Start a new session in the workspace folder. */
    object New : SessionChoice

    /**
     * Attach to a running session.
     *
     * @property sessionId Id of the session.
     */
    data class Attach(val sessionId: String) : SessionChoice

    /**
     * Resume the stored conversation, or (via the entry's second action) fork it.
     *
     * @property claudeSessionId Claude Code's id of the conversation.
     */
    data class Stored(val claudeSessionId: String) : SessionChoice
}

/** One row of the session chooser: an entry or a separator line. */
sealed interface SessionPick {
    /**
     * A separator line above a group of entries.
     *
     * @property label The group's title.
     */
    data class Separator(val label: String) : SessionPick

    /**
     * An entry the user can choose.
     *
     * @property label The entry's main text.
     * @property description Shown after the label, dimmed.
     * @property detail Second line.
     * @property choice What choosing the entry does.
     * @property current True for the session that is attached now.
     */
    data class Entry(
        val label: String,
        val description: String,
        val detail: String,
        val choice: SessionChoice,
        val current: Boolean,
    ) : SessionPick
}

/**
 * Format an age for a list.
 *
 * @param seconds Seconds (negative values count as 0).
 * @return "just now" below a minute, else e.g. "5 min ago", "3 h ago", "2 d ago".
 */
fun formatAge(seconds: Long): String {
    val s = maxOf(0L, seconds)
    if (s < 60) return "just now"
    if (s < 3600) return "${s / 60} min ago"
    if (s < 86400) return "${s / 3600} h ago"
    return "${s / 86400} d ago"
}

/**
 * Shorten a text to one line.
 *
 * @param text Any text.
 * @param limit Maximum length in UTF-16 code units (>= 1).
 * @return The text with white space runs as single spaces and without white space at its ends,
 *   cut to `limit` characters with "…" as the last one.
 */
fun oneLine(text: String, limit: Int = 100): String {
    val flat = jsTrim(text.replace(SPACE_RUN, " "))
    return if (flat.length <= limit) flat else "${flat.take(maxOf(0, limit - 1))}…"
}

/**
 * Build the row of a running session.
 *
 * @param s One element of `running` of the `ide.sessions` result, with a string member `id`.
 *   Other members may be missing: a missing `name`, `directory` or `state` counts as "", a
 *   missing `open_escalations` as 0.
 * @param id The session's id (`s.id`).
 * @param attachedId Id of the attached session, or `null`.
 * @return The row: label = name or directory name; description = state, open escalations,
 *   "stale" and "attached" marks; detail = directory and short id.
 */
private fun runningPick(s: JsonObject, id: String, attachedId: String?): SessionPick.Entry {
    val directory = s.str("directory") ?: ""
    val escalations = s.num("open_escalations") ?: 0.0
    val marks = listOf(
        s.str("state") ?: "",
        if (escalations > 0) "${formatCount(escalations)} waiting for a decision" else "",
        if (s["stale"].jsTruthy()) "stale (its process seems gone)" else "",
        if (id == attachedId) "attached" else "",
    ).filter { it.isNotEmpty() }
    return SessionPick.Entry(
        label = sessionTitle(id, s.str("name") ?: "", directory),
        description = marks.joinToString(" · "),
        detail = "$directory · ${id.take(8)}",
        choice = SessionChoice.Attach(id),
        current = id == attachedId,
    )
}

/**
 * Write a count for a list.
 *
 * @param value A finite number.
 * @return The number without ".0" if it has no fraction, else as Kotlin writes a `Double`.
 */
private fun formatCount(value: Double): String =
    if (value == floor(value) && !value.isInfinite()) value.toLong().toString() else value.toString()

/**
 * Build the row of a stored conversation.
 *
 * @param c One element of `stored` of the `ide.sessions` result, with a string member
 *   `claude_session_id`. A missing `last_input` counts as "", missing `turns` and `age_s` as 0.
 * @param claudeSessionId The conversation's id (`c.claude_session_id`).
 * @return The row: label = its last user input (one line); description = turns and age;
 *   detail = the Claude session id and whether it was started without calu.
 */
private fun storedPick(c: JsonObject, claudeSessionId: String): SessionPick.Entry {
    val turns = c.num("turns") ?: 0.0
    val age = c.num("age_s")?.takeIf { !it.isNaN() } ?: 0.0
    return SessionPick.Entry(
        label = oneLine(c.str("last_input") ?: "").ifEmpty { "(no user input)" },
        description = "${formatCount(turns)} turn${if (turns == 1.0) "" else "s"} · ${formatAge(floor(age).toLong())}",
        detail = "$claudeSessionId${if (c["outside"].jsTruthy()) " · started without calu" else ""}",
        choice = SessionChoice.Stored(claudeSessionId),
        current = false,
    )
}

/**
 * Build the rows of the session chooser.
 *
 * @param sessions The result of `ide.sessions` (untrusted: members may be missing or have
 *   another type). Elements of `running` without a string `id` and elements of `stored` without
 *   a string `claude_session_id` are skipped.
 * @param attachedId Id of the attached session, or `null`.
 * @return "New session…" first; then the running sessions of the workspace (`here` is true),
 *   the other running sessions, and the stored conversations that no running session uses, each
 *   group under a separator and omitted when empty.
 */
fun buildSessionPicks(sessions: JsonObject, attachedId: String?): List<SessionPick> {
    val running = sessions.arr("running").mapNotNull { e ->
        val s = e.asObj() ?: return@mapNotNull null
        val id = s.str("id") ?: return@mapNotNull null
        s to id
    }
    val stored = sessions.arr("stored").mapNotNull { e ->
        val c = e.asObj() ?: return@mapNotNull null
        val id = c.str("claude_session_id") ?: return@mapNotNull null
        if (c["running"].jsTruthy()) null else c to id
    }
    val picks = mutableListOf<SessionPick>(
        SessionPick.Entry(
            label = "New session…",
            description = "",
            detail = "Start a session in this workspace folder, with an optional first message",
            choice = SessionChoice.New,
            current = false,
        ),
    )
    val here = running.filter { (s, _) -> s["here"].jsTruthy() }
    val elsewhere = running.filter { (s, _) -> !s["here"].jsTruthy() }
    if (here.isNotEmpty()) {
        picks.add(SessionPick.Separator("Running in this workspace"))
        here.mapTo(picks) { (s, id) -> runningPick(s, id, attachedId) }
    }
    if (elsewhere.isNotEmpty()) {
        picks.add(SessionPick.Separator("Other running sessions"))
        elsewhere.mapTo(picks) { (s, id) -> runningPick(s, id, attachedId) }
    }
    if (stored.isNotEmpty()) {
        picks.add(SessionPick.Separator("Stored conversations (resume or fork)"))
        stored.mapTo(picks) { (c, id) -> storedPick(c, id) }
    }
    return picks
}

/**
 * Name a session that is cut from another one.
 *
 * Example: `derivedSessionName("rewind", "parser work") == "rewind of parser work"`.
 *
 * @param how "fork" (a copy that continues beside the original) or "rewind" (the conversation
 *   from before one of its turns).
 * @param from The display name of the session it is cut from (its name, or its directory's
 *   last segment).
 * @return "fork of <from>" or "rewind of <from>", cut to `MAX_SESSION_NAME` characters with
 *   "…"; "" if `from` is empty or only white space.
 */
fun derivedSessionName(how: String, from: String): String {
    val base = jsTrim(from)
    if (base.isEmpty()) return ""
    val name = "$how of $base"
    return if (name.length <= MAX_SESSION_NAME) name else "${name.take(MAX_SESSION_NAME - 1)}…"
}

/**
 * Check a session name entered by the user.
 *
 * @param name The text of the input field.
 * @return `null` if the name is acceptable (at most 80 characters after trimming; empty
 *   removes the name), else the message to show.
 */
fun validateSessionName(name: String): String? =
    if (jsTrim(name).length > MAX_SESSION_NAME) "A session name has at most 80 characters." else null
