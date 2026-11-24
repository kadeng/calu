/**
 * Decisions of the "one password, asked once" flow (agent/records/notes/vscode-extension-spec.md,
 * section 8). calu has one login for everything an editor does, the "user" login, protected by
 * the calu user password (D265). Pure: no IntelliJ Platform import, no UI, no I/O. The bridge
 * code of the plugin connects these functions to the password dialog, to the IDE's password
 * store and to the bridge request `ide.unlock`.
 *
 *     bridge process starts
 *       stored password? ── onStoredPassword ──► ide.unlock ── onStoredResult ──► forget it if refused
 *
 *     a request answers error -32010 ── onNeedsPassword ──►
 *       FAIL    background request, or a request that started before the user pressed
 *               Escape, or a login is held already: the caller gets the error
 *       RETRY   an unlock succeeded after the request was sent: send it again
 *       JOIN    a prompt is open: wait for it, then send the request again
 *       PROMPT  open the prompt for the calu user password
 *
 *     prompt ── Escape ───────────────────────► onDismiss        (waiting requests fail)
 *            ── password ──► ide.unlock ── ok ─► onUnlockResult  DONE
 *                                       ── refused ─► onUnlockRefused   ASK_AGAIN | GIVE_UP
 *                                       ── other error ─► onRoundAborted
 *
 *     a request that failed without a password ── notDoneText ──► the message its caller shows
 *
 * One `UnlockState` belongs to one bridge process. All functions return a new state and leave
 * their argument unchanged. No function receives or stores a password.
 *
 * Ported from `extensions/vscode/src/shared/unlock.ts`.
 */
package io.github.kadeng.calu.logic

/** How many passwords may be entered in one prompt round before the round gives up. */
const val MAX_ATTEMPTS_PER_ROUND: Int = 3

/** How often one request may be sent again after error -32010 before it fails. */
const val MAX_RETRIES_PER_REQUEST: Int = 3

/** Title of the password input dialog. */
const val PROMPT_TITLE: String = "calu: password"

/** The prompt's question, without a problem in front (see `promptText`). */
const val PROMPT_ASK: String = "calu user password (asked once)"

/** Message of a request that failed because the user dismissed the prompt (Escape). */
const val NOT_DONE_NO_PASSWORD: String =
    "Not done: no password was given. The next action that needs the database asks again."

/** Message of a request that failed because every password of its prompt round was refused. */
const val NOT_DONE_REFUSED: String =
    "Not done: the password was refused $MAX_ATTEMPTS_PER_ROUND times. The next action that needs the database asks again."

/**
 * What the plugin knows about the logins of one bridge process.
 *
 * @property unlocked Names of the logins the bridge holds, as far as the plugin knows.
 * @property locked True if the user login has a password and the bridge holds no login (shown
 *   as "locked").
 * @property promptOpen True while the password prompt is open.
 * @property dismissals How often the user dismissed a prompt with Escape. Requests remember it at
 *   their start.
 * @property unlocks How many `ide.unlock` requests succeeded. Requests remember it when they are
 *   sent.
 * @property rememberSettled True once "Remember on this machine" needs no (further) offer in this
 *   bridge process.
 */
data class UnlockState(
    val unlocked: List<String>,
    val locked: Boolean,
    val promptOpen: Boolean,
    val dismissals: Int,
    val unlocks: Int,
    val rememberSettled: Boolean,
)

/**
 * The member of an `ide.unlock` result that the decisions use.
 *
 * @property unlocked Names of all logins the bridge holds now.
 */
data class UnlockResult(val unlocked: List<String>)

/**
 * One request that was answered with error -32010.
 *
 * @property interactive False for requests the user did not start (start-up, timers).
 * @property unlockedReported `data.unlocked` of the error: the logins the bridge held when it
 *   answered.
 * @property startedAtDismissals `UnlockState.dismissals` at the moment the request was started by
 *   its caller.
 * @property sentAtUnlocks `UnlockState.unlocks` at the moment this attempt of the request was sent.
 * @property retries How often this request was already sent again after error -32010.
 */
data class PasswordNeed(
    val interactive: Boolean,
    val unlockedReported: List<String>,
    val startedAtDismissals: Int,
    val sentAtUnlocks: Int,
    val retries: Int,
)

