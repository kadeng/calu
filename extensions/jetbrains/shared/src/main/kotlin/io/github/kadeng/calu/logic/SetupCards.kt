/**
 * What the chat shows while it cannot work: one card per problem (pure).
 *
 * Port of the "empty and error states" part of `extensions/vscode/src/shared/chatModel.ts`.
 * A button of the original posts a webview message; here it names an action:
 *
 *     action               what the surface does
 *     install              offer to install calu (`Bootstrap.kt`)
 *     restartBridge        `act("restartBridge")`
 *     showLog              open the plugin's log
 *     startServer          open a terminal with `ViewState.serverHelp.command`
 *     retryServer          `act("retryServer")`: look for the stopped server again
 *     copyServerCommand    copy `ViewState.serverHelp.command`
 *     attach               open the session picker (resume a stored conversation)
 *     newSession           `act("start", {kind: "new"})`
 *     unlock               `act("unlock")`
 *     cancelStart          `act("cancelStart")`
 *     openLink             open `href` in a browser
 *     (any other)          the `action` of a `SetupHint`, passed on unchanged
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.SetupHint
import io.github.kadeng.calu.state.StartingView
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAttached

/**
 * A button of a setup card.
 *
 * @property label The button's text.
 * @property action What it does (see the table in the file comment).
 * @property href For "openLink": the https address; `null` otherwise.
 */
data class CardAction(val label: String, val action: String, val href: String? = null)

/**
 * One card of the empty / error state: one sentence, one primary action.
 *
 * @property id Names the card ("calu-missing", "bridge", "starting", "link-lost",
 *   `STARTING_CARD`, or a hint's id).
 * @property level "info", "warning" or "error".
 * @property message The sentence.
 * @property primary The one primary button; `null` if nothing can be done (e.g. while starting).
 * @property secondary Further buttons.
 * @property serverHelp True for the card that shows the server command box.
 * @property sessionList True for the card that lists the running sessions to pick from.
 * @property busy True while the card stands for a request that runs (it shows a spinner).
 * @property compact True for a card that is shown as one line with a "Dismiss" button: a hint
 *   that matters before the first session but not for the one that is attached and working.
 * @property short For a compact card: the few words shown in the line (`message` is the tooltip).
 */
data class SetupCard(
    val id: String,
    val level: String,
    val message: String,
    val primary: CardAction? = null,
    val secondary: List<CardAction> = emptyList(),
    val serverHelp: Boolean = false,
    val sessionList: Boolean = false,
    val busy: Boolean = false,
    val compact: Boolean = false,
    val short: String? = null,
)

/** Id of the card of a session request that waits for the server (`ViewState.starting`). */
const val STARTING_CARD: String = "starting-session"

/**
 * Build the card of a session request that waits for the server.
 *
 *     at once        "<title>… Asking the calu server for a session."          [Cancel]
 *     after ~3 s     "… No answer yet: maybe no calu server is running. ..."   [Start calu server] [Cancel]
 *
 * @param starting `ViewState.starting`.
 * @return The card; "Cancel" is the action "cancelStart", "Start calu server" is "startServer"
 *   (the request keeps waiting, so a server started now still answers it).
 */
fun startingCard(starting: StartingView): SetupCard {
    val cancel = CardAction("Cancel", "cancelStart")
    val message = "${starting.title}… Asking the calu server for a session."
    if (!starting.slow) return SetupCard(STARTING_CARD, "info", message, secondary = listOf(cancel), busy = true)
    return SetupCard(
        STARTING_CARD, "warning",
        "$message No answer yet: maybe no calu server is running. You can start one now; this request keeps waiting for it.",
        primary = CardAction("Start calu server", "startServer"), secondary = listOf(cancel), busy = true,
    )
}

/**
 * Turn a hint's action into a button.
 *
 * @param hint The hint.
 * @return The button, or `null` for a hint without one (or "openLink" without `href`).
 */
fun hintAction(hint: SetupHint): CardAction? {
    if (hint.action.isEmpty()) return null
    if (hint.action == "openLink") return hint.href?.takeIf { it.isNotEmpty() }?.let { CardAction(hint.actionLabel, "openLink", it) }
    return CardAction(hint.actionLabel, hint.action)
}

