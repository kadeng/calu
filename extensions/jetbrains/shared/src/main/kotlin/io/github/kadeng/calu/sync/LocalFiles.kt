/**
 * The local project copy as the synchronisation sees it, and the plugin's own files about it.
 *
 *     LocalFiles        what the synchronisation asks of the local side (list, read, write, delete,
 *                       unsaved editor text)
 *     DiskLocalFiles    the implementation over the file system (java.nio): tests, and the part
 *                       of the IDE's implementation that needs no editor
 *     SyncStore         the plugin's state directory of one project copy: bases (hashes), the
 *                       pending queue, and the base contents by hash. It lies in the IDE's
 *                       system directory, never in the project.
 *
 * Which local files take part: regular files below the root, without `.git`, `.idea`, the
 * excluded directories of [PathMapper], upload leftovers (`.calu-upload-*`), symbolic links
 * (never followed, never written through) and what the project's `.gitignore` files name.
 */
package io.github.kadeng.calu.sync

import java.io.IOException
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.PosixFilePermission

/**
 * A local path that is not synchronised.
 *
 * @property path relative path.
 * @property reason "symlink", "not-regular" or "too-large".
 */
data class LocalSkipped(val path: String, val reason: String)

/**
 * The local files that take part, and those that are left out visibly.
 *
 * @property files the regular files by relative path, with the hash of their content on disk.
 * @property skipped links and special files below the root.
 */
class LocalListing(val files: Map<String, FileEntry>, val skipped: List<LocalSkipped>)

/**
 * The local side of the synchronisation. All paths are canonical relative paths; an
 * implementation refuses a path that would leave its root. Implementations are safe to call
 * from any thread; the IDE's implementation switches to the threads its APIs need.
 */
interface LocalFiles {
    /**
     * List the files that take part. Reads the files whose hash is not known yet.
     *
     * @param maxFileBytes files above this size are in `skipped` ("too-large") and not hashed.
     * @return files and skipped paths.
     */
    suspend fun list(maxFileBytes: Long): LocalListing

    /**
     * Describe one file as it is on disk.
     *
     * @param path relative path.
     * @return size, mode and hash; null if there is no regular file at the path.
     */
    suspend fun entry(path: String): FileEntry?

    /**
     * Tell why a path does not take part in the synchronisation although a file may be there.
     *
     * @param path relative path.
     * @return "excluded" (`.git`, `.idea`, excluded directories, upload leftovers), "ignored"
     *   (a `.gitignore` names it), "symlink" (the path or a directory on the way is a link),
     *   or null if the path takes part.
     */
    suspend fun leftOut(path: String): String?

    /**
     * Read one file from disk.
     *
     * @param path relative path.
     * @return the content; null if there is no regular file.
     */
    suspend fun read(path: String): ByteArray?

    /**
     * Create or replace one file with its missing directories. An implementation with editors
     * changes an open file through its document, so that undo works.
     *
     * @param path relative path.
     * @param bytes the new content.
     * @param mode POSIX permission bits to set, or null to keep the file's (default for a new one).
     * @throws IOException if the file cannot be written, or the path leaves the root or leads
     *   through a symbolic link.
     */
    suspend fun write(path: String, bytes: ByteArray, mode: Int?)

    /**
     * Delete one file and the directories that become empty by it (up to the root). A missing
     * file is ignored.
     *
     * @param path relative path.
     * @throws IOException if the file cannot be deleted.
     */
    suspend fun delete(path: String)

    /**
     * Return the editor's text of a file if it differs from the file on disk.
     *
     * @param path relative path.
     * @return the unsaved text as the bytes a save would write; null if no editor holds unsaved
     *   text of the file.
     */
    suspend fun unsaved(path: String): ByteArray?

    /**
     * Tell whether the local project directory has a git repository of its own (`.git`, a
     * directory or a worktree's file, at the root). It is never synchronised; the server's
     * repository is the one calu and Claude use.
     *
     * @return true if `<root>/.git` exists.
     */
    suspend fun hasOwnRepository(): Boolean = false
}

/** Names that never take part, at any depth: version control and IDE metadata. */
val LOCAL_EXCLUDED_NAMES: Set<String> = setOf(".git", ".idea", ".hg", ".svn")

/** Prefix of the temporary files of an upload on the server; never synchronised from either side. */
const val UPLOAD_PREFIX: String = ".calu-upload-"

/**
 * [LocalFiles] over a directory, without editors.
 *
 * @param root the local project directory (absolute).
 * @param paths the path rules (excluded directories).
 */
open class DiskLocalFiles(val root: Path, private val paths: PathMapper = PathMapper()) : LocalFiles {
    /** Hash of a file as it was when last read: size, modification time (ns) and hash. */
    private class Cached(val size: Long, val mtimeNs: Long, val sha256: String)

