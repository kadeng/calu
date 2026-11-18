// The recorded conversation of a session as transcript entries, and its place in the view state.
import { describe, expect, it } from 'vitest';
import { MAX_HISTORY_ENTRIES, historyEntries, type Covered } from '../../src/shared/history';
import { HISTORY_START_TEXT, MAIN, applyEvent, applyEvents, initialViewState, own, type ViewEvent, type ViewState } from '../../src/shared/viewState';

/**
 * Shorthand for a reducer event.
 *
 * @returns The event.
 */
function ev(method: string, params: Record<string, unknown> = {}): ViewEvent {
  return { method, params };
}

/**
 * Build what a replay covers.
 *
 * @returns The sets of `Covered`.
 */
function covered(uuids: string[] = [], inputs: string[] = [], toolUseIds: string[] = []): Covered {
  return { uuids: new Set(uuids), inputs: new Set(inputs), toolUseIds: new Set(toolUseIds) };
}

/** Three recorded turns: two inherited by a fork (their steps are in the turn records only), one of the session itself. */
const HISTORY = {
  calu_session_id: 's2',
  claude_session_id: 'c2',
  chat_summary: '',
  turns: [
    {
      index: 1,
      prompt_uuid: 'u1',
      user_inputs: ['read the greeter'],
      steps: [
        { tool_name: 'Read', summary: 'Reads greeter.py', decision: 'pass', agent_id: null },
        { tool_name: 'Bash', summary: 'Runs the tests', decision: 'allow', agent_id: 'toolu_agent' },
      ],
      answer: 'It greets.',
      ended_at: '2026-11-18T10:00:00Z',
    },
    { index: 2, prompt_uuid: 'u2', user_inputs: ['now change it', '  '], steps: [], answer: '', ended_at: '2026-11-18T10:01:00Z' },
    {
      index: 3,
      prompt_uuid: 'u3',
      user_inputs: ['and commit'],
      steps: [{ tool_name: 'Bash', summary: 'Commits', decision: 'escalate' }],
      answer: 'Committed.',
      ended_at: '2026-11-18T10:02:00Z',
    },
  ],
  steps: [
    { seq: 1, turn: 3, tool_use_id: 't31', tool_name: 'Bash', tool_input: { command: 'git commit -m x' }, decision: 'escalate', reason: 'a commit', summary: 'Commits', result_summary: '[main 1234] x' },
    { seq: 2, turn: 9, tool_use_id: 't99', tool_name: 'Bash', tool_input: {}, decision: 'pass' },
  ],
};

