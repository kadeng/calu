/**
 * The pure reducer of the chat view: it applies one event to a `ViewState` and returns the new
 * state.
 *
 *     calu ide ──session.event──► applyEvent(state, ViewEvent(method, params)) ──► new state
 *     plugin (ui.* / bridge.status events of its own) ──────same function──────►
 *
 *     applyEvent
 *        ├─ bridge.status, ui.*      -> reduceUi        (ui.attached -> applyAttach, ui.history -> applyHistory; Attach.kt)
 *        ├─ session events           -> reduceSession   (session.state, assistant.*, user.input, tool.*, check.result,
 *        │                                               edit.diff, escalation.*, turn.result, agents.update, ...)
 *        └─ lastSeq := params.seq    (session events only, and only if it is larger)
 *
 * `applyEvent` never modifies its input state and never throws for any `JsonObject` as params:
 * the params are read only through the defensive readers of `io.github.kadeng.calu.json`.
 *
 * Port of `applyEvent` / `reduce` / `applyEvents` of `extensions/vscode/src/shared/viewState.ts`.
 */
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.isArr
import io.github.kadeng.calu.json.jsString
import io.github.kadeng.calu.json.jsTruthy
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.num
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.objOrEmpty
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonObject

/**
 * Apply one event to the view state.
 *
 * Unknown methods and events with missing required fields leave the state unchanged. The input
 * state is not modified. For any JSON object as `event.params` the function returns a state and
 * does not throw (malformed values are converted or ignored); it relies on the invariants of
 * `state` that the reducer creates itself.
 *
 * @param state The current state.
 * @param event A session event or a plugin event (see `ViewEvent`).
 * @return The new state (the same object if nothing changed).
 */
fun applyEvent(state: ViewState, event: ViewEvent): ViewState {
    val p = event.params
    val next = reduce(state, event.method, p)
    // Session events carry their sequence number; the plugin's own events have none.
    val seq = p.int("seq")
    return if (seq != null && seq > next.lastSeq && !event.method.startsWith("ui.")) next.copy(lastSeq = seq) else next
}

/**
 * Apply a sequence of events.
 *
 * @param state The start state (not modified).
 * @param events Events in arrival order.
 * @return The state after all events.
 */
fun applyEvents(state: ViewState, events: List<ViewEvent>): ViewState = events.fold(state, ::applyEvent)

/**
 * Body of `applyEvent` without the sequence number bookkeeping.
 *
 * @param state The current state (not modified).
 * @param method The event's method.
 * @param p The event's params (members untrusted).
 * @return The new state.
 */
private fun reduce(state: ViewState, method: String, p: JsonObject): ViewState =
    if (method == "bridge.status" || method.startsWith("ui.")) reduceUi(state, method, p) else reduceSession(state, method, p)

// ------------------------------------------------------------------ the plugin's own events

/**
 * Apply one of the plugin's own events (`bridge.status` and `ui.*`, see `ViewEvent`).
 *
 * @param state The current state (not modified).
 * @param method The event's method.
 * @param p The event's params (members untrusted).
 * @return The new state; the same object for an unknown method.
 */
