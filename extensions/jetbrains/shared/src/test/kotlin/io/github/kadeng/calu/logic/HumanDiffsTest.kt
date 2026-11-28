package io.github.kadeng.calu.logic

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The user's saves as recorded diffs (decision D253 in `agent/records/PLAN.md`): params, gate, the settings offer. */
class HumanDiffsTest {
    @Test
    fun `a save is sent with its path relative to the project and line ends as newlines`() {
        val params = humanDiffParams("/w", "/w/doc/a.md", "a\r\n", "b\n")!!
        assertEquals(JsonPrimitive("doc/a.md"), params["path"])
        assertEquals(JsonPrimitive("a\n"), params["before"])
        assertEquals(JsonPrimitive("b\n"), params["after"])
        val windows = humanDiffParams("C:\\work\\copy\\", "C:\\work\\copy\\src\\a.py", "1", "2")!!
        assertEquals(JsonPrimitive("src/a.py"), windows["path"])
    }

    @Test
    fun `nothing is sent outside the project, without a before, for equal or long texts`() {
        assertNull(humanDiffParams("/w", "/elsewhere/a.md", "a", "b"))
        assertNull(humanDiffParams("/w", "/w2/a.md", "a", "b"))
        assertNull(humanDiffParams("/w", "/w/a.md", null, "b"))
        assertNull(humanDiffParams("/w", "/w/a.md", "a\r\n", "a\n"))
        assertNull(humanDiffParams("/w", "/w/../x", "a", "b"))
        assertNull(humanDiffParams("/w", "/w/a.md", "x".repeat(HUMAN_DIFF_MAX_CHARS + 1), "b"))
        assertEquals("a\nb\nc", normaliseLineEnds("a\rb\r\nc"))
    }

    @Test
    fun `the gate sends only while attached and pauses after an off answer`() {
        val gate = HumanDiffGate()
        assertFalse(gate.wanted(false, 0))
        assertTrue(gate.wanted(true, 0))
        gate.heard(humanDiffEnabled(buildJsonObject { put("enabled", JsonPrimitive(false)) }), 1000)
        assertFalse(gate.wanted(true, 1000 + HUMAN_DIFF_OFF_PAUSE_MS - 1))
        assertTrue(gate.wanted(true, 1000 + HUMAN_DIFF_OFF_PAUSE_MS))
        assertTrue(humanDiffEnabled(null))
    }

    @Test
    fun `the offer of a settings answer is read`() {
        val answer = buildJsonObject {
            put("value", JsonPrimitive(true))
            put("offer", buildJsonObject { put("key", JsonPrimitive("traces.enabled")); put("text", JsonPrimitive("Also?")) })
        }
        assertEquals(EnableOffer("traces.enabled", "Also?"), enableOffer(answer))
        assertNull(enableOffer(buildJsonObject { put("value", JsonPrimitive(true)) }))
        assertNull(enableOffer(JsonObject(mapOf("offer" to buildJsonObject { put("key", JsonPrimitive(3)) }))))
        assertNull(enableOffer(null))
    }

    @Test
    fun `a normal save is diffed against the loaded text, the next against the saved one`() {
        val bases = SaveBases<String>()
        bases.loaded("a.md", "v1\n")
        val first = humanDiffParams("/w", "/w/a.md", bases.saving("a.md", "v1 mine\n")?.toString(), "v1 mine\n")!!
        assertEquals(JsonPrimitive("v1\n"), first["before"])
        val second = humanDiffParams("/w", "/w/a.md", bases.saving("a.md", "v2\n")?.toString(), "v2\n")!!
        assertEquals(JsonPrimitive("v1 mine\n"), second["before"])
    }

    @Test
    fun `a save that overwrites the version Claude wrote meanwhile is diffed against the buffer's start`() {
        val bases = SaveBases<String>()
        bases.loaded("a.md", "a\nb\n")
        // Claude writes "a\nb claude\n" on disk; the user keeps the memory changes and saves.
        val params = humanDiffParams("/w", "/w/a.md", bases.saving("a.md", "a mine\nb\n")?.toString(), "a mine\nb\n")!!
        assertEquals(JsonPrimitive("a\nb\n"), params["before"])
        assertEquals(JsonPrimitive("a mine\nb\n"), params["after"])
    }

    @Test
    fun `a save after the user loaded Claude's version is diffed against that version`() {
        val bases = SaveBases<String>()
        bases.loaded("a.md", "a\nb\n")
        bases.loaded("a.md", "a\nb claude\n") // fileContentReloaded
        val params = humanDiffParams("/w", "/w/a.md", bases.saving("a.md", "a\nb claude mine\n")?.toString(), "a\nb claude mine\n")!!
        assertEquals(JsonPrimitive("a\nb claude\n"), params["before"])
        assertNull(SaveBases<String>().saving("unknown.md", "x"))
    }
}
