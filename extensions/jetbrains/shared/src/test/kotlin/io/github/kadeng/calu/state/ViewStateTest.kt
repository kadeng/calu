// The chat view's reducer: session events, the plugin's own events, malformed input.
// Port of extensions/vscode/test/unit/viewState.test.ts (same cases, same order).
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import kotlinx.serialization.json.JsonArray
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** `applyEvent`: the general cases. */
class ApplyEventTest {
    @Test
    fun `does not mutate the input state`() {
        val s0 = initialViewState()
        val frozen = CaluJson.encodeToString(ViewState.serializer(), s0)
        applyEvents(s0, listOf(ev("assistant.text", "text" to "hi"), ev("session.state", "state" to "idle")))
        assertEquals(frozen, CaluJson.encodeToString(ViewState.serializer(), s0))
    }

    @Test
    fun `ignores unknown and malformed events`() {
        val s0 = initialViewState()
        assertSame(s0, applyEvent(s0, ev("nope")))
        assertSame(s0, applyEvent(s0, ev("assistant.text", "text" to 3)))
        assertSame(s0, applyEvent(s0, ev("tool.use")))
        assertSame(s0, applyEvent(s0, ev("session.state")))
        assertSame(s0, applyEvent(s0, ev("escalation.resolved", "escalation_id" to "x", "decision" to "allow")))
        // The TypeScript test passes `params: null`. `ViewEvent.params` cannot be null here; the
        // caller turns params that are not an object into the empty object.
        assertSame(s0, applyEvent(s0, ViewEvent("tool.use", EMPTY_OBJECT)))
    }

