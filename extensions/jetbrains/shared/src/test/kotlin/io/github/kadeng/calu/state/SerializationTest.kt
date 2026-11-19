// The view state as JSON: it can be written and read back, in the shape of the TypeScript state.
// Not in the TypeScript tests (there the state is a plain JSON value).
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** `ViewState` with `CaluJson`. */
class SerializationTest {
    /**
     * Build a state that contains every item kind and a value in every part of the state.
     *
     * @return The state: attached to "s1", with recorded turns above the session's events.
     */
    private fun fullState(): ViewState {
        val attached = applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running", "version" to "1.2", "message" to "ok"),
                ev("ui.attached", "session_id" to "s1", "snapshot" to jo("state" to "running", "workspace" to "/w", "model" to "opus"), "events" to ja()),
                ev(
                    "ui.history",
                    "session_id" to "s1",
                    "history" to jo(
                        "turns" to listOf(
                            jo("index" to 1, "prompt_uuid" to "u0", "user_inputs" to listOf("earlier"), "steps" to listOf(jo("tool_name" to "Read", "decision" to "pass")), "answer" to "done", "ended_at" to "x"),
                        ),
                    ),
                ),
            ),
        )
        return applyEvents(
            attached,
            listOf(
                ev("user.input", "text" to "go", "uuid" to "u1", "images" to listOf(jo("data" to "aGk=", "media_type" to "image/png")), "seq" to 1),
                ev("assistant.delta", "message_id" to "m1", "text" to "thinking…", "thinking" to true),
                ev("assistant.thinking", "message_id" to "m1", "text" to "thought"),
                ev("assistant.delta", "message_id" to "m1", "text" to "wor"),
                ev("tool.use", "tool_use_id" to "t1", "name" to "Edit", "input" to jo("file_path" to "a.py", "nested" to jo("list" to listOf(1, 2.5, true, null)))),
                ev("edit.diff", "tool_use_id" to "t1", "path" to "a.py", "start_line" to 3, "end_line" to 4),
                ev("tool.files", "tool_use_id" to "t1", "files" to listOf(jo("raw" to "a.py", "path" to "/w/a.py"))),
                ev("check.result", "tool_use_id" to "t1", "tool_name" to "Edit", "route" to "edit", "decision" to "PASS", "comment" to "ok, but short", "git_head" to "abc"),
                ev("tool.result", "tool_use_id" to "t1", "content" to "ok"),
                ev("tool.use", "tool_use_id" to "L1", "name" to "Agent", "input" to jo("description" to "d", "prompt" to "p", "run_in_background" to true)),
                ev("tool.result", "tool_use_id" to "L1", "content" to "launched"),
                ev("agents.update", "agents" to listOf(jo("agent_id" to "L1", "description" to "d", "background" to true, "status" to "running", "subagent_type" to "Explore", "sdk_agent_id" to "x"))),
                ev("user.input", "text" to "psst", "to" to "L1"),
                ev(
                    "escalation.request",
                    "escalation_id" to "e1",
                    "tool_use_id" to "t2",
                    "tool_name" to "AskUserQuestion",
                    "tool_input" to jo("q" to 1),
                    "reason" to "r",
                    "rendered" to "x",
                    "opened_at" to "2026-11-18T10:00:00Z",
                    "questions" to listOf(jo("question" to "Which?", "header" to "H", "options" to listOf(jo("label" to "A", "description" to "a")), "multiSelect" to true)),
                ),
                ev("ui.decisionPending", "escalation_key" to "e1", "answer" to "A"),
                ev("escalation.request", "escalation_id" to "e2", "tool_name" to "Bash", "tool_input" to jo(), "reason" to "", "rendered" to ""),
                ev("escalation.resolved", "escalation_id" to "e2", "decision" to "deny", "note" to "no"),
                ev("turn.result", "subtype" to "success", "cost_usd" to 0.25, "num_turns" to 1, "background_tasks" to 1, "seq" to 9),
                ev("ui.notice", "level" to "warning", "text" to "careful"),
                ev("chat.queue", "messages" to listOf(jo("id" to "q1", "text" to "later", "delivery" to "inject", "to" to "L1"))),
                ev("ui.sideAsk", "id" to 1, "question" to "why?"),
                ev("ui.sideAnswer", "id" to 1, "answer" to "because"),
                ev("ui.sideAsk", "id" to 2, "question" to "how?"),
                ev("ui.sideAnswer", "id" to 2, "error" to "failed"),
                ev("ui.sessions", "running" to listOf(jo("id" to "s1", "name" to "n", "directory" to "/w", "state" to "running", "open_escalations" to 1, "here" to true, "stale" to false))),
                ev("ui.serverHelp", "command" to "calu serve", "tool" to "plain", "detach" to "", "note" to "n"),
                ev("ui.hints", "hints" to listOf(jo("id" to "no-server", "level" to "warning", "message" to "m", "action" to "openLink", "actionLabel" to "Open", "href" to "https://example.org", "short" to "s", "actionShort" to "o"))),
                ev("auto_escalate.changed", "settings" to jo("edits" to true)),
                ev("honesty.update", "mode" to "enforce", "baseline" to "abc", "baseline_source" to "user", "marks" to listOf(jo("id" to 1, "path" to "a.py", "reason" to "stub", "symbol" to "f", "opened_by" to "checker")), "unreviewed" to listOf("b.py"), "unreviewed_count" to 1, "review_required" to true),
                ev("ui.honestyStored", "mode" to "track"),
                ev("ui.starting", "title" to "Starting", "slow" to true),
                ev("usage.update", "rate_limits" to listOf(jo("type" to "five_hour", "status" to "allowed", "utilization" to 0.5, "resets_at" to "2026-11-18T12:00:00Z"))),
                ev("ui.usage", "context" to jo("tokens" to 50, "max_tokens" to 200, "model" to "m")),
                ev("ui.link", "state" to "reconnecting", "message" to "retrying"),
            ),
        )
    }

    @Test
    fun `round-trips a state with every item kind through CaluJson`() {
        val s = fullState()
        val kinds = s.items.values.flatten().map { it.kind }.toSet()
        assertEquals(
            setOf("user", "assistant", "thinking", "tool", "checker", "escalation", "turn", "launch", "notice", "relay", "divider"),
            kinds,
        )
        val text = CaluJson.encodeToString(ViewState.serializer(), s)
        val back = CaluJson.decodeFromString(ViewState.serializer(), text)
        assertEquals(s, back)
        // The order of the map entries and of the lists survives.
        assertEquals(s.items.keys.toList(), back.items.keys.toList())
        assertEquals(s.tools.keys.toList(), back.tools.keys.toList())
        assertEquals(text, CaluJson.encodeToString(ViewState.serializer(), back))
    }

    @Test
    fun `writes the shape of the TypeScript state`() {
        val root = CaluJson.encodeToJsonElement(ViewState.serializer(), fullState()) as JsonObject
        // Items carry their kind as the member "kind"; the state's members are camelCase.
        val main = root.obj("items")?.arr(MAIN).orEmpty().mapNotNull { it.asObj() }
        assertEquals(kinds(fullState()), main.map { it.str("kind") })
        assertTrue(main.none { "type" in it })
        assertEquals("t1", main.first { it.str("kind") == "tool" && it.str("toolUseId") == "t1" }.str("toolUseId"))
        assertEquals("s1", root.obj("session")?.str("sessionId"))
        // The types taken from the protocol keep its snake_case names.
        val agent = root.arr("agents").first().asObj()
        assertEquals("L1", agent?.str("agent_id"))
        assertEquals("Explore", agent?.str("subagent_type"))
        assertFalse("agentId" in agent.orEmpty())
        val honesty = root.obj("honesty")
        assertTrue("review_required" in honesty.orEmpty())
        assertEquals("checker", honesty?.arr("marks")?.first().asObj()?.str("opened_by"))
        assertEquals("2026-11-18T12:00:00Z", root.obj("usage")?.arr("rateLimits")?.first().asObj()?.str("resets_at"))
    }
}

