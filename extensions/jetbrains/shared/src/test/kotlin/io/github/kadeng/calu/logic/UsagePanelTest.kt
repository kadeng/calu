package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ContextView
import io.github.kadeng.calu.state.RateLimit
import io.github.kadeng.calu.state.UsagePanelView
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.ja
import io.github.kadeng.calu.state.jo
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import java.time.Instant

/** The usage panel's sections (`usagePanelSections`) and its reducer case `ui.usagePanel`. */
class UsagePanelTest {
    private val now: Instant = Instant.parse("2026-11-20T12:00:00Z")

    @Test fun `nothing reported - both main limits say so, the session section says no turn ended`() {
        val sections = usagePanelSections(UsagePanelView("ready", claudeUsageProblem = "Claude Code did not answer"), now)
        assertEquals(listOf("Subscription limits", "Claude Code's /usage", "This session"), sections.map { it.title })
        assertEquals(
            listOf(UsagePanelLine("5-hour: not reported yet by Claude Code", "muted"), UsagePanelLine("weekly: not reported yet by Claude Code", "muted")),
            sections[0].lines,
        )
        assertEquals("Claude Code did not answer", sections[1].lines.single().text)
        assertNull(sections[1].preformatted)
        assertEquals("cost: no turn has ended yet", sections[2].lines.single().text)
    }

    @Test fun `reported limits carry their age and reset, other limits follow, a warning is a problem`() {
        val panel = UsagePanelView(
            "ready",
            limits = listOf(
                RateLimit("seven_day_opus", "allowed_warning", 0.91, "2026-11-23T12:00:00Z", "2026-11-20T09:00:00Z"),
                RateLimit("five_hour", "allowed", 0.42, "2026-11-20T14:10:00Z", "2026-11-20T11:57:00Z"),
            ),
            claudeUsage = "Total cost: $0.01\nUsage: 1 input",
            costUsd = 0.0123,
            numTurns = 2,
            context = ContextView(42000, 200000, 21.0, "haiku"),
        )
        val sections = usagePanelSections(panel, now)
        assertEquals(
            listOf(
                UsagePanelLine("5-hour: 42% used, resets in 2 h 10 min · reported 3 min ago"),
                UsagePanelLine("weekly: not reported yet by Claude Code", "muted"),
                UsagePanelLine("weekly Opus: 91% used, allowed warning, resets in 3 d 0 h · reported 3 h ago", "problem"),
            ),
            sections[0].lines,
        )
        assertEquals("Total cost: $0.01\nUsage: 1 input", sections[1].preformatted)
        assertEquals(
            listOf("cost $0.0123 (Claude Code, whole session) · last turn: 2 model calls", "context 42,000 of 200,000 tokens (21%) · haiku"),
            sections[2].lines.map { it.text },
        )
    }

    @Test fun `the plan's limits of get_usage, or why they are missing`() {
        val plan = usagePanelSections(UsagePanelView("ready", accountLimits = listOf(RateLimit("seven_day", "", 30.0)), subscription = "max"), now)
        assertEquals("Your plan now (max)", plan[1].title)
        assertEquals("weekly: 30% used", plan[1].lines.single().text, "no report time on a figure read now")
        val missing = usagePanelSections(UsagePanelView("ready", accountLimitsProblem = "get_usage failed: timeout"), now)
        assertEquals(UsagePanelLine("get_usage failed: timeout", "muted"), missing[1].lines.single())
    }

    @Test fun `resetText and limitName`() {
        assertEquals("reset time passed", resetText(-1))
        assertEquals("resets in under a minute", resetText(30))
        assertEquals("resets in 5 min", resetText(300))
        assertEquals("extra usage", limitName("overage"))
        assertEquals("some new limit", limitName("some_new_limit"))
    }

    @Test fun `reducer - asking, the answer updates the chip, a refusal`() {
        val asking = applyEvent(initialViewState(), ev("ui.usagePanel", "phase" to "asking"))
        assertEquals("asking", asking.usagePanel?.phase)
        val result = jo(
            "limits" to ja(jo("type" to "five_hour", "status" to "allowed", "utilization" to 0.5, "reported_at" to "2026-11-20T11:00:00Z")),
            "claude_usage" to "text",
            "cost_usd" to 0.5,
            "num_turns" to 1,
            "context" to jo("tokens" to 10, "max_tokens" to 100, "percent" to 10.0),
        )
        val ready = applyEvent(asking, ev("ui.usagePanel", "result" to result))
        assertEquals("ready", ready.usagePanel?.phase)
        assertEquals("2026-11-20T11:00:00Z", ready.usagePanel?.limits?.single()?.reportedAt)
        assertEquals("text", ready.usagePanel?.claudeUsage)
        assertEquals(0.5, ready.usage.costUsd)
        assertEquals(10, ready.usage.context?.tokens)
        assertEquals("five_hour", ready.usage.rateLimits.single().type)
        val noContext = applyEvent(ready, ev("ui.usagePanel", "result" to jo("claude_usage" to "t")))
        assertEquals(10, noContext.usage.context?.tokens, "an answer without context keeps the chip's")
        assertEquals("five_hour", noContext.usage.rateLimits.single().type, "an answer without limits keeps the chip's")
        val failed = applyEvent(ready, ev("ui.usagePanel", "error" to "the session did not answer"))
        assertEquals(UsagePanelView("failed", error = "the session did not answer"), failed.usagePanel)
    }
}
