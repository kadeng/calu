package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.SetupHint
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.jo
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests of what the chat shows after the calu server stops (the owner's decision of
 * 2026-11-19), ported from `extensions/vscode/test/unit/serverLoss.test.ts`, and of the
 * projects directory the server command names (PLAN D243).
 */
class ServerLossTest {
    private val running = ev("bridge.status", "status" to "running")
    private val attached = ev(
        "ui.attached",
        "session_id" to "s1", "initialize" to mapOf("workspace" to "/w"),
        "snapshot" to mapOf("state" to "running", "claude_session_id" to "c-1"), "events" to emptyList<Any>(),
    )
    private val said = ev("assistant.text", "text" to "hello", "agent_id" to null)
    private val escalate = ev(
        "escalation.request",
        "escalation_id" to "e1", "tool_use_id" to "t1", "tool_name" to "Bash", "tool_input" to mapOf("command" to "deploy"),
        "reason" to "r", "rendered" to "", "opened_at" to "2026-11-19T10:00:00+00:00",
    )
    private val unreachable = ev(
        "ui.link",
        "state" to "lost", "message" to "nothing answers at the session's address: the session or its calu server stopped", "cause" to "unreachable",
    )

    /**
     * Build a state from events.
     *
     * @param events The events in order.
     * @return The state.
     */
    private fun stateOf(vararg events: ViewEvent): ViewState = applyEvents(initialViewState(), events.toList())

    /**
     * Build an `ide.status` result with server counts.
     *
     * @param sessions `sessions_running`.
     * @param servers `servers_running`; `null` writes JSON null, `missing` leaves it out.
     * @param missing True to leave `servers_running` out (an older calu).
     * @param reachable `database.reachable`.
     * @return The result.
     */
    private fun status(sessions: Int, servers: Int?, missing: Boolean = false, reachable: Boolean = true): JsonObject = jo(
        "findings" to emptyList<Any>(),
        "database" to mapOf("reachable" to reachable, "place" to "local", "problem" to ""),
        "server" to if (missing) mapOf("sessions_running" to sessions) else mapOf("sessions_running" to sessions, "servers_running" to servers),
    )

    /**
     * Build the `ui.hints` event of a list of hints.
     *
     * @param hints The hints.
     * @return The event.
     */
    private fun hintsEvent(hints: List<SetupHint>): ViewEvent =
        ViewEvent("ui.hints", JsonObject(mapOf("hints" to JsonArray(hints.map { CaluJson.encodeToJsonElement(SetupHint.serializer(), it) }))))

    private val stoppedHints = buildHints(HintInput(caluMissing = false, status = status(0, 0), attached = false, noServer = false, serverStopped = true))

    @Test
    fun `a session that ended with its server holds no step`() {
        val other = ev("tool.use", "tool_use_id" to "t2", "name" to "Bash", "input" to mapOf("command" to "make"), "agent_id" to null)
        val open = stateOf(running, attached, other, escalate)
        assertEquals(setOf("t2"), heldTools(open))
        val ended = applyEvents(open, listOf(unreachable))
        assertTrue(transcriptEnded(ended))
        assertTrue(heldTools(ended).isEmpty())
        assertTrue(heldByAgent(ended).isEmpty())
        val withAgent = applyEvents(open, listOf(ev("agents.update", "agents" to listOf(mapOf("agent_id" to "A", "description" to "lint", "status" to "running"))), unreachable))
        assertEquals(listOf("stopped"), tabList(withAgent, MAIN, emptyMap()).drop(1).map { it.status })
    }

    @Test
    fun `reads the cause, and for an older bridge the sentence of a refused connection`() {
        assertEquals("revoked", lostCause(JsonPrimitive("revoked"), JsonPrimitive("x")))
        assertEquals("unreachable", lostCause(null, JsonPrimitive("nothing answers at the session's address: the session or its calu server stopped")))
        assertNull(lostCause(null, JsonPrimitive("the session did not answer in time")))
        assertNull(lostCause(JsonPrimitive("something new"), JsonPrimitive("nothing answers at the session's address")))
        assertNull(lostCause(JsonPrimitive(3), null))
    }

    @Test
    fun `tells a stopped server from a running one by ide status`() {
        assertEquals(ServerCheck.RUNNING, serverCheck(status(0, 1)))
        assertEquals(ServerCheck.RUNNING, serverCheck(status(2, null)))
        assertEquals(ServerCheck.STOPPED, serverCheck(status(0, 0)))
        assertEquals(ServerCheck.UNKNOWN, serverCheck(status(0, 0, reachable = false)))
        assertEquals(ServerCheck.UNKNOWN, serverCheck(status(0, null)))
        assertEquals(ServerCheck.UNKNOWN, serverCheck(status(0, null, missing = true)))
        assertEquals(ServerCheck.UNKNOWN, serverCheck(null))
        assertTrue(serverStoppedAfterLoss("unreachable", ServerCheck.STOPPED))
        assertTrue(serverStoppedAfterLoss("unreachable", ServerCheck.UNKNOWN))
        assertFalse(serverStoppedAfterLoss("unreachable", ServerCheck.RUNNING))
        assertFalse(serverStoppedAfterLoss("revoked", ServerCheck.STOPPED))
    }

