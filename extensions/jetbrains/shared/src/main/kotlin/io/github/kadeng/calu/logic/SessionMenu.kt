/**
 * The session dropdown of a chat surface (pure): its rows and the text of its button.
 *
 *     ViewState ──► sessionMenu()      entries of the session dropdown
 *               ──► sessionLabel()     text of the dropdown's button
 *
 * Port of the "session dropdown" part of `extensions/vscode/src/shared/chatModel.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.SessionEntry
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.sessionTitle

/** One row of the session dropdown. */
sealed interface SessionMenuEntry {
    /**
     * A heading above a group of sessions.
     *
     * @property label The heading's text.
     */
    data class Heading(val label: String) : SessionMenuEntry

    /**
     * A running session.
     *
     * @property id The session's id.
     * @property title Its name, else its directory's last segment, else its id (`sessionTitle`).
     * @property directory Short directory for the second line.
     * @property directoryFull The whole directory (tooltip).
     * @property phase Phase for the state dot (`sessionPhase`).
     * @property state The session's protocol state.
     * @property openEscalations Number of steps that wait for a decision.
     * @property stale True if the session's process did not report lately.
     * @property attached True for the attached session.
     */
    data class Session(
        val id: String,
        val title: String,
        val directory: String,
        val directoryFull: String,
        val phase: String,
        val state: String,
        val openEscalations: Int,
        val stale: Boolean,
        val attached: Boolean,
    ) : SessionMenuEntry

    /**
     * A line of text (the list is empty, or could not be loaded).
     *
     * @property label The text.
     */
    data class Note(val label: String) : SessionMenuEntry

    /**
     * An action at the bottom of the dropdown.
     *
     * @property id "new", "resume", "fork", "rename", "refresh" or "detach".
     * @property label The entry's text.
     * @property enabled False if the action needs an attached session and none is.
     */
    data class Action(val id: String, val label: String, val enabled: Boolean) : SessionMenuEntry
}

/**
 * Build the rows of the session dropdown.
 *
 *     This workspace        (heading, if a session of the workspace runs)
 *       ● name / directory  one row per running session, the attached one marked
 *     Other sessions        (heading, if sessions of other directories run)
 *       ...
 *     (note: "No session is running." / the list's error)
 *     New session… · Resume a stored conversation… · Fork conversation · Rename session… ·
 *     Refresh · Detach
 *
 * @param state The view state.
 * @return The rows. "Fork conversation" needs an attached session with a conversation;
 *   "Rename session…" and "Detach" need an attached session.
 */
fun sessionMenu(state: ViewState): List<SessionMenuEntry> {
    val entries = ArrayList<SessionMenuEntry>()
    val attachedId = state.sessions.attachedId
    fun row(s: SessionEntry) = SessionMenuEntry.Session(
        id = s.id, title = sessionTitle(s), directory = shortDirectory(s.directory, 36), directoryFull = s.directory,
        phase = sessionPhase(s.state), state = s.state, openEscalations = s.openEscalations, stale = s.stale, attached = s.id == attachedId,
    )
    val here = state.sessions.list.filter { it.here }
    val other = state.sessions.list.filter { !it.here }
    if (here.isNotEmpty()) { entries.add(SessionMenuEntry.Heading("This workspace")); entries.addAll(here.map(::row)) }
    if (other.isNotEmpty()) {
        entries.add(SessionMenuEntry.Heading(if (here.isNotEmpty()) "Other sessions" else "Running sessions"))
        entries.addAll(other.map(::row))
    }
    val error = state.sessions.error
    if (!error.isNullOrEmpty()) {
        entries.add(SessionMenuEntry.Note("The sessions could not be listed: $error"))
    } else if (state.sessions.list.isEmpty()) {
        entries.add(SessionMenuEntry.Note(if (state.sessions.loaded) "No session is running." else "Loading the sessions…"))
    }
    val attached = attachedId != null
    entries.add(SessionMenuEntry.Action("new", "New session…", true))
    entries.add(SessionMenuEntry.Action("resume", "Resume a stored conversation…", true))
    entries.add(SessionMenuEntry.Action("fork", "Fork conversation", attached && state.session.claudeSessionId != null))
    entries.add(SessionMenuEntry.Action("rename", "Rename session…", attached))
    entries.add(SessionMenuEntry.Action("refresh", "Refresh", true))
    entries.add(SessionMenuEntry.Action("detach", "Detach", attached))
    return entries
}

/**
 * Text and state of the session dropdown's button.
 *
 * @property label The attached session's name, else the last segment of its directory, else
 *   "No session"; "calu" while the bridge does not run.
 * @property title The full directory and state, for the tooltip.
 * @property dot Kind of the state dot: "off" or "starting" while the bridge does not run,
 *   "detached", "lost", "reconnecting", or the session's phase.
 */
data class SessionLabel(val label: String, val title: String, val dot: String)

/**
 * Return the text and state of the session dropdown's button.
 *
 * @param state The view state.
 * @return The label (see `SessionLabel`).
 */
fun sessionLabel(state: ViewState): SessionLabel {
    if (state.bridge.status != BridgeStatus.RUNNING) {
        return SessionLabel("calu", "The calu bridge is ${state.bridge.status}.", if (state.bridge.status == BridgeStatus.STARTING) "starting" else "off")
    }
    val id = state.sessions.attachedId
    if (id.isNullOrEmpty() || state.link.status == LinkStatus.DETACHED) {
        return SessionLabel("No session", "No session is attached. Choose or start one.", "detached")
    }
    val entry = state.sessions.list.firstOrNull { it.id == id }
    val label = if (entry != null) sessionTitle(entry) else sessionTitle(id, "", state.session.workspace.orEmpty())
    val where = entry?.directory?.takeIf { it.isNotEmpty() } ?: state.session.workspace.orEmpty()
    if (state.link.status != LinkStatus.ATTACHED) {
        val message = state.link.message?.takeIf { it.isNotEmpty() }?.let { ": $it" }.orEmpty()
        return SessionLabel(label, "$where\n${state.link.status}$message", state.link.status)
    }
    return SessionLabel(label, "$where\nSession ${id.take(8)}, ${state.session.state}", sessionPhase(state.session.state))
}
