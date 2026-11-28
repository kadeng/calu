package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.InterpreterOfferView
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit

/** The offer of an SSH interpreter: endpoint, the guess of the Python, the card. */
class SshInterpreterTest {
    @Test
    fun `splits the host setting into host and user`() {
        assertEquals("sshbox" to null, sshDestination("sshbox"))
        assertEquals("sshbox" to "dev", sshDestination(" dev@sshbox "))
        assertEquals("10.0.0.5" to "a@b", sshDestination("a@b@10.0.0.5"))
    }

    @Test
    fun `takes user and port from ssh -G and the user of the setting first`() {
        val dump = "host sshbox\nuser dev\nhostname sshbox\nport 2222\nidentityfile ~/.ssh/calu_sshbox\n"
        assertEquals(SshEndpoint("sshbox", "dev", 2222), sshEndpoint("sshbox", dump, "cua"))
        assertEquals(SshEndpoint("sshbox", "root", 2222), sshEndpoint("root@sshbox", dump, "cua"))
        assertEquals(SshEndpoint("box", "cua", 22), sshEndpoint("box", null, "cua"))
        assertEquals(SshEndpoint("box", "cua", 22), sshEndpoint("box", "port 99999\n", "cua"))
    }

    @Test
    fun `reads the guess and skips other lines`() {
        assertEquals("/home/dev/project/.venv/bin/python", parsePythonGuess("Welcome\ncalu-python:/home/dev/project/.venv/bin/python\n"))
        assertNull(parsePythonGuess("calu-python:\n"))
        assertNull(parsePythonGuess("python3\n"))
    }

    @Test
    fun `guesses the venv of the project, else python3, by running the command in sh`() {
        val dir = Files.createTempDirectory("calu-guess").toFile()
        try {
            val spaced = File(dir, "my project").apply { mkdirs() }
            val noVenv = parsePythonGuess(sh(pythonGuessCommand(spaced.path)))
            // Without a venv: python3 (or python) of the PATH, if this machine has one.
            if (noVenv != null) assertTrue(noVenv.endsWith("python3") || noVenv.endsWith("python"), noVenv)
            val venv = File(spaced, ".venv/bin").apply { mkdirs() }
            File(venv, "python").apply { writeText("#!/bin/sh\n"); setExecutable(true) }
            assertEquals(File(venv, "python").path, parsePythonGuess(sh(pythonGuessCommand(spaced.path))))
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun `falls back to the newest python3 in ~ local bin when PATH has none`() {
        val home = Files.createTempDirectory("calu-home").toFile()
        try {
            val tools = File(home, "tools").apply { mkdirs() }
            for (name in listOf("ls", "sort", "tail")) {
                val real = listOf("/bin/$name", "/usr/bin/$name").map(::File).firstOrNull { it.canExecute() }
                assumeTrue(real != null, "skipped: needs $name in /bin or /usr/bin")
                Files.createSymbolicLink(File(tools, name).toPath(), real!!.toPath())
            }
            val bin = File(home, ".local/bin").apply { mkdirs() }
            for (v in listOf("python3.9", "python3.12")) File(bin, v).apply { writeText("#!/bin/sh\n"); setExecutable(true) }
            val p = ProcessBuilder("/bin/sh", "-c", pythonGuessCommand("/nonexistent/project"))
            p.environment().apply { clear(); put("PATH", tools.path); put("HOME", home.path) }
            val process = p.start()
            val out = process.inputStream.bufferedReader().readText()
            process.waitFor(10, TimeUnit.SECONDS)
            assertEquals(File(bin, "python3.12").path, parsePythonGuess(out))
        } finally {
            home.deleteRecursively()
        }
    }

    @Test
    fun `checks the typed path`() {
        assertNull(pythonPathProblem(" /usr/bin/python3 "))
        assertNotNull(pythonPathProblem(""))
        assertNotNull(pythonPathProblem("python3"))
        assertNotNull(pythonPathProblem("/usr/bin/"))
        assertNotNull(pythonPathProblem("/usr/bin/python3\nrm"))
    }

    @Test
    fun `reads the version answer`() {
        assertEquals("3.12.12", parsePythonVersion("calu-python:3.12.12\n"))
        assertNull(parsePythonVersion("bash: /x: No such file or directory\n"))
        val version = parsePythonVersion(sh(pythonVersionCommand("/usr/bin/env")).ifEmpty { "" })
        // `/usr/bin/env -c ...` is no Python: no version.
        assertNull(version)
    }

    @Test
    fun `the card offers, works and fails`() {
        val offer = applyEvents(initialViewState(), listOf(ev("ui.interpreter", "host" to "sshbox", "directory" to "/home/dev/project", "phase" to "offer")))
        assertEquals(InterpreterOfferView("sshbox", "/home/dev/project", "offer"), offer.interpreter)
        val card = interpreterCard(offer.interpreter)!!
        assertEquals("Set up running and debugging on sshbox", card.primary?.label)
        assertEquals("setupInterpreter", card.primary?.action)
        assertEquals("dismiss:$INTERPRETER_CARD", card.secondary.single().action)
        assertTrue(setupCards(offer.copy(bridge = offer.bridge.copy(status = "running"))).any { it.id == INTERPRETER_CARD })
        assertNull(interpreterCard(offer.interpreter, listOf(INTERPRETER_CARD)))
        val working = interpreterCard(offer.interpreter!!.copy(phase = "working"))!!
        assertTrue(working.busy)
        assertNull(working.primary)
        val failed = interpreterCard(offer.interpreter!!.copy(phase = "failed", message = "no Python"))!!
        assertTrue(failed.message.contains("no Python"))
        assertEquals("Try again", failed.primary?.label)
        val cleared = applyEvents(offer, listOf(ev("ui.interpreter")))
        assertNull(cleared.interpreter)
    }

    private fun sh(command: String): String {
        val p = ProcessBuilder("sh", "-c", command).redirectErrorStream(false).start()
        val out = p.inputStream.bufferedReader().readText()
        p.waitFor(10, TimeUnit.SECONDS)
        return out
    }
}
