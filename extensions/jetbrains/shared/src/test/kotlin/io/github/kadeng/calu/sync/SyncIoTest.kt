package io.github.kadeng.calu.sync

import io.github.kadeng.calu.rpc.RpcClosedException
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import java.nio.file.Files

/** The pure parts of the sync I/O layer: ignore rules, the remote store over a scripted bridge, the local store, texts. */
class SyncIoTest {
    @Test fun `gitignore - names, directories, anchors, globs and negation`() {
        val g = GitIgnore()
        g.add("", "# comment\n__pycache__/\n*.log\n/build\n!keep.log\ndocs/**/tmp\nsrc/*.o\n\\#hash\ntrail \n")
        assertTrue(g.isIgnored("__pycache__/a.pyc"))
        assertTrue(g.isIgnored("pkg/__pycache__/a.pyc"))
        assertFalse(g.isIgnored("__pycache__"), "a file of that name is not a directory")
        assertTrue(g.isIgnored("a.log"))
        assertTrue(g.isIgnored("deep/er/x.log"))
        assertFalse(g.isIgnored("keep.log"))
        assertTrue(g.isIgnored("build/out.txt"))
        assertFalse(g.isIgnored("sub/build/out.txt"), "/build is anchored to the root")
        assertTrue(g.isIgnored("docs/a/b/tmp"))
        assertTrue(g.isIgnored("docs/tmp"))
        assertTrue(g.isIgnored("src/x.o"))
        assertFalse(g.isIgnored("src/sub/x.o"))
        assertTrue(g.isIgnored("#hash"))
        assertTrue(g.isIgnored("trail"))
        assertFalse(g.isIgnored("greeter.py"))
        assertTrue(g.isIgnoredDir("build"))
    }

    @Test fun `gitignore - a nested file applies below its directory and wins over the root`() {
        val g = GitIgnore()
        g.add("", "*.txt\n")
        g.add("docs", "!*.txt\n/secret.txt\ngen/\n")
        assertTrue(g.isIgnored("a.txt"))
        assertFalse(g.isIgnored("docs/a.txt"))
        assertTrue(g.isIgnored("docs/secret.txt"))
        assertTrue(g.isIgnored("docs/gen/x.md"))
        assertFalse(g.isIgnored("gen/x.md"))
        assertTrue(g.isIgnored("b.txt"))
    }

    @Test fun `status text - one line per state`() {
        assertEquals("", syncStatusText(SyncStatus()))
        assertEquals("calu sync: in sync", syncStatusText(SyncStatus(SyncPhase.IDLE)))
        assertEquals("calu sync: in sync (2 skipped)", syncStatusText(SyncStatus(SyncPhase.IDLE, notes = mapOf("a" to "x", "b" to "y"))))
        assertEquals("calu sync: 3 files uploading", syncStatusText(SyncStatus(SyncPhase.SYNCING, uploading = 3)))
        assertEquals("calu sync: 1 file downloading", syncStatusText(SyncStatus(SyncPhase.SYNCING, downloading = 1)))
        assertEquals("calu sync: 1 file held until the turn ends", syncStatusText(SyncStatus(SyncPhase.IDLE, held = listOf("x.py"), queued = 1)))
        assertEquals("calu sync: conflict in x.py", syncStatusText(SyncStatus(SyncPhase.IDLE, conflicts = listOf("x.py"), held = listOf("y"))))
        assertEquals("calu sync: conflict in x.py and 2 more", syncStatusText(SyncStatus(SyncPhase.IDLE, conflicts = listOf("x.py", "b", "c"))))
        assertEquals("calu sync: offline: 2 changes queued", syncStatusText(SyncStatus(SyncPhase.OFFLINE, queued = 2)))
        assertEquals("calu sync: offline", syncStatusText(SyncStatus(SyncPhase.OFFLINE)))
        assertEquals("calu sync: not available", syncStatusText(SyncStatus(SyncPhase.REFUSED, message = "why")))
        assertEquals("calu sync: waits for your confirmation", syncStatusText(SyncStatus(SyncPhase.WAITING)))
    }

