package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** The three-way table of [SyncEngine], the incremental decisions and the replay of the queue. */
class SyncEngineTest {
    private val engine = DefaultSyncEngine()
    private val p = "src/a.py"

    /**
     * Decides [p] for contents given as text.
     *
     * @param base the last synchronised text, null for none.
     * @param local the local text, null for no file.
     * @param remote the remote text, null for no file.
     * @param held whether the agent edits the path.
     * @return the engine's decision.
     */
    private fun decide(base: String?, local: String?, remote: String?, held: Boolean = false): SyncAction? =
        engine.decide(p, base?.let(::h), local?.let { entry(p, it) }, remote?.let { entry(p, it) }, held)

    @Test fun `nothing on any side needs nothing`() = assertNull(decide(null, null, null))

    @Test fun `created locally is uploaded with a null base`() =
        assertEquals(SyncAction.Upload(p, null), decide(null, "L", null))

    @Test fun `created remotely is downloaded`() =
        assertEquals(SyncAction.Download(p, h("R")), decide(null, null, "R"))

    @Test fun `created on both sides with the same content settles`() =
        assertEquals(SyncAction.Settle(p, h("X")), decide(null, "X", "X"))

    @Test fun `created on both sides with different content is a conflict`() =
        assertEquals(SyncAction.Conflict(p, ConflictKind.CREATED_BOTH), decide(null, "L", "R"))

    @Test fun `unchanged on both sides needs nothing`() = assertNull(decide("B", "B", "B"))

    @Test fun `changed only remotely is downloaded`() =
        assertEquals(SyncAction.Download(p, h("R")), decide("B", "B", "R"))

    @Test fun `changed only locally is uploaded with the base`() =
        assertEquals(SyncAction.Upload(p, h("B")), decide("B", "L", "B"))

    @Test fun `changed to the same content on both sides settles`() =
        assertEquals(SyncAction.Settle(p, h("X")), decide("B", "X", "X"))

    @Test fun `changed differently on both sides is merged`() =
        assertEquals(SyncAction.Merge(p, h("B"), h("L"), h("R")), decide("B", "L", "R"))

    @Test fun `deleted remotely and unchanged locally is deleted locally`() =
        assertEquals(SyncAction.DeleteLocal(p), decide("B", "B", null))

    @Test fun `deleted locally and unchanged remotely is deleted remotely`() =
        assertEquals(SyncAction.DeleteRemote(p, h("B")), decide("B", null, "B"))

    @Test fun `deleted remotely and changed locally is a conflict`() =
        assertEquals(SyncAction.Conflict(p, ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY), decide("B", "L", null))

    @Test fun `changed remotely and deleted locally is a conflict`() =
        assertEquals(SyncAction.Conflict(p, ConflictKind.MODIFIED_REMOTELY_DELETED_LOCALLY), decide("B", null, "R"))

    @Test fun `deleted on both sides is forgotten`() =
        assertEquals(SyncAction.Settle(p, null), decide("B", null, null))

    @Test fun `a held path holds every decision that needs the local change`() {
        val hold = SyncAction.Hold(p, HoldReason.AGENT_EDITING)
        assertEquals(hold, decide(null, "L", null, held = true))
        assertEquals(hold, decide(null, "L", "R", held = true))
        assertEquals(hold, decide("B", "L", "B", held = true))
        assertEquals(hold, decide("B", "L", "R", held = true))
        assertEquals(hold, decide("B", null, "B", held = true))
        assertEquals(hold, decide("B", "L", null, held = true))
        assertEquals(hold, decide("B", null, "R", held = true))
    }

    @Test fun `a held path still takes remote changes and settles`() {
        assertEquals(SyncAction.Download(p, h("R")), decide("B", "B", "R", held = true))
        assertEquals(SyncAction.Download(p, h("R")), decide(null, null, "R", held = true))
        assertEquals(SyncAction.DeleteLocal(p), decide("B", "B", null, held = true))
        assertEquals(SyncAction.Settle(p, h("X")), decide("B", "X", "X", held = true))
        assertNull(decide("B", "B", "B", held = true))
    }

    @Test fun `files above the limits are skipped or conflict`() {
        val small = DefaultSyncEngine(maxFileBytes = 100, maxMergeBytes = 10)
        val b = h("B")
        assertEquals(
            SyncAction.Skip(p, SkipReason.TOO_LARGE),
            small.decide(p, b, entry(p, "B"), entry(p, "R", size = 101), false),
        )
        assertEquals(
            SyncAction.Skip(p, SkipReason.TOO_LARGE),
            small.decide(p, b, entry(p, "L", size = 101), entry(p, "B"), false),
        )
        assertEquals(
            SyncAction.Conflict(p, ConflictKind.TOO_LARGE),
            small.decide(p, b, entry(p, "L", size = 11), entry(p, "R"), false),
        )
        assertEquals(SyncAction.Download(p, h("R")), small.decide(p, b, entry(p, "B"), entry(p, "R", size = 100), false))
    }