/** What to do with a request that was answered with error -32010 (see the file comment). */
enum class NeedAction {
    /** The caller of the request gets the error; no prompt is opened. */
    FAIL,

    /** An unlock succeeded after the request was sent: send the request again. */
    RETRY,

    /** A prompt is open: wait for it, then send the request again. */
    JOIN,

    /** Open the prompt for the calu user password. */
    PROMPT,
}

/** What the explicit "Unlock" action does (see `onExplicitUnlock`). */
enum class ExplicitAction {
    /** A login is held: nothing is left to unlock. */
    NONE,

    /** A prompt is open: wait for it. */
    JOIN,

    /** Open the prompt for the calu user password. */
    PROMPT,
}

/** What to do after one answer of `ide.unlock` inside a prompt round. */
enum class RoundOutcome {
    /** Retry the waiting requests. */
    DONE,

    /** Show the prompt again. */
    ASK_AGAIN,

    /** Fail the waiting requests. */
    GIVE_UP,
}

/** How a prompt round ended. */
enum class RoundEnd {
    /** A password was accepted. */
    UNLOCKED,

    /** The user dismissed the prompt (Escape, a closed dialog). */
    DISMISSED,

    /** `MAX_ATTEMPTS_PER_ROUND` passwords were refused. */
    REFUSED,

    /** The bridge ended, the database was unreachable, or the prompt failed. */
    ABORTED,
}

/**
 * Result of `onNeedsPassword`.
 *
 * @property action What to do with the request.
 * @property state The new state.
 */
data class NeedDecision(val action: NeedAction, val state: UnlockState)

/**
 * Result of `onExplicitUnlock`.
 *
 * @property action What the "Unlock" action does.
 * @property state The new state.
 */
data class ExplicitDecision(val action: ExplicitAction, val state: UnlockState)

/**
 * Result of `onUnlockResult` and `onUnlockRefused`.
 *
 * @property outcome `DONE`: retry the waiting requests. `ASK_AGAIN`: show the prompt again.
 *   `GIVE_UP`: fail them.
 * @property problem Text to show with the next prompt (`ASK_AGAIN` only); `null` otherwise.
 * @property left How many entries the round still allows (`ASK_AGAIN` only, >= 1); `null`
 *   otherwise.
 * @property offerRemember True if "Remember on this machine" is to be offered now (at most once
 *   per state).
 * @property state The new state.
 */
data class RoundStep(
    val outcome: RoundOutcome,
    val problem: String?,
    val offerRemember: Boolean,
    val state: UnlockState,
    val left: Int? = null,
)

/**
 * Result of `onStoredResult`.
 *
 * @property forget True if the caller must remove the stored password from the password store.
 * @property state The new state.
 */
data class StoredDecision(val forget: Boolean, val state: UnlockState)

/**
 * Create the state of a new bridge process: nothing held, nothing asked, not locked.
 *
 * @return A fresh state.
 */
fun initialUnlockState(): UnlockState = UnlockState(
    unlocked = emptyList(),
    locked = false,
    promptOpen = false,
    dismissals = 0,
    unlocks = 0,
    rememberSettled = false,
)

/**
 * Decide what happens to a request that the bridge answered with error -32010.
 *
 * Rules, in this order:
 *
 *     background request                               -> FAIL   (never prompts)
 *     request started before the last Escape           -> FAIL   (Escape stops the asking)
 *     request was already sent again MAX_RETRIES times -> FAIL   (no endless loop)
 *     an unlock succeeded after the attempt was sent   -> RETRY  (the answer is out of date)
 *     a prompt is open                                 -> JOIN
 *     no login is held                                 -> PROMPT
 *     a login is held                                  -> FAIL   (no second password)
 *
 * "Escape stops asking until the user starts an action that needs it" is implemented by the
 * second rule: a request that starts after the dismissal has `startedAtDismissals ==
 * state.dismissals` and may open a prompt again.
 *
 * @param state The state of the bridge process.
 * @param need The request's facts.
 * @return The action and the new state. With `PROMPT` the new state has `promptOpen` set: the
 *   caller must open the prompt and later call `onDismiss`, `onUnlockResult`, `onUnlockRefused`
 *   or `onRoundAborted`. `locked` becomes true if no login is held.
 */
