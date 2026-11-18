package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.protocol.unlockedLogins
import kotlinx.serialization.json.JsonNull
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Tests of the password decisions, ported from `extensions/vscode/test/unit/unlock.test.ts`. */
class UnlockTest {
    /**
     * Build the facts of a request that was answered with error -32010.
     *
     * @param state The state at the moment the request was started and sent.
     * @param interactive False for a background request.
     * @param unlockedReported The logins the bridge reported with the error.
     * @param retries How often the request was already sent again.
     * @return The facts.
     */
    private fun need(
        state: UnlockState,
        interactive: Boolean = true,
        unlockedReported: List<String> = emptyList(),
        retries: Int = 0,
    ): PasswordNeed = PasswordNeed(
        interactive = interactive,
        unlockedReported = unlockedReported,
        startedAtDismissals = state.dismissals,
        sentAtUnlocks = state.unlocks,
        retries = retries,
    )

    /**
     * Build a state in which the first password was accepted.
     *
     * @param changing Whether the held login may change settings.
     * @return The state after one prompt round of kind "first".
     */
    private fun unlockedState(changing: Boolean): UnlockState {
        val prompted = onNeedsPassword(initialUnlockState(), need(initialUnlockState())).state
        return onUnlockResult(prompted, PROMPT_FIRST, UnlockResult(listOf("operator"), changing), 1).state
    }

    // ------------------------------------------------------------------ onNeedsPassword

    @Test
    fun `never prompts for a background request, and reports locked`() {
        val state = initialUnlockState()
        val decision = onNeedsPassword(state, need(state, interactive = false))
        assertEquals(NeedAction.FAIL, decision.action)
        assertNull(decision.state.promptOpen)
        assertTrue(decision.state.locked)
    }

    @Test
    fun `opens one prompt for the first interactive request, concurrent requests join it`() {
        val state = initialUnlockState()
        val first = onNeedsPassword(state, need(state))
        assertEquals(NeedAction.PROMPT_FIRST, first.action)
        assertEquals("first", first.state.promptOpen)
        val second = onNeedsPassword(first.state, need(state))
        assertEquals(NeedAction.JOIN, second.action)
        val background = onNeedsPassword(first.state, need(state, interactive = false))
        assertEquals(NeedAction.FAIL, background.action)
    }

    @Test
    fun `fails requests that started before an Escape and prompts again for a new one`() {
        val start = initialUnlockState()
        val waiting = need(start)
        val prompted = onNeedsPassword(start, waiting).state
        val dismissed = onDismiss(prompted)
        assertTrue(dismissed.locked)
        assertNull(dismissed.promptOpen)
        // A request that was already under way when the user pressed Escape.
        assertEquals(NeedAction.FAIL, onNeedsPassword(dismissed, waiting).action)
        // A request the user starts afterwards.
        assertEquals(NeedAction.PROMPT_FIRST, onNeedsPassword(dismissed, need(dismissed)).action)
    }

    @Test
    fun `retries without a prompt if an unlock succeeded after the request was sent`() {
        val before = initialUnlockState()
        val sentEarly = need(before)
        val after = unlockedState(false)
        assertEquals(NeedAction.RETRY, onNeedsPassword(after, sentEarly).action)
    }

    @Test
    fun `asks the admin password only when a login is held that may not change settings`() {
        val reading = unlockedState(false)
        val decision = onNeedsPassword(reading, need(reading, unlockedReported = listOf("operator")))
        assertEquals(NeedAction.PROMPT_ADMIN, decision.action)
        assertEquals("admin", decision.state.promptOpen)
        assertFalse(decision.state.locked)
        val changing = unlockedState(true)
        assertEquals(NeedAction.FAIL, onNeedsPassword(changing, need(changing)).action)
    }

    @Test
    fun `uses the logins the bridge reports when the state knows none`() {
        val state = initialUnlockState()
        val decision = onNeedsPassword(state, need(state, unlockedReported = listOf("operator")))
        assertEquals(NeedAction.PROMPT_ADMIN, decision.action)
        assertFalse(decision.state.locked)
    }