    @Test
    fun `tracks bridge status, session state, sequence numbers and messages`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running", "version" to "0.9"),
                ev("session.state", "state" to "idle", "session_id" to "C1", "seq" to 3),
                ev("user.input", "text" to "hello", "seq" to 4),
                ev("assistant.text", "agent_id" to null, "text" to "Hi **there**", "seq" to 5),
                ev("turn.result", "subtype" to "success", "cost_usd" to 0.01, "num_turns" to 2, "background_tasks" to 1, "seq" to 6),
                ev("ui.notice", "level" to "info", "text" to "x", "seq" to 99),
            ),
        )
        assertEquals(BridgeView(status = "running", version = "0.9"), s.bridge)
        assertEquals("idle", s.session.state)
        assertEquals("C1", s.session.claudeSessionId)
        assertEquals(1, s.session.backgroundTasks)
        assertEquals(0.01, s.usage.costUsd)
        assertEquals(2, s.usage.numTurns)
        assertEquals(6, s.lastSeq)
        assertEquals(listOf("user", "assistant", "turn", "notice"), kinds(s))
        val turn = list(s)[2] as TurnItem
        assertEquals(1, turn.backgroundTasks)
        assertEquals(0.01, turn.costUsd)
        assertEquals(4, list(s).map { it.id }.toSet().size)
    }

    @Test
    fun `computes a turn's own cost from the session totals that Claude Code reports`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("turn.result", "subtype" to "success", "cost_usd" to 0.0027, "num_turns" to 3),
                ev("turn.result", "subtype" to "success", "cost_usd" to 0.004, "num_turns" to 5),
                ev("turn.result", "subtype" to "success", "num_turns" to 1),
                // A restarted Claude Code counts from zero: the difference is not this turn's cost.
                ev("turn.result", "subtype" to "success", "cost_usd" to 0.001, "num_turns" to 1),
            ),
        )
        val turns = list(s).filterIsInstance<TurnItem>()
        assertEquals(0.0027, turns[0].costUsd)
        assertNull(turns[0].costDelta)
        assertEquals(0.004, turns[1].costUsd)
        assertEquals(0.0013, turns[1].costDelta!!, 1e-6)
        assertNull(turns[2].costUsd)
        assertNull(turns[2].costDelta)
        assertEquals(0.001, turns[3].costUsd)
        assertNull(turns[3].costDelta)
        assertEquals(0.001, s.usage.costUsd)
        assertEquals(1, s.usage.numTurns)

        // Another session starts without the cost and the context of the one shown before: the
        // first turn of a fork showed the difference to the old session's total as its own cost.
        val withContext = applyEvent(
            s,
            ev("ui.usage", "context" to mapOf("tokens" to 20000, "max_tokens" to 1000000), "rate_limits" to emptyList<Any>(), "cost_usd" to 0.0041, "num_turns" to 1),
        )
        assertEquals(20000, withContext.usage.context?.tokens)
        val other = applyEvent(
            withContext,
            ev("ui.attached", "session_id" to "s2", "snapshot" to mapOf("state" to "idle", "workspace" to "/w"), "events" to emptyList<Any>()),
        )
        assertNull(other.usage.costUsd)
        assertEquals(0, other.usage.numTurns)
        assertNull(other.usage.context)
        val first = applyEvent(other, ev("turn.result", "subtype" to "success", "cost_usd" to 0.0044, "num_turns" to 1))
        val turn = list(first).filterIsInstance<TurnItem>()[0]
        assertEquals(0.0044, turn.costUsd)
        assertNull(turn.costDelta)
    }

    @Test
    fun `merges tool use, check result, edit diff and tool result by tool_use_id`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "t1", "name" to "Edit", "input" to jo("file_path" to "a.py")),
                ev("edit.diff", "tool_use_id" to "t1", "path" to "a.py", "before" to "a", "after" to "b"),
                ev("check.result", "tool_use_id" to "t1", "tool_name" to "Edit", "route" to "edit", "decision" to "PASS", "reason" to "", "step_summary" to "Edits a.py"),
                ev("tool.result", "tool_use_id" to "t1", "content" to "ok", "is_error" to false),
            ),
        )
        assertEquals(1, list(s).size)
        assertEquals(
            ToolView(
                toolUseId = "t1",
                agentKey = MAIN,
                name = "Edit",
                input = jo("file_path" to "a.py"),
                diffPath = "a.py",
                editRange = null,
                gitHead = null,
                check = ToolCheck(route = "edit", decision = "PASS", reason = "", summary = "Edits a.py", comment = "", checkerRoute = "edit"),
                result = ToolResultView(content = "ok", isError = false),
            ),
            s.tools["t1"],
        )
    }

    @Test
    fun `creates a placeholder when check result comes first`() {
        val s = applyEvent(initialViewState(), ev("check.result", "tool_use_id" to "t9", "tool_name" to "Bash", "route" to "command", "decision" to "DENY"))
        assertEquals("Bash", s.tools["t9"]?.name)
        assertEquals(listOf<Item>(ToolItem(id = 1, toolUseId = "t9")), list(s))
    }

    @Test
    fun `routes subagent events into per-agent lists`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("agents.update", "agents" to listOf(jo("agent_id" to "A1", "description" to "worker", "background" to true, "status" to "running"))),
                ev("assistant.text", "agent_id" to "A1", "text" to "sub says"),
                ev("tool.use", "agent_id" to "A1", "tool_use_id" to "t2", "name" to "Bash", "input" to jo("command" to "ls")),
            ),
        )
        assertEquals(listOf("assistant", "tool"), kinds(s, "A1"))
        assertEquals(emptyList<Item>(), list(s))
        assertEquals(MessageTarget(to = "main", label = "Claude (main agent)"), messageTarget(s, MAIN))
        assertEquals(MessageTarget(to = "A1", label = "subagent \"worker\""), messageTarget(s, "A1"))
        assertEquals("main", messageTarget(s, "no-such-tab").to)
        val done = applyEvent(s, ev("agents.update", "agents" to listOf(jo("agent_id" to "A1", "background" to true, "status" to "completed"))))
        val finished = messageTarget(done, "A1")
        assertNull(finished.to)
        assertTrue(finished.reason.orEmpty().contains("has finished"))
        val fg = applyEvent(s, ev("agents.update", "agents" to listOf(jo("agent_id" to "A1", "background" to false, "status" to "running"))))
        assertNull(messageTarget(fg, "A1").to)
        // The tab of a finished subagent stays readable.
        assertEquals(2, list(done, "A1").size)
    }

    @Test
    fun `shows only warning and error logs, and ui notices`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("log", "level" to "info", "message" to "quiet"),
                ev("log", "level" to "error", "message" to "loud"),
                ev("ui.notice", "level" to "error", "text" to "request failed"),
            ),
        )
        assertEquals(
            listOf<Item>(NoticeItem(id = 1, level = "error", text = "loud"), NoticeItem(id = 2, level = "error", text = "request failed")),
            list(s),
        )
    }

    @Test
    fun `marks a resolved notice and keeps it`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("ui.notice", "level" to "warning", "text" to "conflict in a.py", "key" to "sync-conflict:a.py"),
                ev("ui.notice", "level" to "warning", "text" to "conflict in b.py", "key" to "sync-conflict:b.py"),
                ev("ui.noticeResolved", "key" to "sync-conflict:a.py"),
                ev("ui.noticeResolved", "key" to "sync-conflict:unknown"),
            ),
        )
        assertEquals(
            listOf<Item>(
                NoticeItem(id = 1, level = "warning", text = "conflict in a.py", key = "sync-conflict:a.py", resolved = true),
                NoticeItem(id = 2, level = "warning", text = "conflict in b.py", key = "sync-conflict:b.py"),
            ),
            list(s),
        )
        val rows = io.github.kadeng.calu.chat.rowsOf(s, MAIN)
        assertEquals("resolved", rows[0].chip)
        assertEquals("", rows[0].chipKind)
        assertEquals("warning", rows[1].chipKind)
        // A second conflict of the same path gets a new line; the old one stays resolved.
        val again = applyEvents(s, listOf(ev("ui.notice", "level" to "warning", "text" to "again", "key" to "sync-conflict:a.py")))
        assertEquals(false, (list(again).last() as NoticeItem).resolved)
    }

    @Test
    fun `caps list length`() {
        val events = List(MAX_ITEMS_PER_AGENT + 5) { i -> ev("assistant.text", "text" to "m$i") }
        val s = applyEvents(initialViewState(), events)
        assertEquals(MAX_ITEMS_PER_AGENT, list(s).size)
        assertEquals("m5", (list(s)[0] as AssistantItem).text)
    }

    @Test
    fun `ui clear drops the transcript but keeps the session`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("session.state", "state" to "idle"),
                ev("assistant.text", "text" to "x"),
                ev("ui.sideAsk", "id" to 1, "question" to "q"),
                ev("ui.clear"),
            ),
        )
        assertEquals(mapOf(MAIN to emptyList<Item>()), s.items)
        assertEquals(emptyList<SideExchangeView>(), s.side)
        assertEquals("idle", s.session.state)
    }

    @Test
    fun `stores ids such as __proto__ as ordinary keys`() {
        // A Kotlin map has no prototype; the test keeps the keys of the TypeScript test and checks
        // that they are stored and read like any other key.
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "__proto__", "name" to "Bash", "input" to jo("command" to "ls", "__proto__" to "x")),
                ev("assistant.text", "agent_id" to "constructor", "text" to "x"),
            ),
        )
        assertEquals(listOf("__proto__"), s.tools.keys.toList())
        assertEquals("Bash", own(s.tools, "__proto__")?.name)
        assertEquals(jo("command" to "ls", "__proto__" to "x"), own(s.tools, "__proto__")?.input)
        assertEquals(1, list(s, "constructor").size)
    }
}

