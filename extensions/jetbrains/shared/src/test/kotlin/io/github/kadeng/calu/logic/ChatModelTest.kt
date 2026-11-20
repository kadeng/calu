package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ContextView
import io.github.kadeng.calu.state.RateLimit
import io.github.kadeng.calu.state.StartingView
import io.github.kadeng.calu.state.ToolCheck
import io.github.kadeng.calu.state.ToolFile
import io.github.kadeng.calu.state.ToolResultView
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.UsageView
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.jo
import io.github.kadeng.calu.state.withParams
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The pure helpers of a chat surface in `ChatModel.kt`, `SessionMenu.kt` and `SetupCards.kt` (port of `chatModel.test.ts` and of parts of `secondRun.test.ts`). */
class ChatModelTest {
    private fun stateOf(vararg events: ViewEvent): ViewState = applyEvents(initialViewState(), events.toList())

    private val running = ev("bridge.status", "status" to "running")
    private val sessions = ev(
        "ui.sessions",
        "running" to listOf(
            mapOf("id" to "far-1", "name" to "", "directory" to "/srv/other/project", "state" to "idle", "open_escalations" to 0, "here" to false, "stale" to true),
            mapOf("id" to "here-1", "name" to "refactor", "directory" to "/home/me/work/app", "state" to "running", "open_escalations" to 2, "here" to true, "stale" to false),
        ),
    )
    private val attached = ev(
        "ui.attached",
        "session_id" to "here-1", "initialize" to mapOf("workspace" to "/home/me/work/app"),
        "snapshot" to mapOf("state" to "idle", "claude_session_id" to "c-1"), "events" to emptyList<Any>(),
    )

    private fun tool(name: String, decision: String? = null, result: ToolResultView? = null) = ToolView(
        toolUseId = "t", agentKey = "main", name = name, input = jo(),
        check = decision?.let { ToolCheck(route = "command", decision = it, reason = "", summary = "", comment = "", checkerRoute = "command") },
        result = result,
    )

    // ------------------------------------------------------------------ texts

    @Test
    fun `cuts long texts in the middle and keeps both ends`() {
        val cut = middleTruncate("/home/me/project/src/module/file.ts", 20)
        assertEquals(20, cut.length)
        assertTrue(cut.startsWith("/home/me/"))
        assertTrue(cut.endsWith("file.ts"))
        assertEquals("short", middleTruncate("short", 20))
        assertEquals(5, middleTruncate("abcdefghij", 1).length)
    }

    @Test
    fun `shortens the home directory`() {
        assertEquals("~/projects/calu", shortDirectory("/home/me/projects/calu"))
        assertEquals("~", shortDirectory("/Users/me"))
        assertEquals("/srv/x", shortDirectory("/srv/x"))
    }

    @Test
    fun `previews the head or the tail of a text`() {
        assertEquals(Preview("b\nc", true, 3), previewLines("a\nb\nc", 2, tail = true))
        assertEquals(Preview("a\nb", true, 3), previewLines("a\nb\nc", 2, tail = false))
        assertEquals(Preview("a\nb\n", false, 2), previewLines("a\nb\n", 2, tail = false))
    }

    @Test
    fun `describes a tool input in one line`() {
        assertEquals(ToolHead("ls -la", false), toolHead(jo("command" to "ls\n  -la")))
        assertEquals(ToolHead("/w/a.py", true), toolHead(jo("file_path" to "/w/a.py")))
        assertEquals(ToolHead("b.py", true), toolHead(jo(), "b.py"))
        assertEquals("TODO", toolHead(jo("pattern" to "TODO")).text)
        assertEquals(
            ToolHead("Which runner?", false),
            toolHead(jo("questions" to listOf(mapOf("question" to "Which\n runner?", "options" to emptyList<Any>())))),
        )
        assertEquals("{\"questions\":\"nope\"}", toolHead(jo("questions" to "nope")).text)
        assertEquals("", toolHead(jo()).text)
        assertEquals("{\"n\":1}", toolHead(jo("n" to 1)).text)
    }

    @Test
    fun `words the outcome of a turn`() {
        assertEquals("turn done", turnLabel("success", false))
        assertEquals("turn interrupted", turnLabel("interrupted", false))
        assertEquals("turn ended: error max turns", turnLabel("error_max_turns", true))
        assertEquals("turn ended: success", turnLabel("success", true))
        assertEquals("turn ended: error", turnLabel("", true))
    }