private fun reduceUi(state: ViewState, method: String, p: JsonObject): ViewState = when (method) {
    "bridge.status" -> onBridgeStatus(state, p)
    "ui.link" -> onLink(state, p)
    "ui.attached" -> applyAttach(state, p)
    "ui.history" -> applyHistory(state, p)
    "ui.honestyStored" -> state.copy(honestyStored = nonEmptyStr(p, "mode"))
    "ui.starting" -> state.copy(starting = p.str("title")?.let { StartingView(title = it, slow = p.bool("slow")) })
    "ui.detached" -> {
        val next = finishStreaming(cancelOpenEscalations(state))
        next.copy(
            link = LinkView(LinkStatus.DETACHED),
            session = SessionView(workspace = next.session.workspace),
            sessions = next.sessions.copy(attachedId = null),
            queue = emptyList(),
        )
    }
    "ui.sessions" -> {
        val error = p.str("error")
        state.copy(
            sessions = SessionsView(
                list = if (error != null) state.sessions.list else parseSessions(p["running"]),
                attachedId = state.sessions.attachedId,
                error = error,
                loaded = true,
            ),
        )
    }
    "ui.serverHelp" -> {
        val command = p.str("command")
        val tool = p.str("tool")?.takeIf { it == "tmux" || it == "screen" || it == "plain" }
        if (command == null || tool == null) {
            state.copy(serverHelp = null)
        } else {
            state.copy(serverHelp = ServerHelp(command = command, tool = tool, detach = p.str("detach") ?: "", note = p.str("note") ?: ""))
        }
    }
    "ui.hints" -> state.copy(hints = p.arr("hints").mapNotNull { parseHint(it) })
    "ui.model" -> state.copy(
        session = state.session.copy(
            model = if ("model" in p) nonEmptyStr(p, "model") else state.session.model,
            effort = if ("effort" in p) nonEmptyStr(p, "effort") else state.session.effort,
        ),
    )
    "ui.usage" -> onUsage(state, p)
    "ui.usagePanel" -> onUsagePanel(state, p)
    "ui.notice" -> {
        val level = p.str("level") ?: "info"
        val text = textOrEmpty(p["text"])
        appendItem(state, MAIN) { id -> NoticeItem(id, level, text, nonEmptyStr(p, "key")) }
    }
    "ui.noticeResolved" -> nonEmptyStr(p, "key")?.let { resolveNotices(state, it) } ?: state
    "ui.sync" -> state.copy(
        sync = p.str("phase")?.let { phase ->
            SyncView(phase, p.str("text").orEmpty(), nonEmptyStr(p, "message"), p.strings("conflicts"), p.strings("held"), p.strings("notes"), p.int("queued") ?: 0)
        },
    )
    "ui.interpreter" -> state.copy(
        interpreter = p.str("phase")?.let { phase ->
            val host = nonEmptyStr(p, "host")
            val directory = nonEmptyStr(p, "directory")
            if (host == null || directory == null) null else InterpreterOfferView(host, directory, phase, nonEmptyStr(p, "message"))
        },
    )
    "ui.clear" -> clearTranscript(state)
    "ui.decisionPending" -> p.str("escalation_key")?.let { setPending(state, it, true, p.str("answer")) } ?: state
    "ui.decisionFailed" -> p.str("escalation_key")?.let { setPending(state, it, false) } ?: state
    "ui.sideAsk" -> {
        val id = p.int("id")
        val question = p.str("question")
        if (id == null || question == null) {
            state
        } else {
            state.copy(side = (state.side + SideExchangeView(id = id, question = question)).takeLast(MAX_SIDE_EXCHANGES))
        }
    }
    "ui.sideAnswer" -> {
        val id = p.int("id")
        val error = p.str("error")
        if (id == null) {
            state
        } else {
            state.copy(
                side = state.side.map { s ->
                    when {
                        s.id != id -> s
                        error != null -> s.copy(error = error)
                        else -> s.copy(answer = p.str("answer") ?: "")
                    }
                },
            )
        }
    }
    else -> state
}

/**
 * Reducer case `bridge.status`: the bridge process changed state.
 *
 * Any status other than "running" also detaches: nothing streams any more, no decision is
 * pending, the queue is empty and the session counts as stopped. The transcript stays readable,
 * and the open cards stay as they were (the steps still wait in the session).
 *
 * @param state The current state (not modified).
 * @param p The params `{status, message?, version?}` (untrusted; a `status` that is not a string
 *   is stored as "").
 * @return The new state. `bridge.version` is kept from the state if the event names none and the
 *   new status is "running".
 */
private fun onBridgeStatus(state: ViewState, p: JsonObject): ViewState {
    val status = p.str("status") ?: ""
    val version = p.str("version")
        ?: if (status == BridgeStatus.RUNNING) state.bridge.version?.takeIf { it.isNotEmpty() } else null
    var next = state.copy(bridge = BridgeView(status = status, message = p.str("message"), version = version))
    if (status != BridgeStatus.RUNNING) {
        next = finishStreaming(unpendEscalations(next))
        next = next.copy(
            starting = null,
            link = LinkView(LinkStatus.DETACHED),
            session = next.session.copy(state = "stopped"),
            sessions = next.sessions.copy(attachedId = null),
            queue = emptyList(),
        )
    }
    return next
}

