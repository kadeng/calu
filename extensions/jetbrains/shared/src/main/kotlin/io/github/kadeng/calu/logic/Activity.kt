/**
 * The activity line of the chat (pure): what Claude is doing right now.
 *
 * The line has three columns, so that changing spinner glyphs, verbs and digits never move the
 * other parts:
 *
 *     | icon | label (LABEL_COLUMN_CH wide) | elapsed |
 *     | ✻    | Combobulating…               | 12s     |   busy (spinner and verb change)
 *     | ‖    | Waiting for you…             | 40s     |   waiting (an approval is open)
 *     | ✓    | Done                         | 52s     |   idle after a turn
 *     |      | Idle / Starting… / Stopped / Not connected |
 *
 * Port of `extensions/vscode/src/shared/activity.ts`.
 */
package io.github.kadeng.calu.logic

/** Spinner frames, played forward and back (like Claude Code's terminal UI). */
val SPINNER_FRAMES: List<String> = listOf("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")

/** Verbs shown while Claude works; one is picked per few seconds. */
val ACTIVITY_VERBS: List<String> = listOf(
    "Combobulating", "Pondering", "Cogitating", "Ruminating", "Percolating", "Noodling",
    "Mulling", "Deliberating", "Brewing", "Tinkering", "Wrangling", "Untangling", "Musing",
    "Synthesizing", "Concocting", "Crunching", "Simmering", "Puzzling", "Contemplating",
    "Reticulating", "Spelunking", "Marinating", "Finagling", "Discombobulating",
)

/** Label shown while a checker judges a step. */
const val CHECKING_LABEL: String = "Checking…"

/** Label shown while an approval is open. */
const val WAITING_LABEL: String = "Waiting for you…"

/**
 * Width of the label column in characters of a monospace font: the longest busy or waiting
 * label plus one character of space before the timer.
 */
const val LABEL_COLUMN_CH: Int = 18

/** Milliseconds between two spinner frames. */
const val FRAME_MS: Long = 120

/** Milliseconds a verb stays before the next one. */
const val VERB_MS: Long = 4000

/**
 * Map a protocol session state to its coarse phase.
 *
 * @param state The `state` of `session.state` or of the snapshot.
 * @return "busy" for "running", "checking" and "tool_call"; "waiting" for "escalated"; the
 *   state itself for "starting", "idle", "stopped" and "failed"; "idle" for an unknown value.
 */
fun sessionPhase(state: String): String = when (state) {
    "running", "checking", "tool_call" -> "busy"
    "escalated" -> "waiting"
    "starting", "stopped", "failed" -> state
    else -> "idle"
}

/**
 * What the activity line shows.
 *
 * @property icon First column: spinner frame, "‖", "✓" or "".
 * @property label Second column, e.g. "Combobulating…".
 * @property elapsed Third column: elapsed time ("12s") or "".
 * @property text All parts as one text, e.g. "✻ Combobulating… 12s" (for screen readers and tests).
 * @property active True while Claude works or waits (the line animates and the timer keeps running).
 * @property kind "busy", "waiting", "done" or "idle".
 */
data class Activity(val icon: String, val label: String, val elapsed: String, val text: String, val active: Boolean, val kind: String)

/**
 * Format a duration for the activity line.
 *
 * @param ms Milliseconds (negative values count as 0).
 * @return "12s" below a minute, else "3m 05s".
 */
fun formatElapsed(ms: Long): String {
    val s = maxOf(0L, Math.floorDiv(ms, 1000L))
    return if (s < 60) "${s}s" else "${s / 60}m ${(s % 60).toString().padStart(2, '0')}s"
}

/**
 * Build an `Activity` from its columns.
 *
 * @param icon First column.
 * @param label Second column.
 * @param elapsed Third column.
 * @param active Whether the line animates.
 * @param kind "busy", "waiting", "done" or "idle".
 * @return The activity, with `text` joining the non-empty columns by spaces.
 */
private fun activity(icon: String, label: String, elapsed: String, active: Boolean, kind: String): Activity =
    Activity(icon, label, elapsed, listOf(icon, label, elapsed).filter { it.isNotEmpty() }.joinToString(" "), active, kind)

/**
 * Find when the waiting began. The result is the time that the session gives for the oldest
 * open escalation (`escalation.request.opened_at`, also in the snapshot of a later attach).
 * The time comes from the session, not from the view, so that the "waiting for you" timer
 * survives a bridge restart, a reattach and a reopened tool window.
 * Port of `waitingSince` in `extensions/vscode/src/shared/activity.ts`.
 *
 * Example: `waitingSince(listOf("2026-11-19T10:00:00Z", null), 5, <10:03:00Z in ms>)` is the
 * epoch milliseconds of 10:00:00Z (the timer shows "3m 00s").
 *
 * @param openedAt `openedAt` of each open escalation (ISO times from the session, with a zone
 *   offset or "Z"); `null` or an unreadable value is skipped.
 * @param fallback Time (ms) to use when no escalation gives a time (a session of an older calu):
 *   the start of the current waiting period as this view saw it.
 * @param now Current time (ms). A time after it (the session's clock is ahead) counts as `now`.
 * @return Milliseconds since the epoch.
 */
fun waitingSince(openedAt: List<String?>, fallback: Long, now: Long): Long {
    val times = openedAt.mapNotNull { t ->
        if (t == null) null else runCatching { java.time.OffsetDateTime.parse(t).toInstant().toEpochMilli() }.getOrNull()
    }
    if (times.isEmpty()) return fallback
    return minOf(now, times.min())
}

/**
 * Compute the activity line.
 *
 * @param connected True if a session is attached.
 * @param state The session state from `session.state` ("starting", "idle", "running",
 *   "checking", "tool_call", "escalated", "stopped", "failed").
 * @param since Time (ms) the current busy/waiting period began, or the last one began (idle).
 * @param lastDurationMs Length of the last finished busy period, or `null` before the first.
 * @param now Current time (ms).
 * @param seed A non-negative number picked at the start of the period; selects the first verb.
 * @return The columns, whether the line animates, and its kind.
 */
fun activityLine(connected: Boolean, state: String, since: Long, lastDurationMs: Long?, now: Long, seed: Int): Activity {
    if (!connected) return activity("", "Not connected", "", false, "idle")
    val elapsed = maxOf(0L, now - since)
    when (sessionPhase(state)) {
        "busy" -> {
            val frame = SPINNER_FRAMES[((elapsed / FRAME_MS) % SPINNER_FRAMES.size).toInt()]
            if (state == "checking") return activity(frame, CHECKING_LABEL, formatElapsed(elapsed), true, "busy")
            val verb = ACTIVITY_VERBS[Math.floorMod(seed + elapsed / VERB_MS, ACTIVITY_VERBS.size.toLong()).toInt()]
            return activity(frame, "$verb…", formatElapsed(elapsed), true, "busy")
        }
        "waiting" -> return activity("‖", WAITING_LABEL, formatElapsed(elapsed), true, "waiting")
        "starting" -> return activity("", "Starting…", "", false, "idle")
        "failed" -> return activity("", "Failed", "", false, "idle")
        "stopped" -> return activity("", "Stopped", "", false, "idle")
    }
    if (lastDurationMs != null) return activity("✓", "Done", formatElapsed(lastDurationMs), false, "done")
    return activity("", "Idle", "", false, "idle")
}
