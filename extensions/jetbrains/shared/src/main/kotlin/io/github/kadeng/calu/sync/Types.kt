/**
 * Data types of the file synchronisation of the SSH mode (`docs/notes/jetbrains-sync-spec.md`).
 *
 * The local side is the project copy on the user's machine, the remote side is the project
 * directory on the server, reached through `calu ide`. Every path in these types is a project
 * relative path with "/" separators (see [PathMapper]); every hash is the SHA-256 of the file's
 * bytes as 64 lowercase hexadecimal digits. Nothing in this package imports the IntelliJ Platform.
 */
package io.github.kadeng.calu.sync

import java.security.MessageDigest

/**
 * Computes the SHA-256 of [bytes]. No side effects.
 *
 * @param bytes the content to hash; may be empty.
 * @return the hash as 64 lowercase hexadecimal digits.
 */
fun sha256Hex(bytes: ByteArray): String {
    val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
    val out = StringBuilder(digest.size * 2)
    for (b in digest) {
        out.append(HEX[(b.toInt() shr 4) and 0xF]).append(HEX[b.toInt() and 0xF])
    }
    return out.toString()
}

/**
 * Tells whether [text] has the form of a hash as this package uses it. No side effects.
 *
 * @param text the candidate.
 * @return true if [text] consists of exactly 64 characters out of `0-9a-f`.
 */
fun isSha256(text: String): Boolean = text.length == 64 && text.all { it in '0'..'9' || it in 'a'..'f' }

/** The digits [sha256Hex] writes. */
private const val HEX = "0123456789abcdef"

/**
 * One regular file of one side.
 *
 * @property path project relative path with "/" separators.
 * @property size length of the content in bytes.
 * @property mode POSIX permission bits (for example `0b110_100_100` = 0644). A side that has no
 *   such bits (Windows) reports [DEFAULT_MODE].
 * @property sha256 hash of the content, 64 lowercase hexadecimal digits.
 */
data class FileEntry(val path: String, val size: Long, val mode: Int, val sha256: String) {
    companion object {
        /** The mode of a file whose side has no permission bits: 0644. */
        const val DEFAULT_MODE: Int = 420
    }
}

/**
 * The answer of `ide.fs.manifest`: every file of the remote side that is synchronised.
 *
 * @property head the commit the remote `HEAD` points to (40 hexadecimal digits), or null if the
 *   remote project is not a git repository or has no commit.
 * @property files the files; each path occurs once.
 */
data class Manifest(val head: String?, val files: List<FileEntry>) {
    /**
     * Returns the files keyed by their path. No side effects.
     *
     * @return a map from [FileEntry.path] to the entry; of two entries with one path the later wins.
     */
    fun byPath(): Map<String, FileEntry> = files.associateBy { it.path }
}

/**
 * One entry of an `ide.fs.changed` notification: the remote side changed a file.
 *
 * @property path project relative path with "/" separators.
 * @property beforeSha256 the last hash the bridge computed for the path before the change
 *   (manifest, read, scan, its own write); null if this bridge never read the file. Null does
 *   **not** say that the file did not exist (spec section 13): whether a file is new is decided
 *   from the base of the [SyncState], never from this member.
 * @property afterSha256 hash of the content after the change, null if [deleted].
 * @property deleted true if the file no longer exists on the remote side.
 * @property origin who changed the file, as the bridge reports it: [ORIGIN_IDE] (an `ide.fs.*`
 *   request of this connection: the echo of the plugin's own write), [ORIGIN_AGENT] or
 *   [ORIGIN_OTHER]. Only [ORIGIN_IDE] is reliable: "agent" is partly a guess of the bridge (a
 *   change found while a command of the session ran), and "other" also covers the writes of
 *   another bridge on the same directory. No decision of the engine depends on it.
 * @constructor `origin` may be left out (also for callers compiled before it was added).
 */
data class RemoteChange @JvmOverloads constructor(
    val path: String,
    val beforeSha256: String?,
    val afterSha256: String?,
    val deleted: Boolean,
    val origin: String = ORIGIN_OTHER,
) {
    /** True if the change is the echo of a write of this connection (`origin` "ide"). */
    val isOwnWrite: Boolean get() = origin == ORIGIN_IDE

    companion object {
        /** `origin` of a change that an `ide.fs.*` request of this connection made. */
        const val ORIGIN_IDE: String = "ide"

        /** `origin` of a change the bridge attributes to a tool call of the attached session. */
        const val ORIGIN_AGENT: String = "agent"

        /** `origin` of every other change. */
        const val ORIGIN_OTHER: String = "other"
    }
}

/** Why both sides cannot be brought together without the user. */
enum class ConflictKind {
    /** Both sides changed the file to different contents and the text merge found overlapping changes. */
    BOTH_MODIFIED,