/**
 * Reducer case `ui.link`: the bridge's connection to the session changed (`ide.link`).
 *
 * With "lost" the session can no longer be reached: nothing is streaming, and nothing can be
 * decided until it is attached again (the cards stay open, see `unpendEscalations`).
 *
 * @param state The current state (not modified).
 * The cause of a loss (`cause`, see `lostCause`) is kept in `link.cause`.
 *
 * @param p The params `{state: "attached" | "reconnecting" | "lost", message, cause?}` (untrusted).
 * @return The new state; unchanged for another `state` value.
 */
private fun onLink(state: ViewState, p: JsonObject): ViewState {
    val status = p.str("state")
        ?.takeIf { it == LinkStatus.ATTACHED || it == LinkStatus.RECONNECTING || it == LinkStatus.LOST }
        ?: return state
    val cause = if (status == LinkStatus.LOST) io.github.kadeng.calu.logic.lostCause(p["cause"], p["message"]) else null
    val next = state.copy(link = LinkView(status = status, message = nonEmptyStr(p, "message"), cause = cause))
    if (status != LinkStatus.LOST) return next
    return finishStreaming(unpendEscalations(next)).copy(
        sessions = next.sessions.copy(attachedId = null),
        queue = emptyList(),
    )
}

/**
 * Reducer case `ui.usage`: the result of `usage.get`.
 *
 * @param state The current state (not modified).
 * @param p The params `{context: {tokens, max_tokens, percent?, model?}, rate_limits, cost_usd,
 *   num_turns}` (untrusted).
 * @return The new state. `usage.context` is `null` unless `tokens` and `max_tokens` are whole
 *   numbers; a missing `percent` is computed (0 for `max_tokens` <= 0). Limits, cost and turn
 *   count keep their value if the event does not give them.
 */
private fun onUsage(state: ViewState, p: JsonObject): ViewState {
    val c = p.obj("context")
    val tokens = c?.int("tokens")
    val maxTokens = c?.int("max_tokens")
    val context = if (c != null && tokens != null && maxTokens != null) {
        ContextView(
            tokens = tokens,
            maxTokens = maxTokens,
            percent = c.num("percent") ?: if (maxTokens > 0) 100.0 * tokens / maxTokens else 0.0,
            model = c.str("model") ?: "",
        )
    } else {
        null
    }
    return state.copy(
        usage = UsageView(
            rateLimits = if (p["rate_limits"].isArr()) parseRateLimits(p["rate_limits"]) else state.usage.rateLimits,
            costUsd = p.num("cost_usd") ?: state.usage.costUsd,
            numTurns = p.int("num_turns") ?: state.usage.numTurns,
            context = context,
        ),
    )
}

/**
 * Reducer case `ui.usagePanel`: the usage panel's request was sent, answered or refused.
 *
 * @param state The current state (not modified).
 * @param p `{phase: "asking"}`, `{result: UsagePanelResult}` or `{error}` (untrusted).
 * @return The new state. An answer also updates `usage` (context when it gives one, cost, turns,
 *   and the limits when it lists some), so the chip shows the same numbers as the panel. Other params leave the
 *   state unchanged.
 */
private fun onUsagePanel(state: ViewState, p: JsonObject): ViewState {
    p.str("error")?.let { return state.copy(usagePanel = UsagePanelView("failed", error = it)) }
    val r = p.obj("result") ?: return if (p.str("phase") == "asking") state.copy(usagePanel = UsagePanelView("asking")) else state
    val merged = onUsage(state, JsonObject(buildMap {
        r["context"]?.let { put("context", it) }
        r["cost_usd"]?.let { put("cost_usd", it) }
        r["num_turns"]?.let { put("num_turns", it) }
        if (r["limits"].isArr() && parseRateLimits(r["limits"]).isNotEmpty()) put("rate_limits", r["limits"]!!)
    }))
    // An answer without a context leaves the chip's last known context as it was.
    val withUsage = if (r.obj("context") == null) merged.copy(usage = merged.usage.copy(context = state.usage.context)) else merged
    return withUsage.copy(
        usagePanel = UsagePanelView(
            phase = "ready",
            limits = parseRateLimits(r["limits"]),
            accountLimits = parseRateLimits(r["account_limits"]),
            accountLimitsProblem = r.str("account_limits_problem").orEmpty(),
            subscription = r.str("subscription").orEmpty(),
            claudeUsage = r.str("claude_usage").orEmpty(),
            claudeUsageProblem = r.str("claude_usage_problem").orEmpty(),
            costUsd = r.num("cost_usd"),
            numTurns = r.int("num_turns") ?: 0,
            context = withUsage.usage.context.takeIf { r.obj("context") != null },
        ),
    )
}

