// The recorded conversation of a session as transcript entries, and its place in the view state.
// Port of extensions/vscode/test/unit/history.test.ts (same cases, same order).
package io.github.kadeng.calu.state

import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Build what a replay covers.
 *
 * @param uuids Ids of the replayed user messages.
 * @param inputs Trimmed texts of the replayed user messages.
 * @param toolUseIds Ids of the replayed tool calls.
 * @return The sets of `Covered`.
 */
private fun covered(uuids: List<String> = emptyList(), inputs: List<String> = emptyList(), toolUseIds: List<String> = emptyList()): Covered =
    Covered(uuids = uuids.toSet(), inputs = inputs.toSet(), toolUseIds = toolUseIds.toSet())

/** Three recorded turns: two inherited by a fork (their steps are in the turn records only), one of the session itself. */
private val HISTORY: JsonObject = jo(
    "calu_session_id" to "s2",
    "claude_session_id" to "c2",
    "chat_summary" to "",
    "turns" to listOf(
        jo(
            "index" to 1,
            "prompt_uuid" to "u1",
            "user_inputs" to listOf("read the greeter"),
            "steps" to listOf(
                jo("tool_name" to "Read", "summary" to "Reads greeter.py", "decision" to "pass", "agent_id" to null),
                jo("tool_name" to "Bash", "summary" to "Runs the tests", "decision" to "allow", "agent_id" to "toolu_agent"),
            ),
            "answer" to "It greets.",
            "ended_at" to "2026-11-18T10:00:00Z",
        ),
        jo("index" to 2, "prompt_uuid" to "u2", "user_inputs" to listOf("now change it", "  "), "steps" to ja(), "answer" to "", "ended_at" to "2026-11-18T10:01:00Z"),
        jo(
            "index" to 3,
            "prompt_uuid" to "u3",
            "user_inputs" to listOf("and commit"),
            "steps" to listOf(jo("tool_name" to "Bash", "summary" to "Commits", "decision" to "escalate")),
            "answer" to "Committed.",
            "ended_at" to "2026-11-18T10:02:00Z",
        ),
    ),
    "steps" to listOf(
        jo(
            "seq" to 1,
            "turn" to 3,
            "tool_use_id" to "t31",
            "tool_name" to "Bash",
            "tool_input" to jo("command" to "git commit -m x"),
            "decision" to "escalate",
            "reason" to "a commit",
            "summary" to "Commits",
            "result_summary" to "[main 1234] x",
        ),
        jo("seq" to 2, "turn" to 9, "tool_use_id" to "t99", "tool_name" to "Bash", "tool_input" to jo(), "decision" to "pass"),
    ),
)

/**
 * Build 300 short recorded turns ("m1" .. "m300", each with an answer and an end).
 *
 * @return The `turns` member of a `session.history` result.
 */
private fun manyTurns(): List<JsonObject> =
    List(300) { i -> jo("index" to i + 1, "user_inputs" to listOf("m${i + 1}"), "answer" to "ok", "ended_at" to "x") }

/** `historyEntries`. */
class HistoryEntriesTest {
    @Test
    fun `converts every turn when the replay covers nothing`() {
        val h = historyEntries(HISTORY, covered())
        assertEquals(3, h.turns)
        assertEquals(0, h.omittedTurns)
        assertEquals(
            listOf("user", "step", "step", "answer", "turnEnd", "user", "turnEnd", "user", "step", "answer", "turnEnd"),
            h.entries.map { it.kind },
        )
        assertEquals(HistoryEntry.User(text = "read the greeter", turn = 1, uuid = "u1"), h.entries[0])
        // An inherited step: only what the turn record holds.
        assertEquals(
            HistoryEntry.Step(toolUseId = null, name = "Read", input = null, decision = "PASS", reason = "", summary = "Reads greeter.py", result = null, subagent = false),
            h.entries[1],
        )
        val bash = h.entries[2] as HistoryEntry.Step
        assertEquals("Bash", bash.name)
        assertEquals("ALLOW", bash.decision)
        assertTrue(bash.subagent)
        // A step this session recorded: with its id, input and the start of its result.
        assertEquals(
            HistoryEntry.Step(
                toolUseId = "t31",
                name = "Bash",
                input = jo("command" to "git commit -m x"),
                decision = "ESCALATE",
                reason = "a commit",
                summary = "Commits",
                result = "[main 1234] x",
                subagent = false,
            ),
            h.entries[8],
        )
    }

