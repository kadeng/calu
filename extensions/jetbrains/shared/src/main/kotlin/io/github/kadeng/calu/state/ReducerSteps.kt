/**
 * Building blocks of the reducer (`Reducer.kt`): each function takes a `ViewState` and returns a
 * changed copy. None modifies its input, and none throws.
 *
 *     appendItem ──► items[key] += item (ids from nextId; cap MAX_ITEMS_PER_AGENT; placeholder agent)
 *     appendFragment / completeBlock / finishStreaming ──► streamed text and thinking items
 *     updateTool / ensureTool ──► tools[tool_use_id]
 *     recordLaunch / recordAgentResult ──► subagents[agent_id] and the "launch" item of its list
 *     addEscalation / setPending / unpendEscalations / cancelOpenEscalations ──► escalations[key]
 *     clearTranscript ──► empty lists
 *
 * Port of the non-exported helpers of `extensions/vscode/src/shared/viewState.ts`.
 */
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.jsString
import io.github.kadeng.calu.json.str
import kotlinx.serialization.json.JsonObject

/**
 * Make sure a subagent key has an entry in `state.agents`, so its tab exists even if an event of
 * the subagent (e.g. an escalation) arrives before the `agents.update` that lists it.
 *
 * @param state The current state (not modified).
 * @param key A list key; `MAIN` and known agents leave the state unchanged.
 * @return The state, with a placeholder agent `{agent_id: key, status: "running",
 *   description: "", background: false}` appended if the key was unknown. A later
 *   `agents.update` replaces the placeholder with the real entry.
 */
internal fun ensureAgent(state: ViewState, key: String): ViewState {
    if (key == MAIN || state.agents.any { it.agentId == key }) return state
    val placeholder = AgentInfo(agentId = key, description = "", background = false, status = "running")
    return state.copy(agents = state.agents + placeholder)
}

/**
 * Append an item to an agent's list (copy-on-write). Above `MAX_ITEMS_PER_AGENT` the oldest
 * items are dropped together with the tools and escalations that only they referenced; a
 * leading "launch" item (subagent instructions) is always kept as the first item. An unknown
 * subagent key gets a placeholder agent (see `ensureAgent`), so its tab exists.
 *
 * @param state The current state (not modified).
 * @param key The list to append to; created if missing.
 * @param make Builds the item from its id; it is called once with `state.nextId`.
 * @return A new state containing the item, with `nextId` incremented.
 */
internal fun appendItem(state: ViewState, key: String, make: (id: Int) -> Item): ViewState {
    val list = (own(state.items, key) ?: emptyList()) + make(state.nextId)
    var next = ensureAgent(state.copy(nextId = state.nextId + 1), key)
    if (list.size > MAX_ITEMS_PER_AGENT) {
        val head = if (list[0] is LaunchItem) listOf(list[0]) else emptyList()
        val body = list.drop(head.size)
        val keep = MAX_ITEMS_PER_AGENT - head.size
        next = dropReferenced(next, body.take(body.size - keep))
        return next.copy(items = withEntry(next.items, key, head + body.takeLast(keep)))
    }
    return next.copy(items = withEntry(next.items, key, list))
}

/**
 * Remove the tools and escalations referenced by dropped items.
 *
 * @param state The current state (not modified).
 * @param dropped Items that are removed from their list.
 * @return The state without those map entries.
 */
internal fun dropReferenced(state: ViewState, dropped: List<Item>): ViewState {
    val tools = dropped.filterIsInstance<ToolItem>().map { it.toolUseId }
    val escalations = dropped.filterIsInstance<EscalationItem>().map { it.escalationKey }
    return state.copy(
        tools = withoutEntries(state.tools, tools),
        escalations = withoutEntries(state.escalations, escalations),
    )
}

/**
 * Find the latest text or thinking item with a given message id in an agent's list. Text and
 * thinking items of the same message are separate items.
 *
 * @param state The current state.
 * @param key The agent's list.
 * @param messageId calu's message id.
 * @param kind `ItemKind.ASSISTANT` (text) or `ItemKind.THINKING`.
 * @return The index of the item in `items[key]`, or -1 if the list has no such item.
 */
internal fun findLatestStream(state: ViewState, key: String, messageId: String, kind: String): Int {
    val list = own(state.items, key) ?: return -1
    return list.indexOfLast { it is StreamItem && it.kind == kind && it.messageId == messageId }
}

