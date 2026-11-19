package io.github.kadeng.calu.frontend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.application.asContextElement
import com.intellij.openapi.options.Configurable
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.ComboBox
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import io.github.kadeng.calu.remote.PluginSettingsDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.swing.JComponent

/** The choices of "How calu is reached": stored value and label. */
val MODE_CHOICES: List<Pair<String, String>> = listOf(
    "auto" to "Automatic",
    "local" to "On this machine",
    "wsl" to "Inside WSL",
    "ssh" to "On a server, over SSH",
)

/** The choices of "Sync": stored value and label. */
val SYNC_CHOICES: List<Pair<String, String>> = listOf(
    "calu" to "Through calu",
    "none" to "None (I sync myself)",
)

/**
 * The settings page "calu" (Settings | Tools): the editor-side settings only. calu's own
 * settings are in calu's database; the tool window's title actions open them.
 *
 * The values are stored by the backend: `reset` loads them in the background and fills the
 * fields when they arrive; `apply` sends them (the backend restarts the bridge if needed).
 *
 * @param project The project.
 */
class CaluConfigurable(private val project: Project) : Configurable {
    private val mode = ComboBox(MODE_CHOICES.map { it.second }.toTypedArray())
    private val command = JBTextField().apply { emptyText.text = "empty: calu on PATH, then ~/.local/bin/calu" }
    private val sshHost = JBTextField().apply { emptyText.text = "host alias of ~/.ssh/config, or user@host" }
    private val sshDirectory = JBTextField().apply { emptyText.text = "/path/of/the/project/on/the/server" }
    private val sshOptions = JBTextField().apply { emptyText.text = "e.g. -p 2222" }
    private val sync = ComboBox(SYNC_CHOICES.map { it.second }.toTypedArray())
    private val notifications = JBCheckBox("Notify with Allow / Deny when a step waits and the calu tool window is hidden")
    private val sudo = JBCheckBox("Server command: add --sudo")
    private val insecure = JBCheckBox("Server command: add --insecure")
    private val extraArgs = JBTextField()
    private var loaded = PluginSettingsDto()

    override fun getDisplayName(): String = "calu"

    override fun createComponent(): JComponent = FormBuilder.createFormBuilder()
        .addLabeledComponent("How calu is reached:", mode)
        .addComponent(note("Automatic: inside WSL for a project below \\\\wsl.localhost on Windows, else on this machine. \"On this machine\" is also right for JetBrains remote development, where the plugin's backend runs on the host. \"On a server, over SSH\": this project is a local copy."))
        .addLabeledComponent("calu command:", command)
        .addSeparator()
        .addLabeledComponent("SSH host:", sshHost)
        .addLabeledComponent("Remote project directory:", sshDirectory)
        .addLabeledComponent("Extra ssh options:", sshOptions)
        .addLabeledComponent("Sync (SSH mode):", sync)
        .addComponent(note("SSH uses the system's OpenSSH client with BatchMode: it never asks for a passphrase, so the key must be in your SSH agent or have none. \"Through calu\": this project folder is kept equal to the remote directory (an empty folder is cloned from the server after a confirmation; nothing local is overwritten without one). \"None\": only the chat is remote and you keep the copy equal yourself (the IDE's Deployment, rsync, Mutagen); file links open the local file of the same relative path."))
        .addSeparator()
        .addComponent(notifications)
        .addComponent(sudo)
        .addComponent(insecure)
        .addLabeledComponent("Extra arguments of calu serve:", extraArgs)
        .addComponentFillVertically(javax.swing.JPanel(), 0)
        .panel

    /**
     * Build an explanation that wraps to the page's width.
     *
     * @param text The explanation.
     * @return The component.
     */
    private fun note(text: String): JComponent = textArea(text).apply { foreground = UiStyle.dim; columns = 40 }

    /**
     * Read the fields.
     *
     * @return The settings as the fields show them.
     */
    private fun current(): PluginSettingsDto = PluginSettingsDto(
        mode = MODE_CHOICES[maxOf(0, mode.selectedIndex)].first,
        caluCommand = command.text.trim(),
        sshHost = sshHost.text.trim(),
        sshDirectory = sshDirectory.text.trim(),
        sshOptions = sshOptions.text.trim(),
        syncMode = SYNC_CHOICES[maxOf(0, sync.selectedIndex)].first,
        notifications = notifications.isSelected,
        serverSudo = sudo.isSelected,
        serverInsecure = insecure.isSelected,
        serverExtraArgs = extraArgs.text.trim(),
    )

    /**
     * Fill the fields.
     *
     * @param s The settings to show.
     */
    private fun show(s: PluginSettingsDto) {
        mode.selectedIndex = maxOf(0, MODE_CHOICES.indexOfFirst { it.first == s.mode })
        command.text = s.caluCommand
        sshHost.text = s.sshHost
        sshDirectory.text = s.sshDirectory
        sshOptions.text = s.sshOptions
        sync.selectedIndex = maxOf(0, SYNC_CHOICES.indexOfFirst { it.first == s.syncMode })
        notifications.isSelected = s.notifications
        sudo.isSelected = s.serverSudo
        insecure.isSelected = s.serverInsecure
        extraArgs.text = s.serverExtraArgs
    }

    override fun isModified(): Boolean = current() != loaded

    override fun apply() {
        val next = current()
        loaded = next
        val service = CaluFrontendService.of(project)
        service.launch { service.storeSettings(next) }
    }

    override fun reset() {
        show(loaded)
        val service = CaluFrontendService.of(project)
        val modality = ModalityState.any().asContextElement()
        service.launch {
            val s = service.loadSettings()
            withContext(Dispatchers.EDT + modality) {
                val untouched = current() == loaded
                loaded = s
                if (untouched) show(s)
            }
        }
    }
}
