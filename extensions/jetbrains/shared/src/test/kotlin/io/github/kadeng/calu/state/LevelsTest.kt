// Supervision levels: the auto-escalation switches as one of five named levels.
// Port of extensions/vscode/test/unit/levels.test.ts (same cases, same order).
package io.github.kadeng.calu.state

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

/** `LEVELS`, `levelOf`, `flagsForLevel` and `levelLabel`. */
class LevelsTest {
    @Test
    fun `are a ladder, no checkers first, then each level includes the one before`() {
        assertEquals(LEVEL_IDS, LEVELS.map { it.id })
        val count = { l: Level -> listOf(l.flags.commands, l.flags.edits, l.flags.subagents).count { it } }
        assertEquals(listOf(0, 0, 1, 2, 3), LEVELS.map(count))
        assertEquals(listOf(true, false, false, false, false), LEVELS.map { it.flags.unchecked })
    }

    @Test
    fun `name the level without checkers, and tell it from Checkers decide`() {
        assertEquals(AutoEscalateFlags(unchecked = true), flagsForLevel("unchecked"))
        assertEquals("Unchecked / Claude Code only", levelLabel(AutoEscalateFlags(unchecked = true)))
        assertEquals("custom", levelOf(AutoEscalateFlags(unchecked = true, edits = true)))
        assertEquals("Custom: no checkers, confirm edits", levelLabel(AutoEscalateFlags(unchecked = true, edits = true)))
    }

    @Test
    fun `map to switches and back`() {
        for (id in LEVEL_IDS) assertEquals(id, levelOf(flagsForLevel(id)))
        assertEquals(AutoEscalateFlags(commands = true, edits = true, subagents = false), flagsForLevel("writes"))
        assertThrows(IllegalArgumentException::class.java) { flagsForLevel("custom") }
        assertEquals("custom", levelOf(AutoEscalateFlags(commands = true)))
        assertEquals("checkers", levelOf(null))
    }

    @Test
    fun `have a label, also outside the ladder`() {
        assertEquals("Confirm edits", levelLabel(AutoEscalateFlags(edits = true)))
        assertEquals("Custom: confirm commands, subagent results", levelLabel(AutoEscalateFlags(commands = true, subagents = true)))
        assertEquals("Checkers decide", levelLabel(null))
    }
}
