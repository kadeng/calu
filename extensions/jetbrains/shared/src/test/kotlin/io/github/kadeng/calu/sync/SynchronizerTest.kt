package io.github.kadeng.calu.sync

import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files
import java.nio.file.Path

/**
 * A local directory whose files can have unsaved editor text, as the IDE's implementation has.
 *
 * @param root the directory.
 */
class EditorLocalFiles(root: Path) : DiskLocalFiles(root) {
    /** Unsaved text by relative path. */
    val editors = HashMap<String, String>()

    override suspend fun unsaved(path: String): ByteArray? = editors[path]?.toByteArray()

    override suspend fun write(path: String, bytes: ByteArray, mode: Int?) {
        editors.remove(path)
        super.write(path, bytes, mode)
    }
}

/** A user interface that records what it is asked and answers as the test says. */
class RecordingUi(var answer: Boolean = true) : SyncUi {
    /** The plans that were shown. */
    val asked = ArrayList<PlanSummary>()

    /** The conflicts that were reported. */
    val conflicts = ArrayList<SyncConflict>()

    /** The notes that were shown. */
    val notes = ArrayList<String>()

    override suspend fun confirm(summary: PlanSummary): Boolean {
        asked.add(summary)
        return answer
    }

    override fun conflict(conflict: SyncConflict) {
        conflicts.add(conflict)
    }

    override fun note(level: String, text: String) {
        notes.add(text)
    }
}

/** [Synchronizer] with a local directory and the scripted `ide.fs.*` bridge. */
class SynchronizerTest {
    private val root: Path = Files.createTempDirectory("calu-sync-local")
    private val storeDir: Path = Files.createTempDirectory("calu-sync-store")
    private val local = EditorLocalFiles(root)
    private val ui = RecordingUi()
    private val held = HashSet<String>()
    private val caps = listOf("cwd", "ping", "paths", "fs", "fs.watch")

    /** Lines 1 to 9, one per line: a text two sides can change in different places. */
    private val lines = (1..9).joinToString("") { "line $it\n" }

    /**
     * Create a synchroniser over a bridge, with the store of this test.
     *
     * @param bridge the remote side.
     * @return the synchroniser (not connected).
     */
    private fun sync(bridge: FakeFsBridge): Synchronizer =
        Synchronizer(RemoteFileStore(bridge.call), local, SyncStore(storeDir), { held }, ui, "box:/srv/project", maxFileBytes = 500)

    /**
     * Write a local file.
     *
     * @param path relative path.
     * @param text its text.
     */
    private fun put(path: String, text: String) {
        Files.createDirectories(root.resolve(path).parent)
        Files.writeString(root.resolve(path), text)
    }

    /**
     * Read a local file.
     *
     * @param path relative path.
     * @return its text; null if there is none.
     */
    private fun get(path: String): String? = root.resolve(path).takeIf { Files.isRegularFile(it) }?.let { Files.readString(it) }

