package io.github.kadeng.calu.logic

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The activity line (port of `activity.test.ts`). */
class ActivityTest {
    @Test
    fun `animates a spinner and a verb with the elapsed time while busy`() {
        val a = activityLine(true, "running", 0, null, 12_000, 0)
        assertTrue(a.active)
        assertEquals("busy", a.kind)
        assertEquals("${ACTIVITY_VERBS[3]}…", a.label)
        assertEquals("12s", a.elapsed)
        assertEquals(SPINNER_FRAMES[100 % SPINNER_FRAMES.size], a.icon)
        assertEquals("${a.icon} ${a.label} 12s", a.text)
        val next = activityLine(true, "running", 0, null, 12_000 + FRAME_MS, 0)
        assertNotEquals(a.icon, next.icon)
        assertEquals(a.label, next.label)
        assertEquals("${ACTIVITY_VERBS[5]}…", activityLine(true, "running", 0, null, VERB_MS - 1, 5).label)
    }

    @Test
    fun `keeps every animated label within the fixed label column`() {
        for (label in ACTIVITY_VERBS.map { "$it…" } + WAITING_LABEL) assertTrue(label.length < LABEL_COLUMN_CH, label)
    }

    @Test
    fun `shows waiting, done, idle and stopped states`() {
        val waiting = activityLine(true, "escalated", 0, null, 40_000, 0)
        assertEquals(Activity("‖", WAITING_LABEL, "40s", "‖ $WAITING_LABEL 40s", true, "waiting"), waiting)
        val done = activityLine(true, "idle", 0, 52_000, 99_000, 0)
        assertEquals(listOf("✓", "Done", "52s"), listOf(done.icon, done.label, done.elapsed))
        assertEquals("Idle", activityLine(true, "idle", 0, null, 0, 0).text)
        assertEquals("Stopped", activityLine(true, "stopped", 0, 5, 0, 0).text)
        val off = activityLine(false, "running", 0, null, 0, 0)
        assertEquals("Not connected", off.text)
        assertFalse(off.active)
    }

    @Test
    fun `maps session states to phases and labels a running check`() {
        assertEquals("busy", sessionPhase("tool_call"))
        assertEquals("waiting", sessionPhase("escalated"))
        assertEquals("failed", sessionPhase("failed"))
        assertEquals("idle", sessionPhase("nonsense"))
        val checking = activityLine(true, "checking", 0, null, 3000, 0)
        assertEquals(CHECKING_LABEL, checking.label)
        assertEquals("busy", checking.kind)
        assertEquals("Failed", activityLine(true, "failed", 0, null, 0, 0).text)
    }

    @Test
    fun `formats durations`() {
        assertEquals("0s", formatElapsed(-5))
        assertEquals("59s", formatElapsed(59_999))
        assertEquals("3m 05s", formatElapsed(185_000))
    }

    @Test
    fun `counts the waiting from the oldest open step's time, not from the view's`() {
        val now = java.time.Instant.parse("2026-11-19T10:03:00Z").toEpochMilli()
        val oldest = java.time.Instant.parse("2026-11-19T10:00:00Z").toEpochMilli()
        val since = waitingSince(listOf("2026-11-19T11:01:00+01:00", null, "2026-11-19T10:00:00Z", "garbage"), 5, now)
        assertEquals(oldest, since)
        assertEquals("3m 00s", activityLine(true, "escalated", since, null, now, 0).elapsed)
        assertEquals(5L, waitingSince(listOf(null, "not a time"), 5, now))
        assertEquals(now, waitingSince(listOf("2026-11-19T10:09:00Z"), 5, now))
    }
}