    @Test fun `every error code maps to its failure`() {
        val b = FakeFsBridge()
        fun f(code: Int, vararg data: Pair<String, kotlinx.serialization.json.JsonElement>) = fsFailureOf(b.error("m", code, *data))
        assertEquals(FsFailure.Refused("excluded"), f(-32030, "reason" to JsonPrimitive("excluded")))
        assertEquals(FsFailure.Mismatch("ab", 3), f(-32031, "sha256" to JsonPrimitive("ab"), "size" to JsonPrimitive(3)))
        assertEquals(FsFailure.Mismatch(null, 0), f(-32031))
        assertEquals(FsFailure.TooLarge(9, 5), f(-32032, "size" to JsonPrimitive(9), "limit" to JsonPrimitive(5)))
        assertEquals(FsFailure.NotRegular("symlink"), f(-32033, "kind" to JsonPrimitive("symlink")))
        assertEquals(FsFailure.NotFound, f(-32034))
        assertEquals(FsFailure.Held("s9"), f(-32035, "session_id" to JsonPrimitive("s9")))
        assertEquals(FsFailure.UploadFailed("offset"), f(-32036, "reason" to JsonPrimitive("offset")))
        assertEquals(FsFailure.Io("No space left"), f(-32037, "strerror" to JsonPrimitive("No space left")))
        assertEquals(FsFailure.TargetExists("cd"), f(-32038, "sha256" to JsonPrimitive("cd")))
        assertEquals(FsFailure.NoManifest("git"), f(-32039, "reason" to JsonPrimitive("git")))
        assertEquals(FsFailure.Other(-32602, "refused"), f(-32602))
        assertTrue(fsFailureOf(RpcClosedException("gone")) is FsFailure.Offline)
        assertTrue(fsFailureOf(IllegalStateException("not running")) is FsFailure.Offline)
    }

    @Test fun `paths - the limits of the bridge are taken over, missing ones are the defaults`() = runBlocking {
        val b = FakeFsBridge()
        val store = RemoteFileStore(b.call)
        val p = store.paths()
        assertEquals("/srv/project", p.root)
        assertEquals(RemoteLimits(1000, 8, 2, 4), store.limits)
        assertEquals(RemoteLimits(), parseRemotePaths(kotlinx.serialization.json.JsonObject(emptyMap())).limits)
    }

    @Test fun `manifest - pages are joined, and a listing whose head moved starts again`() = runBlocking {
        val b = FakeFsBridge((1..5).associate { "f$it.txt" to "v$it" })
        b.skipped["link"] = "symlink"
        val store = RemoteFileStore(b.call)
        store.paths()
        b.log.clear()
        val m = store.manifest()
        assertEquals(setOf("f1.txt", "f2.txt", "f3.txt", "f4.txt", "f5.txt"), m.files.keys)
        assertEquals(listOf(RemoteSkipped("link", RemoteSkipReason.SYMLINK, "symlink")), m.skipped)
        assertEquals(3, b.log.size, "three pages of two entries")
        b.log.clear()
        b.before[1] = { b.head = "b".repeat(40) }
        val again = store.manifest()
        assertEquals("b".repeat(40), again.head)
        assertEquals(5, again.files.size)
        assertEquals(5, b.log.size, "a page, a void page, then three pages")
    }

    @Test fun `manifest - an expired cursor starts again, and a lasting failure is passed on`() = runBlocking {
        val b = FakeFsBridge((1..3).associate { "f$it" to "v" })
        val store = RemoteFileStore(b.call, RemoteLimits(pageEntries = 2))
        b.failNext["ide.fs.manifest"] = ArrayDeque(listOf(b.error("ide.fs.manifest", -32039, "reason" to JsonPrimitive("cursor-expired"))))
        assertEquals(3, store.manifest().files.size)
        b.failNext["ide.fs.manifest"] = ArrayDeque(List(9) { b.error("ide.fs.manifest", -32039, "reason" to JsonPrimitive("head-moved")) })
        val error = assertThrows<FsException> { runBlocking { store.manifest() } }
        assertEquals(FsFailure.NoManifest("head-moved"), error.failure)
        b.failNext.clear()
        b.failNext["ide.fs.manifest"] = ArrayDeque(listOf(b.error("ide.fs.manifest", -32039, "reason" to JsonPrimitive("git"))))
        assertEquals(FsFailure.NoManifest("git"), assertThrows<FsException> { runBlocking { store.manifest() } }.failure)
    }