/**
 * Create a text or thinking item.
 *
 * @param id The item's id.
 * @param kind `ItemKind.THINKING` for a thinking item; any other value gives a text item.
 * @param text The text.
 * @param messageId calu's message id, or `null`.
 * @param streaming The item's `streaming` flag (`null`: not set).
 * @return The new item.
 */
private fun streamItem(id: Int, kind: String, text: String, messageId: String?, streaming: Boolean?): StreamItem =
    if (kind == ItemKind.THINKING) {
        ThinkingItem(id = id, text = text, messageId = messageId, streaming = streaming)
    } else {
        AssistantItem(id = id, text = text, messageId = messageId, streaming = streaming)
    }

/**
 * Append a streamed fragment: extend the latest streaming item of the same message and kind
 * (also one that an attach cut off, see `StreamItem.cut`), or start a new streaming item (also
 * after a finalized block of the same message).
 *
 * @param state The current state (not modified).
 * @param key The agent's list.
 * @param kind `ItemKind.ASSISTANT` or `ItemKind.THINKING`.
 * @param messageId calu's message id.
 * @param text The fragment.
 * @return The new state.
 */
internal fun appendFragment(state: ViewState, key: String, kind: String, messageId: String, text: String): ViewState {
    val index = findLatestStream(state, key, messageId, kind)
    val found = own(state.items, key)?.getOrNull(index) as? StreamItem
    if (found == null || !(found.streaming == true || found.cut == true)) {
        return appendItem(state, key) { id -> streamItem(id, kind, text, messageId, streaming = true) }
    }
    return replaceItem(state, key, index, found.continued(found.text + text, streaming = true))
}

/**
 * Add a complete block: it finalizes the latest streaming item of the same message and kind,
 * otherwise it is appended.
 *
 * @param state The current state (not modified).
 * @param key The agent's list.
 * @param kind `ItemKind.ASSISTANT` or `ItemKind.THINKING`.
 * @param messageId calu's message id; `null` or "" if the event gave none.
 * @param text The complete text.
 * @return The new state.
 */
internal fun completeBlock(state: ViewState, key: String, kind: String, messageId: String?, text: String): ViewState {
    val id = messageId?.takeIf { it.isNotEmpty() }
    val index = if (id != null) findLatestStream(state, key, id, kind) else -1
    val found = own(state.items, key)?.getOrNull(index) as? StreamItem
    if (found != null && (found.streaming == true || found.cut == true)) {
        return replaceItem(state, key, index, found.continued(text, streaming = false))
    }
    return appendItem(state, key) { itemId -> streamItem(itemId, kind, text, id, streaming = null) }
}

/**
 * Return a copy of `state` in which `items[key][index]` is replaced by `item`.
 *
 * @param state The current state (not modified).
 * @param key The agent's list.
 * @param index Position of the item; outside `0 until items[key].size` the state is returned
 *   unchanged.
 * @param item The replacement (keeps its id).
 * @return The new state.
 */
internal fun replaceItem(state: ViewState, key: String, index: Int, item: Item): ViewState {
    val list = (own(state.items, key) ?: emptyList()).toMutableList()
    if (index !in list.indices) return state
    list[index] = item
    return state.copy(items = withEntry(state.items, key, list))
}

/**
 * Return a copy of `state` with `tools[id]` replaced by `update(old)`.
 *
 * @param state The current state (not modified).
 * @param id The tool_use_id (any string).
 * @param update Receives the existing ToolView or `null`; returns the new ToolView.
 * @return The new state.
 */
internal fun updateTool(state: ViewState, id: String, update: (old: ToolView?) -> ToolView): ViewState =
    state.copy(tools = withEntry(state.tools, id, update(own(state.tools, id))))

/**
 * Ensure a placeholder ToolView (and its list item) exists for a tool call whose `tool.use`
 * event was not seen yet (e.g. `edit.diff` came first, or the client attached mid-turn).
 *
 * @param state The current state (not modified).
 * @param id The tool_use_id.
 * @param agentKey The list where a new item is appended.
 * @param name The tool name to use for a new placeholder.
 * @return The state with `tools[id]` present.
 */
internal fun ensureTool(state: ViewState, id: String, agentKey: String, name: String): ViewState {
    if (own(state.tools, id) != null) return state
    val withTool = updateTool(state, id) { ToolView(toolUseId = id, agentKey = agentKey, name = name) }
    return appendItem(withTool, agentKey) { itemId -> ToolItem(itemId, id) }
}