    private val cache = java.util.concurrent.ConcurrentHashMap<String, Cached>()

    /**
     * Tell whether a relative path is excluded by its names alone.
     *
     * @param path relative path.
     * @return true for a path with a segment of [LOCAL_EXCLUDED_NAMES] or one that starts with
     *   [UPLOAD_PREFIX], and for the excluded directories of the path rules.
     */
    protected fun excludedByName(path: String): Boolean =
        paths.isExcluded(path) || path.split('/').any { it in LOCAL_EXCLUDED_NAMES || it.startsWith(UPLOAD_PREFIX) }

    /**
     * Resolve a relative path below the root, refusing every path that leaves it.
     *
     * @param path relative path.
     * @return the absolute path.
     * @throws IOException if the path is not canonical, is excluded, or a directory on the way
     *   to it is a symbolic link.
     */
    protected fun resolve(path: String): Path {
        if (paths.problem(path) != null) throw IOException("not a path of the project: $path")
        var at = root
        val segments = path.split('/')
        for ((i, segment) in segments.withIndex()) {
            at = at.resolve(segment)
            if (i < segments.size - 1 && Files.isSymbolicLink(at)) throw IOException("a directory on the way to $path is a symbolic link")
        }
        return at
    }

    override suspend fun list(maxFileBytes: Long): LocalListing {
        val files = java.util.TreeMap<String, FileEntry>()
        val skipped = ArrayList<LocalSkipped>()
        val ignore = GitIgnore()
        if (!Files.isDirectory(root)) return LocalListing(files, skipped)
        Files.walkFileTree(root, object : SimpleFileVisitor<Path>() {
            override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                val rel = relative(dir)
                if (rel.isNotEmpty() && (excludedByName(rel) || ignore.isIgnoredDir(rel))) return FileVisitResult.SKIP_SUBTREE
                readIgnore(dir, rel, ignore)
                return FileVisitResult.CONTINUE
            }

            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                val rel = relative(file)
                if (excludedByName(rel) || paths.problem(rel) != null || ignore.isIgnored(rel)) return FileVisitResult.CONTINUE
                when {
                    attrs.isSymbolicLink -> skipped.add(LocalSkipped(rel, "symlink"))
                    !attrs.isRegularFile -> skipped.add(LocalSkipped(rel, "not-regular"))
                    attrs.size() > maxFileBytes -> skipped.add(LocalSkipped(rel, "too-large"))
                    else -> describe(rel, file, attrs)?.let { files[rel] = it }
                }
                return FileVisitResult.CONTINUE
            }

            override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult = FileVisitResult.CONTINUE
        })
        cache.keys.retainAll(files.keys)
        return LocalListing(files, skipped)
    }

    /**
     * Read the `.gitignore` of one directory into the rules, if there is one.
     *
     * @param dir the directory.
     * @param rel its relative path ("" for the root).
     * @param ignore the rules to add to.
     */
    private fun readIgnore(dir: Path, rel: String, ignore: GitIgnore) {
        val file = dir.resolve(".gitignore")
        if (Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS) && Files.size(file) < 1_000_000) {
            try {
                ignore.add(rel, Files.readString(file))
            } catch (_: IOException) {
            }
        }
    }

    /**
     * Return the relative path of an absolute one below the root.
     *
     * @param path a path below [root].
     * @return the relative path with "/" separators; "" for the root.
     */
    private fun relative(path: Path): String = root.relativize(path).joinToString("/") { it.toString() }

    /**
     * Describe a regular file, hashing it unless its size and modification time are those of
     * the last hash and the modification time is more than 2 seconds old. A write within the
     * timestamp granularity can leave size and modification time unchanged, so a younger file
     * is hashed again.
     *
     * @param rel relative path.
     * @param file the absolute path.
     * @param attrs the file's attributes.
     * @return the entry; null if the file cannot be read (it vanished meanwhile).
     */
    private fun describe(rel: String, file: Path, attrs: BasicFileAttributes): FileEntry? {
        val mtime = attrs.lastModifiedTime().to(java.util.concurrent.TimeUnit.NANOSECONDS)
        val known = cache[rel]
        val sha = if (known != null && known.size == attrs.size() && known.mtimeNs == mtime && System.currentTimeMillis() * 1_000_000 - mtime > 2_000_000_000L) {
            known.sha256
        } else {
            val bytes = try {
                Files.readAllBytes(file)
            } catch (_: IOException) {
                return null
            }
            sha256Hex(bytes).also { cache[rel] = Cached(bytes.size.toLong(), mtime, it) }
        }
        return FileEntry(rel, attrs.size(), modeOf(file), sha)
    }

    /**
     * Read the permission bits of a file.
     *
     * @param file the file.
     * @return the bits 0 to 0o777; [FileEntry.DEFAULT_MODE] on a file system without them.
     */
    private fun modeOf(file: Path): Int = try {
        var mode = 0
        for (p in Files.getPosixFilePermissions(file, LinkOption.NOFOLLOW_LINKS)) mode = mode or (1 shl (8 - p.ordinal))
        mode
    } catch (_: Exception) {
        FileEntry.DEFAULT_MODE
    }

    override suspend fun entry(path: String): FileEntry? {
        val file = try {
            resolve(path)
        } catch (_: IOException) {
            return null
        }
        val attrs = try {
            Files.readAttributes(file, BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
        } catch (_: IOException) {
            return null
        }
        if (!attrs.isRegularFile) return null
        return describe(path, file, attrs)
    }

    override suspend fun leftOut(path: String): String? {
        if (paths.problem(path) != null || excludedByName(path)) return "excluded"
        val segments = path.split('/')
        val ignore = GitIgnore()
        var at = root
        readIgnore(at, "", ignore)
        for ((i, segment) in segments.withIndex()) {
            at = at.resolve(segment)
            if (Files.isSymbolicLink(at)) return "symlink"
            if (i < segments.size - 1) readIgnore(at, segments.subList(0, i + 1).joinToString("/"), ignore)
        }
        return if (ignore.isIgnored(path)) "ignored" else null
    }

    override suspend fun read(path: String): ByteArray? {
        val file = try {
            resolve(path)
        } catch (_: IOException) {
            return null
        }
        if (!Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS)) return null
        return try {
            Files.readAllBytes(file)
        } catch (_: NoSuchFileException) {
            null
        }
    }

    override suspend fun write(path: String, bytes: ByteArray, mode: Int?) {
        val file = resolve(path)
        if (Files.isSymbolicLink(file)) throw IOException("$path is a symbolic link")
        Files.createDirectories(file.parent)
        val temp = Files.createTempFile(file.parent, ".calu-sync-", ".tmp")
        try {
            Files.write(temp, bytes)
            applyMode(temp, mode ?: if (Files.exists(file)) modeOf(file) else FileEntry.DEFAULT_MODE)
            Files.move(temp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } finally {
            Files.deleteIfExists(temp)
        }
    }

    /**
     * Set the permission bits of a file; does nothing on a file system without them.
     *
     * @param file the file.
     * @param mode the bits 0 to 0o777.
     */
    protected fun applyMode(file: Path, mode: Int) {
        try {
            val all = PosixFilePermission.values().toList()
            Files.setPosixFilePermissions(file, all.filterTo(HashSet()) { mode and (1 shl (8 - it.ordinal)) != 0 })
        } catch (_: Exception) {
        }
    }

    override suspend fun delete(path: String) {
        val file = resolve(path)
        Files.deleteIfExists(file)
        cache.remove(path)
        var dir = file.parent
        while (dir != null && dir != root && dir.startsWith(root)) {
            try {
                Files.newDirectoryStream(dir).use { if (it.iterator().hasNext()) return }
                Files.delete(dir)
            } catch (_: IOException) {
                return
            }
            dir = dir.parent
        }
    }

    override suspend fun unsaved(path: String): ByteArray? = null

    override suspend fun hasOwnRepository(): Boolean = java.nio.file.Files.exists(root.resolve(".git"), java.nio.file.LinkOption.NOFOLLOW_LINKS)
}