    @Test
    fun `words the cost of a turn next to the cost of the session`() {
        assertEquals("$0.0003 · session $0.0050", turnCost(0.005, 0.0003))
        assertEquals("session $0.0027", turnCost(0.0027, null))
        assertEquals("session $0.0027", turnCost(0.0027))
        assertEquals("", turnCost(null, 0.1))
        assertEquals("Cost $0.0050 (Claude Code, whole session) · last turn: 2 model calls", usageCostLine(0.005, 2))
        assertEquals("Cost $0.0050 (Claude Code, whole session) · last turn: 1 model call", usageCostLine(0.005, 1))
        assertEquals("Cost unknown", usageCostLine(null, 0))
    }

    @Test
    fun `shows the error chip only where the decision chip does not already say it`() {
        val failed = ToolResultView("x", true)
        assertTrue(showsErrorChip(tool("Bash", "PASS", failed)))
        assertTrue(showsErrorChip(tool("Bash", "ESCALATE", failed)))
        assertFalse(showsErrorChip(tool("Bash", "DENY", failed)))
        assertFalse(showsErrorChip(tool("AskUserQuestion", "ESCALATE", failed)))
        assertFalse(showsErrorChip(tool("Bash", "PASS", ToolResultView("x", false))))
        assertFalse(showsErrorChip(tool("Bash")))
    }

    @Test
    fun `splits a label into text and file links, longest match first`() {
        val files = listOf(ToolFile("a.py", "/w/a.py"), ToolFile("src/a.py", "/w/src/a.py"))
        assertEquals(
            listOf(LabelPart("cat "), LabelPart("src/a.py", 1), LabelPart(" "), LabelPart("a.py", 0)),
            splitByFiles("cat src/a.py a.py", files),
        )
        assertEquals(listOf(LabelPart("ls")), splitByFiles("ls", null))
    }

    @Test
    fun `names the look of a decision`() {
        assertEquals(
            listOf("pass", "allow", "deny", "escalate", "pending", "pending"),
            listOf("PASS", "allow", "DENY", "ESCALATE", null, "other").map(::decisionKind),
        )
    }

    @Test
    fun `separates the directory from the file name`() {
        assertEquals(PathParts("src/app/", "main.py"), splitPath("src/app/main.py"))
        assertEquals(PathParts("", "main.py"), splitPath("main.py"))
        assertEquals(PathParts("C:\\work\\", "a.ts"), splitPath("C:\\work\\a.ts"))
    }

    // ------------------------------------------------------------------ session dropdown

    private fun name(e: SessionMenuEntry): String = when (e) {
        is SessionMenuEntry.Session -> e.id
        is SessionMenuEntry.Action -> e.id
        is SessionMenuEntry.Heading -> e.label
        is SessionMenuEntry.Note -> e.label
    }

    @Test
    fun `lists workspace sessions first and marks the attached one`() {
        val entries = sessionMenu(stateOf(running, sessions, attached))
        assertEquals(
            listOf("This workspace", "here-1", "Other sessions", "far-1", "new", "resume", "fork", "rename", "refresh", "detach"),
            entries.map(::name),
        )
        val here = entries.filterIsInstance<SessionMenuEntry.Session>().first { it.id == "here-1" }
        assertEquals("refactor", here.title)
        assertEquals("~/work/app", here.directory)
        assertTrue(here.attached)
        assertEquals(2, here.openEscalations)
        assertEquals("busy", here.phase)
        val far = entries.filterIsInstance<SessionMenuEntry.Session>().first { it.id == "far-1" }
        assertEquals("project", far.title)
        assertTrue(far.stale)
        assertFalse(far.attached)
        assertTrue(entries.filterIsInstance<SessionMenuEntry.Action>().all { it.enabled })
    }