// ------------------------------------------------------------------ session events

/**
 * Apply a session event (a `method` of the protocol's `EventParams`).
 *
 * @param state The current state (not modified).
 * @param method The event's method.
 * @param p The event's params (members untrusted).
 * @return The new state; the same object for an unknown method or missing required members.
 */
private fun reduceSession(state: ViewState, method: String, p: JsonObject): ViewState = when (method) {
    "session.state" -> {
        val name = p.str("state")
        if (name == null) {
            state
        } else {
            val next = state.copy(
                session = state.session.copy(state = name, claudeSessionId = p.str("session_id") ?: state.session.claudeSessionId),
            )
            if (name == "stopped" || name == "failed") finishStreaming(cancelOpenEscalations(next)) else next
        }
    }
    "assistant.delta" -> {
        // A fragment of the in-progress text (or, with `thinking: true`, thinking) block of message
        // `message_id`. A message can have several blocks; a delta after a finalized block starts a
        // new item. Text and thinking are separate items.
        val text = p.str("text")
        val messageId = p.str("message_id")
        if (text == null || messageId == null) {
            state
        } else {
            val kind = if (p.bool("thinking")) ItemKind.THINKING else ItemKind.ASSISTANT
            appendFragment(state, agentKeyOf(p["agent_id"]), kind, messageId, text)
        }
    }
    "assistant.text", "assistant.thinking" -> {
        // A complete text or thinking block; it finalizes the latest streaming item of the same
        // message_id and kind, otherwise it is appended.
        val text = p.str("text")
        if (text == null) {
            state
        } else {
            val kind = if (method == "assistant.thinking") ItemKind.THINKING else ItemKind.ASSISTANT
            completeBlock(state, agentKeyOf(p["agent_id"]), kind, p.str("message_id"), text)
        }
    }
    "user.input" -> onUserInput(state, p)
    "tool.use" -> onToolUse(state, p)
    "check.result" -> onCheckResult(state, p)
    "tool.result" -> onToolResult(state, p)
    "chat.queue" -> {
        if (!p["messages"].isArr()) {
            state
        } else {
            val queue = p.arr("messages").mapNotNull { raw ->
                val m = raw.asObj() ?: return@mapNotNull null
                val id = m.str("id") ?: return@mapNotNull null
                val text = m.str("text") ?: return@mapNotNull null
                QueuedView(
                    id = id,
                    text = text,
                    delivery = if (m.str("delivery") == Delivery.INJECT) Delivery.INJECT else Delivery.QUEUED,
                    to = nonEmptyStr(m, "to") ?: MAIN,
                )
            }
            state.copy(queue = queue)
        }
    }
    "tool.files" -> {
        val toolUseId = p.str("tool_use_id")
        if (toolUseId == null || !p["files"].isArr()) {
            state
        } else {
            val files = p.arr("files").mapNotNull { raw ->
                val f = raw.asObj() ?: return@mapNotNull null
                val rawPath = nonEmptyStr(f, "raw") ?: return@mapNotNull null
                val path = nonEmptyStr(f, "path") ?: return@mapNotNull null
                ToolFile(raw = rawPath, path = path)
            }.take(MAX_TOOL_FILES)
            val next = ensureTool(state, toolUseId, agentKeyOf(p["agent_id"]), "?")
            updateTool(next, toolUseId) { old -> (old ?: ToolView(toolUseId, MAIN, "?")).copy(files = files) }
        }
    }
    "edit.diff" -> {
        val toolUseId = p.str("tool_use_id")
        if (toolUseId == null) {
            state
        } else {
            val next = ensureTool(state, toolUseId, agentKeyOf(p["agent_id"]), "Edit")
            val start = p.int("start_line")?.takeIf { it >= 1 }
            val end = p.int("end_line")
            val editRange = if (start != null && end != null && end >= start - 1) EditRange(start, end) else null
            val diffPath = p["path"].jsString()
            updateTool(next, toolUseId) { old ->
                (old ?: ToolView(toolUseId, MAIN, "Edit")).copy(diffPath = diffPath, editRange = editRange)
            }
        }
    }
    "escalation.request" -> addEscalation(state, p, true)
    "escalation.resolved" -> onEscalationResolved(state, p)
    "turn.result" -> onTurnResult(state, p)
    "agents.update" -> {
        if (!p["agents"].isArr()) {
            state
        } else {
            val agents = p.arr("agents").mapNotNull { parseAgent(it) }
            var items = state.items
            for (a in agents) if (own(items, a.agentId) == null) items = withEntry(items, a.agentId, emptyList())
            // Keep agents that the update does not list but whose tab already has content (e.g. a
            // placeholder created by an early event), so their tab does not disappear.
            val listed = agents.map { it.agentId }.toSet()
            val kept = state.agents.filter { it.agentId !in listed && !own(items, it.agentId).isNullOrEmpty() }
            state.copy(agents = agents + kept, items = items)
        }
    }
    "auto_escalate.changed" -> {
        val s = p.objOrEmpty("settings")
        state.copy(autoEscalate = AutoEscalateFlags(commands = s.bool("commands"), edits = s.bool("edits"), subagents = s.bool("subagents")))
    }
    "permission_mode.changed" -> p.str("mode")?.takeIf { it.isNotEmpty() }?.let { state.copy(permissionMode = it) } ?: state
    "honesty.update" -> state.copy(honesty = parseHonesty(p))
    "usage.update" ->
        if (p["rate_limits"].isArr()) state.copy(usage = state.usage.copy(rateLimits = parseRateLimits(p["rate_limits"]))) else state
    "log" -> {
        // A missing level counts as "info"; only warnings and errors are shown.
        val level = p.str("level")?.takeIf { it == "warning" || it == "error" }
        if (level == null) {
            state
        } else {
            val text = textOrEmpty(p["message"])
            appendItem(state, MAIN) { id -> NoticeItem(id, level, text) }
        }
    }
    else -> state
}

