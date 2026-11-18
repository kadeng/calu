package io.github.kadeng.calu.chat

import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.initialViewState
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The rows of a scripted conversation: what each row shows and what a click opens. */
class RowsTest {
    private fun ev(method: String, json: String) = ViewEvent(method, parseObjectOrNull(json)!!)

    private val conversation = listOf(
        ev("user.input", """{"text":"fix the parser","source":"user"}"""),
        ev("assistant.delta", """{"text":"Looking ","message_id":"m1"}"""),
        ev("assistant.delta", """{"text":"at **it**.","message_id":"m1"}"""),
        ev("tool.use", """{"tool_use_id":"r1","name":"Read","input":{"file_path":"/p/src/parser.py","offset":40}}"""),
        ev("tool.use", """{"tool_use_id":"e1","name":"Edit","input":{"file_path":"/p/src/parser.py"}}"""),
        ev("edit.diff", """{"tool_use_id":"e1","path":"src/parser.py","before":"a","after":"b","start_line":42,"end_line":43}"""),
        ev("check.result", """{"tool_use_id":"e1","tool_name":"Edit","route":"edit","decision":"ALLOW","step_summary":"Fixes the off-by-one","comment":"Add a test"}"""),
        ev("tool.use", """{"tool_use_id":"b1","name":"Bash","input":{"command":"rm -rf build\nmake"}}"""),
        ev("check.result", """{"tool_use_id":"b1","tool_name":"Bash","route":"command","decision":"ESCALATE","reason":"deletes files"}"""),
        ev("escalation.request", """{"escalation_id":"x1","tool_use_id":"b1","tool_name":"Bash","tool_input":{"command":"rm -rf build"},"reason":"deletes files"}"""),
        ev("tool.result", """{"tool_use_id":"b1","content":"done","is_error":false}"""),
        ev("turn.result", """{"subtype":"success","is_error":false,"cost_usd":0.5}"""),
    )

    @Test
    fun `a scripted conversation becomes one row per item with the right texts`() {
        val rows = rowsOf(applyEvents(initialViewState(), conversation), MAIN)
        assertEquals(listOf("user", "assistant", "tool", "tool", "checker", "tool", "escalation", "turn"), rows.map { it.kind })
        assertEquals("You", rows[0].title)
        assertEquals("Looking at **it**.", rows[1].text)
        assertTrue(rows[1].markdown)
        assertEquals("turn done · session \$0.5000", rows[7].text)
    }

    @Test
    fun `a turn row names an interrupted turn and the turn's own cost`() {
        val rows = rowsOf(
            applyEvents(
                initialViewState(),
                listOf(
                    ev("turn.result", """{"subtype":"success","cost_usd":0.5}"""),
                    ev("turn.result", """{"subtype":"interrupted","cost_usd":0.75,"background_tasks":2}"""),
                ),
            ),
            MAIN,
        )
        assertEquals("turn interrupted · \$0.2500 · session \$0.7500 · 2 background tasks running", rows[1].text)
    }

    @Test
    fun `a row shows bidirectional control characters as placeholders`() {
        val rows = rowsOf(
            applyEvents(initialViewState(), listOf(ev("tool.use", """{"tool_use_id":"b1","name":"Bash","input":{"command":"rm -rf \u202egnp.tropxe"}}"""))),
            MAIN,
        )
        assertEquals("rm -rf \u27e6RLO\u27e7gnp.tropxe", rows[0].text)
        assertEquals("rm -rf \u27e6RLO\u27e7gnp.tropxe", rows[0].detail)
    }

    @Test
    fun `an edit row opens the diff, its file name opens the file at the edited line`() {
        val edit = rowsOf(applyEvents(initialViewState(), conversation), MAIN)[3]
        assertEquals("Edit", edit.title)
        assertEquals("parser.py", edit.linkText)
        assertEquals("openDiff", edit.action!!.name)
        assertEquals("e1", edit.action!!.args.str("tool_use_id"))
        assertEquals("openFile", edit.link!!.name)
        assertEquals("42", edit.link!!.args["line"].toString())
        assertEquals("Open diff", edit.tooltip)
        assertEquals("ALLOW", edit.chip)
        assertEquals("ok", edit.chipKind)
        assertEquals("Fixes the off-by-one", edit.text)
    }

    @Test
    fun `a read row opens the file at the line read, a command row expands command and output`() {
        val rows = rowsOf(applyEvents(initialViewState(), conversation), MAIN)
        assertEquals("openFile", rows[2].action!!.name)
        assertEquals("40", rows[2].action!!.args["line"].toString())
        val bash = rows[5]
        assertEquals(null, bash.action)
        assertEquals("rm -rf build", bash.text)
        assertTrue(bash.detail.startsWith("rm -rf build\nmake"))
        assertTrue(bash.detail.contains("Reason: deletes files"))
        assertTrue(bash.detail.endsWith("Output:\ndone"))
        assertEquals("warning", bash.chipKind)
    }

    @Test
    fun `a checker remark links to the checker's instructions and the escalation row carries its card`() {
        val rows = rowsOf(applyEvents(initialViewState(), conversation), MAIN)
        assertEquals("Edit checker", rows[4].title)
        assertEquals("Add a test", rows[4].text)
        assertEquals("edit", rows[4].action!!.args.str("route"))
        assertNotNull(rows[6].escalation)
        assertEquals("deletes files", rows[6].escalation!!.reason)
    }

    @Test
    fun `an edit without a held diff opens the file and says so`() {
        val state = applyEvents(initialViewState(), listOf(ev("tool.use", """{"tool_use_id":"e2","name":"Write","input":{"file_path":"/p/a.txt"}}""")))
        val row = rowsOf(state, MAIN).single()
        assertEquals("openFile", row.action!!.name)
        assertTrue(row.tooltip.contains("no diff is held"))
    }
}
