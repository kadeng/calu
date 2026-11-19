package io.github.kadeng.calu.backend

import com.intellij.openapi.components.BaseState
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.SimplePersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import io.github.kadeng.calu.remote.PluginSettingsDto

/**
 * Keeps the IDE from rewriting the settings' texts as paths of this machine.
 *
 * The IDE replaces every stored text that starts with the project directory or the home
 * directory by a macro (`$PROJECT_DIR$/../../dev/project` for `/home/dev/project`) and expands
 * it at the next start without normalising it. The remote project directory, the calu command
 * and the option texts name things on the server (or are command lines), so they are stored
 * as typed.
 */
class CaluPathMacroFilter : com.intellij.openapi.application.PathMacroFilter() {
    override fun skipPathMacros(attribute: org.jdom.Attribute): Boolean {
        val option = attribute.parent ?: return false
        return attribute.name == "value" && option.name == "option" && option.parentElement?.getAttributeValue("name") == "CaluSettings"
    }
}

/** The stored form of the editor-side settings (see `PluginSettingsDto` for each field). */
class CaluSettingsState : BaseState() {
    var mode by string("auto")
    var caluCommand by string("")
    var sshHost by string("")
    var sshDirectory by string("")
    var sshOptions by string("")
    var syncMode by string("calu")
    var notifications by property(true)
    var serverSudo by property(false)
    var serverInsecure by property(false)
    var serverExtraArgs by string("")
}

/**
 * The editor-side settings of one project, stored in `.idea/calu.xml`. They live in the backend
 * because the backend starts the bridge; the settings page of the frontend reads and writes them
 * through `CaluRemoteApi`. calu's own settings are in calu's database, not here.
 */
@Service(Service.Level.PROJECT)
@State(name = "CaluSettings", storages = [Storage("calu.xml")])
class CaluSettings : SimplePersistentStateComponent<CaluSettingsState>(CaluSettingsState()) {
    /**
     * Return the settings as the value type that crosses the RPC boundary.
     *
     * @return A copy of the stored settings; missing texts are "".
     */
    fun toDto(): PluginSettingsDto = PluginSettingsDto(
        mode = state.mode ?: "auto",
        caluCommand = state.caluCommand.orEmpty(),
        sshHost = state.sshHost.orEmpty(),
        sshDirectory = state.sshDirectory.orEmpty(),
        sshOptions = state.sshOptions.orEmpty(),
        syncMode = state.syncMode ?: "calu",
        notifications = state.notifications,
        serverSudo = state.serverSudo,
        serverInsecure = state.serverInsecure,
        serverExtraArgs = state.serverExtraArgs.orEmpty(),
    )

    /**
     * Replace the stored settings. Side effect: the IDE writes `.idea/calu.xml` at its next save.
     *
     * @param dto The new settings; an unknown `mode` or `syncMode` is stored as "auto" / "calu".
     */
    fun update(dto: PluginSettingsDto) {
        state.mode = dto.mode.takeIf { it in setOf("auto", "local", "wsl", "ssh") } ?: "auto"
        state.caluCommand = dto.caluCommand.trim()
        state.sshHost = dto.sshHost.trim()
        state.sshDirectory = dto.sshDirectory.trim()
        state.sshOptions = dto.sshOptions.trim()
        state.syncMode = dto.syncMode.takeIf { it in setOf("calu", "none") } ?: "calu"
        state.notifications = dto.notifications
        state.serverSudo = dto.serverSudo
        state.serverInsecure = dto.serverInsecure
        state.serverExtraArgs = dto.serverExtraArgs.trim()
    }

    companion object {
        /**
         * Return the settings service of a project.
         *
         * @param project The project.
         * @return The service.
         */
        fun of(project: Project): CaluSettings = project.service()
    }
}