    @Test fun `paths that break a rule are skipped`() {
        assertEquals(SyncAction.Skip("../x", SkipReason.INVALID_PATH), engine.decide("../x", null, entry("../x", "L"), null, false))
        assertEquals(SyncAction.Skip("/etc/x", SkipReason.INVALID_PATH), engine.decide("/etc/x", null, null, entry("/etc/x", "R"), false))
        assertEquals(
            SyncAction.Skip(".git/config", SkipReason.EXCLUDED),
            engine.decide(".git/config", null, entry(".git/config", "L"), null, false),
        )
        val windows = DefaultSyncEngine(PathMapper(windowsLocal = true))
        assertEquals(
            SyncAction.Skip("x/a?b.txt", SkipReason.NOT_REPRESENTABLE_LOCALLY),
            windows.decide("x/a?b.txt", null, null, entry("x/a?b.txt", "R"), false),
        )
    }

    @Test fun `plan covers every path of local, remote and state in path order`() {
        val local = listOf(entry("a", "B"), entry("b", "L"), entry("d", "new"), entry("e", "B")).associateBy { it.path }
        val remote = Manifest("c".repeat(40), listOf(entry("a", "R"), entry("b", "B"), entry("c", "B"), entry("f", "remote-new")))
        val state = SyncState(null, mapOf("a" to h("B"), "b" to h("B"), "c" to h("B"), "e" to h("B"), "g" to h("B")))
        assertEquals(
            listOf(
                SyncAction.Download("a", h("R")),
                SyncAction.Upload("b", h("B")),
                SyncAction.DeleteRemote("c", h("B")),
                SyncAction.Upload("d", null),
                SyncAction.DeleteLocal("e"),
                SyncAction.Download("f", h("remote-new")),
                SyncAction.Settle("g", null),
            ),
            engine.plan(local, remote, state, emptySet()),
        )
    }

    @Test fun `plan holds the uploads of held paths only`() {
        val local = listOf(entry("a", "L"), entry("b", "L")).associateBy { it.path }
        val remote = Manifest(null, listOf(entry("a", "B"), entry("b", "B")))
        val state = SyncState(null, mapOf("a" to h("B"), "b" to h("B")))
        assertEquals(
            listOf(SyncAction.Hold("a", HoldReason.AGENT_EDITING), SyncAction.Upload("b", h("B"))),
            engine.plan(local, remote, state, setOf("a")),
        )
    }

    @Test fun `plan leaves excluded directories out and skips case collisions on a case-insensitive side`() {
        val windows = DefaultSyncEngine(PathMapper(windowsLocal = true))
        val local = listOf(entry(".git/HEAD", "x"), entry("Readme.md", "L")).associateBy { it.path }
        val remote = Manifest(null, listOf(entry("README.md", "R"), entry("ok.txt", "R")))
        assertEquals(
            listOf(
                SyncAction.Skip("README.md", SkipReason.CASE_COLLISION),
                SyncAction.Skip("Readme.md", SkipReason.CASE_COLLISION),
                SyncAction.Download("ok.txt", h("R")),
            ),
            windows.plan(local, remote, SyncState(), emptySet()),
        )
        assertEquals(
            listOf(
                SyncAction.Download("README.md", h("R")),
                SyncAction.Upload("Readme.md", null),
                SyncAction.Download("ok.txt", h("R")),
            ),
            engine.plan(local, remote, SyncState(), emptySet()),
        )
    }

    @Test fun `a remote change of a clean file is downloaded`() {
        val state = SyncState(null, mapOf(p to h("B")))
        val change = RemoteChange(p, h("B"), h("R"), deleted = false)
        assertEquals(SyncAction.Download(p, h("R")), engine.onRemoteChanged(change, h("B"), state, dirtyInEditor = false))
    }

    @Test fun `the echo of an own upload needs nothing`() {
        val state = SyncState(null, mapOf(p to h("L")))
        val change = RemoteChange(p, h("B"), h("L"), deleted = false)
        assertNull(engine.onRemoteChanged(change, h("L"), state, dirtyInEditor = false))
        assertNull(engine.onRemoteChanged(change, h("L"), state, dirtyInEditor = true))
    }

    @Test fun `a remote change of a file with unsaved text is merged`() {
        val state = SyncState(null, mapOf(p to h("B")))
        val change = RemoteChange(p, h("B"), h("R"), deleted = false)
        assertEquals(
            SyncAction.Merge(p, h("B"), h("B"), h("R")),
            engine.onRemoteChanged(change, h("B"), state, dirtyInEditor = true),
        )
        assertEquals(
            SyncAction.Hold(p, HoldReason.AGENT_EDITING),
            engine.onRemoteChanged(change, h("B"), state, dirtyInEditor = true, held = setOf(p)),
        )
    }

