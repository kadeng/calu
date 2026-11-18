package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** [SyncRunner] against two in-memory stores: applying actions and updating the state. */
class SyncRunnerTest {
    private val engine = DefaultSyncEngine()

    @Test fun `the first run copies each side's files to the other`() {
        val local = MemoryFileStore(mapOf("both.txt" to "same\n", "mine.txt" to "local\n"))
        val remote = MemoryFileStore(mapOf("both.txt" to "same\n", "theirs/deep.txt" to "remote\r\n"))
        val report = SyncRunner(engine, local, remote).run(SyncState(), head = "a".repeat(40))
        assertTrue(report.open.isEmpty())
        assertEquals(local.texts(), remote.texts())
        assertEquals(setOf("both.txt", "mine.txt", "theirs/deep.txt"), report.state.paths)
        assertEquals(h("remote\r\n"), report.state.base("theirs/deep.txt"))
        assertEquals("a".repeat(40), report.state.head)
    }

    @Test fun `two diverged stores end with the same content, and a second run does nothing`() {
        val common = mapOf(
            "keep.txt" to "k\n",
            "local-edit.txt" to "v1\n",
            "remote-edit.txt" to "v1\n",
            "merge.txt" to "one\ntwo\nthree\nfour\nfive\n",
            "local-delete.txt" to "x\n",
            "remote-delete.txt" to "y\n",
            "both-delete.txt" to "z\n",
            "same-edit.txt" to "v1\n",
        )
        val local = MemoryFileStore(common)
        val remote = MemoryFileStore(common)
        val bases = MemoryBaseContents()
        val runner = SyncRunner(engine, local, remote, bases)
        val synced = runner.run(SyncState())
        assertEquals(common.keys, synced.state.paths)
        assertTrue(synced.applied.all { it is SyncAction.Settle })

        local.put("local-edit.txt", "v2 local\n")
        remote.put("remote-edit.txt", "v2 remote\n")
        local.put("merge.txt", "ONE\ntwo\nthree\nfour\nfive\n")
        remote.put("merge.txt", "one\ntwo\nthree\nfour\nFIVE\nsix\n")
        local.delete("local-delete.txt")
        remote.delete("remote-delete.txt")
        local.delete("both-delete.txt")
        remote.delete("both-delete.txt")
        local.put("same-edit.txt", "v2\n")
        remote.put("same-edit.txt", "v2\n")
        local.put("local-new.txt", "n\n")
        remote.put("dir/remote-new.txt", "m\n")

        val report = runner.run(synced.state)
        assertTrue(report.open.isEmpty(), report.open.toString())
        assertEquals(local.texts(), remote.texts())
        assertEquals(
            mapOf(
                "dir/remote-new.txt" to "m\n",
                "keep.txt" to "k\n",
                "local-edit.txt" to "v2 local\n",
                "local-new.txt" to "n\n",
                "merge.txt" to "ONE\ntwo\nthree\nfour\nFIVE\nsix\n",
                "remote-edit.txt" to "v2 remote\n",
                "same-edit.txt" to "v2\n",
            ),
            local.texts(),
        )
        assertEquals(local.texts().keys, report.state.paths)
        for ((path, text) in local.texts()) assertEquals(h(text), report.state.base(path), path)

        val again = runner.run(report.state)
        assertTrue(again.applied.isEmpty() && again.open.isEmpty())
        assertEquals(report.state, again.state)
    }

    @Test fun `conflicts stay open and neither side is changed`() {
        val start = mapOf("text.txt" to "a\nb\n", "bin.dat" to "b0", "del.txt" to "d\n")
        val local = MemoryFileStore(start)
        val remote = MemoryFileStore(start)
        val runner = SyncRunner(engine, local, remote)
        val state = runner.run(SyncState()).state

        local.put("text.txt", "local\nb\n")
        remote.put("text.txt", "remote\nb\n")
        local.write("bin.dat", byteArrayOf(1, 0, 1), FileEntry.DEFAULT_MODE)
        remote.write("bin.dat", byteArrayOf(2, 0, 2), FileEntry.DEFAULT_MODE)
        local.put("del.txt", "changed\n")
        remote.delete("del.txt")
        local.put("created.txt", "mine\n")
        remote.put("created.txt", "theirs\n")
        val before = local.texts() to remote.texts()

        val report = runner.run(state)
        assertEquals(
            listOf(
                SyncAction.Conflict("bin.dat", ConflictKind.BINARY),
                SyncAction.Conflict("created.txt", ConflictKind.CREATED_BOTH),
                SyncAction.Conflict("del.txt", ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY),
                SyncAction.Conflict("text.txt", ConflictKind.BOTH_MODIFIED),
            ),
            report.open,
        )
        assertTrue(report.applied.isEmpty())
        assertEquals(before, local.texts() to remote.texts())
        assertEquals(state, report.state)
    }

    @Test fun `a held path is not uploaded until the hold is lifted`() {
        val local = MemoryFileStore(mapOf("a.py" to "v1\n"))
        val remote = MemoryFileStore(mapOf("a.py" to "v1\n"))
        val runner = SyncRunner(engine, local, remote)
        val state = runner.run(SyncState()).state
        local.put("a.py", "v2\n")

        val held = runner.run(state, held = setOf("a.py"))
        assertEquals(listOf(SyncAction.Hold("a.py", HoldReason.AGENT_EDITING)), held.open)
        assertEquals("v1\n", remote.texts()["a.py"])

        val after = runner.run(held.state)
        assertEquals(listOf(SyncAction.Upload("a.py", h("v1\n"))), after.applied)
        assertEquals("v2\n", remote.texts()["a.py"])
    }

    @Test fun `a merge without the stored base content becomes a conflict`() {
        val local = MemoryFileStore(mapOf("a.txt" to "one\ntwo\nthree\n"))
        val remote = MemoryFileStore(mapOf("a.txt" to "one\ntwo\nthree\n"))
        val state = SyncRunner(engine, local, remote).run(SyncState()).state
        local.put("a.txt", "ONE\ntwo\nthree\n")
        remote.put("a.txt", "one\ntwo\nTHREE\n")
        val report = SyncRunner(engine, local, remote, MemoryBaseContents()).run(state)
        assertEquals(listOf(SyncAction.Conflict("a.txt", ConflictKind.BOTH_MODIFIED)), report.open)
    }

    @Test fun `downloads carry the file mode and the exact bytes`() {
        val local = MemoryFileStore()
        val remote = MemoryFileStore()
        val bytes = byteArrayOf(0, 1, 2, 13, 10, -1)
        remote.write("bin/tool", bytes, 493)
        val report = SyncRunner(engine, local, remote).run(SyncState())
        assertEquals(493, local.list().getValue("bin/tool").mode)
        assertEquals(sha256Hex(bytes), local.list().getValue("bin/tool").sha256)
        assertEquals(sha256Hex(bytes), report.state.base("bin/tool"))
        assertNull(report.state.base("other"))
    }
}