    @Test fun `manifest - named paths report missing ones, and more than a page of names is asked in parts`() = runBlocking {
        val b = FakeFsBridge(mapOf("a" to "1", "b" to "2", "c" to "3"))
        val store = RemoteFileStore(b.call, RemoteLimits(pageEntries = 2))
        val m = store.manifest(paths = listOf("a", "gone", "c", "also-gone", "b"))
        assertEquals(setOf("a", "b", "c"), m.files.keys)
        assertEquals(setOf("gone", "also-gone"), m.missing)
        assertTrue(store.manifest(paths = emptyList()).files.isEmpty())
    }

    @Test fun `read - pieces are joined until eof, and a file that changes meanwhile is read again`() = runBlocking {
        val text = "0123456789abcdefghij"
        val b = FakeFsBridge(mapOf("a.txt" to text, "empty" to ""))
        val store = RemoteFileStore(b.call, RemoteLimits(requestBytes = 8))
        val c = store.read("a.txt")
        assertEquals(text, String(c.bytes))
        assertEquals(sha256Hex(text.toByteArray()), c.sha256)
        assertEquals(3, b.log.size)
        assertEquals(0, store.read("empty").bytes.size)
        b.log.clear()
        b.before[1] = { b.files["a.txt"] = "changed while it was read".toByteArray() }
        assertEquals("changed while it was read", String(store.read("a.txt").bytes))
        assertEquals(FsFailure.NotFound, assertThrows<FsException> { runBlocking { store.read("nope") } }.failure)
    }

    @Test fun `write - whole up to the limit, chunked above it, and a failed chunk aborts the upload`() = runBlocking {
        val b = FakeFsBridge(mapOf("old" to "v1"))
        val store = RemoteFileStore(b.call, RemoteLimits(requestBytes = 8, fileBytes = 1000))
        store.write("small", "12345678".toByteArray(), null)
        assertEquals(listOf("ide.fs.write"), b.log)
        b.log.clear()
        val big = "x".repeat(20)
        assertEquals(sha256Hex(big.toByteArray()), store.write("old", big.toByteArray(), sha256Hex("v1".toByteArray())))
        assertEquals(listOf("ide.fs.write.begin", "ide.fs.write.chunk", "ide.fs.write.chunk", "ide.fs.write.chunk", "ide.fs.write.commit"), b.log)
        assertEquals(big, b.text("old"))
        b.log.clear()
        b.failNext["ide.fs.write.chunk"] = ArrayDeque(listOf(b.error("ide.fs.write.chunk", -32036, "reason" to JsonPrimitive("offset"))))
        val error = assertThrows<FsException> { runBlocking { store.write("old", "y".repeat(20).toByteArray(), sha256Hex(big.toByteArray())) } }
        assertEquals(FsFailure.UploadFailed("offset"), error.failure)
        assertEquals("ide.fs.write.abort", b.log.last())
        assertEquals(0, b.openUploads)
        assertEquals(big, b.text("old"), "the target is untouched")
        assertTrue(assertThrows<FsException> { runBlocking { store.write("old", "z".toByteArray(), null) } }.failure is FsFailure.Mismatch)
        assertEquals(FsFailure.TooLarge(2000, 1000), assertThrows<FsException> { runBlocking { store.write("huge", ByteArray(2000), null) } }.failure)
    }

    @Test fun `delete, rename and subscribe`() = runBlocking {
        val b = FakeFsBridge(mapOf("a" to "1", "b" to "2"))
        val store = RemoteFileStore(b.call)
        store.rename("a", "c", sha256Hex("1".toByteArray()))
        assertEquals(setOf("b", "c"), b.files.keys)
        assertTrue(assertThrows<FsException> { runBlocking { store.rename("c", "b", sha256Hex("1".toByteArray())) } }.failure is FsFailure.TargetExists)
        b.held.add("b")
        assertEquals(FsFailure.Held("s1"), assertThrows<FsException> { runBlocking { store.delete("b", sha256Hex("2".toByteArray())) } }.failure)
        store.delete("b", sha256Hex("2".toByteArray()), duringTurn = true)
        assertEquals(setOf("c"), b.files.keys)
        assertEquals(FsSubscription("e1", 0, true), store.subscribe())
        assertEquals(FsSubscription("e1", 0, false), store.subscribe(5, "old"))
        b.online = false
        assertTrue(assertThrows<FsException> { runBlocking { store.delete("c", "0".repeat(64)) } }.failure is FsFailure.Offline)
        store.unsubscribe()
    }

