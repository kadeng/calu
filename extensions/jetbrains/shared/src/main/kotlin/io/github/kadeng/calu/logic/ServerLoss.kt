/**
 * What the chat makes of a lost connection to the session (pure, no IntelliJ Platform import).
 *
 *     ide.link lost {cause}
 *       cause "unreachable" ──► ide.status ──► serverCheck
 *          "running"  (a server or a session of this host runs)  -> the session ended; the
 *                                                                   server still runs: the
 *                                                                   session list is shown
 *          "stopped" / "unknown"                                  -> "The calu server stopped"
 *                                                                   [Start server] [Retry]
 *       any other cause (revoked token, refused, closed)         -> "connection lost" (as before)
 *
 *     while "The calu server stopped" is shown: Retry, or the tool window's check
 *     (every 10 s while it is visible, every 30 s while it is hidden)
 *       serverCheck "running" -> attach as at the start (the same session, else the project's)
 *
 * In every unreachable case the transcript of the session stays visible, greyed, under
 * "Session ended" (`sessionEnded`), until another session is attached; its open cards are one
 * line each and are not counted as waiting.
 *
 * Ported from `extensions/vscode/src/shared/serverLoss.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.jsTruthy
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.LinkView
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.openEscalationCount
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Causes of a lost link that `calu.ide.link` names (`ide.link lost` member `cause`). */
object LostCause {
    /** Nothing answers at the session's address: its process or its server stopped, or the network is down. */
    const val UNREACHABLE: String = "unreachable"

    /** The session's token was revoked. */
    const val REVOKED: String = "revoked"

    /** The session refused the connection. */
    const val REFUSED: String = "refused"

    /** The session closed every new connection. */
    const val CLOSED: String = "closed"
}

/** Seconds between two checks for a stopped server while the tool window is visible. */
const val SERVER_CHECK_VISIBLE_S: Int = 10

/** Seconds between two checks for a stopped server while the tool window is hidden. */
const val SERVER_CHECK_HIDDEN_S: Int = 30

/** Id of the setup hint of a stopped server. */
const val SERVER_STOPPED_HINT: String = "server-stopped"

/** The sentence an older bridge sends for a refused connection (it sends no `cause`). */
private const val UNREACHABLE_TEXT = "nothing answers at the session's address"

/**
 * Read the cause of a lost link from the `ide.link` params.
 *
 * @param cause The `cause` member (untrusted; `null` when it is missing).
 * @param message The `message` member (untrusted); used only when `cause` is missing, for a
 *   bridge of an older calu, whose sentence for a refused connection starts with
 *   "nothing answers at the session's address".
 * @return One of `LostCause`, or `null` if neither member names one.
 */
fun lostCause(cause: JsonElement?, message: JsonElement?): String? {
    val c = (cause as? JsonPrimitive)?.takeIf { it.isString }?.content
    if (c in setOf(LostCause.UNREACHABLE, LostCause.REVOKED, LostCause.REFUSED, LostCause.CLOSED)) return c
    if (cause == null || cause is JsonNull) {
        val m = (message as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (m != null && m.startsWith(UNREACHABLE_TEXT)) return LostCause.UNREACHABLE
    }
    return null
}

/**
 * What `ide.status` says about the calu server of the project's machine.
 *
 * RUNNING: a `calu serve` process of this host is recorded and not known to be gone, or a
 * session of this host says it runs. STOPPED: the database was read and holds neither.
 * UNKNOWN: no status, or the status could not tell (no database, a login that may not read
 * the servers, or an older calu without `servers_running`) and no session runs.
 */
enum class ServerCheck { RUNNING, STOPPED, UNKNOWN }

/**
 * Tell from `ide.status` whether a calu server runs on the project's machine.
 *
 * @param status The last `ide.status` result, or `null` if it failed or was not asked.
 * @return See `ServerCheck`.
 */
fun serverCheck(status: JsonObject?): ServerCheck {
    if (status == null) return ServerCheck.UNKNOWN
    val server = status.obj("server")
    val servers = server?.int("servers_running")
    val sessions = server?.int("sessions_running") ?: 0
    if ((servers != null && servers > 0) || sessions > 0) return ServerCheck.RUNNING
    return if (servers != null && status.obj("database")?.get("reachable").jsTruthy()) ServerCheck.STOPPED else ServerCheck.UNKNOWN
}

/**
 * Decide whether a lost link means that the calu server stopped.
 *
 * @param cause The cause of the loss (`lostCause`).
 * @param check What `ide.status` said right after the loss.
 * @return True for an unreachable session while no server is known to run (STOPPED or
 *   UNKNOWN: a session that nothing answers for is most likely gone with its server).
 */
fun serverStoppedAfterLoss(cause: String?, check: ServerCheck): Boolean =
    cause == LostCause.UNREACHABLE && check != ServerCheck.RUNNING

/**
 * Tell whether the transcript shows a session that ended: the link to it was lost because
 * nothing answers at its address, and no session was attached since. The view greys the
 * transcript and writes "Session ended" above it; open cards can no longer be decided.
 *
 * @param attached True if a session is attached (`isAttached`).
 * @param link The view's link state.
 * @return True in that case.
 */
fun sessionEnded(attached: Boolean, link: LinkView): Boolean =
    !attached && link.status == LinkStatus.LOST && link.cause == LostCause.UNREACHABLE

/**
 * Tell whether the view shows the transcript of a session that ended (`sessionEnded`).
 *
 * @param state The view state.
 * @return True if no session is attached and the last link was lost as "unreachable".
 */
fun transcriptEnded(state: ViewState): Boolean = sessionEnded(isAttached(state), state.link)

/**
 * Count the decisions the user can still make: the open escalations, none while the
 * transcript is of a session that ended (its cards cannot be decided). The status bar widget,
 * the tab chips and the activity line count these.
 *
 * @param state The view state.
 * @return The number.
 */
fun decisionsWaiting(state: ViewState): Int = if (transcriptEnded(state)) 0 else openEscalationCount(state)

/**
 * Build the decisions part of the status bar widget.
 *
 *     the server stopped (hint "server-stopped")   "calu: server stopped"
 *     the session ended, a server runs              "calu: session ended"
 *     decisions open                                "calu: 2 waiting"
 *     otherwise                                     "" (the widget takes no room)
 *
 * @param state The view state.
 * @return The text.
 */
fun decisionsStatus(state: ViewState): String {
    if (transcriptEnded(state)) {
        return if (state.hints.any { it.id == SERVER_STOPPED_HINT }) "calu: server stopped" else "calu: session ended"
    }
    val open = openEscalationCount(state)
    return if (open > 0) "calu: $open waiting" else ""
}

/**
 * Build the text of the "server stopped" card.
 *
 * @param retriedAt Local time ("14:02:11") of the last Retry that found no server, or `null`
 *   before the first.
 * @return One or two sentences.
 */
fun serverStoppedMessage(retriedAt: String?): String {
    val base = "The calu server stopped; the session ended with it. Start server opens a terminal that runs it."
    return if (retriedAt != null) "$base Checked at $retriedAt: no server runs yet." else base
}

/**
 * Return the seconds between two checks for a stopped server.
 *
 * @param visible True while the tool window is visible.
 * @return `SERVER_CHECK_VISIBLE_S` or `SERVER_CHECK_HIDDEN_S`.
 */
fun serverCheckInterval(visible: Boolean): Int = if (visible) SERVER_CHECK_VISIBLE_S else SERVER_CHECK_HIDDEN_S
