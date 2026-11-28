/**
 * The `ide.fs.*` requests and answers as the bridge takes and gives them
 * (`agent/records/notes/jetbrains-sync-spec.md` sections 2 to 5, with the deviations of section 13).
 * Pure: builds params, reads results; sends nothing.
 *
 *     fsWriteParams / fsWriteBeginParams / fsDeleteParams / fsRenameParams   -> params (JSON)
 *     parseManifestPage(result)    -> files, skipped (with the bridge's reason), missing, next cursor
 *     parseFsChanged(params)       -> epoch, seq, changes
 *     manifestMustRestart(code, data)  -> a paged manifest must be asked again from the start
 *
 * Rules of section 13 that this file enforces:
 *
 *     paths        only canonical relative paths are sent ([wirePathProblem]); the bridge
 *                  refuses every other form and repairs none
 *     base_sha256  the key is always present; JSON null says "the file must not exist"
 *     mode         only 0 to 0o777 ([wireMode] drops other bits); left out to keep the mode
 */
package io.github.kadeng.calu.sync

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.long
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Error codes of the `ide.fs.*` requests. */
object FsError {
    /** The path is not a canonical relative path, leaves the root or is excluded (`data.reason`). */
    const val PATH_REFUSED: Int = -32030

    /** The file's hash is not `base_sha256` (`data.sha256` is the hash now, null if no file). */
    const val BASE_MISMATCH: Int = -32031

    /** The content, or the target on the server, is above the per-file limit. */
    const val TOO_LARGE: Int = -32032

    /** The path is no regular file (`data.kind`). */
    const val NOT_REGULAR: Int = -32033

    /** `ide.fs.read` or `ide.fs.rename` of a missing file. */
    const val NOT_FOUND: Int = -32034

    /** The agent edits the file in a running turn and `during_turn` is false. */
    const val HELD: Int = -32035

    /** A chunked upload failed (`data.reason`); the upload is over. */
    const val UPLOAD_FAILED: Int = -32036

    /** The operating system refused. */
    const val IO_ERROR: Int = -32037

    /** `ide.fs.rename` to an existing path. */
    const val TARGET_EXISTS: Int = -32038

    /** The file listing failed (`data.reason`, see [manifestMustRestart]). */
    const val NO_MANIFEST: Int = -32039
}

/** Largest value of `mode`: the permission bits 0o777. */
const val MAX_WIRE_MODE: Int = 511

/** Longest path the bridge takes, in UTF-8 bytes. */
const val MAX_WIRE_PATH_BYTES: Int = 4096

/** Longest path segment the bridge takes, in UTF-8 bytes. */
const val MAX_WIRE_SEGMENT_BYTES: Int = 255

private val WIRE_DRIVE = Regex("^[A-Za-z]:")

/**
 * Check a path against the form the bridge accepts (section 13, "path form"). No side effects.
 *
 * @param path the path as it would be sent.
 * @return null for a canonical relative path. "outside" for an absolute path, a drive letter or
 *   a `..` segment. "invalid" for any other fault:
 *   - an empty path
 *   - a control character (NUL included) or a backslash
 *   - an empty or `.` segment (`a//b`, `./a`, `a/`)
 *   - more than 4096 bytes, or a segment above 255 bytes
 *
 *   The words are the `data.reason` of error -32030.
 */
fun wirePathProblem(path: String): String? {
    if (path.isEmpty() || path.any { it < ' ' || it == '\u007f' || it == '\\' }) return "invalid"
    if (path.startsWith("/") || WIRE_DRIVE.containsMatchIn(path)) return "outside"
    val segments = path.split('/')
    if (segments.any { it == ".." }) return "outside"
    if (segments.any { it.isEmpty() || it == "." }) return "invalid"
    if (path.toByteArray(Charsets.UTF_8).size > MAX_WIRE_PATH_BYTES) return "invalid"
    if (segments.any { it.toByteArray(Charsets.UTF_8).size > MAX_WIRE_SEGMENT_BYTES }) return "invalid"
    return null
}

/**
 * Return a path that may be sent.
 *
 * @param path the path.
 * @return `path` unchanged.
 * @throws IllegalArgumentException if [wirePathProblem] finds a problem: the caller normalises
 *   paths with [PathMapper.normalize] before it builds a request.
 */
private fun wirePath(path: String): String {
    val problem = wirePathProblem(path)
    require(problem == null) { "not a canonical relative path ($problem): $path" }
    return path
}

/**
 * Reduce a file mode to what `mode` may carry.
 *
 * @param mode POSIX mode bits of a local file, or null (a Windows plugin never sends a mode).
 * @return the permission bits 0 to 0o777 (set-id and sticky bits and the file type are
 *   dropped); null for null.
 */
fun wireMode(mode: Int?): Int? = mode?.and(MAX_WIRE_MODE)

/**
 * Write a base hash as the member `base_sha256`.
 *
 * @param baseSha256 the hash, or null.
 * @return the JSON string, or JSON null ("the file must not exist").
 */
private fun base(baseSha256: String?): JsonElement = baseSha256?.let { JsonPrimitive(it) } ?: JsonNull

