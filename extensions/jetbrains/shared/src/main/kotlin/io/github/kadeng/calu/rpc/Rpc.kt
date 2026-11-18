/**
 * JSON-RPC 2.0 client over NDJSON (one JSON object per line), as spoken by `calu ide`.
 *
 * No IntelliJ Platform and no process dependency, so it is unit-tested alone. The caller connects
 * it to a transport:
 *
 *     plugin --write(line)--> bridge stdin
 *     bridge stdout --chunks--> feed(chunk) --> responses complete request() calls
 *                                         --> notifications go to the onEvent listener
 *     process exit --> close(reason) --> all pending requests fail with RpcClosedException
 *
 * Ported from `extensions/vscode/src/rpc.ts`.
 */
package io.github.kadeng.calu.rpc

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.asLong
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import kotlinx.coroutines.CompletableDeferred
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * An error answer of the bridge (`{"error": {...}}`) for one request.
 *
 * @property method The request method that failed.
 * @property code JSON-RPC error code, e.g. -32602 for invalid params; -32603 if the answer had none.
 * @property bridgeMessage The bridge's message without method and code; "unknown error" if none.
 * @property data The `data` member of the error (e.g. validation details), or `null`.
 */
class RpcRequestException(
    val method: String,
    val code: Int,
    val bridgeMessage: String,
    val data: JsonElement?,
) : Exception("$method: $bridgeMessage (code $code)") {
    companion object {
        /**
         * Create the exception from the `error` member of a JSON-RPC response.
         *
         * @param method The request method that failed.
         * @param error The `error` member; anything but an object counts as an empty object.
         * @return The exception; a missing `code` is -32603, a missing `message` "unknown error".
         */
        fun of(method: String, error: JsonElement?): RpcRequestException {
            val e = error as? JsonObject ?: EMPTY_OBJECT
            return RpcRequestException(method, e.int("code") ?: -32603, e.str("message") ?: "unknown error", e["data"])
        }
    }
}

/**
 * The connection was closed (the bridge exited) before a response arrived, or after `close()`.
 *
 * @param reason Human-readable reason, e.g. "calu ide exited with code 1".
 */
class RpcClosedException(reason: String) : Exception(reason)

/**
 * A notification from the bridge: `{"method": ..., "params": ...}` without an id.
 *
 * @property method The notification's method, e.g. "session.event".
 * @property params Its params; an empty object if the message had none or another JSON type.
 */
data class RpcEvent(val method: String, val params: JsonObject)

/**
 * Splits a stream of text chunks into complete lines.
 *
 * Invariant: `push` returns each complete line exactly once, in order, without the "\n" (and
 * without a trailing "\r"); an incomplete last line stays buffered until its "\n" arrives. Not
 * thread-safe: one reader thread calls it.
 */
class LineSplitter {
    private val buffer = StringBuilder()

    /**
     * Append a chunk and return the lines it completed.
     *
     * @param chunk Any piece of the stream (zero, one or many newlines).
     * @return The completed lines, possibly empty. Blank lines are returned as "".
     */
    fun push(chunk: String): List<String> {
        buffer.append(chunk)
        val parts = buffer.split('\n')
        buffer.setLength(0)
        buffer.append(parts.last())
        return parts.dropLast(1).map { it.removeSuffix("\r") }
    }

    /**
     * Return and clear the buffered incomplete line (used at the end of the stream).
     *
     * @return The rest of the buffer; "" if nothing is buffered.
     */
    fun flush(): String {
        val rest = buffer.toString()
        buffer.setLength(0)
        return rest
    }
}

/**
 * Client side of the protocol. Create it with a `write` function, call `feed` with every chunk of
 * the bridge's stdout (from one thread) and `close` when the bridge exits. `request` may be called
 * from any thread or coroutine.
 *
 * @param write Sends one complete line (the "\n" is included) to the bridge's stdin. It may throw;
 *   the exception then fails the request that tried to send. Calls are serialized by this class.
 * @param onEvent Called on the feeding thread, in arrival order, for each notification.
 * @param onProtocolError Called for lines that are not valid JSON-RPC (e.g. stray prints); such
 *   lines are otherwise ignored.
 */
