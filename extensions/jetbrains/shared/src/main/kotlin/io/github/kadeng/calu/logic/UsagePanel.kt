/**
 * The usage panel that a click on the context chip opens (`usage.panel`, PLAN D248): what it
 * says, as sections of lines. The VSCode extension has the same rules in
 * `extensions/vscode/src/shared/usagePanel.ts`; the surface only draws the sections.
 *
 *     Subscription limits   5-hour and weekly always (each with when Claude Code reported it,
 *                           or "not reported yet by Claude Code"), then any other reported limit
 *     Your plan now         the plan's limits Claude Code read at the click (`get_usage`), or why not
 *     Claude Code's /usage  its text, read without a turn
 *     This session          cost, model calls, context window
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.RateLimit
import io.github.kadeng.calu.state.UsagePanelView
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Locale

/**
 * One line of the usage panel.
 *
 * @property text The text (plain, never markup).
 * @property tone "plain", "muted" (missing or secondary) or "problem" (a limit that is not
 *   "allowed", a failed request).
 */
data class UsagePanelLine(val text: String, val tone: String = "plain")

/**
 * One section of the usage panel.
 *
 * @property title The heading.
 * @property note A short explanation after the heading; "" for none.
 * @property lines The lines.
 * @property preformatted Text shown as it is in a monospaced block (Claude Code's `/usage`);
 *   `null` for none.
 */
data class UsagePanelSection(val title: String, val note: String, val lines: List<UsagePanelLine>, val preformatted: String? = null)

/** The two limits every subscription has; the panel always names them. */
val MAIN_LIMIT_TYPES: List<String> = listOf("five_hour", "seven_day")

/**
 * Name a limit type for people.
 *
 * @param type The protocol's type, e.g. "five_hour".
 * @return "5-hour", "weekly", "weekly Opus", "weekly Sonnet", "extra usage", otherwise the type
 *   with spaces for underscores ("limit" for "").
 */
fun limitName(type: String): String = when (type) {
    "five_hour" -> "5-hour"
    "seven_day" -> "weekly"
    "seven_day_opus" -> "weekly Opus"
    "seven_day_sonnet" -> "weekly Sonnet"
    "overage" -> "extra usage"
    "" -> "limit"
    else -> type.replace('_', ' ')
}

/**
 * Read an ISO time of the protocol.
 *
 * @param text The time, with offset or "Z"; `null` or "" for none.
 * @return The instant, or `null` if absent or not such a time.
 */
private fun instantOf(text: String?): Instant? {
    if (text.isNullOrEmpty()) return null
    return runCatching { Instant.parse(text) }.getOrNull() ?: runCatching { OffsetDateTime.parse(text).toInstant() }.getOrNull()
}

/**
 * Word the time until a reset.
 *
 * @param seconds Seconds from now until the reset.
 * @return "resets in 2 h 10 min", "resets in 3 d 4 h", "resets in under a minute"; "reset time
 *   passed" for a time in the past.
 */
fun resetText(seconds: Long): String {
    if (seconds < 0) return "reset time passed"
    val d = seconds / 86400
    val h = (seconds % 86400) / 3600
    val m = (seconds % 3600) / 60
    return when {
        d > 0 -> "resets in $d d $h h"
        h > 0 -> "resets in $h h $m min"
        m > 0 -> "resets in $m min"
        else -> "resets in under a minute"
    }
}

/**
 * Describe one limit.
 *
 * @param limit The limit.
 * @param now The time to measure ages and resets from.
 * @param stamped True to add when Claude Code reported it ("reported 3 min ago").
 * @return The line; tone "problem" when the status is set and is not "allowed".
 */