/** Checker messages from `check.result`. */
class CheckerMessagesTest {
    /**
     * Build a `check.result` event of the command checker for tool call "t1".
     *
     * @param extra Members that replace the defaults (a plain PASS without texts).
     * @return The event.
     */
    private fun check(vararg extra: Pair<String, Any?>): ViewEvent =
        withParams(
            ev("check.result", "tool_use_id" to "t1", "tool_name" to "Bash", "route" to "command", "decision" to "PASS", "reason" to "", "step_summary" to "s", "comment" to ""),
            *extra,
        )

    @Test
    fun `adds no message for a plain PASS`() {
        val s = applyEvent(initialViewState(), check())
        assertEquals(listOf("tool"), kinds(s))
    }

    @Test
    fun `posts a remark made while approving, after the tool item`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "t1", "name" to "Bash", "input" to jo()),
                check("comment" to "Fine, but add a docstring next time."),
            ),
        )
        assertEquals(
            listOf(
                ToolItem(id = 1, toolUseId = "t1"),
                CheckerItem(id = 2, label = "Command checker", checkerRoute = "command", decision = "PASS", text = "Fine, but add a docstring next time.", toolUseId = "t1"),
            ),
            list(s),
        )
        assertEquals("Fine, but add a docstring next time.", s.tools["t1"]?.check?.comment)
    }

    @Test
    fun `posts DENY reasons, labelled by route, into the calling agent list`() {
        val s = applyEvent(initialViewState(), check("agent_id" to "A1", "route" to "changeset", "decision" to "DENY", "reason" to "Breaks the style rules."))
        val last = list(s, "A1").last() as CheckerItem
        assertEquals("Changeset checker", last.label)
        assertEquals("changeset", last.checkerRoute)
        assertEquals("DENY", last.decision)
        assertEquals(emptyList<Item>(), list(s))
    }

    @Test
    fun `labels decisions of the rules and ignores ESCALATE reasons`() {
        val denied = applyEvent(initialViewState(), check("route" to "deny", "decision" to "DENY", "reason" to "rule"))
        val last = list(denied).last() as CheckerItem
        assertEquals("Rules", last.label)
        assertEquals("", last.checkerRoute)
        val escalated = applyEvent(initialViewState(), check("decision" to "ESCALATE", "reason" to "ask the human"))
        assertEquals(listOf("tool"), kinds(escalated))
    }
}

