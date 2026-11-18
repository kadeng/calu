/**
 * What the synchronisation needs from one side, and a runner that applies the engine's actions
 * to two sides. The IDE side implements [FileStore] over the local project copy and over the
 * bridge (`ide.fs.*`); tests use in-memory stores.
 */
package io.github.kadeng.calu.sync

/**
 * The files of one side (local project copy, or remote project directory behind the bridge).
 * All paths are canonical relative paths ([PathMapper]).
 */
interface FileStore {
    /**
     * Lists every regular file of the side that takes part in the synchronisation (ignored
     * files and excluded directories left out). Reads the side; changes nothing.
     *
     * @return the files by relative path.
     */
    fun list(): Map<String, FileEntry>

    /**
     * Reads one file completely. Changes nothing.
     *
     * @param path relative path.
     * @return the content, or null if there is no such file.
     */
    fun read(path: String): ByteArray?

    /**
     * Creates or replaces one file, with its missing parent directories.
     *
     * @param path relative path.
     * @param bytes the new content, written byte for byte.
     * @param mode POSIX permission bits for the file; a side without such bits ignores them.
     */
    fun write(path: String, bytes: ByteArray, mode: Int)

    /**
     * Deletes one file. A path without a file is ignored.
     *
     * @param path relative path.
     */
    fun delete(path: String)
}

/**
 * Keeps the last synchronised contents, so that a later [SyncAction.Merge] has its base.
 * The [SyncState] holds only hashes; the contents are kept here, addressed by their hash.
 */
interface BaseContents {
    /**
     * Returns a stored content. Changes nothing.
     *
     * @param sha256 the hash of the content.
     * @return the content, or null if it is not stored (never stored, or evicted).
     */
    fun get(sha256: String): ByteArray?

    /**
     * Stores a content under its hash. An implementation may drop contents again (size limit).
     *
     * @param bytes the content.
     * @return the hash under which [get] finds it.
     */
    fun put(bytes: ByteArray): String
}

/** A [BaseContents] in memory without a limit, for tests and small projects. Not thread-safe. */
class MemoryBaseContents : BaseContents {
    private val contents = HashMap<String, ByteArray>()

    override fun get(sha256: String): ByteArray? = contents[sha256]

    override fun put(bytes: ByteArray): String {
        val hash = sha256Hex(bytes)
        contents[hash] = bytes
        return hash
    }
}

/**
 * The outcome of [SyncRunner.run].
 *
 * @property state the bases after the run.
 * @property applied the actions that were carried out, in order.
 * @property open the actions that were not carried out: every `Conflict`, `Hold` and `Skip` of
 *   the plan, a `Merge` that did not come out clean (as a `Conflict`), and an action whose
 *   precondition no longer held when it was applied (kept as it was; the next run decides anew).
 */
class SyncReport(val state: SyncState, val applied: List<SyncAction>, val open: List<SyncAction>)

/**
 * Applies the decisions of a [SyncEngine] to two [FileStore]s: one full pass of plan and apply.
 * It is the reference for how an action changes files and the [SyncState]; the IDE side does
 * the same steps with documents, the VFS and bridge requests.
 *
 *     Download     read remote; the hash must equal expectedSha256; write local; base := that hash
 *     Upload       the remote hash must equal baseSha256 (no file for null); write remote; base := local hash
 *     DeleteLocal  delete local; base := none
 *     DeleteRemote the remote hash must equal baseSha256; delete remote; base := none
 *     Merge        mergeContents(base content, local, remote); clean: write both; base := merged hash
 *     Settle       base := sha256
 *
 * @property engine decides.
 * @property local the local side.
 * @property remote the remote side.
 * @property bases the contents of earlier synchronisations; every content the runner writes or
 *   agrees on is added.
 */
