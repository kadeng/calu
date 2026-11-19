package io.github.kadeng.calu.sync

import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.long
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import io.github.kadeng.calu.rpc.RpcRequestException
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.util.Base64

/**
 * The `ide.fs.*` requests over a map in memory, with the bridge's error codes, small limits (so
 * that paging and chunking happen with a few bytes) and failures a test can plan. Not thread-safe.
 *
 * @param initial the remote files: relative path to text.
 * @property pageEntries entries per manifest page.
 * @property requestBytes raw bytes per read piece, per `ide.fs.write` and per chunk.
 * @property fileBytes the per-file limit.
 */
class FakeFsBridge(
    initial: Map<String, String> = emptyMap(),
    val pageEntries: Int = 2,
    val requestBytes: Int = 8,
    val fileBytes: Long = 1000,
) {
    /** The remote files. */
    val files = java.util.TreeMap<String, ByteArray>(initial.mapValues { it.value.toByteArray() })

    /** Paths the manifest reports as skipped, with the reason. */
    val skipped = LinkedHashMap<String, String>()

    /** Files the manifest leaves out without a word, as git does for an ignored file: not listed, not skipped, not missing. */
    val unlisted = HashSet<String>()

    /** Paths a write, delete or rename refuses with -32035. */
    val held = HashSet<String>()

    /** Every request's method, in order. */
    val log = ArrayList<String>()

    /** Planned failures: the next request of the method throws the exception and the entry is removed. */
    val failNext = HashMap<String, ArrayDeque<Exception>>()

    /** Runs before the request with this number of the log (0-based), e.g. to change a file between two pages. */
    val before = HashMap<Int, () -> Unit>()

    /** True while the bridge answers; false makes every request fail as a closed connection. */
    var online = true

    /** The commit the manifest reports. */
    var head: String? = "a".repeat(40)

    private val uploads = HashMap<String, Triple<JsonObject, java.io.ByteArrayOutputStream, Int>>()
    private var listing: List<String>? = null
    private var listingHead: String? = null
    private var nextUpload = 1
    private var seq = 0L

    /** Number of uploads that were begun and neither committed nor aborted. */
    val openUploads: Int get() = uploads.size

    /**
     * Read a remote file as text.
     *
     * @param path relative path.
     * @return the text; null if there is no file.
     */
    fun text(path: String): String? = files[path]?.toString(Charsets.UTF_8)

    /**
     * Build an error answer.
     *
     * @param method the request method.
     * @param code the error code.
     * @param data members of `data`.
     * @return the exception the supervisor would throw.
     */
    fun error(method: String, code: Int, vararg data: Pair<String, JsonElement>): RpcRequestException =
        RpcRequestException(method, code, "refused", JsonObject(data.toMap()))

    /** The function to hand to [RemoteFileStore]. */
    val call: BridgeCall = { method, params -> handle(method, params) }

    /**
     * Answer one request.
     *
     * @param method the method.
     * @param p the params.
     * @return the result.
     * @throws RpcRequestException with the bridge's code for a refusal.
     * @throws IllegalStateException while [online] is false.
     */
    private fun handle(method: String, p: JsonObject): JsonElement {
        before.remove(log.size)?.invoke()
        log.add(method)
        if (!online) throw IllegalStateException("The calu bridge is not running.")
        failNext[method]?.removeFirstOrNull()?.let { throw it }
        return when (method) {
            "ide.paths" -> buildJsonObject {
                put("root", JsonPrimitive("/srv/project")); put("home", JsonPrimitive("/home/dev")); put("git", JsonPrimitive(true))
                put("head", head?.let { JsonPrimitive(it) } ?: JsonNull); put("case_sensitive", JsonPrimitive(true))
                put("limits", buildJsonObject {
                    put("file_bytes", JsonPrimitive(fileBytes)); put("request_bytes", JsonPrimitive(requestBytes))
                    put("page_entries", JsonPrimitive(pageEntries)); put("uploads", JsonPrimitive(4))
                })
            }
            "ide.fs.manifest" -> manifest(p)
            "ide.fs.read" -> {
                val path = p.str("path")!!
                val bytes = files[path] ?: throw error(method, FsError.NOT_FOUND)
                val offset = (p.long("offset") ?: 0).toInt()
                val end = minOf(bytes.size, offset + minOf(p.int("length") ?: requestBytes, requestBytes))
                buildJsonObject {
                    put("content_b64", JsonPrimitive(Base64.getEncoder().encodeToString(bytes.copyOfRange(minOf(offset, bytes.size), end))))
                    put("sha256", JsonPrimitive(sha256Hex(bytes))); put("size", JsonPrimitive(bytes.size)); put("mode", JsonPrimitive(420))
                    put("eof", JsonPrimitive(end >= bytes.size))
                }
            }
            "ide.fs.write" -> {
                val bytes = Base64.getDecoder().decode(p.str("content_b64")!!)
                if (bytes.size > requestBytes) throw error(method, FsError.TOO_LARGE, "size" to JsonPrimitive(bytes.size), "limit" to JsonPrimitive(requestBytes))
                check(method, p.str("path")!!, p, bytes.size.toLong())
                files[p.str("path")!!] = bytes
                buildJsonObject { put("sha256", JsonPrimitive(sha256Hex(bytes))); put("size", JsonPrimitive(bytes.size)) }
            }
            "ide.fs.write.begin" -> {
                check(method, p.str("path")!!, p, p.long("size")!!)
                val id = "u${nextUpload++}"
                uploads[id] = Triple(p, java.io.ByteArrayOutputStream(), 0)
                buildJsonObject { put("upload_id", JsonPrimitive(id)); put("chunk_size", JsonPrimitive(requestBytes)) }
            }
            "ide.fs.write.chunk" -> {
                val u = uploads[p.str("upload_id")] ?: throw error(method, FsError.UPLOAD_FAILED, "reason" to JsonPrimitive("unknown-upload"))
                if (p.long("offset") != u.second.size().toLong()) throw error(method, FsError.UPLOAD_FAILED, "reason" to JsonPrimitive("offset"))
                u.second.write(Base64.getDecoder().decode(p.str("content_b64")!!))
                buildJsonObject { put("received", JsonPrimitive(u.second.size())) }
            }
            "ide.fs.write.commit" -> {
                val u = uploads.remove(p.str("upload_id")) ?: throw error(method, FsError.UPLOAD_FAILED, "reason" to JsonPrimitive("unknown-upload"))
                val bytes = u.second.toByteArray()
                if (sha256Hex(bytes) != u.first.str("sha256")) throw error(method, FsError.UPLOAD_FAILED, "reason" to JsonPrimitive("sha256"))
                check(method, u.first.str("path")!!, u.first, bytes.size.toLong())
                files[u.first.str("path")!!] = bytes
                buildJsonObject { put("sha256", JsonPrimitive(sha256Hex(bytes))); put("size", JsonPrimitive(bytes.size)) }
            }
            "ide.fs.write.abort" -> { uploads.remove(p.str("upload_id")); JsonObject(emptyMap()) }
            "ide.fs.delete" -> {
                check(method, p.str("path")!!, p, 0)
                files.remove(p.str("path")!!)
                JsonObject(emptyMap())
            }
            "ide.fs.rename" -> {
                val from = p.str("from")!!
                val to = p.str("to")!!
                if (from !in files) throw error(method, FsError.NOT_FOUND)
                if (to in files) throw error(method, FsError.TARGET_EXISTS, "sha256" to JsonPrimitive(sha256Hex(files[to]!!)))
                check(method, from, p, 0)
                files[to] = files.remove(from)!!
                JsonObject(emptyMap())
            }
            "ide.fs.subscribe" -> buildJsonObject { put("epoch", JsonPrimitive("e1")); put("seq", JsonPrimitive(seq)); put("complete", JsonPrimitive(p["after_seq"] == null)) }
            "ide.fs.unsubscribe" -> JsonObject(emptyMap())
            else -> throw RpcRequestException(method, -32601, "unknown method", null)
        }
    }

    /**
     * The checks of a write, delete or rename: hold, size limit, base.
     *
     * @param method the method, for the error.
     * @param path the target.
     * @param p the params with `base_sha256` and `during_turn`.
     * @param size size of the new content.
     */
    private fun check(method: String, path: String, p: JsonObject, size: Long) {
        if (size > fileBytes) throw error(method, FsError.TOO_LARGE, "size" to JsonPrimitive(size), "limit" to JsonPrimitive(fileBytes))
        if (path in held && !p.bool("during_turn")) throw error(method, FsError.HELD, "session_id" to JsonPrimitive("s1"))
        require("base_sha256" in p) { "base_sha256 is missing" }
        val now = files[path]?.let(::sha256Hex)
        if (now != p.str("base_sha256")) {
            throw error(method, FsError.BASE_MISMATCH, "sha256" to (now?.let { JsonPrimitive(it) } ?: JsonNull), "size" to JsonPrimitive(files[path]?.size ?: 0))
        }
    }

    /**
     * Answer `ide.fs.manifest` with pages of [pageEntries] entries.
     *
     * @param p the params.
     * @return one page.
     */
    private fun manifest(p: JsonObject): JsonElement {
        val cursor = p.str("cursor")
        val named = if ("paths" in p) p.strings("paths") else null
        val all: List<String>
        val start: Int
        if (cursor == null) {
            all = files.keys.filter { it !in skipped && it !in unlisted && (named == null || named.any { n -> it == n || it.startsWith("$n/") }) }
            listing = all
            listingHead = head
            start = 0
        } else {
            all = listing ?: throw error("ide.fs.manifest", FsError.NO_MANIFEST, "reason" to JsonPrimitive("cursor-expired"))
            if (listingHead != head) {
                listing = null
                throw error("ide.fs.manifest", FsError.NO_MANIFEST, "reason" to JsonPrimitive("head-moved"))
            }
            start = cursor.toInt()
        }
        val end = minOf(all.size, start + pageEntries)
        return buildJsonObject {
            put("head", head?.let { JsonPrimitive(it) } ?: JsonNull)
            put("files", JsonArray(all.subList(start, end).filter { it in files }.map { path ->
                buildJsonObject {
                    put("path", JsonPrimitive(path)); put("size", JsonPrimitive(files[path]!!.size)); put("mode", JsonPrimitive(420))
                    put("sha256", JsonPrimitive(sha256Hex(files[path]!!)))
                }
            }))
            put("skipped", JsonArray(if (cursor == null) skipped.filterKeys { named == null || it in named }.map { buildJsonObject { put("path", JsonPrimitive(it.key)); put("reason", JsonPrimitive(it.value)) } } else emptyList()))
            put("missing", JsonArray(if (cursor == null && named != null) named.filter { n -> n !in skipped && n !in unlisted && files.keys.none { it == n || it.startsWith("$n/") } }.map { JsonPrimitive(it) } else emptyList()))
            put("next_cursor", if (end < all.size) JsonPrimitive(end.toString()) else JsonNull)
        }
    }

    /**
     * Change a remote file as another writer would, and build the notification about it.
     *
     * @param path relative path.
     * @param text the new text; null deletes the file.
     * @param origin "agent" or "other".
     * @return the `ide.fs.changed` notification with the next sequence number.
     */
    fun change(path: String, text: String?, origin: String = RemoteChange.ORIGIN_AGENT): FsChanged {
        val before = files[path]?.let(::sha256Hex)
        if (text == null) files.remove(path) else files[path] = text.toByteArray()
        seq += 1
        return FsChanged("e1", seq, listOf(RemoteChange(path, before, text?.let { sha256Hex(it.toByteArray()) }, text == null, origin)))
    }
}
