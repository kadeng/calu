package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ToolFile
import io.github.kadeng.calu.state.ToolView
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** Links for the files in Claude's text that tool calls named. */
class FileLinksTest {
    private val index = buildFileIndex(mapOf("t1" to ToolView("t1", "main", "Read", files = listOf(ToolFile("greeter.py", "/p/my dir/greeter.py")))))

    @Test
    fun `a code span that names a known file becomes a link, with its line`() {
        val html = markdownToHtml("I changed `greeter.py` and `greeter.py:3`, not `other.py` or `x < y`.")
        val linked = linkFiles(html, index)
        // A file no tool call touched is a source reference: a link to the relative path,
        // which the IDE resolves against the session's directory.
        assertEquals(
            "<p>I changed <a href=\"calu-file:%2Fp%2Fmy+dir%2Fgreeter.py\"><code>greeter.py</code></a> and " +
                "<a href=\"calu-file:%2Fp%2Fmy+dir%2Fgreeter.py#L3\"><code>greeter.py:3</code></a>, not " +
                "<a href=\"calu-file:other.py\"><code>other.py</code></a> or <code>x &lt; y</code>.</p>",
            linked,
        )
        assertEquals(FileRef("/p/my dir/greeter.py", 3), parseFileLink("calu-file:%2Fp%2Fmy+dir%2Fgreeter.py#L3"))
        assertEquals(FileRef("/p/my dir/greeter.py"), parseFileLink("calu-file:%2Fp%2Fmy+dir%2Fgreeter.py"))
        assertNull(parseFileLink("https://example.org/greeter.py"))
        assertNull(parseFileLink("calu-file:"))
        assertNull(parseFileLink("calu-file:%zz"))
    }
}