class RpcClient(
    private val write: (String) -> Unit,
    private val onEvent: (RpcEvent) -> Unit,
    private val onProtocolError: (String) -> Unit = {},
) {
    private class Pending(val method: String, val answer: CompletableDeferred<JsonElement>)

    private val lock = Any()
    private var nextId = 1L
    private val pending = HashMap<Long, Pending>()
    private val splitter = LineSplitter()
    private var closedReason: String? = null

    /** True once `close` was called. No requests can be sent afterwards. */
    val closed: Boolean get() = synchronized(lock) { closedReason != null }

    /** Number of requests still waiting for a response (for tests and diagnostics). */
    val pendingCount: Int get() = synchronized(lock) { pending.size }

    /**
     * Send a request and wait for its response.
     *
     * @param method The JSON-RPC method, e.g. "ide.status".
     * @param params The params object (sent as `{}` by default).
     * @return The response's `result` member (`JsonNull` if the response has none).
     * @throws RpcRequestException if the bridge answers with an error.
     * @throws RpcClosedException if the connection is closed or closes before the answer arrives.
     * @throws Exception whatever `write` throws.
     */
    suspend fun request(method: String, params: JsonObject = EMPTY_OBJECT): JsonElement {
        val answer = CompletableDeferred<JsonElement>()
        synchronized(lock) {
            closedReason?.let { throw RpcClosedException(it) }
            val id = nextId++
            pending[id] = Pending(method, answer)
            val message = buildJsonObject {
                put("jsonrpc", JsonPrimitive("2.0"))
                put("id", JsonPrimitive(id))
                put("method", JsonPrimitive(method))
                put("params", params)
            }
            try {
                write(CaluJson.encodeToString(JsonObject.serializer(), message) + "\n")
            } catch (error: Exception) {
                pending.remove(id)
                throw error
            }
        }
        return answer.await()
    }

    /**
     * Process a chunk of the bridge's stdout: complete requests and dispatch notifications for
     * every complete line in it.
     *
     * @param chunk Text received from stdout (any split; partial lines are buffered).
     * @throws Exception what `onEvent` throws, after the line that caused it; later lines of the
     *   same chunk are then not processed.
     */
    fun feed(chunk: String) {
        for (line in splitter.push(chunk)) handleLine(line)
    }

    /**
     * Mark the connection as closed and fail all pending requests with `RpcClosedException`.
     * A buffered last line without newline is processed first. Idempotent.
     *
     * @param reason Text for the exceptions, e.g. "calu ide exited with code 1".
     */
    fun close(reason: String) {
        if (closed) return
        val rest = splitter.flush()
        if (rest.isNotBlank()) handleLine(rest)
        val waiting: List<Pending>
        synchronized(lock) {
            if (closedReason != null) return
            closedReason = reason
            waiting = pending.values.toList()
            pending.clear()
        }
        for (p in waiting) p.answer.completeExceptionally(RpcClosedException("${p.method}: $reason"))
    }

    /**
     * Handle one complete line (see `feed`).
     *
     * @param line One line without newline.
     */
    private fun handleLine(line: String) {
        if (line.isBlank()) return
        val parsed: JsonElement = try {
            CaluJson.parseToJsonElement(line)
        } catch (_: Exception) {
            onProtocolError("not JSON: ${line.take(200)}")
            return
        }
        val m = parsed as? JsonObject
        if (m == null) {
            onProtocolError("not a JSON object: ${line.take(200)}")
            return
        }
        val id = m["id"].asLong()
        val method = m.str("method")
        if (id != null && m["id"] is JsonPrimitive && !(m["id"] as JsonPrimitive).isString && ("result" in m || "error" in m)) {
            val p = synchronized(lock) { pending.remove(id) }
            if (p == null) {
                onProtocolError("response for unknown id $id")
                return
            }
            val error = m["error"]
            if (error != null && error !is JsonNull) {
                p.answer.completeExceptionally(RpcRequestException.of(p.method, error))
            } else {
                p.answer.complete(m["result"] ?: JsonNull)
            }
        } else if (method != null) {
            onEvent(RpcEvent(method, m.obj("params") ?: EMPTY_OBJECT))
        } else if ("error" in m) {
            // An error without a usable id, e.g. a parse error of a line we sent.
            onProtocolError("error without id: ${line.take(200)}")
        } else {
            onProtocolError("unrecognised message: ${line.take(200)}")
        }
    }
}