/** `assistant.delta` streaming. */
class StreamingTest {
    @Test
    fun `appends fragments to one item and finalises it with assistant text`() {
        var s = applyEvents(
            initialViewState(),
            listOf(
                ev("assistant.delta", "agent_id" to null, "message_id" to "m1", "text" to "Hel"),
                ev("assistant.delta", "agent_id" to null, "message_id" to "m1", "text" to "lo **wo"),
            ),
        )
        assertEquals(listOf<Item>(AssistantItem(id = 1, text = "Hello **wo", messageId = "m1", streaming = true)), list(s))
        s = applyEvent(s, ev("assistant.text", "agent_id" to null, "message_id" to "m1", "text" to "Hello **world**"))
        assertEquals(listOf<Item>(AssistantItem(id = 1, text = "Hello **world**", messageId = "m1", streaming = false)), list(s))
    }

    @Test
    fun `keeps thinking and text of one message apart`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("assistant.delta", "message_id" to "m1", "text" to "hm", "thinking" to true),
                ev("assistant.delta", "message_id" to "m1", "text" to "a"),
                ev("assistant.thinking", "message_id" to "m1", "text" to "hmm"),
                ev("assistant.delta", "agent_id" to "A", "message_id" to "m1", "text" to "c"),
            ),
        )
        assertEquals(
            listOf(
                ThinkingItem(id = 1, text = "hmm", messageId = "m1", streaming = false),
                AssistantItem(id = 2, text = "a", messageId = "m1", streaming = true),
            ),
            list(s),
        )
        assertEquals(listOf("c"), list(s, "A").map { (it as AssistantItem).text })
        // A turn end finishes what still streams.
        assertEquals(false, (list(applyEvent(s, ev("turn.result")))[1] as AssistantItem).streaming)
    }

    @Test
    fun `ignores deltas without message_id`() {
        val s0 = initialViewState()
        assertSame(s0, applyEvent(s0, ev("assistant.delta", "text" to "x")))
    }
}