fun usageLimitLine(limit: RateLimit, now: Instant, stamped: Boolean): UsagePanelLine {
    val parts = ArrayList<String>()
    limit.utilization?.let { parts.add("${Math.round(if (it <= 1) it * 100 else it)}% used") }
    if (limit.status.isNotEmpty() && limit.status != "allowed") parts.add(limit.status.replace('_', ' '))
    instantOf(limit.resetsAt)?.let { parts.add(resetText(it.epochSecond - now.epochSecond)) }
    if (parts.isEmpty()) parts.add(if (limit.status == "allowed") "allowed" else "no figures")
    var text = "${limitName(limit.type)}: ${parts.joinToString(", ")}"
    if (stamped) {
        val at = instantOf(limit.reportedAt)
        text += if (at != null) " · reported ${formatAge(now.epochSecond - at.epochSecond)}" else " · time of the report not known"
    }
    return UsagePanelLine(text, if (limit.status.isNotEmpty() && limit.status != "allowed") "problem" else "plain")
}

/**
 * Build the sections of the usage panel. No side effects.
 *
 * @param panel The panel's state; its `phase` must be "ready" (for "asking" and "failed" the
 *   surface shows one line of its own).
 * @param now The time to measure ages and resets from.
 * @return Four sections: "Subscription limits", "Your plan now" (only when the plan's limits or
 *   a reason why they are missing were given), "Claude Code's /usage", "This session".
 *
 * Example: no limit reported yet and no `get_usage` answer gives the lines
 * "5-hour: not reported yet by Claude Code" and "weekly: not reported yet by Claude Code".
 */
fun usagePanelSections(panel: UsagePanelView, now: Instant): List<UsagePanelSection> {
    val out = ArrayList<UsagePanelSection>()
    val byType = panel.limits.associateBy { it.type }
    val limitLines = MAIN_LIMIT_TYPES.map { type ->
        byType[type]?.let { usageLimitLine(it, now, true) } ?: UsagePanelLine("${limitName(type)}: not reported yet by Claude Code", "muted")
    } + panel.limits.filter { it.type !in MAIN_LIMIT_TYPES }.map { usageLimitLine(it, now, true) }
    out.add(UsagePanelSection("Subscription limits", "as Claude Code reported them", limitLines))
    if (panel.accountLimits.isNotEmpty()) {
        val plan = if (panel.subscription.isNotEmpty()) " (${panel.subscription})" else ""
        out.add(UsagePanelSection("Your plan now$plan", "read from Claude Code at this click", panel.accountLimits.map { usageLimitLine(it, now, false) }))
    } else if (panel.accountLimitsProblem.isNotEmpty()) {
        out.add(UsagePanelSection("Your plan now", "", listOf(UsagePanelLine(panel.accountLimitsProblem, "muted"))))
    }
    out.add(
        if (panel.claudeUsage.isNotEmpty()) {
            UsagePanelSection("Claude Code's /usage", "read without a turn; not recorded, not shown to the checkers", emptyList(), panel.claudeUsage)
        } else {
            UsagePanelSection("Claude Code's /usage", "", listOf(UsagePanelLine(panel.claudeUsageProblem.ifEmpty { "not available" }, "muted")))
        },
    )
    val session = ArrayList<UsagePanelLine>()
    session.add(
        if (panel.costUsd == null) {
            UsagePanelLine("cost: no turn has ended yet", "muted")
        } else {
            val calls = if (panel.numTurns > 0) " · last turn: ${panel.numTurns} model call${if (panel.numTurns == 1) "" else "s"}" else ""
            UsagePanelLine("cost ${String.format(Locale.ROOT, "$%.4f", panel.costUsd)} (Claude Code, whole session)$calls")
        },
    )
    panel.context?.let { c ->
        val model = if (c.model.isNotEmpty()) " · ${c.model}" else ""
        session.add(UsagePanelLine("context ${String.format(Locale.ROOT, "%,d", c.tokens)} of ${String.format(Locale.ROOT, "%,d", c.maxTokens)} tokens (${Math.round(c.percent)}%)$model"))
    }
    out.add(UsagePanelSection("This session", "", session))
    return out
}
