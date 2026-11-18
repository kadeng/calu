/**
 * The decision logic of the file synchronisation: which action brings the local project copy
 * and the remote project directory together, per path. The engine reads no file and sends no
 * request; it maps hashes to [SyncAction]s.
 */
package io.github.kadeng.calu.sync

/**
 * Decides what to do with each path. Implementations are pure: equal arguments give equal
 * results, and nothing outside the return value changes.
 *
 * All decisions follow one table over three hashes of a path: `b` the base ([SyncState.base],
 * the last synchronised content), `l` the local content, `r` the remote content; `-` means
 * "no file" (for `b`: never synchronised).
 *
 *     b   l      r       decision                                    when the path is held
 *     -   -      -       nothing
 *     -   L      -       Upload(base = null)                         Hold
 *     -   -      R       Download(R)
 *     -   L      L       Settle(L)            both created the same
 *     -   L      R       Conflict(CREATED_BOTH)                      Hold
 *     B   B      B       nothing
 *     B   B      R       Download(R)          only remote changed
 *     B   L      B       Upload(base = B)     only local changed     Hold
 *     B   L      L       Settle(L)            both made the same change
 *     B   L      R       Merge(B, L, R); Conflict(TOO_LARGE) above   Hold
 *                        the merge limit
 *     B   B      -       DeleteLocal          deleted remotely
 *     B   -      B       DeleteRemote(B)      deleted locally        Hold
 *     B   L      -       Conflict(DELETED_REMOTELY_MODIFIED_LOCALLY) Hold
 *     B   -      R       Conflict(MODIFIED_REMOTELY_DELETED_LOCALLY) Hold
 *     B   -      -       Settle(null)         deleted on both sides: the state forgets the path
 *
 * (L and R stand for hashes that differ from B and from each other.) A path is *held* while the
 * agent edits it in a running turn: every decision that would send a local change to the remote
 * side or ask the user becomes `Hold(AGENT_EDITING)`; downloads and local deletions go on.
 * A path that breaks a path rule gets `Skip`, and so does a one-sided change of a file above
 * the per-file limit.
 */
interface SyncEngine {
    /**
     * Decides one path from the three hashes, by the table of [SyncEngine]. No side effects.
     *
     * @param path canonical relative path.
     * @param baseSha256 the base of the path, null if it has none.
     * @param local the local file, null if there is none. Only `sha256` and `size` are read.
     * @param remote the remote file, null if there is none. Only `sha256` and `size` are read.
     * @param held whether the agent is editing the path in a running turn.
     * @return the action, or null if nothing is to do.
     */
    fun decide(path: String, baseSha256: String?, local: FileEntry?, remote: FileEntry?, held: Boolean): SyncAction?

    /**
     * Compares both sides completely: the first copy, the catch-up after a reconnect with a gap,
     * and the periodic check. No side effects.
     *
     * @param local the local files by relative path.
     * @param remote the answer of `ide.fs.manifest`.
     * @param state the bases.
     * @param held the paths the agent is editing in a running turn.
     * @return one action for every path of `local`, `remote` or `state` that needs one, in
     *   ascending path order. If the local file system ignores letter case, every path that
     *   collides with another ([PathMapper.caseCollisions]) gets `Skip(CASE_COLLISION)`. Paths
     *   inside an excluded directory ([PathMapper.isExcluded]) are left out without an action,
     *   so a local listing that contains `.git` does not produce one `Skip` per file.
     */
    fun plan(local: Map<String, FileEntry>, remote: Manifest, state: SyncState, held: Set<String>): List<SyncAction>

    /**
     * Decides what to do after one entry of an `ide.fs.changed` notification. No side effects.
     *
     * The remote hash is `change.afterSha256` (none if `change.deleted`); `change.beforeSha256`
     * is not needed, because the base of the state is what the local side was synchronised
     * with. A notification about content the state already has as base (the echo of the
     * plugin's own upload) gives null.
     *
     * @param change the entry.
     * @param localSha256 hash of the local file on disk, null if there is none.
     * @param state the bases.
     * @param dirtyInEditor whether the IDE holds unsaved text of this file. Unsaved text counts
     *   as a local change: instead of `Download` the result is `Merge` (with the editor's text
     *   as the local side; `Conflict(CREATED_BOTH)` if the path has no base), and instead of
     *   `DeleteLocal` it is `Conflict(DELETED_REMOTELY_MODIFIED_LOCALLY)`.
     * @param held the paths the agent is editing in a running turn; a decision that needs the
     *   local change (see the table) becomes `Hold(AGENT_EDITING)`.
     * @return the action, or null if nothing is to do.
     */
    fun onRemoteChanged(
        change: RemoteChange,
        localSha256: String?,
        state: SyncState,
        dirtyInEditor: Boolean,
        held: Set<String> = emptySet(),
    ): SyncAction?

