/**
 * The queue of local changes that could not be uploaded yet (no connection, or the agent is
 * editing the file).
 */
package io.github.kadeng.calu.sync

/** What the user did to a path while it could not be uploaded. */
enum class PendingKind {
    /** The file was created or saved. */
    SAVE,

    /** The file was deleted. */
    DELETE,
}

/**
 * One queued local change.
 *
 * @property path canonical relative path.
 * @property kind the last thing that happened to the path. It is information for the user; the
 *   replay decides from the content the local side has at that time.
 */
data class PendingOp(val path: String, val kind: PendingKind)

/**
 * Local changes waiting for an upload, in the order in which their paths were first queued.
 *
 * The queue stores paths, not contents: the replay ([SyncEngine.replay]) uploads what the local
 * file holds then. Therefore repeated changes of one path coalesce into one entry:
 *
 *     save a, save b, save a      ->  [SAVE a, SAVE b]
 *     save a, delete a            ->  [DELETE a]
 *     rename a -> b               ->  [DELETE a, SAVE b]      (a rename is a delete and a create)
 *
 * The class is mutable and not thread-safe; the caller confines it to one thread or locks.
 */
class PendingQueue {
    private val entries = LinkedHashMap<String, PendingKind>()

    /** Number of queued paths. */
    val size: Int get() = entries.size

    /**
     * Tells whether nothing is queued. No side effects.
     *
     * @return true if the queue has no entry.
     */
    fun isEmpty(): Boolean = entries.isEmpty()

    /**
     * Records that a file was created or saved. A path that is already queued keeps its place
     * and becomes a SAVE.
     *
     * @param path canonical relative path.
     */
    fun saved(path: String) {
        entries[path] = PendingKind.SAVE
    }

    /**
     * Records that a file was deleted. A path that is already queued keeps its place and
     * becomes a DELETE.
     *
     * @param path canonical relative path.
     */
    fun deleted(path: String) {
        entries[path] = PendingKind.DELETE
    }

    /**
     * Records a rename as a delete of [from] followed by a save of [to].
     *
     * @param from canonical relative path the file had.
     * @param to canonical relative path the file has now.
     */
    fun renamed(from: String, to: String) {
        deleted(from)
        saved(to)
    }

    /**
     * Removes a path, after its change reached the remote side or was resolved otherwise.
     *
     * @param path canonical relative path; a path that is not queued is ignored.
     */
    fun remove(path: String) {
        entries.remove(path)
    }

    /**
     * Returns the queued changes in replay order. No side effects.
     *
     * @return a copy: the entries in the order in which their paths were first queued.
     */
    fun ops(): List<PendingOp> = entries.map { PendingOp(it.key, it.value) }

    /**
     * Serialises the queue, so that it survives a restart of the IDE. No side effects.
     *
     * @return one line per entry in replay order: `S` or `D`, a tab, the path; each line ends
     *   with "\n". Paths with a line feed or a tab are not canonical ([PathMapper.problem]) and
     *   must not be queued.
     */
    fun encode(): String = entries.entries.joinToString("") { (path, kind) ->
        (if (kind == PendingKind.SAVE) "S" else "D") + "\t" + path + "\n"
    }

    companion object {
        /**
         * Parses the text of [encode]. No side effects.
         *
         * @param text the stored text.
         * @return a queue with the same entries in the same order.
         * @throws IllegalArgumentException if a line does not start with `S` or `D` and a tab.
         */
        fun decode(text: String): PendingQueue {
            val queue = PendingQueue()
            for (line in text.split('\n').map { it.removeSuffix("\r") }.filter { it.isNotEmpty() }) {
                require(line.length > 2 && line[1] == '\t' && line[0] in "SD") { "malformed line: $line" }
                val path = line.substring(2)
                if (line[0] == 'S') queue.saved(path) else queue.deleted(path)
            }
            return queue
        }
    }
}
