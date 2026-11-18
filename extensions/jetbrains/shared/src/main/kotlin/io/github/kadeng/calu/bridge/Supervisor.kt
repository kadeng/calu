/**
 * Supervision of the bridge process (`calu ide`), whatever launcher starts it.
 *
 *     status: stopped --ensureStarted()--> starting --ide.initialize ok--> running
 *                                             |                               |
 *                        calu not found ------+--> missing                    |
 *                        start/initialize error --> error        unexpected exit --> exited
 *     running --stop()--> stopped        exited/error/missing --ensureStarted()/restart()--> starting
 *
 *     launcher.reconnects (SSH): unexpected exit --> wait reconnectDelayMs(n) --> starting --> ...
 *                                (not after a diagnosis that says a new start cannot help)
 *
 *     request(method, params) ──► calu ide
 *         error -32010, interactive ──► one prompt (shared by concurrent requests)
 *                                         ├─ password ──► ide.unlock ── ok ──► same request again
 *                                         │                           └─ refused ──► prompt again
 *                                         └─ cancelled ──► the waiting requests fail, `locked` is true
 *         error -32010, background  ──► passed to the caller, never a prompt
 *     session(method, params) = request("session.call", {method, params})
 *
 *     start: ide.initialize ok ──► password in memory? ──► ide.unlock ── refused ──► forget it
 *
 * The one-password rule (spec sections 8 and 10): the password the user typed is kept in a
 * `PasswordMemory` (a variable, never a file, a setting or a log) until the IDE closes, and a
 * restarted bridge is unlocked with it without a prompt. An empty password is sent like any
 * other; nothing here refuses one. The decisions are in `logic/Unlock.kt`.
 *
 * Ported from `extensions/vscode/src/bridge.ts`; no IntelliJ Platform dependency.
 */
package io.github.kadeng.calu.bridge

import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.ExplicitAction
import io.github.kadeng.calu.logic.NeedAction
import io.github.kadeng.calu.logic.PROMPT_ADMIN
import io.github.kadeng.calu.logic.PROMPT_FIRST
import io.github.kadeng.calu.logic.PasswordNeed
import io.github.kadeng.calu.logic.RoundOutcome
import io.github.kadeng.calu.logic.UnlockResult
import io.github.kadeng.calu.logic.UnlockState
import io.github.kadeng.calu.logic.initialUnlockState
import io.github.kadeng.calu.logic.onDismiss
import io.github.kadeng.calu.logic.onExplicitUnlock
import io.github.kadeng.calu.logic.onNeedsPassword
import io.github.kadeng.calu.logic.onRoundAborted
import io.github.kadeng.calu.logic.onStatusLocked
import io.github.kadeng.calu.logic.onStoredResult
import io.github.kadeng.calu.logic.onUnlockRefused
import io.github.kadeng.calu.logic.onUnlockResult
import io.github.kadeng.calu.protocol.BridgeError
import io.github.kadeng.calu.protocol.CLIENT_NAME
import io.github.kadeng.calu.protocol.PROTOCOL_VERSION
import io.github.kadeng.calu.protocol.unlockedLogins
import io.github.kadeng.calu.rpc.RpcClient
import io.github.kadeng.calu.rpc.RpcClosedException
import io.github.kadeng.calu.rpc.RpcEvent
import io.github.kadeng.calu.rpc.RpcRequestException
import io.github.kadeng.calu.state.BridgeStatus
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlin.concurrent.thread

/** Milliseconds to wait for `ide.shutdown` and for the process to end before it is killed. */
const val STOP_TIMEOUT_MS: Long = 5000

/** Characters of the bridge's stderr kept for the diagnosis of an unexpected end. */
const val STDERR_TAIL_CHARS: Int = 4000

/**
 * The bridge's status as the chat view shows it.
 *
 * @property status One of the `BridgeStatus` names.
 * @property message Why (for "missing", "error", "exited", and while connecting again); else `null`.
 * @property version calu's version while "running"; else `null`.
 */
data class BridgeStatusInfo(val status: String, val message: String? = null, val version: String? = null)

/** Holds the one password in memory for as long as the IDE runs. Thread-safe. */
class PasswordMemory {
    /** The password the bridge accepted last; `null` if none was typed yet or it was refused. */
    @Volatile
    var password: String? = null
}

