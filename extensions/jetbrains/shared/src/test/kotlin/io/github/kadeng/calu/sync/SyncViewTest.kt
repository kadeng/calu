package io.github.kadeng.calu.sync

import io.github.kadeng.calu.logic.setupCards
import io.github.kadeng.calu.logic.syncCards
import io.github.kadeng.calu.state.SyncView
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.initialViewState
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The synchronisation's status in the chat's state and as cards. */
class SyncViewTest {
    @Test fun `ui sync sets and clears the view`() {
        val on = applyEvent(initialViewState(), ViewEvent("ui.sync", buildJsonObject {
            put("phase", JsonPrimitive("idle")); put("text", JsonPrimitive("calu sync: conflict in a.py"))
            put("conflicts", JsonArray(listOf(JsonPrimitive("a.py")))); put("held", JsonArray(listOf(JsonPrimitive("b.py"))))
            put("notes", JsonArray(listOf(JsonPrimitive("l: a link")))); put("queued", JsonPrimitive(1))
        }))
        assertEquals(SyncView("idle", "calu sync: conflict in a.py", null, listOf("a.py"), listOf("b.py"), listOf("l: a link"), 1), on.sync)
        assertNull(applyEvent(on, ViewEvent("ui.sync", JsonObject(emptyMap()))).sync)
    }

    @Test fun `cards - what waits for the user, most urgent first`() {
        assertTrue(syncCards(null).isEmpty())
        assertTrue(syncCards(SyncView("idle", "calu sync: in sync")).isEmpty())
        val refused = syncCards(SyncView("refused", "calu sync: not available", "Update calu on the server.")).single()
        assertEquals("error", refused.level)
        assertEquals("openSettings", refused.primary!!.action)
        val clone = syncCards(SyncView("waiting", "t", "The local folder is empty: 307 files can be cloned from box.")).single()
        assertEquals("Clone from server", clone.primary!!.label)
        assertEquals("syncNow", clone.primary!!.action)
        assertEquals("Sync now", syncCards(SyncView("waiting", "t", "A plan waits for your confirmation.")).single().primary!!.label)
        assertEquals("syncNow", syncCards(SyncView("failed", "t", "git")).single().primary!!.action)
        val busy = syncCards(SyncView("idle", "t", null, listOf("src/a.py", "b.py"), listOf("h.py"), listOf("l: a link"), 1))
        assertEquals(listOf("sync-conflict", "sync-held", "sync-notes"), busy.map { it.id })
        assertEquals("Merge a.py…", busy[0].primary!!.label)
        assertTrue(busy[0].message.contains("and 1 more: b.py"))
        assertTrue(busy[1].compact)
        assertEquals("1 file held until the turn ends", busy[1].short)
        assertEquals("calu sync: 1 path is not synchronised", busy[2].short)
        assertEquals(listOf("sync-conflict"), syncCards(busy.let { SyncView("idle", "t", null, listOf("a"), listOf("h"), listOf("n")) }, listOf("sync-held", "sync-notes")).map { it.id })
        assertTrue(syncCards(SyncView("offline", "t", null, held = listOf("h"))).isEmpty(), "offline: the status bar says it")
    }

    @Test fun `the cards come before the setup cards of the chat while the bridge runs`() {
        val state = initialViewState().copy(bridge = io.github.kadeng.calu.state.BridgeView(io.github.kadeng.calu.state.BridgeStatus.RUNNING), sync = SyncView("failed", "t", "why"))
        assertEquals("sync-failed", setupCards(state).first().id)
    }

    @Test fun `the project view marks conflicts, folders above them and held files`() {
        val status = SyncStatus(conflicts = listOf("src/a.py"), held = listOf("h.py"))
        assertEquals(SyncMark.CONFLICT, syncMark(status, "src/a.py", false))
        assertEquals(SyncMark.CONFLICT_BELOW, syncMark(status, "src", true))
        assertEquals(SyncMark.CONFLICT_BELOW, syncMark(status, "", true))
        assertEquals(null, syncMark(status, "sr", true), "a prefix of a name is not a parent")
        assertEquals(SyncMark.HELD, syncMark(status, "h.py", false))
        assertEquals(null, syncMark(status, "b.py", false))
    }
}

