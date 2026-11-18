package io.github.kadeng.calu.logic

import io.github.kadeng.calu.logic.InlineKind.CODE
import io.github.kadeng.calu.logic.InlineKind.STRONG
import io.github.kadeng.calu.logic.InlineKind.TEXT
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The inline marks of calu's descriptions (port of `inlineText.test.ts`). */
class InlineTextTest {
    @Test
    fun `splits code and strong spans from the text around them`() {
        assertEquals(
            listOf(InlinePart(TEXT, "Set "), InlinePart(CODE, "a.b"), InlinePart(TEXT, " to "), InlinePart(STRONG, "true"), InlinePart(TEXT, " now")),
            inlineParts("Set `a.b` to **true** now"),
        )
        assertEquals(
            listOf(InlinePart(CODE, "calu config set claude.sandbox.enabled false")),
            inlineParts("`calu config set claude.sandbox.enabled false`"),
        )
        assertEquals(listOf(InlinePart(STRONG, "two words")), inlineParts("**two words**"))
    }

    @Test
    fun `leaves marks without a partner, empty spans and spaced asterisks as written`() {
        for (plain in listOf("a ` b", "a ** b", "``", "****", "a ** b ** c", "2 * 3 * 4", "line `one\ntwo` end", "")) {
            assertEquals(plain, plainInline(plain))
            assertTrue(inlineParts(plain).all { it.kind == TEXT })
        }
        assertEquals(emptyList<InlinePart>(), inlineParts(""))
    }

    @Test
    fun `does not look for marks inside a code span, and does not nest`() {
        assertEquals(listOf(InlinePart(CODE, "a **b** c")), inlineParts("`a **b** c`"))
        assertEquals(listOf(InlinePart(STRONG, "a `b` c")), inlineParts("**a `b` c**"))
    }

    @Test
    fun `keeps markup characters as text, nothing is HTML`() {
        assertEquals(
            listOf(InlinePart(TEXT, "<img src=x onerror=alert(1)> "), InlinePart(CODE, "<b>"), InlinePart(TEXT, " "), InlinePart(STRONG, "<i>")),
            inlineParts("<img src=x onerror=alert(1)> `<b>` **<i>**"),
        )
        assertEquals("use x now", plainInline("use `x` **now**"))
    }
}