    @Test
    fun `disables what needs a session and says why the list is empty`() {
        val none = sessionMenu(stateOf(running, ev("ui.sessions", "running" to emptyList<Any>())))
        assertEquals(SessionMenuEntry.Note("No session is running."), none[0])
        assertEquals(
            mapOf("new" to true, "resume" to true, "fork" to false, "rename" to false, "refresh" to true, "detach" to false),
            none.filterIsInstance<SessionMenuEntry.Action>().associate { it.id to it.enabled },
        )
        assertEquals(SessionMenuEntry.Note("Loading the sessions…"), sessionMenu(stateOf(running))[0])
        val failed = sessionMenu(stateOf(running, ev("ui.sessions", "error" to "locked")))[0] as SessionMenuEntry.Note
        assertTrue(failed.label.contains("locked"))
    }

    @Test
    fun `labels the button`() {
        assertEquals(listOf("calu", "off"), sessionLabel(stateOf()).let { listOf(it.label, it.dot) })
        assertEquals(listOf("No session", "detached"), sessionLabel(stateOf(running)).let { listOf(it.label, it.dot) })
        assertEquals(listOf("refactor", "idle"), sessionLabel(stateOf(running, sessions, attached)).let { listOf(it.label, it.dot) })
        assertEquals("app", sessionLabel(stateOf(running, withParams(attached, "session_id" to "zzz"))).label)
        val reconnecting = sessionLabel(stateOf(running, sessions, attached, ev("ui.link", "state" to "reconnecting", "message" to "retry")))
        assertEquals("reconnecting", reconnecting.dot)
        assertTrue(reconnecting.title.contains("retry"))
    }

    // ------------------------------------------------------------------ tabs

    private val withAgent = stateOf(
        running,
        attached,
        ev("tool.use", "tool_use_id" to "A1", "name" to "Agent", "input" to mapOf("description" to "tests", "prompt" to "run", "run_in_background" to true)),
        ev("agents.update", "agents" to listOf(mapOf("agent_id" to "A1", "description" to "tests", "background" to true, "status" to "running"))),
        ev("assistant.text", "text" to "hi", "agent_id" to "A1"),
        ev("escalation.request", "escalation_id" to "e1", "tool_name" to "Bash", "tool_input" to emptyMap<String, Any>(), "agent_id" to "A1"),
    )

    @Test
    fun `has Main and one tab per subagent with attention marks`() {
        val tabs = tabList(withAgent, "main", emptyMap())
        assertEquals(
            listOf(listOf("main", "Main", "", false), listOf("A1", "tests", "running", true)),
            tabs.map { listOf(it.key, it.label, it.status, it.background) },
        )
        assertEquals(1, tabs[1].openEscalations)
        assertTrue(tabs[1].attention)
        assertFalse(tabs[0].attention)
    }

    @Test
    fun `marks unseen output until it was seen, never on the active tab`() {
        val seen = newestItemIds(withAgent.items)
        val resolved = applyEvents(withAgent, listOf(ev("escalation.resolved", "escalation_id" to "e1", "decision" to "allow")))
        assertFalse(tabList(resolved, "main", seen)[1].attention)
        assertTrue(tabList(resolved, "main", emptyMap())[1].attention)
        assertFalse(tabList(resolved, "A1", emptyMap())[1].attention)
        assertTrue(tabList(resolved, "A1", emptyMap())[0].attention)
    }

    @Test
    fun `lists the open escalations of other tabs`() {
        assertEquals(listOf(StripEntry("e1", "A1", "tests: Bash")), otherOpenEscalations(withAgent, "main"))
        assertEquals(emptyList<StripEntry>(), otherOpenEscalations(withAgent, "A1"))
    }

    // ------------------------------------------------------------------ setup cards

    private fun hint(id: String, action: String, vararg extra: Pair<String, Any?>): Map<String, Any?> =
        mapOf("id" to id, "level" to "info", "message" to "$id.", "action" to action, "actionLabel" to "Do $id") + extra

    @Test
    fun `offers one primary action per bridge problem`() {
        val missing = setupCards(stateOf(ev("bridge.status", "status" to "missing", "message" to "not found")))[0]
        assertEquals("calu-missing", missing.id)
        assertEquals(CardAction("Install calu", "install"), missing.primary)
        val crashed = setupCards(stateOf(ev("bridge.status", "status" to "exited", "message" to "calu ide exited with code 1.")))[0]
        assertEquals(listOf("bridge", "error", "calu ide exited with code 1."), listOf(crashed.id, crashed.level, crashed.message))
        assertEquals("restartBridge", crashed.primary?.action)
        assertEquals(listOf(CardAction("Show log", "showLog")), crashed.secondary)
        assertNull(setupCards(stateOf(ev("bridge.status", "status" to "starting")))[0].primary)
        val stopped = setupCards(stateOf())[0]
        assertEquals("bridge", stopped.id)
        assertEquals("Start", stopped.primary?.label)
    }

