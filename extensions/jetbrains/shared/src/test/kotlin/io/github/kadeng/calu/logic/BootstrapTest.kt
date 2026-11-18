package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.TimeUnit

/** Commands that install uv and calu, the install flow, and the report about claude (port of `bootstrap.test.ts`). */
class BootstrapTest {
    private fun facts(executables: List<String>, isWsl: Boolean = false) =
        HostFacts("/mnt/c/tools:/usr/bin", ":", "/home/me", isWsl, false) { it in executables }

    @Test
    fun `uv is found in PATH, the local bin and the cargo bin directory, without Windows entries on WSL`() {
        assertEquals("/usr/bin/uv", findUv(facts(listOf("/usr/bin/uv"))))
        assertEquals("/home/me/.local/bin/uv", findUv(facts(listOf("/mnt/c/tools/uv", "/home/me/.local/bin/uv"), isWsl = true)))
        assertEquals("/home/me/.cargo/bin/uv", findUv(facts(listOf("/home/me/.cargo/bin/uv"))))
        assertNull(findUv(facts(emptyList())))
    }

    @Test
    fun `uv is installed with the astral script, by curl or else wget, without editing shell files`() {
        val command = uvInstallCommand()
        assertTrue(command.contains("curl -LsSf https://astral.sh/uv/install.sh"))
        assertTrue(command.contains("wget -qO- https://astral.sh/uv/install.sh"))
        assertTrue(command.contains("UV_NO_MODIFY_PATH=1 sh"))
    }

    @Test
    fun `calu is installed as a uv tool with Python 3_14 from the configured source`() {
        assertEquals(
            listOf("/u/uv", "tool", "install", "--force", "--python", "3.14", "git+https://github.com/kadeng/calu"),
            caluInstallArgv("/u/uv", DEFAULT_INSTALL_SOURCE),
        )
        assertEquals(listOf("/u/uv", "tool", "upgrade", "calu"), caluUpgradeArgv("/u/uv"))
    }

    @Test
    fun `checks the install source`() {
        assertEquals(DEFAULT_INSTALL_SOURCE, installSource(null))
        assertEquals(DEFAULT_INSTALL_SOURCE, installSource("  "))
        assertEquals("/home/me/calu", installSource(" /home/me/calu "))
        val error = assertThrows(IllegalArgumentException::class.java) { installSource("--index-url=http://x") }
        assertTrue(error.message!!.contains("must not start"))
    }

    @Test
    fun `claude is reported with its version, or with the address of its instructions`() {
        assertEquals(ClaudeCheck.Found("/usr/bin/claude", "2.1.80 (Claude Code)"), claudeCheck("/usr/bin/claude", "2.1.80 (Claude Code)\n"))
        assertEquals(ClaudeCheck.Found("/usr/bin/claude", "unknown version"), claudeCheck("/usr/bin/claude", ""))
        val missing = claudeCheck(null, "") as ClaudeCheck.Missing
        assertTrue(missing.message.contains(CLAUDE_DOCS_URL))
        assertEquals(listOf("/usr/bin/claude", "--version"), claudeVersionArgv("/usr/bin/claude"))
    }

    @Test
    fun `nothing is installed before the user said yes, and a decline ends the run`() {
        val ask = nextInstallAction(InstallProgress(uv = "/u/uv")) as InstallAction.Ask
        assertEquals("calu", ask.question.what)
        assertEquals("Install calu on this machine?", ask.question.question)
        assertEquals("Install", ask.question.button)
        assertTrue(ask.question.detail.contains("uv tool install --python 3.14 git+https://github.com/kadeng/calu"))
        assertEquals("Install calu on the SSH host box?", (nextInstallAction(InstallProgress(where = "on the SSH host box")) as InstallAction.Ask).question.question)
        assertEquals(InstallAction.Done(false, ""), nextInstallAction(InstallProgress(uv = "/u/uv", caluAnswer = false)))
    }

    @Test
    fun `with uv, calu is installed from the source, and the exit code decides how the run ends`() {
        val confirmed = InstallProgress(source = " /src/calu ", uv = "/u/uv", caluAnswer = true)
        assertEquals(
            InstallAction.Run(InstallStep("calu", "Installing calu", listOf("/u/uv", "tool", "install", "--force", "--python", "3.14", "/src/calu"))),
            nextInstallAction(confirmed),
        )
        assertEquals(InstallAction.Done(true, INSTALLED_MESSAGE), nextInstallAction(confirmed.copy(caluExit = 0)))
        assertEquals(InstallAction.Failed("Installing calu failed (exit code 2)."), nextInstallAction(confirmed.copy(caluExit = 2)))
    }

    @Test
    fun `without uv, uv is asked for and installed first`() {
        val noUv = InstallProgress(caluAnswer = true)
        val ask = nextInstallAction(noUv) as InstallAction.Ask
        assertEquals("uv", ask.question.what)
        assertEquals("Install uv", ask.question.button)
        assertTrue(ask.question.detail.contains(UV_INSTALL_URL))
        assertEquals(InstallAction.Done(false, ""), nextInstallAction(noUv.copy(uvAnswer = false)))
        assertEquals(
            InstallAction.Run(InstallStep("uv", "Installing uv", listOf("sh", "-c", uvInstallCommand()))),
            nextInstallAction(noUv.copy(uvAnswer = true)),
        )
        assertEquals(InstallAction.Failed("Installing uv failed (exit code -1)."), nextInstallAction(noUv.copy(uvAnswer = true, uvExit = -1)))
        assertEquals(
            InstallAction.Failed("uv was installed, but cannot be found in ~/.local/bin."),
            nextInstallAction(noUv.copy(uvAnswer = true, uvExit = 0)),
        )
        val next = nextInstallAction(noUv.copy(uvAnswer = true, uvExit = 0, uv = "/home/me/.local/bin/uv")) as InstallAction.Run
        assertEquals("calu", next.step.what)
        assertEquals("/home/me/.local/bin/uv", next.step.argv[0])
    }

    @Test
    fun `a source that uv would read as an option stops the run before any question`() {
        val failed = nextInstallAction(InstallProgress(source = "--index-url=http://x", uv = "/u/uv")) as InstallAction.Failed
        assertTrue(failed.message.contains("must not start"))
    }

    @Test
    fun `an update runs uv tool upgrade, and needs uv`() {
        assertEquals(InstallAction.Run(InstallStep("calu", "Updating calu", listOf("/u/uv", "tool", "upgrade", "calu"))), nextUpdateAction("/u/uv"))
        assertEquals(InstallAction.Done(true, UPDATED_MESSAGE), nextUpdateAction("/u/uv", 0))
        assertEquals(InstallAction.Failed("Updating calu failed (exit code 1)."), nextUpdateAction("/u/uv", 1))
        assertTrue((nextUpdateAction(null) as InstallAction.Failed).message.startsWith("uv was not found"))
    }

    @Test
    fun `the script for a remote machine installs uv only if it is missing, quotes the source, and is valid shell`() {
        val script = remoteInstallScript("/my dir/calu")
        assertTrue(script.startsWith("set -e; "))
        assertTrue(script.contains("if ! command -v uv >/dev/null 2>&1; then (" ))
        assertTrue(script.endsWith("uv tool install --force --python 3.14 '/my dir/calu'"))
        val check = ProcessBuilder("sh", "-n", "-c", script).redirectErrorStream(true).start()
        assertTrue(check.waitFor(10, TimeUnit.SECONDS))
        assertEquals(0, check.exitValue(), check.inputStream.bufferedReader().readText())
    }
}
