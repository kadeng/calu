package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

/** [SyncState]: immutability and the text form. */
class SyncStateTest {
    @Test fun `with and without return new states`() {
        val empty = SyncState()
        val one = empty.with("a", h("1"))
        assertNull(empty.base("a"))
        assertEquals(h("1"), one.base("a"))
        assertNull(one.with("a", null).base("a"))
        assertEquals(setOf("a", "b"), one.with("b", h("2")).paths)
        assertEquals("f".repeat(40), one.withHead("f".repeat(40)).head)
        assertNotEquals(one, empty)
    }

    @Test fun `a malformed hash is refused`() {
        assertThrows(IllegalArgumentException::class.java) { SyncState().with("a", "ABC") }
        assertThrows(IllegalArgumentException::class.java) { SyncState(null, mapOf("a" to h("1").uppercase())) }
    }

    @Test fun `encode writes the documented format`() {
        val state = SyncState("a".repeat(40), mapOf("src/b.py" to h("2"), "a.txt" to h("1")))
        assertEquals(
            "calu-sync-state 1\nhead ${"a".repeat(40)}\n${h("1")}\ta.txt\n${h("2")}\tsrc/b.py\n",
            state.encode(),
        )
        assertEquals("calu-sync-state 1\nhead -\n", SyncState().encode())
    }

    @Test fun `decode reverses encode, also for unusual names`() {
        val state = SyncState(
            null,
            mapOf("with space.txt" to h("1"), "tab\there" to h("2"), "back\\slash" to h("3"), "line\nfeed\r" to h("4"), "ünï/代码.py" to h("5")),
        )
        assertEquals(state, SyncState.decode(state.encode()))
        val withHead = state.withHead("0123456789".repeat(4))
        assertEquals(withHead, SyncState.decode(withHead.encode()))
        assertEquals(withHead, SyncState.decode(withHead.encode().replace("\n", "\r\n")))
        assertEquals(SyncState(), SyncState.decode(SyncState().encode()))
    }

    @Test fun `decode refuses other texts`() {
        for (text in listOf(
            "",
            "something else\n",
            "calu-sync-state 2\nhead -\n",
            "calu-sync-state 1\n",
            "calu-sync-state 1\nhead -\nno-tab-here\n",
            "calu-sync-state 1\nhead -\nnothex\ta\n",
            "calu-sync-state 1\nhead -\n${h("1")}\ta\n${h("2")}\ta\n",
            "calu-sync-state 1\nhead -\n${h("1")}\tbad\\escape\n",
        )) {
            assertThrows(IllegalArgumentException::class.java, { SyncState.decode(text) }, text)
        }
    }
}
