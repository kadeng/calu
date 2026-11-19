/**
 * The remote project directory as the plugin reaches it: the `ide.fs.*` requests of the bridge
 * with their paging, continuation, chunking and errors (`agent/records/notes/jetbrains-sync-spec.md`
 * sections 2 to 5 and 13). This file sends requests through a function it is given and touches
 * no local file.
 *
 *     manifest()   ide.fs.manifest, page after page; started again on "head-moved"/"cursor-expired"
 *     read()       ide.fs.read from offset 0 until `eof`; started again if the hash changes between pieces
 *     write()      ide.fs.write up to the request limit, else begin / chunk... / commit (abort on a failure)
 *     delete(), rename(), subscribe(), unsubscribe(), paths()
 *
 *     an error answer  ──fsFailureOf──►  FsFailure  ──►  FsException
 */
package io.github.kadeng.calu.sync

import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.long
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.rpc.RpcClosedException
import io.github.kadeng.calu.rpc.RpcRequestException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.io.ByteArrayOutputStream
import java.util.Base64

/** Sends one request to the bridge and returns its result; throws what the supervisor throws. */
typealias BridgeCall = suspend (method: String, params: JsonObject) -> JsonElement

/** Why an `ide.fs.*` request failed, as the synchronisation decides on it. */
sealed interface FsFailure {
    /**
     * -32031: the remote file is not what `base_sha256` named. Decision: the path is decided
     * again with this hash as the remote side (three-way merge or conflict).
     *
     * @property sha256 the hash the remote file has now; null if there is no file.
     * @property size its size in bytes; 0 if unknown.
     */
    data class Mismatch(val sha256: String?, val size: Long) : FsFailure

    /**
     * -32035: the agent edits the file in a running turn. Decision: the change waits in the
     * queue until the turn ended.
     *
     * @property sessionId the session that holds the file; "" if unknown.
     */
    data class Held(val sessionId: String) : FsFailure

    /**
     * -32032: the content or the remote target is above the per-file limit. Decision: skipped, shown.
     *
     * @property size the size that was refused; 0 if unknown.
     * @property limit the limit in bytes; 0 if unknown.
     */
    data class TooLarge(val size: Long, val limit: Long) : FsFailure

    /**
     * -32033: the remote path is no regular file. Decision: skipped, shown.
     *
     * @property kind "directory", "symlink", "fifo", "socket", "device" or "file" (a path below a file).
     */
    data class NotRegular(val kind: String) : FsFailure

    /**
     * -32030: the bridge refuses the path. Decision: skipped, shown.
     *
     * @property reason "outside", "excluded" or "invalid".
     */
    data class Refused(val reason: String) : FsFailure

    /** -32034: the remote file does not exist (read or rename). Decision: decided again as "no remote file". */
    data object NotFound : FsFailure

    /**
     * -32038: the target of a rename exists. Decision: the rename is done as delete and upload.
     *
     * @property sha256 hash of the file at the target; null if unknown.
     */
    data class TargetExists(val sha256: String?) : FsFailure

    /**
     * -32036: a chunked upload failed and is over. Decision: tried again with the next change or "Sync now".
     *
     * @property reason "unknown-upload", "offset", "size", "sha256" or "too-many-uploads".
     */
    data class UploadFailed(val reason: String) : FsFailure

    /**
     * -32037: the server's operating system refused (permissions, disk full). Decision: shown; the
     * change stays queued.
     *
     * @property text the system's message.
     */
    data class Io(val text: String) : FsFailure

    /**
     * -32039: the file listing failed. Decision: shown; no synchronisation without a listing.
     *
     * @property reason "git", "not-git", "too-many-files", "unreadable", or "head-moved" /
     *   "cursor-expired" after the restarts were used up.
     */
    data class NoManifest(val reason: String) : FsFailure

    /**
     * The bridge is not running or ended before it answered. Decision: the change waits in the
     * queue until the connection is back.
     *
     * @property text what the supervisor said.
     */
    data class Offline(val text: String) : FsFailure

    /**
     * Any other error answer. Decision: shown; nothing is changed.
     *
     * @property code the JSON-RPC code.
     * @property text the bridge's message.
     */
    data class Other(val code: Int, val text: String) : FsFailure
}

/**
 * An `ide.fs.*` request failed.
 *
 * @property failure what happened, for the decision.
 * @param message text for the log and for a note to the user.
 */
