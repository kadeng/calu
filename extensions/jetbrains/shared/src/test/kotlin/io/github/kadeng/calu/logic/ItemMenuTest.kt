package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.EditRange
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.ToolFile
import io.github.kadeng.calu.state.ToolView
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.applyEvents
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.jo
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The context menus of the transcript's items (port of `itemMenu.test.ts`, and the user message's actions). */
class ItemMenuTest {
    private val head = "0123456789abcdef0123456789abcdef01234567"

    /** State with one edit and one command, as produced by the session's events. */
    private val state = applyEvents(
        initialViewState(),
        listOf(
            ev("tool.use", "tool_use_id" to "e1", "name" to "Edit", "input" to mapOf("file_path" to "/w/proj/src/a.py")),
            ev("edit.diff", "tool_use_id" to "e1", "path" to "src/a.py", "before" to "x", "after" to "y", "start_line" to 12, "end_line" to 15),
            ev("check.result", "tool_use_id" to "e1", "tool_name" to "Edit", "route" to "edit", "decision" to "ALLOW", "git_head" to head),
            ev("tool.use", "tool_use_id" to "c1", "name" to "Bash", "input" to mapOf("command" to "ls -la")),
            ev("check.result", "tool_use_id" to "c1", "tool_name" to "Bash", "route" to "command", "decision" to "DENY", "reason" to "no", "git_head" to null),
            ev("tool.use", "tool_use_id" to "g1", "name" to "Grep", "input" to mapOf("pattern" to "x")),
        ),
    )
    private val edit = state.tools["e1"]!!
    private val command = state.tools["c1"]!!
    private val grep = state.tools["g1"]!!

    private fun tool(name: String = "Bash", id: String = "t1", input: Map<String, Any?> = emptyMap(), diffPath: String? = null, range: EditRange? = null) =
        ToolView(toolUseId = id, agentKey = MAIN, name = name, input = jo(*input.toList().toTypedArray()), diffPath = diffPath, editRange = range)

    @Test
    fun `stores git_head from check result and the line range from edit diff`() {
        assertEquals(head, edit.gitHead)
        assertEquals(EditRange(12, 15), edit.editRange)
        assertNull(command.gitHead)
    }