    @Test
    fun `asks no third password after the admin password was accepted`() {
        val reading = unlockedState(false)
        val prompted = onNeedsPassword(reading, need(reading)).state
        val step = onUnlockResult(prompted, PROMPT_ADMIN, UnlockResult(listOf("operator", "admin"), true), 1)
        assertEquals(RoundOutcome.DONE, step.outcome)
        assertEquals(NeedAction.FAIL, onNeedsPassword(step.state, need(step.state, retries = 2)).action)
    }

    @Test
    fun `fails a request that was already sent again too often`() {
        val state = initialUnlockState()
        assertEquals(NeedAction.FAIL, onNeedsPassword(state, need(state, retries = MAX_RETRIES_PER_REQUEST)).action)
        assertEquals(
            NeedAction.PROMPT_FIRST,
            onNeedsPassword(state, need(state, retries = MAX_RETRIES_PER_REQUEST - 1)).action,
        )
    }

    // ------------------------------------------------------------------ prompt round

    @Test
    fun `asks again with the bridge message after a wrong password, up to the cap`() {
        val start = initialUnlockState()
        val prompted = onNeedsPassword(start, need(start)).state
        val refused = onUnlockRefused(prompted, "wrong password for the 'admin' credential", 1)
        assertEquals(RoundOutcome.ASK_AGAIN, refused.outcome)
        assertEquals("wrong password for the 'admin' credential", refused.problem)
        assertEquals("first", refused.state.promptOpen)
        assertEquals(
            "Wrong password for the 'admin' credential — calu password (asked once)",
            promptText(PROMPT_FIRST, refused.problem),
        )
        val last = onUnlockRefused(refused.state, "wrong password", MAX_ATTEMPTS_PER_ROUND)
        assertEquals(RoundOutcome.GIVE_UP, last.outcome)
        assertNull(last.state.promptOpen)
        // Giving up is not an Escape: a new request may prompt again.
        assertEquals(0, last.state.dismissals)
        assertEquals(NeedAction.PROMPT_FIRST, onNeedsPassword(last.state, need(last.state)).action)
    }

    @Test
    fun `holds the logins, clears locked and offers Remember once after the first unlock`() {
        val start = onStatusLocked(initialUnlockState(), true)
        val prompted = onNeedsPassword(start, need(start)).state
        val step = onUnlockResult(prompted, PROMPT_FIRST, UnlockResult(listOf("operator"), false), 1)
        assertEquals(RoundOutcome.DONE, step.outcome)
        assertTrue(step.offerRemember)
        assertEquals(listOf("operator"), step.state.unlocked)
        assertFalse(step.state.locked)
        assertNull(step.state.promptOpen)
        assertEquals(1, step.state.unlocks)
        val again = onUnlockResult(step.state, PROMPT_FIRST, UnlockResult(listOf("operator"), false), 1)
        assertFalse(again.offerRemember)
    }

    @Test
    fun `does not offer Remember for the admin password`() {
        val reading = unlockedState(false)
        val fresh = reading.copy(rememberSettled = false)
        val step = onUnlockResult(fresh, PROMPT_ADMIN, UnlockResult(listOf("operator", "admin"), true), 1)
        assertFalse(step.offerRemember)
    }

    @Test
    fun `asks the admin password again if the entered one does not allow changes`() {
        val reading = unlockedState(false)
        val prompted = onNeedsPassword(reading, need(reading)).state
        val step = onUnlockResult(prompted, PROMPT_ADMIN, UnlockResult(listOf("operator"), false), 1)
        assertEquals(RoundOutcome.ASK_AGAIN, step.outcome)
        assertTrue(step.problem!!.contains("does not allow changing settings"))
        assertEquals("admin", step.state.promptOpen)
        val last = onUnlockResult(
            step.state,
            PROMPT_ADMIN,
            UnlockResult(listOf("operator"), false),
            MAX_ATTEMPTS_PER_ROUND,
        )
        assertEquals(RoundOutcome.GIVE_UP, last.outcome)
        assertNull(last.state.promptOpen)
    }

