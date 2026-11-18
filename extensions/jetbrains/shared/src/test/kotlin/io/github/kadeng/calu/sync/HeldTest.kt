package io.github.kadeng.calu.sync

import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.state.ViewEvent
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.initialViewState
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** Which files are held back from upload while the agent edits them. */
class HeldTest {
    private fun ev(method: String, json: String) = ViewEvent(method, parseObjectOrNull(json)!!)

    @Test
    fun `files edited in the running turn are held until the turn ends`() {
        val attached = ev("ui.attached", """{"session_id":"s","snapshot":{"state":"running","workspace":"/srv/p"},"events":[]}""")
        val editing = listOf(
            attached,
            ev("session.state", """{"state":"running"}"""),
            ev("tool.use", """{"tool_use_id":"a","name":"Edit","input":{"file_path":"src/a.py"}}"""),
            ev("tool.result", """{"tool_use_id":"a","content":"ok"}"""),
            ev("tool.use", """{"tool_use_id":"b","name":"Write","input":{"file_path":"/srv/p/b.py"}}"""),
            ev("tool.use", """{"tool_use_id":"c","name":"Bash","input":{"command":"ls"}}"""),
        )
        assertEquals(setOf("/srv/p/src/a.py", "/srv/p/b.py"), heldPaths(applyEvents(initialViewState(), editing)))
        val ended = editing + ev("tool.result", """{"tool_use_id":"b","content":"ok"}""") + ev("turn.result", """{"subtype":"success"}""") + ev("session.state", """{"state":"idle"}""")
        assertEquals(emptySet<String>(), heldPaths(applyEvents(initialViewState(), ended)))
        val next = ended + ev("session.state", """{"state":"running"}""") + ev("tool.use", """{"tool_use_id":"d","name":"Edit","input":{"file_path":"c.py"}}""")
        assertEquals(setOf("/srv/p/c.py"), heldPaths(applyEvents(initialViewState(), next)))
    }
}