    @Test fun `local files - the listing honours gitignore, skips links and metadata, and never leaves the root`() = runBlocking {
        val root = Files.createTempDirectory("calu-local")
        val outside = Files.createTempDirectory("calu-outside")
        Files.writeString(outside.resolve("secret"), "s")
        Files.createDirectories(root.resolve("pkg/__pycache__"))
        Files.createDirectories(root.resolve(".idea"))
        Files.createDirectories(root.resolve(".git"))
        Files.writeString(root.resolve(".gitignore"), "__pycache__/\n*.log\n")
        Files.writeString(root.resolve("a.py"), "a\n")
        Files.writeString(root.resolve("pkg/b.py"), "b\n")
        Files.writeString(root.resolve("pkg/__pycache__/b.pyc"), "x")
        Files.writeString(root.resolve("x.log"), "x")
        Files.writeString(root.resolve(".idea/workspace.xml"), "x")
        Files.writeString(root.resolve(".git/HEAD"), "x")
        Files.writeString(root.resolve("big.bin"), "0123456789".repeat(4))
        Files.createSymbolicLink(root.resolve("link"), outside.resolve("secret"))
        Files.createSymbolicLink(root.resolve("dirlink"), outside)
        val local = DiskLocalFiles(root)
        val listing = local.list(25)
        assertEquals(setOf(".gitignore", "a.py", "pkg/b.py"), listing.files.keys)
        assertEquals(sha256Hex("a\n".toByteArray()), listing.files["a.py"]!!.sha256)
        assertEquals(setOf(LocalSkipped("link", "symlink"), LocalSkipped("dirlink", "symlink"), LocalSkipped("big.bin", "too-large")), listing.skipped.toSet())
        assertEquals("ignored", local.leftOut("x.log"))
        assertEquals("ignored", local.leftOut("pkg/__pycache__/b.pyc"))
        assertEquals("excluded", local.leftOut(".idea/workspace.xml"))
        assertEquals("excluded", local.leftOut(".git/HEAD"))
        assertEquals("symlink", local.leftOut("dirlink/secret"))
        assertNull(local.leftOut("pkg/new.py"))
        assertNull(local.read("dirlink/secret"), "nothing is read through a link")
        assertThrows<java.io.IOException> { runBlocking { local.write("dirlink/x", "x".toByteArray(), null) } }
        assertThrows<java.io.IOException> { runBlocking { local.write("../x", "x".toByteArray(), null) } }
        assertFalse(Files.exists(outside.resolve("x")))
        local.write("new/deep/c.txt", "c".toByteArray(), 384)
        assertArrayEquals("c".toByteArray(), local.read("new/deep/c.txt"))
        assertEquals(384, local.entry("new/deep/c.txt")!!.mode)
        local.delete("new/deep/c.txt")
        assertFalse(Files.exists(root.resolve("new")), "empty directories are removed")
        assertNull(local.entry("new/deep/c.txt"))
    }

    @Test fun `the store keeps bases, queue and base contents, and prunes what no path needs`() {
        val dir = Files.createTempDirectory("calu-store")
        val store = SyncStore(dir)
        assertTrue(store.loadState().paths.isEmpty())
        val h = store.bases.put("one".toByteArray())
        val other = store.bases.put("two".toByteArray())
        store.saveState(SyncState("c".repeat(40), mapOf("a" to h)))
        store.saveQueue(PendingQueue().apply { saved("a"); deleted("b") })
        val again = SyncStore(dir)
        assertEquals(h, again.loadState().base("a"))
        assertEquals(2, again.loadQueue().size)
        assertArrayEquals("one".toByteArray(), again.bases.get(h))
        again.bases.prune(setOf(h))
        assertNull(again.bases.get(other))
        assertArrayEquals("one".toByteArray(), again.bases.get(h))
        assertEquals(16, syncStoreName("/p", "h", "/r").length)
        assertFalse(syncStoreName("/p", "h", "/r") == syncStoreName("/p", "h2", "/r"))
    }
}