    @Test
    fun `stops at the first turn the replay holds`() {
        // A fork that has run its own turn 3: the replay holds its input.
        val byUuid = historyEntries(HISTORY, covered(listOf("u3")))
        assertEquals(listOf(1, 2), byUuid.entries.filterIsInstance<HistoryEntry.User>().map { it.turn })
        assertEquals(2, byUuid.turns)
        // Without uuids the text of the input decides.
        assertEquals(byUuid.entries, historyEntries(HISTORY, covered(emptyList(), listOf("and commit"))).entries)
        // Only a step of the turn was replayed (the buffer moved on past the input): the input is
        // added, the replayed step and everything after it is not.
        val byStep = historyEntries(HISTORY, covered(emptyList(), emptyList(), listOf("t31")))
        assertEquals(listOf<HistoryEntry>(HistoryEntry.User(text = "and commit", turn = 3, uuid = "u3")), byStep.entries.takeLast(1))
        // A session that replays from its first turn: nothing to add.
        assertEquals(emptyList<HistoryEntry>(), historyEntries(HISTORY, covered(listOf("u1"))).entries)
        // Where the replay begins in the conversation's own numbering.
        assertEquals(1, historyEntries(HISTORY, covered(listOf("u1"))).firstReplayedTurn)
        assertEquals(3, byUuid.firstReplayedTurn)
        assertEquals(3, byStep.firstReplayedTurn)
        assertEquals(4, historyEntries(HISTORY, covered()).firstReplayedTurn)
    }

    @Test
    fun `survives malformed input`() {
        val malformed = listOf(
            null,
            json(7),
            json("x"),
            ja(),
            jo(),
            jo("turns" to "no"),
            jo("turns" to listOf(null, 3, jo("index" to "one")), "steps" to listOf(null, jo("turn" to 1))),
        )
        for (raw in malformed) {
            assertEquals(History(entries = emptyList(), turns = 0, omittedTurns = 0, firstReplayedTurn = 1), historyEntries(raw, covered()))
        }
        val odd = historyEntries(
            jo("turns" to listOf(jo("index" to 1, "user_inputs" to listOf(3, "hi"), "steps" to listOf(jo(), jo("tool_name" to "Read")), "answer" to 5))),
            covered(),
        )
        assertEquals(listOf("user", "step"), odd.entries.map { it.kind })
    }

    @Test
    fun `keeps the newest whole turns when the record is long`() {
        val h = historyEntries(jo("turns" to manyTurns()), covered())
        assertTrue(h.entries.size <= MAX_HISTORY_ENTRIES)
        assertEquals(0, h.entries.size % 3)
        assertEquals(300, h.turns + h.omittedTurns)
        assertTrue(h.omittedTurns > 0)
        assertEquals("m300", (h.entries[h.entries.size - 3] as HistoryEntry.User).text)
        // One turn that is longer than the limit is kept whole.
        val long = jo("index" to 1, "user_inputs" to listOf("x"), "steps" to List(MAX_HISTORY_ENTRIES + 5) { jo("tool_name" to "Bash", "summary" to "s") })
        assertEquals(MAX_HISTORY_ENTRIES + 6, historyEntries(jo("turns" to listOf(long)), covered()).entries.size)
    }
}

/** The reducer case `ui.history`. */
class UiHistoryTest {
    /**
     * Build the state of a forked session: attached, with the events of its own turn 3.
     *
     * @param events The recorded events the attach replays.
     * @return The state.
     */
    private fun forked(events: List<ViewEvent>): ViewState =
        applyEvents(
            initialViewState(),
            listOf(
                ev("bridge.status", "status" to "running"),
                ev("ui.attached", "session_id" to "s2", "snapshot" to jo("state" to "idle", "workspace" to "/w"), "events" to events),
            ),
        )

