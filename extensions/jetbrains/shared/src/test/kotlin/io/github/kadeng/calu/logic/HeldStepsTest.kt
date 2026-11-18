package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.EscalationView
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.QuestionView
import io.github.kadeng.calu.state.ToolCheck
import io.github.kadeng.calu.state.ToolResultView
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.ViewState
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Tests of the derivation of held steps. */
class HeldStepsTest {
    /**
     * Build an escalation.
     *
     * @param tool The tool call it is about.
     * @param status Its status.
     * @param question True for a question of the agent.
     * @return The escalation.
     */
    private fun escalation(tool: String, status: String = EscalationStatus.OPEN, question: Boolean = false) = EscalationView(
        key = "e-$tool", escalationId = "e-$tool", toolUseId = tool, agentKey = MAIN, toolName = "Bash", toolInput = EMPTY_OBJECT,
        reason = "", rendered = "", questions = if (question) listOf(QuestionView("q", "h", emptyList(), false)) else emptyList(),
        openedAt = null, status = status,
    )

    private val check = ToolCheck("command", "PASS", "", "", "", "command")

    @Test
    fun `a call without check and result is held while a decision is open`() {
        val tools = mapOf(
            "a" to ToolView("a", MAIN, "Bash"),
            "b" to ToolView("b", "sub1", "Bash"),
            "c" to ToolView("c", "sub1", "Bash", check = check),
            "d" to ToolView("d", "sub2", "Bash", result = ToolResultView("", false)),
            "q" to ToolView("q", MAIN, "AskUserQuestion"),
        )
        val state = ViewState(tools = tools, escalations = mapOf("e-a" to escalation("a")))
        assertTrue(isHolding(state))
        assertEquals(setOf("b"), heldTools(state))
        assertEquals(mapOf("sub1" to 1), heldByAgent(state))
    }

    @Test
    fun `nothing is held without an open decision, and a question holds nothing`() {
        val tools = mapOf("b" to ToolView("b", "sub1", "Bash"))
        assertTrue(heldTools(ViewState(tools = tools)).isEmpty())
        assertTrue(heldTools(ViewState(tools = tools, escalations = mapOf("e" to escalation("x", EscalationStatus.ALLOW)))).isEmpty())
        val question = ViewState(tools = tools, escalations = mapOf("e" to escalation("x", question = true)))
        assertFalse(isHolding(question))
        assertTrue(heldTools(question).isEmpty())
    }
}