    /**
     * Decides what to do after the user saved or deleted a local file. No side effects.
     *
     * The remote content is assumed to be the base; `ide.fs.write` checks that with
     * `base_sha256`, and a refusal is decided with [decide] and the hash the bridge reports.
     *
     *     path breaks a rule            -> Skip
     *     localSha256 == base           -> null (nothing changed)
     *     not online                    -> Hold(OFFLINE)        the caller queues the path
     *     path in held                  -> Hold(AGENT_EDITING)  the caller queues the path
     *     localSha256 == null           -> DeleteRemote(base)
     *     otherwise                     -> Upload(base)
     *
     * @param path canonical relative path.
     * @param localSha256 hash of the saved content, null if the file was deleted.
     * @param state the bases.
     * @param held the paths the agent is editing in a running turn.
     * @param online whether the bridge is connected.
     * @return the action, or null if nothing is to do.
     */
    fun onLocalSaved(path: String, localSha256: String?, state: SyncState, held: Set<String>, online: Boolean): SyncAction?

    /**
     * Decides the queued local changes after the connection is back or a turn ended. No side
     * effects; the queue is not changed.
     *
     * Each queued path is decided with [decide] from the files both sides have now, so a path
     * whose base moved on the remote side meanwhile gives `Merge` or `Conflict` instead of
     * `Upload`.
     *
     * @param pending the queued changes ([PendingQueue.ops]).
     * @param local the local files by relative path.
     * @param remote a manifest taken after the connection came back.
     * @param state the bases.
     * @param held the paths the agent is editing in a running turn.
     * @return the actions in queue order; a path that needs nothing has no action. The caller
     *   removes from the queue every path whose action is not a `Hold`.
     */
    fun replay(
        pending: List<PendingOp>,
        local: Map<String, FileEntry>,
        remote: Manifest,
        state: SyncState,
        held: Set<String>,
    ): List<SyncAction>
}

/**
 * The [SyncEngine] of the plugin.
 *
 * @property paths the path rules; `paths.caseInsensitiveLocal` switches the collision check on.
 * @property maxFileBytes per-file limit: a file above it is not transferred (`Skip(TOO_LARGE)`).
 * @property maxMergeBytes merge limit: if both sides changed a file above it, the result is
 *   `Conflict(TOO_LARGE)` instead of `Merge`.
 */
