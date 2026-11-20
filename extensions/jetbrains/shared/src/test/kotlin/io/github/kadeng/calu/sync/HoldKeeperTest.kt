package io.github.kadeng.calu.sync

import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.ja
import io.github.kadeng.calu.state.jo
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files
import java.nio.file.Path

/**
 * B23: a save held for a running turn stays held across a restart of the IDE until the turn is
 * known to have ended. Runs [Synchronizer] with [HoldKeeper] over the scripted `ide.fs.*` bridge;
 * a "restart" is a new synchroniser and keeper on the same store directory.
 */
class HoldKeeperTest {
    private val root: Path = Files.createTempDirectory("calu-hold-local")
    private val storeDir: Path = Files.createTempDirectory("calu-hold-store")
    private val local = EditorLocalFiles(root)
    private val ui = RecordingUi()
    private val caps = listOf("cwd", "ping", "paths", "fs", "fs.watch")
    private val lines = (1..9).joinToString("") { "line $it\n" }

    /** The chat's state as the keeper sees it; the tests replace it. */
    private var chat: ViewState = initialViewState()

    /** The live holds of the running turn (what `heldPaths` would give for [chat]). */
    private val live = HashSet<String>()

    /**
     * Start one "IDE run": a keeper and a synchroniser on the test's store.
     *
     * @param bridge the remote side.
     * @return the synchroniser (not connected).
     */
    private fun ideRun(bridge: FakeFsBridge): Synchronizer {
        val keeper = HoldKeeper(SyncStore(storeDir))
        return Synchronizer(RemoteFileStore(bridge.call), local, SyncStore(storeDir), { keeper.held(chat, live) }, ui, "box:/srv/project", maxFileBytes = 500)
    }

    /**
     * Write a local file.
     *
     * @param path relative path.
     * @param text its text.
     */
    private fun put(path: String, text: String) {
        Files.writeString(root.resolve(path), text)
    }

    /**
     * Read a local file.
     *
     * @param path relative path.
     * @return its text.
     */
    private fun get(path: String): String = Files.readString(root.resolve(path))

    /**
     * The state after an attach to a session.
     *
     * @param id the session id.
     * @param state the session's state in the snapshot.
     * @return the view state.
     */
    private fun attached(id: String, state: String): ViewState =
        applyEvent(initialViewState(), ev("ui.attached", "session_id" to id, "snapshot" to jo("state" to state, "workspace" to "/srv/project"), "events" to ja()))

    /**
     * The state after a session list was read, nothing attached.
     *
     * @param running the listed sessions as (id, state, stale).
     * @return the view state.
     */
    private fun listed(vararg running: Triple<String, String, Boolean>): ViewState = applyEvent(
        initialViewState(),
        ev("ui.sessions", "running" to running.map { (id, state, stale) -> mapOf("id" to id, "state" to state, "directory" to "/srv/project", "here" to true, "stale" to stale) }),
    )

    /**
     * Run the first IDE run up to the held save: a turn of session s1 edits `h.txt`, the user
     * saves another line of it, the save is held. Then the IDE "closes".
     *
     * @param bridge the remote side.
     */
    private suspend fun holdBeforeRestart(bridge: FakeFsBridge) {
        val first = ideRun(bridge)
        first.connect(caps)
        chat = attached("s1", "running")
        live.add("h.txt")
        first.remoteChanged(bridge.change("h.txt", lines.replace("line 8\n", "agent\n")))
        put("h.txt", lines.replace("line 8\n", "agent\n").replace("line 2\n", "user\n"))
        first.localChanged(listOf("h.txt"))
        assertEquals(lines.replace("line 8\n", "agent\n"), bridge.text("h.txt"), "held in the first run")
        assertEquals(KeptHold("s1", setOf("h.txt")), SyncStore(storeDir).loadHolds(), "the hold is stored with its session")
        // The IDE closes; after its restart the chat knows nothing yet.
        chat = initialViewState()
        live.clear()
    }

    @Test fun `after a restart the held save is not uploaded before the turn is known to have ended`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("h.txt" to lines))
        holdBeforeRestart(bridge)
        val second = ideRun(bridge)
        second.connect(caps)
        assertEquals(lines.replace("line 8\n", "agent\n"), bridge.text("h.txt"), "not uploaded by the first comparison")
        assertEquals(listOf("h.txt"), second.status.value.held)
        // Attached again; the turn still runs (its edits not replayed yet): still held.
        chat = attached("s1", "running")
        second.holdsChanged()
        assertEquals(lines.replace("line 8\n", "agent\n"), bridge.text("h.txt"), "held while the turn runs")
        // The agent edits once more, then the turn ends: both changes are merged on both sides.
        second.remoteChanged(bridge.change("h.txt", lines.replace("line 8\n", "agent again\n")))
        assertEquals(lines.replace("line 8\n", "agent\n").replace("line 2\n", "user\n"), get("h.txt"), "the local change is kept")
        chat = attached("s1", "idle")
        second.holdsChanged()
        val both = lines.replace("line 8\n", "agent again\n").replace("line 2\n", "user\n")
        assertEquals(both, bridge.text("h.txt"))
        assertEquals(both, get("h.txt"))
        assertTrue(second.status.value.held.isEmpty())
        assertNull(SyncStore(storeDir).loadHolds(), "the record is gone with the hold")
    }

    @Test fun `a session list without the session ends the kept hold, a running or unread list does not`() = runBlocking {
        val bridge = FakeFsBridge(mapOf("h.txt" to lines))
        holdBeforeRestart(bridge)
        val second = ideRun(bridge)
        second.connect(caps)
        chat = applyEvent(initialViewState(), ev("ui.sessions", "error" to "the database is not reachable"))
        second.holdsChanged()
        assertEquals(listOf("h.txt"), second.status.value.held, "a list that failed tells nothing")
        chat = listed(Triple("s1", "running", false))
        second.holdsChanged()
        assertEquals(listOf("h.txt"), second.status.value.held, "s1 still runs")
        chat = listed(Triple("s1", "running", true), Triple("s2", "running", false))
        second.holdsChanged()
        assertTrue(second.status.value.held.isEmpty(), "s1's process is gone: its turn ended")
        assertEquals(lines.replace("line 8\n", "agent\n").replace("line 2\n", "user\n"), bridge.text("h.txt"))
    }

    @Test fun `holdEnded - only a known end counts`() {
        assertFalse(holdEnded(initialViewState(), "s1"), "nothing known after a restart")
        assertFalse(holdEnded(attached("s1", "running"), "s1"))
        assertTrue(holdEnded(attached("s1", "idle"), "s1"))
        assertFalse(holdEnded(attached("s2", "idle"), "s1"), "another session says nothing about s1")
        assertTrue(holdEnded(attached("s2", "idle"), null))
        assertTrue(holdEnded(listed(), "s1"))
        assertTrue(holdEnded(listed(Triple("s1", "idle", false)), "s1"))
        assertFalse(holdEnded(listed(Triple("s1", "escalated", false)), null))
    }

    @Test fun `KeptHold - encode and decode`() {
        val hold = KeptHold("s1", setOf("b.py", "pkg/a.py"))
        assertEquals(hold, KeptHold.decode(hold.encode()))
        assertEquals(KeptHold(null, setOf("x")), KeptHold.decode(KeptHold(null, setOf("x")).encode()))
        assertNull(KeptHold.decode(""))
        assertNull(KeptHold.decode("s1\n"))
    }
}
