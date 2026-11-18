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
 *     bridge "missing"                 -> Install calu
 *     bridge "error" / "exited"        -> Restart bridge (secondary: Show log)
 *     bridge "stopped"                 -> Start (restartBridge)
 *     bridge "starting"                -> a line without a button
 *     bridge "running", not attached   -> the host's hints; "no-server" shows the server command
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
            "no-session" -> c.copy(sessionList = true, secondary = listOf(CardAction("Resume a stored conversation…", "attach")))
            else -> c
        }
    }
    val text = bridge.message?.takeIf { it.isNotEmpty() }
    when (bridge.status) {
        BridgeStatus.MISSING -> {
            val hint = state.hints.firstOrNull { it.id == "calu-missing" }
            return listOf(
                if (hint != null) fromHint(hint)
                else SetupCard("calu-missing", "error", text ?: "calu is not installed on this machine.", CardAction("Install calu", "install")),
            )
        }
        BridgeStatus.ERROR, BridgeStatus.EXITED -> return listOf(
            SetupCard(
                "bridge", "error", text ?: "The calu bridge stopped unexpectedly.",
                CardAction("Restart bridge", "restartBridge"), listOf(CardAction("Show log", "showLog")),
            ),
        )
        BridgeStatus.STOPPED -> return listOf(SetupCard("bridge", "info", text ?: "The calu bridge is not running.", CardAction("Start", "restartBridge")))
        BridgeStatus.STARTING -> return listOf(SetupCard("starting", "info", "Starting calu…"))
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
    if (!attached && state.link.status == LinkStatus.LOST) {
        val why = state.link.message?.takeIf { it.isNotEmpty() }?.let { ": $it" } ?: "."
        cards = listOf(SetupCard("link-lost", "warning", "The connection to the session was lost$why")) + cards
    }
    state.starting?.let { cards = listOf(startingCard(it)) + cards.filter { c -> c.id != "no-session" } }
    return cards
}