class FsException(val failure: FsFailure, message: String) : Exception(message)

/**
 * Map what a bridge request threw to the decision the synchronisation takes. No side effects.
 *
 * @param error the exception of a request.
 * @return the failure: one of the codes -32030 to -32039 by the table of [FsFailure];
 *   `Offline` for a closed connection or a bridge that does not run; `Other` for the rest.
 */
fun fsFailureOf(error: Throwable): FsFailure {
    if (error is FsException) return error.failure
    if (error is RpcClosedException || error is IllegalStateException) return FsFailure.Offline(error.message ?: "the bridge is not running")
    if (error !is RpcRequestException) return FsFailure.Other(0, error.message ?: error.toString())
    val data = error.data.asObj() ?: EMPTY_OBJECT
    return when (error.code) {
        FsError.PATH_REFUSED -> FsFailure.Refused(data.str("reason") ?: "invalid")
        FsError.BASE_MISMATCH -> FsFailure.Mismatch(data.str("sha256"), data.long("size") ?: 0)
        FsError.TOO_LARGE -> FsFailure.TooLarge(data.long("size") ?: 0, data.long("limit") ?: 0)
        FsError.NOT_REGULAR -> FsFailure.NotRegular(data.str("kind") ?: "other")
        FsError.NOT_FOUND -> FsFailure.NotFound
        FsError.HELD -> FsFailure.Held(data.str("session_id").orEmpty())
        FsError.UPLOAD_FAILED -> FsFailure.UploadFailed(data.str("reason") ?: "unknown")
        FsError.IO_ERROR -> FsFailure.Io(data.str("strerror") ?: error.bridgeMessage)
        FsError.TARGET_EXISTS -> FsFailure.TargetExists(data.str("sha256"))
        FsError.NO_MANIFEST -> FsFailure.NoManifest(data.str("reason") ?: "unknown")
        else -> FsFailure.Other(error.code, error.bridgeMessage)
    }
}

/**
 * The limits `ide.paths` reports.
 *
 * @property fileBytes largest file the bridge transfers.
 * @property requestBytes most raw content bytes in one request or response.
 * @property pageEntries most entries of one manifest page.
 * @property uploads most chunked uploads open at once.
 */
data class RemoteLimits(
    val fileBytes: Long = DefaultSyncEngine.DEFAULT_MAX_FILE_BYTES,
    val requestBytes: Int = 1 shl 20,
    val pageEntries: Int = 2000,
    val uploads: Int = 4,
)

/**
 * The answer of `ide.paths`.
 *
 * @property root the resolved absolute project directory on the server.
 * @property home the home directory of the user the bridge runs as.
 * @property git whether the root is inside a git repository.
 * @property head the commit of `HEAD`, or null.
 * @property branch the checked-out branch, or null.
 * @property caseSensitive whether the server's file system keeps `A` and `a` apart.
 * @property limits the bridge's limits (the defaults of the spec if the bridge names none).
 */
data class RemotePaths(
    val root: String,
    val home: String,
    val git: Boolean,
    val head: String?,
    val branch: String?,
    val caseSensitive: Boolean,
    val limits: RemoteLimits,
)

/**
 * Read the result of `ide.paths` defensively. No side effects.
 *
 * @param result the result (untrusted).
 * @return the paths; a missing `root` is "", a missing or non-positive limit is the spec's default.
 */
fun parseRemotePaths(result: JsonObject): RemotePaths {
    val l = result["limits"].asObj() ?: EMPTY_OBJECT
    val d = RemoteLimits()
    return RemotePaths(
        root = result.str("root").orEmpty(),
        home = result.str("home").orEmpty(),
        git = result.bool("git"),
        head = result.str("head")?.takeIf { it.isNotEmpty() },
        branch = result.str("branch")?.takeIf { it.isNotEmpty() },
        caseSensitive = result["case_sensitive"]?.let { it == JsonPrimitive(true) } ?: true,
        limits = RemoteLimits(
            fileBytes = l.long("file_bytes")?.takeIf { it > 0 } ?: d.fileBytes,
            requestBytes = l.int("request_bytes")?.takeIf { it > 0 } ?: d.requestBytes,
            pageEntries = l.int("page_entries")?.takeIf { it > 0 } ?: d.pageEntries,
            uploads = l.int("uploads")?.takeIf { it > 0 } ?: d.uploads,
        ),
    )
}

