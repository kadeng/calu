package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests of finding calu and other programs on the workspace machine, ported from
 * `extensions/vscode/test/unit/caluCommand.test.ts`.
 */
class CaluCommandTest {
    /**
     * Build the facts of a machine where exactly the given paths are executable.
     *
     * @param executables The paths of the executable files.
     * @param pathEnv Value of PATH.
     * @param pathDelimiter Separator of PATH entries.
     * @param home The home directory, or "".
     * @param isWsl True for a WSL machine.
     * @param isWindows True for a Windows machine.
     * @return The facts (a Linux machine by default).
     */
    private fun facts(
        executables: List<String>,
        pathEnv: String = "/usr/local/bin:/mnt/c/Users/me/bin:/usr/bin",
        pathDelimiter: String = ":",
        home: String = "/home/me",
        isWsl: Boolean = false,
        isWindows: Boolean = false,
    ): HostFacts = HostFacts(pathEnv, pathDelimiter, home, isWsl, isWindows) { it in executables }

    // ------------------------------------------------------------------ resolveCaluCommand

    @Test
    fun `prefers the setting`() {
        val r = resolveCaluCommand(listOf("uv", "run", "calu"), facts(listOf("/usr/bin/calu")))
        assertEquals(ResolveResult.Found(listOf("uv", "run", "calu"), "setting"), r)
    }

    @Test
    fun `ignores an empty setting and searches PATH in order`() {
        assertEquals(
            ResolveResult.Found(listOf("/usr/bin/calu"), "path"),
            resolveCaluCommand(listOf(""), facts(listOf("/usr/bin/calu"))),
        )
        assertEquals(
            ResolveResult.Found(listOf("/mnt/c/Users/me/bin/calu"), "path"),
            resolveCaluCommand(emptyList(), facts(listOf("/mnt/c/Users/me/bin/calu", "/usr/bin/calu"))),
        )
    }

    @Test
    fun `skips mnt PATH entries on WSL`() {
        val r = resolveCaluCommand(emptyList(), facts(listOf("/mnt/c/Users/me/bin/calu", "/usr/bin/calu"), isWsl = true))
        assertEquals(ResolveResult.Found(listOf("/usr/bin/calu"), "path"), r)
    }

    @Test
    fun `falls back to the local bin directory of the home directory`() {
        val r = resolveCaluCommand(emptyList(), facts(listOf("/home/me/.local/bin/calu"), isWsl = true))
        assertEquals(ResolveResult.Found(listOf("/home/me/.local/bin/calu"), "local-bin"), r)
    }

    @Test
    fun `says what to do when nothing is found`() {
        val r = resolveCaluCommand(emptyList(), facts(emptyList(), isWsl = true))
        assertTrue(r is ResolveResult.NotFound)
        val message = (r as ResolveResult.NotFound).message
        assertTrue(message.contains("uv tool install"))
        assertTrue(message.contains("\"calu command\" (Settings | Tools | calu)"))
    }

    @Test
    fun `finds calu exe on Windows`() {
        val windows = facts(
            listOf("C:\\tools\\calu.exe"),
            pathEnv = "C:\\tools;C:\\other",
            pathDelimiter = ";",
            isWindows = true,
            home = "",
        )
        assertEquals(ResolveResult.Found(listOf("C:\\tools\\calu.exe"), "path"), resolveCaluCommand(emptyList(), windows))
    }

    // ------------------------------------------------------------------ findExecutable

    @Test
    fun `looks in PATH, then in the given home directories`() {
        assertEquals(FoundExecutable("/usr/bin/tmux", "path"), findExecutable("tmux", facts(listOf("/usr/bin/tmux"))))
        assertEquals(
            FoundExecutable("/home/me/.cargo/bin/uv", "local-bin"),
            findExecutable(
                "uv",
                facts(listOf("/home/me/.cargo/bin/uv")),
                listOf(listOf(".local", "bin"), listOf(".cargo", "bin")),
            ),
        )
        assertNull(findExecutable("tmux", facts(listOf("/home/me/.local/bin/tmux")), emptyList()))
        assertNull(findExecutable("screen", facts(emptyList())))
    }

    // ------------------------------------------------------------------ buildIdeArgv

    @Test
    fun `appends ide`() {
        assertEquals(listOf("uv", "run", "calu", "ide"), buildIdeArgv(listOf("uv", "run", "calu")))
    }

    @Test
    fun `rejects an empty command`() {
        assertThrows(IllegalArgumentException::class.java) { buildIdeArgv(emptyList()) }
    }

    // ------------------------------------------------------------------ small helpers

    @Test
    fun `detects WSL kernels`() {
        assertTrue(looksLikeWsl("6.6.87.2-microsoft-standard-WSL2"))
        assertFalse(looksLikeWsl("6.8.0-45-generic"))
    }

    @Test
    fun `joins paths in the style of the directory`() {
        assertEquals("/usr/bin/calu", joinPath("/usr/bin/", "calu"))
        assertEquals("C:\\tools\\calu.exe", joinPath("C:\\tools", "calu.exe"))
    }
}