/**
 * Reducer case `user.input`: a message entered the conversation as user input.
 *
 * A message of the user for a background subagent (`to`) belongs to that subagent's tab; Main
 * only gets a "relay" item that says something was sent (the main agent relays it). For another
 * source than "user", `to` is ignored.
 *
 * @param state The current state (not modified).
 * @param p The params `{text, source?, images?, uuid?, to?}` (untrusted).
 * @return The new state; unchanged without a string `text`.
 */
private fun onUserInput(state: ViewState, p: JsonObject): ViewState {
    val text = p.str("text") ?: return state
    val source = p.str("source")?.takeIf { it == UserSource.CROSS_SESSION || it == UserSource.SYSTEM } ?: UserSource.USER
    val images = parseImages(p["images"])
    val turn = (state.turnBase ?: 0) + state.turnCount + 1
    val uuid = nonEmptyStr(p, "uuid")
    val to = if (source == UserSource.USER) agentKeyOf(p["to"]) else MAIN
    if (to == MAIN) {
        return appendItem(state, MAIN) { id -> UserItem(id = id, text = text, source = source, images = images, turn = turn, uuid = uuid) }
    }
    val inTab = appendItem(state, to) { id ->
        UserItem(id = id, text = text, source = source, images = images, turn = turn, uuid = uuid, to = to)
    }
    return appendItem(inTab, MAIN) { id -> RelayItem(id, to) }
}

