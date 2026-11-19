/**
 * The frontend's copy of the chat of one project.
 *
 *     backend ──CaluRemoteApi.updates()──► snapshot, then events ──applyEvent──► state (StateFlow)
 *     UI ──act(name, args)──► CaluRemoteApi.act          (from a coroutine, never from the EDT)
 *     backend ──passwordRequests()──► password dialog (EDT) ──answerPassword──► backend
 *     "The calu server stopped" shown ──every 10 s (tool window visible) / 30 s (hidden)──►
 *                                       act("lookForServer")
 *
 * The flows are collected inside `durable {}`: after a lost connection of the thin client the
 * collection starts again and the first message is a fresh snapshot.
 */
@file:Suppress("UnstableApiUsage")

package io.github.kadeng.calu.frontend

import com.intellij.notification.Notification
import com.intellij.notification.NotificationAction
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.EDT
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.wm.WindowManager
import com.intellij.platform.project.projectId
import fleet.rpc.client.durable
import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.logic.PROMPT_TITLE
import io.github.kadeng.calu.logic.SERVER_CHECK_VISIBLE_S
import io.github.kadeng.calu.logic.SERVER_STOPPED_HINT
import io.github.kadeng.calu.logic.serverCheckInterval
import io.github.kadeng.calu.logic.transcriptEnded
import io.github.kadeng.calu.remote.CallResultDto
import io.github.kadeng.calu.remote.CaluRemoteApi
import io.github.kadeng.calu.remote.PluginSettingsDto
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.initialViewState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Id of the plugin's tool window. */
const val TOOL_WINDOW_ID: String = "calu"

/** Id of the plugin's notification group (registered in `calu.frontend.xml`). */
const val NOTIFICATION_GROUP: String = "calu"

/** What a chat surface needs from its surroundings: the state and a way to act. */
interface ChatHost {
    /** The view state; a new value after every applied event. */
    val state: StateFlow<ViewState>

    /**
     * Run an action of `CaluRemoteApi.act` in the background.
     *
     * @param action The action's name.
     * @param args Its arguments.
     */
    fun act(action: String, args: JsonObject = EMPTY_OBJECT)

    /**
     * Send one bridge request (see `CaluRemoteApi.call`).
     *
     * @param method The method.
     * @param params Its params.
     * @param session True for a request of the session protocol.
     * @return The result or the error.
     */
    suspend fun call(method: String, params: JsonObject = EMPTY_OBJECT, session: Boolean = false): CallResultDto

    /**
     * Run a block in the host's coroutine scope (off the EDT).
     *
     * @param block The work.
     */
    fun launch(block: suspend CoroutineScope.() -> Unit)

    /**
     * Register a listener for the events that change no state but ask the surface for
     * something (`ui.restoreInput {text}`: put a text back into the composer).
     *
     * @param listener Called on the EDT with the event's method and params.
     */
    fun onUi(listener: (String, JsonObject) -> Unit)
}

/**
 * The frontend side of the plugin for one project.
 *
 * @param project The project.
 * @param scope The service's coroutine scope.
 */
@Service(Service.Level.PROJECT)
class CaluFrontendService(private val project: Project, private val scope: CoroutineScope) : ChatHost {
    private val log = Logger.getInstance(CaluFrontendService::class.java)
    private val stateFlow = MutableStateFlow(initialViewState())
    private val notified = HashSet<String>()
    private val shownNotifications = java.util.concurrent.ConcurrentHashMap<String, Notification>()
    private var started = false

    /** The notification "the calu server stopped" while it is shown; `null` otherwise. */
    @Volatile
    private var serverNotification: Notification? = null
    private val uiListeners = java.util.concurrent.CopyOnWriteArrayList<(String, JsonObject) -> Unit>()

    @Volatile
    private var settings = PluginSettingsDto()

    override val state: StateFlow<ViewState> get() = stateFlow

