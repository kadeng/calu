// Escalations (also questions of the agent), subagent tasks and results, and attaching.
// Port of extensions/vscode/test/unit/viewStateEscalations.test.ts (same cases, same order).
package io.github.kadeng.calu.state

import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Build an `escalation.request` event.
 *
 * @param id calu's escalation id.
 * @param extra Members that are added to the params or replace the defaults.
 * @return The event.
 */
private fun request(id: String, vararg extra: Pair<String, Any?>): ViewEvent =
    withParams(
        ev("escalation.request", "escalation_id" to id, "tool_use_id" to "t", "tool_name" to "Bash", "tool_input" to jo("command" to "rm x"), "reason" to "r", "rendered" to "`rm x`"),
        *extra,
    )

/**
 * Read an escalation that must exist.
 *
 * @param state The state.
 * @param key The escalation's state key.
 * @return `escalations[key]`.
 * @throws IllegalStateException if the state has no such escalation.
 */
private fun esc(state: ViewState, key: String): EscalationView = state.escalations[key] ?: error("no escalation $key")

/** Escalation cards. */
class EscalationsTest {
    @Test
    fun `opens and resolves escalations, including cancelled`() {
        var s = applyEvents(initialViewState(), listOf(request("esc-1"), request("esc-2"), request("esc-3", "opened_at" to "2026-11-18T10:00:00Z")))
        assertEquals(3, openEscalationCount(s))
        assertEquals("2026-11-18T10:00:00Z", esc(s, "esc-3").openedAt)
        s = applyEvents(
            s,
            listOf(
                ev("escalation.resolved", "escalation_id" to "esc-1", "decision" to "allow"),
                ev("escalation.resolved", "escalation_id" to "esc-2", "decision" to "deny", "note" to "no"),
                ev("escalation.resolved", "escalation_id" to "esc-3", "decision" to "cancelled"),
            ),
        )
        assertEquals("allow", esc(s, "esc-1").status)
        assertEquals("deny", esc(s, "esc-2").status)
        assertEquals("no", esc(s, "esc-2").note)
        assertEquals("cancelled", esc(s, "esc-3").status)
        assertEquals(0, openEscalationCount(s))
    }

    @Test
    fun `never reopens a closed card, a reused id gets a new key`() {
        var s = applyEvents(initialViewState(), listOf(request("e"), ev("escalation.resolved", "escalation_id" to "e", "decision" to "deny"), request("e")))
        assertEquals(2, s.escalations.size)
        assertEquals("deny", esc(s, "e").status)
        s = applyEvent(s, ev("escalation.resolved", "escalation_id" to "e", "decision" to "allow"))
        assertEquals(listOf("allow", "deny"), s.escalations.values.map { it.status }.sorted())
    }

    @Test
    fun `requests the tab of a new escalation`() {
        val s = applyEvents(initialViewState(), listOf(request("e1"), request("e2", "agent_id" to "A1")))
        assertEquals(FocusRequest(tab = "A1", seq = 2), s.focus)
        assertEquals("e2", (list(s, "A1").last() as EscalationItem).escalationKey)
        assertEquals(listOf("A1"), s.agents.map { it.agentId })
    }

    @Test
    fun `marks a decision as pending and takes it back when sending failed`() {
        var s = applyEvent(initialViewState(), request("e"))
        s = applyEvent(s, ev("ui.decisionPending", "escalation_key" to "e"))
        assertEquals(true, esc(s, "e").pending)
        s = applyEvent(s, ev("ui.decisionFailed", "escalation_key" to "e"))
        assertEquals(false, esc(s, "e").pending)
        assertSame(s, applyEvent(s, ev("ui.decisionPending", "escalation_key" to "unknown")))
    }

    @Test
    fun `cancels open escalations when the session stops or is left`() {
        val open = applyEvent(initialViewState(), request("e"))
        assertEquals("cancelled", esc(applyEvent(open, ev("session.state", "state" to "stopped")), "e").status)
        assertEquals("cancelled", esc(applyEvent(open, ev("session.state", "state" to "failed")), "e").status)
        assertEquals("cancelled", esc(applyEvent(open, ev("ui.detached")), "e").status)
        assertEquals("open", esc(applyEvent(open, ev("session.state", "state" to "escalated")), "e").status)
    }

