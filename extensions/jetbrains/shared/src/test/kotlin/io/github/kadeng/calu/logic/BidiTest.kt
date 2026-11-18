package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Bidirectional control characters in untrusted text are made visible (port of `bidi.test.ts`). */
class BidiTest {
    @Test
    fun `shows an override as a placeholder, so that the stored order is what is read`() {
        assertEquals("rm -rf ⟦RLO⟧gnp.tropxe", neutraliseBidi("rm -rf ‮gnp.tropxe"))
        assertEquals(
            "a⟦LRI⟧b⟦PDI⟧c⟦RLM⟧d⟦ALM⟧e",
            neutraliseBidi("a⁦b⁩c‏d؜e"),
        )
    }

    @Test
    fun `replaces every character of the list and nothing else`() {
        for ((code, name) in BIDI_CONTROLS) {
            val c = String(Character.toChars(code))
            assertEquals("x⟦$name⟧y", neutraliseBidi("x${c}y"))
            assertTrue(hasBidiControls(c))
        }
        assertEquals(12, BIDI_CONTROLS.size)
    }

    @Test
    fun `leaves ordinary text alone, also right-to-left scripts and other invisible characters`() {
        val plain = "English, العربية, עברית, 日本語, zero​width, é, 👩‍💻"
        assertSame(plain, neutraliseBidi(plain))
        assertFalse(hasBidiControls(plain))
        assertEquals("", neutraliseBidi(""))
    }

    @Test
    fun `is stable when applied twice`() {
        val once = neutraliseBidi("‭abc‬")
        assertEquals(once, neutraliseBidi(once))
    }
}
