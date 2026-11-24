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

    /** The bridge's message after a wrong calu user password (`logins.refused`). */
    private val refusedMessage =
        "That is not the calu user password. Forgot it? DOCUMENTATION.md, \"A forgotten user password\", says how to set a new one."

    /**
     * Build a state in which the calu user password was accepted in a prompt.
     *
     * @return The state after one successful prompt round.
     */
    private fun unlockedState(): UnlockState {
        val prompted = onNeedsPassword(initialUnlockState(), need(initialUnlockState())).state
        return onUnlockResult(prompted, UnlockResult(listOf("user"))).state
    }

    // ------------------------------------------------------------------ onNeedsPassword

    @Test
    fun `never prompts for a background request, and reports locked`() {
        val state = initialUnlockState()
        val decision = onNeedsPassword(state, need(state, interactive = false))
        assertEquals(NeedAction.FAIL, decision.action)
        assertFalse(decision.state.promptOpen)
        assertTrue(decision.state.locked)
    }

    @Test
    fun `opens one prompt for the first interactive request, concurrent requests join it`() {
        val state = initialUnlockState()
        val first = onNeedsPassword(state, need(state))
        assertEquals(NeedAction.PROMPT, first.action)
        assertTrue(first.state.promptOpen)
        assertEquals(NeedAction.JOIN, onNeedsPassword(first.state, need(state)).action)
        assertEquals(NeedAction.FAIL, onNeedsPassword(first.state, need(state, interactive = false)).action)
    }

    @Test
    fun `fails requests that started before an Escape and prompts again for a new one`() {
        val start = initialUnlockState()
        val waiting = need(start)
        val prompted = onNeedsPassword(start, waiting).state
        val dismissed = onDismiss(prompted)
        assertTrue(dismissed.locked)
        assertFalse(dismissed.promptOpen)
        // A request that was already under way when the user pressed Escape.
        assertEquals(NeedAction.FAIL, onNeedsPassword(dismissed, waiting).action)
        // A request the user starts afterwards.
        assertEquals(NeedAction.PROMPT, onNeedsPassword(dismissed, need(dismissed)).action)
    }

    @Test
    fun `retries without a prompt if an unlock succeeded after the request was sent`() {
        val sentEarly = need(initialUnlockState())
        assertEquals(NeedAction.RETRY, onNeedsPassword(unlockedState(), sentEarly).action)
    }

    @Test
    fun `asks no second password while a login is held`() {
        val held = unlockedState()
        val decision = onNeedsPassword(held, need(held))
        assertEquals(NeedAction.FAIL, decision.action)
        assertFalse(decision.state.promptOpen)
        assertFalse(decision.state.locked)
    }

    @Test
    fun `uses the logins the bridge reports when the state knows none`() {
        val state = initialUnlockState()
        val decision = onNeedsPassword(state, need(state, unlockedReported = listOf("user")))
        assertEquals(NeedAction.FAIL, decision.action)
        assertFalse(decision.state.locked)
    }

    @Test
    fun `fails a request that was already sent again too often`() {
        val state = initialUnlockState()
        assertEquals(NeedAction.FAIL, onNeedsPassword(state, need(state, retries = MAX_RETRIES_PER_REQUEST)).action)
        assertEquals(
            NeedAction.PROMPT,
            onNeedsPassword(state, need(state, retries = MAX_RETRIES_PER_REQUEST - 1)).action,
        )
    }

    // ------------------------------------------------------------------ prompt round

    @Test
    fun `asks again with the bridge message after a wrong password, up to the cap`() {
        val start = initialUnlockState()
        val prompted = onNeedsPassword(start, need(start)).state
        val refused = onUnlockRefused(prompted, refusedMessage, 1)
        assertEquals(RoundOutcome.ASK_AGAIN, refused.outcome)
        assertEquals(refusedMessage, refused.problem)
        assertTrue(refused.state.promptOpen)
        assertEquals(2, refused.left)
        assertEquals("$refusedMessage (2 tries left) — calu user password (asked once)", promptText(refused.problem, refused.left))
        val second = onUnlockRefused(refused.state, refusedMessage, 2)
        assertEquals(1, second.left)
        assertEquals("$refusedMessage (1 try left) — calu user password (asked once)", promptText(second.problem, second.left))
        val last = onUnlockRefused(refused.state, refusedMessage, MAX_ATTEMPTS_PER_ROUND)
        assertEquals(RoundOutcome.GIVE_UP, last.outcome)
        assertFalse(last.state.promptOpen)
        // Giving up is not an Escape: a new request may prompt again.
        assertEquals(0, last.state.dismissals)
        assertEquals(NeedAction.PROMPT, onNeedsPassword(last.state, need(last.state)).action)
    }

    @Test
    fun `holds the logins, clears locked and offers Remember once after the unlock`() {
        val start = onStatusLocked(initialUnlockState(), true)
        val prompted = onNeedsPassword(start, need(start)).state
        val step = onUnlockResult(prompted, UnlockResult(listOf("user")))
        assertEquals(RoundOutcome.DONE, step.outcome)
        assertTrue(step.offerRemember)
        assertEquals(listOf("user"), step.state.unlocked)
        assertFalse(step.state.locked)
        assertFalse(step.state.promptOpen)
        assertEquals(1, step.state.unlocks)
        assertFalse(onUnlockResult(step.state, UnlockResult(listOf("user"))).offerRemember)
    }

    @Test
    fun `closes the prompt when the round is aborted`() {
        val start = initialUnlockState()
        val aborted = onRoundAborted(onNeedsPassword(start, need(start)).state)
        assertFalse(aborted.promptOpen)
        assertEquals(0, aborted.dismissals)
    }

    // ------------------------------------------------------------------ notDoneText

    @Test
    fun `says what did not happen and how to go on after an Escape`() {
        val start = initialUnlockState()
        val waiting = need(start)
        val dismissed = onDismiss(onNeedsPassword(start, waiting).state)
        assertEquals("Not done: no password was given. The next action that needs the database asks again.", NOT_DONE_NO_PASSWORD)
        // The request that opened the prompt, and one that started before the Escape without a round.
        assertEquals(NOT_DONE_NO_PASSWORD, notDoneText(dismissed, waiting, RoundEnd.DISMISSED))
        assertEquals(NOT_DONE_NO_PASSWORD, notDoneText(dismissed, waiting, null))
    }

    @Test
    fun `says that the passwords were refused after a round gave up`() {
        val start = initialUnlockState()
        assertEquals(
            "Not done: the password was refused 3 times. The next action that needs the database asks again.",
            NOT_DONE_REFUSED,
        )
        assertEquals(NOT_DONE_REFUSED, notDoneText(start, need(start), RoundEnd.REFUSED))
    }

    @Test
    fun `keeps the bridge message for background requests and other failures`() {
        val start = initialUnlockState()
        val dismissed = onDismiss(onNeedsPassword(start, need(start)).state)
        assertNull(notDoneText(dismissed, need(start, interactive = false), null))
        assertNull(notDoneText(start, need(start), RoundEnd.ABORTED))
        assertNull(notDoneText(start, need(start, retries = MAX_RETRIES_PER_REQUEST), null))
    }

    // ------------------------------------------------------------------ onExplicitUnlock

    @Test
    fun `explicit unlock prompts after an Escape, joins an open prompt, and asks nothing while a login is held`() {
        val start = initialUnlockState()
        val dismissed = onDismiss(onNeedsPassword(start, need(start)).state)
        val explicit = onExplicitUnlock(dismissed)
        assertEquals(ExplicitAction.PROMPT, explicit.action)
        assertTrue(explicit.state.promptOpen)
        assertEquals(ExplicitAction.JOIN, onExplicitUnlock(explicit.state).action)
        assertEquals(ExplicitAction.NONE, onExplicitUnlock(unlockedState()).action)
    }

    // ------------------------------------------------------------------ stored password

    @Test
    fun `a stored password is sent at the start only if one is stored`() {
        assertTrue(onStoredPassword("secret"))
        assertFalse(onStoredPassword(null))
        assertFalse(onStoredPassword(""))
    }

    @Test
    fun `a stored password unlocks without offering Remember again`() {
        val step = onStoredResult(initialUnlockState(), UnlockResult(listOf("user")), refused = false)
        assertFalse(step.forget)
        assertEquals(listOf("user"), step.state.unlocked)
        assertFalse(step.state.locked)
        assertTrue(step.state.rememberSettled)
        val prompted = step.state.copy(unlocked = emptyList(), promptOpen = true)
        assertFalse(onUnlockResult(prompted, UnlockResult(listOf("user"))).offerRemember)
    }

    @Test
    fun `a refused stored password is forgotten, and the next interactive request asks and offers again`() {
        val step = onStoredResult(initialUnlockState(), null, refused = true)
        assertTrue(step.forget)
        assertTrue(step.state.locked)
        val asked = onNeedsPassword(step.state, need(step.state))
        assertEquals(NeedAction.PROMPT, asked.action)
        assertTrue(onUnlockResult(asked.state, UnlockResult(listOf("user"))).offerRemember)
    }

    @Test
    fun `a stored password is kept when the unlock failed for another reason`() {
        val start = initialUnlockState()
        val step = onStoredResult(start, null, refused = false)
        assertFalse(step.forget)
        assertSame(start, step.state)
    }

    // ------------------------------------------------------------------ texts and guards

    @Test
    fun `builds the prompt texts`() {
        assertEquals("calu user password (asked once)", promptText(null, null))
        assertEquals("calu user password (asked once)", promptText("", 2))
        assertEquals(
            "No password was typed, and this login has one. (2 tries left) — calu user password (asked once)",
            promptText("no password was typed, and this login has one.", 2),
        )
        assertEquals(
            "No password was typed, and this login has one. — calu user password (asked once)",
            promptText("no password was typed, and this login has one.", null),
        )
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
            listOf("user"),
            unlockedLogins(CaluJson.parseToJsonElement("""{"needs":"user","unlocked":["user",3,""]}""")),
        )
        assertEquals(emptyList<String>(), unlockedLogins(CaluJson.parseToJsonElement("""{"needs":"user"}""")))
        assertEquals(emptyList<String>(), unlockedLogins(JsonNull))
        assertEquals(emptyList<String>(), unlockedLogins(null))
        assertEquals(emptyList<String>(), unlockedLogins(CaluJson.parseToJsonElement("""{"unlocked":"user"}""")))
    }
}