/**
 * [BaseContents] in a directory: one file per content, named by its hash. Contents above
 * [maxEntryBytes] are not kept (a file that large is never merged). `put` never removes
 * anything: `prune` removes the contents that no base names, and then the oldest files while
 * the directory is above [maxTotalBytes]. The caller runs `prune` after a synchronisation.
 * Safe for use from several threads.
 *
 * @param dir the directory; created on the first `put`.
 * @param maxEntryBytes largest content that is kept.
 * @param maxTotalBytes size of all contents above which [prune] removes the oldest.
 */
class DiskBaseContents(
    private val dir: Path,
    private val maxEntryBytes: Long = DefaultSyncEngine.DEFAULT_MAX_MERGE_BYTES,
    private val maxTotalBytes: Long = 512L * 1024 * 1024,
) : BaseContents {
    @Synchronized
    override fun get(sha256: String): ByteArray? {
        if (!isSha256(sha256)) return null
        val file = dir.resolve(sha256)
        return try {
            Files.readAllBytes(file).takeIf { sha256Hex(it) == sha256 }
        } catch (_: IOException) {
            null
        }
    }

    @Synchronized
    override fun put(bytes: ByteArray): String {
        val hash = sha256Hex(bytes)
        if (bytes.size > maxEntryBytes) return hash
        try {
            Files.createDirectories(dir)
            val file = dir.resolve(hash)
            if (!Files.exists(file)) {
                val temp = Files.createTempFile(dir, "put-", ".tmp")
                Files.write(temp, bytes)
                Files.move(temp, file, StandardCopyOption.REPLACE_EXISTING)
            }
        } catch (_: IOException) {
            // Without the base a later merge becomes a conflict; nothing is lost.
        }
        return hash
    }

    /**
     * Remove contents no path has as its base, and then the oldest ones while the directory is
     * above the size limit. Failures are ignored.
     *
     * @param keep the hashes that are bases of the sync state.
     */
    @Synchronized
    fun prune(keep: Set<String>) {
        try {
            if (!Files.isDirectory(dir)) return
            val files = Files.list(dir).use { s -> s.filter { Files.isRegularFile(it) }.toList() }
            val kept = ArrayList<Path>()
            for (f in files) if (f.fileName.toString() in keep) kept.add(f) else Files.deleteIfExists(f)
            var total = kept.sumOf { Files.size(it) }
            for (f in kept.sortedBy { Files.getLastModifiedTime(it) }) {
                if (total <= maxTotalBytes) break
                total -= Files.size(f)
                Files.deleteIfExists(f)
            }
        } catch (_: IOException) {
        }
    }
}

