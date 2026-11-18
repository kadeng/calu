/**
 * Decisions of the "one password, asked once" flow (docs/notes/vscode-extension-spec.md,
 * section 8). Pure: no IntelliJ Platform import, no UI, no I/O. The bridge code of the plugin
 * connects these functions to the password dialog, to the IDE's password store and to the bridge
 * request `ide.unlock`.
 *
 *     bridge process starts
 *       stored password? ── onStoredPassword ──► ide.unlock ── onStoredResult ──► forget it if refused
 *
 *     a request answers error -32010 ── onNeedsPassword ──►
 *       FAIL          background request, or a request that started before the user pressed
 *                     Escape, or nothing more can be asked: the caller gets the error
 *       RETRY         an unlock succeeded after the request was sent: send it again
 *       JOIN          a prompt is open: wait for it, then send the request again
 *       PROMPT_FIRST  open the prompt for the one password
 *       PROMPT_ADMIN  a login is held, but it may not change settings: ask the admin password
 *
 *     prompt ── Escape ───────────────────────► onDismiss        (waiting requests fail)
 *            ── password ──► ide.unlock ── ok ─► onUnlockResult  DONE | ASK_AGAIN | GIVE_UP
 *                                       ── refused ─► onUnlockRefused   ASK_AGAIN | GIVE_UP
 *                                       ── other error ─► onRoundAborted
 *
 * One `UnlockState` belongs to one bridge process. All functions return a new state and leave
 * their argument unchanged. No function receives or stores a password.
 *
 * Ported from `extensions/vscode/src/shared/unlock.ts`.
 */
package io.github.kadeng.calu.logic

/** Prompt kind (`UnlockState.promptOpen`, parameter `kind`): the prompt asks the one password. */
const val PROMPT_FIRST: String = "first"

/** Prompt kind (`UnlockState.promptOpen`, parameter `kind`): the prompt asks the admin password for changes. */
const val PROMPT_ADMIN: String = "admin"

/** How many passwords may be entered in one prompt round before the round gives up. */
const val MAX_ATTEMPTS_PER_ROUND: Int = 3

/** How often one request may be sent again after error -32010 before it fails. */
const val MAX_RETRIES_PER_REQUEST: Int = 3

/** Title of the password input dialog. */
const val PROMPT_TITLE: String = "calu: password"

/**
 * What the plugin knows about the logins of one bridge process.
 *
 * @property unlocked Names of the logins the bridge holds, as far as the plugin knows.
 * @property changing True if the bridge holds a login that may change settings (no admin prompt
 *   is needed).
 * @property locked True if a protected credential exists and no login is held (shown as "locked").
 * @property promptOpen The kind of the prompt that is open now (`PROMPT_FIRST` or `PROMPT_ADMIN`);
 *   `null` if none is open.
 * @property dismissals How often the user dismissed a prompt with Escape. Requests remember it at
 *   their start.
 * @property unlocks How many `ide.unlock` requests succeeded. Requests remember it when they are
 *   sent.
 * @property rememberSettled True once "Remember on this machine" needs no (further) offer in this
 *   bridge process.
 */
data class UnlockState(
    val unlocked: List<String>,
    val changing: Boolean,
    val locked: Boolean,
    val promptOpen: String?,
    val dismissals: Int,
    val unlocks: Int,
    val rememberSettled: Boolean,
)

/**
 * The members of an `ide.unlock` result that the decisions use.
 *
 * @property unlocked Names of all logins the bridge holds now.
 * @property changing True if a request that changes settings now finds a login.
 */
data class UnlockResult(val unlocked: List<String>, val changing: Boolean)

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

    /** Open the prompt for the one password. */
    PROMPT_FIRST,

    /** Open the prompt for the admin password. */
    PROMPT_ADMIN,
}

/** What the explicit "Unlock" action does (see `onExplicitUnlock`). */
enum class ExplicitAction {
    /** A login that may change settings is held: nothing is left to unlock. */
    NONE,