    @Test fun `a remote change of a file saved locally with other content is merged`() {
        val state = SyncState(null, mapOf(p to h("B")))
        val change = RemoteChange(p, h("B"), h("R"), deleted = false)
        assertEquals(
            SyncAction.Merge(p, h("B"), h("L"), h("R")),
            engine.onRemoteChanged(change, h("L"), state, dirtyInEditor = false),
        )
        assertEquals(
            SyncAction.Hold(p, HoldReason.AGENT_EDITING),
            engine.onRemoteChanged(change, h("L"), state, dirtyInEditor = false, held = setOf(p)),
        )
    }

    @Test fun `a remote deletion deletes a clean file and conflicts with unsaved text`() {
        val state = SyncState(null, mapOf(p to h("B")))
        val change = RemoteChange(p, h("B"), null, deleted = true)
        assertEquals(SyncAction.DeleteLocal(p), engine.onRemoteChanged(change, h("B"), state, dirtyInEditor = false))
        assertEquals(
            SyncAction.Conflict(p, ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY),
            engine.onRemoteChanged(change, h("B"), state, dirtyInEditor = true),
        )
        assertEquals(SyncAction.Settle(p, null), engine.onRemoteChanged(change, null, state, dirtyInEditor = false))
    }

    @Test fun `a file created remotely is downloaded, or conflicts with an unsaved new document`() {
        val change = RemoteChange(p, null, h("R"), deleted = false)
        assertEquals(SyncAction.Download(p, h("R")), engine.onRemoteChanged(change, null, SyncState(), dirtyInEditor = false))
        assertEquals(
            SyncAction.Conflict(p, ConflictKind.CREATED_BOTH),
            engine.onRemoteChanged(change, null, SyncState(), dirtyInEditor = true),
        )
    }

    @Test fun `a local save is uploaded, held, queued or ignored`() {
        val state = SyncState(null, mapOf(p to h("B")))
        assertEquals(SyncAction.Upload(p, h("B")), engine.onLocalSaved(p, h("L"), state, emptySet(), online = true))
        assertEquals(SyncAction.Upload("new.txt", null), engine.onLocalSaved("new.txt", h("L"), state, emptySet(), online = true))
        assertEquals(SyncAction.DeleteRemote(p, h("B")), engine.onLocalSaved(p, null, state, emptySet(), online = true))
        assertNull(engine.onLocalSaved(p, h("B"), state, emptySet(), online = true))
        assertNull(engine.onLocalSaved("never.txt", null, state, emptySet(), online = true))
        assertEquals(SyncAction.Hold(p, HoldReason.AGENT_EDITING), engine.onLocalSaved(p, h("L"), state, setOf(p), online = true))
        assertEquals(SyncAction.Hold(p, HoldReason.OFFLINE), engine.onLocalSaved(p, h("L"), state, setOf(p), online = false))
        assertEquals(
            SyncAction.Skip(".git/config", SkipReason.EXCLUDED),
            engine.onLocalSaved(".git/config", h("L"), state, emptySet(), online = true),
        )
    }

    @Test fun `replay uploads queued paths in queue order and merges where the base moved`() {
        val state = SyncState(null, mapOf("a" to h("B"), "b" to h("B"), "c" to h("B"), "old" to h("B"), "gone" to h("B")))
        val queue = PendingQueue()
        for (path in listOf("c", "a", "b")) {
            assertEquals(SyncAction.Hold(path, HoldReason.OFFLINE), engine.onLocalSaved(path, h("L"), state, emptySet(), online = false))
            queue.saved(path)
        }
        queue.renamed("old", "new")
        queue.deleted("gone")
        queue.saved("same")
        val local = listOf(entry("a", "L"), entry("b", "L"), entry("c", "L"), entry("new", "B"), entry("same", "X"))
            .associateBy { it.path }
        // Meanwhile the remote side changed b (the base moved), deleted c, changed "gone" and created "same".
        val remote = Manifest(null, listOf(entry("a", "B"), entry("b", "R"), entry("old", "B"), entry("gone", "R"), entry("same", "X")))
        assertEquals(
            listOf(
                SyncAction.Conflict("c", ConflictKind.DELETED_REMOTELY_MODIFIED_LOCALLY),
                SyncAction.Upload("a", h("B")),
                SyncAction.Merge("b", h("B"), h("L"), h("R")),
                SyncAction.DeleteRemote("old", h("B")),
                SyncAction.Upload("new", null),
                SyncAction.Conflict("gone", ConflictKind.MODIFIED_REMOTELY_DELETED_LOCALLY),
                SyncAction.Settle("same", h("X")),
            ),
            engine.replay(queue.ops(), local, remote, state, emptySet()),
        )
        assertEquals(
            SyncAction.Hold("a", HoldReason.AGENT_EDITING),
            engine.replay(queue.ops(), local, remote, state, setOf("a"))[1],
        )
    }
}
