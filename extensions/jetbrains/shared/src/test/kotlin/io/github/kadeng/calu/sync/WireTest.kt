package io.github.kadeng.calu.sync

import io.github.kadeng.calu.state.jo
import kotlinx.serialization.json.JsonNull
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The `ide.fs.*` requests and answers as the bridge takes and gives them (sync spec section 13). */
class WireTest {
    private val h = "a".repeat(64)

    @Test
    fun `only canonical relative paths pass, the bridge repairs none`() {
        for (ok in listOf("a.py", "src/a.py", ".gitignore", "dir with space/ü.txt")) assertNull(wirePathProblem(ok), ok)
        for (invalid in listOf("", "a//b", "./a", "a/", "a/./b", "a\\b", "a\u0000b", "a\nb", "x".repeat(4097), "d/" + "y".repeat(256))) {
            assertEquals("invalid", wirePathProblem(invalid), invalid.take(20))
        }
        for (outside in listOf("/etc/passwd", "C:/x", "c:x", "../a", "a/../b")) assertEquals("outside", wirePathProblem(outside), outside)
        // 255 bytes, not 255 characters: "ü" is two bytes.
        assertNull(wirePathProblem("y".repeat(255)))
        assertEquals("invalid", wirePathProblem("ü".repeat(128)))
    }

    @Test
    fun `a write always carries the key base_sha256, null says the file must not exist`() {
        assertEquals(
            jo("path" to "src/a.py", "content_b64" to "aGk=", "base_sha256" to null, "during_turn" to false),
            fsWriteParams("src/a.py", "aGk=", null),
        )
        assertTrue(fsWriteParams("a", "", null).containsKey("base_sha256"))
        assertEquals(JsonNull, fsWriteBeginParams("a", null, 10, h)["base_sha256"])
        assertEquals(
            jo("path" to "a", "content_b64" to "aGk=", "base_sha256" to h, "mode" to 420, "during_turn" to true),
            fsWriteParams("a", "aGk=", h, mode = 420, duringTurn = true),
        )
        assertEquals(
            jo("path" to "big.bin", "base_sha256" to h, "size" to 2_000_000, "sha256" to h, "mode" to 493, "during_turn" to false),
            fsWriteBeginParams("big.bin", h, 2_000_000, h, mode = 493),
        )
    }

    @Test
    fun `mode carries the permission bits only, and is left out when unknown`() {
        assertEquals(493, wireMode(493)) // 0o755
        assertEquals(493, wireMode(33261)) // 0o100755: a regular file's st_mode
        assertEquals(493, wireMode(2541)) // 0o4755: the set-user-id bit is dropped
        assertNull(wireMode(null))
        assertFalse(fsWriteParams("a", "", h).containsKey("mode"))
        assertEquals(MAX_WIRE_MODE, 511)
    }

    @Test
    fun `delete and rename name a hash, and no request is built for a path the bridge would refuse`() {
        assertEquals(jo("path" to "a.py", "base_sha256" to h, "during_turn" to false), fsDeleteParams("a.py", h))
        assertEquals(jo("from" to "a.py", "to" to "b/c.py", "base_sha256" to h, "during_turn" to false), fsRenameParams("a.py", "b/c.py", h))
        assertThrows(IllegalArgumentException::class.java) { fsWriteParams("./a.py", "", null) }
        assertThrows(IllegalArgumentException::class.java) { fsDeleteParams("/abs", h) }
        assertThrows(IllegalArgumentException::class.java) { fsRenameParams("a", "b\\c", h) }
        assertThrows(IllegalArgumentException::class.java) { fsWriteBeginParams("a", null, -1, h) }
    }

    @Test
    fun `a moved head or an expired cursor restarts the manifest, other failures do not`() {
        assertTrue(manifestMustRestart(-32039, jo("reason" to "head-moved")))
        assertTrue(manifestMustRestart(-32039, jo("reason" to "cursor-expired")))
        for (reason in listOf("git", "not-git", "too-many-files", "unreadable")) assertFalse(manifestMustRestart(-32039, jo("reason" to reason)), reason)
        assertFalse(manifestMustRestart(-32039, null))
        assertFalse(manifestMustRestart(-32031, jo("reason" to "head-moved")))
        assertEquals(-32039, FsError.NO_MANIFEST)
    }