    @Test
    fun `turns the hints into cards - server help, session list, links`() {
        val state = stateOf(
            running,
            ev("ui.serverHelp", "command" to "tmux new-session -A -s calu 'calu serve'", "tool" to "tmux", "detach" to "Ctrl+B, then D", "note" to "n"),
            ev(
                "ui.hints",
                "hints" to listOf(
                    hint("claude-missing", "openLink", "href" to "https://example.org"), hint("locked", "unlock"),
                    hint("no-server", "startServer"), hint("no-session", "newSession"),
                ),
            ),
        )
        val cards = setupCards(state)
        assertEquals(listOf("claude-missing", "locked", "no-server", "no-session"), cards.map { it.id })
        assertEquals(CardAction("Do claude-missing", "openLink", "https://example.org"), cards[0].primary)
        assertEquals("unlock", cards[1].primary?.action)
        assertTrue(cards[2].serverHelp)
        assertEquals(CardAction("Open terminal with this command", "startServer"), cards[2].primary)
        assertEquals(listOf(CardAction("Copy command", "copyServerCommand")), cards[2].secondary)
        assertTrue(cards[3].sessionList)
        assertEquals("newSession", cards[3].primary?.action)
    }

    @Test
    fun `is empty once a session is attached and nothing is missing`() {
        assertEquals(emptyList<SetupCard>(), setupCards(stateOf(running, attached)))
    }

    @Test
    fun `a waiting session request shows progress with Cancel at once`() {
        val card = startingCard(StartingView("Starting a session", false))
        assertEquals(listOf(STARTING_CARD, "info"), listOf(card.id, card.level))
        assertTrue(card.busy)
        assertEquals(listOf(CardAction("Cancel", "cancelStart")), card.secondary)
        assertEquals("Starting a session… Asking the calu server for a session.", card.message)
        assertNull(card.primary)
    }

    @Test
    fun `a waiting session request says after a while that no server may be running, and offers to start one`() {
        val card = startingCard(StartingView("Forking the conversation", true))
        assertEquals("warning", card.level)
        assertTrue(card.message.contains("maybe no calu server is running"))
        assertEquals(CardAction("Start calu server", "startServer"), card.primary)
        assertEquals(listOf("Cancel"), card.secondary.map { it.label })
    }

    @Test
    fun `a waiting session request comes first and replaces the no session card`() {
        val noSession = mapOf("id" to "no-session", "level" to "info", "message" to "No session is attached.", "action" to "newSession", "actionLabel" to "New session")
        val state = stateOf(running, ev("ui.hints", "hints" to listOf(noSession)), ev("ui.starting", "title" to "Starting a session", "slow" to false))
        assertEquals(listOf(STARTING_CARD), setupCards(state).map { it.id })
        // A fork from an attached session: the card stands above the transcript.
        val forking = stateOf(running, attached, ev("ui.starting", "title" to "Forking the conversation", "slow" to false))
        assertEquals(listOf(STARTING_CARD), setupCards(forking).map { it.id })
    }

    @Test
    fun `hints while a session is attached are one dismissible line with a short text and a short action`() {
        val claudeMissing = hint("claude-missing", "openLink", "href" to "https://example.org", "short" to "Claude Code not found", "actionShort" to "Guide")
        val hints = ev("ui.hints", "hints" to listOf(claudeMissing))
        // Before the first session: the whole card.
        val before = setupCards(stateOf(running, hints))[0]
        assertEquals("claude-missing", before.id)
        assertFalse(before.compact)
        assertEquals("Do claude-missing", before.primary?.label)
        assertNull(before.short)
        // Attached: one line; the whole message stays (the view shows it as the tooltip).
        val line = setupCards(stateOf(running, attached, hints))[0]
        assertTrue(line.compact)
        assertEquals("Claude Code not found", line.short)
        assertEquals(CardAction("Guide", "openLink", "https://example.org"), line.primary)
        assertEquals(before.message, line.message)
        assertEquals(emptyList<SetupCard>(), setupCards(stateOf(running, attached, hints), listOf("claude-missing")))
        // A dismissal does not hide the card of the empty state.
        assertEquals(1, setupCards(stateOf(running, hints), listOf("claude-missing")).size)
    }