/**
 * The plugin's files about one synchronised project copy.
 *
 *     <dir>/state.txt     SyncState.encode()
 *     <dir>/queue.txt     PendingQueue.encode()
 *     <dir>/holds.txt     KeptHold.encode(): files held for a running turn (absent when none)
 *     <dir>/bases/<hash>  the last synchronised contents (DiskBaseContents)
 *
 * @param dir the directory (in the IDE's system directory; see [syncStoreName]).
 */
class SyncStore(private val dir: Path) {
    /** The base contents of this copy. */
    val bases: DiskBaseContents = DiskBaseContents(dir.resolve("bases"))

    /**
     * Read the stored bases.
     *
     * @return the state; an empty state if none was stored or the file cannot be read (every
     *   file then counts as never synchronised, and the next plan asks before it changes anything).
     */
    fun loadState(): SyncState = try {
        SyncState.decode(Files.readString(dir.resolve("state.txt")))
    } catch (_: Exception) {
        SyncState()
    }

    /**
     * Store the bases (written to a temporary file and moved into place). Failures are ignored.
     *
     * @param state the bases.
     */
    fun saveState(state: SyncState) = save("state.txt", state.encode())

    /**
     * Read the stored queue.
     *
     * @return the queue; empty if none was stored or the file cannot be read.
     */
    fun loadQueue(): PendingQueue = try {
        PendingQueue.decode(Files.readString(dir.resolve("queue.txt")))
    } catch (_: Exception) {
        PendingQueue()
    }

    /**
     * Store the queue. Failures are ignored.
     *
     * @param queue the queue.
     */
    fun saveQueue(queue: PendingQueue) = save("queue.txt", queue.encode())

    /**
     * Read the held paths stored by the last IDE run (`HoldKeeper`, B23).
     *
     * @return the record; null if none was stored or the file cannot be read.
     */
    fun loadHolds(): KeptHold? = try {
        KeptHold.decode(Files.readString(dir.resolve("holds.txt")))
    } catch (_: Exception) {
        null
    }

    /**
     * Store the held paths, or remove the record. Failures are ignored.
     *
     * @param hold the record; null removes it.
     */
    fun saveHolds(hold: KeptHold?) {
        if (hold != null) {
            save("holds.txt", hold.encode())
        } else {
            try {
                Files.deleteIfExists(dir.resolve("holds.txt"))
            } catch (_: IOException) {
            }
        }
    }

    /**
     * Write one of the store's text files.
     *
     * @param name the file name.
     * @param text the content.
     */
    private fun save(name: String, text: String) {
        try {
            Files.createDirectories(dir)
            val temp = Files.createTempFile(dir, name, ".tmp")
            Files.writeString(temp, text)
            Files.move(temp, dir.resolve(name), StandardCopyOption.REPLACE_EXISTING)
        } catch (_: IOException) {
        }
    }
}

/**
 * Name the state directory of one project copy.
 *
 * @param localRoot the local project directory.
 * @param host the SSH destination.
 * @param remoteDir the project directory on the server.
 * @return 16 hexadecimal digits: the start of the SHA-256 of the three texts. Another host or
 *   another directory gives another name, so a copy that is pointed at another server starts
 *   with an empty state.
 */
fun syncStoreName(localRoot: String, host: String, remoteDir: String): String =
    sha256Hex("$localRoot\n$host\n$remoteDir".toByteArray(Charsets.UTF_8)).take(16)
