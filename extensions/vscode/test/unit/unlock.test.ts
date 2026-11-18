import { describe, expect, it } from 'vitest';
import { unlockedLogins } from '../../src/bridgeProtocol';
import {
  MAX_ATTEMPTS_PER_ROUND,
  MAX_RETRIES_PER_REQUEST,
  initialUnlockState,
  onDismiss,
  onExplicitUnlock,
  onNeedsPassword,
  onRoundAborted,
  onStatusLocked,
  onStoredPassword,
  onStoredResult,
  onUnlockRefused,
  onUnlockResult,
  promptText,
  type PasswordNeed,
  type UnlockState,
} from '../../src/shared/unlock';

/**
 * Build the facts of a request that was answered with error -32010.
 *
 * @param state The state at the moment the request was started and sent.
 * @param over Members that differ from an interactive first attempt.
 * @returns The facts.
 */
function need(state: UnlockState, over: Partial<PasswordNeed> = {}): PasswordNeed {
  return {
    interactive: true,
    unlockedReported: [],
    startedAtDismissals: state.dismissals,
    sentAtUnlocks: state.unlocks,
    retries: 0,
    ...over,
  };
}

/**
 * Build a state in which the first password was accepted.
 *
 * @param changing Whether the held login may change settings.
 * @returns The state after one prompt round of kind "first".
 */
function unlockedState(changing: boolean): UnlockState {
  const prompted = onNeedsPassword(initialUnlockState(), need(initialUnlockState())).state;
  return onUnlockResult(prompted, 'first', { unlocked: ['operator'], changing }, 1).state;
}

describe('onNeedsPassword', () => {
  it('never prompts for a background request, and reports locked', () => {
    const state = initialUnlockState();
    const decision = onNeedsPassword(state, need(state, { interactive: false }));
    expect(decision.action).toBe('fail');
    expect(decision.state.promptOpen).toBeUndefined();
    expect(decision.state.locked).toBe(true);
  });

  it('opens one prompt for the first interactive request; concurrent requests join it', () => {
    const state = initialUnlockState();
    const first = onNeedsPassword(state, need(state));
    expect(first.action).toBe('prompt-first');
    expect(first.state.promptOpen).toBe('first');
    const second = onNeedsPassword(first.state, need(state));
    expect(second.action).toBe('join');
    const background = onNeedsPassword(first.state, need(state, { interactive: false }));
    expect(background.action).toBe('fail');
  });

  it('fails requests that started before an Escape and prompts again for a new one', () => {
    const start = initialUnlockState();
    const waiting = need(start);
    const prompted = onNeedsPassword(start, waiting).state;
    const dismissed = onDismiss(prompted);
    expect(dismissed.locked).toBe(true);
    expect(dismissed.promptOpen).toBeUndefined();
    // A request that was already under way when the user pressed Escape.
    expect(onNeedsPassword(dismissed, waiting).action).toBe('fail');
    // A request the user starts afterwards.
    expect(onNeedsPassword(dismissed, need(dismissed)).action).toBe('prompt-first');
  });

  it('retries without a prompt if an unlock succeeded after the request was sent', () => {
    const before = initialUnlockState();
    const sentEarly = need(before);
    const after = unlockedState(false);
    expect(onNeedsPassword(after, sentEarly).action).toBe('retry');
  });

  it('asks the admin password only when a login is held that may not change settings', () => {
    const reading = unlockedState(false);
    const decision = onNeedsPassword(reading, need(reading, { unlockedReported: ['operator'] }));
    expect(decision.action).toBe('prompt-admin');
    expect(decision.state.promptOpen).toBe('admin');
    expect(decision.state.locked).toBe(false);
    const changing = unlockedState(true);
    expect(onNeedsPassword(changing, need(changing)).action).toBe('fail');
  });

  it('uses the logins the bridge reports when the state knows none', () => {
    const state = initialUnlockState();
    const decision = onNeedsPassword(state, need(state, { unlockedReported: ['operator'] }));
    expect(decision.action).toBe('prompt-admin');
    expect(decision.state.locked).toBe(false);
  });

  it('asks no third password after the admin password was accepted', () => {
    const reading = unlockedState(false);
    const prompted = onNeedsPassword(reading, need(reading)).state;
    const step = onUnlockResult(prompted, 'admin', { unlocked: ['operator', 'admin'], changing: true }, 1);
    expect(step.outcome).toBe('done');
    expect(onNeedsPassword(step.state, need(step.state, { retries: 2 })).action).toBe('fail');
  });

  it('fails a request that was already sent again too often', () => {
    const state = initialUnlockState();
    expect(onNeedsPassword(state, need(state, { retries: MAX_RETRIES_PER_REQUEST })).action).toBe('fail');
    expect(onNeedsPassword(state, need(state, { retries: MAX_RETRIES_PER_REQUEST - 1 })).action).toBe('prompt-first');
  });
});