/**
 * A whole manifest: every page of one listing.
 *
 * @property head the remote `HEAD`, or null.
 * @property files the listed files by path.
 * @property skipped files the bridge does not synchronise (links, too large, not regular).
 * @property missing paths that were asked for by name and do not exist.
 */
data class RemoteManifest(
    val head: String?,
    val files: Map<String, FileEntry>,
    val skipped: List<RemoteSkipped>,
    val missing: Set<String>,
) {
    /**
     * Return the manifest in the engine's type.
     *
     * @return head and files.
     */
    fun toManifest(): Manifest = Manifest(head, files.values.toList())
}

/**
 * A file read from the server.
 *
 * @property bytes the whole content.
 * @property sha256 its hash, computed here from [bytes] (equal to what the bridge reported).
 * @property mode the file's permission bits.
 */
class RemoteContent(val bytes: ByteArray, val sha256: String, val mode: Int)

/**
 * The answer of `ide.fs.subscribe`.
 *
 * @property epoch id of the bridge's change log.
 * @property seq the sequence number of the last notification of that log.
 * @property complete true if every notification after the named `after_seq` is replayed; false
 *   if the caller must compare both sides completely.
 */
data class FsSubscription(val epoch: String, val seq: Long, val complete: Boolean)

/**
 * The remote side of the synchronisation.
 *
 * @param call sends one bridge request (the supervisor's `request`, not interactive).
 * @param limits the bridge's limits; replaced by [paths].
 */
class RemoteFileStore(private val call: BridgeCall, limits: RemoteLimits = RemoteLimits()) {
    /** The limits in use: the constructor's until [paths] was called, then the bridge's. */
    @Volatile var limits: RemoteLimits = limits
        private set

    /**
     * Send a request and translate its failure.
     *
     * @param method the bridge method.
     * @param params its params.
     * @return the result as an object (an empty object for another JSON type).
     * @throws FsException for every failure but a cancellation.
     */
    private suspend fun send(method: String, params: JsonObject = EMPTY_OBJECT): JsonObject = try {
        call(method, params).asObj() ?: EMPTY_OBJECT
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        throw FsException(fsFailureOf(error), "$method: ${error.message}")
    }

    /**
     * Ask `ide.paths` and take over its limits.
     *
     * @return the paths of the server.
     * @throws FsException if the request fails.
     */
    suspend fun paths(): RemotePaths = parseRemotePaths(send("ide.paths")).also { limits = it.limits }

    /**
     * List the remote files (`ide.fs.manifest`), all pages. A listing that the bridge voids
     * ("head-moved", "cursor-expired") is asked again from its first page, at most
     * [MANIFEST_RESTARTS] times.
     *
     * @param scope "all" or "dirty" (git only: files that differ from `HEAD` or are untracked).
     * @param paths only these files or directories (canonical relative paths); null for the whole
     *   tree. More than a page's worth of paths are asked in several listings and joined.
     * @return the manifest.
     * @throws FsException `NoManifest` if the listing fails, `Offline` without a connection.
     */
    suspend fun manifest(scope: String = "all", paths: Collection<String>? = null): RemoteManifest {
        if (paths == null) return manifestOnce(scope, null)
        if (paths.isEmpty()) return RemoteManifest(null, emptyMap(), emptyList(), emptySet())
        var head: String? = null
        val files = LinkedHashMap<String, FileEntry>()
        val skipped = ArrayList<RemoteSkipped>()
        val missing = LinkedHashSet<String>()
        for (part in paths.toList().chunked(limits.pageEntries)) {
            val m = manifestOnce(scope, part)
            head = m.head
            files.putAll(m.files)
            skipped.addAll(m.skipped)
            missing.addAll(m.missing)
        }
        return RemoteManifest(head, files, skipped, missing)
    }

