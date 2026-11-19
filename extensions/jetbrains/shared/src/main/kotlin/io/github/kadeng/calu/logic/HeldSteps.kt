/**
 * Which tool calls calu holds back while a decision of the user is open.
 *
 * The session reports a hold only as a log line, so a client derives it (the same rule as the
 * full-screen terminal client, `calu.tui.model.Model.held`):
 *
 *     an escalation that is no question is open
 *       and a tool call has neither a check nor a result, is not recorded history,
 *       is not a question of the agent, and is not an agent launch whose subagent runs
 *     => that call is held
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAgentLaunch

/**
 * Tell whether an open escalation holds other steps back.
 *
 * @param state The view state.
 * @return True if at least one open escalation is a decision about a step (not a question).
 */
fun isHolding(state: ViewState): Boolean =
    state.escalations.values.any { it.status == EscalationStatus.OPEN && it.questions.isEmpty() }

/**
 * Return the tool calls that calu holds back now.
 *
 * @param state The view state.
 * @return The `toolUseId`s of the held calls; empty while no decision is open.
 */
fun heldTools(state: ViewState): Set<String> {
    if (!isHolding(state) || transcriptEnded(state)) return emptySet()
    val waiting = state.escalations.values.filter { it.status == EscalationStatus.OPEN }.mapNotNull { it.toolUseId }.toSet()
    val running = state.agents.map { it.agentId }.toSet()
    return state.tools.values.filter { t ->
        t.check == null && t.result == null && t.stored == null && t.toolUseId !in waiting &&
            t.name != "AskUserQuestion" && !(isAgentLaunch(t.name) && t.toolUseId in running)
    }.map { it.toolUseId }.toSet()
}

/**
 * Count the held tool calls per tab.
 *
 * @param state The view state.
 * @return Per agent key (`MAIN` or a subagent's id) the number of its held calls; agents without
 *   a held call are left out.
 */
fun heldByAgent(state: ViewState): Map<String, Int> {
    val held = heldTools(state)
    return state.tools.values.filter { it.toolUseId in held }.groupingBy { it.agentKey }.eachCount()
}
