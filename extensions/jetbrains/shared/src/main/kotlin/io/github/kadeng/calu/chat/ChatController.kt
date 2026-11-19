/**
 * The chat of one project, without any user interface: it owns the view state, applies the
 * bridge's events to it with the reducer, and turns the user's actions into bridge requests.
 * It is the part of the VSCode extension's host (`extension.ts`, `sessions.ts`, `chatView.ts`)
 * that a second front end needs as well, so it lives in `shared` and runs in the backend.
 *
 *     BridgeSupervisor ──status──────────► bridge.status ─┐
 *                      ──session.event──► <event>        ├─► applyEvent ──► state ──► subscribers
 *                      ──ide.link───────► ui.link        │     (snapshot first, then each event)
 *     actions (attach, start, send, decide, ...) ──ui.*──┘
 *
 * Further actions (rewind and fork from a message, side questions, honesty, comments) and the
 * dispatch of an action by its name (`act`) are extension functions in `ChatActions.kt`.
 *
 *     bridge "running" ──► ide.status, ide.sessions, then
 *                            attach again to the session shown before (events after lastSeq), else
 *                            attach to the session of the project directory if one runs
 *
 * Every action catches the failures of its requests and reports them as a notice in the chat;
 * no action throws for a refused or failed request. No IntelliJ Platform dependency.
 */
package io.github.kadeng.calu.chat

import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.bridge.LaunchException
import io.github.kadeng.calu.bridge.errorCode
import io.github.kadeng.calu.bridge.errorText
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.HintInput
import io.github.kadeng.calu.logic.OutgoingImage
import io.github.kadeng.calu.logic.ServerCommandInput
import io.github.kadeng.calu.logic.buildHints
import io.github.kadeng.calu.logic.buildServerHelp
import io.github.kadeng.calu.logic.chatSendParams
import io.github.kadeng.calu.logic.checkSend
import io.github.kadeng.calu.logic.findingOk
import io.github.kadeng.calu.protocol.BridgeError
import io.github.kadeng.calu.rpc.RpcEvent
import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.SetupHint
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.flagsForLevel
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.messageTarget
import io.github.kadeng.calu.state.own
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject

/** Milliseconds after which a waiting `ide.start` is shown as slow (no server may be running). */
const val SLOW_START_MS: Long = 3000

/** Steps asked from `session.history` after an attach. */
const val HISTORY_STEPS: Int = 300

/** Milliseconds between two requests for the running sessions while no session is attached. */
const val SESSION_LIST_REFRESH_MS: Long = 15_000

/**
 * The editor-side settings that the server command needs.
 *
 * @property calu The calu command prefix; empty stands for `calu`.
 * @property sudo Add `--sudo` to `calu serve`.
 * @property insecure Add `--insecure` to `calu serve`.
 * @property extraArgs Further arguments of `calu serve`.
 */
data class ServerSettings(
    val calu: List<String> = emptyList(),
    val sudo: Boolean = false,
    val insecure: Boolean = false,
    val extraArgs: List<String> = emptyList(),
)

/** One message to a subscriber of the chat. */
sealed interface ChatUpdate {
    /**
     * The whole state; always the first message of a subscription.
     *
     * @property state The state at the moment of subscribing.
     */
    data class Snapshot(val state: ViewState) : ChatUpdate

    /**
     * One event that was applied to the state after the snapshot; applying it with `applyEvent`
     * to the subscriber's copy gives the controller's state.
     *
     * @property event The event.
     */
    data class Event(val event: ViewEvent) : ChatUpdate
}

/**
 * The chat of one project.
 *
 * @param supervisor The bridge of the project. The controller sets its `onNotification` and
 *   `onLockedChange` callbacks.
 * @param scope Scope of the controller's background work.
 * @param serverSettings Reads the editor-side settings when the server command is built.
 * @param log Receives messages for the plugin's log (never a password).
 */