    @Test
    fun `keeps the cards open when the bridge or the connection is lost, that is no decision`() {
        var open = applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running"),
                ev(
                    "ui.attached",
                    "session_id" to "s1",
                    "snapshot" to jo("state" to "escalated", "workspace" to "/w", "escalations" to listOf(request("e").params)),
                    "events" to listOf(request("e")),
                ),
            ),
        )
        open = applyEvent(open, ev("ui.decisionPending", "escalation_key" to "e"))
        for (loss in listOf(ev("bridge.status", "status" to "exited", "message" to "gone"), ev("ui.link", "state" to "lost", "message" to "gone"))) {
            val lost = applyEvent(open, loss)
            // Still open, and a decision that was on its way no longer counts as sent.
            assertEquals("open", esc(lost, "e").status)
            assertEquals(false, esc(lost, "e").pending)
            assertEquals(1, openEscalationCount(lost))
            assertFalse(isAttached(lost))
            // The session it had stays known, so that the same one is attached again.
            assertEquals("s1", lost.session.sessionId)
        }
    }

    @Test
    fun `continues the transcript when the same session is attached again without a gap`() {
        val first = applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running"),
                ev(
                    "ui.attached",
                    "session_id" to "s1",
                    "snapshot" to jo("state" to "escalated", "workspace" to "/w", "last_seq" to 3, "escalations" to listOf(request("e").params)),
                    "events" to listOf(ev("user.input", "text" to "go", "seq" to 1), ev("assistant.text", "text" to "on it", "seq" to 2), request("e", "seq" to 3)),
                ),
                ev("ui.honestyStored", "mode" to "track"),
                ev("bridge.status", "status" to "exited"),
                ev("bridge.status", "status" to "running"),
            ),
        )
        assertEquals(listOf("user", "assistant", "escalation"), kinds(first))
        // The decision was made elsewhere meanwhile; the replay after seq 3 says so.
        val resumed = applyEvent(
            first,
            ev(
                "ui.attached",
                "session_id" to "s1",
                "resumed" to true,
                "snapshot" to jo("state" to "running", "workspace" to "/w", "last_seq" to 5, "escalations" to ja()),
                "events" to listOf(ev("escalation.resolved", "escalation_id" to "e", "decision" to "allow", "seq" to 4), ev("assistant.text", "text" to "done", "seq" to 5)),
            ),
        )
        assertEquals(listOf("user", "assistant", "escalation", "assistant"), kinds(resumed))
        assertEquals("allow", esc(resumed, "e").status)
        assertEquals(list(first), list(resumed).take(3))
        assertEquals(5, resumed.lastSeq)
        assertEquals("track", resumed.honestyStored)
        assertEquals("attached", resumed.link.status)
        // `resumed` for another session than the one shown is a full attach.
        val other = applyEvent(first, ev("ui.attached", "session_id" to "s2", "resumed" to true, "snapshot" to jo("state" to "idle", "workspace" to "/w"), "events" to ja()))
        assertEquals(emptyList<Item>(), list(other))
        assertNull(other.honestyStored)
    }
}

/** Questions of the agent (`AskUserQuestion`). */
class QuestionsTest {
    /** Two usable questions and one entry without a question text. */
    private val questions = ja(
        jo(
            "question" to "Which database?",
            "header" to "Database",
            "options" to listOf(jo("label" to "PostgreSQL", "description" to "as now"), jo("label" to "SQLite"), jo("nolabel" to 1)),
            "multiSelect" to false,
        ),
        jo("question" to "Which checks?", "options" to listOf(jo("label" to "lint", "description" to ""), jo("label" to "types", "description" to "")), "multiSelect" to true),
        jo("header" to "no question text"),
    )

    @Test
    fun `reads the questions defensively`() {
        val parsed = parseQuestions(questions)
        assertEquals(
            listOf(
                QuestionView(
                    question = "Which database?",
                    header = "Database",
                    options = listOf(QuestionOption("PostgreSQL", "as now"), QuestionOption("SQLite", "")),
                    multiSelect = false,
                ),
                QuestionView(question = "Which checks?", header = "", options = listOf(QuestionOption("lint", ""), QuestionOption("types", "")), multiSelect = true),
            ),
            parsed,
        )
        assertEquals(emptyList<QuestionView>(), parseQuestions(json("nope")))
    }