/** `applyEvent` with params of the wrong shape (not in the TypeScript tests). */
class MalformedParamsTest {
    @Test
    fun `returns a state for every method with members of the wrong type`() {
        val methods = listOf(
            "bridge.status", "ui.link", "ui.attached", "ui.history", "ui.honestyStored", "ui.starting", "ui.detached", "ui.sessions",
            "ui.serverHelp", "ui.hints", "ui.model", "ui.usage", "ui.notice", "ui.noticeResolved", "ui.clear", "ui.decisionPending", "ui.decisionFailed",
            "ui.sideAsk", "ui.sideAnswer", "session.state", "assistant.delta", "assistant.text", "assistant.thinking", "user.input",
            "tool.use", "check.result", "tool.result", "chat.queue", "tool.files", "edit.diff", "escalation.request",
            "escalation.resolved", "turn.result", "agents.update", "auto_escalate.changed", "permission_mode.changed", "honesty.update", "usage.update", "log",
        )
        val members = listOf(
            "status", "state", "message", "version", "session_id", "snapshot", "initialize", "events", "resumed", "history", "mode",
            "title", "slow", "running", "error", "command", "tool", "hints", "model", "effort", "context", "rate_limits", "cost_usd",
            "num_turns", "level", "text", "escalation_key", "answer", "id", "question", "message_id", "thinking", "agent_id", "source",
            "images", "uuid", "to", "tool_use_id", "name", "input", "tool_name", "route", "decision", "reason", "comment", "content",
            "is_error", "messages", "files", "path", "start_line", "end_line", "escalation_id", "tool_input", "questions", "note",
            "agents", "settings", "marks", "unreviewed", "seq",
        )
        val junk = listOf(null, 7, -1.5, "", "x", true, ja(), ja(null, 3, "x", ja(), jo()), jo(), jo("id" to jo(), "method" to 3, "params" to "x"))
        var state = applyEvent(initialViewState(), ev("escalation.request", "escalation_id" to "x", "tool_name" to "Bash"))
        for (value in junk) {
            val params = jo(*members.map { it to value }.toTypedArray())
            for (method in methods) {
                state = applyEvent(state, ViewEvent(method, params))
                // Every state the reducer returns can be written and read back.
                assertEquals(state, CaluJson.decodeFromString(ViewState.serializer(), CaluJson.encodeToString(ViewState.serializer(), state)))
            }
        }
        assertTrue(state.items.containsKey(MAIN))
    }
}