fun onNeedsPassword(state: UnlockState, need: PasswordNeed): NeedDecision {
    val holds = state.unlocked.isNotEmpty() || need.unlockedReported.isNotEmpty()
    val next = if (holds) state else state.copy(locked = true)
    if (!need.interactive) return NeedDecision(NeedAction.FAIL, next)
    if (need.startedAtDismissals < state.dismissals) return NeedDecision(NeedAction.FAIL, next)
    if (need.retries >= MAX_RETRIES_PER_REQUEST) return NeedDecision(NeedAction.FAIL, next)
    if (need.sentAtUnlocks < state.unlocks) return NeedDecision(NeedAction.RETRY, next)
    if (state.promptOpen) return NeedDecision(NeedAction.JOIN, next)
    if (!holds) return NeedDecision(NeedAction.PROMPT, next.copy(promptOpen = true))
    return NeedDecision(NeedAction.FAIL, next)
}

/**
 * Decide what the explicit "Unlock" action does (the chat's button, the action "Unlock").
 * The action ignores an earlier Escape.
 *
 * @param state The state of the bridge process.
 * @return `NONE` if a login is held (nothing is left to unlock); `JOIN` if a prompt is open;
 *   else `PROMPT`, with `promptOpen` set in the new state (the caller's duties are those of
 *   `onNeedsPassword`).
 */
fun onExplicitUnlock(state: UnlockState): ExplicitDecision {
    if (state.promptOpen) return ExplicitDecision(ExplicitAction.JOIN, state)
    if (state.unlocked.isNotEmpty()) return ExplicitDecision(ExplicitAction.NONE, state)
    return ExplicitDecision(ExplicitAction.PROMPT, state.copy(promptOpen = true))
}

/**
 * Record that the user dismissed the open prompt with Escape.
 *
 * @param state The state with an open prompt.
 * @return The new state: no prompt open, `dismissals` one higher (so that every request
 *   started before now fails without a prompt), `locked` true (a prompt is only opened while
 *   no login is held).
 */
fun onDismiss(state: UnlockState): UnlockState =
    state.copy(promptOpen = false, dismissals = state.dismissals + 1, locked = true)

/**
 * Record that a prompt round ended without an answer about the password (the bridge exited,
 * the database is unreachable, the input dialog failed).
 *
 * @param state The state with an open prompt.
 * @return The new state: no prompt open. Later requests may prompt again.
 */
fun onRoundAborted(state: UnlockState): UnlockState = state.copy(promptOpen = false)

/**
 * Record a successful `ide.unlock` of a prompt round.
 *
 * @param state The state with the open prompt.
 * @param result The answer of `ide.unlock`.
 * @return Always `DONE`. The new state holds `result.unlocked`, is not locked, has no prompt
 *   open and has `unlocks` one higher. `offerRemember` is true unless an earlier unlock of the
 *   state (by a prompt or by the stored password) settled it.
 */
fun onUnlockResult(state: UnlockState, result: UnlockResult): RoundStep {
    val offerRemember = !state.rememberSettled
    val held = state.copy(
        unlocked = result.unlocked.toList(),
        locked = false,
        promptOpen = false,
        unlocks = state.unlocks + 1,
        rememberSettled = true,
    )
    return RoundStep(RoundOutcome.DONE, null, offerRemember, held)
}

/**
 * Record that `ide.unlock` refused the password of a prompt round.
 *
 * @param state The state with an open prompt.
 * @param message The bridge's error message (shown with the next prompt).
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @return `ASK_AGAIN` with `problem = message` and `left = MAX_ATTEMPTS_PER_ROUND - attempt`
 *   (the prompt stays open), or `GIVE_UP` after `MAX_ATTEMPTS_PER_ROUND` entries (the prompt
 *   is closed; a later new request may prompt again). `offerRemember` is always false.
 */
fun onUnlockRefused(state: UnlockState, message: String, attempt: Int): RoundStep {
    if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
        return RoundStep(RoundOutcome.GIVE_UP, null, false, state.copy(promptOpen = false))
    }
    return RoundStep(RoundOutcome.ASK_AGAIN, message, false, state, left = MAX_ATTEMPTS_PER_ROUND - attempt)
}

