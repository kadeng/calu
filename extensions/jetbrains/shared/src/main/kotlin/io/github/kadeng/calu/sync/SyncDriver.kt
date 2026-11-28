/**
 * Connects a [Synchronizer] to a running bridge: what happens at the bridge becomes one ordered
 * stream of work for the synchroniser.
 *
 *     supervisor.status "running"        ──► connect(capabilities)
 *     supervisor.status anything else    ──► disconnected()
 *     notification ide.fs.changed        ──► remoteChanged(...)
 *     session event turn.result,         ──► holdsChanged()      (a hold may have ended)
 *       session.state; ide.link
 *     local saves (the IDE's listener)   ──► post { localChanged(paths) }
 *
 * A single coroutine takes the work in arrival order. A remote change is therefore never
 * decided before the notification that precedes it.
 */
package io.github.kadeng.calu.sync

import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.json.strings
import io.github.kadeng.calu.rpc.RpcEvent
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.BridgeStatus
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch

/**
 * Feeds one synchroniser from one bridge supervisor.
 *
 * @param sync the synchroniser.
 * @param supervisor the bridge of the project.
 * @param scope scope of the driver's two coroutines; cancelling it ends the driver.
 * @param log receives lines for the IDE's log.
 * @param around runs the connection's work (which can be a long first copy), e.g. under a
 *   progress indicator whose cancel button cancels the work; the default just runs it.
 */
class SyncDriver(
    val sync: Synchronizer,
    private val supervisor: BridgeSupervisor,
    private val scope: CoroutineScope,
    private val log: (String) -> Unit = {},
    private val around: suspend (work: suspend () -> Unit) -> Unit = { it() },
) {
    private val inbox = Channel<suspend () -> Unit>(Channel.UNLIMITED)
    private val listener: (RpcEvent) -> Unit = ::onNotification
    private var jobs: List<Job> = emptyList()

    /**
     * Start listening: the supervisor's status and notifications lead to work from now on. A
     * bridge that already runs is connected at once.
     */
    fun start() {
        supervisor.notificationListeners.add(listener)
        val worker = scope.launch {
            for (work in inbox) {
                try {
                    work()
                } catch (error: CancellationException) {
                    // A cancelled piece of work (the user stopped a clone) does not end the driver.
                    ensureActive()
                } catch (error: Exception) {
                    log("[calu] sync: ${error.javaClass.simpleName}: ${error.message}")
                }
            }
        }
        val watcher = scope.launch {
            supervisor.status.collect { info ->
                if (info.status == BridgeStatus.RUNNING) {
                    val capabilities = supervisor.initializeResult?.strings("capabilities").orEmpty()
                    post { around { sync.connect(capabilities) } }
                } else {
                    post { sync.disconnected() }
                }
            }
        }
        jobs = listOf(worker, watcher)
    }

    /** Stop listening and end the driver's coroutines. Queued work is dropped. */
    fun stop() {
        supervisor.notificationListeners.remove(listener)
        for (job in jobs) job.cancel()
        inbox.close()
    }

    /**
     * Add a piece of work behind what is queued.
     *
     * @param work the work; it runs on the driver's coroutine.
     */
    fun post(work: suspend () -> Unit) {
        inbox.trySend(work)
    }

    /**
     * Turn a notification of the bridge into work (called on the supervisor's reader thread).
     *
     * @param event the notification.
     */
    private fun onNotification(event: RpcEvent) {
        when (event.method) {
            "ide.fs.changed" -> {
                val changed = parseFsChanged(event.params)
                post { sync.remoteChanged(changed) }
            }
            "session.event" -> if (event.params.str("method") in HOLD_EVENTS) post { sync.holdsChanged() }
            "ide.link" -> post { sync.holdsChanged() }
        }
    }

    companion object {
        /** Session events after which a held file may be free. */
        val HOLD_EVENTS: Set<String> = setOf("turn.result", "session.state")
    }
}
