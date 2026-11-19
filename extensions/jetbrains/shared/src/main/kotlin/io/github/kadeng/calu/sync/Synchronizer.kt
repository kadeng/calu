/**
 * The synchronisation of one project copy with its remote directory: the part that reads and
 * writes. It asks the [SyncEngine] for each decision and carries it out on a [LocalFiles] and a
 * [RemoteFileStore]; it knows no IDE class, so the same code runs in tests against a real bridge.
 *
 *     bridge running ──connect()──► ide.paths, ide.fs.subscribe ──► queue replay ──► full comparison
 *     ide.fs.changed ──remoteChanged()──┐
 *     a local save   ──localChanged()───┼──► reconcile(path): base, local, remote ──► engine.decide ──► carry out
 *     the turn ended ──holdsChanged()───┘        │
 *     bridge gone    ──disconnected()            ├─ Download / Upload / Delete / Settle: done, base updated
 *                                                ├─ Merge: clean → both sides; else a conflict for the user
 *                                                ├─ Hold, offline → the pending queue (persisted)
 *                                                └─ Skip, a refusal of the bridge → a visible note
 *
 * Every public function takes one lock, so decisions never interleave; a request the bridge
 * refuses because the remote file moved (-32031) is decided again with the hash it reports.
 */
package io.github.kadeng.calu.sync

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException

/** Where the synchronisation stands. */
enum class SyncPhase {
    /** Not started (another mode, or "Sync: none"). */
    OFF,

    /** The bridge is asked for its paths and the subscription. */
    CONNECTING,

    /** The bridge cannot synchronise files (no `fs` capability); nothing is sent. */
    REFUSED,

    /** A plan waits for the user's confirmation (the first synchronisation, or many deletions). */
    WAITING,

    /** Files are compared or transferred. */
    SYNCING,

    /** Connected; nothing is being transferred. */
    IDLE,

    /** No connection; local changes are queued. */
    OFFLINE,

    /** The last comparison failed; `message` says why. "Sync now" tries again. */
    FAILED,
}

/**
 * What the status bar and the tool window show.
 *
 * @property phase where the synchronisation stands.
 * @property uploading files still to upload in the running batch.
 * @property downloading files still to download in the running batch.
 * @property held paths whose local change waits for the agent's turn to end.
 * @property conflicts paths that need the user's decision.
 * @property queued number of queued local changes (held ones included).
 * @property notes paths that are not synchronised, each with the reason.
 * @property message detail of the phase (why refused or failed, what waits), or null.
 */
data class SyncStatus(
    val phase: SyncPhase = SyncPhase.OFF,
    val uploading: Int = 0,
    val downloading: Int = 0,
    val held: List<String> = emptyList(),
    val conflicts: List<String> = emptyList(),
    val queued: Int = 0,
    val notes: Map<String, String> = emptyMap(),
    val message: String? = null,
)

/**
 * Write a number with its noun.
 *
 * @param n the number.
 * @param one the noun for 1.
 * @param many the noun for every other number.
 * @return e.g. "1 file", "3 files".
 */
private fun count(n: Int, one: String, many: String): String = "$n ${if (n == 1) one else many}"

/**
 * The one-line text of a status, for the status bar. No side effects.
 *
 * @param s the status.
 * @return "" for [SyncPhase.OFF]; otherwise a line that starts with "calu sync: ": the first that
 *   applies of: not available, connecting, offline (with the queued changes), waiting for the
 *   user, failed, a conflict (the first path, and how many more), files uploading or
 *   downloading, files held until the turn ends, "in sync" (with the number of skipped paths).
 */
fun syncStatusText(s: SyncStatus): String {
    val body = when {
        s.phase == SyncPhase.OFF -> return ""
        s.phase == SyncPhase.REFUSED -> "not available"
        s.phase == SyncPhase.CONNECTING -> "connecting"
        s.phase == SyncPhase.OFFLINE -> if (s.queued > 0) "offline: ${count(s.queued, "change", "changes")} queued" else "offline"
        s.phase == SyncPhase.WAITING -> "waits for your confirmation"
        s.phase == SyncPhase.FAILED -> "failed"
        s.conflicts.isNotEmpty() -> "conflict in ${s.conflicts[0]}" + if (s.conflicts.size > 1) " and ${s.conflicts.size - 1} more" else ""
        s.uploading > 0 && s.downloading > 0 -> "${count(s.downloading, "file", "files")} downloading, ${s.uploading} uploading"
        s.uploading > 0 -> "${count(s.uploading, "file", "files")} uploading"
        s.downloading > 0 -> "${count(s.downloading, "file", "files")} downloading"
        s.phase == SyncPhase.SYNCING -> "comparing"
        s.held.isNotEmpty() -> "${count(s.held.size, "file", "files")} held until the turn ends"
        s.notes.isNotEmpty() -> "in sync (${s.notes.size} skipped)"
        else -> "in sync"
    }
    return "calu sync: $body"
}

/**
 * A path both sides changed in ways that need the user.
 *
 * @property path relative path.
 * @property kind what happened.
 * @property baseSha256 the last synchronised hash, or null.
 * @property localSha256 hash of the local content (unsaved editor text included), null if no file.
 * @property remoteSha256 hash of the remote content, null if no file.
 */