/**
 * Reducer case `tool.use`: Claude calls a tool.
 *
 * Sets name and input of `tools[tool_use_id]` (keeping what earlier events stored there), appends
 * a "tool" item to the calling agent's list if the call is new, and records the launch of a
 * subagent for the tools "Agent" and "Task" (see `recordLaunch`).
 *
 * @param state The current state (not modified).
 * @param p The params `{tool_use_id, name, input, agent_id?}` (untrusted).
 * @return The new state; unchanged without a string `tool_use_id`.
 */
private fun onToolUse(state: ViewState, p: JsonObject): ViewState {
    val toolUseId = p.str("tool_use_id") ?: return state
    val key = agentKeyOf(p["agent_id"])
    val existed = own(state.tools, toolUseId) != null
    val name = p["name"].jsString()
    val input = boundInput(p["input"])
    val next = updateTool(state, toolUseId) { old ->
        (old ?: ToolView(toolUseId = toolUseId, agentKey = key, name = name)).copy(toolUseId = toolUseId, name = name, input = input)
    }
    val withLaunch = if (isAgentLaunch(p.str("name"))) recordLaunch(next, toolUseId, p.objOrEmpty("input")) else next
    return if (existed) withLaunch else appendItem(withLaunch, key) { id -> ToolItem(id, toolUseId) }
}

/**
 * Reducer case `check.result`: the rules or a checker decided on a tool call.
 *
 * Updates the tool call's decision and, for a checker remark or a DENY reason, appends a
 * "checker" message after the tool item in the list of the agent that made the call.
 *
 * @param state The current state (not modified).
 * @param p The params `{tool_use_id?, tool_name, route, decision, reason?, step_summary?,
 *   comment?, git_head?, agent_id?}` (untrusted; `route`, `decision` and `tool_name` of another
 *   type are converted to text).
 * @return The new state.
 */
private fun onCheckResult(state: ViewState, p: JsonObject): ViewState {
    val key = agentKeyOf(p["agent_id"])
    val route = p["route"].jsString()
    val decision = p["decision"].jsString()
    val checkerRoute = checkerRouteOf(route)
    val toolUseId = p.str("tool_use_id")
    var next = state
    if (toolUseId != null) {
        val toolName = p["tool_name"].jsString()
        next = ensureTool(next, toolUseId, key, toolName)
        next = updateTool(next, toolUseId) { old ->
            (old ?: ToolView(toolUseId, key, toolName)).copy(
                check = ToolCheck(
                    route = route,
                    decision = decision,
                    reason = p.str("reason") ?: "",
                    summary = p.str("step_summary") ?: "",
                    comment = p.str("comment") ?: "",
                    checkerRoute = checkerRoute,
                ),
                honesty = parseHonestyResult(p) ?: old?.honesty,
                gitHead = p.str("git_head") ?: old?.gitHead,
            )
        }
    }
    val message = checkerMessage(p) ?: return next
    return appendItem(next, key) { id ->
        CheckerItem(
            id = id,
            label = checkerLabel(route),
            checkerRoute = checkerRoute,
            decision = decision,
            text = message,
            toolUseId = toolUseId,
        )
    }
}

/**
 * Reducer case `tool.result`: a tool call returned.
 *
 * Stores the result (cut to `MAX_STORED_TEXT`) in `tools[tool_use_id]`, creating a placeholder
 * call named "?" if the call is unknown. The result of a call that launched a subagent is also
 * recorded as the subagent's result (see `recordAgentResult`).
 *
 * @param state The current state (not modified).
 * @param p The params `{tool_use_id, content, is_error?, agent_id?}` (untrusted; `content` of
 *   another type is converted to text, `is_error` counts as JavaScript's `Boolean(is_error)`).
 * @return The new state; unchanged without a string `tool_use_id`.
 */
private fun onToolResult(state: ViewState, p: JsonObject): ViewState {
    val toolUseId = p.str("tool_use_id") ?: return state
    val content = textOrEmpty(p["content"])
    val isError = p["is_error"].jsTruthy()
    var next = ensureTool(state, toolUseId, agentKeyOf(p["agent_id"]), "?")
    next = updateTool(next, toolUseId) { old ->
        (old ?: ToolView(toolUseId, MAIN, "?")).copy(result = ToolResultView(content = truncate(content), isError = isError))
    }
    val launched = isAgentLaunch(own(next.tools, toolUseId)?.name) || own(next.subagents, toolUseId) != null
    return if (launched) recordAgentResult(next, toolUseId, content, isError) else next
}