    // ------------------------------------------------------------------ usage

    @Test
    fun `formats tokens`() {
        assertEquals(listOf("950", "1k", "1.3k", "84k", "1.2M", "12M"), listOf(950L, 1000L, 1250L, 84_000L, 1_200_000L, 12_000_000L).map(::formatTokens))
    }

    @Test
    fun `shows percent and tokens, or that nothing is known`() {
        val unknown = usageMeter(UsageView())
        assertEquals(listOf(null, "ctx –", "unknown"), listOf(unknown.percent, unknown.label, unknown.level))
        val known = usageMeter(UsageView(context = ContextView(84_000, 200_000, 42.0, "opus")))
        assertEquals(listOf(42, "42% · 84k", "ok"), listOf(known.percent, known.label, known.level))
        assertTrue(known.title.contains("84,000 of 200,000"))
        assertEquals("high", usageMeter(UsageView(context = ContextView(190_000, 200_000, 95.0, ""))).level)
        assertEquals("warn", usageMeter(UsageView(context = ContextView(150_000, 200_000, 75.0, ""))).level)
    }

    @Test
    fun `the context meter says in its tooltip that the number is from the last turn while Claude works`() {
        val usage = UsageView(numTurns = 1, context = ContextView(84_000, 200_000, 42.0, "opus"))
        assertEquals("Context: 84,000 of 200,000 tokens (42%). Click for the usage panel (limits, /usage, cost).", usageMeter(usage).title)
        assertTrue(usageMeter(usage, true).title.contains("As of the last turn or usage report"))
        assertEquals(usageMeter(usage).label, usageMeter(usage, true).label)
        assertEquals("Context usage is not known yet. Click for the usage panel.", usageMeter(usage.copy(context = null), true).title)
    }

    @Test
    fun `describes a rate limit`() {
        assertEquals(
            "five hour: 63% used, allowed warning, resets 2026-11-18 14:30 UTC",
            rateLimitLine(RateLimit(type = "five_hour", status = "allowed_warning", utilization = 0.63, resetsAt = "2026-11-18T14:30:00Z")),
        )
        assertEquals("limit: no data", rateLimitLine(RateLimit()))
    }

    // ------------------------------------------------------------------ links and files

    @Test
    fun `opens http(s) and relative links and drops the rest`() {
        assertEquals("https://example.org/a", linkTarget("https://example.org/a"))
        assertEquals("src/app.py:12", linkTarget("src/app.py:12"))
        for (bad in listOf("", "#top", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "//evil.example", null, "vscode://x")) {
            assertNull(linkTarget(bad), bad.toString())
        }
    }

    @Test
    fun `finds files that tool calls named, with a line`() {
        val state = stateOf(
            ev("tool.use", "tool_use_id" to "t1", "name" to "Bash", "input" to mapOf("command" to "cat src/app.py")),
            ev(
                "tool.files", "tool_use_id" to "t1",
                "files" to listOf(mapOf("raw" to "src/app.py", "path" to "/w/src/app.py"), mapOf("raw" to "lib/util.py", "path" to "/w/lib/util.py")),
            ),
            ev("tool.use", "tool_use_id" to "t2", "name" to "Read", "input" to emptyMap<String, Any>()),
            ev("tool.files", "tool_use_id" to "t2", "files" to listOf(mapOf("raw" to "other/util.py", "path" to "/w/other/util.py"))),
        )
        val index = buildFileIndex(state.tools)
        assertEquals(FileRef("/w/src/app.py"), lookupFile(index, "src/app.py"))
        assertEquals(FileRef("/w/src/app.py", 42), lookupFile(index, "app.py:42"))
        assertEquals(FileRef("/w/src/app.py", 42), lookupFile(index, "/w/src/app.py:42:7"))
        assertEquals(FileRef("/w/src/app.py"), lookupFile(index, "./src/app.py"))
        assertNull(lookupFile(index, "util.py")) // two files have this name
        assertNull(lookupFile(index, "unknown.py"))
        assertNull(lookupFile(index, "__proto__"))
    }
}
