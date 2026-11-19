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
