package io.github.kadeng.calu.sync

/**
 * A [FileStore] in memory, for tests. Not thread-safe.
 *
 * @param files initial files: relative path to text content (UTF-8).
 */
class MemoryFileStore(files: Map<String, String> = emptyMap()) : FileStore {
    private val contents = HashMap<String, ByteArray>()
    private val modes = HashMap<String, Int>()

    init {
        for ((path, text) in files) write(path, text.toByteArray(), FileEntry.DEFAULT_MODE)
    }

    override fun list(): Map<String, FileEntry> =
        contents.mapValues { (path, bytes) -> FileEntry(path, bytes.size.toLong(), modes.getValue(path), sha256Hex(bytes)) }

    override fun read(path: String): ByteArray? = contents[path]

    override fun write(path: String, bytes: ByteArray, mode: Int) {
        contents[path] = bytes
        modes[path] = mode
    }

    override fun delete(path: String) {
        contents.remove(path)
        modes.remove(path)
    }

    /**
     * Returns every file as text, for comparing two stores. No side effects.
     *
     * @return relative path to content decoded as ISO-8859-1 (one character per byte), sorted by path.
     */
    fun texts(): Map<String, String> = contents.mapValues { String(it.value, Charsets.ISO_8859_1) }.toSortedMap()

    /**
     * Writes a text file with the default mode.
     *
     * @param path relative path.
     * @param text the content (UTF-8).
     */
    fun put(path: String, text: String) = write(path, text.toByteArray(), FileEntry.DEFAULT_MODE)
}

/**
 * Hash of a text, for building test inputs. No side effects.
 *
 * @param text the content (UTF-8).
 * @return its SHA-256 as lowercase hex.
 */
fun h(text: String): String = sha256Hex(text.toByteArray())

/**
 * Builds the [FileEntry] of a text file. No side effects.
 *
 * @param path relative path.
 * @param text the content (UTF-8).
 * @param size the size to report; defaults to the real size.
 * @return the entry with the default mode.
 */
fun entry(path: String, text: String, size: Long = text.toByteArray().size.toLong()): FileEntry =
    FileEntry(path, size, FileEntry.DEFAULT_MODE, h(text))
