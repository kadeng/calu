package io.github.kadeng.calu.logic

import io.github.kadeng.calu.protocol.BridgeError
import io.github.kadeng.calu.protocol.CheckerConflictData
import io.github.kadeng.calu.protocol.checkerConflict
import io.github.kadeng.calu.protocol.neededCredential
import io.github.kadeng.calu.state.jo
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Texts of the read-only context documents, and the guards for the bridge's error data (port of `contextText.test.ts`). */
class ContextTextTest {
    @Test
    fun `demotes headings outside code fences`() {
        assertEquals("## A\ntext\n```\n# not a heading\n```\n### B", demoteHeadings("# A\ntext\n```\n# not a heading\n```\n## B"))
        assertEquals("~~~~\n# x\n~~~\n# still code\n~~~~\n## H", demoteHeadings("~~~~\n# x\n~~~\n# still code\n~~~~\n# H"))
    }

    @Test
    fun `builds the document, also from an empty context`() {
        val text = formatCheckerContext(jo("chat_summary" to "# Chat summary\nEarlier work.", "recent" to "## Turn 4\nUser: go"), "14:03:10")
        assertTrue(text.contains("# Folded summary\n\n## Chat summary\nEarlier work."))
        assertTrue(text.contains("# Recent turns\n\n### Turn 4\nUser: go"))
        assertTrue(text.contains("loaded 14:03:10"))
        val empty = formatCheckerContext(jo(), "")
        assertTrue(empty.contains("_Nothing folded yet._"))
        assertTrue(empty.contains("_No turns yet._"))
    }

    @Test
    fun `reloads after the events that change it`() {
        assertTrue("check.result" in CONTEXT_EVENTS)
        assertFalse("assistant.delta" in CONTEXT_EVENTS)
    }

    @Test
    fun `uses a fence the text cannot close`() {
        assertEquals("```\nplain\n```", fenced("plain"))
        assertEquals("`````md\na ```` b\n`````", fenced("a ```` b", "md"))
    }

    @Test
    fun `shows the step, the checker runs and the context of that moment`() {
        val text = formatEscalationContext(
            jo(
                "escalation_id" to "e1", "state" to "denied", "resolved_by" to "kai", "tool_name" to "Bash", "note" to "not now",
                "reason" to "Deletes files outside the project.", "rendered" to "# Command\n`rm -rf /tmp/x`",
                "tool_input" to mapOf("command" to "rm -rf /tmp/x"),
                "checker_runs" to listOf(
                    mapOf(
                        "checker_name" to "command", "checker_version" to 3, "decision" to "ESCALATE", "model" to "anthropic:claude-haiku-5-5",
                        "reason" to "Outside.", "input_tokens" to 900, "output_tokens" to 40, "duration_ms" to 1200,
                    ),
                ),
                "files" to mapOf("a.py" to "print(\"```\")"),
                "chat_summary" to "# Chat summary\nSetup.", "recent" to "## Turn 2\ngo",
            ),
        )
        for (part in listOf(
            "# Escalation e1", "- **State:** denied by kai", "- **Note:** not now", "## Command\n`rm -rf /tmp/x`", "\"command\": \"rm -rf /tmp/x\"",
            "## command (version 3): ESCALATE\n\nanthropic:claude-haiku-5-5 · 900 in, 40 out · 1200 ms\n\nOutside.",
            "## a.py\n\n````\nprint(\"```\")\n````", "# Folded summary at that moment\n\n## Chat summary", "### Turn 2",
        )) assertTrue(text.contains(part), part)
    }

    @Test
    fun `survives a result without members`() {
        val text = formatEscalationContext(jo())
        assertTrue(text.contains("- **State:** unknown"))
        assertTrue(text.contains("_No checker ran"))
        assertFalse(text.contains("# Files before the step"))
    }

    @Test
    fun `reads the credential of error -32010 and the stored checker of -32016`() {
        assertEquals(-32010, BridgeError.NEEDS_PASSWORD)
        assertEquals("admin", neededCredential(jo("needs" to "admin")))
        assertNull(neededCredential(jo("needs" to 5)))
        assertNull(neededCredential(JsonNull))
        assertEquals(-32016, BridgeError.CHECKER_CONFLICT)
        assertEquals(CheckerConflictData(4, "text"), checkerConflict(jo("version" to 4, "instructions" to "text")))
        assertNull(checkerConflict(jo("version" to "4", "instructions" to "text")))
        assertNull(checkerConflict(JsonPrimitive("x")))
    }
}