    /**
     * One listing with its pages and restarts.
     *
     * @param scope "all" or "dirty".
     * @param paths the named paths (at most a page's worth), or null.
     * @return the manifest.
     * @throws FsException see [manifest].
     */
    private suspend fun manifestOnce(scope: String, paths: List<String>?): RemoteManifest {
        var restarts = 0
        while (true) {
            try {
                val pages = ArrayList<ManifestPage>()
                var cursor: String? = null
                do {
                    val page = parseManifestPage(send("ide.fs.manifest", buildJsonObject {
                        put("scope", JsonPrimitive(scope))
                        if (paths != null) put("paths", JsonArray(paths.map { JsonPrimitive(it) }))
                        cursor?.let { put("cursor", JsonPrimitive(it)) }
                    }))
                    pages.add(page)
                    cursor = page.nextCursor
                } while (cursor != null)
                return RemoteManifest(pages[0].head, pages.flatMap { it.files }.associateBy { it.path }, pages.flatMap { it.skipped }, pages.flatMap { it.missing }.toSet())
            } catch (error: FsException) {
                val reason = (error.failure as? FsFailure.NoManifest)?.reason
                if ((reason != "head-moved" && reason != "cursor-expired") || restarts >= MANIFEST_RESTARTS) throw error
                restarts++
            }
        }
    }

    /**
     * Read one file completely (`ide.fs.read`), piece after piece until `eof`. If the file's
     * hash changes between two pieces the read starts again, at most [READ_RESTARTS] times.
     *
     * @param path canonical relative path.
     * @return content, hash and mode of one state of the file.
     * @throws FsException `NotFound`, `NotRegular`, `TooLarge`, `Refused`, `Offline`; `Other` if
     *   the file keeps changing or the content does not have the hash the bridge reported.
     */
    suspend fun read(path: String): RemoteContent {
        repeat(READ_RESTARTS + 1) {
            val out = ByteArrayOutputStream()
            var sha: String? = null
            var mode = FileEntry.DEFAULT_MODE
            var changed = false
            while (true) {
                val r = send("ide.fs.read", buildJsonObject {
                    put("path", JsonPrimitive(path))
                    put("offset", JsonPrimitive(out.size().toLong()))
                    put("length", JsonPrimitive(limits.requestBytes))
                })
                val pieceSha = r.str("sha256")
                if (sha != null && pieceSha != sha) { changed = true; break }
                sha = pieceSha
                mode = r.int("mode") ?: mode
                val piece = try {
                    Base64.getDecoder().decode(r.str("content_b64").orEmpty())
                } catch (_: IllegalArgumentException) {
                    throw FsException(FsFailure.Other(0, "content_b64 is not base64"), "ide.fs.read $path: the content is not base64")
                }
                out.write(piece)
                if (r.bool("eof")) break
                if (piece.isEmpty()) throw FsException(FsFailure.Other(0, "empty piece before eof"), "ide.fs.read $path: an empty piece before the end")
                if (out.size() > limits.fileBytes) throw FsException(FsFailure.TooLarge(out.size().toLong(), limits.fileBytes), "ide.fs.read $path: more than the per-file limit")
            }
            if (!changed) {
                val bytes = out.toByteArray()
                val actual = sha256Hex(bytes)
                if (sha == null || actual == sha) return RemoteContent(bytes, actual, mode)
                // The file was replaced between the last piece and its hash: read again.
            }
        }
        throw FsException(FsFailure.Other(0, "the file keeps changing"), "ide.fs.read $path: the file changed during every attempt to read it")
    }

