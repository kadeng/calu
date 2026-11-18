package io.github.kadeng.calu.bridge

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The pure parts of the launchers: command lines, path translation, diagnoses, waits. */
class LaunchersTest {
    @Test
    fun `the ssh command line never asks, keeps the connection checked, and quotes the remote command`() {
        val argv = sshArgv(SshSettings("build-box", "/srv/my project", listOf("-p", "2222")))
        assertEquals(
            listOf(
                "ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-p", "2222", "build-box", "calu ide --cwd '/srv/my project'",
            ),
            argv,
        )
    }

    @Test
    fun `the ssh command line uses the configured calu command of the server`() {
        val argv = sshArgv(SshSettings("me@host", "/p", calu = listOf("/home/me/.local/bin/calu")))
        assertEquals("/home/me/.local/bin/calu ide --cwd /p", argv.last())
    }

    @Test
    fun `a missing host or directory and a host that looks like an option are refused`() {
        assertThrows(IllegalArgumentException::class.java) { sshArgv(SshSettings("", "/p")) }
        assertThrows(IllegalArgumentException::class.java) { sshArgv(SshSettings("h", " ")) }
        assertThrows(IllegalArgumentException::class.java) { sshArgv(SshSettings("-oProxyCommand=x", "/p")) }
    }

    @Test
    fun `a sign-in that would need a passphrase is explained and not retried`() {
        val d = diagnoseSsh("box", 255, "me@box: Permission denied (publickey,password).\n")
        assertNotNull(d)
        assertTrue(d!!.message.contains("SSH agent"))
        assertFalse(d.retry)
    }

    @Test
    fun `network failures are retried, a missing calu and an unknown host key are not`() {
        assertTrue(diagnoseSsh("box", 255, "ssh: connect to host box port 22: Connection refused")!!.retry)
        assertTrue(diagnoseSsh("box", 255, "client_loop: send disconnect: Broken pipe")!!.retry)
        assertFalse(diagnoseSsh("box", 127, "bash: line 1: calu: command not found")!!.retry)
        assertFalse(diagnoseSsh("box", 255, "Host key verification failed.")!!.retry)
        assertNull(diagnoseSsh("box", 1, "Traceback ..."))
    }

    @Test
    fun `the wsl command line runs calu inside the distribution in the project directory`() {
        assertEquals(
            listOf("wsl.exe", "-d", "Ubuntu", "--cd", "/home/me/p", "--", "calu", "ide"),
            wslArgv("Ubuntu", "/home/me/p", emptyList()),
        )
        assertEquals(
            listOf("wsl.exe", "-d", "Ubuntu", "--cd", "/p", "--", "/usr/local/bin/calu", "ide"),
            wslArgv("Ubuntu", "/p", listOf("/usr/local/bin/calu")),
        )
    }

    @Test
    fun `windows paths into wsl are translated at the edge, both ways`() {
        assertEquals(WslLocation("Ubuntu", "/home/me/p"), parseWslPath("\\\\wsl.localhost\\Ubuntu\\home\\me\\p"))
        assertEquals(WslLocation("Ubuntu", "/home/me"), parseWslPath("//wsl$/Ubuntu/home/me/"))
        assertEquals(WslLocation("Debian", "/"), parseWslPath("//wsl.localhost/Debian"))
        assertNull(parseWslPath("C:\\Users\\me\\p"))
        assertNull(parseWslPath("/home/me/p"))
        assertEquals("\\\\wsl.localhost\\Ubuntu\\home\\me\\p", wslToWindowsPath("Ubuntu", "/home/me/p"))
    }

    @Test
    fun `the wait between reconnects doubles and is capped`() {
        assertEquals(listOf(500L, 1000L, 2000L, 4000L), (1..4).map(::reconnectDelayMs))
        assertEquals(30_000L, reconnectDelayMs(9))
        assertEquals(30_000L, reconnectDelayMs(500))
    }
}
