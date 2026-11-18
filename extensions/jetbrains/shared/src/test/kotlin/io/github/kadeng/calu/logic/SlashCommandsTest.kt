package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.jo
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Slash commands of the composer (port of `slashCommands.test.ts`). */
class SlashCommandsTest {
    private val table = allCommands()

    @Test
    fun `holds the plugin commands and the built-in Claude Code commands, sorted`() {
        assertEquals(
            listOf("model", "effort", "level", "honesty", "checkers", "checker-context", "sessions", "new", "fork", "rename", "settings", "interrupt"),
            CALU_COMMANDS.map { it.name },
        )
        // No /help: a session without a terminal answers that it is not available.
        assertEquals(listOf("compact", "clear", "usage", "context", "cost"), CLAUDE_COMMANDS.map { it.name })
        val names = table.map { it.name }
        assertEquals(names.sorted(), names)
        assertEquals(names.size, names.toSet().size)
    }

    @Test
    fun `adds reported commands once and drops malformed names`() {
        val more = allCommands(listOf("/review", "review", "compact", "bad name", ""))
        assertEquals(1, more.count { it.name == "review" })
        assertEquals("claude", more.first { it.name == "review" }.source)
        assertEquals(table.size + 1, more.size)
    }

    @Test
    fun `opens only for one line that starts with a slash`() {
        assertEquals(emptyList<Completion>(), completions("hello", table))
        assertEquals(emptyList<Completion>(), completions(" /model", table))
        assertEquals(emptyList<Completion>(), completions("/model\nmore", table))
        assertEquals(table.size, completions("/", table).size)
    }

    @Test
    fun `ranks prefix matches before other matches`() {
        assertEquals(listOf("/compact", "/context", "/cost", "/checker-context"), completions("/co", table).map { it.label })
        assertEquals(emptyList<Completion>(), completions("/zzz", table))
    }

    @Test
    fun `completes a command with values to the name and a space, and offers the values next`() {
        val model = completions("/mod", table)[0]
        assertEquals(listOf("/model", "/model ", false, "calu"), listOf(model.label, model.insert, model.submit, model.kind))
        assertEquals(listOf("default", "fable", "opus", "sonnet", "haiku"), completions("/model ", table).map { it.label })
        assertEquals(listOf(Completion("sonnet", "The latest sonnet model", "value", "/model sonnet", true)), completions("/model so", table))
        assertEquals(listOf("checkers", "edits", "writes", "everything"), completions("/level ", table).map { it.label })
    }

    @Test
    fun `submits commands without values at once and stops after an argument`() {
        val settings = completions("/sett", table)[0]
        assertEquals("/settings", settings.insert)
        assertTrue(settings.submit)
        assertEquals(emptyList<Completion>(), completions("/compact keep the plan", table))
        assertEquals(emptyList<Completion>(), completions("/compact ", table))
    }

    @Test
    fun `sends Claude Code commands, unknown commands and plain text as chat`() {
        for (text in listOf("hello", "/compact", "/compact keep the plan", "/my-project-command arg", "/ not a command", "/context")) {
            assertEquals(SlashResult.Chat, resolveSlash(text, "s1"), text)
        }
    }

    @Test
    fun `maps plugin commands to chat actions and view parts`() {
        val cases = listOf(
            "/model opus" to SlashResult.Act("setModel", jo("model" to "opus")),
            "/model claude-opus-4-5[1m]" to SlashResult.Act("setModel", jo("model" to "claude-opus-4-5[1m]")),
            "/effort high" to SlashResult.Act("setEffort", jo("effort" to "high")),
            "/level writes" to SlashResult.Act("setLevel", jo("level" to "writes")),
            "/honesty enforce" to SlashResult.Act("setHonesty", jo("mode" to "enforce")),
            "/checkers" to SlashResult.Ui("checkers"),
            "/checker-context" to SlashResult.Act("showContext"),
            "/new" to SlashResult.Act("start", jo("kind" to "new")),
            "/fork" to SlashResult.Act("forkConversation"),
            "/rename" to SlashResult.Ui("rename"),
            "/settings" to SlashResult.Ui("settings"),
            "  /interrupt  " to SlashResult.Act("interrupt"),
        )
        for ((text, result) in cases) assertEquals(result, resolveSlash(text, "s1"), text)
    }

    @Test
    fun `opens a part of the view when a choice is missing`() {
        assertEquals(SlashResult.Ui("sessions"), resolveSlash("/sessions", null))
        for (name in listOf("model", "effort", "level", "honesty")) assertEquals(SlashResult.Ui(name), resolveSlash("/$name", "s1"))
    }

    @Test
    fun `explains a wrong value and a missing session`() {
        assertEquals(SlashResult.Error("/effort takes one of: default, low, medium, high, xhigh, max."), resolveSlash("/effort extreme", "s1"))
        assertTrue(resolveSlash("/level custom", "s1") is SlashResult.Error)
        assertTrue(resolveSlash("/model has space", "s1") is SlashResult.Error)
        for (text in listOf("/fork", "/rename", "/interrupt", "/model opus", "/level edits", "/checker-context")) {
            val result = resolveSlash(text, null)
            assertTrue(result is SlashResult.Error && result.text.contains("needs an attached session"), text)
        }
        assertEquals(SlashResult.Ui("settings"), resolveSlash("/settings", null))
    }
}