    /** The remote side deleted the file, the local side changed it. */
    DELETED_REMOTELY_MODIFIED_LOCALLY,

    /** The remote side changed the file, the local side deleted it. */
    MODIFIED_REMOTELY_DELETED_LOCALLY,

    /** Both sides created the file with different contents; there is no merge base. */
    CREATED_BOTH,

    /** Both sides changed the file and at least one content is binary ([isProbablyBinary]). */
    BINARY,

    /** Both sides changed the file and it is larger than the engine's merge limit. */
    TOO_LARGE,
}

/** Why an upload waits. */
enum class HoldReason {
    /** The agent is editing the file in a running turn; the upload waits until the turn ended. */
    AGENT_EDITING,

    /** There is no connection to the bridge; the caller puts the path into the [PendingQueue]. */
    OFFLINE,
}

/** Why a path is not synchronised at all. */
enum class SkipReason {
    /** The path is not a valid project relative path ([PathProblem.EMPTY], `ABSOLUTE`, `PARENT_SEGMENT`, `INVALID_CHARACTER`). */
    INVALID_PATH,

    /** The path is inside a directory that is never synchronised (`.git`, `.agent/traces`). */
    EXCLUDED,

    /** The local file system cannot hold a file of this name. */
    NOT_REPRESENTABLE_LOCALLY,

    /** Another path differs from this one only in letter case and the local file system ignores case. */
    CASE_COLLISION,

    /** The file is larger than the engine's per-file limit. */
    TOO_LARGE,
}

/**
 * One decision of the [SyncEngine] about one path. The engine only decides; the caller (the IDE
 * side, or [SyncRunner] in tests) applies the action and then updates the [SyncState].
 */
sealed interface SyncAction {
    /** The project relative path the action is about. */
    val path: String

    /**
     * Copy the remote content to the local side.
     *
     * @property expectedSha256 hash the downloaded content must have; another hash means the
     *   remote side changed again and the path must be decided anew. After success the base of
     *   the path is this hash.
     */
    data class Download(override val path: String, val expectedSha256: String) : SyncAction

    /**
     * Copy the local content to the remote side with `ide.fs.write`.
     *
     * @property baseSha256 hash the remote file must still have, null if the remote file must
     *   not exist. After success the base of the path is the hash of the uploaded content.
     */
    data class Upload(override val path: String, val baseSha256: String?) : SyncAction

    /** Delete the local file; afterwards the path has no base. */
    data class DeleteLocal(override val path: String) : SyncAction

    /**
     * Delete the remote file with `ide.fs.delete`; afterwards the path has no base.
     *
     * @property baseSha256 hash the remote file must still have.
     */
    data class DeleteRemote(override val path: String, val baseSha256: String) : SyncAction

    /**
     * Both sides changed a file that has a base: merge the three contents ([mergeContents]).
     * A clean result is written to both sides, anything else becomes a [Conflict].
     *
     * @property baseSha256 hash of the last synchronised content (the merge base).
     * @property localSha256 hash of the local file on disk. If the decision came from
     *   [SyncEngine.onRemoteChanged] with unsaved text in the editor, the editor's text is the
     *   local side of the merge and this hash describes the file on disk only.
     * @property remoteSha256 hash of the remote content.
     */
    data class Merge(
        override val path: String,
        val baseSha256: String,
        val localSha256: String,
        val remoteSha256: String,
    ) : SyncAction

    /**
     * The user has to decide (the IDE's merge dialog, or a choice between the two sides).
     *
     * @property kind what happened on the two sides.
     */
    data class Conflict(override val path: String, val kind: ConflictKind) : SyncAction

    /**
     * The local change is kept and not uploaded now; neither side is touched.
     *
     * @property reason why, and by that when the path is decided again.
     */
    data class Hold(override val path: String, val reason: HoldReason) : SyncAction

    /**
     * The path is left alone on both sides and shown to the user as not synchronised.
     *
     * @property reason why.
     */
    data class Skip(override val path: String, val reason: SkipReason) : SyncAction

    /**
     * No file is touched; only the [SyncState] changes. The engine returns it when both sides
     * already agree and the state does not say so yet.
     *
     * @property sha256 the new base of the path; null removes the path from the state (both
     *   sides deleted the file).
     */
    data class Settle(override val path: String, val sha256: String?) : SyncAction
}

/**
 * What the two sides last agreed on: per path the hash of the last synchronised content, which
 * is the base of the next three-way decision. Instances are immutable; the IDE side stores the
 * text of [encode] in a file and reads it back with [decode].
 *
 * @property head the git commit both sides were at when the state was created or last rebased,
 *   null if unknown or not a git repository.
 * @param bases per path the base hash; copied.
 * @throws IllegalArgumentException if a hash is not 64 lowercase hexadecimal digits.
 */
class SyncState(val head: String? = null, bases: Map<String, String> = emptyMap()) {
    private val bases: Map<String, String> = bases.toSortedMap()