/**
 * Build the params of `ide.fs.write` (the whole content in one request, at most 1 MiB raw).
 *
 * @param path canonical relative path.
 * @param contentB64 the content, base64.
 * @param baseSha256 hash the remote file must still have; null says that the file must not
 *   exist. The key is always sent: without it the bridge answers -32602.
 * @param mode permission bits (see [wireMode]); null leaves the member out, so an existing file
 *   keeps its mode and a new one gets 0o644.
 * @param duringTurn true to write although the agent edits the file in a running turn.
 * @return `{path, content_b64, base_sha256, mode?, during_turn}`.
 * @throws IllegalArgumentException if `path` is not canonical ([wirePathProblem]).
 */
fun fsWriteParams(path: String, contentB64: String, baseSha256: String?, mode: Int? = null, duringTurn: Boolean = false): JsonObject =
    buildJsonObject {
        put("path", JsonPrimitive(wirePath(path)))
        put("content_b64", JsonPrimitive(contentB64))
        put("base_sha256", base(baseSha256))
        wireMode(mode)?.let { put("mode", JsonPrimitive(it)) }
        put("during_turn", JsonPrimitive(duringTurn))
    }

/**
 * Build the params of `ide.fs.write.begin` (a chunked upload of a file above 1 MiB).
 *
 * @param path canonical relative path.
 * @param baseSha256 hash the remote file must still have; null says that it must not exist.
 *   The key is always sent.
 * @param size length of the whole content in bytes.
 * @param sha256 hash of the whole content.
 * @param mode permission bits (see [wireMode]); null leaves the member out.
 * @param duringTurn true to write although the agent edits the file in a running turn.
 * @return `{path, base_sha256, size, sha256, mode?, during_turn}`.
 * @throws IllegalArgumentException if `path` is not canonical or `size` is negative.
 */
fun fsWriteBeginParams(path: String, baseSha256: String?, size: Long, sha256: String, mode: Int? = null, duringTurn: Boolean = false): JsonObject {
    require(size >= 0) { "negative size: $size" }
    return buildJsonObject {
        put("path", JsonPrimitive(wirePath(path)))
        put("base_sha256", base(baseSha256))
        put("size", JsonPrimitive(size))
        put("sha256", JsonPrimitive(sha256))
        wireMode(mode)?.let { put("mode", JsonPrimitive(it)) }
        put("during_turn", JsonPrimitive(duringTurn))
    }
}

/**
 * Build the params of `ide.fs.delete`.
 *
 * @param path canonical relative path.
 * @param baseSha256 hash the remote file must still have (a delete always names one).
 * @param duringTurn true to delete although the agent edits the file in a running turn.
 * @return `{path, base_sha256, during_turn}`.
 * @throws IllegalArgumentException if `path` is not canonical.
 */
fun fsDeleteParams(path: String, baseSha256: String, duringTurn: Boolean = false): JsonObject = buildJsonObject {
    put("path", JsonPrimitive(wirePath(path)))
    put("base_sha256", JsonPrimitive(baseSha256))
    put("during_turn", JsonPrimitive(duringTurn))
}

/**
 * Build the params of `ide.fs.rename`.
 *
 * @param from canonical relative path of the file.
 * @param to canonical relative path it gets; it must not exist on the remote side (-32038).
 * @param baseSha256 hash the remote file `from` must still have.
 * @param duringTurn true to rename although the agent edits one of the files in a running turn.
 * @return `{from, to, base_sha256, during_turn}`.
 * @throws IllegalArgumentException if a path is not canonical.
 */
fun fsRenameParams(from: String, to: String, baseSha256: String, duringTurn: Boolean = false): JsonObject = buildJsonObject {
    put("from", JsonPrimitive(wirePath(from)))
    put("to", JsonPrimitive(wirePath(to)))
    put("base_sha256", JsonPrimitive(baseSha256))
    put("during_turn", JsonPrimitive(duringTurn))
}

/**
 * Tell whether a failed `ide.fs.manifest` request means that the paged listing is void and must
 * be asked again from its first page.
 *
 * @param code the error's code.
 * @param data the error's `data`.
 * @return true for -32039 with `data.reason` "head-moved" (HEAD changed since the first page)
 *   or "cursor-expired" (the bridge no longer keeps the listing); false for every other error,
 *   also the other reasons of -32039 ("git", "not-git", "too-many-files", "unreadable"), which
 *   a new request would meet again.
 */
fun manifestMustRestart(code: Int, data: JsonElement?): Boolean =
    code == FsError.NO_MANIFEST && data.asObj()?.str("reason").let { it == "head-moved" || it == "cursor-expired" }

/** Why the bridge left a file out of the manifest (`skipped[].reason`). */
enum class RemoteSkipReason(val wire: String) {
    /** A symbolic link (never followed, never created). */
    SYMLINK("symlink"),

    /** The file is above the bridge's per-file limit. */
    TOO_LARGE("too-large"),

    /** A FIFO, socket or device. */
    NOT_REGULAR("not-regular"),

    /** A git submodule. */
    SUBMODULE("submodule"),

