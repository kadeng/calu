/**
 * The backend's state for one project: the bridge supervisor, the chat controller, the pending
 * password request, and the actions that open editors.
 *
 *     settings ──chooseLauncher──► BridgeSupervisor ──► ChatController ──updates()──► RPC ──► frontend
 *                                        ▲ password needed                               │
 *                                        └──── answerPassword ◄── passwordRequests ──────┘
 *
 * The password the user types travels frontend → backend → `ide.unlock` and is kept in a
 * `PasswordMemory` of the application (`CaluPasswords`), keyed by the calu that is reached, so
 * that one entry serves every project and every restart of a bridge until the IDE closes.
 *
 * Nothing runs before a person approved the project's settings (`.idea/calu.xml` can come with a
 * repository; D279, S4). Until then the wiring has a launcher that refuses, and an approval
 * request goes to the frontend like a password request:
 *
 *     build ── not approved ──► approvalRequests ──► dialog ──► answerApproval ── yes ──► rebuild
 *
 * The wiring keeps the settings that were approved (`Wiring.settings`); the server command and
 * the terminal command are built from them, not from a file that changed since.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.Disposable
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.guessProjectDir
import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.bridge.PasswordMemory
import io.github.kadeng.calu.bridge.PasswordPrompt
import io.github.kadeng.calu.chat.ChatController
import io.github.kadeng.calu.chat.ServerSettings
import io.github.kadeng.calu.logic.ResolveResult
import io.github.kadeng.calu.logic.approvalText
import io.github.kadeng.calu.logic.launchContent
import io.github.kadeng.calu.logic.promptText
import io.github.kadeng.calu.logic.resolveCaluCommand
import io.github.kadeng.calu.logic.splitArgs
import io.github.kadeng.calu.remote.ApprovalRequestDto
import io.github.kadeng.calu.remote.PasswordRequestDto
import io.github.kadeng.calu.remote.PluginSettingsDto
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import java.util.concurrent.ConcurrentHashMap

/** Version the plugin reports to the bridge in `ide.initialize`. */
const val CLIENT_VERSION: String = "0.1.0"

/** The passwords typed in this IDE process, by the calu they unlock. Memory only. */
@Service(Service.Level.APP)
class CaluPasswords {
    private val memories = ConcurrentHashMap<String, PasswordMemory>()

    /**
     * Return the memory for one calu installation.
     *
     * @param target The key of `LauncherChoice.target`.
     * @return The memory; created empty at the first call for the key.
     */
    fun memory(target: String): PasswordMemory = memories.computeIfAbsent(target) { PasswordMemory() }
}

/**
 * The backend side of the plugin for one project.
 *
 * @param project The project.
 * @param scope The service's coroutine scope (cancelled when the project closes).
 */
@Service(Service.Level.PROJECT)
class CaluProjectService(private val project: Project, private val scope: CoroutineScope) : Disposable {
    /**
     * The parts that are rebuilt when the settings change how calu is reached.
     *
     * @property settings The settings the wiring runs with: approved ones, kept up to date by
     *   the settings page (`applySettings`), never re-read from the file.
     * @property approved False while the settings wait for approval: the launcher refuses then.
     */
    class Wiring(
        val choice: LauncherChoice,
        val supervisor: BridgeSupervisor,
        val controller: ChatController,
        val sync: CaluSync? = null,
        val interpreter: CaluInterpreter? = null,
        @Volatile var settings: PluginSettingsDto = PluginSettingsDto(),
        val approved: Boolean = true,
    )

    private val log = Logger.getInstance(CaluProjectService::class.java)
    private val lock = Any()
    private var wiring: Wiring? = null
    private var nextPasswordId = 1
    private var pendingPassword: Pair<Int, CompletableDeferred<String?>>? = null
    private val passwordFlow = MutableStateFlow<PasswordRequestDto?>(null)
    private var nextApprovalId = 1
    private var pendingApproval: Pair<Int, String>? = null
    private val approvalFlow = MutableStateFlow<ApprovalRequestDto?>(null)