/** `user.input`. */
class UserInputTest {
    @Test
    fun `keeps the source and the images`() {
        // Larger than any image a session accepts: not kept.
        val big = "A".repeat(MAX_IMAGE_DATA + 1)
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev(
                    "user.input",
                    "text" to "Fix it.",
                    "images" to listOf(jo("data" to "aGk=", "media_type" to "image/png"), jo("data" to big, "media_type" to "image/jpeg"), jo("nope" to 1)),
                ),
                ev("user.input", "text" to "<cross-session-message>push</cross-session-message>", "source" to "cross_session"),
                ev("user.input", "text" to "A background task finished.", "source" to "system"),
                ev("user.input", "text" to "x", "source" to "martian"),
            ),
        )
        val users = list(s).map { it as UserItem }
        assertEquals(listOf("user", "cross_session", "system", "user"), users.map { it.source })
        assertEquals(
            listOf(
                ImageView(mediaType = "image/png", data = "aGk=", omitted = false),
                ImageView(mediaType = "image/jpeg", data = "", omitted = true),
            ),
            users[0].images,
        )
        assertEquals(emptyList<ImageView>(), users[1].images)
    }
}

/** `chat.queue`. */
class ChatQueueTest {
    @Test
    fun `replaces the queue, validates entries, and clears it when the bridge stops`() {
        val s = applyEvent(
            initialViewState(),
            ev("chat.queue", "messages" to listOf(jo("id" to "q1", "text" to "hi", "delivery" to "queued", "to" to "main"), jo("id" to "q2"))),
        )
        assertEquals(listOf(QueuedView(id = "q1", text = "hi", delivery = "queued", to = "main")), s.queue)
        val injected = applyEvent(s, ev("chat.queue", "messages" to listOf(jo("id" to "q3", "text" to "now", "delivery" to "inject", "to" to "A1"))))
        assertEquals(listOf(QueuedView(id = "q3", text = "now", delivery = "inject", to = "A1")), injected.queue)
        assertEquals(emptyList<QueuedView>(), applyEvent(injected, ev("ui.clear")).queue)
        assertEquals(emptyList<QueuedView>(), applyEvent(injected, ev("bridge.status", "status" to "exited")).queue)
    }
}