class ChatController(
    internal val supervisor: BridgeSupervisor,
    private val scope: CoroutineScope,
    private val serverSettings: () -> ServerSettings = { ServerSettings() },
    private val log: (String) -> Unit = {},
) {
    private val lock = Any()
    private var current: ViewState = initialViewState()
    private val subscribers = ArrayList<Channel<ChatUpdate>>()
    private var lastStatus: JsonObject? = null
    private var noServer = false
    private var lastHints: List<SetupHint>? = null
    private var started = false

    /** Source of the ids of side questions (`ui.sideAsk`); see `sideAsk` in `ChatActions.kt`. */
    internal val sideIds = java.util.concurrent.atomic.AtomicInteger(1)

    /** The diffs of the attached session's edits (for "open diff"). */
    val diffs: DiffStore = DiffStore()

    /** The view state now. */
    val state: ViewState get() = synchronized(lock) { current }

    /**
     * Subscribe to the chat.
     *
     * @return A cold flow: collecting it yields the state at that moment as a `Snapshot`, then
     *   every event applied afterwards, in order and without a gap. The subscription ends when
     *   the collector is cancelled.
     */
    fun updates(): Flow<ChatUpdate> = flow {
        val channel = Channel<ChatUpdate>(Channel.UNLIMITED)
        synchronized(lock) {
            channel.trySend(ChatUpdate.Snapshot(current))
            subscribers.add(channel)
        }
        try {
            for (update in channel) emit(update)
        } finally {
            synchronized(lock) { subscribers.remove(channel) }
        }
    }

    /**
     * Apply an event to the state and send it to the subscribers.
     *
     * @param method The event's method (a session event or one of the `ui.*` events).
     * @param params Its params.
     */
    fun dispatch(method: String, params: JsonObject = EMPTY_OBJECT) {
        val event = ViewEvent(method, params)
        synchronized(lock) {
            current = applyEvent(current, event)
            for (s in subscribers) s.trySend(ChatUpdate.Event(event))
        }
    }

    /**
     * Connect the controller to the supervisor and start the bridge in the background. Later
     * calls do nothing. A bridge that cannot be started is shown in the state, not thrown.
     */
    fun start() {
        synchronized(lock) { if (started) return; started = true }
        supervisor.onNotification = ::onNotification
        supervisor.onLockedChange = { updateHints() }
        scope.launch {
            supervisor.status.collect { info ->
                dispatch("bridge.status", buildJsonObject {
                    put("status", JsonPrimitive(info.status))
                    info.message?.let { put("message", JsonPrimitive(it)) }
                    info.version?.let { put("version", JsonPrimitive(it)) }
                })
                updateHints()
                if (info.status == BridgeStatus.RUNNING) scope.launch { connectWorkspace() }
            }
        }
        scope.launch { ensureBridge() }
        // While no session is attached the list of running sessions is what the chat shows:
        // keep it current (a server that stopped takes its sessions with it).
        scope.launch {
            while (true) {
                delay(SESSION_LIST_REFRESH_MS)
                if (supervisor.status.value.status == BridgeStatus.RUNNING && !isAttached(state)) refreshSessions(false)
            }
        }
    }

    /**
     * Start the bridge if it does not run.
     *
     * @return True if it runs afterwards. A failure is in the state (`bridge.status`).
     */
    suspend fun ensureBridge(): Boolean = try {
        supervisor.ensureStarted()
        true
    } catch (_: LaunchException) {
        false
    }

    /** Stop the bridge and start it again ("Restart bridge"). Failures are in the state. */
    suspend fun restartBridge() {
        try {
            supervisor.restart()
        } catch (_: LaunchException) {
        }
    }

    /**
     * Ask the one password now ("Unlock"), then load what needed it.
     *
     * @return True if the bridge holds a login afterwards.
     */
    suspend fun unlock(): Boolean {
        val opened = try {
            supervisor.unlock()
        } catch (_: LaunchException) {
            false
        }
        if (opened) connectWorkspace()
        updateHints()
        return opened
    }

    /**
     * Handle one notification of the bridge (called on the supervisor's reader thread).
     *
     * @param event The notification.
     */
    private fun onNotification(event: RpcEvent) {
        when (event.method) {
            "session.event" -> {
                val method = event.params.str("method") ?: return
                val params = event.params.objOrEmptyLocal("params")
                if (method == "edit.diff") diffs.record(params)
                dispatch(method, params)
                if (method == "turn.result") scope.launch { refreshUsage() }
                if (method == "session.state") scope.launch { refreshSessions(false) }
            }
            "ide.link" -> {
                dispatch("ui.link", event.params)
                if (event.params.str("state") == "attached" && event.params["complete"] == JsonPrimitive(false)) {
                    scope.launch { attach(state.session.sessionId, interactive = false) }
                }
                updateHints()
                // The session may have ended, or its server: ask for the list now and once more
                // after the server had time to record the end.
                scope.launch {
                    refreshSessions(false)
                    delay(2000)
                    refreshSessions(false)
                }
            }
            "ide.log" -> log("[calu] bridge: ${event.params.str("message").orEmpty()}")
        }
    }

    /**
     * After the bridge started: check the setup, load the session list, and attach again to the
     * session shown before (asking only for the events the state lacks), else to the session of
     * the project directory if one runs. No password prompt. Never throws.
     */
    suspend fun connectWorkspace() {
        refreshStatus()
        refreshSessions(false)
        val before = state
        val wanted = before.session.sessionId
        if (isAttached(before)) return
        if (wanted != null && before.lastSeq > 0 && attach(wanted, interactive = false, afterSeq = before.lastSeq, quiet = true)) return
        attach(null, interactive = false, quiet = true)
        updateHints()
    }

    /** Ask `ide.status`, take over `locked` and recompute the hints. Failures are logged. */
    suspend fun refreshStatus() {
        try {
            val status = supervisor.request("ide.status", interactive = false).asObj() ?: return
            synchronized(lock) { lastStatus = status }
            supervisor.noteStatusLocked(status.bool("locked"))
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            log("[calu] the setup was not checked: ${errorText(error)}")
        }
        updateHints()
    }

    /**
     * Ask `ide.sessions` and put the running sessions into the state (`ui.sessions`).
     *
     * @param interactive True if the user asked (a needed password is then asked).
     * @return The result of `ide.sessions`, or `null` if it failed (the error is in the state).
     */
    suspend fun refreshSessions(interactive: Boolean): JsonObject? = try {
        val result = supervisor.request("ide.sessions", EMPTY_OBJECT, interactive).asObj() ?: EMPTY_OBJECT
        dispatch("ui.sessions", buildJsonObject { put("running", result["running"] ?: JsonNull) })
        result
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        dispatch("ui.sessions", buildJsonObject { put("error", JsonPrimitive(errorText(error))) })
        null
    }

    /**
     * Attach to a running session (`ide.attach`) and rebuild the chat from the result.
     *
     * @param session The session's id or a prefix; `null` for the session of the project directory.
     * @param interactive True if the user asked (a needed password is then asked).
     * @param afterSeq Ask only for the events after this sequence number; the transcript is
     *   then continued if the answer has no gap, else rebuilt from a second, full request.
     * @param quiet True to leave a failure out of the chat (the automatic attach at start).
     * @return True if a session is attached afterwards.
     */
    suspend fun attach(session: String?, interactive: Boolean = true, afterSeq: Int = 0, quiet: Boolean = false): Boolean {
        try {
            val params = buildJsonObject {
                put("session", session?.let { JsonPrimitive(it) } ?: JsonNull)
                if (afterSeq > 0) put("after_seq", JsonPrimitive(afterSeq))
            }
            val result = supervisor.request("ide.attach", params, interactive).asObj() ?: EMPTY_OBJECT
            if (afterSeq > 0 && !result.bool("complete")) return attach(session, interactive, 0, quiet)
            onAttached(result, resumed = afterSeq > 0)
            return true
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (!quiet && session == null && errorCode(error) == BridgeError.AMBIGUOUS_SESSION) {
                // Several sessions run in the project directory: the surface lets the user choose.
                refreshSessions(false)
                dispatch("ui.pickSession")
                return false
            }
            if (!quiet) notice("error", "Attaching failed: ${errorText(error)}")
            else if (errorCode(error) != BridgeError.NO_SESSION) log("[calu] no session was attached: ${errorText(error)}")
            return false
        }
    }

    /**
     * Take over the result of `ide.attach` / `ide.start`: remember the diffs of the recorded
     * edits, rebuild (or continue) the chat, and load the usage and the recorded earlier turns.
     *
     * @param result The result.
     * @param resumed True if `result.events` are only those after the ones the chat has.
     */
    private fun onAttached(result: JsonObject, resumed: Boolean) {
        if (!resumed) diffs.clear()
        for (e in result.arr("events")) {
            val event = e.asObj() ?: continue
            if (event.str("method") == "edit.diff") diffs.record(event.obj("params") ?: continue)
        }
        synchronized(lock) { noServer = false }
        dispatch("ui.attached", JsonObject(result + ("resumed" to JsonPrimitive(resumed))))
        updateHints()
        val sessionId = result.str("session_id") ?: return
        scope.launch { refreshUsage() }
        scope.launch { refreshSessions(false) }
        if (!resumed) scope.launch { loadHistory(sessionId) }
    }

    /**
     * Put the recorded earlier turns of the conversation above the transcript (`ui.history`).
     * Failures are logged only.
     *
     * @param sessionId The session the attach was for; the answer is dropped if another one is
     *   attached when it arrives.
     */
    private suspend fun loadHistory(sessionId: String) {
        try {
            val history = supervisor.session("session.history", buildJsonObject { put("limit", JsonPrimitive(HISTORY_STEPS)) }, false)
            if (state.sessions.attachedId != sessionId) return
            dispatch("ui.history", buildJsonObject { put("session_id", JsonPrimitive(sessionId)); put("history", history) })
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            log("[calu] the recorded conversation was not loaded: ${errorText(error)}")
        }
    }

    /** Ask the attached session for its usage (`usage.get`) and put it into the state. */
    suspend fun refreshUsage() {
        if (!isAttached(state)) return
        try {
            dispatch("ui.usage", supervisor.session("usage.get", EMPTY_OBJECT, false).asObj() ?: return)
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            log("[calu] the usage of the session was not read: ${errorText(error)}")
        }
    }

    /**
     * Ask the calu server for a session (`ide.start`) and attach to it. While the request
     * waits the state has `starting`; after `SLOW_START_MS` it is marked slow. If no server
     * answered (error -32013), the state gets the server command (`serverHelp`) and the
     * "no-server" hint.
     *
     * @param kind "new", "resume" or "fork".
     * @param claudeSessionId The conversation to resume or fork; `null` for "new".
     * @param forkBeforeTurn Cut the conversation before this turn; `null` for all of it.
     * @param prompt First message of the session; `null` for none.
     * @param name Name of the session; `null` or "" for none.
     * @return True if the session runs and is attached.
     */
    suspend fun startSession(
        kind: String,
        claudeSessionId: String? = null,
        forkBeforeTurn: Int? = null,
        prompt: String? = null,
        name: String? = null,
    ): Boolean = startSessionAs(
        when (kind) {
            "resume" -> if (forkBeforeTurn != null) "Rewinding the conversation" else "Resuming the conversation"
            "fork" -> "Forking the conversation"
            else -> "Starting a session"
        },
        kind, claudeSessionId, forkBeforeTurn, prompt, name,
    )

    /**
     * `startSession` with the words that say what is being done (a resume before a turn is a
     * rewind or a fork, depending on what the user chose).
     *
     * @param title What is being done, e.g. "Forking the conversation": shown while the request
     *   waits (`ViewState.starting.title`) and in a failure message.
     * @param kind "new", "resume" or "fork".
     * @param claudeSessionId The conversation to resume or fork; `null` for "new".
     * @param forkBeforeTurn Cut the conversation before this turn; `null` for all of it.
     * @param prompt First message of the session; `null` for none.
     * @param name Name of the session; `null` or "" for none.
     * @return True if the session runs and is attached.
     */
    suspend fun startSessionAs(
        title: String,
        kind: String,
        claudeSessionId: String? = null,
        forkBeforeTurn: Int? = null,
        prompt: String? = null,
        name: String? = null,
    ): Boolean {
        dispatch("ui.starting", buildJsonObject { put("title", JsonPrimitive(title)); put("slow", JsonPrimitive(false)) })
        val slow = scope.launch {
            delay(SLOW_START_MS)
            dispatch("ui.starting", buildJsonObject { put("title", JsonPrimitive(title)); put("slow", JsonPrimitive(true)) })
        }
        try {
            val params = buildJsonObject {
                put("kind", JsonPrimitive(kind))
                claudeSessionId?.let { put("claude_session_id", JsonPrimitive(it)) }
                forkBeforeTurn?.let { put("fork_before_turn", JsonPrimitive(it)) }
                prompt?.takeIf { it.isNotBlank() }?.let { put("prompt", JsonPrimitive(it)) }
                name?.trim()?.takeIf { it.isNotEmpty() }?.let { put("name", JsonPrimitive(it)) }
            }
            val result = supervisor.request("ide.start", params).asObj() ?: EMPTY_OBJECT
            slow.cancel()
            onAttached(result, resumed = false)
            return true
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            slow.cancel()
            dispatch("ui.starting")
            when (errorCode(error)) {
                BridgeError.NO_SERVER -> {
                    synchronized(lock) { noServer = true }
                    notice("warning", "No calu server answered: ${errorText(error)}")
                }
                BridgeError.START_CANCELLED -> notice("info", "The session request was taken back.")
                else -> notice("error", "$title failed: ${errorText(error)}")
            }
            updateHints()
            return false
        }
    }

    /** Take a waiting session request back (`ide.start.cancel`). */
    suspend fun cancelStart() = run("Cancelling the session request") { supervisor.request("ide.start.cancel") }

    /** Detach from the session (`ide.detach`); the transcript stays readable. */
    suspend fun detach() {
        run("Detaching") { supervisor.request("ide.detach") }
        dispatch("ui.detached")
        updateHints()
    }

    /**
     * Send a chat message without images to the agent of a tab (see `sendMessage`).
     *
     * @param text The message; blank text is not sent.
     * @param inject True to deliver it into the running turn ("inject"), false for the next turn.
     * @param tab The visible tab: "main" or a subagent's id (see `messageTarget`).
     * @return True if the session accepted the message.
     */
    suspend fun send(text: String, inject: Boolean, tab: String): Boolean = sendMessage(text, inject, tab, emptyList())

    /**
     * Send a chat message, with or without images, to the agent of a tab (`chat.send`).
     *
     *     the tab's agent cannot receive messages     -> warning notice, false
     *     `checkSend` refuses the text or an image    -> warning notice, false (nothing is sent)
     *     the session refuses or cannot be reached    -> error notice, the event
     *                                                    `ui.restoreInput {text}`, false
     *
     * @param text The message; may be blank only if there is an image.
     * @param inject True to deliver it into the running turn ("inject"), false for the next turn.
     * @param tab The visible tab: "main" or a subagent's id (see `messageTarget`).
     * @param images The attached images (`outgoingImage`); only the main agent takes images.
     * @return True if the session accepted the message.
     */
    suspend fun sendMessage(text: String, inject: Boolean, tab: String, images: List<OutgoingImage>): Boolean {
        if (text.isBlank() && images.isEmpty()) return false
        val target = messageTarget(state, tab)
        val to = target.to
        if (to == null) {
            notice("warning", target.reason ?: "This tab cannot receive messages.")
            return false
        }
        checkSend(text, to, images)?.let { problem ->
            notice("warning", problem)
            return false
        }
        val sent = run("Sending the message") {
            supervisor.session("chat.send", chatSendParams(text, if (inject) "inject" else "queued", to, images))
        }
        if (!sent) dispatch("ui.restoreInput", buildJsonObject { put("text", JsonPrimitive(text)) })
        return sent
    }

    /**
     * Decide an escalation, or answer the agent's questions.
     *
     * @param escalationKey The escalation's key in the state.
     * @param allow True to allow the step, false to deny it (ignored with `answer`).
     * @param note A note for Claude and the record; `null` or blank for none.
     * @param answer The answer to the agent's questions; `null` for an allow/deny decision.
     * @return True if the session accepted the decision. While the request runs the card is
     *   marked pending; a failure clears the mark and is reported in the chat.
     */
    suspend fun decide(escalationKey: String, allow: Boolean, note: String? = null, answer: String? = null): Boolean {
        val esc = own(state.escalations, escalationKey)
        if (esc == null || esc.status != EscalationStatus.OPEN) return false
        dispatch("ui.decisionPending", buildJsonObject {
            put("escalation_key", JsonPrimitive(escalationKey))
            answer?.let { put("answer", JsonPrimitive(it)) }
        })
        val ok = run("Sending the decision") {
            supervisor.session("escalation.decide", buildJsonObject {
                put("escalation_id", JsonPrimitive(esc.escalationId))
                put("decision", JsonPrimitive(if (allow) "allow" else "deny"))
                note?.takeIf { it.isNotBlank() }?.let { put("note", JsonPrimitive(it)) }
                answer?.let { put("answer", JsonPrimitive(it)) }
            })
        }
        if (!ok) dispatch("ui.decisionFailed", buildJsonObject { put("escalation_key", JsonPrimitive(escalationKey)) })
        return ok
    }

    /** Interrupt Claude's running turn (`interrupt`). */
    suspend fun interrupt() = run("Interrupting") { supervisor.session("interrupt") }

    /**
     * Change the model of the running session (`model.set`).
     *
     * @param model A Claude Code alias or model id; `null` or "default" for Claude Code's default.
     */
    suspend fun setModel(model: String?) {
        val value = model?.takeIf { it.isNotEmpty() && it != "default" }
        if (run("Changing the model") { supervisor.session("model.set", buildJsonObject { put("model", value?.let { JsonPrimitive(it) } ?: JsonNull) }) }) {
            dispatch("ui.model", buildJsonObject { put("model", value?.let { JsonPrimitive(it) } ?: JsonNull) })
        }
    }

    /**
     * Store the effort for sessions started from now on (`claude.effort` in calu's global
     * settings); the effort of a running session is fixed.
     *
     * @param effort The effort name; "default" removes the stored value.
     */
    suspend fun setEffort(effort: String) {
        val ok = run("Changing the effort") {
            if (effort == "default") supervisor.request("ide.settings.unset", buildJsonObject { put("key", JsonPrimitive("claude.effort")); put("project", JsonNull) })
            else supervisor.request("ide.settings.set", buildJsonObject { put("key", JsonPrimitive("claude.effort")); put("value", JsonPrimitive(effort)); put("project", JsonNull) })
        }
        if (ok) notice("info", "Effort \"$effort\" is stored; it applies to sessions started from now on.")
    }

    /**
     * Change the supervision level of the session (`auto_escalate.set`).
     *
     * @param level One of the level ids of `Levels.kt`; an unknown id is reported in the chat.
     */
    suspend fun setLevel(level: String) = run("Changing the supervision level") {
        val f = flagsForLevel(level)
        supervisor.session("auto_escalate.set", buildJsonObject {
            put("commands", JsonPrimitive(f.commands)); put("edits", JsonPrimitive(f.edits)); put("subagents", JsonPrimitive(f.subagents))
        })
    }

    /**
     * Give a session a name (`ide.session.rename`) and reload the session list.
     *
     * @param sessionId The session.
     * @param name The new name; "" removes it.
     */
    suspend fun rename(sessionId: String, name: String) {
        if (run("Renaming the session") {
                supervisor.request("ide.session.rename", buildJsonObject { put("session_id", JsonPrimitive(sessionId)); put("name", JsonPrimitive(name.trim())) })
            }
        ) refreshSessions(false)
    }

    /**
     * Send any bridge request for a front-end feature that keeps no state here (settings form,
     * model presets, checker instructions, contexts).
     *
     * @param method The method: an `ide.*` method, or with `session` true a session method.
     * @param params Its params.
     * @param session True to send it to the attached session through `session.call`.
     * @param interactive True if the user asked (a needed password is then asked).
     * @return The result.
     * @throws io.github.kadeng.calu.rpc.RpcRequestException for an error answer.
     * @throws io.github.kadeng.calu.rpc.RpcClosedException if the bridge exits before answering.
     * @throws IllegalStateException if the bridge is not running.
     */
    suspend fun call(method: String, params: JsonObject, session: Boolean, interactive: Boolean = true): JsonElement =
        if (session) supervisor.session(method, params, interactive) else supervisor.request(method, params, interactive)

    /**
     * Show a message in the main transcript.
     *
     * @param level "info", "warning" or "error".
     * @param text The message.
     * @param key What the message is about, for a later [noticeResolved]; `null` for none.
     */
    fun notice(level: String, text: String, key: String? = null) =
        dispatch(
            "ui.notice",
            buildJsonObject {
                put("level", JsonPrimitive(level))
                put("text", JsonPrimitive(text))
                if (key != null) put("key", JsonPrimitive(key))
            },
        )

    /**
     * Mark the messages of the main transcript that carry a key as resolved: they stay and lose
     * their warning colour.
     *
     * @param key The key given to [notice].
     */
    fun noticeResolved(key: String) = dispatch("ui.noticeResolved", buildJsonObject { put("key", JsonPrimitive(key)) })

    /**
     * Build the command that starts `calu serve`, from the editor-side settings and from what
     * `ide.status` found installed (tmux, screen).
     *
     * @return The command with its explanation.
     */
    fun serverHelp(): io.github.kadeng.calu.state.ServerHelp {
        val s = serverSettings()
        val status = synchronized(lock) { lastStatus }
        return buildServerHelp(ServerCommandInput(s.calu, s.sudo, s.insecure, s.extraArgs, findingOk(status, "tmux"), findingOk(status, "screen")))
    }

    /** Recompute the setup hints and the server command, and put them into the state if they changed. */
    fun updateHints() {
        val s = state
        val attached = s.sessions.attachedId != null
        val running = s.bridge.status == BridgeStatus.RUNNING
        val (status, none) = synchronized(lock) { lastStatus to noServer }
        val hints = buildHints(HintInput(s.bridge.status == BridgeStatus.MISSING, status, attached, none, supervisor.locked && !attached))
            .filter { running || it.id == "calu-missing" }
        val changed = synchronized(lock) { (hints != lastHints).also { lastHints = hints } }
        if (changed) {
            dispatch("ui.hints", buildJsonObject {
                put("hints", buildJsonArray {
                    for (h in hints) add(io.github.kadeng.calu.json.CaluJson.encodeToJsonElement(SetupHint.serializer(), h))
                })
            })
        }
        val help = if (!attached && running) serverHelp() else null
        if (help != state.serverHelp) {
            dispatch("ui.serverHelp", if (help == null) EMPTY_OBJECT else buildJsonObject {
                put("command", JsonPrimitive(help.command)); put("tool", JsonPrimitive(help.tool))
                put("detach", JsonPrimitive(help.detach)); put("note", JsonPrimitive(help.note))
            })
        }
    }

    /**
     * Run the requests of one action and report a failure as a notice.
     *
     * @param what The action, for the message ("Sending the message").
     * @param body The requests.
     * @return True if `body` returned, false if it threw (the failure is then in the chat).
     */
    internal suspend fun run(what: String, body: suspend () -> Unit): Boolean = try {
        body()
        true
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        notice("error", "$what failed: ${errorText(error)}")
        false
    }
}

/**
 * Read an object member, with an empty object for anything else (local alias that keeps the
 * import list of this file short).
 *
 * @param key The member's name.
 * @return The member if it is a JSON object, else an empty object.
 */
private fun JsonObject.objOrEmptyLocal(key: String): JsonObject = this[key].asObj() ?: EMPTY_OBJECT