    /** A prompt is open: wait for it. */
    JOIN,

    /** Open the prompt for the one password. */
    PROMPT_FIRST,

    /** Open the prompt for the admin password. */
    PROMPT_ADMIN,
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
 * @property offerRemember True if "Remember on this machine" is to be offered now (at most once
 *   per state).
 * @property state The new state.
 */
data class RoundStep(
    val outcome: RoundOutcome,
    val problem: String?,
    val offerRemember: Boolean,
    val state: UnlockState,
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
    changing = false,
    locked = false,
    promptOpen = null,
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
 *     no login is held                                 -> PROMPT_FIRST
 *     a login is held that may not change settings     -> PROMPT_ADMIN
 *     a login is held that may change settings         -> FAIL   (no third password)
 *
 * "Escape stops asking until the user starts an action that needs it" is implemented by the
 * second rule: a request that starts after the dismissal has `startedAtDismissals ==
 * state.dismissals` and may open a prompt again.
 *
 * @param state The state of the bridge process.
 * @param need The request's facts.
 * @return The action and the new state. With `PROMPT_FIRST`/`PROMPT_ADMIN` the new state has
 *   `promptOpen` set: the caller must open the prompt and later call `onDismiss`,
 *   `onUnlockResult`, `onUnlockRefused` or `onRoundAborted`. `locked` becomes true if no login
 *   is held.
 */
fun onNeedsPassword(state: UnlockState, need: PasswordNeed): NeedDecision {
    val holds = state.unlocked.isNotEmpty() || need.unlockedReported.isNotEmpty()
    val next = if (holds) state else state.copy(locked = true)
    if (!need.interactive) return NeedDecision(NeedAction.FAIL, next)
    if (need.startedAtDismissals < state.dismissals) return NeedDecision(NeedAction.FAIL, next)
    if (need.retries >= MAX_RETRIES_PER_REQUEST) return NeedDecision(NeedAction.FAIL, next)
    if (need.sentAtUnlocks < state.unlocks) return NeedDecision(NeedAction.RETRY, next)
    if (!state.promptOpen.isNullOrEmpty()) return NeedDecision(NeedAction.JOIN, next)
    if (!holds) return NeedDecision(NeedAction.PROMPT_FIRST, next.copy(promptOpen = PROMPT_FIRST))
    if (state.changing) return NeedDecision(NeedAction.FAIL, next)
    return NeedDecision(NeedAction.PROMPT_ADMIN, next.copy(promptOpen = PROMPT_ADMIN))
}

/**
 * Decide what the explicit "Unlock" action does (the chat's button, the action "Unlock").
 * The action ignores an earlier Escape.
 *
 * @param state The state of the bridge process.
 * @return `NONE` if a login that may change settings is held (nothing is left to unlock);
 *   `JOIN` if a prompt is open; else the prompt to open, with `promptOpen` set in the new state
 *   (the caller's duties are those of `onNeedsPassword`).
 */
fun onExplicitUnlock(state: UnlockState): ExplicitDecision {
    if (!state.promptOpen.isNullOrEmpty()) return ExplicitDecision(ExplicitAction.JOIN, state)
    if (state.unlocked.isEmpty()) {
        return ExplicitDecision(ExplicitAction.PROMPT_FIRST, state.copy(promptOpen = PROMPT_FIRST))
    }
    if (state.changing) return ExplicitDecision(ExplicitAction.NONE, state)
    return ExplicitDecision(ExplicitAction.PROMPT_ADMIN, state.copy(promptOpen = PROMPT_ADMIN))
}

/**
 * Record that the user dismissed the open prompt with Escape.
 *
 * @param state The state with an open prompt.
 * @return The new state: no prompt open, `dismissals` one higher (so that every request
 *   started before now fails without a prompt). Dismissing the first prompt sets `locked`;
 *   dismissing the admin prompt leaves `locked` unchanged, because a login is held.
 */
fun onDismiss(state: UnlockState): UnlockState = state.copy(
    promptOpen = null,
    dismissals = state.dismissals + 1,
    locked = if (state.promptOpen == PROMPT_FIRST) true else state.locked,
)

/**
 * Record that a prompt round ended without an answer about the password (the bridge exited,
 * the database is unreachable, the input dialog failed).
 *
 * @param state The state with an open prompt.
 * @return The new state: no prompt open. Later requests may prompt again.
 */
fun onRoundAborted(state: UnlockState): UnlockState = state.copy(promptOpen = null)

/**
 * Record a successful `ide.unlock` of a prompt round.
 *
 * @param state The state with the open prompt of kind `kind`.
 * @param kind The kind of the open prompt: `PROMPT_FIRST` or `PROMPT_ADMIN`.
 * @param result The answer of `ide.unlock`.
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @return `DONE`, except for an admin prompt whose password still does not allow changing
 *   settings: then `ASK_AGAIN` with a `problem` text, or `GIVE_UP` after
 *   `MAX_ATTEMPTS_PER_ROUND` entries. In every case the new state holds `result.unlocked`, is
 *   not locked and has `unlocks` one higher. `offerRemember` is true for the first successful
 *   first-kind unlock of the state; `promptOpen` is cleared unless the outcome is `ASK_AGAIN`.
 */
fun onUnlockResult(state: UnlockState, kind: String, result: UnlockResult, attempt: Int): RoundStep {
    val offerRemember = kind == PROMPT_FIRST && !state.rememberSettled
    val held = state.copy(
        unlocked = result.unlocked.toList(),
        changing = result.changing,
        locked = false,
        unlocks = state.unlocks + 1,
        rememberSettled = state.rememberSettled || offerRemember,
    )
    if (kind == PROMPT_FIRST || result.changing) {
        return RoundStep(RoundOutcome.DONE, null, offerRemember, held.copy(promptOpen = null))
    }
    if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
        return RoundStep(RoundOutcome.GIVE_UP, null, offerRemember, held.copy(promptOpen = null))
    }
    return RoundStep(RoundOutcome.ASK_AGAIN, "This password does not allow changing settings", offerRemember, held)
}

/**
 * Record that `ide.unlock` refused the password of a prompt round.
 *
 * @param state The state with an open prompt.
 * @param message The bridge's error message (shown with the next prompt).
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @return `ASK_AGAIN` with `problem = message` (the prompt stays open), or `GIVE_UP` after
 *   `MAX_ATTEMPTS_PER_ROUND` entries (the prompt is closed; a later new request may prompt
 *   again). `offerRemember` is always false.
 */
fun onUnlockRefused(state: UnlockState, message: String, attempt: Int): RoundStep {
    if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
        return RoundStep(RoundOutcome.GIVE_UP, null, false, state.copy(promptOpen = null))
    }
    return RoundStep(RoundOutcome.ASK_AGAIN, message, false, state)
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
                changing = result.changing,
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
 * Build the text of the password prompt.
 *
 * Examples:
 *
 *     promptText("first", null)  -> "calu password (asked once)"
 *     promptText("admin", null)  -> "calu admin password, needed to change settings (asked once)"
 *     promptText("first", "wrong password for the 'admin' credential")
 *         -> "Wrong password for the 'admin' credential — calu password (asked once)"
 *
 * @param kind Which password is asked: `PROMPT_ADMIN` for the admin password; every other value
 *   counts as `PROMPT_FIRST`.
 * @param problem Why the previous entry was not accepted; `null` or "" for the first entry.
 * @return The prompt text; a problem is put in front with its first letter in upper case.
 */
fun promptText(kind: String, problem: String?): String {
    val ask = if (kind == PROMPT_ADMIN) {
        "calu admin password, needed to change settings (asked once)"
    } else {
        "calu password (asked once)"
    }
    if (problem.isNullOrEmpty()) return ask
    return "${problem.substring(0, 1).uppercase()}${problem.substring(1)} — $ask"
}
