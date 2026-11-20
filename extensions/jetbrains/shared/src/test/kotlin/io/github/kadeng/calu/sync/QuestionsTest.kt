package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The synchronisation's questions: texts, and which button Enter presses. */
class QuestionsTest {
    private fun plan(deleteLocal: List<String> = emptyList(), deleteRemote: List<String> = emptyList(), localFiles: Int = 3) =
        PlanSummary("box:/p", listOf("a.py"), 10, emptyList(), deleteLocal, deleteRemote, emptyList(), 2, localFiles)

    @Test
    fun `changed on the server, deleted here has no default button and the focus on Cancel`() {
        val q = sideChoice("k_b.txt", "changed on the server, deleted here", mineExists = false, theirsExists = true)
        assertEquals(listOf("Keep Mine", "Take Theirs", "Cancel"), q.options)
        assertNull(q.defaultIndex)
        assertEquals(SIDE_CANCEL, q.focusIndex)
        assertEquals(SIDE_CANCEL, q.cancelIndex)
        assertTrue(q.message.contains("Keep Mine: the server gets the deletion."), q.message)
        assertTrue(q.message.contains("Take Theirs: this machine gets the server's file."), q.message)
    }

    @Test
    fun `every side choice has no default, whichever side is deleted`() {
        for ((mine, theirs) in listOf(true to false, false to true, true to true)) {
            val q = sideChoice("x.bin", "a binary file changed on both sides", mine, theirs)
            assertNull(q.defaultIndex)
            assertEquals(SIDE_CANCEL, q.focusIndex)
        }
        val deletedThere = sideChoice("a.py", "deleted on the server, changed here", mineExists = true, theirsExists = false)
        assertTrue(deletedThere.message.contains("Take Theirs: this machine gets the deletion."), deletedThere.message)
    }

    @Test
    fun `a plan that deletes has no default, one that does not keeps Apply`() {
        val safe = planQuestion(plan())
        assertEquals(PLAN_APPLY, safe.defaultIndex)
        assertEquals("Apply", safe.options[PLAN_APPLY])
        for (q in listOf(planQuestion(plan(deleteLocal = listOf("old.py"))), planQuestion(plan(deleteRemote = listOf("old.py"))))) {
            assertNull(q.defaultIndex)
            assertEquals(PLAN_NOT_NOW, q.focusIndex)
            assertEquals(PLAN_NOT_NOW, q.cancelIndex)
        }
        val clone = planQuestion(plan(localFiles = 0))
        assertEquals("Clone from Server", clone.options[PLAN_APPLY])
        assertEquals(PLAN_APPLY, clone.defaultIndex)
    }

    @Test
    fun `run while files are held asks with three answers`() {
        val one = heldRunQuestion(listOf("greeter.py"))
        assertEquals(listOf("Upload now and run", "Run anyway", "Cancel"), one.options)
        assertTrue(one.message.startsWith("1 file is held until Claude's turn ends; the server still runs the old version."), one.message)
        assertTrue(one.message.contains("greeter.py"))
        assertEquals(0, one.defaultIndex)
        assertEquals(2, one.cancelIndex)
        val many = heldRunQuestion((1..10).map { "f$it.py" })
        assertTrue(many.message.startsWith("10 files are held until Claude's turn ends; the server still runs the old version."), many.message)
        assertTrue(many.message.contains("f8.py") && !many.message.contains("f9.py") && many.message.endsWith("..."), many.message)
        assertThrows(IllegalArgumentException::class.java) { heldRunQuestion(emptyList()) }
    }

    @Test
    fun `answers map to choices, a closed dialog cancels`() {
        assertEquals(HeldRunChoice.UPLOAD_AND_RUN, heldRunChoice(0))
        assertEquals(HeldRunChoice.RUN_ANYWAY, heldRunChoice(1))
        assertEquals(HeldRunChoice.CANCEL, heldRunChoice(2))
        assertEquals(HeldRunChoice.CANCEL, heldRunChoice(-1))
    }

    @Test
    fun `files left after the upload make Run anyway no default`() {
        val q = notUploadedQuestion(listOf("greeter.py"))
        assertEquals(listOf("Run anyway", "Cancel"), q.options)
        assertNull(q.defaultIndex)
        assertEquals(1, q.focusIndex)
        assertTrue(q.message.startsWith("1 file was not uploaded"), q.message)
        assertThrows(IllegalArgumentException::class.java) { notUploadedQuestion(emptyList()) }
    }

    @Test
    fun `a question refuses buttons out of range`() {
        assertThrows(IllegalArgumentException::class.java) { Question("t", "m", listOf("A"), defaultIndex = 1, focusIndex = 0, cancelIndex = 0) }
        assertThrows(IllegalArgumentException::class.java) { Question("t", "m", emptyList(), null, 0, 0) }
    }
}