describe('historyEntries', () => {
  it('converts every turn when the replay covers nothing', () => {
    const h = historyEntries(HISTORY, covered());
    expect(h.turns).toBe(3);
    expect(h.omittedTurns).toBe(0);
    expect(h.entries.map((e) => e.kind)).toEqual(['user', 'step', 'step', 'answer', 'turnEnd', 'user', 'turnEnd', 'user', 'step', 'answer', 'turnEnd']);
    expect(h.entries[0]).toEqual({ kind: 'user', text: 'read the greeter', turn: 1, uuid: 'u1' });
    // An inherited step: only what the turn record holds.
    expect(h.entries[1]).toEqual({ kind: 'step', toolUseId: null, name: 'Read', input: null, decision: 'PASS', reason: '', summary: 'Reads greeter.py', result: null, subagent: false });
    expect(h.entries[2]).toMatchObject({ name: 'Bash', decision: 'ALLOW', subagent: true });
    // A step this session recorded: with its id, input and the start of its result.
    expect(h.entries[8]).toEqual({
      kind: 'step',
      toolUseId: 't31',
      name: 'Bash',
      input: { command: 'git commit -m x' },
      decision: 'ESCALATE',
      reason: 'a commit',
      summary: 'Commits',
      result: '[main 1234] x',
      subagent: false,
    });
  });

  it('stops at the first turn the replay holds', () => {
    // A fork that has run its own turn 3: the replay holds its input.
    const byUuid = historyEntries(HISTORY, covered(['u3']));
    expect(byUuid.entries.filter((e) => e.kind === 'user').map((e) => (e.kind === 'user' ? e.turn : 0))).toEqual([1, 2]);
    expect(byUuid.turns).toBe(2);
    // Without uuids the text of the input decides.
    expect(historyEntries(HISTORY, covered([], ['and commit'])).entries).toEqual(byUuid.entries);
    // Only a step of the turn was replayed (the buffer moved on past the input): the input is
    // added, the replayed step and everything after it is not.
    const byStep = historyEntries(HISTORY, covered([], [], ['t31']));
    expect(byStep.entries.slice(-1)).toEqual([{ kind: 'user', text: 'and commit', turn: 3, uuid: 'u3' }]);
    // A session that replays from its first turn: nothing to add.
    expect(historyEntries(HISTORY, covered(['u1'])).entries).toEqual([]);
    // Where the replay begins in the conversation's own numbering.
    expect(historyEntries(HISTORY, covered(['u1'])).firstReplayedTurn).toBe(1);
    expect(byUuid.firstReplayedTurn).toBe(3);
    expect(byStep.firstReplayedTurn).toBe(3);
    expect(historyEntries(HISTORY, covered()).firstReplayedTurn).toBe(4);
  });

  it('survives malformed input', () => {
    for (const raw of [null, 7, 'x', [], {}, { turns: 'no' }, { turns: [null, 3, { index: 'one' }], steps: [null, { turn: 1 }] }]) {
      expect(historyEntries(raw, covered())).toEqual({ entries: [], turns: 0, omittedTurns: 0, firstReplayedTurn: 1 });
    }
    const odd = historyEntries({ turns: [{ index: 1, user_inputs: [3, 'hi'], steps: [{}, { tool_name: 'Read' }], answer: 5 }] }, covered());
    expect(odd.entries.map((e) => e.kind)).toEqual(['user', 'step']);
  });

  it('keeps the newest whole turns when the record is long', () => {
    const turns = Array.from({ length: 300 }, (_, i) => ({ index: i + 1, user_inputs: [`m${i + 1}`], answer: 'ok', ended_at: 'x' }));
    const h = historyEntries({ turns }, covered());
    expect(h.entries.length).toBeLessThanOrEqual(MAX_HISTORY_ENTRIES);
    expect(h.entries.length % 3).toBe(0);
    expect(h.turns + h.omittedTurns).toBe(300);
    expect(h.omittedTurns).toBeGreaterThan(0);
    expect(h.entries.at(-3)).toMatchObject({ kind: 'user', text: 'm300' });
    // One turn that is longer than the limit is kept whole.
    const long = { index: 1, user_inputs: ['x'], steps: Array.from({ length: MAX_HISTORY_ENTRIES + 5 }, () => ({ tool_name: 'Bash', summary: 's' })) };
    expect(historyEntries({ turns: [long] }, covered()).entries).toHaveLength(MAX_HISTORY_ENTRIES + 6);
  });
});

