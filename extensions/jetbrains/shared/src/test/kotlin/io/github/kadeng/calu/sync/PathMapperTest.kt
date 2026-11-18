package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The path rules and the mapping between the two roots ([PathMapper]). */
class PathMapperTest {
    private val linux = PathMapper(localRoot = "/home/u/copy", remoteRoot = "/srv/proj")
    private val windows = PathMapper(localRoot = "C:\\work\\proj", remoteRoot = "/srv/proj", windowsLocal = true)

    @Test fun `valid relative paths have no problem`() {
        for (path in listOf("a", "src/a.py", ".gitignore", ".agent/notes.md", "a b/ü.txt", "x/.github/ci.yml", "a..b/c")) {
            assertNull(linux.problem(path), path)
        }
    }

    @Test fun `problems are named`() {
        assertEquals(PathProblem.EMPTY, linux.problem(""))
        assertEquals(PathProblem.ABSOLUTE, linux.problem("/etc/passwd"))
        assertEquals(PathProblem.ABSOLUTE, linux.problem("C:/Windows/x"))
        assertEquals(PathProblem.PARENT_SEGMENT, linux.problem("../x"))
        assertEquals(PathProblem.PARENT_SEGMENT, linux.problem("a/../../x"))
        assertEquals(PathProblem.PARENT_SEGMENT, linux.problem("a/.."))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("a\\b"))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("a\u0000b"))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("a\nb"))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("a//b"))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("./a"))
        assertEquals(PathProblem.INVALID_CHARACTER, linux.problem("a/"))
    }

    @Test fun `the excluded directories are rejected at the documented places`() {
        for (path in listOf(".git", ".git/config", "sub/.git/HEAD", ".GIT/config", ".agent/traces", ".agent/traces/2026/s.md", ".Agent/Traces/x")) {
            assertEquals(PathProblem.EXCLUDED, linux.problem(path), path)
            assertTrue(linux.isExcluded(path), path)
        }
        for (path in listOf(".gitignore", ".github/x", ".agent/notes.md", "sub/.agent/traces/x", "git/config")) {
            assertFalse(linux.isExcluded(path), path)
        }
        val more = PathMapper(excluded = PathMapper.DEFAULT_EXCLUDED + ".calu")
        assertEquals(PathProblem.EXCLUDED, more.problem("x/.calu/state.db"))
    }

    @Test fun `names Windows cannot hold are rejected only for a Windows local side`() {
        for (path in listOf("a:b", "x/what?.txt", "x/star*.txt", "trailing./a", "trailing /a", "NUL", "x/con.txt", "COM1", "a<b", "q\"q", "p|p")) {
            val expected = if (path == "a:b") PathProblem.ABSOLUTE else PathProblem.NOT_REPRESENTABLE_LOCALLY
            assertEquals(expected, windows.problem(path), path)
        }
        assertNull(linux.problem("x/what?.txt"))
        assertNull(linux.problem("x/con.txt"))
        assertNull(windows.problem("console.txt"))
    }

    @Test fun `normalize brings a path into canonical form`() {
        assertEquals("src/a.py", linux.normalize("./src//a.py"))
        assertEquals("src/a.py", linux.normalize("src/a.py/"))
        assertEquals("src/a.py", windows.normalize(".\\src\\a.py"))
        assertEquals("src/a.py", windows.normalize("src/a.py"))
    }

    @Test fun `normalize rejects absolute paths, parent segments and excluded directories`() {
        for (path in listOf("/etc/passwd", "../x", "a/../../x", ".git/config", ".agent/traces/x.md", "", ".", "a\\b")) {
            assertThrows(IllegalArgumentException::class.java, { linux.normalize(path) }, path)
        }
        for (path in listOf("C:\\Windows\\x", "\\\\server\\share\\x", "..\\x", "a\\..\\..\\x", ".git\\config", "\\x")) {
            assertThrows(IllegalArgumentException::class.java, { windows.normalize(path) }, path)
        }
    }

    @Test fun `local paths map to relative paths and back`() {
        assertEquals("src/a.py", linux.toRelativeLocal("/home/u/copy/src/a.py"))
        assertEquals("/home/u/copy/src/a.py", linux.toLocal("src/a.py"))
        assertEquals("src/a.py", windows.toRelativeLocal("C:\\work\\proj\\src\\a.py"))
        assertEquals("src/a.py", windows.toRelativeLocal("c:/WORK/proj/src/a.py"))
        assertEquals("C:\\work\\proj\\src\\a.py", windows.toLocal("src/a.py"))
    }

    @Test fun `local paths outside the root or in excluded directories map to null`() {
        assertNull(linux.toRelativeLocal("/home/u/copy"))
        assertNull(linux.toRelativeLocal("/home/u/copy2/a"))
        assertNull(linux.toRelativeLocal("/home/u/copy/../other/a"))
        assertNull(linux.toRelativeLocal("/home/u/copy/.git/config"))
        assertNull(linux.toRelativeLocal("/HOME/u/copy/a"))
        assertNull(windows.toRelativeLocal("D:\\work\\proj\\a"))
        assertNull(windows.toRelativeLocal("C:\\work\\proj\\..\\x"))
    }

    @Test fun `remote paths map to relative paths and back`() {
        assertEquals("src/a.py", linux.toRelativeRemote("/srv/proj/src/a.py"))
        assertEquals("src/a.py", linux.toRelativeRemote("src/a.py"))
        assertEquals("src/a.py", linux.toRelativeRemote("./src/a.py"))
        assertEquals("/srv/proj/src/a.py", linux.toRemote("src/a.py"))
        assertNull(linux.toRelativeRemote("/srv/proj"))
        assertNull(linux.toRelativeRemote("/srv/project/a"))
        assertNull(linux.toRelativeRemote("/srv/proj/../x"))
        assertNull(linux.toRelativeRemote("../x"))
        assertNull(linux.toRelativeRemote("/srv/proj/.git/HEAD"))
        assertNull(windows.toRelativeRemote("/srv/proj/a:b"))
    }

    @Test fun `toLocal and toRemote refuse paths that break a rule`() {
        assertThrows(IllegalArgumentException::class.java) { linux.toLocal("../x") }
        assertThrows(IllegalArgumentException::class.java) { linux.toRemote("/abs") }
        assertThrows(IllegalArgumentException::class.java) { linux.toRemote(".git/config") }
    }

    @Test fun `case collisions are found for files and for directories`() {
        assertEquals(emptySet<String>(), linux.caseCollisions(listOf("a", "b/c", "b/d")))
        assertEquals(setOf("README.md", "Readme.md"), linux.caseCollisions(listOf("README.md", "Readme.md", "other")))
        assertEquals(setOf("Src/a.py", "src/b.py"), linux.caseCollisions(listOf("Src/a.py", "src/b.py", "lib/c.py")))
        assertEquals(setOf("x/Y/a", "x/y/b", "x/y/c"), linux.caseCollisions(listOf("x/Y/a", "x/y/b", "x/y/c", "x/z")))
    }

    @Test fun `the defaults follow the local operating system flag`() {
        assertTrue(windows.caseInsensitiveLocal)
        assertFalse(linux.caseInsensitiveLocal)
        assertEquals(listOf(".git", ".agent/traces"), PathMapper.DEFAULT_EXCLUDED)
    }
}