/**
 * Decide whether a stored password is sent with `ide.unlock` right after the bridge started.
 *
 * @param stored The password read from the password store, or `null`. Only its presence is
 *   inspected; it is neither kept nor logged.
 * @return True ("unlock") if a non-empty password is stored, else false ("skip").
 */
fun onStoredPassword(stored: String?): Boolean = !stored.isNullOrEmpty()

/**
 * Record the answer to the `ide.unlock` that carried the stored password.
 *
 * @param state The state of the new bridge process.
 * @param result The result of `ide.unlock`, or `null` if the request failed with an error.
 * @param refused Used only if `result` is `null`: true only if the bridge said that the password
 *   is wrong (error -32010).
 * @return `forget` is true exactly if the password was refused: the caller removes it from the
 *   password store, and the next interactive request asks the user. On success the state holds
 *   the logins, is not locked and never offers "Remember" (the password is stored already). On
 *   a refusal the state is locked. On another error (e.g. database unreachable) the state is
 *   unchanged (the same object) and the stored password is kept.
 */
fun onStoredResult(state: UnlockState, result: UnlockResult?, refused: Boolean): StoredDecision {
    if (result != null) {
        return StoredDecision(
            forget = false,
            state = state.copy(
                unlocked = result.unlocked.toList(),
                locked = false,
                unlocks = state.unlocks + 1,
                rememberSettled = true,
            ),
        )
    }
    if (!refused) return StoredDecision(forget = false, state = state)
    return StoredDecision(forget = true, state = state.copy(locked = true))
}

/**
 * Record the bridge's own report (`ide.status.locked`).
 *
 * @param state The state of the bridge process.
 * @param locked The reported value (a missing value counts as false at the caller).
 * @return The state with `locked` replaced; the same object if nothing changes.
 */
fun onStatusLocked(state: UnlockState, locked: Boolean): UnlockState =
    if (state.locked == locked) state else state.copy(locked = locked)

/**
 * Choose the message of a request whose error -32010 no password resolved, so that the user
 * reads what did not happen and how to go on instead of the bridge's request for a password.
 *
 * Rules, in this order:
 *
 *     background request                                 -> null (the bridge's message)
 *     the round the request waited for ended REFUSED     -> NOT_DONE_REFUSED
 *     the round ended DISMISSED, or the user pressed
 *       Escape after the request started                 -> NOT_DONE_NO_PASSWORD
 *     otherwise                                          -> null (the bridge's message)
 *
 * @param state The state after the request's failure was decided.
 * @param need The request's facts.
 * @param end How the prompt round the request opened or joined ended; `null` if it failed
 *   without one (`onNeedsPassword` said `FAIL`).
 * @return The message to fail the request with, or `null` to keep the bridge's error.
 */
fun notDoneText(state: UnlockState, need: PasswordNeed, end: RoundEnd?): String? {
    if (!need.interactive) return null
    if (end == RoundEnd.REFUSED) return NOT_DONE_REFUSED
    if (end == RoundEnd.DISMISSED || state.dismissals > need.startedAtDismissals) return NOT_DONE_NO_PASSWORD
    return null
}

/**
 * Build the text of the password prompt.
 *
 * Examples:
 *
 *     promptText(null, null)  -> "calu user password (asked once)"
 *     promptText("that is not the calu user password.", 2)
 *         -> "That is not the calu user password. (2 tries left) — calu user password (asked once)"
 *     promptText("that is not the calu user password.", 1)
 *         -> "That is not the calu user password. (1 try left) — calu user password (asked once)"
 *
 * @param problem Why the previous entry was not accepted (the bridge's message); `null` or ""
 *   for the first entry.
 * @param left How many entries the round still allows (`RoundStep.left`); `null` to say nothing
 *   about it. Used only together with a problem.
 * @return `PROMPT_ASK`; a problem is put in front with its first letter in upper case, followed
 *   by the tries left.
 */
fun promptText(problem: String?, left: Int?): String {
    if (problem.isNullOrEmpty()) return PROMPT_ASK
    val tries = if (left == null) "" else " ($left ${if (left == 1) "try" else "tries"} left)"
    return "${problem.substring(0, 1).uppercase()}${problem.substring(1)}$tries — $PROMPT_ASK"
}