describe('ui.history', () => {
  /**
   * Build the state of a forked session: attached, with the events of its own turn 3.
   *
   * @returns The state.
   */
  function forked(events: ViewEvent[]): ViewState {
    return applyEvents(initialViewState(), [
      ev('bridge.status', { status: 'running' }),
      ev('ui.attached', { session_id: 's2', snapshot: { state: 'idle', workspace: '/w' }, events }),
    ]);
  }

  it('puts the inherited turns above the events of the session, marked as recorded', () => {
    const live = forked([
      ev('user.input', { text: 'and commit', uuid: 'u3', seq: 1 }),
      ev('tool.use', { tool_use_id: 't31', name: 'Bash', input: { command: 'git commit -m x' }, seq: 2 }),
    ]);
    const s = applyEvent(live, ev('ui.history', { session_id: 's2', history: HISTORY }));
    const main = s.items[MAIN];
    expect(main.map((i) => i.kind)).toEqual(['divider', 'user', 'tool', 'tool', 'assistant', 'turn', 'user', 'turn', 'divider', 'user', 'tool']);
    expect(main[0]).toMatchObject({ kind: 'divider', mark: 'history-start', text: HISTORY_START_TEXT });
    expect(main[1]).toMatchObject({ kind: 'user', text: 'read the greeter', turn: 1, uuid: 'u1', stored: true, source: 'user' });
    expect(main[4]).toMatchObject({ kind: 'assistant', text: 'It greets.', stored: true });
    expect(main[5]).toMatchObject({ kind: 'turn', stored: true, costUsd: null });
    // The session's own items follow, numbered as the conversation numbers its turns: the
    // view counted "and commit" as its turn 1, the conversation has it as turn 3.
    expect(live.items[MAIN][0]).toMatchObject({ kind: 'user', turn: 1 });
    expect(main[9]).toMatchObject({ kind: 'user', text: 'and commit', uuid: 'u3', turn: 3 });
    expect(main[10]).toBe(live.items[MAIN][1]);
    expect(s.turnBase).toBe(2);
    // A message that arrives later continues that numbering.
    const later = applyEvents(s, [ev('turn.result', {}), ev('user.input', { text: 'more' })]);
    expect(later.items[MAIN].at(-1)).toMatchObject({ kind: 'user', text: 'more', turn: 4 });
    expect(new Set(main.map((i) => i.id)).size).toBe(main.length);
    // A recorded step: decision and summary, no invented input or result.
    const read = main[2].kind === 'tool' ? own(s.tools, main[2].toolUseId) : undefined;
    expect(read).toMatchObject({ name: 'Read', input: {}, check: { decision: 'PASS', summary: 'Reads greeter.py', route: 'stored', checkerRoute: '' }, stored: { subagent: false, hasInput: false } });
    expect(read?.result).toBeUndefined();
    const bash = main[3].kind === 'tool' ? own(s.tools, main[3].toolUseId) : undefined;
    expect(bash?.stored).toEqual({ subagent: true, hasInput: false });
    // No tab is invented for the subagent of a recorded step.
    expect(s.agents).toEqual([]);
    // Applied once: a second answer changes nothing, and neither does one for another session.
    expect(applyEvent(s, ev('ui.history', { session_id: 's2', history: HISTORY }))).toBe(s);
    expect(applyEvent(live, ev('ui.history', { session_id: 'other', history: HISTORY }))).toBe(live);
  });

  it('shows a fresh fork that has no events yet, with the steps of its own record in full', () => {
    const s = applyEvent(forked([]), ev('ui.history', { session_id: 's2', history: HISTORY }));
    const tools = s.items[MAIN].filter((i) => i.kind === 'tool');
    expect(tools).toHaveLength(3);
    expect(own(s.tools, 't31')).toMatchObject({
      input: { command: 'git commit -m x' },
      check: { decision: 'ESCALATE', reason: 'a commit' },
      result: { content: '[main 1234] x', isError: false },
      stored: { subagent: false, hasInput: true },
    });
    // New events go below the recorded turns.
    const next = applyEvent(s, ev('user.input', { text: 'next', uuid: 'u4' }));
    expect(next.items[MAIN].at(-1)).toMatchObject({ kind: 'user', text: 'next' });
    expect(next.items[MAIN].at(-2)).toMatchObject({ kind: 'divider', mark: 'history-end' });
  });

  it('adds nothing when the replay holds the whole conversation', () => {
    const live = forked([ev('user.input', { text: 'read the greeter', uuid: 'u1', seq: 1 })]);
    expect(live.turnBase).toBeNull();
    // Only the numbering becomes known: the first message is the conversation's first.
    const known = applyEvent(live, ev('ui.history', { session_id: 's2', history: HISTORY }));
    expect(known).toEqual({ ...live, turnBase: 0 });
    expect(known.items).toBe(live.items);
    expect(applyEvent(live, ev('ui.history', { session_id: 's2', history: null }))).toEqual({ ...live, turnBase: 0 });
    // Another attach forgets it until the history was read again.
    expect(applyEvent(known, ev('ui.attached', { session_id: 's2', snapshot: { state: 'idle', workspace: '/w' }, events: [] })).turnBase).toBeNull();
  });

  it('says how many older turns are left out', () => {
    const turns = Array.from({ length: 300 }, (_, i) => ({ index: i + 1, user_inputs: [`m${i + 1}`], answer: 'ok', ended_at: 'x' }));
    const s = applyEvent(forked([]), ev('ui.history', { session_id: 's2', history: { turns } }));
    expect(s.items[MAIN][0]).toMatchObject({ kind: 'divider' });
    expect((s.items[MAIN][0] as { text: string }).text).toMatch(/^Earlier in this conversation \(\d+ older turns are not shown\)$/);
  });
});