data class SyncConflict(val path: String, val kind: ConflictKind, val baseSha256: String?, val localSha256: String?, val remoteSha256: String?)

/**
 * The three contents of a conflict, for the merge dialog.
 *
 * @property base the last synchronised content; null if it is not kept or the path has no base.
 * @property local the local content (the editor's unsaved text if there is some); null if no file.
 * @property remote the remote content; null if no file.
 */
class ConflictContents(val base: ByteArray?, val local: ByteArray?, val remote: ByteArray?)

/**
 * What a plan would do, for the confirmation before the first synchronisation.
 *
 * @property host what the remote side is called in the text (e.g. "sshbox:/home/dev/project").
 * @property downloads paths to copy from the server.
 * @property downloadBytes their total size.
 * @property uploads paths to copy to the server.
 * @property deleteLocal paths to delete locally.
 * @property deleteRemote paths to delete on the server.
 * @property conflicts paths that differ on both sides and are left alone until the user decides.
 * @property same number of paths that are equal on both sides.
 * @property localFiles number of files in the local copy before the plan.
 */
data class PlanSummary(
    val host: String,
    val downloads: List<String>,
    val downloadBytes: Long,
    val uploads: List<String>,
    val deleteLocal: List<String>,
    val deleteRemote: List<String>,
    val conflicts: List<String>,
    val same: Int,
    val localFiles: Int,
) {
    /** True if the local copy is empty and the plan only downloads: a clone. */
    val isClone: Boolean get() = localFiles == 0 && uploads.isEmpty() && deleteRemote.isEmpty() && conflicts.isEmpty()

    /**
     * Describe the plan for the user. No side effects.
     *
     * @return several lines: one per kind of change that occurs, with up to eight paths each,
     *   and a closing line that says that conflicting files are not overwritten.
     */
    fun text(): String {
        val out = StringBuilder()
        fun part(title: String, paths: List<String>) {
            if (paths.isEmpty()) return
            out.append(title).append(" (").append(paths.size).append("): ").append(paths.take(8).joinToString(", "))
            if (paths.size > 8) out.append(", ...")
            out.append('\n')
        }
        if (isClone) {
            out.append("The local folder is empty. Clone ${count(downloads.size, "file", "files")} (${downloadBytes / 1024} KiB) from $host?\n")
            return out.toString()
        }
        out.append("First synchronisation with $host. ${count(same, "file is", "files are")} equal on both sides.\n")
        part("Download", downloads)
        part("Upload", uploads)
        part("Delete here", deleteLocal)
        part("Delete on the server", deleteRemote)
        part("Differ on both sides (left alone; you decide each in the merge dialog)", conflicts)
        return out.toString()
    }
}

/** What the synchronisation needs from the user interface. Called from the synchroniser's coroutine. */
interface SyncUi {
    /**
     * Ask whether a plan may be applied (the first synchronisation of a copy, or a plan that
     * deletes many files).
     *
     * @param summary what the plan would do.
     * @return true to apply it; false leaves both sides as they are.
     */
    suspend fun confirm(summary: PlanSummary): Boolean

    /**
     * Report the progress of a batch.
     *
     * @param done actions carried out so far.
     * @param total actions of the batch.
     * @param path the path being worked on.
     */
    fun progress(done: Int, total: Int, path: String) {}

    /**
     * Report a new conflict that needs the user.
     *
     * @param conflict the conflict.
     */
    fun conflict(conflict: SyncConflict) {}

    /**
     * Tell the user something once (a clean merge, a file that could not be written).
     *
     * @param level "info", "warning" or "error".
     * @param text the message.
     */
    fun note(level: String, text: String) {}
}

/**
 * Synchronises one local project copy with the remote project directory.
 *
 * @param remote the remote side.
 * @param local the local side.
 * @param store the plugin's files about this copy (bases, queue, base contents).
 * @param held returns the relative paths the agent is editing in the running turn.
 * @param ui confirmation, progress, conflicts and notes.
 * @param hostLabel what the remote side is called in texts.
 * @param maxFileBytes the plugin's per-file limit; the smaller of it and the bridge's applies.
 * @param log receives lines for the IDE's log (paths and hashes, never contents).
 */
