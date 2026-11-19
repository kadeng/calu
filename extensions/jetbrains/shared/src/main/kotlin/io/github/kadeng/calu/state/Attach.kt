/**
 * The two reducer cases that rebuild the transcript from data of calu instead of from one live
 * event: `ui.attached` (`applyAttach`) and `ui.history` (`applyHistory`).
 *
 *     ide.attach / ide.start result ──ui.attached──► applyAttach:  replay events, then apply the snapshot
 *     session.history result ────────ui.history───► applyHistory: recorded turns above the transcript
 *
 *     Main after both:  divider "Earlier in this conversation"
 *                       user · tool · tool · assistant · turn      (per recorded turn, `stored`)
 *                       divider "From calu's record ..."
 *                       [replayed and live events of this session ...]
 *
 * Neither function modifies its input state or throws for any `JsonObject`.
 *
 * Port of `applyAttach` / `applyHistory` of `extensions/vscode/src/shared/viewState.ts`.
 */
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.isArr
import io.github.kadeng.calu.json.objOrEmpty
import io.github.kadeng.calu.json.str
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

/** Methods of recorded events that describe the live session and are taken from the snapshot. */
private val NOT_REPLAYED: Set<String> =
    setOf("session.state", "log", "chat.queue", "auto_escalate.changed", "permission_mode.changed", "honesty.update", "usage.update")

/**
 * Rebuild the state from the result of `ide.attach` / `ide.start` (reducer case `ui.attached`).
 *
 *     1. drop the transcript of the session shown before -- unless `resumed` is true: the
 *        plugin attached again to the session this state shows, asked only for the events after
 *        `lastSeq` and got them without a gap (after a restart of the bridge); the transcript
 *        then stays and the events continue it
 *     2. replay `events` (the recorded events of the session, oldest first)  -> transcript
 *     3. apply `snapshot` (authoritative for what is true now):
 *          session state, model, effort, agents, queue, auto-escalation, honesty, limits;
 *          escalations: one that the replay left open but the snapshot does not list is
 *          closed as "cancelled"; one that the snapshot lists but the replay did not produce
 *          is added
 *
 * Postconditions: `link.status` is "attached"; `session.sessionId` and `sessions.attachedId`
 * are `session_id`; exactly the snapshot's escalations are open; nothing is streaming;
 * `serverHelp` is cleared; `focus` is the value before the call, or a request for the tab of
 * the first open escalation if there is one.
 *
 * @param state The current state (not modified).
 * @param p The params of `ui.attached`: `session_id`, `initialize`, `snapshot`, `events`,
 *   `resumed` (untrusted; missing members are treated as empty). `resumed` is honoured only if
 *   `session_id` is the session the state shows.
 * @return The rebuilt state; unchanged without a string `session_id`.
 */
