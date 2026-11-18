package io.github.kadeng.calu.chat

import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.bridge.PasswordPrompt
import io.github.kadeng.calu.bridge.SshLauncher
import io.github.kadeng.calu.bridge.SshSettings
import io.github.kadeng.calu.bridge.fakeBridgePath
import io.github.kadeng.calu.bridge.fakeLocalLauncher
import io.github.kadeng.calu.bridge.script
import io.github.kadeng.calu.bridge.waitUntil
import io.github.kadeng.calu.state.AssistantItem
import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.EscalationItem
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.ToolItem
import io.github.kadeng.calu.state.TurnItem
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.openEscalationCount
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files
import java.util.concurrent.atomic.AtomicReference

/** The chat controller against the scripted bridge process (`fake_bridge.py`). */
class ChatControllerTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val tmp = Files.createTempDirectory("calu-chat").toFile().absolutePath
    private val supervisors = ArrayList<BridgeSupervisor>()

    @AfterEach
    fun cleanup() {
        supervisors.forEach { it.dispose() }
        scope.cancel()
    }

    private fun controller(vararg options: String, prompt: PasswordPrompt = PasswordPrompt { _, _ -> null }): ChatController {
        val s = BridgeSupervisor(fakeLocalLauncher(tmp, *options), scope, "test", prompt, delayMs = { 20 })
        supervisors.add(s)
        return ChatController(s, scope, { ServerSettings(sudo = true) })
    }

    @Test
    fun `a scripted conversation - attach at start, send, an escalation that is allowed, the turn ends`() = runBlocking {
        val c = controller()
        c.start()
        waitUntil("attached") { c.state.link.status == LinkStatus.ATTACHED }
        assertEquals("s-1", c.state.session.sessionId)
        assertTrue(c.state.hints.none { it.id == "no-session" })

        assertTrue(c.send("run the tests", inject = false, tab = MAIN))
        waitUntil("the escalation card") { openEscalationCount(c.state) == 1 }
        val items = c.state.items[MAIN]!!
        assertEquals("run the tests", items.filterIsInstance<UserItem>().single().text)
        assertEquals("I will run a command.", items.filterIsInstance<AssistantItem>().single().text)
        assertFalse(items.filterIsInstance<AssistantItem>().single().streaming == true)
        val tool = c.state.tools[items.filterIsInstance<ToolItem>().single().toolUseId]!!
        assertEquals("ESCALATE", tool.check!!.decision)
        val card = c.state.escalations[items.filterIsInstance<EscalationItem>().single().escalationKey]!!
        assertEquals("runs project code", card.reason)
        assertEquals(MAIN, c.state.focus!!.tab)

        assertTrue(c.decide(card.key, allow = true, note = "fine"))
        waitUntil("the turn's end") { c.state.items[MAIN]!!.any { it is TurnItem } }
        assertEquals(EscalationStatus.ALLOW, c.state.escalations[card.key]!!.status)
        assertEquals("fine", c.state.escalations[card.key]!!.note)
        assertEquals("ok", c.state.tools["t-1"]!!.result!!.content)
        waitUntil("the usage") { c.state.usage.context != null }
        assertEquals(1000, c.state.usage.context!!.tokens)
    }

    @Test
    fun `a subscriber gets the snapshot first and then every event, and rebuilds the same state`() = runBlocking {
        val c = controller()
        val mirror = AtomicReference<ViewState?>(null)
        val job = scope.launch {
            c.updates().collect { u ->
                when (u) {
                    is ChatUpdate.Snapshot -> mirror.set(u.state)
                    is ChatUpdate.Event -> mirror.set(applyEvent(mirror.get()!!, u.event))
                }
            }
        }
        c.start()
        waitUntil("attached") { c.state.link.status == LinkStatus.ATTACHED }
        c.send("go", inject = false, tab = MAIN)
        waitUntil("the escalation card") { openEscalationCount(c.state) == 1 }
        waitUntil("the mirror") { mirror.get() == c.state }
        job.cancel()
    }

    @Test
    fun `no session and no server - the hints say so and the tmux command is offered`() = runBlocking {
        val c = controller("--no-session")
        c.start()
        waitUntil("the no-session hint") { c.state.hints.any { it.id == "no-session" } }
        assertEquals(LinkStatus.DETACHED, c.state.link.status)
        assertFalse(c.startSession("new", prompt = "hello"))
        waitUntil("the no-server hint") { c.state.hints.any { it.id == "no-server" } }
        val help = c.state.serverHelp
        assertNotNull(help)
        assertEquals("tmux", help!!.tool)
        assertEquals("tmux new-session -A -s calu 'calu serve --sudo'", help.command)
        assertEquals(null, c.state.starting)
    }

    @Test
    fun `a locked store - the hint offers Unlock, and one password attaches`() = runBlocking {
        val asked = java.util.concurrent.atomic.AtomicInteger()
        val c = controller("--locked", prompt = PasswordPrompt { _, _ -> asked.incrementAndGet(); "secret" })
        c.start()
        waitUntil("the locked hint") { c.state.hints.any { it.id == "locked" } }
        assertEquals(0, asked.get())
        assertTrue(c.unlock())
        waitUntil("attached") { c.state.link.status == LinkStatus.ATTACHED }
        assertEquals(1, asked.get())
        assertTrue(c.state.hints.none { it.id == "locked" })
    }

    @Test
    fun `ssh mode - after a dropped connection the controller attaches again and continues the transcript`() = runBlocking {
        val counter = java.io.File(tmp, "count").apply { writeText("") }
        // Each connection is a new bridge process, as over SSH; the first one ends after eight
        // answers. (The scripted bridge keeps no session between processes, so the second attach
        // finds an empty session: the test checks that the attach happens with `after_seq`.)
        val ssh = script(
            "ssh",
            "echo x >> '${counter.absolutePath}'\n" +
                "if [ \$(wc -l < '${counter.absolutePath}') -le 1 ]; then exec python3 '${fakeBridgePath()}' ide --cwd /srv/p --exit-after 8; fi\n" +
                "exec python3 '${fakeBridgePath()}' ide --cwd /srv/p",
        )
        val s = BridgeSupervisor(SshLauncher(SshSettings("box", "/srv/p", sshExecutable = ssh)), scope, "test", { _, _ -> null }, delayMs = { 20 })
        supervisors.add(s)
        val c = ChatController(s, scope)
        c.start()
        waitUntil("attached") { c.state.link.status == LinkStatus.ATTACHED }
        c.send("go", inject = false, tab = MAIN)
        waitUntil("the drop") { counter.readLines().size >= 2 }
        waitUntil("attached again") { c.state.bridge.status == BridgeStatus.RUNNING && c.state.link.status == LinkStatus.ATTACHED }
        assertEquals("/srv/p", c.state.session.workspace)
    }
}