    @Test
    fun `formats the answer text`() {
        val parsed = parseQuestions(questions)
        assertEquals("SQLite", formatAnswer(parsed.take(1), listOf(AnswerChoice(listOf("SQLite"), ""))))
        assertEquals(
            "Database: SQLite, but only for tests\nWhich checks?: lint, types",
            formatAnswer(parsed, listOf(AnswerChoice(listOf("SQLite"), " but only for tests "), AnswerChoice(listOf("lint", "types"), ""))),
        )
        assertEquals("Database: (no answer)\nWhich checks?: none", formatAnswer(parsed, listOf(null, AnswerChoice(emptyList(), "none"))))
        assertEquals("", formatAnswer(parsed, emptyList()))
    }

    @Test
    fun `shows an answered escalation as answered`() {
        var s = applyEvent(initialViewState(), request("q", "tool_name" to "AskUserQuestion", "questions" to questions))
        assertEquals(2, esc(s, "q").questions.size)
        s = applyEvent(s, ev("ui.decisionPending", "escalation_key" to "q", "answer" to "SQLite"))
        assertEquals(true, esc(s, "q").pending)
        assertEquals("SQLite", esc(s, "q").answer)
        s = applyEvent(s, ev("escalation.resolved", "escalation_id" to "q", "decision" to "allow"))
        assertEquals("answered", esc(s, "q").status)
        assertEquals("SQLite", esc(s, "q").answer)
        assertEquals(false, esc(s, "q").pending)
    }

    @Test
    fun `shows a question that was answered elsewhere, or replayed, as answered with its answer`() {
        val note = "The user answered your question: Database: SQLite\nWhich checks?: lint\nThe AskUserQuestion call itself was not run; this is the answer. Carry on with it."
        var s = applyEvent(initialViewState(), request("q", "tool_name" to "AskUserQuestion", "questions" to questions))
        s = applyEvent(s, ev("escalation.resolved", "escalation_id" to "q", "decision" to "deny", "note" to note))
        assertEquals("answered", esc(s, "q").status)
        assertEquals("Database: SQLite\nWhich checks?: lint", esc(s, "q").answer)
        assertNull(esc(s, "q").note)
        assertEquals("another text", answerFromNote("another text"))
        var cancelled = applyEvent(initialViewState(), request("q", "tool_name" to "AskUserQuestion", "questions" to questions))
        cancelled = applyEvent(cancelled, ev("escalation.resolved", "escalation_id" to "q", "decision" to "cancelled"))
        assertEquals("cancelled", esc(cancelled, "q").status)
    }
}

/** Subagent tasks and results. */
class SubagentsTest {
    /** The `tool.use` event that launches a foreground subagent "L1". */
    private val launch = ev(
        "tool.use",
        "tool_use_id" to "L1",
        "name" to "Agent",
        "input" to jo("description" to "Survey the tests", "prompt" to "List every test file.", "subagent_type" to "Explore", "run_in_background" to false),
    )

    @Test
    fun `records the task from the launching tool call and the result from its tool result`() {
        var s = applyEvents(
            initialViewState(),
            listOf(
                launch,
                ev("assistant.text", "agent_id" to "L1", "text" to "looking"),
                ev("tool.result", "tool_use_id" to "L1", "content" to "There are 12 test files.", "is_error" to false),
            ),
        )
        assertEquals(
            SubagentView(
                agentId = "L1",
                description = "Survey the tests",
                subagentType = "Explore",
                background = false,
                task = "List every test file.",
                result = SubagentResult(content = "There are 12 test files.", isError = false, final = true),
            ),
            s.subagents["L1"],
        )
        // The main transcript has the launching call; the subagent's tab starts with the task.
        assertEquals(listOf("L1"), list(s).map { (it as ToolItem).toolUseId })
        assertEquals(listOf("launch", "assistant"), kinds(s, "L1"))
        val first = list(s, "L1")[0] as LaunchItem
        assertEquals("List every test file.", first.prompt)
        assertEquals("Survey the tests", first.description)
        assertEquals(listOf("L1"), s.agents.map { it.agentId })
        s = applyEvent(s, ev("agents.update", "agents" to listOf(jo("agent_id" to "L1", "description" to "Survey the tests", "status" to "completed"))))
        assertEquals(2, list(s, "L1").size)
    }

    @Test
    fun `puts the task first even if events of the subagent came earlier`() {
        val s = applyEvents(initialViewState(), listOf(ev("assistant.text", "agent_id" to "L1", "text" to "early"), launch, launch))
        assertEquals(listOf("launch", "assistant"), kinds(s, "L1"))
    }

