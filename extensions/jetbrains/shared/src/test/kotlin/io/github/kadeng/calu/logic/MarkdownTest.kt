package io.github.kadeng.calu.logic

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The transcript's Markdown converter and the context documents. */
class MarkdownTest {
    @Test
    fun `source references in text, code spans and links open the file at the line`() {
        val html = markdownToHtml("See src/app.py:12 and `docs/guide.md`, or [this](lib/x.rs#L7).")
        assertTrue(html.contains("<a href=\"calu-file:src%2Fapp.py#L12\">src/app.py:12</a>"), html)
        assertTrue(html.contains("<a href=\"calu-file:lib%2Fx.rs#L7\">this</a>"), html)
        val linked = linkFiles(html, buildFileIndex(emptyMap()))
        assertTrue(linked.contains("<a href=\"calu-file:docs%2Fguide.md\"><code>docs/guide.md</code></a>"), linked)
        assertEquals(FileRef("src/app.py", 12), parseFileLink("calu-file:src%2Fapp.py#L12"))
    }

    @Test
    fun `URLs, absolute paths and unknown extensions are no source references`() {
        val html = markdownToHtml("https://e.org/a.py /etc/x.conf picture.png version 1.2.3 jane@x.md")
        assertFalse(html.contains("calu-file:"), html)
        assertEquals(null, sourceRefOf("app.pyc"))
        assertEquals(FileRef("hello_world.py", 1293), sourceRefOf("hello_world.py:1293"))
        assertEquals(FileRef("a.py"), sourceRefOf("a.py:0"))
    }

    @Test
    fun `paragraphs, inline marks and line breaks`() {
        assertEquals("<p>a <b>b</b> <i>c</i> <code>d*e*</code><br>next</p><p>two</p>", markdownToHtml("a **b** *c* `d*e*`\nnext\n\ntwo"))
    }

    @Test
    fun `markup of the input never reaches the output`() {
        val html = markdownToHtml("<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://e.org/a?b=1&c=2) \"q\"")
        assertFalse(html.contains("<script"))
        assertFalse(html.contains("href=\"javascript"))
        assertTrue(html.contains("<a href=\"https://e.org/a?b=1&amp;c=2\">ok</a>"))
        assertTrue(html.contains("&lt;script&gt;"))
    }

    @Test
    fun `code fences keep their text, also when the fence is not closed yet`() {
        assertEquals("<p>run</p><div class=\"pre\">a &lt; b<br>**x**</div><p>done</p>", markdownToHtml("run\n```sh\na < b\n**x**\n```\ndone"))
        // Indentation and column gaps survive, and the line can wrap at the single spaces left.
        assertEquals("<div class=\"pre\">def f():<br>&nbsp;&nbsp;&nbsp;&nbsp;x = 1 &nbsp; # two &nbsp;gaps<br>&nbsp;<br>&nbsp;&nbsp;&nbsp;&nbsp;tab</div>", markdownToHtml("```\ndef f():\n    x = 1   # two  gaps\n\n\ttab\n```"))
        assertEquals("<div class=\"pre\">still streaming</div>", markdownToHtml("```\nstill streaming"))
    }

    @Test
    fun `headings, lists, quotes and rules`() {
        assertEquals("<h3>T</h3><ul><li>a</li><li>b</li></ul><ol><li>one</li></ol><blockquote>q</blockquote><hr>", markdownToHtml("# T\n- a\n* b\n1. one\n> q\n---"))
    }

    @Test
    fun `a pipe table becomes a table, and a line with a pipe alone stays text`() {
        val html = markdownToHtml("| file | change |\n|---|---|\n| `a.py` | the <b> word |\n\nafter | this")
        assertTrue(html.contains("<th align=\"left\">file</th><th align=\"left\">change</th>"), html)
        assertTrue(html.contains("<tr><td><code>a.py</code></td><td>the &lt;b&gt; word</td></tr></table>"), html)
        assertTrue(html.endsWith("<p>after | this</p>"), html)
    }

    @Test
    fun `quoted text gets lower headings outside fences and a fence it cannot close`() {
        assertEquals("## A\n```\n# not\n```\n### B", demoteHeadings("# A\n```\n# not\n```\n## B"))
        assertEquals("````\na ``` b\n````", fenced("a ``` b"))
    }

    @Test
    fun `the escalation document names state, reason and step`() {
        val text = formatEscalationContext(buildJsonObject {
            put("escalation_id", JsonPrimitive("e-1")); put("state", JsonPrimitive("open"))
            put("reason", JsonPrimitive("runs code")); put("tool_name", JsonPrimitive("Bash"))
            put("tool_input", buildJsonObject { put("command", JsonPrimitive("make")) })
        })
        assertTrue(text.startsWith("# Escalation e-1\n\n- **State:** open\n- **Tool:** Bash\n"))
        assertTrue(text.contains("# Reason\n\nruns code"))
        assertTrue(text.contains("\"command\": \"make\""))
        assertTrue(text.contains("_No checker ran"))
        assertTrue(formatCheckerContext(buildJsonObject { }, "12:00").contains("_Nothing folded yet._"))
    }
}