class Synchronizer(
    private val remote: RemoteFileStore,
    private val local: LocalFiles,
    private val store: SyncStore,
    private val held: () -> Set<String>,
    private val ui: SyncUi,
    private val hostLabel: String = "the server",
    private val maxFileBytes: Long = DefaultSyncEngine.DEFAULT_MAX_FILE_BYTES,
    private val log: (String) -> Unit = {},
) {
    /**
     * What is known about the remote file of a decision.
     *
     * @property entry the remote file; null if there is none.
     */
    private class Hint(val entry: FileEntry?)

    private val mutex = Mutex()
    @Volatile private var state: SyncState = store.loadState()
    private val queue: PendingQueue = store.loadQueue()
    private var engine: DefaultSyncEngine = DefaultSyncEngine(maxFileBytes = maxFileBytes)
    private var online = false
    private var epoch: String? = null
    private var seq = 0L
    private var phase = SyncPhase.OFF
    private var message: String? = null
    private var pendingUp = 0
    private var pendingDown = 0
    private val conflicts = LinkedHashMap<String, SyncConflict>()
    private val notes = java.util.TreeMap<String, String>()
    private var gitWarned = false
    private val statusFlow = MutableStateFlow(SyncStatus())

    /** The answer of `ide.paths` of the current connection; null before the first [connect]. */
    @Volatile var remotePaths: RemotePaths? = null
        private set

    /** The status; a new value after every change. */
    val status: StateFlow<SyncStatus> get() = statusFlow

    /**
     * List the synchronised paths inside a directory (for a directory the user deleted or moved).
     *
     * @param directory relative path of a directory.
     * @return every path with a base that lies below it.
     */
    fun pathsBelow(directory: String): List<String> = state.paths.filter { it.startsWith("$directory/") }

    /**
     * Tell whether a path was synchronised before.
     *
     * @param path relative path.
     * @return true if the path has a base.
     */
    fun isKnown(path: String): Boolean = state.base(path) != null

    /**
     * Return a conflict that waits for the user.
     *
     * @param path relative path.
     * @return the conflict, or null if the path has none.
     */
    fun conflictOf(path: String): SyncConflict? = synchronized(conflicts) { conflicts[path] }

    /**
     * Start synchronising over a bridge that just started: check its capability, ask its paths,
     * subscribe, replay the queue, and compare both sides unless the subscription continued
     * without a gap. Never throws but for a cancellation; a failure is in [status].
     *
     * @param capabilities the `capabilities` of `ide.initialize`.
     * @return true if the synchronisation is running afterwards.
     */
    suspend fun connect(capabilities: Collection<String>): Boolean = mutex.withLock {
        if ("fs" !in capabilities) {
            online = false
            publish(SyncPhase.REFUSED, "The calu on $hostLabel does not offer file synchronisation (its bridge reports no \"fs\" capability). Update calu on the server, or choose \"Sync: none\" in Settings | Tools | calu and synchronise the copy yourself.")
            return false
        }
        guarded {
            publish(SyncPhase.CONNECTING, null)
            val p = remote.paths()
            remotePaths = p
            engine = DefaultSyncEngine(PathMapper(remoteRoot = p.root), minOf(maxFileBytes, p.limits.fileBytes))
            val known = epoch
            val sub = remote.subscribe(if (known != null) seq else null, known)
            online = true
            val continued = known != null && sub.complete && sub.epoch == known
            epoch = sub.epoch
            if (!continued) seq = sub.seq
            log("[calu] sync: connected to ${p.root} (epoch ${sub.epoch}, seq ${sub.seq}, ${if (continued) "continued" else "full comparison"})")
            replayQueue()
            if (continued) publish(SyncPhase.IDLE, null) else fullSync()
        }
        online
    }

    /** Note that the bridge is gone: local changes are queued from now on. */
    suspend fun disconnected() = mutex.withLock {
        online = false
        if (phase != SyncPhase.OFF && phase != SyncPhase.REFUSED) publish(SyncPhase.OFFLINE, null)
    }

    /**
     * Compare both sides completely and carry out what needs no user ("Sync now"). Never throws
     * but for a cancellation; a failure is in [status].
     */
    suspend fun syncNow() = mutex.withLock {
        if (!online) return@withLock
        guarded {
            replayQueue()
            fullSync()
        }
    }

    /**
     * Handle an `ide.fs.changed` notification. A gap in the sequence numbers or another epoch
     * leads to a full comparison (the resync rule of the spec). A path the bridge reports as
     * skipped is added to the list of paths that are not synchronised at once; its local copy
     * is left as it is.
     *
     * @param n the notification.
     */
    suspend fun remoteChanged(n: FsChanged) = mutex.withLock {
        if (!online || phase == SyncPhase.WAITING) return@withLock
        guarded {
            if (n.epoch == epoch && n.seq <= seq) return@guarded
            if (n.epoch != epoch || n.seq != seq + 1) {
                log("[calu] sync: gap (epoch ${n.epoch}, seq ${n.seq} after $seq): full comparison")
                epoch = n.epoch
                seq = n.seq
                fullSync()
                return@guarded
            }
            seq = n.seq
            for (skip in n.skipped) notes[skip.path] = "on the server: " + skipText(skip.reasonText)
            val skippedNow = n.skipped.map { it.path }.toSet()
            if (skippedNow.isNotEmpty()) publish(phase, message)
            val todo = n.changes.filterNot { (it.isOwnWrite && state.base(it.path) == it.afterSha256) || it.path in skippedNow }
            batch(todo.map { it.path }) { path ->
                val c = todo.first { it.path == path }
                val hint = when {
                    c.deleted -> Hint(null)
                    c.afterSha256 != null -> Hint(FileEntry(path, 0, FileEntry.DEFAULT_MODE, c.afterSha256))
                    else -> null
                }
                reconcile(path, hint, false, 0)
            }
        }
    }

    /**
     * Handle local saves, creations and deletions. While offline the paths are queued.
     *
     * @param paths relative paths that changed on disk (a path may no longer exist).
     */
    suspend fun localChanged(paths: Collection<String>) = mutex.withLock {
        if (phase == SyncPhase.OFF || phase == SyncPhase.REFUSED) return@withLock
        guarded {
            val mine = paths.distinct().filter { engine.paths.problem(it) == null && local.leftOut(it) == null }
            if (!online || phase == SyncPhase.WAITING) {
                for (path in mine) enqueueIfChanged(path)
                store.saveQueue(queue)
                publish(phase, message)
                return@guarded
            }
            batch(mine) { path ->
                // The remote file is assumed to be the base; the bridge checks that (base_sha256).
                val base = state.base(path)
                reconcile(path, Hint(base?.let { FileEntry(path, 0, FileEntry.DEFAULT_MODE, it) }), false, 0)
            }
        }
    }

    /**
     * Handle a local rename of one file: `ide.fs.rename` if the content is the base of the old
     * path, otherwise a deletion and an upload.
     *
     * @param from the relative path the file had.
     * @param to the relative path it has now.
     */
    suspend fun localRenamed(from: String, to: String) {
        val renamed = mutex.withLock {
            val base = state.base(from) ?: return@withLock false
            if (!online || phase != SyncPhase.IDLE || state.base(to) != null) return@withLock false
            if (engine.paths.problem(to) != null || local.leftOut(to) != null) return@withLock false
            if (local.entry(to)?.sha256 != base || local.entry(from) != null) return@withLock false
            if (from in held() || to in held()) return@withLock false
            try {
                remote.rename(from, to, base)
                state = state.with(from, null).with(to, base)
                store.saveState(state)
                log("[calu] sync: renamed $from -> $to")
                true
            } catch (error: FsException) {
                log("[calu] sync: rename $from -> $to not done on the server (${error.message}); as delete and upload")
                false
            }
        }
        if (!renamed) localChanged(listOf(from, to))
    }

    /** Decide the queued changes again: a turn ended, or a hold was lifted. */
    suspend fun holdsChanged() = mutex.withLock {
        if (!online || queue.isEmpty() || phase == SyncPhase.WAITING) {
            publish(phase, message)
            return@withLock
        }
        guarded {
            replayQueue()
            publish(SyncPhase.IDLE, null)
        }
    }

    /**
     * Upload a held file now, although the agent edits it ("Upload now"; the base check still applies).
     *
     * @param path relative path.
     */
    suspend fun uploadNow(path: String) = mutex.withLock {
        if (!online) return@withLock
        guarded {
            reconcile(path, null, true, 0)
            persist()
            publish(SyncPhase.IDLE, null)
        }
    }

    /**
     * Read the three contents of a conflict for the merge dialog.
     *
     * @param path relative path of a conflict.
     * @return the contents; null if the path has no conflict any more.
     * @throws FsException if the remote content cannot be read.
     */
    suspend fun conflictContents(path: String): ConflictContents? = mutex.withLock {
        val c = conflictOf(path) ?: return@withLock null
        val mine = local.unsaved(path) ?: local.read(path)
        val theirs = try {
            remote.read(path).bytes
        } catch (error: FsException) {
            if (error.failure is FsFailure.NotFound) null else throw error
        }
        ConflictContents(c.baseSha256?.let { store.bases.get(it) }, mine, theirs)
    }

    /**
     * Resolve a conflict with the content the user chose: it is written to both sides and
     * becomes the base. Never throws but for a cancellation; a failure is noted and the conflict stays.
     *
     * @param path relative path of a conflict.
     * @param result the content both sides get; null deletes the file on both sides.
     * @return true if both sides have the result afterwards.
     */
    suspend fun resolve(path: String, result: ByteArray?): Boolean = mutex.withLock {
        if (!online) return@withLock false
        try {
            val m = remote.manifest(paths = listOf(path))
            val theirs = m.files[path]
            if (result == null) {
                local.delete(path)
                if (theirs != null) remote.delete(path, theirs.sha256)
                state = state.with(path, null)
            } else {
                val sha = sha256Hex(result)
                if ((local.unsaved(path) ?: local.read(path))?.let(::sha256Hex) != sha) local.write(path, result, null)
                if (theirs?.sha256 != sha) remote.write(path, result, theirs?.sha256)
                store.bases.put(result)
                state = state.with(path, sha)
            }
            synchronized(conflicts) { conflicts.remove(path) }
            queue.remove(path)
            persist()
            log("[calu] sync: conflict in $path resolved")
            publish(phase, message)
            true
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            ui.note("warning", "The conflict in $path was not resolved: ${error.message}")
            false
        }
    }

    /**
     * Run a block and turn its failures into a status: a lost connection into OFFLINE, anything
     * else into FAILED with the message. A cancellation saves the state and is passed on.
     *
     * @param block the work.
     */
    private suspend fun guarded(block: suspend () -> Unit) {
        try {
            block()
        } catch (error: CancellationException) {
            persist()
            pendingUp = 0
            pendingDown = 0
            if (phase == SyncPhase.SYNCING) publish(SyncPhase.WAITING, "The synchronisation was cancelled. \"Sync now\" continues it.")
            throw error
        } catch (error: FsException) {
            persist()
            pendingUp = 0
            pendingDown = 0
            if (error.failure is FsFailure.Offline) {
                online = false
                publish(SyncPhase.OFFLINE, null)
            } else {
                log("[calu] sync: ${error.message}")
                publish(SyncPhase.FAILED, error.message)
            }
        } catch (error: IOException) {
            persist()
            pendingUp = 0
            pendingDown = 0
            log("[calu] sync: local file error: ${error.message}")
            publish(SyncPhase.FAILED, "A local file could not be read or written: ${error.message}")
        }
    }

    /**
     * Decide a set of paths one after the other, with the counters of the status.
     *
     * @param paths the paths.
     * @param each decides and carries out one path.
     */
    private suspend fun batch(paths: List<String>, each: suspend (String) -> Unit) {
        if (paths.isEmpty()) return
        val before = phase
        for (path in paths) each(path)
        pendingUp = 0
        pendingDown = 0
        persist()
        publish(if (before == SyncPhase.SYNCING || before == SyncPhase.CONNECTING) SyncPhase.IDLE else before.takeIf { it != SyncPhase.FAILED } ?: SyncPhase.IDLE, null)
    }

    /** Store bases and queue. */
    private fun persist() {
        store.saveState(state)
        store.saveQueue(queue)
    }

    /**
     * Publish a status built from the fields.
     *
     * @param next the phase.
     * @param text its detail.
     */
    private fun publish(next: SyncPhase, text: String?) {
        phase = next
        message = text
        val heldNow = if (online) queue.ops().map { it.path } else emptyList()
        statusFlow.value = SyncStatus(next, pendingUp, pendingDown, heldNow, synchronized(conflicts) { conflicts.keys.toList() }, queue.size, HashMap(notes), text)
    }

    /**
     * Put a path into the queue if its local content differs from its base.
     *
     * @param path relative path.
     */
    private suspend fun enqueueIfChanged(path: String) {
        val entry = local.entry(path)
        if (entry?.sha256 == state.base(path)) return
        if (entry == null) queue.deleted(path) else queue.saved(path)
    }

    /**
     * Decide the queued paths that are not held, with a manifest of exactly those paths.
     *
     * @throws FsException if the manifest cannot be read.
     */
    private suspend fun replayQueue() {
        val heldNow = held()
        val ops = queue.ops().filter { it.path !in heldNow }
        if (ops.isEmpty()) return
        val m = remote.manifest(paths = ops.map { it.path })
        for (op in ops) {
            val hint = when {
                op.path in m.files -> Hint(m.files[op.path])
                op.path in m.missing -> Hint(null)
                else -> {
                    notes[op.path] = "not listed by the server (ignored or special there)"
                    queue.remove(op.path)
                    continue
                }
            }
            reconcile(op.path, hint, false, 0)
        }
        persist()
    }

    /**
     * Compare both sides completely and carry out the plan. The first plan of a copy, and a
     * plan that deletes many files, is confirmed by the user first.
     *
     * @throws FsException if the manifest cannot be read.
     */
    private suspend fun fullSync() {
        publish(SyncPhase.SYNCING, null)
        val started = System.nanoTime()
        val m = remote.manifest()
        val listing = local.list(engine.maxFileBytes)
        notes.clear()
        for (s in m.skipped) notes[s.path] = "on the server: " + skipText(s.reasonText)
        for (s in listing.skipped) notes.putIfAbsent(s.path, "here: " + skipText(s.reason))
        if (local.hasOwnRepository()) {
            notes[".git"] = LOCAL_GIT_NOTE
            if (!gitWarned) {
                gitWarned = true
                ui.note("warning", localGitWarning(hostLabel))
            }
        }
        val remoteSkipped = m.skipped.map { it.path }.toSet()
        val localFiles = listing.files.filterKeys { it !in remoteSkipped }
        var plan = engine.plan(localFiles, m.toManifest(), state, held()).filter { it.path !in remoteSkipped }
        // A file the server does not list is deleted here only if the server says it is gone:
        // a file that git ignores there is not in the listing either.
        val gone = plan.filterIsInstance<SyncAction.DeleteLocal>().map { it.path }
        if (gone.isNotEmpty()) {
            val check = remote.manifest(paths = gone)
            plan = plan.filter { it !is SyncAction.DeleteLocal || it.path in check.missing }
            for (path in gone) if (path !in check.missing) notes.putIfAbsent(path, "not listed by the server (ignored or special there)")
        }
        val summary = summarize(plan, m, localFiles.size)
        val first = state.paths.isEmpty()
        val changes = summary.downloads.size + summary.uploads.size + summary.deleteLocal.size + summary.deleteRemote.size + summary.conflicts.size
        val deletes = summary.deleteLocal.size + summary.deleteRemote.size
        val manyDeletes = deletes > maxOf(MANY_DELETES, state.paths.size / 10)
        if ((first && changes > 0) || manyDeletes) {
            publish(SyncPhase.WAITING, if (summary.isClone) "The local folder is empty: ${summary.downloads.size} files can be cloned from $hostLabel." else "A plan waits for your confirmation.")
            if (!ui.confirm(summary)) {
                publish(SyncPhase.WAITING, "The synchronisation with $hostLabel was not started. \"Sync now\" asks again.")
                return
            }
            publish(SyncPhase.SYNCING, null)
        }
        pendingDown = summary.downloads.size
        pendingUp = summary.uploads.size
        publish(SyncPhase.SYNCING, null)
        for ((i, action) in plan.withIndex()) {
            ui.progress(i, plan.size, action.path)
            reconcile(action.path, Hint(m.files[action.path]), false, 0)
            if (action is SyncAction.Download && pendingDown > 0) pendingDown--
            if (action is SyncAction.Upload && pendingUp > 0) pendingUp--
            if (i % 10 == 9) publish(SyncPhase.SYNCING, null)
            if (i % 50 == 49) store.saveState(state)
        }
        pendingUp = 0
        pendingDown = 0
        state = state.withHead(m.head)
        persist()
        store.bases.prune(state.paths.mapNotNull { state.base(it) }.toSet())
        log("[calu] sync: full comparison of ${m.files.size} remote and ${localFiles.size} local files, ${plan.size} actions, ${(System.nanoTime() - started) / 1_000_000} ms")
        publish(SyncPhase.IDLE, null)
    }

    /**
     * Count a plan by kind.
     *
     * @param plan the actions.
     * @param m the manifest the plan was made from (for sizes).
     * @param localCount number of local files.
     * @return the summary.
     */
    private fun summarize(plan: List<SyncAction>, m: RemoteManifest, localCount: Int): PlanSummary {
        val downloads = plan.filterIsInstance<SyncAction.Download>().map { it.path }
        return PlanSummary(
            host = hostLabel,
            downloads = downloads,
            downloadBytes = downloads.sumOf { m.files[it]?.size ?: 0 },
            uploads = plan.filterIsInstance<SyncAction.Upload>().map { it.path },
            deleteLocal = plan.filterIsInstance<SyncAction.DeleteLocal>().map { it.path },
            deleteRemote = plan.filterIsInstance<SyncAction.DeleteRemote>().map { it.path },
            conflicts = plan.filter { it is SyncAction.Conflict || it is SyncAction.Merge }.map { it.path },
            same = plan.count { it is SyncAction.Settle && it.sha256 != null },
            localFiles = localCount,
        )
    }

    /**
     * Decide one path from what both sides hold now and carry the decision out.
     *
     * @param path relative path.
     * @param hint the remote file if the caller knows it; null to ask the bridge.
     * @param duringTurn true to send the change although the agent edits the file.
     * @param depth how often this path was decided again in one go; above [MAX_REDECIDE] the path
     *   is left with a note (both sides keep changing).
     * @throws FsException `Offline` and `NoManifest`; every other failure of a request is handled here.
     * @throws IOException if a local file cannot be read or written.
     */
    private suspend fun reconcile(path: String, hint: Hint?, duringTurn: Boolean, depth: Int) {
        if (depth > MAX_REDECIDE) {
            notes[path] = "changes on both sides too quickly; \"Sync now\" tries again"
            return
        }
        if (engine.paths.problem(path) != null) return
        when (local.leftOut(path)) {
            null -> {}
            "symlink" -> {
                notes[path] = "here: " + skipText("symlink")
                return
            }
            else -> return
        }
        val theirs: FileEntry? = if (hint != null) {
            hint.entry
        } else {
            val m = remote.manifest(paths = listOf(path))
            when {
                path in m.files -> m.files[path]
                path in m.missing -> null
                else -> {
                    notes[path] = m.skipped.firstOrNull { it.path == path }?.let { "on the server: " + skipText(it.reasonText) } ?: "not listed by the server (ignored or special there)"
                    queue.remove(path)
                    return
                }
            }
        }
        val base = state.base(path)
        val disk = local.entry(path)
        val unsaved = local.unsaved(path)
        val mine = if (unsaved != null) FileEntry(path, unsaved.size.toLong(), disk?.mode ?: FileEntry.DEFAULT_MODE, sha256Hex(unsaved)) else disk
        val isHeld = !duringTurn && path in held()
        var action = engine.decide(path, base, mine, theirs, isHeld)
        // Unsaved text is merged with a remote change, but it is not uploaded before it is saved.
        if (unsaved != null && (action is SyncAction.Upload || action is SyncAction.DeleteRemote || action is SyncAction.Hold)) {
            action = engine.decide(path, base, disk, theirs, isHeld)
        }
        when (action) {
            null -> {
                queue.remove(path)
                synchronized(conflicts) { conflicts.remove(path) }
            }
            is SyncAction.Settle -> {
                if (action.sha256 != null) (unsaved ?: local.read(path))?.let { store.bases.put(it) }
                state = state.with(path, action.sha256)
                done(path)
            }
            is SyncAction.Download -> {
                val content = try {
                    remote.read(path)
                } catch (error: FsException) {
                    if (handled(path, error, mine)) return
                    if (error.failure is FsFailure.NotFound) return reconcile(path, Hint(null), duringTurn, depth + 1)
                    throw error
                }
                local.write(path, content.bytes, content.mode)
                store.bases.put(content.bytes)
                state = state.with(path, content.sha256)
                done(path)
                log("[calu] sync: downloaded $path (${content.sha256.take(12)}, ${content.bytes.size} bytes)")
            }
            is SyncAction.Upload -> {
                val bytes = local.read(path) ?: return reconcile(path, Hint(theirs), duringTurn, depth + 1)
                try {
                    remote.write(path, bytes, action.baseSha256, disk?.mode, duringTurn)
                } catch (error: FsException) {
                    if (handled(path, error, mine)) return
                    val f = error.failure
                    if (f is FsFailure.Mismatch) return reconcile(path, Hint(f.sha256?.let { FileEntry(path, f.size, FileEntry.DEFAULT_MODE, it) }), duringTurn, depth + 1)
                    throw error
                }
                state = state.with(path, store.bases.put(bytes))
                done(path)
                log("[calu] sync: uploaded $path (${sha256Hex(bytes).take(12)}, ${bytes.size} bytes)")
            }
            is SyncAction.DeleteLocal -> {
                local.delete(path)
                state = state.with(path, null)
                done(path)
                log("[calu] sync: deleted here: $path")
            }
            is SyncAction.DeleteRemote -> {
                try {
                    remote.delete(path, action.baseSha256, duringTurn)
                } catch (error: FsException) {
                    if (handled(path, error, mine)) return
                    val f = error.failure
                    if (f is FsFailure.Mismatch) return reconcile(path, Hint(f.sha256?.let { FileEntry(path, f.size, FileEntry.DEFAULT_MODE, it) }), duringTurn, depth + 1)
                    throw error
                }
                state = state.with(path, null)
                done(path)
                log("[calu] sync: deleted on the server: $path")
            }
            is SyncAction.Merge -> merge(path, action, unsaved ?: local.read(path), duringTurn, depth)
            is SyncAction.Conflict -> conflict(SyncConflict(path, action.kind, base, mine?.sha256, theirs?.sha256))
            is SyncAction.Hold -> {
                if (mine == null) queue.deleted(path) else queue.saved(path)
            }
            is SyncAction.Skip -> notes[path] = when (action.reason) {
                SkipReason.TOO_LARGE -> "larger than the limit of ${engine.maxFileBytes / (1024 * 1024)} MiB"
                SkipReason.CASE_COLLISION -> "differs from another path only in letter case"
                SkipReason.NOT_REPRESENTABLE_LOCALLY -> "this machine cannot hold a file of that name"
                SkipReason.EXCLUDED -> "in a directory that is never synchronised"
                SkipReason.INVALID_PATH -> "not a valid path"
            }
        }
    }

    /**
     * Merge a file both sides changed. A clean result goes to both sides; otherwise the path
     * becomes a conflict.
     *
     * @param path relative path.
     * @param action the decision with the three hashes.
     * @param mineBytes the local content (the editor's unsaved text if there is some); null if
     *   the file vanished meanwhile.
     * @param duringTurn true to write although the agent edits the file.
     * @param depth see [reconcile].
     * @throws FsException `Offline`.
     * @throws IOException if the local file cannot be written.
     */
    private suspend fun merge(path: String, action: SyncAction.Merge, mineBytes: ByteArray?, duringTurn: Boolean, depth: Int) {
        if (mineBytes == null) return reconcile(path, null, duringTurn, depth + 1)
        val theirs = try {
            remote.read(path)
        } catch (error: FsException) {
            if (error.failure is FsFailure.NotFound) return reconcile(path, Hint(null), duringTurn, depth + 1)
            if (handled(path, error, null)) return
            throw error
        }
        when (val outcome = mergeContents(store.bases.get(action.baseSha256), mineBytes, theirs.bytes)) {
            is MergeOutcome.Unresolved -> conflict(SyncConflict(path, outcome.kind, action.baseSha256, sha256Hex(mineBytes), theirs.sha256))
            is MergeOutcome.Merged -> {
                val merged = outcome.bytes
                val sha = sha256Hex(merged)
                local.write(path, merged, null)
                // From here the local file is "theirs plus my change": the base is theirs.
                store.bases.put(theirs.bytes)
                state = state.with(path, theirs.sha256)
                if (sha != theirs.sha256) {
                    try {
                        remote.write(path, merged, theirs.sha256, null, duringTurn)
                    } catch (error: FsException) {
                        if (handled(path, error, FileEntry(path, merged.size.toLong(), FileEntry.DEFAULT_MODE, sha))) return
                        val f = error.failure
                        if (f is FsFailure.Mismatch) return reconcile(path, Hint(f.sha256?.let { FileEntry(path, f.size, FileEntry.DEFAULT_MODE, it) }), duringTurn, depth + 1)
                        throw error
                    }
                }
                state = state.with(path, store.bases.put(merged))
                done(path)
                log("[calu] sync: merged $path (${sha.take(12)})")
                ui.note("info", "calu sync merged your change of $path with the change on $hostLabel.")
            }
        }
    }

    /**
     * Take a path out of the queue and the conflicts after its sides agree.
     *
     * @param path relative path.
     */
    private fun done(path: String) {
        queue.remove(path)
        notes.remove(path)
        synchronized(conflicts) { conflicts.remove(path) }
    }

    /**
     * Register a conflict and tell the user interface about a new one.
     *
     * @param c the conflict.
     */
    private fun conflict(c: SyncConflict) {
        val known = synchronized(conflicts) { conflicts.put(c.path, c) }
        queue.remove(c.path)
        if (known != c) {
            log("[calu] sync: conflict in ${c.path} (${c.kind})")
            ui.conflict(c)
        }
    }

    /**
     * Handle the failures of a request that end the decision of a path without a new decision:
     * held (queued), too large, not regular, refused (noted), I/O and upload errors (noted and queued).
     *
     * @param path relative path.
     * @param error the failure.
     * @param mine the local file, to know whether a queued entry is a save or a deletion; null
     *   if nothing is to queue.
     * @return true if the failure was handled here; false for `Mismatch`, `NotFound`, `Offline`
     *   and `NoManifest`, which the caller handles.
     */
    private fun handled(path: String, error: FsException, mine: FileEntry?): Boolean {
        fun requeue() {
            if (mine == null) queue.deleted(path) else queue.saved(path)
        }
        when (val f = error.failure) {
            is FsFailure.Held -> requeue()
            is FsFailure.TooLarge -> notes[path] = "larger than the server's limit" + if (f.limit > 0) " of ${f.limit / (1024 * 1024)} MiB" else ""
            is FsFailure.NotRegular -> notes[path] = "on the server: " + skipText(f.kind)
            is FsFailure.Refused -> notes[path] = "refused by the server (${f.reason})"
            is FsFailure.Io -> {
                notes[path] = "the server could not write it: ${f.text}"
                ui.note("warning", "calu sync: $path was not written on $hostLabel: ${f.text}")
            }
            is FsFailure.UploadFailed -> notes[path] = "the upload failed (${f.reason}); \"Sync now\" tries again"
            is FsFailure.TargetExists -> notes[path] = "a file of that name exists on the server"
            is FsFailure.Other -> {
                notes[path] = "the server answered: ${f.text}"
                log("[calu] sync: $path: ${error.message}")
            }
            is FsFailure.Mismatch, FsFailure.NotFound, is FsFailure.Offline, is FsFailure.NoManifest -> return false
        }
        return true
    }

    /**
     * Word a skip reason of either side.
     *
     * @param reason the bridge's word ("symlink", "too-large", "not-regular", "submodule",
     *   "unsupported-name", "directory", "fifo", ...).
     * @return the text for the list of skipped paths.
     */
    private fun skipText(reason: String): String = when (reason) {
        "symlink" -> "a symbolic link (never followed, not synchronised)"
        "too-large" -> "larger than the limit of ${engine.maxFileBytes / (1024 * 1024)} MiB"
        "not-regular" -> "not a regular file"
        "submodule" -> "a git submodule"
        "unsupported-name" -> "a name the protocol cannot carry"
        "directory" -> "a directory"
        else -> reason
    }

    companion object {
        /** A plan that deletes more files than this (and more than a tenth of the copy) is confirmed first. */
        const val MANY_DELETES: Int = 20

        /** How often one path is decided again in one go when the remote side keeps moving. */
        const val MAX_REDECIDE: Int = 3
    }
}