    @Test
    fun `marks the result of a background launch as not final`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "B1", "name" to "Task", "input" to jo("description" to "Watch CI", "prompt" to "Poll.", "run_in_background" to true)),
                ev("tool.result", "tool_use_id" to "B1", "content" to "Launched in the background."),
            ),
        )
        assertEquals(true, s.subagents["B1"]?.background)
        assertEquals(SubagentResult(content = "Launched in the background.", isError = false, final = false), s.subagents["B1"]?.result)
        assertTrue((list(s, "B1")[0] as LaunchItem).background)
    }

    @Test
    fun `keeps ordinary tool results out of the subagents`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "t", "name" to "Bash", "input" to jo("command" to "ls")),
                ev("tool.result", "tool_use_id" to "t", "content" to "a"),
            ),
        )
        assertEquals(emptySet<String>(), s.subagents.keys)
    }
}

/** `ui.attached` / `applyAttach`. */
class AttachingTest {
    /** A result of `ide.attach` with a snapshot and recorded events (one entry is not an event). */
    private val result: JsonObject = jo(
        "session_id" to "S1",
        "initialize" to jo("core_version" to "1", "workspace" to "/w", "project" to "demo"),
        "snapshot" to jo(
            "state" to "escalated",
            "workspace" to "/w",
            "claude_session_id" to "C1",
            "model" to "opus",
            "effort" to "high",
            "last_seq" to 9,
            "agents" to listOf(jo("agent_id" to "A1", "description" to "worker", "background" to true, "status" to "running")),
            "queue" to listOf(jo("id" to "q1", "text" to "later", "delivery" to "queued", "to" to "main")),
            "auto_escalate" to jo("edits" to true),
            "honesty" to jo("mode" to "warn", "clean" to true),
            "rate_limits" to listOf(jo("type" to "five_hour", "status" to "allowed")),
            "escalations" to listOf(
                jo("escalation_id" to "e-open", "tool_use_id" to "t2", "tool_name" to "Bash", "tool_input" to jo(), "reason" to "r", "rendered" to ""),
                jo("escalation_id" to "e-new", "agent_id" to "A1", "tool_use_id" to null, "tool_name" to "Edit", "tool_input" to jo(), "reason" to "r", "rendered" to ""),
            ),
        ),
        "events" to listOf(
            ev("session.state", "state" to "stopped", "seq" to 1),
            ev("user.input", "text" to "go", "seq" to 2),
            ev("assistant.delta", "message_id" to "m", "text" to "wor", "seq" to 3),
            ev("escalation.request", "escalation_id" to "e-gone", "tool_name" to "Bash", "tool_input" to jo(), "reason" to "", "rendered" to "", "seq" to 4),
            ev("escalation.request", "escalation_id" to "e-open", "tool_use_id" to "t2", "tool_name" to "Bash", "tool_input" to jo(), "reason" to "r", "rendered" to "", "seq" to 5),
            ev("log", "level" to "error", "message" to "old noise", "seq" to 6),
            ev("ui.clear"),
            "junk",
        ),
        "complete" to true,
    )

