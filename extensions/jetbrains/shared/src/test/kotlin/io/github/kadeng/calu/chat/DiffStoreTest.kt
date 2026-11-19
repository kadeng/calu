package io.github.kadeng.calu.chat

import io.github.kadeng.calu.state.jo
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** The diffs the backend keeps, and finding one by the text a diff editor shows. */
class DiffStoreTest {
    private fun edit(id: String, path: String, before: String, after: String) =
        jo("tool_use_id" to id, "path" to path, "before" to before, "after" to after)

    @Test
    fun `finds the newest diff with the text on either side, after first`() {
        val store = DiffStore()
        store.record(edit("t1", "a.py", "x = 1\n", "x = 2\n"))
        store.record(edit("t2", "b.py", "x = 2\n", "x = 3\n"))
        assertEquals("b.py" to "before", store.findByText("x = 2\n")?.let { it.first.path to it.second })
        assertEquals("a.py" to "before", store.findByText("x = 1\n")?.let { it.first.path to it.second })
        assertEquals("b.py" to "after", store.findByText("x = 3\n")?.let { it.first.path to it.second })
        assertNull(store.findByText("y"))
    }

    @Test
    fun `a cleared store finds nothing`() {
        val store = DiffStore()
        store.record(edit("t1", "a.py", "", "new\n"))
        store.clear()
        assertNull(store.findByText("new\n"))
    }
}