/**
 * Reducer case `escalation.resolved`: an escalated step was decided or cancelled.
 *
 * calu reports a question closed by an answer as "deny" (the AskUserQuestion call itself does
 * not run) with the text Claude receives as the note. The card shows it as answered: also after
 * a replay and when another client answered, where no `ui.decisionPending` of this view recorded
 * the answer.
 *
 * @param state The current state (not modified).
 * @param p The params `{escalation_id, decision: "allow" | "deny" | "cancelled", note?}`
 *   (untrusted; another `decision` counts as "cancelled").
 * @return The new state; unchanged if no open escalation has that id.
 */
private fun onEscalationResolved(state: ViewState, p: JsonObject): ViewState {
    val escalationId = p.str("escalation_id") ?: return state
    val old = findOpenEscalation(state, escalationId) ?: return state
    val decided = p.str("decision")
        ?.takeIf { it == EscalationStatus.ALLOW || it == EscalationStatus.DENY || it == EscalationStatus.CANCELLED }
        ?: EscalationStatus.CANCELLED
    val question = old.questions.isNotEmpty()
    val answered = decided != EscalationStatus.CANCELLED && (question || old.answer != null)
    val note = p.str("note")
    val answer = old.answer ?: if (answered && note != null) answerFromNote(note) else null
    return state.copy(
        escalations = withEntry(
            state.escalations,
            old.key,
            old.copy(
                status = if (answered) EscalationStatus.ANSWERED else decided,
                pending = false,
                answer = answer,
                note = if (answered && question) null else note,
            ),
        ),
    )
}

/**
 * Reducer case `turn.result`: a turn of the main agent ended.
 *
 * Finishes what still streams, appends a "turn" item to Main, increments `turnCount`, and takes
 * the number of background tasks, Claude's session id, the cost and the turn count from the
 * event (cost, session id and turn count keep their value if the event does not give them).
 * `cost_usd` is Claude Code's running total of the session; the item's `costDelta` is the
 * difference to `usage.costUsd` before the event (`null` if one of the two is unknown or the
 * total went down).
 *
 * @param state The current state (not modified).
 * @param p The params `{subtype?, is_error?, cost_usd?, background_tasks?, session_id?,
 *   num_turns?}` (untrusted).
 * @return The new state.
 */
private fun onTurnResult(state: ViewState, p: JsonObject): ViewState {
    val costUsd = p.num("cost_usd")
    val before = state.usage.costUsd
    val costDelta = if (costUsd != null && before != null && costUsd >= before) costUsd - before else null
    val backgroundTasks = p.int("background_tasks") ?: 0
    val subtype = p.str("subtype") ?: "success"
    val isError = p["is_error"].jsTruthy()
    val next = appendItem(finishStreaming(state), MAIN) { id ->
        TurnItem(id = id, subtype = subtype, isError = isError, costUsd = costUsd, backgroundTasks = backgroundTasks, costDelta = costDelta)
    }
    return next.copy(
        turnCount = next.turnCount + 1,
        session = next.session.copy(
            backgroundTasks = backgroundTasks,
            claudeSessionId = p.str("session_id") ?: next.session.claudeSessionId,
        ),
        usage = next.usage.copy(
            costUsd = costUsd ?: next.usage.costUsd,
            numTurns = p.int("num_turns") ?: next.usage.numTurns,
        ),
    )
}

/**
 * Mark the unresolved notices of the main list that carry a key as resolved.
 *
 * @param state The current state (not modified).
 * @param key The notices' key, e.g. "sync-conflict:src/a.py".
 * @return The new state; the same object if no notice matched.
 */
internal fun resolveNotices(state: ViewState, key: String): ViewState {
    val list = state.items[MAIN] ?: return state
    if (list.none { it is NoticeItem && it.key == key && !it.resolved }) return state
    val next = list.map { if (it is NoticeItem && it.key == key && !it.resolved) it.copy(resolved = true) else it }
    return state.copy(items = withEntry(state.items, MAIN, next))
}