/**
 * Decide what the chat shows while it cannot work: one card per problem, most important first.
 *
 *     bridge "missing"                 -> Install calu (secondary: the hint's button, Open settings)
 *     bridge "error" / "exited"        -> Restart bridge (secondary: Show log)
 *     bridge "stopped"                 -> Start (restartBridge)
 *     bridge "starting"                -> a line without a button
 *     bridge "running", not attached   -> the host's hints; "server-stopped": Start server
 *                                         (secondary: Retry); "no-server" shows the server command
 *                                         (Open terminal / Copy command), "no-session" lists the
 *                                         running sessions (secondary: Resume a stored conversation…)
 *     attached                         -> the hints that still apply (e.g. "claude-missing"),
 *                                         as one dismissible line each (`compact`)
 *     a session request waits          -> `startingCard` first; the "no-session" card is left out
 *
 * @param state The view state.
 * @param dismissed Ids of compact cards the user dismissed; they are left out.
 * @return The cards; empty if nothing is missing.
 */
fun setupCards(state: ViewState, dismissed: Collection<String> = emptyList()): List<SetupCard> {
    val bridge = state.bridge
    fun fromHint(hint: SetupHint): SetupCard {
        val c = SetupCard(hint.id, hint.level, hint.message, hintAction(hint))
        return when (hint.id) {
            "no-server" -> c.copy(
                serverHelp = state.serverHelp != null,
                primary = CardAction("Open terminal with this command", "startServer"),
                secondary = listOf(CardAction("Copy command", "copyServerCommand")),
            )
            // Short, so that the ended transcript below keeps its room; the command is in the
            // terminal that "Start server" opens.
            SERVER_STOPPED_HINT -> c.copy(secondary = listOf(CardAction("Retry", "retryServer")))
            "no-session" -> c.copy(sessionList = true, secondary = listOf(CardAction("Resume a stored conversation…", "attach")))
            else -> c
        }
    }
    val text = bridge.message?.takeIf { it.isNotEmpty() }
    when (bridge.status) {
        BridgeStatus.MISSING -> {
            // calu is missing on this machine (only the local launcher reports that): the card
            // offers the installation; the hint's own button (the settings, for a calu that is
            // installed elsewhere) comes second.
            val hint = state.hints.firstOrNull { it.id == "calu-missing" }
            val card = if (hint != null) fromHint(hint) else SetupCard("calu-missing", "error", text ?: "calu is not installed on this machine.")
            return listOf(card.copy(primary = CardAction("Install calu", "install"), secondary = listOfNotNull(card.primary?.takeIf { it.action != "install" })))
        }
        BridgeStatus.ERROR, BridgeStatus.EXITED -> return listOf(
            SetupCard(
                "bridge", "error", text ?: "The calu bridge stopped unexpectedly.",
                CardAction("Restart bridge", "restartBridge"), listOf(CardAction("Show log", "showLog")),
            ),
        )
        BridgeStatus.STOPPED -> return listOf(SetupCard("bridge", "info", text ?: "The calu bridge is not running.", CardAction("Start", "restartBridge")))
        BridgeStatus.STARTING -> return listOf(SetupCard("starting", "info", text ?: "Starting calu…", busy = true))
    }
    val attached = isAttached(state)
    var cards = if (attached) {
        state.hints.filter { it.id !in dismissed }.map { h ->
            val c = fromHint(h)
            val primary = c.primary?.let { p -> h.actionShort?.takeIf { it.isNotEmpty() }?.let { p.copy(label = it) } ?: p }
            c.copy(primary = primary, compact = true, short = h.short?.takeIf { it.isNotEmpty() })
        }
    } else {
        state.hints.map(::fromHint)
    }
    // A session that nothing answers for ended: the "server-stopped" card or the session list
    // says what to do, and the transcript is marked "Session ended" (`sessionEnded`).
    if (!attached && state.link.status == LinkStatus.LOST && state.link.cause != LostCause.UNREACHABLE) {
        val why = state.link.message?.takeIf { it.isNotEmpty() }?.let { ": $it" } ?: "."
        cards = listOf(SetupCard("link-lost", "warning", "The connection to the session was lost$why")) + cards
    }
    state.starting?.let { cards = listOf(startingCard(it)) + cards.filter { c -> c.id != "no-session" } }
    return syncCards(state.sync, dismissed) + listOfNotNull(interpreterCard(state.interpreter, dismissed)) + cards
}

/** Id of the card that offers PyCharm's SSH interpreter (`ViewState.interpreter`). */
const val INTERPRETER_CARD: String = "ssh-interpreter"

/**
 * Build the card that offers to set up running and debugging on the server (SSH mode).
 *
 *     offer     "PyCharm can run and debug this project on <host> …"   [Set up running and debugging on <host>] [Not now]
 *     working   the same sentence with a spinner, no button
 *     failed    the reason                                              [Try again] [Not now]
 *
 * "Set up …" and "Try again" are the action "setupInterpreter"; "Not now" is
 * "dismiss:ssh-interpreter", which the surface handles by hiding the card until the IDE restarts.
 *
 * @param offer The offer; `null` for none.
 * @param dismissed Ids of cards the user dismissed.
 * @return The card, or `null` if there is no offer or it was dismissed.
 */