    @Test fun `clone - an empty folder is filled after the confirmation, and a second comparison does nothing`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.py" to "a\n", "pkg/b.py" to "b\n", "long.txt" to "x".repeat(40)))
        bridge.skipped["link"] = "symlink"
        val s = sync(bridge)
        assertTrue(s.connect(caps))
        assertEquals(1, ui.asked.size)
        assertTrue(ui.asked[0].isClone)
        assertEquals(3, ui.asked[0].downloads.size)
        assertTrue(ui.asked[0].text().startsWith("The local folder is empty. Clone 3 files"))
        assertEquals("a\n", get("a.py"))
        assertEquals("b\n", get("pkg/b.py"))
        assertEquals("x".repeat(40), get("long.txt"))
        assertEquals(SyncPhase.IDLE, s.status.value.phase)
        assertEquals("calu sync: in sync (1 skipped)", syncStatusText(s.status.value))
        assertTrue(s.status.value.notes["link"]!!.contains("symbolic link"))
        bridge.log.clear()
        s.syncNow()
        assertEquals(1, ui.asked.size, "no second question")
        assertFalse(bridge.log.any { it.startsWith("ide.fs.read") || it.startsWith("ide.fs.write") })
        assertEquals(3, SyncStore(storeDir).loadState().paths.size, "the bases are stored")
    }

    @Test fun `clone - refused leaves the folder empty, and Sync now asks again`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.py" to "a\n"))
        ui.answer = false
        val s = sync(bridge)
        s.connect(caps)
        assertNull(get("a.py"))
        assertEquals(SyncPhase.WAITING, s.status.value.phase)
        ui.answer = true
        s.syncNow()
        assertEquals(2, ui.asked.size)
        assertEquals("a\n", get("a.py"))
    }

    @Test fun `first sync of an existing copy - equal files settle, different ones are conflicts and nothing is overwritten`() = runBlocking {
        put("same.py", "s\n")
        put("mine.py", "m\n")
        put("differs.py", "local\n")
        put("build/out.log", "ignored")
        put(".gitignore", "build/\n")
        put(".idea/workspace.xml", "<x/>")
        val bridge = FakeFsBridge(mapOf("same.py" to "s\n", "theirs.py" to "t\n", "differs.py" to "remote\n", ".gitignore" to "build/\n"))
        val s = sync(bridge)
        s.connect(caps)
        val plan = ui.asked.single()
        assertFalse(plan.isClone)
        assertEquals(listOf("theirs.py"), plan.downloads)
        assertEquals(listOf("mine.py"), plan.uploads)
        assertEquals(listOf("differs.py"), plan.conflicts)
        assertEquals(2, plan.same)
        assertEquals("local\n", get("differs.py"))
        assertEquals("remote\n", bridge.text("differs.py"))
        assertEquals("m\n", bridge.text("mine.py"))
        assertEquals("t\n", get("theirs.py"))
        assertNull(bridge.text("build/out.log"))
        assertNull(bridge.text(".idea/workspace.xml"))
        assertEquals(listOf("differs.py"), s.status.value.conflicts)
        assertEquals(ConflictKind.CREATED_BOTH, ui.conflicts.single().kind)
        assertEquals("calu sync: conflict in differs.py", syncStatusText(s.status.value))
        val contents = s.conflictContents("differs.py")!!
        assertEquals("local\n", String(contents.local!!))
        assertEquals("remote\n", String(contents.remote!!))
        assertNull(contents.base)
        assertTrue(s.resolve("differs.py", "both\n".toByteArray()))
        assertEquals("both\n", get("differs.py"))
        assertEquals("both\n", bridge.text("differs.py"))
        assertTrue(s.status.value.conflicts.isEmpty())
    }

    @Test fun `a remote edit is downloaded, a local save is uploaded, and the echo of the upload does nothing`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.py" to "v1\n"))
        val s = sync(bridge)
        s.connect(caps)
        s.remoteChanged(bridge.change("a.py", "v2\n"))
        assertEquals("v2\n", get("a.py"))
        put("a.py", "v3\n")
        s.localChanged(listOf("a.py"))
        assertEquals("v3\n", bridge.text("a.py"))
        bridge.log.clear()
        s.remoteChanged(FsChanged("e1", 2, listOf(RemoteChange("a.py", null, sha256Hex("v3\n".toByteArray()), false, RemoteChange.ORIGIN_IDE))))
        assertTrue(bridge.log.isEmpty())
        s.localChanged(listOf("a.py"))
        assertTrue(bridge.log.isEmpty(), "a save without a change sends nothing")
        put("new/n.py", "n\n")
        s.localChanged(listOf("new/n.py"))
        assertEquals("n\n", bridge.text("new/n.py"))
        s.remoteChanged(bridge.change("r.py", "r\n", "other").copy(seq = 3))
        assertEquals("r\n", get("r.py"))
    }

    @Test fun `deletions travel both ways, and a rename is one request`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.py" to "a\n", "b.py" to "b\n", "c.py" to "c\n"))
        val s = sync(bridge)
        s.connect(caps)
        s.remoteChanged(bridge.change("a.py", null))
        assertNull(get("a.py"))
        Files.delete(root.resolve("b.py"))
        s.localChanged(listOf("b.py"))
        assertNull(bridge.text("b.py"))
        Files.move(root.resolve("c.py"), root.resolve("d.py"))
        bridge.log.clear()
        s.localRenamed("c.py", "d.py")
        assertEquals(listOf("ide.fs.rename"), bridge.log)
        assertEquals(setOf("d.py"), bridge.files.keys)
        bridge.log.clear()
        s.syncNow()
        assertFalse(bridge.log.any { it.startsWith("ide.fs.write") || it == "ide.fs.delete" || it == "ide.fs.read" })
    }

    @Test fun `both sides changed - a clean merge goes to both sides, an overlap is a conflict that keeps both`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("m.txt" to lines, "o.txt" to lines), requestBytes = 1000)
        val s = sync(bridge)
        s.connect(caps)
        put("m.txt", lines.replace("line 2\n", "line two\n"))
        val n = bridge.change("m.txt", lines.replace("line 8\n", "line eight\n"))
        s.localChanged(listOf("m.txt"))
        val merged = lines.replace("line 2\n", "line two\n").replace("line 8\n", "line eight\n")
        assertEquals(merged, get("m.txt"))
        assertEquals(merged, bridge.text("m.txt"))
        assertTrue(ui.notes.any { it.contains("merged") })
        s.remoteChanged(n)
        assertEquals(merged, get("m.txt"), "the late notification changes nothing")
        put("o.txt", lines.replace("line 5\n", "mine\n"))
        bridge.change("o.txt", lines.replace("line 5\n", "theirs\n"))
        s.localChanged(listOf("o.txt"))
        assertEquals(ConflictKind.BOTH_MODIFIED, ui.conflicts.single().kind)
        assertEquals(lines.replace("line 5\n", "mine\n"), get("o.txt"))
        assertEquals(lines.replace("line 5\n", "theirs\n"), bridge.text("o.txt"))
        val c = s.conflictContents("o.txt")!!
        assertEquals(lines, String(c.base!!))
        assertTrue(s.resolve("o.txt", c.remote))
        assertEquals(lines.replace("line 5\n", "theirs\n"), get("o.txt"))
        assertTrue(s.status.value.conflicts.isEmpty())
    }

    @Test fun `a held file waits in the queue while the remote edit still arrives, and is merged when the turn ends`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("h.txt" to lines, "k.txt" to "k1\n"), requestBytes = 1000)
        val s = sync(bridge)
        s.connect(caps)
        held.add("h.txt")
        s.remoteChanged(bridge.change("h.txt", lines.replace("line 8\n", "agent\n")))
        assertEquals(lines.replace("line 8\n", "agent\n"), get("h.txt"), "downloads go on during a turn")
        put("h.txt", lines.replace("line 8\n", "agent\n").replace("line 2\n", "user\n"))
        s.localChanged(listOf("h.txt"))
        assertEquals(lines.replace("line 8\n", "agent\n"), bridge.text("h.txt"), "the save is not uploaded")
        assertEquals("calu sync: 1 file held until the turn ends", syncStatusText(s.status.value))
        s.remoteChanged(bridge.change("h.txt", lines.replace("line 8\n", "agent again\n")))
        assertEquals(lines.replace("line 8\n", "agent\n").replace("line 2\n", "user\n"), get("h.txt"), "the local change is not overwritten")
        held.clear()
        s.holdsChanged()
        val both = lines.replace("line 8\n", "agent again\n").replace("line 2\n", "user\n")
        assertEquals(both, get("h.txt"))
        assertEquals(both, bridge.text("h.txt"))
        assertTrue(s.status.value.held.isEmpty())
        // The bridge's own hold (-32035) queues as well.
        bridge.held.add("k.txt")
        put("k.txt", "k2\n")
        s.localChanged(listOf("k.txt"))
        assertEquals("k1\n", bridge.text("k.txt"))
        assertEquals(listOf("k.txt"), s.status.value.held)
        s.uploadNow("k.txt")
        assertEquals("k2\n", bridge.text("k.txt"), "Upload now sends during_turn")
        assertTrue(s.status.value.held.isEmpty())
    }

    @Test fun `offline saves are queued and replayed, and a file the server changed meanwhile is not overwritten`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.txt" to "a1\n", "b.txt" to "b1\n", "gone.txt" to "g\n"))
        val s = sync(bridge)
        s.connect(caps)
        bridge.online = false
        s.disconnected()
        put("a.txt", "a2\n")
        put("b.txt", "b-local\n")
        put("new.txt", "n\n")
        Files.delete(root.resolve("gone.txt"))
        s.localChanged(listOf("a.txt", "b.txt", "new.txt", "gone.txt"))
        assertEquals("calu sync: offline: 4 changes queued", syncStatusText(s.status.value))
        assertEquals(4, SyncStore(storeDir).loadQueue().size, "the queue survives a restart of the IDE")
        bridge.files["b.txt"] = "b-remote\n".toByteArray()
        bridge.online = true
        // A new synchroniser, as after a restart of the IDE.
        val again = sync(bridge)
        again.connect(caps)
        assertEquals("a2\n", bridge.text("a.txt"))
        assertEquals("n\n", bridge.text("new.txt"))
        assertNull(bridge.text("gone.txt"))
        assertEquals("b-remote\n", bridge.text("b.txt"))
        assertEquals("b-local\n", get("b.txt"))
        assertEquals(listOf("b.txt"), again.status.value.conflicts)
        assertEquals(0, again.status.value.queued)
    }

    @Test fun `a gap in the sequence numbers leads to a full comparison`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.txt" to "1\n"))
        val s = sync(bridge)
        s.connect(caps)
        bridge.change("a.txt", "2\n")
        bridge.files["quiet.txt"] = "q\n".toByteArray()
        s.remoteChanged(bridge.change("a.txt", "3\n"))
        assertEquals("3\n", get("a.txt"))
        assertEquals("q\n", get("quiet.txt"), "found by the comparison, no notification named it")
    }

    @Test fun `unsaved editor text is merged with a remote change and is not uploaded before it is saved`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("e.txt" to lines), requestBytes = 1000)
        val s = sync(bridge)
        s.connect(caps)
        local.editors["e.txt"] = lines.replace("line 1\n", "typed\n")
        s.localChanged(listOf("e.txt"))
        assertEquals(lines, bridge.text("e.txt"), "typing uploads nothing")
        s.remoteChanged(bridge.change("e.txt", lines.replace("line 9\n", "agent\n")))
        val both = lines.replace("line 1\n", "typed\n").replace("line 9\n", "agent\n")
        assertEquals(both, get("e.txt"))
        assertEquals(both, bridge.text("e.txt"))
    }

    @Test fun `what cannot be synchronised is noted and left alone`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.txt" to "a\n", "ignored-there.txt" to "i\n"))
        val s = sync(bridge)
        s.connect(caps)
        // The server stops listing a file (git ignores it there): the local copy is not deleted.
        bridge.unlisted.add("ignored-there.txt")
        bridge.files["was-file.txt"] = "w\n".toByteArray()
        s.syncNow()
        bridge.skipped["was-file.txt"] = "symlink"
        put("big.bin", "x".repeat(600))
        Files.createSymbolicLink(root.resolve("l"), root.resolve("a.txt"))
        s.syncNow()
        assertEquals("i\n", get("ignored-there.txt"))
        assertEquals("w\n", get("was-file.txt"), "a file that became a link on the server is kept here")
        assertNull(bridge.text("big.bin"))
        val notes = s.status.value.notes
        assertTrue(notes["big.bin"]!!.contains("larger"), notes.toString())
        assertTrue(notes["l"]!!.contains("symbolic link"))
        assertNotNull(notes["ignored-there.txt"])
        assertTrue(bridge.files.keys.none { it == "l" })
    }

    @Test fun `a bridge without the fs capability is refused with what to do, and nothing is sent`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("a.txt" to "a\n"))
        val s = sync(bridge)
        assertFalse(s.connect(listOf("cwd", "ping", "paths")))
        assertEquals(SyncPhase.REFUSED, s.status.value.phase)
        assertTrue(s.status.value.message!!.contains("Update calu on the server"))
        put("x.txt", "x")
        s.localChanged(listOf("x.txt"))
        s.syncNow()
        assertTrue(bridge.log.isEmpty())
    }

    @Test fun `a plan that would delete many files asks first`() = runBlocking {
        val bridge = FakeFsBridge((1..30).associate { "f$it.txt" to "v\n" }, pageEntries = 100)
        val s = sync(bridge)
        s.connect(caps)
        assertEquals(1, ui.asked.size)
        bridge.files.clear()
        ui.answer = false
        s.syncNow()
        assertEquals(2, ui.asked.size)
        assertEquals(30, ui.asked[1].deleteLocal.size)
        assertEquals("v\n", get("f1.txt"))
    }
}
