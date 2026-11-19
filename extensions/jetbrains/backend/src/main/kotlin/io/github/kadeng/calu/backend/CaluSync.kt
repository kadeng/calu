/**
 * The file synchronisation of the SSH mode inside the IDE: the wiring of the pure parts of
 * `shared/sync` to the project.
 *
 *     settings: mode "ssh", sync "calu"
 *        │
 *     CaluSync ── Synchronizer (decisions carried out) ── RemoteFileStore ── supervisor.request ── calu ide
 *        │             │                └─ VfsLocalFiles (documents, VFS)
 *        │             └─ SyncStore in <IDE system dir>/calu-sync/<id>  (bases, queue, base contents)
 *        ├─ SyncDriver: bridge status and ide.fs.changed, in order
 *        ├─ VFS listener: saves, creations, deletions, moves ──(300 ms)──► localChanged / localRenamed
 *        ├─ status ──► the chat's state (`ui.sync`): status bar, cards
 *        └─ user interface: confirmation of the first plan, a cancellable progress indicator, merge dialog
 *
 * With "Sync: none" nothing of this is created and no `ide.fs.*` request is sent.
 */
package io.github.kadeng.calu.backend

import com.intellij.diff.DiffManager
import com.intellij.ide.projectView.ProjectView
import com.intellij.diff.DiffRequestFactory
import com.intellij.diff.merge.MergeResult
import com.intellij.notification.Notification
import com.intellij.notification.NotificationAction
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.PathManager
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.fileTypes.FileTypeManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.vfs.VirtualFileManager
import com.intellij.openapi.vfs.newvfs.BulkFileListener
import com.intellij.openapi.vfs.newvfs.events.VFileCopyEvent
import com.intellij.openapi.vfs.newvfs.events.VFileDeleteEvent
import com.intellij.openapi.vfs.newvfs.events.VFileEvent
import com.intellij.openapi.vfs.newvfs.events.VFileMoveEvent
import com.intellij.openapi.vfs.newvfs.events.VFilePropertyChangeEvent
import com.intellij.platform.ide.progress.withBackgroundProgress
import com.intellij.util.messages.MessageBusConnection
import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.chat.ChatController
import io.github.kadeng.calu.sync.ConflictKind
import io.github.kadeng.calu.sync.PathMapper
import io.github.kadeng.calu.sync.PlanSummary
import io.github.kadeng.calu.sync.RemoteFileStore
import io.github.kadeng.calu.sync.SyncConflict
import io.github.kadeng.calu.sync.SyncDriver
import io.github.kadeng.calu.sync.SyncMark
import io.github.kadeng.calu.sync.SyncPhase
import io.github.kadeng.calu.sync.SyncStatus
import io.github.kadeng.calu.sync.SyncStore
import io.github.kadeng.calu.sync.SyncUi
import io.github.kadeng.calu.sync.Synchronizer
import io.github.kadeng.calu.sync.deploymentWarning
import io.github.kadeng.calu.sync.heldPaths
import io.github.kadeng.calu.sync.parseDeployment
import io.github.kadeng.calu.sync.isProbablyBinary
import io.github.kadeng.calu.sync.syncMark
import io.github.kadeng.calu.sync.syncStatusText
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.nio.file.Files
import java.nio.file.Path

/** Milliseconds the listener waits after a local change for further ones before it synchronises. */
const val LOCAL_CHANGE_DEBOUNCE_MS: Long = 300

/** Id of the notification group (registered by the frontend module). */
private const val SYNC_NOTIFICATION_GROUP = "calu"

/**
 * Build the `ui.sync` event of a status.
 *
 * @param status the synchronisation's status.
 * @return the event's params (`{}` for a synchronisation that is off).
 */
fun syncEvent(status: SyncStatus): JsonObject = if (status.phase == SyncPhase.OFF) {
    JsonObject(emptyMap())
} else {
    buildJsonObject {
        put("phase", JsonPrimitive(status.phase.name.lowercase()))
        put("text", JsonPrimitive(syncStatusText(status)))
        status.message?.let { put("message", JsonPrimitive(it)) }
        put("conflicts", JsonArray(status.conflicts.map { JsonPrimitive(it) }))
        put("held", JsonArray(status.held.map { JsonPrimitive(it) }))
        put("notes", JsonArray(status.notes.map { JsonPrimitive("${it.key}: ${it.value}") }))
        put("queued", JsonPrimitive(status.queued))
    }
}