class DefaultSyncEngine(
    val paths: PathMapper = PathMapper(),
    val maxFileBytes: Long = DEFAULT_MAX_FILE_BYTES,
    val maxMergeBytes: Long = DEFAULT_MAX_MERGE_BYTES,
) : SyncEngine {

    override fun decide(
        path: String,
        baseSha256: String?,
        local: FileEntry?,
        remote: FileEntry?,
        held: Boolean,
    ): SyncAction? = core(path, baseSha256, local?.sha256, local?.size ?: 0, remote?.sha256, remote?.size ?: 0, held)

    override fun plan(
        local: Map<String, FileEntry>,
        remote: Manifest,
        state: SyncState,
        held: Set<String>,
    ): List<SyncAction> {
        val remoteFiles = remote.byPath()
        val all = sortedSetOf<String>().apply {
            addAll(local.keys)
            addAll(remoteFiles.keys)
            addAll(state.paths)
        }
        val colliding = if (paths.caseInsensitiveLocal) paths.caseCollisions(local.keys + remoteFiles.keys) else emptySet()
        val actions = ArrayList<SyncAction>()
        for (path in all) {
            if (paths.isExcluded(path)) continue
            val action = if (path in colliding) {
                SyncAction.Skip(path, SkipReason.CASE_COLLISION)
            } else {
                decide(path, state.base(path), local[path], remoteFiles[path], path in held)
            }
            if (action != null) actions.add(action)
        }
        return actions
    }

    override fun onRemoteChanged(
        change: RemoteChange,
        localSha256: String?,
        state: SyncState,
        dirtyInEditor: Boolean,
        held: Set<String>,
    ): SyncAction? {
        val path = change.path
        val base = state.base(path)
        val remoteSha = if (change.deleted) null else change.afterSha256
        val isHeld = path in held
        val action = core(path, base, localSha256, 0, remoteSha, 0, isHeld)
        if (!dirtyInEditor) return action
        return when (action) {
            is SyncAction.Download -> when {
                isHeld -> SyncAction.Hold(path, HoldReason.AGENT_EDITING)
                base == null || localSha256 == null -> SyncAction.Conflict(path, ConflictKind.CREATED_BOTH)
                else -> SyncAction.Merge(path, base, localSha256, action.expectedSha256)
            }
            is SyncAction.DeleteLocal ->
                if (isHeld) {
                    SyncAction.Hold(path, HoldReason.AGENT_EDITING)
                } else {
                    SyncAction.Conflict(path, ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY)
                }
            else -> action
        }
    }

    override fun onLocalSaved(
        path: String,
        localSha256: String?,
        state: SyncState,
        held: Set<String>,
        online: Boolean,
    ): SyncAction? {
        skipFor(path)?.let { return it }
        val base = state.base(path)
        return when {
            localSha256 == base -> null
            !online -> SyncAction.Hold(path, HoldReason.OFFLINE)
            path in held -> SyncAction.Hold(path, HoldReason.AGENT_EDITING)
            localSha256 == null -> SyncAction.DeleteRemote(path, base!!)
            else -> SyncAction.Upload(path, base)
        }
    }

    override fun replay(
        pending: List<PendingOp>,
        local: Map<String, FileEntry>,
        remote: Manifest,
        state: SyncState,
        held: Set<String>,
    ): List<SyncAction> {
        val remoteFiles = remote.byPath()
        return pending.mapNotNull { op ->
            decide(op.path, state.base(op.path), local[op.path], remoteFiles[op.path], op.path in held)
        }
    }

    /**
     * Returns the `Skip` of a path that breaks a path rule.
     *
     * @param path the path.
     * @return the skip action, or null if the path may be synchronised.
     */
    private fun skipFor(path: String): SyncAction.Skip? = when (paths.problem(path)) {
        null -> null
        PathProblem.EXCLUDED -> SyncAction.Skip(path, SkipReason.EXCLUDED)
        PathProblem.NOT_REPRESENTABLE_LOCALLY -> SyncAction.Skip(path, SkipReason.NOT_REPRESENTABLE_LOCALLY)
        else -> SyncAction.Skip(path, SkipReason.INVALID_PATH)
    }

    /**
     * The table of [SyncEngine] over hashes and sizes.
     *
     * @param path canonical relative path.
     * @param b the base hash, or null.
     * @param l the local hash, or null if there is no local file.
     * @param localSize size of the local file; 0 if unknown.
     * @param r the remote hash, or null if there is no remote file.
     * @param remoteSize size of the remote file; 0 if unknown.
     * @param held whether the agent is editing the path.
     * @return the action, or null if nothing is to do.
     */
    private fun core(
        path: String,
        b: String?,
        l: String?,
        localSize: Long,
        r: String?,
        remoteSize: Long,
        held: Boolean,
    ): SyncAction? {
        skipFor(path)?.let { return it }
        val localChanged = l != b
        val remoteChanged = r != b
        if (!localChanged && !remoteChanged) return null
        if (l == r) return SyncAction.Settle(path, l)
        if (!localChanged) {
            return when {
                r == null -> SyncAction.DeleteLocal(path)
                remoteSize > maxFileBytes -> SyncAction.Skip(path, SkipReason.TOO_LARGE)
                else -> SyncAction.Download(path, r)
            }
        }
        if (held) return SyncAction.Hold(path, HoldReason.AGENT_EDITING)
        if (!remoteChanged) {
            return when {
                l == null -> SyncAction.DeleteRemote(path, b!!)
                localSize > maxFileBytes -> SyncAction.Skip(path, SkipReason.TOO_LARGE)
                else -> SyncAction.Upload(path, b)
            }
        }
        return when {
            l == null -> SyncAction.Conflict(path, ConflictKind.MODIFIED_REMOTELY_DELETED_LOCALLY)
            r == null -> SyncAction.Conflict(path, ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY)
            b == null -> SyncAction.Conflict(path, ConflictKind.CREATED_BOTH)
            maxOf(localSize, remoteSize) > maxMergeBytes -> SyncAction.Conflict(path, ConflictKind.TOO_LARGE)
            else -> SyncAction.Merge(path, b, l, r)
        }
    }

    companion object {
        /** Default of [maxFileBytes]: 32 MiB (the setting `sync.max_file_mib`). */
        const val DEFAULT_MAX_FILE_BYTES: Long = 32L * 1024 * 1024

        /** Default of [maxMergeBytes]: 4 MiB. */
        const val DEFAULT_MAX_MERGE_BYTES: Long = 4L * 1024 * 1024
    }
}
