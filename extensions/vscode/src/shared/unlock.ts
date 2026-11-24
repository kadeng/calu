/**
 * Decisions of the "one password, asked once" flow (agent/records/notes/vscode-extension-spec.md,
 * section 8). calu has one login for everything an editor does, the "user" login, protected by
 * the calu user password (D265). Pure: no `vscode` import, no DOM, no I/O. `host/bridgeCore.ts`
 * connects these functions to the prompt, to the password store and to the bridge request
 * `ide.unlock`.
 *
 *     bridge process starts
 *       stored password? ── onStoredPassword ──► ide.unlock ── onStoredResult ──► forget it if refused
 *
 *     a request answers error -32010 ── onNeedsPassword ──►
 *       "fail"    background request, or a request that started before the user pressed
 *                 Escape, or a login is held already: the caller gets the error
 *       "retry"   an unlock succeeded after the request was sent: send it again
 *       "join"    a prompt is open: wait for it, then send the request again
 *       "prompt"  open the prompt for the calu user password
 *
 *     prompt ── Escape ───────────────────────► onDismiss        (waiting requests fail)
 *            ── password ──► ide.unlock ── ok ─► onUnlockResult  "done"
 *                                       ── refused ─► onUnlockRefused   "ask-again" | "give-up"
 *                                       ── other error ─► onRoundAborted
 *
 *     a request that failed without a password ── notDoneText ──► the message its caller shows
 *
 * One `UnlockState` belongs to one bridge process. All functions return a new state and leave
 * their argument unchanged. No function receives or stores a password.
 */

/** How many passwords may be entered in one prompt round before the round gives up. */
export const MAX_ATTEMPTS_PER_ROUND = 3;

/** How often one request may be sent again after error -32010 before it fails. */
export const MAX_RETRIES_PER_REQUEST = 3;

/** Title of the password input box. */
export const PROMPT_TITLE = 'calu: password';

/** The prompt's question, without a problem in front (see `promptText`). */
export const PROMPT_ASK = 'calu user password (asked once)';

/** Message of a request that failed because the user dismissed the prompt (Escape). */
export const NOT_DONE_NO_PASSWORD =
  'Not done: no password was given. The next action that needs the database asks again.';

/** Message of a request that failed because every password of its prompt round was refused. */
export const NOT_DONE_REFUSED =
  `Not done: the password was refused ${MAX_ATTEMPTS_PER_ROUND} times. ` +
  'The next action that needs the database asks again.';

/** What the extension knows about the logins of one bridge process. */
export interface UnlockState {
  /** Names of the logins the bridge holds, as far as the extension knows. */
  unlocked: string[];
  /** True if the user login has a password and the bridge holds no login (shown as "locked"). */
  locked: boolean;
  /** True while the password prompt is open. */
  promptOpen: boolean;
  /** How often the user dismissed a prompt with Escape. Requests remember it at their start. */
  dismissals: number;
  /** How many `ide.unlock` requests succeeded. Requests remember it when they are sent. */
  unlocks: number;
  /** True once "Remember on this machine" needs no (further) offer in this bridge process. */
  rememberSettled: boolean;
}

/** The member of an `ide.unlock` result that the decisions use. */
export interface UnlockResult {
  /** Names of all logins the bridge holds now. */
  unlocked: string[];
}

/** One request that was answered with error -32010. */
export interface PasswordNeed {
  /** False for requests the user did not start (start-up, timers). */
  interactive: boolean;
  /** `data.unlocked` of the error: the logins the bridge held when it answered. */
  unlockedReported: string[];
  /** `UnlockState.dismissals` at the moment the request was started by its caller. */
  startedAtDismissals: number;
  /** `UnlockState.unlocks` at the moment this attempt of the request was sent. */
  sentAtUnlocks: number;
  /** How often this request was already sent again after error -32010. */
  retries: number;
}

/** What to do with a request that was answered with error -32010. */
export type NeedAction = 'fail' | 'retry' | 'join' | 'prompt';

/** What to do after one answer of `ide.unlock` inside a prompt round. */
export type RoundOutcome = 'done' | 'ask-again' | 'give-up';

/**
 * How a prompt round ended: "unlocked" (a password was accepted), "dismissed" (Escape),
 * "refused" (`MAX_ATTEMPTS_PER_ROUND` passwords were refused), "aborted" (the bridge ended, the
 * database was unreachable, the prompt failed).
 */
export type RoundEnd = 'unlocked' | 'dismissed' | 'refused' | 'aborted';

/** Result of `onUnlockResult` and `onUnlockRefused`. */
export interface RoundStep {
  /** "done": retry the waiting requests. "ask-again": show the prompt again. "give-up": fail them. */
  outcome: RoundOutcome;
  /** Text to show with the next prompt ("ask-again" only). */
  problem?: string;
  /** How many entries the round still allows ("ask-again" only; >= 1). */
  left?: number;
  /** True if "Remember on this machine" is to be offered now (at most once per state). */
  offerRemember: boolean;
  state: UnlockState;
}

