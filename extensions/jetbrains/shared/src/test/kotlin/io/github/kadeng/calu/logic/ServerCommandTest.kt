package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests of the command that starts `calu serve` so that it survives the IDE, ported from
 * `extensions/vscode/test/unit/serverCommand.test.ts`. The quoting is checked with a real `sh`.
 */
class ServerCommandTest {
    /**
     * Split a command line into words with a real POSIX shell.
     *
     * Side effect: runs `sh -c` with a loop that prints each word of `command`, NUL-terminated.
     *
     * @param command The words of a shell command, quoted for a POSIX shell.
     * @return The words the shell passes to a program.
     */
    private fun shellWords(command: String): List<String> {
        val process = ProcessBuilder("sh", "-c", "for a in $command; do printf '%s\\0' \"\$a\"; done")
            .redirectErrorStream(true)
            .start()
        val out = process.inputStream.readBytes().toString(Charsets.UTF_8)
        assertEquals(0, process.waitFor(), out)
        return out.split('\u0000').dropLast(1)
    }

    /** Settings without flags and without tmux and screen. */
    private val base = ServerCommandInput(
        calu = listOf("/home/me/.local/bin/calu"),
        sudo = false,
        insecure = false,
        extraArgs = emptyList(),
        tmux = false,
        screen = false,
    )

    // ------------------------------------------------------------------ shell quoting

    @Test
    fun `leaves plain words alone and single-quotes the rest`() {
        assertEquals("calu", shellQuote("calu"))
        assertEquals("/a/b-c_d.e:f=g,h@i%j+k", shellQuote("/a/b-c_d.e:f=g,h@i%j+k"))
        assertEquals("''", shellQuote(""))
        assertEquals("'two words'", shellQuote("two words"))
        assertEquals("'it'\\''s'", shellQuote("it's"))
        assertEquals("'\$(rm -rf ~); `x` \"y\" \\ \n'", shellQuote("\$(rm -rf ~); `x` \"y\" \\ \n"))
        assertEquals("a 'b c'", shellJoin(listOf("a", "b c")))
        val hard = listOf("", "it's", "\$(echo x)", "`y`", "a\nb", "\"q\"", "\\", "*", "~", "-n")
        assertEquals(hard, shellWords(shellJoin(hard)))
    }

    @Test
    fun `quotes a word that ends with a newline`() {
        // Not in the TypeScript tests: Java's `$` would accept "calu\n" as a plain word.
        assertEquals("'calu\n'", shellQuote("calu\n"))
    }

    // ------------------------------------------------------------------ serveArgv

    @Test
    fun `adds the flags of the settings`() {
        assertEquals(
            listOf("/home/me/.local/bin/calu", "serve"),
            serveArgv(base.calu, base.sudo, base.insecure, base.extraArgs),
        )
        assertEquals(
            listOf("/home/me/.local/bin/calu", "serve", "--sudo", "--insecure", "--user", "claude bot"),
            serveArgv(base.calu, sudo = true, insecure = true, extraArgs = listOf("--user", "claude bot", "")),
        )
        assertEquals(listOf("calu", "serve"), serveArgv(emptyList(), base.sudo, base.insecure, base.extraArgs))
    }

    // ------------------------------------------------------------------ buildServerHelp

    @Test
    fun `uses tmux when it is installed`() {
        val help = buildServerHelp(base.copy(calu = listOf("calu"), sudo = true, tmux = true, screen = true))
        assertEquals("tmux new-session -A -s calu 'calu serve --sudo'", help.command)
        assertEquals("tmux", help.tool)
        assertEquals("Ctrl+B, then D", help.detach)
        assertTrue(help.note.contains("keeps running after the IDE closes"))
    }

    @Test
    fun `quotes twice for tmux - the serve command is one argument that a shell reads again`() {
        val calu = listOf("uv", "run", "--project", "/my projects/calu", "calu")
        val help = buildServerHelp(base.copy(calu = calu, extraArgs = listOf("it's", "\$HOME"), tmux = true))
        // A shell splits the command into tmux's arguments; the last one is the serve command.
        val outer = shellWords(help.command)
        assertEquals(listOf("tmux", "new-session", "-A", "-s", "calu"), outer.take(5))
        assertEquals(6, outer.size)
        // tmux hands that argument to a shell, which must see the original arguments.
        assertEquals(calu + listOf("serve", "it's", "\$HOME"), shellWords(outer[5]))
    }

    @Test
    fun `uses screen without tmux`() {
        val help = buildServerHelp(
            base.copy(calu = listOf("calu"), insecure = true, extraArgs = listOf("a b"), screen = true),
        )
        assertEquals("screen -S calu -D -R calu serve --insecure 'a b'", help.command)
        assertEquals("screen", help.tool)
        assertEquals("Ctrl+A, then D", help.detach)
    }

    @Test
    fun `falls back to the plain command and says that it ends with the terminal`() {
        val help = buildServerHelp(base.copy(calu = listOf("calu")))
        assertEquals("calu serve", help.command)
        assertEquals("plain", help.tool)
        assertEquals("", help.detach)
        assertTrue(help.note.contains("ends when the terminal closes"))
    }

    // ------------------------------------------------------------------ rootInstallCommand

    @Test
    fun `is the documented sudo command with the quoted source`() {
        val command = rootInstallCommand("git+https://github.com/kadeng/calu")
        assertTrue(command.startsWith("sudo env UV_TOOL_DIR=/usr/local/lib/calu UV_TOOL_BIN_DIR=/usr/local/bin "))
        assertTrue(command.endsWith("uv tool install git+https://github.com/kadeng/calu"))
        assertTrue(rootInstallCommand("/my checkout").contains("'/my checkout'"))
    }
}