    @Test
    fun `a manifest page lists files, what was skipped and why, and the cursor of the next page`() {
        val page = parseManifestPage(
            jo(
                "head" to "c0ffee",
                "files" to listOf(
                    mapOf("path" to "a.py", "size" to 3, "mode" to 493, "sha256" to h), mapOf("path" to "b.py", "sha256" to h),
                    mapOf("path" to "/abs", "sha256" to h), mapOf("path" to "no-hash"), 7,
                ),
                "skipped" to listOf(
                    mapOf("path" to "bad\\name", "reason" to "unsupported-name"), mapOf("path" to "link", "reason" to "symlink"),
                    mapOf("path" to "big", "reason" to "too-large"), mapOf("path" to "pipe", "reason" to "not-regular"),
                    mapOf("path" to "sub", "reason" to "submodule"), mapOf("path" to "x", "reason" to "new-reason"),
                ),
                "missing" to listOf("gone.py"), "next_cursor" to "p2",
            ),
        )
        assertEquals("c0ffee", page.head)
        assertEquals(listOf(FileEntry("a.py", 3, 493, h), FileEntry("b.py", 0, FileEntry.DEFAULT_MODE, h)), page.files)
        assertEquals(
            listOf(
                RemoteSkipReason.UNSUPPORTED_NAME, RemoteSkipReason.SYMLINK, RemoteSkipReason.TOO_LARGE, RemoteSkipReason.NOT_REGULAR,
                RemoteSkipReason.SUBMODULE, RemoteSkipReason.OTHER,
            ),
            page.skipped.map { it.reason },
        )
        assertEquals(RemoteSkipped("x", RemoteSkipReason.OTHER, "new-reason"), page.skipped.last())
        assertEquals(listOf("gone.py"), page.missing)
        assertEquals("p2", page.nextCursor)

        val last = parseManifestPage(jo("head" to null, "files" to listOf(mapOf("path" to "c.py", "sha256" to h)), "next_cursor" to null))
        assertNull(last.nextCursor)
        assertEquals(Manifest("c0ffee", page.files + last.files), manifestOf(listOf(page, last)))
        assertThrows(IllegalArgumentException::class.java) { manifestOf(emptyList()) }
    }

    @Test
    fun `a change says who made it, and a missing before hash does not say the file is new`() {
        val changed = parseFsChanged(
            jo(
                "epoch" to "e1", "seq" to 7,
                "changes" to listOf(
                    mapOf("path" to "a.py", "before_sha256" to null, "after_sha256" to h, "deleted" to false, "origin" to "ide"),
                    mapOf("path" to "b.py", "before_sha256" to h, "after_sha256" to null, "deleted" to true, "origin" to "agent"),
                    mapOf("path" to "c.py", "after_sha256" to h, "origin" to "someone"),
                    mapOf("path" to "../x", "after_sha256" to h), "nope",
                ),
            ),
        )
        assertEquals("e1", changed.epoch)
        assertEquals(7, changed.seq)
        assertEquals(
            listOf(
                RemoteChange("a.py", null, h, false, "ide"), RemoteChange("b.py", h, null, true, "agent"), RemoteChange("c.py", null, h, false, "other"),
            ),
            changed.changes,
        )
        assertEquals(listOf(true, false, false), changed.changes.map { it.isOwnWrite })

        // The engine decides from the base of the state: with a base, a change whose
        // `before_sha256` is null is a download of a changed file, not a file created on both sides.
        val engine = DefaultSyncEngine()
        val base = "b".repeat(64)
        val state = SyncState().with("a.py", base)
        assertEquals(SyncAction.Download("a.py", h), engine.onRemoteChanged(changed.changes[0], base, state, dirtyInEditor = false))
    }
}