fun interpreterCard(offer: io.github.kadeng.calu.state.InterpreterOfferView?, dismissed: Collection<String> = emptyList()): SetupCard? {
    if (offer == null || INTERPRETER_CARD in dismissed) return null
    val label = "Set up running and debugging on ${offer.host}"
    val notNow = CardAction("Not now", "dismiss:$INTERPRETER_CARD")
    val sentence = "PyCharm can run and debug this project on ${offer.host}: an SSH interpreter whose path mapping " +
        "joins this folder and ${offer.directory}, so that breakpoints and line numbers match. You confirm the Python on the server first."
    return when (offer.phase) {
        "working" -> SetupCard(INTERPRETER_CARD, "info", "Setting up the SSH interpreter on ${offer.host}…", busy = true)
        "failed" -> SetupCard(
            INTERPRETER_CARD, "warning", "The SSH interpreter was not created: ${offer.message ?: "unknown reason"}",
            CardAction("Try again", "setupInterpreter"), listOf(notNow),
        )
        else -> SetupCard(INTERPRETER_CARD, "info", sentence, CardAction(label, "setupInterpreter"), listOf(notNow))
    }
}

/**
 * The cards of the file synchronisation (SSH mode): what waits for the user there.
 *
 *     refused (no "fs" capability)   -> the reason, "Plugin settings"
 *     waiting (first plan, a clone)  -> the reason, "Clone from server" / "Sync now"
 *     failed                         -> the reason, "Sync now"
 *     conflicts                      -> the paths, "Merge <first path>…"
 *     held files                     -> one line, "Upload now" (dismissible)
 *     skipped paths                  -> one line, "Show" (dismissible)
 *
 * @param sync The synchronisation's status; `null` if it is off.
 * @param dismissed Ids of compact cards the user dismissed.
 * @return The cards, most urgent first; empty if nothing waits.
 */
fun syncCards(sync: io.github.kadeng.calu.state.SyncView?, dismissed: Collection<String> = emptyList()): List<SetupCard> {
    if (sync == null) return emptyList()
    val out = ArrayList<SetupCard>()
    when (sync.phase) {
        "refused" -> out.add(SetupCard("sync-refused", "error", sync.message ?: "File synchronisation is not available.", CardAction("Plugin settings", "openSettings")))
        "waiting" -> out.add(
            SetupCard(
                "sync-waiting", "info", "calu sync: " + (sync.message ?: "a plan waits for your confirmation."),
                CardAction(if (sync.message.orEmpty().contains("cloned")) "Clone from server" else "Sync now", "syncNow"),
            ),
        )
        "failed" -> out.add(SetupCard("sync-failed", "warning", "calu sync failed: " + (sync.message ?: "unknown reason"), CardAction("Sync now", "syncNow")))
    }
    if (sync.conflicts.isNotEmpty()) {
        val more = if (sync.conflicts.size > 1) " (and ${sync.conflicts.size - 1} more: ${sync.conflicts.drop(1).take(5).joinToString(", ")})" else ""
        out.add(
            SetupCard(
                "sync-conflict", "warning",
                "calu sync: ${sync.conflicts[0]} was changed here and on the server$more. Neither side is overwritten until you decide.",
                CardAction("Merge ${sync.conflicts[0].substringAfterLast('/')}…", "syncResolve"),
            ),
        )
    }
    if (sync.held.isNotEmpty() && sync.phase != "offline" && "sync-held" !in dismissed) {
        val n = sync.held.size
        out.add(
            SetupCard(
                "sync-held", "info", "calu sync: ${sync.held.joinToString(", ")} ${if (n == 1) "waits" else "wait"} until the agent's turn ends, because the agent is editing ${if (n == 1) "it" else "them"}.",
                CardAction("Upload now", "syncUploadNow"), compact = true, short = "$n ${if (n == 1) "file" else "files"} held until the turn ends",
            ),
        )
    }
    if (sync.notes.isNotEmpty() && "sync-notes" !in dismissed) {
        val n = sync.notes.size
        out.add(
            SetupCard(
                "sync-notes", "info", sync.notes.joinToString("\n"), CardAction("Show", "syncShowSkipped"),
                compact = true, short = "calu sync: $n ${if (n == 1) "path is" else "paths are"} not synchronised",
            ),
        )
    }
    return out
}
