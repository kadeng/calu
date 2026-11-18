/**
 * A scripted bridge inside this JVM: the seam for tests and for a demo without calu.
 *
 *     supervisor ──stdin pipe──► ScriptedBridge thread ──handler(method, params)──► result / error
 *                ◄─stdout pipe── responses, and notifications sent with `notify`
 */
package io.github.kadeng.calu.bridge

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.io.InputStream
import java.io.OutputStream
import java.io.PipedInputStream
import java.io.PipedOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * An error answer of a scripted bridge.
 *
 * @property code The JSON-RPC error code.
 * @param message The error message.
 * @property data The error's `data` member, or `null`.
 */
class ScriptedError(val code: Int, message: String, val data: JsonElement? = null) : Exception(message)

/**
 * Answers the requests of one scripted bridge process.
 *
 * Called on the bridge's own thread, one request at a time.
 */
fun interface ScriptedHandler {
    /**
     * Answer one request.
     *
     * @param bridge The running scripted bridge (to send notifications or to exit).
     * @param method The request's method.
     * @param params The request's params.
     * @return The result.
     * @throws ScriptedError to answer with an error.
     */
    fun handle(bridge: ScriptedBridge, method: String, params: JsonObject): JsonElement
}

/**
 * One running scripted bridge. Reads requests until its stdin closes or `exit` is called.
 *
 * @param handler Answers the requests.
 */
class ScriptedBridge(private val handler: ScriptedHandler) : BridgeProcess {
    private val toBridge = PipedOutputStream()
    private val bridgeIn = PipedInputStream(toBridge, 1 shl 16)
    private val bridgeOut = PipedOutputStream()
    private val fromBridge = PipedInputStream(bridgeOut, 1 shl 20)
    private val errOut = PipedOutputStream()
    private val errIn = PipedInputStream(errOut, 1 shl 16)
    private val ended = CountDownLatch(1)
    private val writeLock = Any()

    @Volatile
    private var exitCode = 0

    override val stdin: OutputStream get() = toBridge
    override val stdout: InputStream get() = fromBridge
    override val stderr: InputStream get() = errIn

    private val thread = Thread({ run() }, "calu-scripted-bridge").apply { isDaemon = true }

    init {
        thread.start()
    }

    /**
     * Send a notification to the client.
     *
     * @param method The notification's method, e.g. "session.event".
     * @param params Its params.
     */
    fun notify(method: String, params: JsonObject) {
        writeLine(buildJsonObject {
            put("jsonrpc", JsonPrimitive("2.0"))
            put("method", JsonPrimitive(method))
            put("params", params)
        })
    }

    /**
     * Write text to the bridge's stderr.
     *
     * @param text The text.
     */
    fun stderrWrite(text: String) {
        try {
            errOut.write(text.toByteArray())
            errOut.flush()
        } catch (_: Exception) {
        }
    }

    /**
     * End the scripted process as if it had exited by itself.
     *
     * @param code The exit code to report.
     */
    fun exit(code: Int) {
        exitCode = code
        finish()
    }

    override fun waitFor(timeoutMs: Long): Int? {
        if (timeoutMs <= 0) ended.await() else if (!ended.await(timeoutMs, TimeUnit.MILLISECONDS)) return null
        return exitCode
    }

    override fun destroy(force: Boolean) {
        if (ended.count > 0) exitCode = 143
        finish()
    }

    /** Close the pipes and mark the process as ended. Idempotent. */
    private fun finish() {
        for (c in listOf(bridgeOut, errOut, bridgeIn)) {
            try {
                c.close()
            } catch (_: Exception) {
            }
        }
        ended.countDown()
    }

    /**
     * Write one message as a line to the client.
     *
     * @param message The JSON-RPC message.
     */
    private fun writeLine(message: JsonObject) {
        synchronized(writeLock) {
            try {
                bridgeOut.write((CaluJson.encodeToString(JsonObject.serializer(), message) + "\n").toByteArray())
                bridgeOut.flush()
            } catch (_: Exception) {
            }
        }
    }

    /** The bridge's loop: read a line, call the handler, write the answer. */
    private fun run() {
        try {
            bridgeIn.bufferedReader().forEachLine { line ->
                val m = parseObjectOrNull(line) ?: return@forEachLine
                val id = m["id"] ?: return@forEachLine
                val method = m.str("method") ?: return@forEachLine
                val answer = try {
                    val result = handler.handle(this, method, m.obj("params") ?: EMPTY_OBJECT)
                    buildJsonObject {
                        put("jsonrpc", JsonPrimitive("2.0")); put("id", id); put("result", result)
                    }
                } catch (e: ScriptedError) {
                    buildJsonObject {
                        put("jsonrpc", JsonPrimitive("2.0")); put("id", id)
                        put("error", buildJsonObject {
                            put("code", JsonPrimitive(e.code)); put("message", JsonPrimitive(e.message ?: ""))
                            e.data?.let { put("data", it) }
                        })
                    }
                }
                writeLine(answer)
                if (method == "ide.shutdown") exit(0)
            }
        } catch (_: Exception) {
        } finally {
            finish()
        }
    }
}

/**
 * Starts scripted bridges.
 *
 * @param handlerFor Creates the handler of launch number `n` (0 for the first); a new process
 *   therefore starts with new state, as a real bridge does.
 * @property reconnects Whether the supervisor starts a new one after an unexpected end.
 */
class ScriptedLauncher(
    override val reconnects: Boolean = false,
    private val handlerFor: (Int) -> ScriptedHandler,
) : BridgeLauncher {
    override val kind: String = "scripted"

    /** Every bridge started so far, oldest first (for tests). */
    val launched: MutableList<ScriptedBridge> = java.util.concurrent.CopyOnWriteArrayList()

    override fun describe(): String = "scripted bridge"

    override fun launch(): BridgeProcess {
        val bridge = ScriptedBridge(handlerFor(launched.size))
        launched.add(bridge)
        return bridge
    }
}