/**
 * Record the launch of a subagent: its task in `subagents`, and a "launch" item (the
 * instructions) at the start of the subagent's list.
 *
 * The subagent's list key is the `tool_use_id` of the Agent/Task call. If the list already
 * starts with a launch item, it is replaced (keeping its id); otherwise the item is inserted
 * before any events of the subagent that arrived earlier. The prompt is stored in full. A
 * result already stored for the subagent is kept. The subagent's tab is created if it does
 * not exist yet.
 *
 * @param state The current state (not modified).
 * @param toolUseId The id of the Agent/Task tool call.
 * @param input The tool input (`description`, `prompt`, `subagent_type`, `run_in_background`;
 *   untrusted).
 * @return The new state.
 */
internal fun recordLaunch(state: ViewState, toolUseId: String, input: JsonObject): ViewState {
    val list = own(state.items, toolUseId) ?: emptyList()
    val existing = list.firstOrNull() as? LaunchItem
    val description = input.str("description") ?: ""
    val subagentType = input.str("subagent_type")
    val background = input.bool("run_in_background")
    val prompt = input.str("prompt") ?: ""
    val launch = LaunchItem(
        id = existing?.id ?: state.nextId,
        toolUseId = toolUseId,
        description = description,
        subagentType = subagentType,
        background = background,
        prompt = prompt,
    )
    val rest = if (existing != null) list.drop(1) else list
    val previous = own(state.subagents, toolUseId)
    val subagent = SubagentView(
        agentId = toolUseId,
        description = description,
        subagentType = subagentType,
        background = background,
        task = prompt,
        result = previous?.result?.copy(final = !background),
    )
    return ensureAgent(state, toolUseId).copy(
        subagents = withEntry(state.subagents, toolUseId, subagent),
        items = withEntry(state.items, toolUseId, listOf<Item>(launch) + rest),
        nextId = if (existing != null) state.nextId else state.nextId + 1,
    )
}

/**
 * Record what the launching call of a subagent returned.
 *
 * @param state The current state (not modified).
 * @param toolUseId The id of the Agent/Task tool call.
 * @param content The result text (cut to `MAX_AGENT_RESULT_TEXT`).
 * @param isError Whether the call failed.
 * @return The new state; `subagents[toolUseId]` exists afterwards (with an empty task if the
 *   launch was not seen).
 */
internal fun recordAgentResult(state: ViewState, toolUseId: String, content: String, isError: Boolean): ViewState {
    val previous = own(state.subagents, toolUseId) ?: SubagentView(
        agentId = toolUseId,
        description = "",
        subagentType = null,
        background = state.agents.firstOrNull { it.agentId == toolUseId }?.background == true,
        task = "",
    )
    val result = SubagentResult(content = truncate(content, MAX_AGENT_RESULT_TEXT), isError = isError, final = !previous.background)
    return state.copy(subagents = withEntry(state.subagents, toolUseId, previous.copy(result = result)))
}

/**
 * Clear the `pending` flag of every open escalation (used when the bridge or the connection to
 * the session is lost). The cards stay open: the steps still wait in the session, and a
 * decision that was on its way is not known to have arrived. The view shows them as "connection
 * lost" while `isAttached` is false; the next attach says what is open then.
 *
 * @param state The current state (not modified).
 * @return The new state; the same object if no open escalation was pending.
 */
internal fun unpendEscalations(state: ViewState): ViewState {
    if (state.escalations.values.none { it.status == EscalationStatus.OPEN && it.pending == true }) return state
    val escalations = state.escalations.mapValues { (_, e) ->
        if (e.status == EscalationStatus.OPEN && e.pending == true) e.copy(pending = false) else e
    }
    return state.copy(escalations = escalations)
}

/**
 * Close all open escalations with status "cancelled" (used when the session ends or is left).
 *
 * @param state The current state (not modified).
 * @return The new state; the same object if no escalation was open.
 */
internal fun cancelOpenEscalations(state: ViewState): ViewState {
    if (state.escalations.values.none { it.status == EscalationStatus.OPEN }) return state
    val escalations = state.escalations.mapValues { (_, e) ->
        if (e.status == EscalationStatus.OPEN) e.copy(status = EscalationStatus.CANCELLED, pending = false) else e
    }
    return state.copy(escalations = escalations)
}

