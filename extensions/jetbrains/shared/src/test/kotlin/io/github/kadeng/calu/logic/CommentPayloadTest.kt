package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.jo
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The request of a comment on a selection (port of `commentPayload.test.ts`). */
class CommentPayloadTest {
    private fun sel(sl: Int, sc: Int, el: Int, ec: Int) = PlainSelection.of(sl, sc, el, ec)

    private fun refused(pattern: String, block: () -> Unit) {
        val error = assertThrows(IllegalArgumentException::class.java) { block() }
        assertTrue(error.message!!.contains(pattern), error.message)
    }

    @Test
    fun `uses the cursor line for an empty selection`() {
        assertEquals(LineRange(5, 5), selectionToLines(sel(4, 3, 4, 3)))
    }

    @Test
    fun `converts to 1-based inclusive lines`() {
        assertEquals(LineRange(1, 3), selectionToLines(sel(0, 2, 2, 5)))
    }

    @Test
    fun `excludes a last line selected only up to column 0`() {
        assertEquals(LineRange(5, 7), selectionToLines(sel(4, 0, 7, 0)))
    }

    @Test
    fun `keeps a one-line selection`() {
        assertEquals(LineRange(4, 4), selectionToLines(sel(3, 0, 3, 4)))
    }

    @Test
    fun `describes refs`() {
        assertEquals("HEAD", describeGitRef("HEAD"))
        assertEquals("index", describeGitRef(""))
        assertEquals("index or HEAD", describeGitRef("~"))
        assertEquals("edit:t-1", editRef("t-1"))
    }

    @Test
    fun `uses forward slashes`() {
        assertEquals("src/a.py", normaliseRelativePath("src\\a.py"))
        assertEquals("a.py", normaliseRelativePath("./a.py"))
    }

    @Test
    fun `rejects paths outside the workspace`() {
        refused("not inside") { normaliseRelativePath("/etc/passwd") }
        refused("not inside") { normaliseRelativePath("C:\\x\\a.py") }
        refused("not inside") { normaliseRelativePath("") }
    }

    @Test
    fun `computes paths inside the folder`() {
        assertEquals("src/a.py", relativePathWithin("/w/proj", "/w/proj/src/a.py"))
        assertEquals("a.py", relativePathWithin("/w/proj/", "/w/proj/a.py"))
    }

    @Test
    fun `a diff side title names the file and reads it back`() {
        assertEquals("src/a.py · after the edit", diffSideTitle("src/a.py", "after"))
        assertEquals("src/a.py", pathFromDiffSideTitle(diffSideTitle("src/a.py", "after")))
        assertEquals("/w/proj/a b.py", pathFromDiffSideTitle(diffSideTitle("/w/proj/a b.py", "before")))
        assertEquals(null, pathFromDiffSideTitle("Before the edit"))
        assertEquals(null, pathFromDiffSideTitle(" · after the edit"))
        assertEquals(null, pathFromDiffSideTitle(null))
        refused("before or after") { diffSideTitle("a.py", "left") }
    }

    @Test
    fun `the comment path comes from a relative or an absolute name inside the project`() {
        assertEquals("src/a.py", commentPathFromShown("/w/proj", "src/a.py"))
        assertEquals("src/a.py", commentPathFromShown("/w/proj", "/w/proj/src/a.py"))
        assertEquals("src/a.py", commentPathFromShown("C:\\w\\proj", "C:\\w\\proj\\src\\a.py"))
        assertEquals(null, commentPathFromShown("/w/proj", "/etc/passwd"))
        assertEquals(null, commentPathFromShown("/w/proj", "../x.py"))
    }

    @Test
    fun `rejects files outside the folder, sibling prefixes and dot-dot`() {
        refused("not inside") { relativePathWithin("/w/proj", "/w/other/a.py") }
        refused("not inside") { relativePathWithin("/w/proj", "/w/project2/a.py") }
        refused("not inside") { relativePathWithin("/w/proj", "/w/proj") }
        refused("not inside") { relativePathWithin("/w/proj", "/w/proj/../etc/passwd") }
        refused("not inside") { normaliseRelativePath("src/../../x") }
    }

    @Test
    fun `builds a file comment`() {
        assertEquals(
            jo("kind" to "file", "path" to "src/a.py", "side" to "after", "start_line" to 2, "end_line" to 3, "after_text" to "l1\nl2\nl3\n", "text" to "explain"),
            buildCommentParams(CommentInput("src/a.py", sel(1, 0, 3, 0), "  explain  ", fileText = "l1\nl2\nl3\n")),
        )
    }

    @Test
    fun `builds a diff comment on the before side with a ref`() {
        assertEquals(
            jo(
                "kind" to "diff", "path" to "a.py", "side" to "before", "start_line" to 1, "end_line" to 1,
                "before_text" to "old\n", "after_text" to "new\n", "ref" to "HEAD", "text" to "why removed?",
            ),
            buildCommentParams(CommentInput("a.py", sel(0, 0, 0, 0), "why removed?", diff = CommentDiff("before", "old\n", "new\n", "HEAD"))),
        )
    }

    @Test
    fun `omits ref when unknown`() {
        val params = buildCommentParams(CommentInput("a.py", sel(0, 0, 0, 0), "x", diff = CommentDiff("after", "", "n")))
        assertFalse(params.containsKey("ref"))
    }

    @Test
    fun `rejects empty comments and ambiguous input`() {
        refused("empty") { buildCommentParams(CommentInput("a", sel(0, 0, 0, 0), "  ", fileText = "")) }
        refused("exactly one") { buildCommentParams(CommentInput("a", sel(0, 0, 0, 0), "x")) }
        refused("exactly one") { buildCommentParams(CommentInput("a", sel(0, 0, 0, 0), "x", fileText = "", diff = CommentDiff("after", "", ""))) }
        refused("side") { buildCommentParams(CommentInput("a", sel(0, 0, 0, 0), "x", diff = CommentDiff("left", "", ""))) }
    }

    @Test
    fun `describes the commented lines for the prompt`() {
        assertEquals("src/a.py, lines 3-5 (before the change)", describeCommentTarget("src/a.py", sel(2, 0, 5, 0), "before"))
        assertEquals("a.py, line 7", describeCommentTarget("a.py", sel(6, 2, 6, 2)))
        assertEquals("a.py, lines 1-2 (after the change)", describeCommentTarget("a.py", sel(0, 0, 1, 3), "after"))
    }
}