/**
 * The file synchronisation of one project in SSH mode. Created by `CaluProjectService` with the
 * bridge's wiring and disposed with it.
 *
 * @param project the project (the local copy).
 * @param supervisor the bridge.
 * @param controller the chat (state for the holds, notices, the `ui.sync` event).
 * @param host the SSH destination, for texts and for the name of the state directory.
 * @param remoteDir the remote project directory of the settings.
 * @param parent the scope the synchronisation's own scope is a child of.
 * @param log receives lines for the IDE's log.
 */
class CaluSync(
    private val project: Project,
    private val supervisor: BridgeSupervisor,
    private val controller: ChatController,
    private val host: String,
    private val remoteDir: String,
    parent: CoroutineScope,
    private val log: (String) -> Unit,
) : SyncUi {
    private val scope = CoroutineScope(parent.coroutineContext + SupervisorJob(parent.coroutineContext.job) + Dispatchers.Default)
    private val root: Path = Path.of(project.basePath ?: ".").toAbsolutePath().normalize()
    private val local = VfsLocalFiles(project, root)
    private val store = SyncStore(Path.of(PathManager.getSystemPath(), "calu-sync", io.github.kadeng.calu.sync.syncStoreName(root.toString(), host, remoteDir)))
    private val remote = RemoteFileStore({ method, params -> supervisor.request(method, params, interactive = false) })

    /** The synchroniser; its `remotePaths` give the server's resolved root for the path mapping. */
    val sync = Synchronizer(remote, local, store, ::heldRelative, this, "$host:$remoteDir", log = log)
    private val driver = SyncDriver(sync, supervisor, scope, log) { work -> withProgress("calu: synchronising with $host", work) }
    private var connection: MessageBusConnection? = null
    private val pendingLock = Any()
    private val pendingPaths = LinkedHashSet<String>()
    private val pendingRenames = ArrayList<Pair<String, String>>()
    private var flushJob: Job? = null

    /** The open conflict notification of each path; expired when the path's conflict is gone. */
    private val conflictNotes = HashMap<String, Notification>()

    /** Start: listen to the bridge and to local file changes, and publish the status. */
    fun start() {
        scope.launch { sync.status.collect { controller.dispatch("ui.sync", syncEvent(it)) } }
        // A conflict resolved in the dialog, by "Sync now" or on the other side closes its
        // notification, and its line in the transcript is marked "resolved" (it stays).
        scope.launch {
            var before = emptySet<String>()
            sync.status.map { it.conflicts.toSet() }.distinctUntilChanged().collect { open ->
                val gone = synchronized(conflictNotes) { conflictNotes.keys.filter { it !in open }.mapNotNull { conflictNotes.remove(it) } }
                gone.forEach { it.expire() }
                for (path in before - open) controller.noticeResolved(conflictKey(path))
                before = open
            }
        }
        // The project view marks conflicted and held files; it is refreshed when they change.
        scope.launch {
            sync.status.map { it.conflicts to it.held }.distinctUntilChanged().drop(1).collect {
                withContext(Dispatchers.EDT) { if (!project.isDisposed) ProjectView.getInstance(project).refresh() }
            }
        }
        val bus = project.messageBus.connect()
        connection = bus
        bus.subscribe(VirtualFileManager.VFS_CHANGES, object : BulkFileListener {
            override fun after(events: List<VFileEvent>) = onVfsEvents(events)
        })
        driver.start()
        scope.launch { checkDeployment() }
    }

    /** The last Deployment warning shown, so that an unchanged file does not warn again. */
    @Volatile private var deploymentWarned: String? = null

    /**
     * Warn when PyCharm's Deployment uploads changed files by itself into the directory calu
     * synchronises (`deploymentWarning`): in the chat and as a notification, once per distinct
     * text. A missing or unreadable `.idea/deployment.xml` warns nothing.
     */
    private fun checkDeployment() {
        val text = try {
            Files.readString(root.resolve(".idea/deployment.xml"))
        } catch (_: Exception) {
            null
        }
        val warning = deploymentWarning(text?.let(::parseDeployment), remoteDir)
        if (warning == null || warning == deploymentWarned) {
            deploymentWarned = warning
            return
        }
        deploymentWarned = warning
        controller.notice("warning", "calu sync: $warning")
        try {
            Notification(SYNC_NOTIFICATION_GROUP, "calu sync: two writers on $host", warning, NotificationType.WARNING).notify(project)
        } catch (error: Exception) {
            log("[calu] sync: the Deployment warning could not be shown: ${error.message}")
        }
    }

    /** Stop listening and end the synchronisation's coroutines; the status becomes "off". */
    fun dispose() {
        connection?.disconnect()
        driver.stop()
        scope.cancel()
        controller.dispatch("ui.sync", JsonObject(emptyMap()))
    }

    /**
     * The relative paths the agent is editing in the running turn.
     *
     * @return the held paths of the chat's state, relative to the server's project root.
     */
    private fun heldRelative(): Set<String> {
        val mapper = PathMapper(remoteRoot = sync.remotePaths?.root ?: remoteDir)
        return heldPaths(controller.state).mapNotNull { mapper.toRelativeRemote(it) }.toSet()
    }

    /**
     * The project view's mark of a file or folder of this copy.
     *
     * @param absolute the path of the VFS ("/" separators).
     * @param directory true for a folder.
     * @return the mark, or null for none (also for a path outside the project directory).
     */
    fun markOf(absolute: String, directory: Boolean): SyncMark? {
        val path = try {
            Path.of(absolute).normalize()
        } catch (_: java.nio.file.InvalidPathException) {
            return null
        }
        if (!path.startsWith(root)) return null
        val rel = if (path == root) "" else root.relativize(path).joinToString("/") { it.toString() }
        return syncMark(sync.status.value, rel, directory)
    }

    /**
     * Map an absolute local path to its relative path.
     *
     * @param absolute a path of the VFS ("/" separators).
     * @return the relative path; null if the path is not below the project directory.
     */
    private fun relative(absolute: String): String? {
        val path = try {
            Path.of(absolute).normalize()
        } catch (_: java.nio.file.InvalidPathException) {
            return null
        }
        if (!path.startsWith(root) || path == root) return null
        return root.relativize(path).joinToString("/") { it.toString() }
    }

    /**
     * Collect what a batch of VFS events changed and schedule the synchronisation.
     *
     * @param events the events, after they were applied.
     */
    private fun onVfsEvents(events: List<VFileEvent>) {
        if (events.any { it.path.endsWith("/.idea/deployment.xml") }) scope.launch { checkDeployment() }
        val changed = ArrayList<String>()
        val renames = ArrayList<Pair<String, String>>()
        for (event in events) {
            when (event) {
                is VFileMoveEvent -> moved(relative(event.oldPath), relative(event.newPath), event.file.isDirectory, changed, renames)
                is VFilePropertyChangeEvent -> if (event.isRename) moved(relative(event.oldPath), relative(event.newPath), event.file.isDirectory, changed, renames)
                is VFileDeleteEvent -> relative(event.path)?.let { changed.add(it); changed.addAll(sync.pathsBelow(it)) }
                is VFileCopyEvent -> relative(event.path)?.let { changed.addAll(filesAt(it)) }
                else -> relative(event.path)?.let { changed.addAll(filesAt(it)) }
            }
        }
        if (changed.isEmpty() && renames.isEmpty()) return
        synchronized(pendingLock) {
            pendingPaths.addAll(changed)
            pendingRenames.addAll(renames)
            flushJob?.cancel()
            flushJob = scope.launch {
                delay(LOCAL_CHANGE_DEBOUNCE_MS)
                val (paths, pairs) = synchronized(pendingLock) {
                    val p = pendingPaths.toList()
                    val r = pendingRenames.toList()
                    pendingPaths.clear()
                    pendingRenames.clear()
                    p to r
                }
                for ((from, to) in pairs) driver.post { sync.localRenamed(from, to) }
                val rest = paths.filter { path -> pairs.none { it.first == path || it.second == path } }
                if (rest.isNotEmpty()) driver.post { sync.localChanged(rest) }
            }
        }
    }

    /**
     * Record a move or rename of a file or directory.
     *
     * @param from the old relative path; null if it was outside the project.
     * @param to the new relative path; null if it is outside the project.
     * @param directory whether a directory moved.
     * @param changed receives the paths to decide one by one.
     * @param renames receives the file renames inside the project.
     */
    private fun moved(from: String?, to: String?, directory: Boolean, changed: MutableList<String>, renames: MutableList<Pair<String, String>>) {
        if (directory) {
            if (from != null) changed.addAll(sync.pathsBelow(from))
            if (to != null) changed.addAll(filesAt(to))
        } else if (from != null && to != null) {
            renames.add(from to to)
        } else {
            listOfNotNull(from, to).forEach { changed.add(it) }
        }
    }

    /**
     * List the regular files at a path: the path itself, or the files below a directory.
     *
     * @param path relative path of a file or directory.
     * @return the relative paths (at most 5000 of a directory; a full comparison finds the rest).
     */
    private fun filesAt(path: String): List<String> {
        val at = root.resolve(path)
        if (!Files.isDirectory(at, java.nio.file.LinkOption.NOFOLLOW_LINKS)) return listOf(path)
        return try {
            Files.walk(at).use { s -> s.filter { Files.isRegularFile(it, java.nio.file.LinkOption.NOFOLLOW_LINKS) }.limit(5000).map { root.relativize(it).joinToString("/") { p -> p.toString() } }.toList() }
        } catch (_: java.io.IOException) {
            emptyList()
        }
    }

    /** Compare both sides now ("Sync now"), with a progress indicator that can cancel it. */
    fun syncNow() {
        driver.post { withProgress("calu: synchronising with $host") { sync.syncNow() } }
    }

    /** Upload every held file now, although the agent is editing it ("Upload now"). */
    fun uploadHeldNow() {
        val paths = sync.status.value.held
        driver.post { for (path in paths) sync.uploadNow(path) }
    }

    /** List the paths that are not synchronised as a notice in the chat. */
    fun showSkipped() {
        val notes = sync.status.value.notes
        controller.notice("info", if (notes.isEmpty()) "calu sync: every path is synchronised." else "calu sync does not synchronise:\n" + notes.entries.joinToString("\n") { "${it.key}: ${it.value}" })
    }

    /**
     * Run a block under a background progress indicator; its cancel button cancels the block.
     *
     * @param title the indicator's title.
     * @param block the work.
     */
    private suspend fun withProgress(title: String, block: suspend () -> Unit) {
        // The indicator has no percentage: the platform's functions that report one are internal
        // API in 2026.3. The status bar's line counts the files that are left.
        withBackgroundProgress(project, title, true) { local.batched(block) }
    }

    override suspend fun confirm(summary: PlanSummary): Boolean = withContext(Dispatchers.EDT) {
        val yes = if (summary.isClone) "Clone from Server" else "Apply"
        Messages.showYesNoDialog(project, summary.text(), "calu Sync", yes, "Not Now", Messages.getQuestionIcon()) == Messages.YES
    }

    override fun conflict(conflict: SyncConflict) {
        controller.notice(
            "warning",
            "calu sync: ${conflict.path} was changed here and on $host (${kindText(conflict.kind)}). Neither side is overwritten; choose \"Merge\" on the card above the chat.",
            conflictKey(conflict.path),
        )
        try {
            val n = Notification(SYNC_NOTIFICATION_GROUP, "calu sync: conflict in ${conflict.path}", "Changed here and on $host: ${kindText(conflict.kind)}.", NotificationType.WARNING)
            n.addAction(NotificationAction.createSimpleExpiring("Merge…") { resolve(conflict.path) })
            synchronized(conflictNotes) { conflictNotes.put(conflict.path, n)?.expire() }
            n.notify(project)
        } catch (error: Exception) {
            log("[calu] sync: the conflict notification could not be shown: ${error.message}")
        }
    }

    override fun note(level: String, text: String) = controller.notice(level, text)

    /**
     * Name the transcript line of a path's conflict, for marking it resolved later.
     *
     * @param path the conflict's relative path.
     * @return the notice key, "sync-conflict:<path>".
     */
    private fun conflictKey(path: String): String = "sync-conflict:$path"

    /**
     * Word a conflict kind.
     *
     * @param kind the kind.
     * @return a short phrase for the user.
     */
    private fun kindText(kind: ConflictKind): String = when (kind) {
        ConflictKind.BOTH_MODIFIED -> "both sides changed the same lines"
        ConflictKind.CREATED_BOTH -> "both sides have the file with different content and no common version is known"
        ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY -> "deleted on the server, changed here"
        ConflictKind.MODIFIED_REMOTELY_DELETED_LOCALLY -> "changed on the server, deleted here"
        ConflictKind.BINARY -> "a binary file changed on both sides"
        ConflictKind.TOO_LARGE -> "too large to merge"
    }

    /**
     * Let the user resolve a conflict: the IDE's three-way merge dialog for two texts, otherwise
     * a choice between the two sides. The result goes to both sides.
     *
     * @param path relative path of the conflict; null for the first open one.
     */
    fun resolve(path: String?) {
        scope.launch {
            val target = path ?: sync.status.value.conflicts.firstOrNull() ?: return@launch
            val conflict = sync.conflictOf(target) ?: return@launch
            val contents = try {
                sync.conflictContents(target) ?: return@launch
            } catch (error: Exception) {
                controller.notice("warning", "calu sync: the server's version of $target could not be read: ${error.message}")
                return@launch
            }
            val mine = contents.local
            val theirs = contents.remote
            val texts = mine != null && theirs != null && !isProbablyBinary(mine) && !isProbablyBinary(theirs) && conflict.kind != ConflictKind.TOO_LARGE
            val result: Pair<Boolean, ByteArray?> = if (texts) mergeDialog(target, contents.base, mine!!, theirs!!) else chooseSide(target, conflict.kind, mine, theirs)
            if (result.first) sync.resolve(target, result.second)
        }
    }

    /**
     * Show the IDE's merge dialog for three texts.
     *
     * @param path relative path (title, file type).
     * @param base the common version; null shows an empty base.
     * @param mine the local text.
     * @param theirs the server's text.
     * @return (true, merged bytes) if the user applied a result; (false, null) if he cancelled.
     */
    private suspend fun mergeDialog(path: String, base: ByteArray?, mine: ByteArray, theirs: ByteArray): Pair<Boolean, ByteArray?> {
        val crlf = String(mine, Charsets.UTF_8).contains("\r\n")
        fun text(bytes: ByteArray?) = String(bytes ?: ByteArray(0), Charsets.UTF_8).replace("\r\n", "\n")
        var outcome: MergeResult? = null
        val output = withContext(Dispatchers.EDT) {
            val document = EditorFactory.getInstance().createDocument("")
            val type = FileTypeManager.getInstance().getFileTypeByFileName(path.substringAfterLast('/'))
            val request = DiffRequestFactory.getInstance().createMergeRequest(
                project, type, document, listOf(text(mine), text(base), text(theirs)),
                "calu Sync: $path", listOf("Yours (this machine)", if (base == null) "No common version" else "Last synchronised", "Theirs ($host)"),
            ) { outcome = it }
            DiffManager.getInstance().showMerge(project, request)
            document.text
        }
        if (outcome == null || outcome == MergeResult.CANCEL) return false to null
        val merged = when (outcome) {
            MergeResult.LEFT -> return true to mine
            MergeResult.RIGHT -> return true to theirs
            else -> if (crlf) output.replace("\n", "\r\n") else output
        }
        return true to merged.toByteArray(Charsets.UTF_8)
    }

    /**
     * Ask which side wins where no text merge is possible (a deletion against a change, a
     * binary file, a file above the merge limit).
     *
     * @param path relative path.
     * @param kind what happened.
     * @param mine the local content; null if the file is deleted here.
     * @param theirs the server's content; null if the file is deleted there.
     * @return (true, the content both sides get, null to delete on both) or (false, null) for a cancel.
     */
    private suspend fun chooseSide(path: String, kind: ConflictKind, mine: ByteArray?, theirs: ByteArray?): Pair<Boolean, ByteArray?> {
        val choice = withContext(Dispatchers.EDT) {
            Messages.showDialog(
                project,
                "$path: ${kindText(kind)}.\n\nKeep Mine: the server gets ${if (mine == null) "the deletion" else "this machine's file"}.\nTake Theirs: this machine gets ${if (theirs == null) "the deletion" else "the server's file"}.",
                "calu Sync Conflict", arrayOf("Keep Mine", "Take Theirs", "Cancel"), 0, Messages.getWarningIcon(),
            )
        }
        return when (choice) {
            0 -> true to mine
            1 -> true to theirs
            else -> false to null
        }
    }
}