    /**
     * Start collecting the chat's updates and the password requests. Later calls do nothing.
     * Side effect in the backend: the bridge of the project is started.
     */
    @Synchronized
    fun start() {
        if (started) return
        started = true
        scope.launch {
            durable {
                CaluRemoteApi.getInstance().updates(project.projectId()).collect { update ->
                    val json = update.stateJson
                    val before = stateFlow.value
                    val next = if (json != null) {
                        CaluJson.decodeFromString(ViewState.serializer(), json)
                    } else {
                        applyEvent(before, ViewEvent(update.method.orEmpty(), update.paramsJson?.let(::parseObjectOrNull) ?: EMPTY_OBJECT))
                    }
                    stateFlow.value = next
                    onStateChanged(before, next)
                    val method = update.method
                    if (json == null && method != null && method.startsWith("ui.") && uiListeners.isNotEmpty()) {
                        val params = update.paramsJson?.let(::parseObjectOrNull) ?: EMPTY_OBJECT
                        withContext(Dispatchers.EDT) { for (l in uiListeners) l(method, params) }
                    }
                }
            }
        }
        scope.launch {
            durable {
                CaluRemoteApi.getInstance().passwordRequests(project.projectId()).collect { request ->
                    if (request == null) return@collect
                    val password = withContext(Dispatchers.EDT) { Messages.showPasswordDialog(project, request.text, PROMPT_TITLE, null) }
                    CaluRemoteApi.getInstance().answerPassword(project.projectId(), request.id, password)
                }
            }
        }
        scope.launch { settings = loadSettings() }
        scope.launch { watchStoppedServer() }
    }

    /**
     * While the chat says "The calu server stopped", ask the backend to look for the server
     * (`lookForServer`): every `SERVER_CHECK_VISIBLE_S` seconds while the tool window is
     * visible, every `SERVER_CHECK_HIDDEN_S` while it is hidden (`ide.status` starts a few
     * programs on the project's machine). Runs until the service's scope ends.
     */
    private suspend fun watchStoppedServer() {
        var waited = 0
        while (true) {
            delay(SERVER_CHECK_VISIBLE_S * 1000L)
            waited += SERVER_CHECK_VISIBLE_S
            if (stateFlow.value.hints.none { it.id == SERVER_STOPPED_HINT }) {
                waited = 0
                continue
            }
            val visible = withContext(Dispatchers.EDT) {
                !project.isDisposed && ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.isVisible == true
            }
            if (waited >= serverCheckInterval(visible)) {
                waited = 0
                act("lookForServer")
            }
        }
    }

    override fun act(action: String, args: JsonObject) {
        if (action == "openTerminal") {
            openServerTerminal()
            return
        }
        scope.launch {
            try {
                CaluRemoteApi.getInstance().act(project.projectId(), action, CaluJson.encodeToString(JsonObject.serializer(), args))
            } catch (error: kotlinx.coroutines.CancellationException) {
                throw error
            } catch (error: Exception) {
                log.warn("calu: the action $action failed", error)
            }
        }
    }

    /**
     * Open a terminal tab that runs the server command. If the terminal cannot be opened, the
     * command is copied to the clipboard and a notification says so.
     */
    private fun openServerTerminal() {
        scope.launch {
            val command = CaluRemoteApi.getInstance().serverTerminalCommand(project.projectId())
            if (command.isEmpty()) return@launch
            withContext(Dispatchers.EDT) {
                try {
                    TerminalOpener.open(project, command)
                } catch (error: kotlinx.coroutines.CancellationException) {
                    throw error
                } catch (error: Throwable) {
                    log.warn("calu: the terminal could not be opened", error)
                    com.intellij.openapi.ide.CopyPasteManager.getInstance().setContents(java.awt.datatransfer.StringSelection(command))
                    Notification(NOTIFICATION_GROUP, "calu: the terminal could not be opened", "The server command is in the clipboard: $command", NotificationType.INFORMATION).notify(project)
                }
            }
        }
    }

    override suspend fun call(method: String, params: JsonObject, session: Boolean): CallResultDto =
        CaluRemoteApi.getInstance().call(project.projectId(), method, CaluJson.encodeToString(JsonObject.serializer(), params), session)

    override fun launch(block: suspend CoroutineScope.() -> Unit) {
        scope.launch(block = block)
    }

    override fun onUi(listener: (String, JsonObject) -> Unit) {
        uiListeners.add(listener)
    }