/** The state behind the header of the view. */
class HeaderStateTest {
    @Test
    fun `follows auto-escalation, honesty, usage limits and the model`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("auto_escalate.changed", "settings" to jo("edits" to true, "commands" to "yes")),
                ev(
                    "honesty.update",
                    "mode" to "enforce",
                    "clean" to false,
                    "marks" to listOf(jo("id" to 3, "path" to "a.py", "reason" to "stub"), jo("path" to "no id")),
                    "unreviewed" to listOf("b.py", 7),
                    "unreviewed_count" to 1,
                ),
                ev(
                    "usage.update",
                    "rate_limits" to listOf(jo("type" to "five_hour", "status" to "allowed_warning", "utilization" to 0.8, "resets_at" to "2026-11-18T12:00:00Z"), "junk"),
                ),
                ev("ui.model", "model" to "sonnet"),
                ev("ui.usage", "context" to jo("tokens" to 50, "max_tokens" to 200, "model" to "m"), "cost_usd" to 1.5, "num_turns" to 4),
            ),
        )
        assertEquals(AutoEscalateFlags(commands = false, edits = true, subagents = false), s.autoEscalate)
        assertEquals(
            HonestyInfo(
                mode = "enforce",
                clean = false,
                marks = listOf(HonestyMarkInfo(id = 3, path = "a.py", reason = "stub", symbol = "", openedBy = null)),
                unreviewed = listOf("b.py"),
                unreviewedCount = 1,
                reviewRunning = false,
            ),
            s.honesty,
        )
        assertEquals(
            listOf(RateLimit(type = "five_hour", status = "allowed_warning", utilization = 0.8, resetsAt = "2026-11-18T12:00:00Z")),
            s.usage.rateLimits,
        )
        assertEquals(ContextView(tokens = 50, maxTokens = 200, percent = 25.0, model = "m"), s.usage.context)
        assertEquals(1.5, s.usage.costUsd)
        assertEquals(4, s.usage.numTurns)
        assertEquals("sonnet", s.session.model)
        assertNull(applyEvent(s, ev("ui.model", "model" to null)).session.model)
        // The TypeScript test sends an array as the params of `honesty.update`. Params are always
        // a JSON object here, so the same input is given to the reader the case uses.
        assertNull(parseHonesty(JsonArray(emptyList())))
        assertNull(parseHonesty(null))
    }

    @Test
    fun `keeps the session list, the server help and the hints`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev(
                    "ui.sessions",
                    "running" to listOf(
                        jo("id" to "s-other", "directory" to "/x", "state" to "idle", "open_escalations" to 0, "here" to false, "stale" to false),
                        jo("id" to "s-here", "name" to "refactor", "directory" to "/w", "state" to "escalated", "open_escalations" to 2, "here" to true, "stale" to false),
                        jo("nope" to true),
                    ),
                ),
                ev("ui.serverHelp", "command" to "tmux new-session -A -s calu 'calu serve'", "tool" to "tmux", "detach" to "Ctrl+B, then D", "note" to "n"),
                ev(
                    "ui.hints",
                    "hints" to listOf(jo("id" to "no-session", "level" to "info", "message" to "m", "action" to "newSession", "actionLabel" to "New session"), jo("id" to 5)),
                ),
            ),
        )
        assertTrue(s.sessions.loaded)
        assertEquals(listOf("s-here", "s-other"), s.sessions.list.map { it.id })
        assertEquals(
            SessionEntry(id = "s-here", name = "refactor", directory = "/w", state = "escalated", openEscalations = 2, here = true, stale = false),
            s.sessions.list[0],
        )
        assertEquals("tmux", s.serverHelp?.tool)
        assertEquals(listOf(SetupHint(id = "no-session", level = "info", message = "m", action = "newSession", actionLabel = "New session")), s.hints)
        // An error keeps the last list; an empty ui.serverHelp clears the help.
        val failed = applyEvent(s, ev("ui.sessions", "error" to "the database is not reachable"))
        assertEquals("the database is not reachable", failed.sessions.error)
        assertEquals(2, failed.sessions.list.size)
        assertNull(applyEvent(s, ev("ui.serverHelp")).serverHelp)
    }

    @Test
    fun `records side questions and their answers`() {
        var s = applyEvents(initialViewState(), listOf(ev("ui.sideAsk", "id" to 1, "question" to "why?"), ev("ui.sideAsk", "id" to 2, "question" to "how?")))
        s = applyEvents(s, listOf(ev("ui.sideAnswer", "id" to 1, "answer" to "because"), ev("ui.sideAnswer", "id" to 2, "error" to "no session")))
        assertEquals(
            listOf(
                SideExchangeView(id = 1, question = "why?", answer = "because"),
                SideExchangeView(id = 2, question = "how?", error = "no session"),
            ),
            s.side,
        )
    }
}

/** The link to the session, messages for subagents, and file links. */
class LinkAndFilesTest {
    @Test
    fun `knows when requests can reach a session`() {
        val running = applyEvent(initialViewState(), ev("bridge.status", "status" to "running"))
        assertFalse(isAttached(running))
        val attached = applyEvent(running, ev("ui.attached", "session_id" to "s1", "snapshot" to jo("state" to "idle", "workspace" to "/w"), "events" to ja()))
        assertTrue(isAttached(attached))
        assertTrue(isAttached(applyEvent(attached, ev("ui.link", "state" to "reconnecting", "message" to "retrying"))))
        val lost = applyEvent(attached, ev("ui.link", "state" to "lost", "message" to "gone"))
        assertFalse(isAttached(lost))
        assertEquals(LinkView(status = "lost", message = "gone"), lost.link)
        assertNull(lost.sessions.attachedId)
        assertSame(attached, applyEvent(attached, ev("ui.link", "state" to "bogus")))
        val detached = applyEvent(attached, ev("ui.detached"))
        assertEquals("detached", detached.link.status)
        assertNull(detached.session.sessionId)
        assertEquals("detached", applyEvent(attached, ev("bridge.status", "status" to "exited", "message" to "gone")).link.status)
    }

