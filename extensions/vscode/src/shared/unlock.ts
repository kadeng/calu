/**
 * Decisions of the "one password, asked once" flow (docs/notes/vscode-extension-spec.md,
 * section 8). Pure: no `vscode` import, no DOM, no I/O. `src/bridge.ts` connects these functions
 * to the input box, to VSCode's secret storage and to the bridge request `ide.unlock`.
 *
 *     bridge process starts
 *       stored password? ── onStoredPassword ──► ide.unlock ── onStoredResult ──► forget it if refused
 *
 *     a request answers error -32010 ── onNeedsPassword ──►
 *       "fail"          background request, or a request that started before the user pressed
 *                       Escape, or nothing more can be asked: the caller gets the error
 *       "retry"         an unlock succeeded after the request was sent: send it again
 *       "join"          a prompt is open: wait for it, then send the request again
 *       "prompt-first"  open the prompt for the one password
 *       "prompt-admin"  a login is held, but it may not change settings: ask the admin password
 *
 *     prompt ── Escape ───────────────────────► onDismiss        (waiting requests fail)
 *            ── password ──► ide.unlock ── ok ─► onUnlockResult  "done" | "ask-again" | "give-up"
 *                                       ── refused ─► onUnlockRefused   "ask-again" | "give-up"
 *                                       ── other error ─► onRoundAborted
 *
 * One `UnlockState` belongs to one bridge process. All functions return a new state and leave
 * their argument unchanged. No function receives or stores a password.
 */

/** Which password a prompt asks for: the one password, or the admin password for changes. */
export type PromptKind = 'first' | 'admin';

/** How many passwords may be entered in one prompt round before the round gives up. */
export const MAX_ATTEMPTS_PER_ROUND = 3;

/** How often one request may be sent again after error -32010 before it fails. */
export const MAX_RETRIES_PER_REQUEST = 3;

/** Title of the password input box. */
export const PROMPT_TITLE = 'calu: password';

/** What the extension knows about the logins of one bridge process. */
export interface UnlockState {
  /** Names of the logins the bridge holds, as far as the extension knows. */
  unlocked: string[];
  /** True if the bridge holds a login that may change settings (no admin prompt is needed). */
  changing: boolean;
  /** True if a protected credential exists and no login is held (shown as "locked"). */
  locked: boolean;
  /** The kind of the prompt that is open now; `undefined` if none is open. */
  promptOpen: PromptKind | undefined;
  /** How often the user dismissed a prompt with Escape. Requests remember it at their start. */
  dismissals: number;
  /** How many `ide.unlock` requests succeeded. Requests remember it when they are sent. */
  unlocks: number;
  /** True once "Remember on this machine" needs no (further) offer in this bridge process. */
  rememberSettled: boolean;
}

