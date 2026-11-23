package io.github.kadeng.calu.chat

import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.bridge.ScriptedError
import io.github.kadeng.calu.bridge.ScriptedHandler
import io.github.kadeng.calu.bridge.ScriptedLauncher
import io.github.kadeng.calu.bridge.waitUntil
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.NoticeItem
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.jo
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The chat actions of `ChatActions.kt` against a scripted bridge inside the JVM that records
 * every request:
 *
 *     session s-1 "parser work" in /w, conversation c-1, replayed: turn 1 (u-1), then the message u-2
 *     session.history: the conversation's turns 4 (u-1) and 5 (u-2)   (the session was resumed)
 *     ide.start: answers with the new session s-2 (or -32013 with `server` false)
 */
class ChatActionsTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val supervisors = ArrayList<BridgeSupervisor>()

    /** Every request the bridge got, as (method, params); a `session.call` is recorded by its inner method. */
    private val requests = CopyOnWriteArrayList<Pair<String, JsonObject>>()

    @AfterEach
    fun cleanup() {
        supervisors.forEach { it.dispose() }
        scope.cancel()
    }

    private fun attachResult(id: String, events: List<Any>): JsonObject = jo(
        "attached" to true, "session_id" to id, "initialize" to mapOf("workspace" to "/w"),
        "snapshot" to mapOf("state" to "idle", "claude_session_id" to "c-1", "workspace" to "/w", "last_seq" to events.size),
        "events" to events, "complete" to true,
    )

    private fun handler(server: Boolean, conversation: Boolean) = ScriptedHandler { _, method, params ->
        val inner = if (method == "session.call") params.str("method").orEmpty() else method
        requests.add(inner to (if (method == "session.call") params.obj("params") ?: EMPTY_OBJECT else params))
        val answer: JsonElement = when (inner) {
            "ide.initialize" -> jo("bridge_version" to "9.9", "protocol_version" to 1)
            "ide.status" -> jo("findings" to emptyList<Any>(), "locked" to false)
            "ide.sessions" -> jo(
                "running" to listOf(
                    mapOf("id" to "s-1", "name" to "parser work", "directory" to "/w", "state" to "idle", "open_escalations" to 0, "here" to true, "stale" to false),
                ),
            )
            "ide.attach" -> attachResult(
                "s-1",
                if (!conversation) emptyList() else listOf(
                    ev("user.input", "text" to "first", "uuid" to "u-1", "seq" to 1), ev("turn.result", "subtype" to "success", "seq" to 2),
                    ev("user.input", "text" to "second thoughts", "uuid" to "u-2", "seq" to 3),
                ),
            ).let { if (conversation) it else JsonObject(it + ("snapshot" to jo("state" to "idle", "workspace" to "/w"))) }
            "ide.start" -> if (server) attachResult("s-2", emptyList()) else throw ScriptedError(-32013, "no calu server answered")
            "session.history" -> jo(
                "turns" to listOf(mapOf("index" to 4, "prompt_uuid" to "u-1"), mapOf("index" to 5, "prompt_uuid" to "u-2")),
                "steps" to emptyList<Any>(),
            )
            "chat.send" -> if (params.obj("params")?.str("text") == "fail") throw ScriptedError(-32000, "the session is stopping") else EMPTY_OBJECT
            "side.ask" -> jo("answer" to "It parses.")
            "honesty.review" -> jo("ran" to false, "problem" to "honesty is off")
            else -> EMPTY_OBJECT
        }
        answer
    }

    private fun attached(server: Boolean = true, conversation: Boolean = true): ChatController = runBlocking {
        val s = BridgeSupervisor(ScriptedLauncher { handler(server, conversation) }, scope, "test", { _, _ -> null }, delayMs = { 20 })
        supervisors.add(s)
        val c = ChatController(s, scope)
        c.start()
        waitUntil("attached") { c.state.link.status == LinkStatus.ATTACHED && c.state.sessions.list.isNotEmpty() }
        c
    }

    private fun starts() = requests.filter { it.first == "ide.start" }.map { it.second }

    private fun notices(c: ChatController) = c.state.items[MAIN].orEmpty().filterIsInstance<NoticeItem>().map { it.text }

    @Test
    fun `rewind resumes the conversation before the recorded turn of the message and offers its text again`() = runBlocking {
        val c = attached()
        val restored = CopyOnWriteArrayList<String>()
        val subscribed = java.util.concurrent.atomic.AtomicBoolean()
        val job = scope.launch {
            c.updates().collect { u ->
                if (u is ChatUpdate.Snapshot) subscribed.set(true)
                if (u is ChatUpdate.Event && u.event.method == "ui.restoreInput") restored.add(u.event.params.str("text").orEmpty())
            }
        }
        waitUntil("the subscription") { subscribed.get() }
        // The view counted the message as turn 2; the record says it is the conversation's turn 5.
        assertEquals(true, c.act("rewind", jo("turn" to 2, "uuid" to "u-2")))
        assertEquals(
            listOf(jo("kind" to "resume", "claude_session_id" to "c-1", "fork_before_turn" to 5, "name" to "rewind of parser work")),
            starts(),
        )
        assertEquals("s-2", c.state.session.sessionId)
        assertNull(c.state.starting)
        waitUntil("the text for the input") { restored.toList() == listOf("second thoughts") }
        job.cancel()
    }

    @Test
    fun `fork from a message starts the same request under the name of a fork, without restoring the input`() = runBlocking {
        val c = attached()
        assertEquals(true, c.act("forkAt", jo("turn" to 2, "uuid" to "u-2")))
        assertEquals(
            listOf(jo("kind" to "resume", "claude_session_id" to "c-1", "fork_before_turn" to 5, "name" to "fork of parser work")),
            starts(),
        )
        assertEquals("s-2", c.state.session.sessionId)
    }

    @Test
    fun `without a uuid the view's turn number is used, and the first turn is refused`() = runBlocking {
        val c = attached()
        assertTrue(c.forkAt(2))
        assertEquals(2, starts().single()["fork_before_turn"]!!.let { (it as kotlinx.serialization.json.JsonPrimitive).content.toInt() })
        requests.clear()
        waitUntil("attached to the new session") { c.state.session.sessionId == "s-2" }
        assertFalse(c.rewind(1))
        assertEquals(emptyList<JsonObject>(), starts())
        assertTrue(notices(c).last().contains("first message of the conversation"))
        assertEquals(false, c.act("rewind", jo("uuid" to "u-2")))
    }

    @Test
    fun `fork of the whole conversation, and nothing without a conversation`() = runBlocking {
        val c = attached()
        assertEquals(true, c.act("forkConversation"))
        assertEquals(listOf(jo("kind" to "fork", "claude_session_id" to "c-1", "name" to "fork of parser work")), starts())

        requests.clear()
        val empty = attached(conversation = false)
        assertFalse(empty.forkConversation())
        assertFalse(empty.rewind(2, "u-2"))
        assertEquals(emptyList<JsonObject>(), starts())
        assertTrue(notices(empty).all { it == "Attach to a session with a conversation first." })
        assertEquals(2, notices(empty).size)
    }

    @Test
    fun `a rewind that no server answers leaves the session attached and says so`() = runBlocking {
        val c = attached(server = false)
        assertFalse(c.rewind(2, "u-2"))
        assertEquals("s-1", c.state.session.sessionId)
        assertNull(c.state.starting)
        assertTrue(notices(c).last().startsWith("No calu server answered"))
    }

    @Test
    fun `a side question is recorded with its answer, honesty requests reach the session`() = runBlocking {
        val c = attached()
        assertEquals(true, c.act("sideAsk", jo("question" to "What does it do?")))
        assertEquals("It parses.", c.state.side.single().answer)
        assertEquals(jo("question" to "What does it do?", "history" to emptyList<Any>()), requests.last { it.first == "side.ask" }.second)
        assertTrue(c.sideAsk("And then?"))
        assertEquals(
            listOf(mapOf("question" to "What does it do?", "answer" to "It parses.")).let { jo("question" to "And then?", "history" to it) },
            requests.last { it.first == "side.ask" }.second,
        )

        assertEquals(true, c.act("honestyReview"))
        assertEquals("No honesty review ran: honesty is off", notices(c).last())
        assertEquals(true, c.act("honestyClear", jo("mark_id" to 7)))
        assertEquals(jo("mark_id" to 7), requests.last { it.first == "honesty.clear" }.second)
        assertEquals(true, c.act("honestyTrust"))
        assertEquals(jo("rev" to "HEAD"), requests.last { it.first == "honesty.trust" }.second)
        assertEquals(true, c.act("honestyTrust", jo("checkout" to "agent-a1", "rev" to "main")))
        assertEquals(jo("rev" to "main", "checkout" to "agent-a1"), requests.last { it.first == "honesty.trust" }.second)
        assertEquals(true, c.act("honestyTrust", jo("path" to "src/x.py")))
        assertEquals(jo("path" to "src/x.py"), requests.last { it.first == "honesty.trust" }.second)
        assertNull(c.act("openDiff", jo("tool_use_id" to "t")))
    }

    @Test
    fun `send carries images as base64 with their media type, to the main agent only`() = runBlocking {
        val c = attached()
        val png = io.github.kadeng.calu.logic.outgoingImage("image/png", byteArrayOf(1, 2, 3))
        assertTrue(c.sendMessage("look at this", inject = false, tab = MAIN, images = listOf(png)))
        assertEquals(
            jo("text" to "look at this", "delivery" to "queued", "to" to "main", "images" to listOf(mapOf("data" to "AQID", "media_type" to "image/png"))),
            requests.last { it.first == "chat.send" }.second,
        )
        // An image alone is a message; the action takes the images as JSON.
        assertEquals(true, c.act("send", jo("text" to "", "inject" to true, "tab" to MAIN, "images" to listOf(mapOf("data" to "AQID", "mediaType" to "image/gif")))))
        assertEquals(
            jo("text" to "", "delivery" to "inject", "to" to "main", "images" to listOf(mapOf("data" to "AQID", "media_type" to "image/gif"))),
            requests.last { it.first == "chat.send" }.second,
        )
        // A message without images has no `images` member.
        assertTrue(c.send("plain", inject = false, tab = MAIN))
        assertEquals(jo("text" to "plain", "delivery" to "queued", "to" to "main"), requests.last { it.first == "chat.send" }.second)

        val sent = requests.count { it.first == "chat.send" }
        assertFalse(c.sendMessage("", inject = false, tab = MAIN, images = emptyList()))
        assertFalse(c.sendMessage("x", inject = false, tab = MAIN, images = listOf(png.copy(mediaType = "image/svg+xml"))))
        assertEquals("Only PNG, JPEG, GIF and WebP images can be attached.", notices(c).last())
        assertFalse(c.sendMessage("x", inject = false, tab = MAIN, images = List(5) { png }))
        assertEquals("At most 4 images fit into one message.", notices(c).last())
        assertEquals(false, c.act("send", jo("text" to "x", "images" to "nope")))
        assertEquals(sent, requests.count { it.first == "chat.send" })
    }

    @Test
    fun `a message the session refuses is offered to the input again`() = runBlocking {
        val c = attached()
        val restored = CopyOnWriteArrayList<String>()
        val subscribed = java.util.concurrent.atomic.AtomicBoolean()
        val job = scope.launch {
            c.updates().collect { u ->
                if (u is ChatUpdate.Snapshot) subscribed.set(true)
                if (u is ChatUpdate.Event && u.event.method == "ui.restoreInput") restored.add(u.event.params.str("text").orEmpty())
            }
        }
        waitUntil("the subscription") { subscribed.get() }
        assertFalse(c.send("fail", inject = false, tab = MAIN))
        assertTrue(notices(c).last().startsWith("Sending the message failed"))
        waitUntil("the text for the input") { restored.toList() == listOf("fail") }
        job.cancel()
    }

    @Test
    fun `a comment on a selection is sent as calu's comment request, for a file and for a diff`() = runBlocking {
        val c = attached()
        val file = io.github.kadeng.calu.logic.CommentInput(
            "src/a.py", io.github.kadeng.calu.logic.PlainSelection.of(1, 0, 3, 0), "  explain  ", fileText = "l1\nl2\nl3\n",
        )
        assertTrue(c.comment(file))
        assertEquals(
            jo("kind" to "file", "path" to "src/a.py", "side" to "after", "start_line" to 2, "end_line" to 3, "after_text" to "l1\nl2\nl3\n", "text" to "explain"),
            requests.last { it.first == "comment.add" }.second,
        )
        val diff = jo(
            "kind" to "diff", "path" to "a.py", "side" to "before", "start_line" to 1, "end_line" to 1,
            "before_text" to "old\n", "after_text" to "new\n", "ref" to "edit:t-0", "text" to "why removed?",
        )
        assertEquals(true, c.act("comment", diff))
        assertEquals(diff, requests.last { it.first == "comment.add" }.second)

        val sent = requests.count { it.first == "comment.add" }
        assertFalse(c.comment(file.copy(text = "  ")))
        assertEquals("The comment was not sent: The comment is empty.", notices(c).last())
        assertEquals(false, c.act("comment", jo("path" to "a.py", "text" to "x")))
        assertEquals(sent, requests.count { it.first == "comment.add" })
    }
}