/**
 * Create the state of a new bridge process: nothing held, nothing asked, not locked.
 *
 * @returns A fresh state.
 */
export function initialUnlockState(): UnlockState {
  return {
    unlocked: [],
    locked: false,
    promptOpen: false,
    dismissals: 0,
    unlocks: 0,
    rememberSettled: false,
  };
}

/**
 * Decide what happens to a request that the bridge answered with error -32010.
 *
 * Rules, in this order:
 *
 *     background request                               -> "fail"   (never prompts)
 *     request started before the last Escape           -> "fail"   (Escape stops the asking)
 *     request was already sent again MAX_RETRIES times -> "fail"   (no endless loop)
 *     an unlock succeeded after the attempt was sent   -> "retry"  (the answer is out of date)
 *     a prompt is open                                 -> "join"
 *     no login is held                                 -> "prompt"
 *     a login is held                                  -> "fail"   (no second password)
 *
 * "Escape stops asking until the user starts an action that needs it" is implemented by the
 * second rule: a request that starts after the dismissal has `startedAtDismissals ===
 * state.dismissals` and may open a prompt again.
 *
 * @param state The state of the bridge process.
 * @param need The request's facts.
 * @returns The action and the new state. With "prompt" the new state has `promptOpen` set: the
 *   caller must open the prompt and later call `onDismiss`, `onUnlockResult`, `onUnlockRefused`
 *   or `onRoundAborted`. `locked` becomes true if no login is held.
 */
export function onNeedsPassword(state: UnlockState, need: PasswordNeed): { action: NeedAction; state: UnlockState } {
  const holds = state.unlocked.length > 0 || need.unlockedReported.length > 0;
  const next: UnlockState = holds ? state : { ...state, locked: true };
  if (!need.interactive) return { action: 'fail', state: next };
  if (need.startedAtDismissals < state.dismissals) return { action: 'fail', state: next };
  if (need.retries >= MAX_RETRIES_PER_REQUEST) return { action: 'fail', state: next };
  if (need.sentAtUnlocks < state.unlocks) return { action: 'retry', state: next };
  if (state.promptOpen) return { action: 'join', state: next };
  if (!holds) return { action: 'prompt', state: { ...next, promptOpen: true } };
  return { action: 'fail', state: next };
}

/**
 * Decide what the explicit "Unlock" action does (the chat's button, the command "calu: Unlock").
 * The action ignores an earlier Escape.
 *
 * @param state The state of the bridge process.
 * @returns "none" if a login is held (nothing is left to unlock); "join" if a prompt is open;
 *   else "prompt", with `promptOpen` set in the new state (the caller's duties are those of
 *   `onNeedsPassword`).
 */
export function onExplicitUnlock(state: UnlockState): {
  action: 'none' | 'join' | 'prompt';
  state: UnlockState;
} {
  if (state.promptOpen) return { action: 'join', state };
  if (state.unlocked.length > 0) return { action: 'none', state };
  return { action: 'prompt', state: { ...state, promptOpen: true } };
}

/**
 * Record that the user dismissed the open prompt with Escape.
 *
 * @param state The state with an open prompt.
 * @returns The new state: no prompt open, `dismissals` one higher (so that every request
 *   started before now fails without a prompt), `locked` true (a prompt is only opened while
 *   no login is held).
 */
export function onDismiss(state: UnlockState): UnlockState {
  return { ...state, promptOpen: false, dismissals: state.dismissals + 1, locked: true };
}

/**
 * Record that a prompt round ended without an answer about the password (the bridge exited,
 * the database is unreachable, the input box failed).
 *
 * @param state The state with an open prompt.
 * @returns The new state: no prompt open. Later requests may prompt again.
 */
export function onRoundAborted(state: UnlockState): UnlockState {
  return { ...state, promptOpen: false };
}

/**
 * Record a successful `ide.unlock` of a prompt round.
 *
 * @param state The state with the open prompt.
 * @param result The answer of `ide.unlock`.
 * @returns Always "done". The new state holds `result.unlocked`, is not locked, has no prompt
 *   open and has `unlocks` one higher. `offerRemember` is true unless an earlier unlock of the
 *   state (by a prompt or by the stored password) settled it.
 */
export function onUnlockResult(state: UnlockState, result: UnlockResult): RoundStep {
  const offerRemember = !state.rememberSettled;
  return {
    outcome: 'done',
    offerRemember,
    state: {
      ...state,
      unlocked: [...result.unlocked],
      locked: false,
      promptOpen: false,
      unlocks: state.unlocks + 1,
      rememberSettled: true,
    },
  };
}