/**
 * Mark every streaming text or thinking item as finished (used at turn end, session stop,
 * bridge exit, attach).
 *
 * @param state The current state (not modified).
 * @param cut True at an attach: the items are marked `cut`, so that the fragments and the final
 *   text that arrive after the replay continue them instead of starting a second item.
 * @return The new state; the same object if nothing was streaming.
 */
internal fun finishStreaming(state: ViewState, cut: Boolean = false): ViewState {
    val streaming = { i: Item -> i is StreamItem && i.streaming == true }
    if (state.items.values.none { list -> list.any(streaming) }) return state
    val items = state.items.mapValues { (_, list) ->
        if (list.any(streaming)) list.map { i -> if (i is StreamItem && i.streaming == true) i.finished(cut) else i } else list
    }
    return state.copy(items = items)
}

/**
 * Find the open escalation with one of calu's escalation ids.
 *
 * @param state The current state.
 * @param escalationId calu's id.
 * @return The latest open escalation with that id (in the insertion order of the map), or `null`.
 */
internal fun findOpenEscalation(state: ViewState, escalationId: String): EscalationView? =
    state.escalations.values.lastOrNull { it.escalationId == escalationId && it.status == EscalationStatus.OPEN }

/**
 * Set or clear the `pending` flag of an open escalation.
 *
 * @param state The current state (not modified).
 * @param key The escalation's state key.
 * @param pending The new flag.
 * @param answer With `pending` true: the answer text the user sent, if it was an answer. With
 *   `pending` false the stored answer is removed.
 * @return The new state; unchanged if the escalation is unknown or not open.
 */
internal fun setPending(state: ViewState, key: String, pending: Boolean, answer: String? = null): ViewState {
    val esc = own(state.escalations, key)
    if (esc == null || esc.status != EscalationStatus.OPEN) return state
    val next = when {
        !pending -> esc.copy(pending = false, answer = null)
        answer != null -> esc.copy(pending = true, answer = answer)
        else -> esc.copy(pending = true)
    }
    return state.copy(escalations = withEntry(state.escalations, key, next))
}

/**
 * Drop the transcript: messages, tool calls, escalations, subagents, agent tabs, queue, side
 * questions and the focus, and set the turn counter back to 0.
 *
 * @param state The current state (not modified).
 * @return The state with empty lists; bridge, link, session, sessions, hints and settings-like
 *   parts are kept.
 */
internal fun clearTranscript(state: ViewState): ViewState =
    state.copy(
        items = mapOf(MAIN to emptyList()),
        tools = emptyMap(),
        escalations = emptyMap(),
        subagents = emptyMap(),
        agents = emptyList(),
        queue = emptyList(),
        side = emptyList(),
        focus = null,
        turnCount = 0,
    )

/**
 * Add an escalation card (reducer case `escalation.request`).
 *
 * Always a new card; an id that is already in the state gets a fresh state key, so a closed
 * card is never reopened or overwritten.
 *
 * @param state The current state (not modified).
 * @param e The event params (untrusted).
 * @param focus True to request a switch to the card's tab.
 * @return The new state; unchanged without a string `escalation_id`.
 */
internal fun addEscalation(state: ViewState, e: JsonObject, focus: Boolean): ViewState {
    val escalationId = e.str("escalation_id") ?: return state
    val key = agentKeyOf(e["agent_id"])
    val escKey = if (own(state.escalations, escalationId) != null) "$escalationId#${state.nextId}" else escalationId
    val view = EscalationView(
        key = escKey,
        escalationId = escalationId,
        toolUseId = e.str("tool_use_id"),
        agentKey = key,
        toolName = e["tool_name"].jsString(),
        toolInput = boundInput(e["tool_input"]),
        reason = e.str("reason") ?: "",
        rendered = e.str("rendered")?.let { truncate(it, 4 * MAX_STORED_TEXT) } ?: "",
        questions = parseQuestions(e["questions"]),
        openedAt = e.str("opened_at"),
        status = EscalationStatus.OPEN,
        suggestedRule = e.str("suggested_rule")?.take(1000) ?: "",
    )
    val next = state.copy(escalations = withEntry(state.escalations, escKey, view))
    val withCard = appendItem(next, key) { id -> EscalationItem(id, escKey) }
    return if (focus) withCard.copy(focus = FocusRequest(tab = key, seq = (state.focus?.seq ?: 0) + 1)) else withCard
}
