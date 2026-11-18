package io.github.kadeng.calu.rpc

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.supervisorScope
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests of the JSON-RPC client, ported from `extensions/vscode/test/unit/rpc.test.ts`.
 *
 * `RpcClient.request` suspends until its answer arrives. The tests start each request with
 * `async(start = CoroutineStart.UNDISPATCHED)`, which runs the request up to its first suspension
 * in the calling thread: the line is written before the next statement of the test (`feed`,
 * `close`) runs.
 */
class RpcTest {
    /**
     * A client whose written lines, received events and protocol errors are recorded.
     *
     * @property written The lines the client wrote, in order.
     * @property events The notifications the client delivered, in order.
     * @property errors The protocol errors the client reported, in order.
     */
    private class Setup {
        val written = mutableListOf<String>()
        val events = mutableListOf<RpcEvent>()
        val errors = mutableListOf<String>()

        /** The client under test. */
        val client = RpcClient({ written.add(it) }, { events.add(it) }, { errors.add(it) })
    }

    /**
     * Parse a JSON text.
     *
     * @param text Any JSON text.
     * @return The JSON value.
     */
    private fun parse(text: String): JsonElement = CaluJson.parseToJsonElement(text)

    /**
     * Start a request so that its line is written before this function returns.
     *
     * @param client The client.
     * @param method The request method.
     * @param params The request params.
     * @return The pending answer; `await` returns the result or throws what `request` throws.
     */
    private fun CoroutineScope.send(
        client: RpcClient,
        method: String,
        params: JsonObject = EMPTY_OBJECT,
    ): Deferred<JsonElement> = async(start = CoroutineStart.UNDISPATCHED) { client.request(method, params) }

    /**
     * Wait for a request that must fail.
     *
     * @param answer The pending answer of the request.
     * @return The exception the request failed with.
     * @throws AssertionError if the request succeeded.
     */
    private suspend fun failure(answer: Deferred<JsonElement>): Throwable =
        runCatching { answer.await() }.exceptionOrNull() ?: throw AssertionError("the request did not fail")

    // ------------------------------------------------------------------ LineSplitter

    @Test
    fun `returns complete lines and buffers the rest`() {
        val s = LineSplitter()
        assertEquals(listOf("a"), s.push("a\nb"))
        assertEquals(listOf("bc", ""), s.push("c\r\n\nd"))
        assertEquals("d", s.flush())
        assertEquals("", s.flush())
    }

    // ------------------------------------------------------------------ RpcClient

    @Test
    fun `writes one NDJSON line per request with increasing ids`(): Unit = runBlocking {
        supervisorScope {
            val t = Setup()
            val a = send(t.client, "initialize", parse("""{"client":"t"}""") as JsonObject)
            val b = send(t.client, "chat.send", parse("""{"text":"hi"}""") as JsonObject)
            assertEquals(2, t.written.size)
            assertTrue(t.written.all { it.endsWith("\n") && !it.dropLast(1).contains("\n") })
            assertEquals(
                parse("""{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"client":"t"}}"""),
                parse(t.written[0]),
            )
            assertEquals(JsonPrimitive(2), (parse(t.written[1]) as JsonObject)["id"])
            // End the two requests, which otherwise keep `runBlocking` waiting.
            t.client.close("test ends")
            assertTrue(failure(a) is RpcClosedException)
            assertTrue(failure(b) is RpcClosedException)
        }
    }

    @Test
    fun `resolves responses split across chunks, in any order`(): Unit = runBlocking {
        val t = Setup()
        val a = send(t.client, "a")
        val b = send(t.client, "b")
        t.client.feed("""{"jsonrpc":"2.0","id":2,"res""")
        t.client.feed("ult\":{\"x\":2}}\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"x\":1}}\n")
        assertEquals(parse("""{"x":1}"""), a.await())
        assertEquals(parse("""{"x":2}"""), b.await())
        assertEquals(0, t.client.pendingCount)
    }

    @Test
    fun `rejects with RpcRequestException for error answers`(): Unit = runBlocking {
        supervisorScope {
            val t = Setup()
            val p = send(t.client, "chat.send", parse("""{"text":"x"}""") as JsonObject)
            t.client.feed("{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32000,\"message\":\"no session\"}}\n")
            val error = failure(p)
            assertTrue(error is RpcRequestException)
            assertEquals(-32000, (error as RpcRequestException).code)
            assertEquals("chat.send", error.method)
            assertTrue(error.message!!.contains("no session"))
        }
    }

    @Test
    fun `delivers notifications in order and reports garbage lines`() {
        val t = Setup()
        t.client.feed("{\"jsonrpc\":\"2.0\",\"method\":\"log\",\"params\":{\"message\":\"m\"}}\nnot json\n")
        t.client.feed("{\"jsonrpc\":\"2.0\",\"method\":\"session.state\"}\n")
        assertEquals(
            listOf(
                RpcEvent("log", parse("""{"message":"m"}""") as JsonObject),
                RpcEvent("session.state", EMPTY_OBJECT),
            ),
            t.events,
        )
        assertEquals(1, t.errors.size)
    }

    @Test
    fun `rejects pending and later requests after close`(): Unit = runBlocking {
        supervisorScope {
            val t = Setup()
            val p = send(t.client, "session.start")
            t.client.close("bridge exited with code 1")
            val pending = failure(p)
            assertTrue(pending is RpcClosedException)
            assertTrue(pending.message!!.contains("bridge exited with code 1"))
            val later = failure(send(t.client, "x"))
            assertTrue(later is RpcClosedException)
            assertTrue(t.client.closed)
        }
    }

    @Test
    fun `processes a final unterminated line on close`(): Unit = runBlocking {
        val t = Setup()
        val p = send(t.client, "a")
        t.client.feed("""{"jsonrpc":"2.0","id":1,"result":7}""")
        t.client.close("eof")
        assertEquals(JsonPrimitive(7), p.await())
    }

    @Test
    fun `rejects when write throws`(): Unit = runBlocking {
        supervisorScope {
            val client = RpcClient({ throw IllegalStateException("EPIPE") }, {})
            val error = failure(send(client, "a"))
            assertTrue(error.message!!.contains("EPIPE"))
            assertEquals(0, client.pendingCount)
        }
    }
}