    /**
     * The file's name has a control character, a backslash or bytes that are not UTF-8: the
     * protocol cannot carry it (section 13).
     */
    UNSUPPORTED_NAME("unsupported-name"),

    /** A reason this plugin does not know (a newer bridge). */
    OTHER(""),
    ;

    companion object {
        /**
         * Read a reason of the bridge.
         *
         * @param wire the `reason` text.
         * @return the reason; [OTHER] for an unknown text.
         */
        fun of(wire: String?): RemoteSkipReason = entries.firstOrNull { it != OTHER && it.wire == wire } ?: OTHER
    }
}

/**
 * A file the bridge does not synchronise.
 *
 * @property path the path as the bridge shows it (for an unsupported name: a readable form of
 *   it, not a path that can be sent).
 * @property reason why.
 * @property reasonText the bridge's own word, for a list shown to the user.
 */
data class RemoteSkipped(val path: String, val reason: RemoteSkipReason, val reasonText: String)

/**
 * One page of `ide.fs.manifest`.
 *
 * @property head the commit the remote `HEAD` points to, or null.
 * @property files the files of this page.
 * @property skipped files the bridge leaves out.
 * @property missing paths that were asked for by name and do not exist (first page only).
 * @property nextCursor the opaque cursor of the next page; null on the last page.
 */
data class ManifestPage(
    val head: String?,
    val files: List<FileEntry>,
    val skipped: List<RemoteSkipped>,
    val missing: List<String>,
    val nextCursor: String?,
)

/**
 * Read one page of `ide.fs.manifest` defensively.
 *
 * @param result the result (untrusted).
 * @return the page. A file entry without a string `path` and `sha256` is dropped, and so is
 *   one whose path is not canonical ([wirePathProblem]): such a path could not be asked for.
 */
fun parseManifestPage(result: JsonObject): ManifestPage = ManifestPage(
    head = result.str("head")?.takeIf { it.isNotEmpty() },
    files = result.arr("files").mapNotNull { entry ->
        val o = entry.asObj() ?: return@mapNotNull null
        val path = o.str("path")?.takeIf { wirePathProblem(it) == null } ?: return@mapNotNull null
        val sha256 = o.str("sha256") ?: return@mapNotNull null
        FileEntry(path, o.long("size") ?: 0, o.int("mode") ?: FileEntry.DEFAULT_MODE, sha256)
    },
    skipped = result.arr("skipped").mapNotNull { entry ->
        val o = entry.asObj() ?: return@mapNotNull null
        val path = o.str("path") ?: return@mapNotNull null
        RemoteSkipped(path, RemoteSkipReason.of(o.str("reason")), o.str("reason").orEmpty())
    },
    missing = result.strings("missing"),
    nextCursor = result.str("next_cursor")?.takeIf { it.isNotEmpty() },
)

/**
 * Join the pages of one manifest.
 *
 * @param pages the pages in the order they were read (at least one).
 * @return the manifest: the head of the first page and the files of all pages.
 * @throws IllegalArgumentException if `pages` is empty.
 */
fun manifestOf(pages: List<ManifestPage>): Manifest {
    require(pages.isNotEmpty()) { "a manifest has at least one page" }
    return Manifest(pages[0].head, pages.flatMap { it.files })
}

/**
 * An `ide.fs.changed` notification.
 *
 * @property epoch identifies the bridge's change log; a new epoch means the sequence numbers
 *   started again.
 * @property seq sequence number of the notification.
 * @property changes the changed files.
 * @property skipped paths that became ones the bridge does not synchronise (a file grown above
 *   the size limit, a new special file); empty from a bridge that does not report them.
 */
data class FsChanged(val epoch: String, val seq: Long, val changes: List<RemoteChange>, val skipped: List<RemoteSkipped> = emptyList())

/**
 * Read an `ide.fs.changed` notification defensively.
 *
 * @param params the notification's params (untrusted).
 * @return the notification; an entry without a canonical string `path` is dropped. A change
 *   without `after_sha256` counts as deleted only if `deleted` is true. `origin` is one of
 *   "ide", "agent", "other" ("other" for a missing or unknown value). `skipped` is read as
 *   a manifest's `skipped`.
 */
fun parseFsChanged(params: JsonObject): FsChanged = FsChanged(
    epoch = params.str("epoch").orEmpty(),
    seq = params.long("seq") ?: 0,
    changes = params.arr("changes").mapNotNull { entry ->
        val o = entry.asObj() ?: return@mapNotNull null
        val path = o.str("path")?.takeIf { wirePathProblem(it) == null } ?: return@mapNotNull null
        val origin = o.str("origin")?.takeIf { it == RemoteChange.ORIGIN_IDE || it == RemoteChange.ORIGIN_AGENT } ?: RemoteChange.ORIGIN_OTHER
        RemoteChange(path, o.str("before_sha256"), o.str("after_sha256"), o.bool("deleted"), origin)
    },
    skipped = params.arr("skipped").mapNotNull { entry ->
        val o = entry.asObj() ?: return@mapNotNull null
        val path = o.str("path") ?: return@mapNotNull null
        RemoteSkipped(path, RemoteSkipReason.of(o.str("reason")), o.str("reason").orEmpty())
    },
)