    @Test
    fun `accepts pure deletions and rejects invalid ranges`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("edit.diff", "tool_use_id" to "d", "path" to "a", "before" to "", "after" to "", "start_line" to 5, "end_line" to 4),
                ev("edit.diff", "tool_use_id" to "n", "path" to "a", "before" to "", "after" to "", "start_line" to null, "end_line" to null),
                ev("edit.diff", "tool_use_id" to "x", "path" to "a", "before" to "", "after" to "", "start_line" to 5, "end_line" to 2),
            ),
        )
        assertEquals(EditRange(5, 4), s.tools["d"]!!.editRange)
        assertNull(s.tools["n"]!!.editRange)
        assertNull(s.tools["x"]!!.editRange)
    }

    @Test
    fun `classifies tools`() {
        assertEquals("edit", toolKind(edit))
        assertEquals("edit", toolKind("Write"))
        assertEquals("command", toolKind(command))
        assertEquals("other", toolKind(grep))
    }

    @Test
    fun `formats heads and ranges`() {
        assertEquals("01234567", shortHead(head))
        assertEquals("unknown", shortHead(null))
        assertEquals("3", describeRange(EditRange(3, 3)))
        assertEquals("3-5", describeRange(EditRange(3, 5)))
        assertEquals("deleted before line 3", describeRange(EditRange(3, 2)))
        assertEquals("unknown", describeRange(null))
    }

    @Test
    fun `builds the edit menu`() {
        assertEquals(
            ItemMenu(
                "edit",
                listOf("File" to "src/a.py", "Tool" to "Edit", "Lines" to "12-15", "Decision" to "ALLOW", "Git HEAD" to "01234567"),
                listOf(
                    ItemMenuAction("openFileAtEdit", "Open file at edit"),
                    ItemMenuAction("showUncommittedChanges", "Show uncommitted changes of this file"),
                    ItemMenuAction("showEdit", "Show this edit"),
                    ItemMenuAction("copyPath", "Copy path"),
                ),
            ),
            menuFor(edit),
        )
    }

    @Test
    fun `builds the command menu and none for other tools`() {
        assertEquals(
            ItemMenu(
                "command",
                listOf("Command" to "ls -la", "Decision" to "DENY", "Git HEAD" to "unknown"),
                listOf(ItemMenuAction("copyCommand", "Copy command")),
            ),
            menuFor(command),
        )
        assertNull(menuFor(grep))
    }

    @Test
    fun `turns valid actions into tasks`() {
        assertEquals(ResolvedAction.OpenFile("openFileAtEdit", "src/a.py", 12), resolveItemAction(edit, "openFileAtEdit", "/w/proj"))
        assertEquals(ResolvedAction.OpenFile("showUncommittedChanges", "src/a.py", 12), resolveItemAction(edit, "showUncommittedChanges", "/w/proj"))
        assertEquals(ResolvedAction.ShowEdit("e1"), resolveItemAction(edit, "showEdit", "/w/proj"))
        assertEquals(ResolvedAction.Copy("src/a.py"), resolveItemAction(edit, "copyPath", "/w/proj"))
        assertEquals(ResolvedAction.Copy("ls -la"), resolveItemAction(command, "copyCommand", "/w/proj"))
    }

    private fun refused(pattern: String, block: () -> Unit) {
        val error = assertThrows(IllegalArgumentException::class.java) { block() }
        assertTrue(error.message!!.contains(pattern), error.message)
    }

    @Test
    fun `rejects unknown tools, actions not offered, and paths outside the workspace`() {
        refused("no longer") { resolveItemAction(null, "copyPath", "/w") }
        refused("not available") { resolveItemAction(command, "openFileAtEdit", "/w") }
        refused("not available") { resolveItemAction(grep, "copyCommand", "/w") }
        refused("outside") { resolveItemAction(edit.copy(diffPath = "/etc/passwd"), "openFileAtEdit", "/w/proj") }
        refused("outside") { resolveItemAction(edit.copy(diffPath = "src/../../x"), "copyPath", "/w/proj") }
    }

    @Test
    fun `uses line 1 when the range is unknown and converts absolute paths inside the folder`() {
        val noRange = edit.copy(editRange = null, diffPath = "/w/proj/b.py")
        assertEquals(ResolvedAction.OpenFile("openFileAtEdit", "b.py", 1), resolveItemAction(noRange, "openFileAtEdit", "/w/proj/"))
        assertEquals("c.py", workspaceRelative("./c.py", "/w"))
        refused("outside") { workspaceRelative("/w/projX/a", "/w/proj") }
    }

    @Test
    fun `ignores the case of the drive letter`() {
        assertEquals("src/a.py", workspaceRelative("C:\\work\\repo\\src\\a.py", "c:\\work\\repo"))
        refused("outside") { workspaceRelative("D:\\other\\a.py", "c:\\work\\repo") }
    }

    private val reads = applyEvents(
        initialViewState(),
        listOf(
            ev("tool.use", "tool_use_id" to "r1", "name" to "Read", "input" to mapOf("file_path" to "/w/proj/docs/a.md", "offset" to 40, "limit" to 20)),
            ev("tool.use", "tool_use_id" to "r2", "name" to "Read", "input" to mapOf("file_path" to "/home/u/.claude/skills/x/SKILL.md")),
        ),
    )

    @Test
    fun `read tool calls are their own kind, with a file link and an Open file action`() {
        val read = reads.tools["r1"]!!
        assertEquals("read", toolKind(read))
        assertEquals("openFile", fileLinkAction(read))
        assertEquals("openFileAtEdit", fileLinkAction(edit))
        assertNull(fileLinkAction(command))
        assertNull(fileLinkAction(grep))
        assertEquals(listOf("openFile", "copyPath"), menuFor(read)!!.actions.map { it.id })
    }

    @Test
    fun `read tool calls open the file at the first line read, inside the workspace only`() {
        assertEquals(ResolvedAction.OpenFile("openFileAtEdit", "docs/a.md", 40), resolveItemAction(reads.tools["r1"], "openFile", "/w/proj"))
        refused("outside") { resolveItemAction(reads.tools["r2"], "openFile", "/w/proj") }
    }

    @Test
    fun `stores valid file links on the tool and replaces them on a later event`() {
        val s = applyEvents(
            initialViewState(),
            listOf(
                ev("tool.use", "tool_use_id" to "b1", "name" to "Bash", "input" to mapOf("command" to "wc -l a.md")),
                ev("tool.files", "tool_use_id" to "b1", "files" to listOf(mapOf("raw" to "a.md", "path" to "/w/a.md"), mapOf("raw" to "", "path" to "/x"), 7)),
            ),
        )
        assertEquals(listOf(ToolFile("a.md", "/w/a.md")), s.tools["b1"]!!.files)
        val t = applyEvents(s, listOf(ev("tool.files", "tool_use_id" to "b1", "files" to emptyList<Any>())))
        assertEquals(emptyList<ToolFile>(), t.tools["b1"]!!.files)
    }

    @Test
    fun `a click opens the diff of an edit that has one, else the file`() {
        val withDiff = tool("Edit", input = mapOf("file_path" to "a.py"), diffPath = "a.py", range = EditRange(42, 44))
        assertEquals(RowClick.OpenDiff("Open diff"), rowClick(withDiff))
        assertEquals(RowClick.OpenFile("openFileAtEdit", 42, "Open file at line 42"), fileClick(withDiff))
        assertEquals(
            RowClick.OpenFile("openFileAtEdit", 1, "Open file at line 1 (no diff was recorded)"),
            rowClick(tool("Write", input = mapOf("file_path" to "a.py"))),
        )
    }

    @Test
    fun `a click opens a read file at the first line read`() {
        assertEquals(RowClick.OpenFile("openFile", 30, "Open file at line 30"), rowClick(tool("Read", input = mapOf("file_path" to "a.py", "offset" to 30))))
    }

    @Test
    fun `a click opens the tab of a subagent launch and expands everything else`() {
        assertEquals(RowClick.OpenTab("A9", "Open the subagent's tab"), rowClick(tool("Agent", id = "A9")))
        assertEquals(RowClick.Expand("Show the command and its output"), rowClick(tool("Bash", input = mapOf("command" to "ls"))))
        assertEquals(RowClick.Expand("Show details"), rowClick(tool("Grep", input = mapOf("pattern" to "x"))))
        assertTrue(rowClick(tool("Edit")) is RowClick.Expand)
        assertNull(fileClick(tool("Bash")))
    }

    @Test
    fun `finds the first changed line`() {
        assertEquals(2, firstChangedLine("a\nb\nc", "a\nB\nc"))
        assertEquals(3, firstChangedLine("a\nb", "a\nb\nc"))
        assertEquals(1, firstChangedLine("a", "a"))
        assertEquals(1, firstChangedLine("", "x"))
    }

    // ------------------------------------------------------------------ user messages

    private val running = ev("bridge.status", "status" to "running")
    private val attached = ev(
        "ui.attached", "session_id" to "s1", "snapshot" to mapOf("state" to "idle", "claude_session_id" to "c-1"),
        "events" to listOf(
            ev("user.input", "text" to "first", "uuid" to "u-1"), ev("turn.result", "subtype" to "success"),
            ev("user.input", "text" to "second", "uuid" to "u-2"),
            ev("user.input", "text" to "note", "source" to "system"),
            ev("user.input", "text" to "for the subagent", "to" to "A1"),
        ),
    )

    private fun users(vararg events: io.github.kadeng.calu.state.ViewEvent) =
        applyEvents(initialViewState(), events.toList()).let { s -> s to s.items.values.flatten().filterIsInstance<UserItem>() }

    @Test
    fun `the user's own message offers rewind, fork and copy with the turn and the uuid`() {
        val (s, messages) = users(running, attached)
        val menu = userMessageMenu(s, messages.first { it.text == "second" })
        assertEquals(listOf("rewind", "forkAt", "copy"), menu.actions.map { it.id })
        assertTrue(menu.actions.all { it.enabled })
        assertEquals(2, menu.turn)
        assertEquals("u-2", menu.uuid)
        assertNull(menu.note)
        assertTrue(menu.actions[0].title.contains("files are not reverted"))
    }

    @Test
    fun `nothing is before the first message of the conversation, once the record was read`() {
        val (before, messages) = users(running, attached)
        // Until the recorded history was read the view cannot know that turn 1 is the first.
        assertTrue(userMessageMenu(before, messages.first { it.text == "first" }).actions[0].enabled)
        val s = applyEvents(before, listOf(ev("ui.history", "session_id" to "s1", "history" to mapOf("turns" to emptyList<Any>(), "steps" to emptyList<Any>()))))
        val first = s.items[MAIN]!!.filterIsInstance<UserItem>().first { it.text == "first" }
        val menu = userMessageMenu(s, first)
        assertEquals(listOf(false, false, true), menu.actions.map { it.enabled })
        assertEquals(FIRST_MESSAGE_NOTE, menu.actions[0].title)
        assertEquals(FIRST_MESSAGE_NOTE, menu.actions[1].title)
    }

    @Test
    fun `rewind and fork need an attached session`() {
        val (s, messages) = users(running, attached, ev("ui.detached"))
        assertEquals(listOf(false, false, true), userMessageMenu(s, messages.first { it.text == "second" }).actions.map { it.enabled })
    }

    @Test
    fun `a system message and a message for a subagent can only be copied`() {
        val (s, messages) = users(running, attached)
        val system = userMessageMenu(s, messages.first { it.text == "note" })
        assertEquals(listOf("copy"), system.actions.map { it.id })
        assertNull(system.note)
        val relayed = userMessageMenu(s, messages.first { it.to == "A1" })
        assertEquals(listOf("copy"), relayed.actions.map { it.id })
        assertEquals(RELAYED_MESSAGE_NOTE, relayed.note)
        assertFalse(relayed.actions.any { it.id == "rewind" })
    }
}