class SyncRunner(
    val engine: SyncEngine,
    val local: FileStore,
    val remote: FileStore,
    val bases: BaseContents = MemoryBaseContents(),
) {
    /**
     * Plans from the listings of both sides and applies every action that needs no user.
     * Side effects: files are written and deleted in both stores, contents are added to [bases].
     *
     * @param state the bases before the run.
     * @param held the paths the agent is editing; they are not uploaded.
     * @param head the remote `HEAD` to put into the manifest and the returned state, or null.
     * @return the new state and what was and was not done. After a run that leaves nothing in
     *   `open`, both stores hold the same files with the same contents and a second run applies
     *   nothing.
     */
    fun run(state: SyncState, held: Set<String> = emptySet(), head: String? = null): SyncReport {
        val localFiles = local.list()
        val remoteFiles = remote.list()
        val plan = engine.plan(localFiles, Manifest(head, remoteFiles.values.toList()), state, held)
        var current = state.withHead(head)
        val applied = ArrayList<SyncAction>()
        val open = ArrayList<SyncAction>()
        for (action in plan) {
            when (val step = apply(action, current, localFiles, remoteFiles)) {
                is Step.Open -> open.add(step.action)
                is Step.Done -> {
                    current = step.state
                    applied.add(action)
                }
            }
        }
        return SyncReport(current, applied, open)
    }

    /**
     * Carries out one action.
     *
     * @param action the action.
     * @param state the bases before it.
     * @param localFiles the local listing the plan was made from (for file modes).
     * @param remoteFiles the remote listing the plan was made from (for file modes).
     * @return [Step.Done] with the new state if the action was carried out; [Step.Open] with a
     *   [SyncAction.Conflict] if a merge needs the user; [Step.Open] with the action itself if
     *   it was not carried out (it needs the user or waits, or a precondition failed).
     */
    private fun apply(
        action: SyncAction,
        state: SyncState,
        localFiles: Map<String, FileEntry>,
        remoteFiles: Map<String, FileEntry>,
    ): Step {
        val path = action.path
        val unchanged = Step.Open(action)
        val next: SyncState = when (action) {
            is SyncAction.Download -> {
                val bytes = remote.read(path) ?: return unchanged
                if (sha256Hex(bytes) != action.expectedSha256) return unchanged
                local.write(path, bytes, remoteFiles[path]?.mode ?: FileEntry.DEFAULT_MODE)
                state.with(path, bases.put(bytes))
            }
            is SyncAction.Upload -> {
                val bytes = local.read(path) ?: return unchanged
                if (remote.read(path)?.let(::sha256Hex) != action.baseSha256) return unchanged
                remote.write(path, bytes, localFiles[path]?.mode ?: FileEntry.DEFAULT_MODE)
                state.with(path, bases.put(bytes))
            }
            is SyncAction.DeleteLocal -> {
                local.delete(path)
                state.with(path, null)
            }
            is SyncAction.DeleteRemote -> {
                if (remote.read(path)?.let(::sha256Hex) != action.baseSha256) return unchanged
                remote.delete(path)
                state.with(path, null)
            }
            is SyncAction.Merge -> {
                val mine = local.read(path) ?: return unchanged
                val theirs = remote.read(path) ?: return unchanged
                when (val outcome = mergeContents(bases.get(action.baseSha256), mine, theirs)) {
                    is MergeOutcome.Unresolved -> return Step.Open(SyncAction.Conflict(path, outcome.kind))
                    is MergeOutcome.Merged -> {
                        local.write(path, outcome.bytes, localFiles[path]?.mode ?: FileEntry.DEFAULT_MODE)
                        remote.write(path, outcome.bytes, remoteFiles[path]?.mode ?: FileEntry.DEFAULT_MODE)
                        state.with(path, bases.put(outcome.bytes))
                    }
                }
            }
            is SyncAction.Settle -> {
                if (action.sha256 != null) local.read(path)?.let { bases.put(it) }
                state.with(path, action.sha256)
            }
            is SyncAction.Conflict, is SyncAction.Hold, is SyncAction.Skip -> return unchanged
        }
        return Step.Done(next)
    }

    /** What [apply] did with one action. */
    private sealed interface Step {
        /**
         * The action was carried out.
         *
         * @property state the bases after it.
         */
        class Done(val state: SyncState) : Step

        /**
         * The action was not carried out.
         *
         * @property action what stays open: the action itself, or the conflict it turned into.
         */
        class Open(val action: SyncAction) : Step
    }
}