/** The members of an `ide.unlock` result that the decisions use. */
export interface UnlockResult {
  /** Names of all logins the bridge holds now. */
  unlocked: string[];
  /** True if a request that changes settings now finds a login. */
  changing: boolean;
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
export type NeedAction = 'fail' | 'retry' | 'join' | 'prompt-first' | 'prompt-admin';

/** What to do after one answer of `ide.unlock` inside a prompt round. */
export type RoundOutcome = 'done' | 'ask-again' | 'give-up';

/** Result of `onUnlockResult` and `onUnlockRefused`. */
export interface RoundStep {
  /** "done": retry the waiting requests. "ask-again": show the prompt again. "give-up": fail them. */
  outcome: RoundOutcome;
  /** Text to show with the next prompt ("ask-again" only). */
  problem?: string;
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
    changing: false,
    locked: false,
    promptOpen: undefined,
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
 *     no login is held                                 -> "prompt-first"
 *     a login is held that may not change settings     -> "prompt-admin"
 *     a login is held that may change settings         -> "fail"   (no third password)
 *
 * "Escape stops asking until the user starts an action that needs it" is implemented by the
 * second rule: a request that starts after the dismissal has `startedAtDismissals ===
 * state.dismissals` and may open a prompt again.
 *
 * @param state The state of the bridge process.
 * @param need The request's facts.
 * @returns The action and the new state. With "prompt-first"/"prompt-admin" the new state has
 *   `promptOpen` set: the caller must open the prompt and later call `onDismiss`,
 *   `onUnlockResult`, `onUnlockRefused` or `onRoundAborted`. `locked` becomes true if no login
 *   is held.
 */
export function onNeedsPassword(state: UnlockState, need: PasswordNeed): { action: NeedAction; state: UnlockState } {
  const holds = state.unlocked.length > 0 || need.unlockedReported.length > 0;
  const next: UnlockState = holds ? state : { ...state, locked: true };
  if (!need.interactive) return { action: 'fail', state: next };
  if (need.startedAtDismissals < state.dismissals) return { action: 'fail', state: next };
  if (need.retries >= MAX_RETRIES_PER_REQUEST) return { action: 'fail', state: next };
  if (need.sentAtUnlocks < state.unlocks) return { action: 'retry', state: next };
  if (state.promptOpen) return { action: 'join', state: next };
  if (!holds) return { action: 'prompt-first', state: { ...next, promptOpen: 'first' } };
  if (state.changing) return { action: 'fail', state: next };
  return { action: 'prompt-admin', state: { ...next, promptOpen: 'admin' } };
}

/**
 * Decide what the explicit "Unlock" action does (the chat's button, the command "calu: Unlock").
 * The action ignores an earlier Escape.
 *
 * @param state The state of the bridge process.
 * @returns "none" if a login that may change settings is held (nothing is left to unlock);
 *   "join" if a prompt is open; else the prompt to open, with `promptOpen` set in the new state
 *   (the caller's duties are those of `onNeedsPassword`).
 */
export function onExplicitUnlock(state: UnlockState): {
  action: 'none' | 'join' | 'prompt-first' | 'prompt-admin';
  state: UnlockState;
} {
  if (state.promptOpen) return { action: 'join', state };
  if (state.unlocked.length === 0) return { action: 'prompt-first', state: { ...state, promptOpen: 'first' } };
  if (state.changing) return { action: 'none', state };
  return { action: 'prompt-admin', state: { ...state, promptOpen: 'admin' } };
}

/**
 * Record that the user dismissed the open prompt with Escape.
 *
 * @param state The state with an open prompt.
 * @returns The new state: no prompt open, `dismissals` one higher (so that every request
 *   started before now fails without a prompt). Dismissing the first prompt sets `locked`;
 *   dismissing the admin prompt leaves `locked` unchanged, because a login is held.
 */
export function onDismiss(state: UnlockState): UnlockState {
  return {
    ...state,
    promptOpen: undefined,
    dismissals: state.dismissals + 1,
    locked: state.promptOpen === 'first' ? true : state.locked,
  };
}

/**
 * Record that a prompt round ended without an answer about the password (the bridge exited,
 * the database is unreachable, the input box failed).
 *
 * @param state The state with an open prompt.
 * @returns The new state: no prompt open. Later requests may prompt again.
 */
export function onRoundAborted(state: UnlockState): UnlockState {
  return { ...state, promptOpen: undefined };
}

/**
 * Record a successful `ide.unlock` of a prompt round.
 *
 * @param state The state with the open prompt of kind `kind`.
 * @param kind The kind of the open prompt.
 * @param result The answer of `ide.unlock`.
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @returns "done", except for an admin prompt whose password still does not allow changing
 *   settings: then "ask-again" with a `problem` text, or "give-up" after
 *   `MAX_ATTEMPTS_PER_ROUND` entries. In every case the new state holds `result.unlocked`, is
 *   not locked and has `unlocks` one higher. `offerRemember` is true for the first successful
 *   first-kind unlock of the state; `promptOpen` is cleared unless the outcome is "ask-again".
 */
export function onUnlockResult(state: UnlockState, kind: PromptKind, result: UnlockResult, attempt: number): RoundStep {
  const offerRemember = kind === 'first' && !state.rememberSettled;
  const held: UnlockState = {
    ...state,
    unlocked: [...result.unlocked],
    changing: result.changing,
    locked: false,
    unlocks: state.unlocks + 1,
    rememberSettled: state.rememberSettled || offerRemember,
  };
  if (kind === 'first' || result.changing) {
    return { outcome: 'done', offerRemember, state: { ...held, promptOpen: undefined } };
  }
  if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
    return { outcome: 'give-up', offerRemember, state: { ...held, promptOpen: undefined } };
  }
  return { outcome: 'ask-again', problem: 'This password does not allow changing settings', offerRemember, state: held };
}

/**
 * Record that `ide.unlock` refused the password of a prompt round.
 *
 * @param state The state with an open prompt.
 * @param message The bridge's error message (shown with the next prompt).
 * @param attempt Number of passwords entered in this round so far, this one included (>= 1).
 * @returns "ask-again" with `problem = message` (the prompt stays open), or "give-up" after
 *   `MAX_ATTEMPTS_PER_ROUND` entries (the prompt is closed; a later new request may prompt
 *   again). `offerRemember` is always false.
 */
export function onUnlockRefused(state: UnlockState, message: string, attempt: number): RoundStep {
  if (attempt >= MAX_ATTEMPTS_PER_ROUND) {
    return { outcome: 'give-up', offerRemember: false, state: { ...state, promptOpen: undefined } };
  }
  return { outcome: 'ask-again', problem: message, offerRemember: false, state };
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
    const { result } = answer;
    return {
      forget: false,
      state: {
        ...state,
        unlocked: [...result.unlocked],
        changing: result.changing,
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
 * Build the text of the password prompt.
 *
 * Examples:
 *
 *     promptText('first', undefined)  -> "calu password (asked once)"
 *     promptText('admin', undefined)  -> "calu admin password, needed to change settings (asked once)"
 *     promptText('first', "wrong password for the 'admin' credential")
 *         -> "Wrong password for the 'admin' credential — calu password (asked once)"
 *
 * @param kind Which password is asked.
 * @param problem Why the previous entry was not accepted; `undefined` or "" for the first entry.
 * @returns The prompt text; a problem is put in front with its first letter in upper case.
 */
export function promptText(kind: PromptKind, problem: string | undefined): string {
  const ask =
    kind === 'admin' ? 'calu admin password, needed to change settings (asked once)' : 'calu password (asked once)';
  if (!problem) return ask;
  return `${problem.charAt(0).toUpperCase()}${problem.slice(1)} — ${ask}`;
}