    @Test
    fun `replays the events and takes what is true now from the snapshot`() {
        val before = applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running"),
                ev("assistant.text", "text" to "from the session shown before"),
                ev("ui.serverHelp", "command" to "calu serve", "tool" to "plain"),
            ),
        )
        val s = applyEvent(before, ViewEvent("ui.attached", result))
        assertEquals("attached", s.link.status)
        assertEquals(
            SessionView(
                state = "escalated",
                sessionId = "S1",
                claudeSessionId = "C1",
                workspace = "/w",
                project = "demo",
                model = "opus",
                effort = "high",
                backgroundTasks = 0,
            ),
            s.session,
        )
        assertEquals("S1", s.sessions.attachedId)
        assertNull(s.serverHelp)
        assertEquals(listOf("user", "assistant", "escalation", "escalation"), kinds(s))
        val text = list(s)[1] as AssistantItem
        assertEquals("wor", text.text)
        assertEquals(false, text.streaming)
        assertEquals("cancelled", esc(s, "e-gone").status)
        assertEquals("open", esc(s, "e-open").status)
        assertEquals("open", esc(s, "e-new").status)
        assertEquals("A1", esc(s, "e-new").agentKey)
        assertEquals(2, openEscalationCount(s))
        assertEquals(MAIN, s.focus?.tab)
        assertEquals(1, s.agents.size)
        assertEquals(1, s.queue.size)
        assertEquals(AutoEscalateFlags(commands = false, edits = true, subagents = false), s.autoEscalate)
        assertEquals("warn", s.honesty?.mode)
        assertEquals(true, s.honesty?.clean)
        assertEquals(1, s.usage.rateLimits.size)
        assertEquals(9, s.lastSeq)
    }

    @Test
    fun `continues a text that the replay of an attach cut off, instead of showing it twice`() {
        val delta = { text: String -> ev("assistant.delta", "agent_id" to null, "message_id" to "m1", "text" to text) }
        var s = applyAttach(initialViewState(), jo("session_id" to "S", "snapshot" to jo("state" to "running"), "events" to listOf(delta("Building, "))))
        assertEquals(listOf<Item>(AssistantItem(id = 1, text = "Building, ", messageId = "m1", streaming = false, cut = true)), list(s))
        s = applyEvent(s, delta("then "))
        assertEquals(listOf<Item>(AssistantItem(id = 1, text = "Building, then ", messageId = "m1", streaming = true)), list(s))
        s = applyEvent(s, ev("assistant.text", "agent_id" to null, "message_id" to "m1", "text" to "Building, then uploading."))
        assertEquals(listOf<Item>(AssistantItem(id = 1, text = "Building, then uploading.", messageId = "m1", streaming = false)), list(s))
        // The final text alone also replaces the cut item.
        var t = applyAttach(initialViewState(), jo("session_id" to "S", "snapshot" to jo(), "events" to listOf(delta("Buil"))))
        t = applyEvent(t, ev("assistant.text", "agent_id" to null, "message_id" to "m1", "text" to "Built."))
        assertEquals(listOf("Built."), list(t).map { (it as? AssistantItem)?.text ?: "" })
    }

    @Test
    fun `survives a result without members and ignores one without a session id`() {
        val s0 = initialViewState()
        assertSame(s0, applyAttach(s0, jo()))
        val s = applyAttach(s0, jo("session_id" to "S", "snapshot" to "x", "events" to 5))
        assertEquals("S", s.session.sessionId)
        assertEquals("idle", s.session.state)
        assertNull(s.session.workspace)
        assertEquals(mapOf(MAIN to emptyList<Item>()), s.items)
    }
}

/** Small helpers of the view. */
class SmallHelpersTest {
    @Test
    fun `names a session by its name, else its directory, else its id`() {
        assertEquals("refactor", sessionTitle(id = "abcdef123456", name = " refactor ", directory = "/w/proj"))
        assertEquals("proj", sessionTitle(id = "abcdef123456", name = "", directory = "/w/proj/"))
        assertEquals("abcdef12", sessionTitle(id = "abcdef123456", name = "", directory = ""))
        assertEquals(
            "proj",
            sessionTitle(SessionEntry(id = "abcdef123456", name = "", directory = "C:\\w\\proj", state = "idle", openEscalations = 0, here = true, stale = false)),
        )
    }

    @Test
    fun `summarises the honesty state`() {
        assertEquals(HonestyChip(kind = "off", label = ""), honestyChip(null))
        assertEquals("off", honestyChip(HonestyInfo(mode = "off")).kind)
        assertEquals(HonestyChip(kind = "clean", label = "Honesty: clean"), honestyChip(HonestyInfo(mode = "enforce", clean = true, baseline = "abc")))
        assertEquals("review", honestyChip(HonestyInfo(mode = "enforce", reviewRunning = true)).kind)
        assertEquals(
            HonestyChip(kind = "open", label = "Honesty: 1 mark, 3 unreviewed"),
            honestyChip(HonestyInfo(mode = "warn", baseline = "abc", marks = listOf(HonestyMarkInfo(id = 1, path = "a", reason = "r")), unreviewedCount = 3)),
        )
        assertEquals(
            "Honesty: all files unreviewed, no baseline, review required",
            honestyChip(HonestyInfo(mode = "warn", unreviewedAll = true, reviewRequired = true)).label,
        )
    }

    @Test
    fun `passes only answered side questions on as history`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("ui.sideAsk", "id" to 1, "question" to "a?"),
                ev("ui.sideAsk", "id" to 2, "question" to "b?"),
                ev("ui.sideAsk", "id" to 3, "question" to "c?"),
                ev("ui.sideAnswer", "id" to 1, "answer" to "A"),
                ev("ui.sideAnswer", "id" to 2, "error" to "failed"),
            ),
        )
        assertEquals(listOf(SideAnswer(question = "a?", answer = "A")), sideHistory(s))
    }
}
