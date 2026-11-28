package io.github.kadeng.calu.sync

import io.github.kadeng.calu.chat.EDIT_TOOLS
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.absoluteRemotePath
import io.github.kadeng.calu.state.ViewState

/** Session states in which no turn runs, so no file is held. */
private val QUIET_STATES: Set<String> = setOf("idle", "stopped", "failed")

/**
 * The files whose upload is held because the agent is editing them in the running turn
 * (`agent/records/notes/jetbrains-sync-spec.md`, "holds").
 *
 *     session idle, stopped or failed          -> nothing is held
 *     a turn runs                              -> every file an edit tool of this turn touched:
 *                                                 calls without a result yet, and calls whose
 *                                                 result arrived after the last turn end
 *
 * The view state keeps no turn number per tool call, so "of this turn" is read from the main
 * transcript: the tool items after the last "turn" item, plus the unfinished edit calls of
 * subagents.
 *
 * @param state The chat's view state of the attached session.
 * @return The held paths, absolute on calu's machine (relative ones are resolved against the
 *   session's directory); empty while no turn runs.
 */
fun heldPaths(state: ViewState): Set<String> {
    if (state.session.state in QUIET_STATES) return emptySet()
    val main = state.items["main"].orEmpty()
    val sinceTurn = main.subList(main.indexOfLast { it.kind == "turn" } + 1, main.size)
        .filterIsInstance<io.github.kadeng.calu.state.ToolItem>().map { it.toolUseId }.toSet()
    return state.tools.values
        .filter { it.name in EDIT_TOOLS && it.stored == null && (it.result == null || it.toolUseId in sinceTurn) }
        .mapNotNull { it.diffPath ?: it.input.str("file_path") ?: it.input.str("notebook_path") }
        .map { absoluteRemotePath(it, state.session.workspace) }
        .toSet()
}

/**
 * The files held when the IDE closed, and the session whose turn held them (test plan B23).
 *
 * A local save of a file the agent edits in the running turn is held (queued, not uploaded)
 * until the turn ends. The queue survives a restart of the IDE. After the restart the chat
 * knows nothing about the running turn until it is attached again, so [heldPaths] is empty.
 * The first comparison would then upload the held save at once. The plugin therefore stores
 * the held paths with the session id. It holds them after a restart until [holdEnded] says
 * that this session's turn is over.
 *
 * @property sessionId calu's session id of the turn that held the files; null if none was attached.
 * @property paths the held paths, relative to the project; never empty in a stored record.
 */
data class KeptHold(val sessionId: String?, val paths: Set<String>) {
    /**
     * Write the record as text: the session id (or "-") on the first line, then one path per line.
     *
     * @return the text.
     */
    fun encode(): String = (sessionId ?: "-") + "\n" + paths.sorted().joinToString("") { "$it\n" }

    companion object {
        /**
         * Parse the text of [encode]. No side effects.
         *
         * @param text the stored text.
         * @return the record; null for an empty text or a record without paths.
         */
        fun decode(text: String): KeptHold? {
            val lines = text.split('\n').map { it.removeSuffix("\r") }
            val id = lines.firstOrNull()?.takeIf { it.isNotEmpty() } ?: return null
            val paths = lines.drop(1).filter { it.isNotEmpty() }.toSet()
            return if (paths.isEmpty()) null else KeptHold(id.takeIf { it != "-" }, paths)
        }
    }
}

/**
 * Tell whether the turn that held files before a restart of the IDE is known to have ended.
 *
 *     attached to that session (or to any, for an unknown id), state idle/stopped/failed -> ended
 *     the session list was read without error and does not list that session as running,
 *       or lists it as stale (its process is gone)                                       -> ended
 *     anything else (not attached yet, attached and running, list not read)             -> not known
 *
 * @param state the chat's view state now.
 * @param sessionId the session of [KeptHold]; null if the hold was recorded without one (then
 *   any attached quiet session, or a list without a running session here, ends it).
 * @return true only when the end is known; false while it is not.
 */
fun holdEnded(state: ViewState, sessionId: String?): Boolean {
    val attached = state.link.status == io.github.kadeng.calu.state.LinkStatus.ATTACHED && state.session.sessionId != null
    if (attached && (sessionId == null || state.session.sessionId == sessionId)) return state.session.state in QUIET_STATES
    val list = state.sessions
    if (!list.loaded || list.error != null) return false
    val running = list.list.filter { !it.stale && it.state !in QUIET_STATES }
    return if (sessionId == null) running.none { it.here } else running.none { it.id == sessionId }
}

/**
 * Keeps the held paths across a restart of the IDE (test plan B23): every answer is stored with the
 * session id, and a record found at the start stays held until [holdEnded] is true for its session.
 *
 * @param load reads the record stored by the last IDE run (null if none).
 * @param save stores the record (null removes it); failures are the caller's to ignore.
 */
class HoldKeeper(load: () -> KeptHold?, private val save: (KeptHold?) -> Unit) {
    private var restored: KeptHold? = load()
    private var saved: KeptHold? = restored

    /**
     * Create a keeper on a synchronisation store.
     *
     * @param store the store of the project copy (file `holds.txt`).
     */
    constructor(store: SyncStore) : this(store::loadHolds, store::saveHolds)

    /**
     * The paths held now: the live holds of the running turn plus a record restored at the start
     * whose turn is not known to have ended. Side effect: stores the result when it changed, and
     * drops the restored record once its turn ended.
     *
     * @param state the chat's view state now.
     * @param live the paths [heldPaths] gives for that state, relative to the project.
     * @return the held paths, relative to the project.
     */
    @Synchronized
    fun held(state: ViewState, live: Set<String>): Set<String> {
        restored?.let { if (holdEnded(state, it.sessionId)) restored = null }
        val kept = restored
        val all = live + kept?.paths.orEmpty()
        val record = if (all.isEmpty()) null else KeptHold(kept?.sessionId ?: state.session.sessionId, all)
        if (record != saved) {
            save(record)
            saved = record
        }
        return all
    }
}