    init {
        for ((path, hash) in this.bases) {
            require(isSha256(hash)) { "not a SHA-256 hash for $path: $hash" }
        }
    }

    /** Every path that has a base, in ascending order. */
    val paths: Set<String> get() = bases.keys

    /**
     * Returns the base of [path]. No side effects.
     *
     * @param path project relative path.
     * @return the hash of the last synchronised content, or null if the path was never
     *   synchronised or was deleted on both sides.
     */
    fun base(path: String): String? = bases[path]

    /**
     * Returns a state in which [path] has the base [sha256]. This state is not changed.
     *
     * @param path project relative path.
     * @param sha256 the new base; null removes the path.
     * @return the new state with the same [head].
     * @throws IllegalArgumentException if [sha256] is not a hash.
     */
    fun with(path: String, sha256: String?): SyncState =
        SyncState(head, if (sha256 == null) bases - path else bases + (path to sha256))

    /**
     * Returns a state with another [head] and the same bases. This state is not changed.
     *
     * @param head the commit both sides are at, or null.
     * @return the new state.
     */
    fun withHead(head: String?): SyncState = SyncState(head, bases)

    /**
     * Serialises the state as text. No side effects. The format, one item per line ("\n"):
     *
     *     calu-sync-state 1
     *     head <commit, or "-">
     *     <sha256><TAB><path>          one line per path, ascending
     *
     * In a path, backslash, line feed, carriage return and tab are written as `\\`, `\n`, `\r`, `\t`.
     *
     * @return the text; [decode] of it returns an equal state.
     */
    fun encode(): String {
        val out = StringBuilder()
        out.append(HEADER).append('\n')
        out.append("head ").append(head ?: "-").append('\n')
        for ((path, hash) in bases) {
            out.append(hash).append('\t').append(escape(path)).append('\n')
        }
        return out.toString()
    }

    /**
     * Compares head and bases.
     *
     * @param other the object to compare with.
     * @return true if [other] is a [SyncState] with the same head and the same base of every path.
     */
    override fun equals(other: Any?): Boolean = other is SyncState && other.head == head && other.bases == bases

    /**
     * Hash code consistent with [equals].
     *
     * @return the hash code.
     */
    override fun hashCode(): Int = 31 * (head?.hashCode() ?: 0) + bases.hashCode()

    /**
     * Text for logs and test failures.
     *
     * @return the head and the number of paths.
     */
    override fun toString(): String = "SyncState(head=$head, ${bases.size} paths)"

    companion object {
        private const val HEADER = "calu-sync-state 1"

        /**
         * Parses the text of [encode]. No side effects.
         *
         * @param text the stored text; a trailing empty line and "\r\n" line ends are accepted.
         * @return the state.
         * @throws IllegalArgumentException if the first line is not the header of version 1, a
         *   line has no tab, a hash is malformed or a path occurs twice. The caller then starts
         *   from an empty state, which makes the next plan treat every file as never synchronised.
         */
        fun decode(text: String): SyncState {
            val lines = text.split('\n').map { it.removeSuffix("\r") }.filter { it.isNotEmpty() }
            require(lines.isNotEmpty() && lines[0] == HEADER) { "not a calu sync state of version 1" }
            require(lines.size >= 2 && lines[1].startsWith("head ")) { "the head line is missing" }
            val head = lines[1].removePrefix("head ").takeUnless { it == "-" }
            val bases = HashMap<String, String>()
            for (line in lines.drop(2)) {
                val tab = line.indexOf('\t')
                require(tab > 0) { "malformed line: $line" }
                val path = unescape(line.substring(tab + 1))
                require(bases.put(path, line.substring(0, tab)) == null) { "path occurs twice: $path" }
            }
            return SyncState(head, bases)
        }

        /**
         * Escapes the characters that would break the line format of [encode].
         *
         * @param path a path.
         * @return the path with `\`, LF, CR and TAB written as two characters each.
         */
        private fun escape(path: String): String =
            path.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

        /**
         * Reverses [escape].
         *
         * @param text an escaped path.
         * @return the path.
         * @throws IllegalArgumentException if a backslash is followed by another character than
         *   `\`, `n`, `r`, `t`, or ends the text.
         */
        private fun unescape(text: String): String {
            val out = StringBuilder(text.length)
            var i = 0
            while (i < text.length) {
                val c = text[i]
                if (c != '\\') {
                    out.append(c)
                    i += 1
                    continue
                }
                require(i + 1 < text.length) { "dangling escape in: $text" }
                out.append(
                    when (text[i + 1]) {
                        '\\' -> '\\'
                        'n' -> '\n'
                        'r' -> '\r'
                        't' -> '\t'
                        else -> throw IllegalArgumentException("unknown escape in: $text")
                    },
                )
                i += 2
            }
            return out.toString()
        }
    }
}
