package io.github.kadeng.calu.logic

import io.github.kadeng.calu.remote.PluginSettingsDto
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Tests of what a person approves before the plugin runs a project's calu settings (D279, S4). */
class ApprovalTest {
    private val ssh = PluginSettingsDto(mode = "ssh", sshHost = "box", sshDirectory = "/srv/p", sshOptions = "-p 2222")

    @Test
    fun `every field that names a program or an argument changes the digest`() {
        val base = approvalDigest(launchContent(ssh))
        val changed = listOf(
            ssh.copy(mode = "local"),
            ssh.copy(caluCommand = "/opt/calu/bin/calu"),
            ssh.copy(sshHost = "other"),
            ssh.copy(sshDirectory = "/srv/q"),
            ssh.copy(sshOptions = "-p 2223"),
            ssh.copy(syncMode = "none"),
            ssh.copy(serverSudo = true),
            ssh.copy(serverInsecure = true),
            ssh.copy(serverExtraArgs = "--port 9"),
        )
        for (settings in changed) assertNotEquals(base, approvalDigest(launchContent(settings)), settings.toString())
    }

    @Test
    fun `what runs nothing does not ask again`() {
        val local = PluginSettingsDto()
        assertEquals(launchContent(local), launchContent(local.copy(notifications = false)))
        // Outside the SSH mode nothing reads the SSH fields.
        assertEquals(launchContent(local), launchContent(local.copy(sshOptions = "-o X=y", sshHost = "h")))
    }

    @Test
    fun `the digest is sha-256 in hex`() {
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", approvalDigest(""))
    }

    @Test
    fun `the dialog names the command and only the settings that are not the defaults`() {
        val text = approvalText("p", "ssh -T box 'calu ide'", ssh.copy(serverExtraArgs = "--x"))
        assertTrue(text.contains("  ssh -T box 'calu ide'"))
        assertTrue(text.contains("SSH options: -p 2222"))
        assertTrue(text.contains("Extra arguments of the server command: --x"))
        assertFalse(text.contains("--sudo"))
        val plain = approvalText("p", "calu ide", PluginSettingsDto())
        assertTrue(plain.contains("are the defaults"))
        assertTrue(plain.contains("asks again when they change"))
    }
}