/**
 * Record that `ide.unlock` refused the password of a prompt round.
 *
 * @param state The state with an open prompt.
 * @param message The bridge's error message (shown with the next prompt).
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @returns "ask-again" with `problem = message` and `left = MAX_ATTEMPTS_PER_ROUND - attempt`
 *   (the prompt stays open), or "give-up" after `MAX_ATTEMPTS_PER_ROUND` entries (the prompt
 *   is closed; a later new request may prompt again). `offerRemember` is always false.
 */
export function onUnlockRefused(state: UnlockState, message: string, attempt: number): RoundStep {
  if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
    return { outcome: 'give-up', offerRemember: false, state: { ...state, promptOpen: false } };
  }
  return { outcome: 'ask-again', problem: message, left: MAX_ATTEMPTS_PER_ROUND - attempt, offerRemember: false, state };
}

/**
 * Decide whether a stored password is sent with `ide.unlock` right after the bridge started.
 *
 * @param stored The password read from the secret storage, or `undefined`. Only its presence
 *   is inspected.
 * @returns "unlock" if a non-empty password is stored, else "skip".
 */
export function onStoredPassword(stored: string | undefined): 'unlock' | 'skip' {
  return stored ? 'unlock' : 'skip';
}

/**
 * Record the answer to the `ide.unlock` that carried the stored password.
 *
 * @param state The state of the new bridge process.
 * @param answer `{ok: true, result}` for a result; `{ok: false, refused}` for an error, where
 *   `refused` is true only if the bridge said that the password is wrong (error -32010).
 * @returns `forget` is true exactly if the password was refused: the caller removes it from the
 *   secret storage, and the next interactive request asks the user. On success the state holds
 *   the logins, is not locked and never offers "Remember" (the password is stored already). On
 *   a refusal the state is locked. On another error (e.g. database unreachable) the state is
 *   unchanged and the stored password is kept.
 */
export function onStoredResult(
  state: UnlockState,
  answer: { ok: true; result: UnlockResult } | { ok: false; refused: boolean },
): { forget: boolean; state: UnlockState } {
  if (answer.ok) {
    return {
      forget: false,
      state: {
        ...state,
        unlocked: [...answer.result.unlocked],
        locked: false,
        unlocks: state.unlocks + 1,
        rememberSettled: true,
      },
    };
  }
  if (!answer.refused) return { forget: false, state };
  return { forget: true, state: { ...state, locked: true } };
}

/**
 * Record the bridge's own report (`ide.status.locked`).
 *
 * @param state The state of the bridge process.
 * @param locked The reported value (a missing value counts as false at the caller).
 * @returns The state with `locked` replaced; the same object if nothing changes.
 */
export function onStatusLocked(state: UnlockState, locked: boolean): UnlockState {
  return state.locked === locked ? state : { ...state, locked };
}

/**
 * Choose the message of a request whose error -32010 no password resolved, so that the user
 * reads what did not happen and how to go on instead of the bridge's request for a password.
 *
 * Rules, in this order:
 *
 *     background request                                 -> undefined (the bridge's message)
 *     the round the request waited for ended "refused"   -> NOT_DONE_REFUSED
 *     the round ended "dismissed", or the user pressed
 *       Escape after the request started                 -> NOT_DONE_NO_PASSWORD
 *     otherwise                                          -> undefined (the bridge's message)
 *
 * @param state The state after the request's failure was decided.
 * @param need The request's facts.
 * @param end How the prompt round the request opened or joined ended; `undefined` if it
 *   failed without one (`onNeedsPassword` said "fail").
 * @returns The message to fail the request with, or `undefined` to keep the bridge's error.
 */
export function notDoneText(state: UnlockState, need: PasswordNeed, end: RoundEnd | undefined): string | undefined {
  if (!need.interactive) return undefined;
  if (end === 'refused') return NOT_DONE_REFUSED;
  if (end === 'dismissed' || state.dismissals > need.startedAtDismissals) return NOT_DONE_NO_PASSWORD;
  return undefined;
}

/**
 * Build the text of the password prompt.
 *
 * Examples:
 *
 *     promptText(undefined, undefined)  -> "calu user password (asked once)"
 *     promptText("that is not the calu user password.", 2)
 *         -> "That is not the calu user password. (2 tries left) — calu user password (asked once)"
 *     promptText("that is not the calu user password.", 1)
 *         -> "That is not the calu user password. (1 try left) — calu user password (asked once)"
 *
 * @param problem Why the previous entry was not accepted (the bridge's message); `undefined`
 *   or "" for the first entry.
 * @param left How many entries the round still allows (`RoundStep.left`); `undefined` to say
 *   nothing about it. Used only together with a problem.
 * @returns `PROMPT_ASK`; a problem is put in front with its first letter in upper case,
 *   followed by the tries left.
 */
export function promptText(problem: string | undefined, left: number | undefined): string {
  if (!problem) return PROMPT_ASK;
  const tries = left === undefined ? '' : ` (${left} ${left === 1 ? 'try' : 'tries'} left)`;
  return `${problem.charAt(0).toUpperCase()}${problem.slice(1)}${tries} — ${PROMPT_ASK}`;
}
