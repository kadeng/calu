package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** Paths and command lines between the IDE's machine and calu's machine. */
class PathsTest {
    @Test
    fun `settings texts are split into arguments, quotes group`() {
        assertEquals(listOf("uv", "run", "--project", "/my dir", "calu"), splitArgs("uv run --project '/my dir' calu"))
        assertEquals(emptyList<String>(), splitArgs("   "))
        assertEquals(listOf("a", "", "b c"), splitArgs("a \"\" \"b c\""))
    }

    @Test
    fun `relative paths of calu are resolved against the session directory`() {
        assertEquals("/srv/p/src/a.py", absoluteRemotePath("src/a.py", "/srv/p/"))
        assertEquals("/etc/hosts", absoluteRemotePath("/etc/hosts", "/srv/p"))
        assertEquals("src/a.py", absoluteRemotePath("src/a.py", null))
    }

    @Test
    fun `in ssh mode a remote path maps into the local copy, and only below the remote root`() {
        assertEquals("/home/me/copy/src/a.py", sshLocalPath("/srv/p/src/a.py", "/srv/p", "/home/me/copy"))
        assertEquals("C:\\work\\copy\\src\\a.py", sshLocalPath("/srv/p/src/a.py", "/srv/p/", "C:\\work\\copy"))
        assertEquals("/home/me/copy", sshLocalPath("/srv/p", "/srv/p", "/home/me/copy"))
        assertNull(sshLocalPath("/srv/p2/a.py", "/srv/p", "/home/me/copy"))
        assertNull(sshLocalPath("/etc/hosts", "/srv/p", "/home/me/copy"))
        assertNull(sshLocalPath("/srv/p/../x", "/srv/p", "/home/me/copy"))
    }

    @Test
    fun `the terminal command runs the server where calu runs`() {
        val serve = "tmux new-session -A -s calu 'calu serve --sudo'"
        assertEquals(serve, terminalCommand(serve, "local"))
        assertEquals("ssh -t -p 2222 box 'tmux new-session -A -s calu '\\''calu serve --sudo'\\'''", terminalCommand(serve, "ssh", sshHost = "box", sshOptions = listOf("-p", "2222")))
        assertEquals("wsl.exe -d Ubuntu -- sh -lc 'tmux new-session -A -s calu '\\''calu serve --sudo'\\'''", terminalCommand(serve, "wsl", wslDistro = "Ubuntu"))
    }
}