    /**
     * Read the editor-side settings from the backend.
     *
     * @return The settings.
     */
    suspend fun loadSettings(): PluginSettingsDto = CaluRemoteApi.getInstance().getSettings(project.projectId()).also { settings = it }

    /**
     * Store the editor-side settings in the backend (which restarts the bridge if needed).
     *
     * @param dto The new settings.
     */
    suspend fun storeSettings(dto: PluginSettingsDto) {
        CaluRemoteApi.getInstance().setSettings(project.projectId(), dto)
        settings = dto
    }

    /**
     * React to a new state: refresh the status bar widget, and notify about escalations that
     * opened while the tool window is hidden.
     *
     * @param before The state before the update.
     * @param next The state after it.
     */
    private suspend fun onStateChanged(before: ViewState, next: ViewState) {
        if (statusLine(before) != statusLine(next)) {
            withContext(Dispatchers.EDT) { WindowManager.getInstance().getStatusBar(project)?.updateWidget(STATUS_WIDGET_ID) }
        }
        onServerStopped(before, next)
        // A notification whose escalation is decided (here, in another client, or by a cancel),
        // or whose session ended, goes away.
        val ended = transcriptEnded(next)
        val closed = shownNotifications.keys.filter { key -> ended || next.escalations[key]?.status != EscalationStatus.OPEN }
        if (closed.isNotEmpty()) {
            val gone = closed.mapNotNull { shownNotifications.remove(it) }
            withContext(Dispatchers.EDT) { for (n in gone) n.expire() }
        }
        if (!settings.notifications) return
        if (ended) return
        val fresh = next.escalations.values.filter { it.status == EscalationStatus.OPEN && it.key !in before.escalations && notified.add(it.key) }
        if (fresh.isEmpty()) return
        withContext(Dispatchers.EDT) {
            if (project.isDisposed || ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.isVisible == true) return@withContext
            for (e in fresh) {
                val question = e.questions.isNotEmpty()
                val n = Notification(
                    NOTIFICATION_GROUP,
                    if (question) "calu: Claude asks a question" else "calu: ${e.toolName} waits for your decision",
                    (e.questions.firstOrNull()?.question ?: e.reason).take(300),
                    NotificationType.WARNING,
                )
                if (!question) {
                    n.addAction(NotificationAction.createSimpleExpiring("Allow") { decide(e.key, true) })
                    n.addAction(NotificationAction.createSimpleExpiring("Deny") { decide(e.key, false) })
                }
                n.addAction(NotificationAction.createSimpleExpiring("Open") { ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.activate(null) })
                shownNotifications[e.key] = n
                n.notify(project)
            }
        }
    }

    /**
     * Show the notification "the calu server stopped" with "Start server" and "Retry" when the
     * chat begins to say so (the hint "server-stopped" appears), whether the tool window is
     * visible or not; expire it when the hint goes (a server was found, a session attached).
     *
     * @param before The state before the update.
     * @param next The state after it.
     */
    private suspend fun onServerStopped(before: ViewState, next: ViewState) {
        val was = before.hints.any { it.id == SERVER_STOPPED_HINT }
        val now = next.hints.any { it.id == SERVER_STOPPED_HINT }
        if (was == now) return
        withContext(Dispatchers.EDT) {
            serverNotification?.expire()
            serverNotification = null
            if (!now || project.isDisposed) return@withContext
            val n = Notification(NOTIFICATION_GROUP, "calu: the calu server stopped", "The session ended with it.", NotificationType.WARNING)
            n.addAction(NotificationAction.createSimpleExpiring("Start server") { openServerTerminal() })
            n.addAction(NotificationAction.createSimpleExpiring("Retry") { act("retryServer") })
            serverNotification = n
            n.notify(project)
        }
    }

    /**
     * Send a decision for an escalation.
     *
     * @param key The escalation's key in the state.
     * @param allow True to allow, false to deny.
     */
    fun decide(key: String, allow: Boolean) =
        act("decide", buildJsonObject { put("key", JsonPrimitive(key)); put("allow", JsonPrimitive(allow)) })

    companion object {
        /**
         * Return the service of a project.
         *
         * @param project The project.
         * @return The service.
         */
        fun of(project: Project): CaluFrontendService = project.service()
    }
}