    /**
     * Create or replace one remote file. Up to the request limit the content goes in one
     * `ide.fs.write`; above it in `ide.fs.write.begin`, chunks and `commit`. If a chunk or the
     * commit fails, or the caller is cancelled, the upload is aborted (`ide.fs.write.abort`) so
     * that the bridge frees it; the remote file is then unchanged.
     *
     * @param path canonical relative path.
     * @param bytes the new content.
     * @param baseSha256 hash the remote file must still have; null if it must not exist.
     * @param mode permission bits to set; null keeps the remote mode (0o644 for a new file).
     * @param duringTurn true to write although the agent edits the file in a running turn.
     * @return the hash of the written content as the bridge reports it.
     * @throws FsException `Mismatch`, `Held`, `TooLarge`, `NotRegular`, `Refused`, `Io`,
     *   `UploadFailed`, `Offline`.
     * @throws IllegalArgumentException if `path` is not canonical ([wirePathProblem]).
     */
    suspend fun write(path: String, bytes: ByteArray, baseSha256: String?, mode: Int? = null, duringTurn: Boolean = false): String {
        if (bytes.size <= limits.requestBytes) {
            val r = send("ide.fs.write", fsWriteParams(path, Base64.getEncoder().encodeToString(bytes), baseSha256, mode, duringTurn))
            return r.str("sha256") ?: sha256Hex(bytes)
        }
        val sha = sha256Hex(bytes)
        val begun = send("ide.fs.write.begin", fsWriteBeginParams(path, baseSha256, bytes.size.toLong(), sha, mode, duringTurn))
        val id = begun.str("upload_id") ?: throw FsException(FsFailure.UploadFailed("no upload_id"), "ide.fs.write.begin $path: no upload_id")
        val chunk = minOf(begun.int("chunk_size")?.takeIf { it > 0 } ?: limits.requestBytes, limits.requestBytes)
        try {
            var offset = 0
            while (offset < bytes.size) {
                val end = minOf(bytes.size, offset + chunk)
                send("ide.fs.write.chunk", buildJsonObject {
                    put("upload_id", JsonPrimitive(id))
                    put("offset", JsonPrimitive(offset.toLong()))
                    put("content_b64", JsonPrimitive(Base64.getEncoder().encodeToString(bytes.copyOfRange(offset, end))))
                })
                offset = end
            }
            val r = send("ide.fs.write.commit", buildJsonObject { put("upload_id", JsonPrimitive(id)) })
            return r.str("sha256") ?: sha
        } catch (error: Throwable) {
            withContext(NonCancellable) {
                try {
                    call("ide.fs.write.abort", buildJsonObject { put("upload_id", JsonPrimitive(id)) })
                } catch (_: Exception) {
                    // The bridge discards an upload without a request after 120 s by itself.
                }
            }
            throw error
        }
    }

    /**
     * Delete one remote file (`ide.fs.delete`).
     *
     * @param path canonical relative path.
     * @param baseSha256 hash the remote file must still have.
     * @param duringTurn true to delete although the agent edits the file in a running turn.
     * @throws FsException `Mismatch` (also for a file that is gone: `sha256` null), `Held`,
     *   `NotRegular`, `Refused`, `Io`, `Offline`.
     */
    suspend fun delete(path: String, baseSha256: String, duringTurn: Boolean = false) {
        send("ide.fs.delete", fsDeleteParams(path, baseSha256, duringTurn))
    }

    /**
     * Rename one remote file (`ide.fs.rename`); the target must not exist.
     *
     * @param from canonical relative path of the file.
     * @param to canonical relative path it gets.
     * @param baseSha256 hash the file must still have.
     * @param duringTurn true to rename although the agent edits one of the paths in a running turn.
     * @throws FsException `NotFound`, `Mismatch`, `TargetExists`, `Held`, `Refused`, `Io`, `Offline`.
     */
    suspend fun rename(from: String, to: String, baseSha256: String, duringTurn: Boolean = false) {
        send("ide.fs.rename", fsRenameParams(from, to, baseSha256, duringTurn))
    }

    /**
     * Ask for `ide.fs.changed` notifications (`ide.fs.subscribe`).
     *
     * @param afterSeq the last sequence number this client handled, with [epoch]; null for a
     *   first subscription.
     * @param epoch the change log that number belongs to; null for a first subscription.
     * @return epoch, last sequence number and whether the later notifications are replayed.
     * @throws FsException `Offline` or `Other`.
     */
    suspend fun subscribe(afterSeq: Long? = null, epoch: String? = null): FsSubscription {
        val r = send("ide.fs.subscribe", buildJsonObject {
            if (afterSeq != null && epoch != null) {
                put("after_seq", JsonPrimitive(afterSeq))
                put("epoch", JsonPrimitive(epoch))
            }
        })
        return FsSubscription(r.str("epoch").orEmpty(), r.long("seq") ?: 0, r.bool("complete"))
    }

    /**
     * Stop the notifications (`ide.fs.unsubscribe`). A failure is ignored: the connection is
     * about to end or has ended.
     */
    suspend fun unsubscribe() {
        try {
            send("ide.fs.unsubscribe")
        } catch (_: FsException) {
        }
    }

    companion object {
        /** How often a voided manifest listing is asked again before the failure is passed on. */
        const val MANIFEST_RESTARTS: Int = 3

        /** How often the read of a file that changes meanwhile starts again. */
        const val READ_RESTARTS: Int = 4
    }
}
