package io.github.kadeng.calu.state

import io.github.kadeng.calu.chat.rowsOf
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The honesty checker's per-file result of `check.result` (port of the VSCode reducer change). */
class HonestyResultTest {
    /**
     * Parse a JSON object.
     *
     * @param text JSON text.
     * @return The object.
     */
    private fun obj(text: String): JsonObject = Json.parseToJsonElement(text) as JsonObject

    private val check = """{"seq": 3, "tool_use_id": "t1", "tool_name": "Edit", "route": "edit", "decision": "PASS", "step_summary": "Edits the greeter",
        "honesty": [
          {"path": "/p/greeter.py", "verdict": "DISHONEST", "reason": "The docstring says Hello, the code returns Good day.",
           "units": [{"symbol": "greet", "honest": false, "reason": "docstring and code disagree"}, {"symbol": "greet_all", "honest": true, "reason": ""}]},
          {"path": "/p/util.py", "verdict": "HONEST", "reason": "", "units": []},
          {"verdict": "HONEST"}, 7
        ],
        "honesty_escalation": "A test was deleted."}"""

    @Test
    fun `the result is read defensively`() {
        val h = parseHonestyResult(obj(check))!!
        assertEquals(listOf("/p/greeter.py", "/p/util.py"), h.files.map { it.path })
        assertFalse(h.files[0].honest)
        assertEquals(listOf(HonestyUnitView("greet", "docstring and code disagree")), h.files[0].dishonestUnits)
        assertTrue(h.files[1].honest)
        assertEquals("A test was deleted.", h.escalation)
        assertNull(parseHonestyResult(obj("""{"decision": "PASS"}""")))
        assertNull(parseHonestyResult(obj("""{"honesty": "no", "honesty_escalation": 3}""")))
        assertEquals(HonestyResultView(emptyList(), "look"), parseHonestyResult(obj("""{"honesty": [], "honesty_escalation": "look"}""")))
    }

    @Test
    fun `a checked edit keeps the result, and a later check without one does not drop it`() {
        var state = applyEvent(initialViewState(), ViewEvent("tool.use", obj("""{"seq": 2, "tool_use_id": "t1", "name": "Edit", "input": {"file_path": "/p/greeter.py"}}""")))
        state = applyEvent(state, ViewEvent("check.result", obj(check)))
        assertEquals(2, state.tools["t1"]!!.honesty!!.files.size)
        assertEquals("PASS", state.tools["t1"]!!.check!!.decision)
        state = applyEvent(state, ViewEvent("check.result", obj("""{"seq": 4, "tool_use_id": "t1", "tool_name": "Edit", "route": "edit", "decision": "ALLOW"}""")))
        assertEquals("ALLOW", state.tools["t1"]!!.check!!.decision)
        assertEquals(2, state.tools["t1"]!!.honesty!!.files.size)
    }

    @Test
    fun `the row lists each file with HONEST or DISHONEST and never a PASS chip for the honesty checker`() {
        var state = applyEvent(initialViewState(), ViewEvent("tool.use", obj("""{"seq": 2, "tool_use_id": "t1", "name": "Edit", "input": {"file_path": "/p/greeter.py"}}""")))
        state = applyEvent(state, ViewEvent("check.result", obj(check)))
        val row = rowsOf(state, MAIN).single { it.kind == "tool" }
        assertEquals("PASS", row.chip, "the chip is the decision of the checkers that approve or refuse")
        assertEquals(
            listOf(
                HonestyLine("greeter.py: DISHONEST. The docstring says Hello, the code returns Good day.", "error", "/p/greeter.py"),
                HonestyLine("util.py: HONEST", "ok", "/p/util.py"),
                HonestyLine("Honesty checker asks a person: A test was deleted.", "warning"),
            ),
            row.honesty,
        )
        assertTrue(row.honesty.none { "PASS" in it.text || "ALLOW" in it.text })
        assertTrue(row.detail.contains("Honesty checker, dishonest units:\n/p/greeter.py · greet: docstring and code disagree"))
        assertTrue(honestyLines(null).isEmpty())
        assertEquals("", honestyUnitsText(HonestyResultView(listOf(HonestyFileView("a", true)))))
    }
}