fun applyAttach(state: ViewState, p: JsonObject): ViewState {
    val sessionId = p.str("session_id") ?: return state
    val snapshot = p.objOrEmpty("snapshot")
    val init = p.objOrEmpty("initialize")

    val resumed = p.bool("resumed") && state.session.sessionId == sessionId
    // Cost, model calls and context belong to the session shown before; the limits are the
    // account's and stay. The replayed turns and the next `ui.usage` bring this session's numbers.
    val fresh = state.usage.copy(costUsd = null, numTurns = 0, context = null)
    var next = if (resumed) unpendEscalations(state) else clearTranscript(state).copy(lastSeq = 0, turnBase = null, usage = fresh)
    for (raw in p.arr("events")) {
        val e = raw.asObj() ?: continue
        val method = e.str("method") ?: continue
        if (method in NOT_REPLAYED || method.startsWith("ui.") || method == "bridge.status") continue
        next = applyEvent(next, ViewEvent(method, e.objOrEmpty("params")))
    }

    // Escalations: the snapshot says which are open now.
    val open = snapshot.arr("escalations").mapNotNull { it.asObj() }.filter { it.str("escalation_id") != null }
    val openIds = open.mapNotNull { it.str("escalation_id") }.toSet()
    next = next.copy(
        escalations = next.escalations.mapValues { (_, esc) ->
            if (esc.status == EscalationStatus.OPEN && esc.escalationId !in openIds) {
                esc.copy(status = EscalationStatus.CANCELLED, pending = false)
            } else {
                esc
            }
        },
    )
    for (e in open) {
        val id = e.str("escalation_id") ?: continue
        if (findOpenEscalation(next, id) == null) next = addEscalation(next, e, false)
    }
    val firstOpen = next.escalations.values.firstOrNull { it.status == EscalationStatus.OPEN }

    if (snapshot["agents"].isArr()) {
        next = applyEvent(next, ViewEvent("agents.update", JsonObject(mapOf("agents" to JsonArray(snapshot.arr("agents"))))))
    }
    next = applyEvent(next, ViewEvent("chat.queue", JsonObject(mapOf("messages" to JsonArray(snapshot.arr("queue"))))))
    next = applyEvent(next, ViewEvent("auto_escalate.changed", JsonObject(mapOf("settings" to snapshot.objOrEmpty("auto_escalate")))))
    next = next.copy(permissionMode = snapshot.str("permission_mode")?.takeIf { it.isNotEmpty() })

    val snapshotSeq = snapshot.int("last_seq") ?: 0
    next = finishStreaming(next, cut = true)
    return next.copy(
        link = LinkView(LinkStatus.ATTACHED),
        session = SessionView(
            state = snapshot.str("state") ?: "idle",
            sessionId = sessionId,
            claudeSessionId = nonEmptyStr(snapshot, "claude_session_id") ?: next.session.claudeSessionId,
            workspace = nonEmptyStr(snapshot, "workspace") ?: nonEmptyStr(init, "workspace"),
            project = nonEmptyStr(snapshot, "project") ?: nonEmptyStr(init, "project"),
            model = nonEmptyStr(snapshot, "model"),
            effort = nonEmptyStr(snapshot, "effort"),
            backgroundTasks = 0,
        ),
        sessions = next.sessions.copy(attachedId = sessionId),
        serverHelp = null,
        starting = null,
        honesty = parseHonesty(snapshot["honesty"]),
        honestyStored = if (resumed) state.honestyStored else null,
        usage = next.usage.copy(
            rateLimits = parseRateLimits(snapshot["rate_limits"]),
            context = if (resumed) next.usage.context else null,
        ),
        focus = if (firstOpen != null) FocusRequest(tab = firstOpen.agentKey, seq = (state.focus?.seq ?: 0) + 1) else state.focus,
        lastSeq = maxOf(next.lastSeq, snapshotSeq),
    )
}

/**
 * Put the recorded turns of the conversation above the transcript (reducer case `ui.history`).
 *
 * The events of an attach only reach back to the start of the session's process (or less, once
 * the replay buffer moved on). A resumed, forked or rewound conversation has earlier turns; the
 * plugin asks `session.history` after the attach and sends the result here.
 *
 *     Main before:  [events of this session ...]
 *     Main after:   divider "Earlier in this conversation"
 *                   user · tool · tool · assistant · turn      (per recorded turn, `stored`)
 *                   divider "From calu's record ..."
 *                   [events of this session ...]
 *
 * Which turns are added is decided by `historyEntries` (those the replayed events do not
 * cover). A recorded user message keeps the conversation's turn number and the prompt's uuid,
 * so "Rewind" and "Fork from here" work on it. A recorded step is a tool row with the decision
 * and the checker's summary; its input and the start of its result are shown if the record
 * holds them. Steps of subagents are rows of Main (the record names no tab).
 *
 * Effect on the numbering: `turnBase` becomes the number of turns before the first replayed
 * one, and the user messages of the replayed events are renumbered to the conversation's turn
 * numbers.
 *
 * @param state The current state (not modified).
 * @param p The params: `session_id` (the session the history was asked from) and `history`
 *   (the result of `session.history`, untrusted).
 * @return The new state; unchanged if another session is shown now or if the history was
 *   already applied (`turnBase` is set). If the record holds nothing the transcript lacks,
 *   only the numbering changes.
 */