/** Asks the user for a password. The implementation shows a dialog; tests answer from a list. */
fun interface PasswordPrompt {
    /**
     * Ask for a password.
     *
     * @param kind `PROMPT_FIRST` for the one password, `PROMPT_ADMIN` for the admin password
     *   that changing settings needs.
     * @param problem Why the previous entry was not accepted; `null` for the first entry of a round.
     * @return The password (possibly empty), or `null` if the user cancelled.
     */
    suspend fun ask(kind: String, problem: String?): String?
}

/**
 * Owner of the bridge process of one project.
 *
 * @param launcher Starts the process.
 * @param scope Scope of the background work (reader threads' callbacks, reconnects); cancelling
 *   it does not stop the process: call `dispose`.
 * @param clientVersion Version string sent in `ide.initialize`.
 * @param prompt Asks for the password.
 * @param memory Where the accepted password is kept in memory; shared by the supervisors of one
 *   IDE process that reach the same calu.
 * @param log Receives the bridge's stderr and lifecycle messages (never a password).
 * @param delayMs The reconnect wait; replaced in tests.
 */
class BridgeSupervisor(
    private val launcher: BridgeLauncher,
    private val scope: CoroutineScope,
    private val clientVersion: String,
    private val prompt: PasswordPrompt,
    private val memory: PasswordMemory = PasswordMemory(),
    private val log: (String) -> Unit = {},
    private val delayMs: (Int) -> Long = ::reconnectDelayMs,
) {
    /** One running bridge process with its RPC connection. */
    private class Running(val process: BridgeProcess, val rpc: RpcClient) {
        @Volatile var stopping = false
        @Volatile var unlock: UnlockState = initialUnlockState()
        @Volatile var round: CompletableDeferred<Boolean>? = null
        val exited = CompletableDeferred<Unit>()
        val stderrTail = StringBuilder()
    }

    private val lock = Any()
    private val startMutex = Mutex()

    @Volatile private var running: Running? = null

    @Volatile private var reportedLocked = false

    @Volatile private var disposed = false
    private var reconnectJob: Job? = null
    private var reconnectAttempt = 0
    private val statusFlow = MutableStateFlow(BridgeStatusInfo(BridgeStatus.STOPPED))

    /** Called on the reader thread for every notification of the bridge, in arrival order. */
    @Volatile var onNotification: (RpcEvent) -> Unit = {}

    /** Called with the new value when `locked` changes. */
    @Volatile var onLockedChange: (Boolean) -> Unit = {}

    /** Result of the last successful `ide.initialize`; `null` while not running. */
    @Volatile var initializeResult: JsonObject? = null
        private set

    /** The process status; a new value for every change. */
    val status: StateFlow<BridgeStatusInfo> get() = statusFlow

    /** The launcher's mode name ("local", "wsl", "ssh", "scripted"). */
    val launcherKind: String get() = launcher.kind

    /**
     * True if a protected credential exists and the bridge holds no login for it: after a
     * cancelled prompt, a refused remembered password, error -32010 without a held login, or as
     * last reported through `noteStatusLocked`. False while no bridge process runs.
     */
    val locked: Boolean get() = running?.unlock?.locked ?: false

    /**
     * Start the bridge if it is not running, and wait until `ide.initialize` succeeded.
     * Concurrent calls share one start.
     *
     * @return The `ide.initialize` result.
     * @throws LaunchException if calu is not found (status "missing"), the process cannot be
     *   started or `ide.initialize` fails (status "error"). The message is also in `status`.
     */
    suspend fun ensureStarted(): JsonObject = startMutex.withLock {
        val r = running
        val init = initializeResult
        if (r != null && init != null && statusFlow.value.status == BridgeStatus.RUNNING) return init
        startProcess()
    }

    /**
     * Stop the bridge, then start it again.
     *
     * @return The new `ide.initialize` result.
     * @throws LaunchException like `ensureStarted`.
     */
    suspend fun restart(): JsonObject {
        stop()
        return ensureStarted()
    }

    /**
     * Send a request to the running bridge. If the bridge answers that a password is needed
     * (error -32010) and the request is interactive, the user is asked once (one prompt for all
     * concurrent requests), the password goes to the bridge with `ide.unlock`, and the request is
     * sent again without a password (rules: `onNeedsPassword`). `ide.unlock` itself is sent as it is.
     *
     * @param method A bridge request method.
     * @param params Its params (without `password`).
     * @param interactive False for requests the user did not ask for (start-up, timers): no
     *   prompt is shown and error -32010 is passed to the caller.
     * @return The response's `result`.
     * @throws IllegalStateException if the bridge is not running.
     * @throws RpcRequestException for an error answer (also -32010 if the user cancelled, ran
     *   out of attempts, or the request started before a cancel).
     * @throws RpcClosedException if the bridge exits before answering.
     */
    suspend fun request(method: String, params: JsonObject = EMPTY_OBJECT, interactive: Boolean = true): JsonElement {
        val startedAtDismissals = running?.unlock?.dismissals ?: 0
        var retries = 0
        while (true) {
            val r = requireRunning()
            val sentAtUnlocks = r.unlock.unlocks
            try {
                return r.rpc.request(method, params)
            } catch (error: RpcRequestException) {
                if (error.code != BridgeError.NEEDS_PASSWORD || method == "ide.unlock") throw error
                val need = PasswordNeed(interactive, unlockedLogins(error.data), startedAtDismissals, sentAtUnlocks, retries)
                if (!unlockFor(r, need)) throw error
            }
            retries++
        }
    }

    /**
     * Send a request of the session protocol to the attached session (`session.call`).
     *
     * @param method A session request method, e.g. "chat.send".
     * @param params Its params.
     * @param interactive See `request`.
     * @return The session's result.
     * @throws IllegalStateException if the bridge is not running.
     * @throws RpcRequestException like `request`; -32015 if no session is attached; the session's
     *   own errors with their code.
     * @throws RpcClosedException if the bridge exits before answering.
     */
    suspend fun session(method: String, params: JsonObject = EMPTY_OBJECT, interactive: Boolean = true): JsonElement =
        request("session.call", buildJsonObject { put("method", JsonPrimitive(method)); put("params", params) }, interactive)

    /**
     * Open the password prompt now (the chat's "Unlock" button), also after an earlier cancel.
     * Starts the bridge if it does not run; joins a prompt that is open; asks the admin password
     * if a login is held that may not change settings, and nothing if one that may is held.
     *
     * @return True if the bridge holds a login afterwards.
     * @throws LaunchException if the bridge cannot be started.
     */
    suspend fun unlock(): Boolean {
        ensureStarted()
        val r = requireRunning()
        val decision = synchronized(lock) { onExplicitUnlock(r.unlock).also { r.unlock = it.state } }
        reportLocked()
        val opened = when (decision.action) {
            ExplicitAction.NONE -> false
            ExplicitAction.JOIN -> r.round?.await() ?: false
            ExplicitAction.PROMPT_FIRST -> runRoundRegistered(r, PROMPT_FIRST)
            ExplicitAction.PROMPT_ADMIN -> runRoundRegistered(r, PROMPT_ADMIN)
        }
        return opened || r.unlock.unlocked.isNotEmpty()
    }

    /**
     * Take over the bridge's own report of `locked` (`ide.status.locked`). Calls
     * `onLockedChange` if the value changes. Does nothing while no bridge process runs.
     *
     * @param locked The reported value (false if the member is missing).
     */
    fun noteStatusLocked(locked: Boolean) {
        val r = running ?: return
        synchronized(lock) { r.unlock = onStatusLocked(r.unlock, locked) }
        reportLocked()
    }

    /**
     * Stop the bridge: send `ide.shutdown`, close stdin, wait for the end and kill the process
     * after `STOP_TIMEOUT_MS`. A pending reconnect is cancelled. Postcondition: status "stopped".
     * Sessions keep running: they belong to `calu serve`, not to the bridge. Never throws.
     */
    suspend fun stop() {
        synchronized(lock) { reconnectJob?.cancel(); reconnectJob = null; reconnectAttempt = 0 }
        val r = running
        if (r == null) {
            setStatus(BridgeStatusInfo(BridgeStatus.STOPPED))
            return
        }
        r.stopping = true
        try {
            withTimeoutOrNull(STOP_TIMEOUT_MS) { r.rpc.request("ide.shutdown") }
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            // The process may be gone already; the wait below decides.
        }
        withContext(Dispatchers.IO) {
            try {
                r.process.stdin.close()
            } catch (_: Exception) {
            }
        }
        if (withTimeoutOrNull(STOP_TIMEOUT_MS) { r.exited.await() } == null) {
            r.process.destroy(true)
            withTimeoutOrNull(STOP_TIMEOUT_MS) { r.exited.await() }
        }
        setStatus(BridgeStatusInfo(BridgeStatus.STOPPED))
    }

    /** Kill the process without the shutdown handshake (the project closes). No reconnect follows. */
    fun dispose() {
        disposed = true
        synchronized(lock) { reconnectJob?.cancel(); reconnectJob = null }
        val r = running ?: return
        r.stopping = true
        r.process.destroy(false)
        thread(isDaemon = true, name = "calu-bridge-kill") {
            if (r.process.waitFor(STOP_TIMEOUT_MS) == null) r.process.destroy(true)
        }
    }

    /**
     * Start the process and run `ide.initialize` (body of `ensureStarted`, under `startMutex`).
     *
     * @return The `ide.initialize` result.
     * @throws LaunchException see `ensureStarted`.
     */
    private suspend fun startProcess(): JsonObject {
        setStatus(BridgeStatusInfo(BridgeStatus.STARTING))
        log("[calu] starting the bridge (${launcher.kind}): ${launcher.describe()}")
        val process = try {
            withContext(Dispatchers.IO) { launcher.launch() }
        } catch (error: LaunchException) {
            fail(if (error.missing) BridgeStatus.MISSING else BridgeStatus.ERROR, error.message ?: "calu could not be started.", error.missing)
        }
        val r = connect(process)
        running = r
        val result: JsonObject
        try {
            result = r.rpc.request(
                "ide.initialize",
                buildJsonObject { put("client", JsonPrimitive(CLIENT_NAME)); put("client_version", JsonPrimitive(clientVersion)) },
            ).asObj() ?: EMPTY_OBJECT
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            r.stopping = true
            r.process.destroy(true)
            withTimeoutOrNull(STOP_TIMEOUT_MS) { r.exited.await() }
            val code = r.process.waitFor(1)
            val diagnosis = code?.let { launcher.diagnose(it, synchronized(r.stderrTail) { r.stderrTail.toString() }) }
            if (diagnosis != null && diagnosis.retry && launcher.reconnects) scheduleReconnect(diagnosis.message)
            fail(BridgeStatus.ERROR, diagnosis?.message ?: "calu ide did not initialize: ${errorText(error)}", false, keepStatus = diagnosis?.retry == true && launcher.reconnects)
        }
        val spoken = result.int("protocol_version")
        if (spoken != null && spoken != PROTOCOL_VERSION) {
            log("[calu] calu speaks protocol $spoken, the plugin $PROTOCOL_VERSION: update both to the same version.")
        }
        initializeResult = result
        unlockWithRemembered(r)
        if (running !== r) fail(BridgeStatus.ERROR, "calu ide exited while it started.", false)
        synchronized(lock) { reconnectAttempt = 0 }
        setStatus(BridgeStatusInfo(BridgeStatus.RUNNING, null, result.str("bridge_version")))
        return result
    }

    /**
     * Connect an RPC client to a started process: one thread reads stdout, one stderr, one waits
     * for the end. The end closes the client and sets the status ("exited", or a new start for a
     * launcher that reconnects).
     *
     * @param process The started process.
     * @return The record of the running bridge.
     */
    private fun connect(process: BridgeProcess): Running {
        val out = process.stdin
        val writeLock = Any()
        lateinit var r: Running
        val rpc = RpcClient(
            write = { line -> synchronized(writeLock) { out.write(line.toByteArray(Charsets.UTF_8)); out.flush() } },
            onEvent = { event -> if (running === r) onNotification(event) },
            onProtocolError = { message -> log("[calu] protocol: $message") },
        )
        r = Running(process, rpc)
        val reader = thread(isDaemon = true, name = "calu-bridge-stdout") {
            try {
                val input = process.stdout.reader(Charsets.UTF_8)
                val buffer = CharArray(1 shl 16)
                while (true) {
                    val n = input.read(buffer)
                    if (n < 0) break
                    try {
                        rpc.feed(String(buffer, 0, n))
                    } catch (error: Exception) {
                        log("[calu] a notification could not be handled: ${error.message}")
                    }
                }
            } catch (_: Exception) {
            }
        }
        val errors = thread(isDaemon = true, name = "calu-bridge-stderr") {
            try {
                process.stderr.bufferedReader(Charsets.UTF_8).forEachLine { line ->
                    log(line)
                    synchronized(r.stderrTail) {
                        r.stderrTail.append(line).append('\n')
                        if (r.stderrTail.length > STDERR_TAIL_CHARS) r.stderrTail.delete(0, r.stderrTail.length - STDERR_TAIL_CHARS)
                    }
                }
            } catch (_: Exception) {
            }
        }
        thread(isDaemon = true, name = "calu-bridge-wait") {
            val code = try {
                process.waitFor(0)
            } catch (_: InterruptedException) {
                null
            }
            reader.join(2000)
            errors.join(500)
            val description = "exited with code ${code ?: "?"}"
            rpc.close("calu ide $description")
            log("[calu] the bridge $description")
            if (running === r) {
                running = null
                initializeResult = null
                reportLocked()
                if (!r.stopping && !disposed) onUnexpectedExit(r, code ?: -1, description)
            }
            r.exited.complete(Unit)
        }
        return r
    }

    /**
     * Report an unexpected end of the bridge and, for a launcher that reconnects, plan a new start.
     *
     * @param r The ended process.
     * @param code Its exit code.
     * @param description "exited with code N".
     */
    private fun onUnexpectedExit(r: Running, code: Int, description: String) {
        val diagnosis = launcher.diagnose(code, synchronized(r.stderrTail) { r.stderrTail.toString() })
        val message = diagnosis?.message ?: "calu ide $description."
        if (launcher.reconnects && diagnosis?.retry != false) {
            setStatus(BridgeStatusInfo(BridgeStatus.EXITED, message))
            scheduleReconnect(message)
        } else {
            setStatus(BridgeStatusInfo(BridgeStatus.EXITED, message))
        }
    }

    /**
     * Start the bridge again after the wait of the next attempt, until a start succeeds, a
     * diagnosis says that it cannot, or `stop`/`dispose` is called.
     *
     * @param why The message of the failure that led here.
     */
    private fun scheduleReconnect(why: String) {
        synchronized(lock) {
            if (disposed || reconnectJob?.isActive == true) return
            val attempt = ++reconnectAttempt
            reconnectJob = scope.launch {
                val wait = delayMs(attempt)
                setStatus(BridgeStatusInfo(BridgeStatus.EXITED, "$why Connecting again in ${(wait + 999) / 1000} s (attempt $attempt)."))
                delay(wait)
                synchronized(lock) { reconnectJob = null }
                try {
                    ensureStarted()
                } catch (_: LaunchException) {
                    // startProcess planned the next attempt if one can help.
                }
            }
        }
    }

    /**
     * Return the running bridge process.
     *
     * @return The record of the process.
     * @throws IllegalStateException if no bridge process runs or its connection is closed.
     */
    private fun requireRunning(): Running {
        val r = running
        if (r == null || r.rpc.closed) throw IllegalStateException("The calu bridge is not running.")
        return r
    }

    /** Call `onLockedChange` if `locked` differs from the value reported last. */
    private fun reportLocked() {
        val now = locked
        val changed = synchronized(lock) { (now != reportedLocked).also { reportedLocked = now } }
        if (changed) onLockedChange(now)
    }

    /**
     * Handle error -32010 of one request: fail, retry, wait for the open prompt, or open one.
     *
     * @param r The process that answered.
     * @param need The request's facts.
     * @return True if the request is to be sent again, false if its caller gets the error.
     */
    private suspend fun unlockFor(r: Running, need: PasswordNeed): Boolean {
        val decision = synchronized(lock) { onNeedsPassword(r.unlock, need).also { r.unlock = it.state } }
        reportLocked()
        return when (decision.action) {
            NeedAction.FAIL -> false
            NeedAction.RETRY -> true
            NeedAction.JOIN -> r.round?.await() ?: false
            NeedAction.PROMPT_FIRST -> runRoundRegistered(r, PROMPT_FIRST)
            NeedAction.PROMPT_ADMIN -> runRoundRegistered(r, PROMPT_ADMIN)
        }
    }

    /**
     * Run a prompt round and register it as the open one, so that other requests can wait for
     * it. Precondition: `r.unlock.promptOpen == kind`.
     *
     * @param r The process to unlock.
     * @param kind Which password is asked.
     * @return True after a successful `ide.unlock`; false after a cancel, too many wrong
     *   passwords or any failure. Postcondition: no prompt is open in the state.
     */
    private suspend fun runRoundRegistered(r: Running, kind: String): Boolean {
        val round = CompletableDeferred<Boolean>()
        r.round = round
        val opened = try {
            runRound(r, kind)
        } catch (error: CancellationException) {
            false
        } catch (error: Exception) {
            log("[calu] unlocking failed: ${errorText(error)}")
            false
        }
        r.round = null
        synchronized(lock) { if (r.unlock.promptOpen != null) r.unlock = onRoundAborted(r.unlock) }
        reportLocked()
        round.complete(opened)
        return opened
    }

    /**
     * Ask for a password and send it with `ide.unlock` until it is accepted, the user cancels,
     * or `MAX_ATTEMPTS_PER_ROUND` entries were refused. An accepted password is put into the
     * `PasswordMemory`.
     *
     * @param r The process to unlock.
     * @param kind Which password is asked.
     * @return True if the round ended with an accepted password.
     * @throws Exception what the prompt throws, and errors of `ide.unlock` other than -32010.
     */
    private suspend fun runRound(r: Running, kind: String): Boolean {
        var problem: String? = null
        var attempt = 1
        while (true) {
            val password = prompt.ask(kind, problem)
            if (password == null) {
                synchronized(lock) { r.unlock = onDismiss(r.unlock) }
                reportLocked()
                return false
            }
            val step = try {
                val result = r.rpc.request("ide.unlock", buildJsonObject { put("password", JsonPrimitive(password)) })
                if (kind == PROMPT_FIRST) memory.password = password
                synchronized(lock) { onUnlockResult(r.unlock, kind, unlockFacts(result), attempt) }
            } catch (error: RpcRequestException) {
                if (error.code != BridgeError.NEEDS_PASSWORD) throw error
                synchronized(lock) { onUnlockRefused(r.unlock, error.bridgeMessage, attempt) }
            }
            synchronized(lock) { r.unlock = step.state }
            reportLocked()
            if (step.outcome != RoundOutcome.ASK_AGAIN) return step.outcome == RoundOutcome.DONE
            problem = step.problem
            attempt++
        }
    }

    /**
     * Send the password kept in memory with `ide.unlock`, right after `ide.initialize`. A
     * refused password is forgotten; the next interactive request that needs one asks. Other
     * failures are logged and change nothing. Never throws.
     *
     * @param r The new process.
     */
    private suspend fun unlockWithRemembered(r: Running) {
        val password = memory.password ?: return
        try {
            val decision = try {
                val result = r.rpc.request("ide.unlock", buildJsonObject { put("password", JsonPrimitive(password)) })
                onStoredResult(r.unlock, unlockFacts(result), false)
            } catch (error: RpcRequestException) {
                val refused = error.code == BridgeError.NEEDS_PASSWORD
                if (!refused) log("[calu] unlocking with the password in memory failed: ${error.bridgeMessage}")
                onStoredResult(r.unlock, null, refused)
            }
            synchronized(lock) { r.unlock = decision.state }
            if (decision.forget) {
                memory.password = null
                log("[calu] the password in memory was refused; it is forgotten.")
            }
            reportLocked()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            log("[calu] the password in memory was not used: ${errorText(error)}")
        }
    }

    /**
     * Set an error status, log the message, and throw it.
     *
     * @param status "error" or "missing".
     * @param message User-facing message.
     * @param missing Whether calu itself was not found.
     * @param keepStatus True if a reconnect was planned and its status message must stay.
     * @return Never returns normally.
     * @throws LaunchException always.
     */
    private fun fail(status: String, message: String, missing: Boolean, keepStatus: Boolean = false): Nothing {
        if (!keepStatus) setStatus(BridgeStatusInfo(status, message))
        log("[calu] $message")
        throw LaunchException(missing, message)
    }

    /**
     * Publish a status.
     *
     * @param info The new status.
     */
    private fun setStatus(info: BridgeStatusInfo) {
        statusFlow.value = info
    }
}

/**
 * Reduce an `ide.unlock` result to the two facts the decisions use, tolerating missing members.
 *
 * @param result The result as received (any JSON value).
 * @return The login names in `result.unlocked` and whether `changing` is true.
 */
fun unlockFacts(result: JsonElement?): UnlockResult =
    UnlockResult(unlockedLogins(result), result.asObj()?.bool("changing") == true)

/**
 * Return a printable message for any failure of a bridge request.
 *
 * @param error The caught exception.
 * @return For an `RpcRequestException` the bridge's message without method and code; else the
 *   exception's message, or its class name if it has none.
 */
fun errorText(error: Throwable): String = when (error) {
    is RpcRequestException -> error.bridgeMessage
    else -> error.message ?: error.javaClass.simpleName
}

/**
 * Return the error code of a failed bridge request.
 *
 * @param error The caught exception.
 * @return The JSON-RPC error code, or `null` if it is not an `RpcRequestException`.
 */
fun errorCode(error: Throwable): Int? = (error as? RpcRequestException)?.code