    @Test
    fun `shows the calu server stopped with Start server and Retry, and no connection lost card`() {
        assertEquals(listOf("server-stopped"), stoppedHints.map { it.id })
        val s = stateOf(running, attached, said, unreachable, hintsEvent(stoppedHints))
        val cards = setupCards(s)
        assertEquals(listOf("server-stopped"), cards.map { it.id })
        assertTrue(cards[0].message.startsWith("The calu server stopped;"))
        assertFalse(cards[0].serverHelp)
        assertEquals(CardAction("Start server", "startServer"), cards[0].primary)
        assertEquals(listOf(CardAction("Retry", "retryServer")), cards[0].secondary)
        assertEquals("calu: server stopped", decisionsStatus(s))
    }

    @Test
    fun `keeps the transcript, marked as ended, and the open card does not count as waiting`() {
        val s = stateOf(running, attached, said, escalate, unreachable, hintsEvent(stoppedHints))
        assertTrue(transcriptEnded(s))
        assertTrue(s.items["main"].orEmpty().isNotEmpty())
        assertEquals("open", s.escalations.values.single().status)
        assertEquals(0, decisionsWaiting(s))
        assertEquals("calu: server stopped", decisionsStatus(s))
        assertTrue(otherOpenEscalations(s, "other").isEmpty())
        assertEquals(0, tabList(s, "main", emptyMap()).single().openEscalations)
        val live = stateOf(running, attached, escalate)
        assertEquals(1, decisionsWaiting(live))
        assertEquals("calu: 1 waiting", decisionsStatus(live))
    }

    @Test
    fun `says when a Retry found no server`() {
        assertTrue(serverStoppedMessage("14:02:11").endsWith("Checked at 14:02:11: no server runs yet."))
        val retried = buildHints(HintInput(false, null, attached = false, noServer = false, serverStopped = true, serverRetriedAt = "14:02:11"))
        assertTrue(retried[0].message.contains("14:02:11"))
        assertEquals(10, serverCheckInterval(true))
        assertEquals(30, serverCheckInterval(false))
    }

    @Test
    fun `with the server back but the session gone the session list, the transcript still marked ended`() {
        val back = buildHints(HintInput(false, status(0, 1), attached = false, noServer = false))
        val s = stateOf(running, attached, said, unreachable, hintsEvent(back))
        assertEquals(listOf("no-session"), setupCards(s).map { it.id })
        assertTrue(transcriptEnded(s))
        assertEquals("calu: session ended", decisionsStatus(s))
    }

    @Test
    fun `a new attach ends the mark, another kind of loss keeps connection lost`() {
        assertFalse(transcriptEnded(stateOf(running, attached, said, unreachable, attached)))
        val revoked = stateOf(running, attached, ev("ui.link", "state" to "lost", "message" to "the token was revoked", "cause" to "revoked"))
        assertFalse(transcriptEnded(revoked))
        assertEquals("link-lost", setupCards(revoked)[0].id)
        assertEquals("revoked", revoked.link.cause)
    }

    @Test
    fun `a detach or a bridge restart is not an ended session`() {
        assertFalse(transcriptEnded(stateOf(running, attached, unreachable, ev("ui.detached"))))
        assertFalse(transcriptEnded(stateOf(running, attached, unreachable, ev("bridge.status", "status" to "exited"))))
    }

    // ------------------------------------------------------------------ the projects directory

    @Test
    fun `names the folder that holds the project, or the project where that is too wide`() {
        assertEquals("/home/ada/src", projectsDirFor("/home/ada/src/demo"))
        assertEquals("/home/ada", projectsDirFor("/home/ada/demo/"))
        assertEquals("/home/ada", projectsDirFor("/home/ada"))
        assertEquals("/srv/demo", projectsDirFor("/srv/demo"))
        assertEquals("/home/ada/.work/demo", projectsDirFor("/home/ada/.work/demo"))
        assertEquals("/var/www/site", projectsDirFor("/var/www/site"))
        assertEquals("/root/demo", projectsDirFor("/root/demo"))
        assertNull(projectsDirFor(""))
        assertNull(projectsDirFor("demo"))
        assertNull(projectsDirFor("/"))
        assertNull(projectsDirFor(null))
    }

    @Test
    fun `the server command names the projects directory and says so`() {
        val input = ServerCommandInput(listOf("calu"), false, false, emptyList(), tmux = true, screen = false, projects = "/home/ada/my projects")
        val help = buildServerHelp(input)
        assertEquals("tmux new-session -A -s calu 'calu serve --projects '\\''/home/ada/my projects'\\'''", help.command)
        assertTrue(help.note.contains("It serves /home/ada/my projects: sessions start in it or below it."))
        val own = buildServerHelp(input.copy(extraArgs = listOf("--projects", "/srv"), tmux = false))
        assertEquals("calu serve --projects /srv", own.command)
        assertTrue(own.note.contains("It serves /srv"))
        assertTrue(buildServerHelp(input.copy(projects = null)).note.contains("the directory it starts in"))
    }
}