fun applyHistory(state: ViewState, p: JsonObject): ViewState {
    val sessionId = p.str("session_id")
    if (sessionId == null || sessionId != state.session.sessionId) return state
    if (state.turnBase != null) return state
    val main = own(state.items, MAIN) ?: emptyList()
    val users = main.filterIsInstance<UserItem>()
    val relayed = state.items.filterKeys { it != MAIN }.values.flatMap { it.filterIsInstance<UserItem>() }
    val history = historyEntries(
        p["history"],
        Covered(
            uuids = (users + relayed).mapNotNull { it.uuid }.toSet(),
            inputs = users.filter { it.source == UserSource.USER }.map { it.text.trim() }.toSet(),
            toolUseIds = state.tools.keys.toSet(),
        ),
    )
    // Number the replayed turns as the conversation does.
    val base = maxOf(0, history.firstReplayedTurn - 1)
    var numbered = state.copy(turnBase = base)
    if (base > 0) {
        var renumbered = state.items
        for ((key, list) in state.items) {
            if (list.none { it is UserItem }) continue
            renumbered = withEntry(renumbered, key, list.map { i -> if (i is UserItem) i.copy(turn = i.turn + base) else i })
        }
        numbered = numbered.copy(items = renumbered)
    }
    if (history.entries.isEmpty()) return numbered
    val live = own(numbered.items, MAIN) ?: emptyList()

    var nextId = numbered.nextId
    var tools = numbered.tools
    val omitted = if (history.omittedTurns > 0) {
        " (${history.omittedTurns} older turn${if (history.omittedTurns == 1) " is" else "s are"} not shown)"
    } else {
        ""
    }
    val items = mutableListOf<Item>(DividerItem(id = nextId++, mark = DividerMark.HISTORY_START, text = "$HISTORY_START_TEXT$omitted"))
    for (entry in history.entries) {
        val id = nextId++
        items += historyItem(entry, id)
        if (entry !is HistoryEntry.Step) continue
        val toolUseId = historyToolId(entry, id)
        tools = withEntry(
            tools,
            toolUseId,
            ToolView(
                toolUseId = toolUseId,
                agentKey = MAIN,
                name = entry.name,
                input = boundInput(entry.input),
                check = if (!entry.decision.isNullOrEmpty()) {
                    ToolCheck(
                        route = "stored",
                        decision = entry.decision,
                        reason = truncate(entry.reason),
                        summary = truncate(entry.summary),
                        comment = "",
                        checkerRoute = "",
                    )
                } else {
                    null
                },
                result = entry.result?.let { ToolResultView(content = truncate(it), isError = false) },
                stored = StoredStepInfo(subagent = entry.subagent, hasInput = entry.input != null),
            ),
        )
    }
    items += DividerItem(id = nextId++, mark = DividerMark.HISTORY_END, text = HISTORY_END_TEXT)
    return numbered.copy(nextId = nextId, tools = tools, items = withEntry(numbered.items, MAIN, items + live))
}

/**
 * Return the key in `ViewState.tools` of a recorded step.
 *
 * @param entry The step.
 * @param id The id of its item.
 * @return The call's own id if the record holds it, else "history:<item id>" (unique in the
 *   state, and never the id of a real call: those do not contain a colon after "history").
 */
private fun historyToolId(entry: HistoryEntry.Step, id: Int): String = entry.toolUseId ?: "history:$id"

/**
 * Build the transcript item of one recorded entry.
 *
 * @param entry The entry.
 * @param id The item's id.
 * @return The item, marked `stored` (a tool item refers to the `ToolView` of `historyToolId`).
 */
private fun historyItem(entry: HistoryEntry, id: Int): Item = when (entry) {
    is HistoryEntry.User -> UserItem(
        id = id,
        text = truncate(entry.text, MAX_AGENT_RESULT_TEXT),
        source = UserSource.USER,
        images = emptyList(),
        turn = entry.turn,
        uuid = entry.uuid,
        stored = true,
    )
    is HistoryEntry.Step -> ToolItem(id = id, toolUseId = historyToolId(entry, id))
    is HistoryEntry.Answer -> AssistantItem(id = id, text = truncate(entry.text, MAX_AGENT_RESULT_TEXT), stored = true)
    HistoryEntry.TurnEnd -> TurnItem(id = id, subtype = "success", isError = false, costUsd = null, backgroundTasks = 0, stored = true)
}