describe('prompt round', () => {
  it('asks again with the bridge message after a wrong password, up to the cap', () => {
    const start = initialUnlockState();
    const prompted = onNeedsPassword(start, need(start)).state;
    const refused = onUnlockRefused(prompted, "wrong password for the 'admin' credential", 1);
    expect(refused.outcome).toBe('ask-again');
    expect(refused.problem).toBe("wrong password for the 'admin' credential");
    expect(refused.state.promptOpen).toBe('first');
    expect(promptText('first', refused.problem)).toBe(
      "Wrong password for the 'admin' credential — calu password (asked once)",
    );
    const last = onUnlockRefused(refused.state, 'wrong password', MAX_ATTEMPTS_PER_ROUND);
    expect(last.outcome).toBe('give-up');
    expect(last.state.promptOpen).toBeUndefined();
    // Giving up is not an Escape: a new request may prompt again.
    expect(last.state.dismissals).toBe(0);
    expect(onNeedsPassword(last.state, need(last.state)).action).toBe('prompt-first');
  });

  it('holds the logins, clears locked and offers "Remember" once after the first unlock', () => {
    const start = onStatusLocked(initialUnlockState(), true);
    const prompted = onNeedsPassword(start, need(start)).state;
    const step = onUnlockResult(prompted, 'first', { unlocked: ['operator'], changing: false }, 1);
    expect(step.outcome).toBe('done');
    expect(step.offerRemember).toBe(true);
    expect(step.state).toMatchObject({ unlocked: ['operator'], locked: false, promptOpen: undefined, unlocks: 1 });
    const again = onUnlockResult(step.state, 'first', { unlocked: ['operator'], changing: false }, 1);
    expect(again.offerRemember).toBe(false);
  });

  it('does not offer "Remember" for the admin password', () => {
    const reading = unlockedState(false);
    const fresh = { ...reading, rememberSettled: false };
    const step = onUnlockResult(fresh, 'admin', { unlocked: ['operator', 'admin'], changing: true }, 1);
    expect(step.offerRemember).toBe(false);
  });

  it('asks the admin password again if the entered one does not allow changes', () => {
    const reading = unlockedState(false);
    const prompted = onNeedsPassword(reading, need(reading)).state;
    const step = onUnlockResult(prompted, 'admin', { unlocked: ['operator'], changing: false }, 1);
    expect(step.outcome).toBe('ask-again');
    expect(step.problem).toMatch(/does not allow changing settings/);
    expect(step.state.promptOpen).toBe('admin');
    const last = onUnlockResult(step.state, 'admin', { unlocked: ['operator'], changing: false }, MAX_ATTEMPTS_PER_ROUND);
    expect(last.outcome).toBe('give-up');
    expect(last.state.promptOpen).toBeUndefined();
  });

  it('keeps locked unchanged when the admin prompt is dismissed', () => {
    const reading = unlockedState(false);
    const prompted = onNeedsPassword(reading, need(reading)).state;
    const dismissed = onDismiss(prompted);
    expect(dismissed.locked).toBe(false);
    expect(dismissed.dismissals).toBe(1);
    expect(dismissed.unlocked).toEqual(['operator']);
  });

  it('closes the prompt when the round is aborted', () => {
    const start = initialUnlockState();
    const prompted = onNeedsPassword(start, need(start)).state;
    const aborted = onRoundAborted(prompted);
    expect(aborted.promptOpen).toBeUndefined();
    expect(aborted.dismissals).toBe(0);
  });
});

describe('onExplicitUnlock', () => {
  it('prompts after an Escape, joins an open prompt, and asks nothing when all is held', () => {
    const start = initialUnlockState();
    const dismissed = onDismiss(onNeedsPassword(start, need(start)).state);
    const explicit = onExplicitUnlock(dismissed);
    expect(explicit.action).toBe('prompt-first');
    expect(onExplicitUnlock(explicit.state).action).toBe('join');
    expect(onExplicitUnlock(unlockedState(false)).action).toBe('prompt-admin');
    expect(onExplicitUnlock(unlockedState(true)).action).toBe('none');
  });
});

describe('stored password', () => {
  it('is sent at the start only if one is stored', () => {
    expect(onStoredPassword('secret')).toBe('unlock');
    expect(onStoredPassword(undefined)).toBe('skip');
    expect(onStoredPassword('')).toBe('skip');
  });

  it('unlocks without offering "Remember" again', () => {
    const answer = { ok: true as const, result: { unlocked: ['admin'], changing: true } };
    const step = onStoredResult(initialUnlockState(), answer);
    expect(step.forget).toBe(false);
    expect(step.state).toMatchObject({ unlocked: ['admin'], changing: true, locked: false, rememberSettled: true });
  });

  it('is forgotten when refused; the next interactive request then asks and offers again', () => {
    const step = onStoredResult(initialUnlockState(), { ok: false, refused: true });
    expect(step.forget).toBe(true);
    expect(step.state.locked).toBe(true);
    const asked = onNeedsPassword(step.state, need(step.state));
    expect(asked.action).toBe('prompt-first');
    const unlocked = onUnlockResult(asked.state, 'first', { unlocked: ['admin'], changing: true }, 1);
    expect(unlocked.offerRemember).toBe(true);
  });

  it('is kept when the unlock failed for another reason', () => {
    const start = initialUnlockState();
    const step = onStoredResult(start, { ok: false, refused: false });
    expect(step.forget).toBe(false);
    expect(step.state).toBe(start);
  });
});

describe('texts and guards', () => {
  it('builds the prompt texts', () => {
    expect(promptText('first', undefined)).toBe('calu password (asked once)');
    expect(promptText('admin', undefined)).toBe('calu admin password, needed to change settings (asked once)');
    expect(promptText('admin', '')).toBe('calu admin password, needed to change settings (asked once)');
  });

  it('takes over the reported locked value', () => {
    const start = initialUnlockState();
    expect(onStatusLocked(start, false)).toBe(start);
    expect(onStatusLocked(start, true).locked).toBe(true);
  });

  it('reads the held logins from error data', () => {
    expect(unlockedLogins({ needs: 'admin', unlocked: ['operator', 3, ''] })).toEqual(['operator']);
    expect(unlockedLogins({ needs: 'admin' })).toEqual([]);
    expect(unlockedLogins(null)).toEqual([]);
    expect(unlockedLogins({ unlocked: 'operator' })).toEqual([]);
  });
});