    /** The open approval request (`null` if none); the frontend shows a dialog for it. */
    val approvalRequests: StateFlow<ApprovalRequestDto?> get() = approvalFlow

    /** The open password request (`null` if none); the frontend shows a dialog for it. */
    val passwordRequests: StateFlow<PasswordRequestDto?> get() = passwordFlow

    /**
     * Return the wiring of the project; the first call creates it and starts the bridge in the
     * background.
     *
     * @return Launcher choice, supervisor and controller.
     */
    fun wiring(): Wiring = synchronized(lock) {
        wiring ?: build().also { wiring = it; it.sync?.start(); it.interpreter?.start(); it.controller.start() }
    }

    /**
     * Return the file synchronisation if the wiring exists and has one, without creating the
     * wiring (for callers such as the project view, which must not start a bridge).
     *
     * @return the synchronisation of the SSH mode, or null.
     */
    fun currentSync(): CaluSync? = synchronized(lock) { wiring?.sync }

    /**
     * Return the wiring if it exists, without creating it (for listeners that must not start a
     * bridge, such as the one for the recorded human diffs).
     *
     * @return the wiring, or null.
     */
    fun currentWiring(): Wiring? = synchronized(lock) { wiring }

    /** Pauses the sending of saves after the session said that human diffs are off (D253). */
    val humanDiffGate: io.github.kadeng.calu.logic.HumanDiffGate = io.github.kadeng.calu.logic.HumanDiffGate()

    /** The chat controller of the project (creates the wiring if needed). */
    val controller: ChatController get() = wiring().controller

    /**
     * Store new editor-side settings; if they change how calu is reached, stop the bridge and
     * start one with the new settings. The chat's transcript starts empty then.
     *
     * @param dto The new settings.
     */
    fun applySettings(dto: PluginSettingsDto) {
        val settings = CaluSettings.of(project)
        val before = settings.toDto()
        settings.update(dto)
        val after = settings.toDto()
        // The person saw and saved these settings: that approves them.
        CaluApprovals.get().approve(launchContent(after))
        val same = before.mode == after.mode && before.caluCommand == after.caluCommand && before.sshHost == after.sshHost &&
            before.sshDirectory == after.sshDirectory && before.sshOptions == after.sshOptions && before.syncMode == after.syncMode
        val current = synchronized(lock) { wiring }
        if (!same || current?.approved == false) {
            rebuild()
        } else if (current != null) {
            current.settings = after
            current.controller.updateHints()
        }
    }

    /** Stop the bridge of the current wiring (if any) and build and start a new one. */
    private fun rebuild() {
        val old = synchronized(lock) { wiring.also { wiring = null } }
        if (old != null) {
            old.sync?.dispose()
            old.interpreter?.dispose()
            old.supervisor.dispose()
        }
        wiring()
        restarted.value += 1
    }

    /**
     * Answer the open approval request.
     *
     * @param id The request's number; another number is ignored.
     * @param approved True to approve the settings it showed and start calu with them.
     */
    fun answerApproval(id: Int, approved: Boolean) {
        val content = synchronized(lock) { pendingApproval?.takeIf { it.first == id }?.also { pendingApproval = null } }?.second ?: return
        approvalFlow.value = null
        if (!approved) return
        CaluApprovals.get().approve(content)
        // Start only if the file still holds what was shown; else build() asks again.
        rebuild()
    }

    /** Counts the rebuilds of the wiring; subscribers of the chat start again when it changes. */
    val restarted: MutableStateFlow<Int> = MutableStateFlow(0)

    /**
     * Answer the open password request.
     *
     * @param id The request's number; another number is ignored.
     * @param password The entered password, or `null` for a cancel.
     */
    fun answerPassword(id: Int, password: String?) {
        val pending = synchronized(lock) { pendingPassword?.takeIf { it.first == id }?.also { pendingPassword = null } } ?: return
        passwordFlow.value = null
        pending.second.complete(password)
    }

