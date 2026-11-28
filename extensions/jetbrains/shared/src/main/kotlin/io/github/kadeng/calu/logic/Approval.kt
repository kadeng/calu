/**
 * The approval of a project's calu settings before the plugin runs anything they name
 * (agent/records/PLAN.md D279, S4).
 *
 * The settings of a project are stored in `.idea/calu.xml`, which a cloned repository can bring
 * along. They name programs and arguments: the calu command, the SSH host and options (`ssh`
 * options can run commands), the arguments of the server command. The plugin therefore starts
 * nothing for a project until a person approved exactly these settings:
 *
 *     project opens ──► launchContent(settings) ──► approved digest? ── yes ──► start the bridge
 *                                                       │ no
 *                                                       ▼
 *                       dialog with approvalText ── Start ──► store the digest, start
 *                                                 ── Don't start ──► nothing runs
 *
 * The stored digests belong to the IDE (application level), not to the project. Settings that a
 * person saves in the settings page count as approved. Pure: no IntelliJ Platform import.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.remote.PluginSettingsDto
import java.security.MessageDigest

/**
 * Return the part of the settings that decides what the plugin runs, as canonical text.
 *
 * Settings with the same text run the same programs with the same arguments. The
 * notifications switch is left out (it runs nothing); so are SSH host, directory, options and
 * sync mode outside the SSH mode, where nothing uses them.
 *
 * @param settings The editor-side settings of a project.
 * @return One line per field, e.g. "mode: auto\ncalu command: \nserver sudo: false\n...".
 */
fun launchContent(settings: PluginSettingsDto): String {
    val lines = mutableListOf(
        "mode: ${settings.mode}",
        "calu command: ${settings.caluCommand}",
    )
    if (settings.mode == "ssh") {
        lines += "ssh host: ${settings.sshHost}"
        lines += "ssh directory: ${settings.sshDirectory}"
        lines += "ssh options: ${settings.sshOptions}"
        lines += "sync: ${settings.syncMode}"
    }
    lines += "server sudo: ${settings.serverSudo}"
    lines += "server insecure: ${settings.serverInsecure}"
    lines += "server extra arguments: ${settings.serverExtraArgs}"
    return lines.joinToString("\n")
}

/**
 * Return the key under which an approval of `content` is stored.
 *
 * @param content The text of `launchContent`.
 * @return SHA-256 of its UTF-8 bytes, as 64 lowercase hex digits.
 */
fun approvalDigest(content: String): String =
    MessageDigest.getInstance("SHA-256").digest(content.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

/**
 * Build the text of the approval dialog.
 *
 * @param project The project's name.
 * @param command The command line the plugin starts (`BridgeLauncher.describe()`).
 * @param settings The settings to approve.
 * @return What the dialog says: the command, the settings from `.idea/calu.xml` that are not
 *   the defaults, and that a change asks again.
 */
fun approvalText(project: String, command: String, settings: PluginSettingsDto): String {
    val defaults = PluginSettingsDto()
    val set = buildList {
        if (settings.mode != defaults.mode) add("How calu is reached: ${settings.mode}")
        if (settings.caluCommand.isNotEmpty()) add("calu command: ${settings.caluCommand}")
        if (settings.mode == "ssh") {
            add("SSH host: ${settings.sshHost}")
            if (settings.sshDirectory.isNotEmpty()) add("Directory on the server: ${settings.sshDirectory}")
            if (settings.sshOptions.isNotEmpty()) add("SSH options: ${settings.sshOptions}")
            if (settings.syncMode != defaults.syncMode) add("File synchronisation: ${settings.syncMode}")
        }
        if (settings.serverSudo) add("Server command with --sudo")
        if (settings.serverInsecure) add("Server command with --insecure")
        if (settings.serverExtraArgs.isNotEmpty()) add("Extra arguments of the server command: ${settings.serverExtraArgs}")
    }
    val from = if (set.isEmpty()) {
        "The project's calu settings are the defaults."
    } else {
        "The project's calu settings (.idea/calu.xml, which a repository can bring along):\n" + set.joinToString("\n") { "  $it" }
    }
    return "calu starts this command for the project $project:\n\n  $command\n\n$from\n\n" +
        "Start calu only if you trust these settings. calu asks again when they change."
}
