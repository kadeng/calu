package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Coalescing, order and persistence of [PendingQueue]. */
class PendingQueueTest {
    @Test fun `repeated saves of one path coalesce and keep the first position`() {
        val queue = PendingQueue()
        queue.saved("a")
        queue.saved("b")
        queue.saved("a")
        assertEquals(listOf(PendingOp("a", PendingKind.SAVE), PendingOp("b", PendingKind.SAVE)), queue.ops())
        assertEquals(2, queue.size)
    }

    @Test fun `a delete after a save replaces it, and a save after a delete replaces that`() {
        val queue = PendingQueue()
        queue.saved("a")
        queue.deleted("a")
        assertEquals(listOf(PendingOp("a", PendingKind.DELETE)), queue.ops())
        queue.saved("a")
        assertEquals(listOf(PendingOp("a", PendingKind.SAVE)), queue.ops())
    }

    @Test fun `a rename is a delete followed by a save`() {
        val queue = PendingQueue()
        queue.saved("x")
        queue.renamed("a", "b")
        assertEquals(
            listOf(PendingOp("x", PendingKind.SAVE), PendingOp("a", PendingKind.DELETE), PendingOp("b", PendingKind.SAVE)),
            queue.ops(),
        )
    }

    @Test fun `remove drops a path and ignores unknown ones`() {
        val queue = PendingQueue()
        queue.saved("a")
        queue.remove("b")
        queue.remove("a")
        assertTrue(queue.isEmpty())
    }

    @Test fun `encode and decode keep entries and order`() {
        val queue = PendingQueue()
        queue.saved("z/last first.txt")
        queue.deleted("a")
        queue.saved("m")
        assertEquals("S\tz/last first.txt\nD\ta\nS\tm\n", queue.encode())
        assertEquals(queue.ops(), PendingQueue.decode(queue.encode()).ops())
        assertTrue(PendingQueue.decode("").isEmpty())
        assertThrows(IllegalArgumentException::class.java) { PendingQueue.decode("X\ta\n") }
    }
}
