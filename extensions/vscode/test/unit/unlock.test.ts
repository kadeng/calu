import { describe, expect, it } from 'vitest';
import { unlockedLogins } from '../../src/bridgeProtocol';
import {
  MAX_ATTEMPTS_PER_ROUND,
  MAX_RETRIES_PER_REQUEST,
  NOT_DONE_NO_PASSWORD,
  NOT_DONE_REFUSED,
  initialUnlockState,
  notDoneText,
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

/** The bridge's message after a wrong calu user password (`logins.refused`). */
const REFUSED =
  'That is not the calu user password. Forgot it? DOCUMENTATION.md, "A forgotten user password", says how to set a new one.';

/**
 * Build a state in which the calu user password was accepted in a prompt.
 *
 * @returns The state after one successful prompt round.
 */
function unlockedState(): UnlockState {
  const prompted = onNeedsPassword(initialUnlockState(), need(initialUnlockState())).state;
  return onUnlockResult(prompted, { unlocked: ['user'] }).state;
}

describe('onNeedsPassword', () => {
  it('never prompts for a background request, and reports locked', () => {
    const state = initialUnlockState();
    const decision = onNeedsPassword(state, need(state, { interactive: false }));
    expect(decision.action).toBe('fail');
    expect(decision.state.promptOpen).toBe(false);
    expect(decision.state.locked).toBe(true);
  });

  it('opens one prompt for the first interactive request; concurrent requests join it', () => {
    const state = initialUnlockState();
    const first = onNeedsPassword(state, need(state));
    expect(first.action).toBe('prompt');
    expect(first.state.promptOpen).toBe(true);
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
    expect(dismissed.promptOpen).toBe(false);
    // A request that was already under way when the user pressed Escape.
    expect(onNeedsPassword(dismissed, waiting).action).toBe('fail');
    // A request the user starts afterwards.
    expect(onNeedsPassword(dismissed, need(dismissed)).action).toBe('prompt');
  });

  it('retries without a prompt if an unlock succeeded after the request was sent', () => {
    const before = initialUnlockState();
    const sentEarly = need(before);
    expect(onNeedsPassword(unlockedState(), sentEarly).action).toBe('retry');
  });

  it('asks no second password while a login is held', () => {
    const held = unlockedState();
    const decision = onNeedsPassword(held, need(held));
    expect(decision.action).toBe('fail');
    expect(decision.state.promptOpen).toBe(false);
    expect(decision.state.locked).toBe(false);
  });

  it('uses the logins the bridge reports when the state knows none', () => {
    const state = initialUnlockState();
    const decision = onNeedsPassword(state, need(state, { unlockedReported: ['user'] }));
    expect(decision.action).toBe('fail');
    expect(decision.state.locked).toBe(false);
  });

  it('fails a request that was already sent again too often', () => {
    const state = initialUnlockState();
    expect(onNeedsPassword(state, need(state, { retries: MAX_RETRIES_PER_REQUEST })).action).toBe('fail');
    expect(onNeedsPassword(state, need(state, { retries: MAX_RETRIES_PER_REQUEST - 1 })).action).toBe('prompt');
  });
});

describe('prompt round', () => {
  it('asks again with the bridge message after a wrong password, up to the cap', () => {
    const start = initialUnlockState();
    const prompted = onNeedsPassword(start, need(start)).state;
    const refused = onUnlockRefused(prompted, REFUSED, 1);
    expect(refused.outcome).toBe('ask-again');
    expect(refused.problem).toBe(REFUSED);
    expect(refused.state.promptOpen).toBe(true);
    expect(refused.left).toBe(2);
    expect(promptText(refused.problem, refused.left)).toBe(`${REFUSED} (2 tries left) — calu user password (asked once)`);
    const second = onUnlockRefused(refused.state, REFUSED, 2);
    expect(second.left).toBe(1);
    expect(promptText(second.problem, second.left)).toBe(`${REFUSED} (1 try left) — calu user password (asked once)`);
    const last = onUnlockRefused(refused.state, REFUSED, MAX_ATTEMPTS_PER_ROUND);
    expect(last.outcome).toBe('give-up');
    expect(last.state.promptOpen).toBe(false);
    // Giving up is not an Escape: a new request may prompt again.
    expect(last.state.dismissals).toBe(0);
    expect(onNeedsPassword(last.state, need(last.state)).action).toBe('prompt');
  });

  it('holds the logins, clears locked and offers "Remember" once after the unlock', () => {
    const start = onStatusLocked(initialUnlockState(), true);
    const prompted = onNeedsPassword(start, need(start)).state;
    const step = onUnlockResult(prompted, { unlocked: ['user'] });
    expect(step.outcome).toBe('done');
    expect(step.offerRemember).toBe(true);
    expect(step.state).toMatchObject({ unlocked: ['user'], locked: false, promptOpen: false, unlocks: 1 });
    const again = onUnlockResult(step.state, { unlocked: ['user'] });
    expect(again.offerRemember).toBe(false);
  });

  it('closes the prompt when the round is aborted', () => {
    const start = initialUnlockState();
    const prompted = onNeedsPassword(start, need(start)).state;
    const aborted = onRoundAborted(prompted);
    expect(aborted.promptOpen).toBe(false);
    expect(aborted.dismissals).toBe(0);
  });
});

describe('notDoneText', () => {
  it('says what did not happen and how to go on after an Escape', () => {
    const start = initialUnlockState();
    const waiting = need(start);
    const dismissed = onDismiss(onNeedsPassword(start, waiting).state);
    expect(NOT_DONE_NO_PASSWORD).toBe(
      'Not done: no password was given. The next action that needs the database asks again.',
    );
    // The request that opened the prompt, and one that started before the Escape without a round.
    expect(notDoneText(dismissed, waiting, 'dismissed')).toBe(NOT_DONE_NO_PASSWORD);
    expect(notDoneText(dismissed, waiting, undefined)).toBe(NOT_DONE_NO_PASSWORD);
  });

  it('says that the passwords were refused after a round gave up', () => {
    const start = initialUnlockState();
    expect(NOT_DONE_REFUSED).toBe(
      'Not done: the password was refused 3 times. The next action that needs the database asks again.',
    );
    expect(notDoneText(start, need(start), 'refused')).toBe(NOT_DONE_REFUSED);
  });

  it("keeps the bridge's message for background requests and other failures", () => {
    const start = initialUnlockState();
    const dismissed = onDismiss(onNeedsPassword(start, need(start)).state);
    expect(notDoneText(dismissed, need(start, { interactive: false }), undefined)).toBeUndefined();
    expect(notDoneText(start, need(start), 'aborted')).toBeUndefined();
    expect(notDoneText(start, need(start, { retries: MAX_RETRIES_PER_REQUEST }), undefined)).toBeUndefined();
  });
});

describe('onExplicitUnlock', () => {
  it('prompts after an Escape, joins an open prompt, and asks nothing while a login is held', () => {
    const start = initialUnlockState();
    const dismissed = onDismiss(onNeedsPassword(start, need(start)).state);
    const explicit = onExplicitUnlock(dismissed);
    expect(explicit.action).toBe('prompt');
    expect(explicit.state.promptOpen).toBe(true);
    expect(onExplicitUnlock(explicit.state).action).toBe('join');
    expect(onExplicitUnlock(unlockedState()).action).toBe('none');
  });
});

describe('stored password', () => {
  it('is sent at the start only if one is stored', () => {
    expect(onStoredPassword('secret')).toBe('unlock');
    expect(onStoredPassword(undefined)).toBe('skip');
    expect(onStoredPassword('')).toBe('skip');
  });

  it('unlocks without offering "Remember" again', () => {
    const answer = { ok: true as const, result: { unlocked: ['user'] } };
    const step = onStoredResult(initialUnlockState(), answer);
    expect(step.forget).toBe(false);
    expect(step.state).toMatchObject({ unlocked: ['user'], locked: false, rememberSettled: true });
    const prompted = { ...step.state, unlocked: [], promptOpen: true };
    expect(onUnlockResult(prompted, { unlocked: ['user'] }).offerRemember).toBe(false);
  });

  it('is forgotten when refused; the next interactive request then asks and offers again', () => {
    const step = onStoredResult(initialUnlockState(), { ok: false, refused: true });
    expect(step.forget).toBe(true);
    expect(step.state.locked).toBe(true);
    const asked = onNeedsPassword(step.state, need(step.state));
    expect(asked.action).toBe('prompt');
    const unlocked = onUnlockResult(asked.state, { unlocked: ['user'] });
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
    expect(promptText(undefined, undefined)).toBe('calu user password (asked once)');
    expect(promptText('', 2)).toBe('calu user password (asked once)');
    expect(promptText('no password was typed, and this login has one.', 2)).toBe(
      'No password was typed, and this login has one. (2 tries left) — calu user password (asked once)',
    );
    expect(promptText('no password was typed, and this login has one.', undefined)).toBe(
      'No password was typed, and this login has one. — calu user password (asked once)',
    );
  });

  it('takes over the reported locked value', () => {
    const start = initialUnlockState();
    expect(onStatusLocked(start, false)).toBe(start);
    expect(onStatusLocked(start, true).locked).toBe(true);
  });

  it('reads the held logins from error data', () => {
    expect(unlockedLogins({ needs: 'user', unlocked: ['user', 3, ''] })).toEqual(['user']);
    expect(unlockedLogins({ needs: 'user' })).toEqual([]);
    expect(unlockedLogins(null)).toEqual([]);
    expect(unlockedLogins({ unlocked: 'user' })).toEqual([]);
  });
});