    @Test
    fun `puts the inherited turns above the events of the session, marked as recorded`() {
        val live = forked(
            listOf(
                ev("user.input", "text" to "and commit", "uuid" to "u3", "seq" to 1),
                ev("tool.use", "tool_use_id" to "t31", "name" to "Bash", "input" to jo("command" to "git commit -m x"), "seq" to 2),
            ),
        )
        val s = applyEvent(live, ev("ui.history", "session_id" to "s2", "history" to HISTORY))
        val main = list(s)
        assertEquals(listOf("divider", "user", "tool", "tool", "assistant", "turn", "user", "turn", "divider", "user", "tool"), main.map { it.kind })
        assertEquals(DividerItem(id = main[0].id, mark = "history-start", text = HISTORY_START_TEXT), main[0])
        assertEquals(
            UserItem(id = main[1].id, text = "read the greeter", source = "user", images = emptyList(), turn = 1, uuid = "u1", stored = true),
            main[1],
        )
        assertEquals(AssistantItem(id = main[4].id, text = "It greets.", stored = true), main[4])
        val turn = main[5] as TurnItem
        assertEquals(true, turn.stored)
        assertNull(turn.costUsd)
        // The session's own items follow, numbered as the conversation numbers its turns: the
        // view counted "and commit" as its turn 1, the conversation has it as turn 3.
        assertEquals(1, (list(live)[0] as UserItem).turn)
        val ownInput = main[9] as UserItem
        assertEquals("and commit", ownInput.text)
        assertEquals("u3", ownInput.uuid)
        assertEquals(3, ownInput.turn)
        assertSame(list(live)[1], main[10])
        assertEquals(2, s.turnBase)
        // A message that arrives later continues that numbering.
        val later = applyEvents(s, listOf(ev("turn.result"), ev("user.input", "text" to "more")))
        val more = list(later).last() as UserItem
        assertEquals("more", more.text)
        assertEquals(4, more.turn)
        assertEquals(main.size, main.map { it.id }.toSet().size)
        // A recorded step: decision and summary, no invented input or result.
        val read = own(s.tools, (main[2] as ToolItem).toolUseId)
        assertEquals("Read", read?.name)
        assertEquals(jo(), read?.input)
        assertEquals(ToolCheck(route = "stored", decision = "PASS", reason = "", summary = "Reads greeter.py", comment = "", checkerRoute = ""), read?.check)
        assertEquals(StoredStepInfo(subagent = false, hasInput = false), read?.stored)
        assertNull(read?.result)
        val bash = own(s.tools, (main[3] as ToolItem).toolUseId)
        assertEquals(StoredStepInfo(subagent = true, hasInput = false), bash?.stored)
        // No tab is invented for the subagent of a recorded step.
        assertEquals(emptyList<AgentInfo>(), s.agents)
        // Applied once: a second answer changes nothing, and neither does one for another session.
        assertSame(s, applyEvent(s, ev("ui.history", "session_id" to "s2", "history" to HISTORY)))
        assertSame(live, applyEvent(live, ev("ui.history", "session_id" to "other", "history" to HISTORY)))
    }

    @Test
    fun `shows a fresh fork that has no events yet, with the steps of its own record in full`() {
        val s = applyEvent(forked(emptyList()), ev("ui.history", "session_id" to "s2", "history" to HISTORY))
        assertEquals(3, list(s).filterIsInstance<ToolItem>().size)
        val commit = own(s.tools, "t31")
        assertEquals(jo("command" to "git commit -m x"), commit?.input)
        assertEquals("ESCALATE", commit?.check?.decision)
        assertEquals("a commit", commit?.check?.reason)
        assertEquals(ToolResultView(content = "[main 1234] x", isError = false), commit?.result)
        assertEquals(StoredStepInfo(subagent = false, hasInput = true), commit?.stored)
        // New events go below the recorded turns.
        val next = applyEvent(s, ev("user.input", "text" to "next", "uuid" to "u4"))
        val main = list(next)
        assertEquals("next", (main[main.size - 1] as UserItem).text)
        assertEquals("history-end", (main[main.size - 2] as DividerItem).mark)
    }

    @Test
    fun `adds nothing when the replay holds the whole conversation`() {
        val live = forked(listOf(ev("user.input", "text" to "read the greeter", "uuid" to "u1", "seq" to 1)))
        assertNull(live.turnBase)
        // Only the numbering becomes known: the first message is the conversation's first.
        val known = applyEvent(live, ev("ui.history", "session_id" to "s2", "history" to HISTORY))
        assertEquals(live.copy(turnBase = 0), known)
        assertSame(live.items, known.items)
        assertEquals(live.copy(turnBase = 0), applyEvent(live, ev("ui.history", "session_id" to "s2", "history" to null)))
        // Another attach forgets it until the history was read again.
        assertNull(applyEvent(known, ev("ui.attached", "session_id" to "s2", "snapshot" to jo("state" to "idle", "workspace" to "/w"), "events" to ja())).turnBase)
    }

    @Test
    fun `says how many older turns are left out`() {
        val s = applyEvent(forked(emptyList()), ev("ui.history", "session_id" to "s2", "history" to jo("turns" to manyTurns())))
        val first = list(s)[0] as DividerItem
        assertTrue(Regex("^Earlier in this conversation \\(\\d+ older turns are not shown\\)$").matches(first.text), first.text)
    }
}