    @Test
    fun `shows a message for a subagent in its tab, and in Main only that it was sent`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("agents.update", "agents" to listOf(jo("agent_id" to "a1", "description" to "sleeper", "background" to true, "status" to "running"))),
                ev("user.input", "text" to "start", "uuid" to "u1"),
                ev("user.input", "text" to "psst", "to" to "a1", "uuid" to "u2"),
                // Not the user's words: `to` is ignored.
                ev("user.input", "text" to "task finished", "source" to "system", "to" to "a1"),
            ),
        )
        assertEquals(listOf("user", "relay", "user"), kinds(s))
        assertEquals("a1", (list(s)[1] as RelayItem).to)
        assertEquals(1, list(s, "a1").size)
        val inTab = list(s, "a1")[0] as UserItem
        assertEquals("psst", inTab.text)
        assertEquals("a1", inTab.to)
        assertEquals("u2", inTab.uuid)
        assertEquals("user", inTab.source)
        assertFalse(list(s).toString().contains("psst"))
        assertEquals(4, (list(s) + list(s, "a1")).map { it.id }.toSet().size)
    }

    @Test
    fun `tracks a waiting session request and the stored honesty mode`() {
        var s = applyEvent(initialViewState(), ev("bridge.status", "status" to "running"))
        s = applyEvent(s, ev("ui.starting", "title" to "Starting a session", "slow" to false))
        assertEquals(StartingView(title = "Starting a session", slow = false), s.starting)
        assertEquals(true, applyEvent(s, ev("ui.starting", "title" to "Starting a session", "slow" to true)).starting?.slow)
        assertNull(applyEvent(s, ev("ui.starting")).starting)
        assertNull(applyEvent(s, ev("bridge.status", "status" to "exited")).starting)
        assertNull(applyEvent(s, ev("ui.attached", "session_id" to "s1", "snapshot" to jo("state" to "idle", "workspace" to "/w"), "events" to ja())).starting)

        assertEquals("", honestyStoredNote(null, null))
        assertEquals("", honestyStoredNote(null, "off"))
        assertEquals("stored: track", honestyStoredNote(null, "track"))
        s = applyEvents(s, listOf(ev("ui.honestyStored", "mode" to "track"), ev("honesty.update", "mode" to "off")))
        assertEquals("stored: track", honestyStoredNote(s.honesty, s.honestyStored))
        s = applyEvent(s, ev("honesty.update", "mode" to "track", "clean" to true))
        assertEquals("", honestyStoredNote(s.honesty, s.honestyStored))
        assertNull(applyEvent(s, ev("ui.honestyStored", "mode" to 7)).honestyStored)
    }

    @Test
    fun `opens only files that a tool call listed`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "t", "name" to "Bash", "input" to jo("command" to "cat a.py")),
                ev("tool.files", "tool_use_id" to "t", "files" to listOf(jo("raw" to "a.py", "path" to "/w/a.py"), jo("raw" to "", "path" to "/w/b.py"))),
            ),
        )
        assertEquals(listOf(ToolFile(raw = "a.py", path = "/w/a.py")), s.tools["t"]?.files)
        assertTrue(isKnownFile(s, "/w/a.py"))
        assertFalse(isKnownFile(s, "/etc/passwd"))
    }

    @Test
    fun `counts open escalations`() {
        val s = applyEvent(initialViewState(), ev("escalation.request", "escalation_id" to "e", "tool_name" to "Bash", "tool_input" to jo(), "reason" to "", "rendered" to ""))
        assertEquals(1, openEscalationCount(s))
    }
}