/** The note on `.git` in the list of paths that are not synchronised. */
const val LOCAL_GIT_NOTE: String = "this copy has a git repository of its own; .git is never synchronised (the server's repository is the one calu and Claude use)"

/**
 * The warning shown once per IDE start when the local copy has a `.git` of its own.
 *
 * @param host where the server's copy is ("host:dir").
 * @return the text.
 */
fun localGitWarning(host: String): String =
    "This folder has a .git of its own (git init, PyCharm's \"Enable Version Control Integration\", or a clone into a repository). " +
        "calu never synchronises .git in either direction: the repository on $host is the one calu and Claude commit to. " +
        "Git in this IDE shows the local repository, which does not follow the server's commits; run git on the server " +
        "(in an SSH terminal, or ask Claude)."

/** What the project view shows next to a file or folder of a synchronised copy. */
enum class SyncMark(val text: String) {
    /** The file differs on both sides and waits for the user's decision. */
    CONFLICT("sync conflict: Merge in the calu tool window"),

    /** A folder with a conflict somewhere below it. */
    CONFLICT_BELOW("contains a sync conflict"),

    /** The file's local change waits until the agent's turn ends. */
    HELD("held until the turn ends"),
}

/**
 * Decide the project view's mark of one path.
 *
 * @param status the synchronisation's status.
 * @param path the path relative to the project directory ("" for the root).
 * @param directory true for a folder.
 * @return the mark, or null for none.
 */
fun syncMark(status: SyncStatus, path: String, directory: Boolean): SyncMark? = when {
    !directory && path in status.conflicts -> SyncMark.CONFLICT
    !directory && path in status.held -> SyncMark.HELD
    directory && status.conflicts.any { path.isEmpty() || it.startsWith("$path/") } -> SyncMark.CONFLICT_BELOW
    else -> null
}