    @Test
    fun `keeps locked unchanged when the admin prompt is dismissed`() {
        val reading = unlockedState(false)
        val prompted = onNeedsPassword(reading, need(reading)).state
        val dismissed = onDismiss(prompted)
        assertFalse(dismissed.locked)
        assertEquals(1, dismissed.dismissals)
        assertEquals(listOf("operator"), dismissed.unlocked)
    }

    @Test
    fun `closes the prompt when the round is aborted`() {
        val start = initialUnlockState()
        val prompted = onNeedsPassword(start, need(start)).state
        val aborted = onRoundAborted(prompted)
        assertNull(aborted.promptOpen)
        assertEquals(0, aborted.dismissals)
    }

    // ------------------------------------------------------------------ onExplicitUnlock

    @Test
    fun `prompts after an Escape, joins an open prompt, and asks nothing when all is held`() {
        val start = initialUnlockState()
        val dismissed = onDismiss(onNeedsPassword(start, need(start)).state)
        val explicit = onExplicitUnlock(dismissed)
        assertEquals(ExplicitAction.PROMPT_FIRST, explicit.action)
        assertEquals(ExplicitAction.JOIN, onExplicitUnlock(explicit.state).action)
        assertEquals(ExplicitAction.PROMPT_ADMIN, onExplicitUnlock(unlockedState(false)).action)
        assertEquals(ExplicitAction.NONE, onExplicitUnlock(unlockedState(true)).action)
    }

    // ------------------------------------------------------------------ stored password

    @Test
    fun `is sent at the start only if one is stored`() {
        assertTrue(onStoredPassword("secret"))
        assertFalse(onStoredPassword(null))
        assertFalse(onStoredPassword(""))
    }

    @Test
    fun `unlocks without offering Remember again`() {
        val step = onStoredResult(initialUnlockState(), UnlockResult(listOf("admin"), true), refused = false)
        assertFalse(step.forget)
        assertEquals(listOf("admin"), step.state.unlocked)
        assertTrue(step.state.changing)
        assertFalse(step.state.locked)
        assertTrue(step.state.rememberSettled)
    }

    @Test
    fun `is forgotten when refused, the next interactive request then asks and offers again`() {
        val step = onStoredResult(initialUnlockState(), null, refused = true)
        assertTrue(step.forget)
        assertTrue(step.state.locked)
        val asked = onNeedsPassword(step.state, need(step.state))
        assertEquals(NeedAction.PROMPT_FIRST, asked.action)
        val unlocked = onUnlockResult(asked.state, PROMPT_FIRST, UnlockResult(listOf("admin"), true), 1)
        assertTrue(unlocked.offerRemember)
    }

    @Test
    fun `is kept when the unlock failed for another reason`() {
        val start = initialUnlockState()
        val step = onStoredResult(start, null, refused = false)
        assertFalse(step.forget)
        assertSame(start, step.state)
    }

    // ------------------------------------------------------------------ texts and guards

    @Test
    fun `builds the prompt texts`() {
        assertEquals("calu password (asked once)", promptText(PROMPT_FIRST, null))
        assertEquals("calu admin password, needed to change settings (asked once)", promptText(PROMPT_ADMIN, null))
        assertEquals("calu admin password, needed to change settings (asked once)", promptText(PROMPT_ADMIN, ""))
    }

    @Test
    fun `takes over the reported locked value`() {
        val start = initialUnlockState()
        assertSame(start, onStatusLocked(start, false))
        assertTrue(onStatusLocked(start, true).locked)
    }

    @Test
    fun `reads the held logins from error data`() {
        assertEquals(
            listOf("operator"),
            unlockedLogins(CaluJson.parseToJsonElement("""{"needs":"admin","unlocked":["operator",3,""]}""")),
        )
        assertEquals(emptyList<String>(), unlockedLogins(CaluJson.parseToJsonElement("""{"needs":"admin"}""")))
        assertEquals(emptyList<String>(), unlockedLogins(JsonNull))
        assertEquals(emptyList<String>(), unlockedLogins(null))
        assertEquals(emptyList<String>(), unlockedLogins(CaluJson.parseToJsonElement("""{"unlocked":"operator"}""")))
    }
}