    /**
     * Create supervisor and controller from the stored settings.
     *
     * @return The new wiring; nothing is started yet.
     */
    private fun build(): Wiring {
        val settings = CaluSettings.of(project).toDto()
        val found = chooseLauncher(settings, project.guessProjectDir()?.path ?: project.basePath)
        val content = launchContent(settings)
        val approved = CaluApprovals.get().isApproved(content)
        val choice = if (approved) {
            found
        } else {
            val id = synchronized(lock) { (nextApprovalId++).also { pendingApproval = it to content } }
            approvalFlow.value = ApprovalRequestDto(id, approvalText(project.name, found.launcher.describe(), settings))
            val why = "calu was not started: its settings for this project (.idea/calu.xml) wait for your approval. " +
                "Answer the dialog, or check them in Settings | Tools | calu and press OK."
            LauncherChoice(RefusingLauncher(why), found.mode, found.wslDistro, found.target)
        }
        val prompt = PasswordPrompt { problem, left ->
            val answer = CompletableDeferred<String?>()
            val id = synchronized(lock) { (nextPasswordId++).also { pendingPassword = it to answer } }
            passwordFlow.value = PasswordRequestDto(id, promptText(problem, left))
            try {
                answer.await()
            } finally {
                synchronized(lock) { if (pendingPassword?.first == id) pendingPassword = null }
                if (passwordFlow.value?.id == id) passwordFlow.value = null
            }
        }
        val supervisor = BridgeSupervisor(choice.launcher, scope, CLIENT_VERSION, prompt, service<CaluPasswords>().memory(choice.target), log = { log.info(it) })
        val controller = ChatController(supervisor, scope, {
            // The approved settings of the wiring; unapproved ones give the plain command.
            val s = synchronized(lock) { wiring }?.takeIf { it.approved }?.settings ?: PluginSettingsDto()
            ServerSettings(serverCalu(choice.mode, splitArgs(s.caluCommand)), s.serverSudo, s.serverInsecure, splitArgs(s.serverExtraArgs))
        }, log = { log.info(it) })
        // SSH mode with "Sync: through calu": the local copy is kept equal to the server's
        // directory through the same bridge. With "none" no ide.fs.* request is ever sent.
        val sync = if (approved && choice.mode == "ssh" && settings.syncMode == "calu" && project.basePath != null) {
            CaluSync(project, supervisor, controller, settings.sshHost.trim(), settings.sshDirectory.trim(), scope) { log.info(it) }
        } else {
            null
        }
        // SSH mode (with either sync mode): offer PyCharm's SSH interpreter for the server.
        val interpreter = if (approved && choice.mode == "ssh" && project.basePath != null) {
            CaluInterpreter(project, controller, settings, scope) { log.info(it) }
        } else {
            null
        }
        return Wiring(choice, supervisor, controller, sync, interpreter, settings, approved)
    }

    /**
     * Return the calu command the server command line starts with.
     *
     * A terminal's shell does not always have `~/.local/bin` on its PATH (a shell that is no
     * login shell, the shell tmux starts). If calu was found there and not on PATH, the server
     * command names the file, so that the command works where it is pasted.
     *
     * @param mode The effective mode ("local", "wsl" or "ssh").
     * @param configured The calu command of the settings, split into arguments; empty if unset.
     * @return The configured command; else the path of calu if this machine has it outside
     *   PATH; else empty, which stands for `calu`.
     */
    private fun serverCalu(mode: String, configured: List<String>): List<String> {
        if (configured.isNotEmpty() || mode != "local") return configured
        val found = resolveCaluCommand(configured, hostFacts())
        return if (found is ResolveResult.Found && found.source == "local-bin") found.command else configured
    }

    override fun dispose() {
        synchronized(lock) { wiring }?.sync?.dispose()
        synchronized(lock) { wiring }?.interpreter?.dispose()
        synchronized(lock) { wiring }?.supervisor?.dispose()
    }

    /**
     * Run a block in the service's scope (for actions that outlive the caller).
     *
     * @param block The work.
     */
    fun launch(block: suspend CoroutineScope.() -> Unit) {
        scope.launch(block = block)
    }

    companion object {
        /**
         * Return the service of a project.
         *
         * @param project The project.
         * @return The service.
         */
        fun of(project: Project): CaluProjectService = project.service()
    }
}
