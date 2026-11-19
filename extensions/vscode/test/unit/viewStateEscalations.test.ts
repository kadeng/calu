// Escalations (also questions of the agent), subagent tasks and results, and attaching.
import { describe, expect, it } from 'vitest';
import {
  MAIN,
  answerFromNote,
  applyAttach,
  applyEvent,
  applyEvents,
  formatAnswer,
  honestyChip,
  initialViewState,
  isAttached,
  openEscalationCount,
  parseQuestions,
  sessionTitle,
  sideHistory,
  type ViewEvent,
} from '../../src/shared/viewState';

/**
 * Shorthand for a reducer event.
 *
 * @returns The event.
 */
function ev(method: string, params: Record<string, unknown> = {}): ViewEvent {
  return { method, params };
}

/**
 * Build an `escalation.request` event.
 *
 * @returns The event.
 */
function request(id: string, extra: Record<string, unknown> = {}): ViewEvent {
  return ev('escalation.request', { escalation_id: id, tool_use_id: 't', tool_name: 'Bash', tool_input: { command: 'rm x' }, reason: 'r', rendered: '`rm x`', ...extra });
}

describe('escalations', () => {
  it('opens and resolves escalations, including cancelled', () => {
    let s = applyEvents(initialViewState(), [request('esc-1'), request('esc-2'), request('esc-3', { opened_at: '2026-11-18T10:00:00Z' })]);
    expect(openEscalationCount(s)).toBe(3);
    expect(s.escalations['esc-3'].openedAt).toBe('2026-11-18T10:00:00Z');
    s = applyEvents(s, [
      ev('escalation.resolved', { escalation_id: 'esc-1', decision: 'allow' }),
      ev('escalation.resolved', { escalation_id: 'esc-2', decision: 'deny', note: 'no' }),
      ev('escalation.resolved', { escalation_id: 'esc-3', decision: 'cancelled' }),
    ]);
    expect(s.escalations['esc-1'].status).toBe('allow');
    expect(s.escalations['esc-2']).toMatchObject({ status: 'deny', note: 'no' });
    // calu sends the text Claude receives; the card keeps the user's own words only.
    const framed = applyEvent(s, ev('escalation.request', { escalation_id: 'esc-9', tool_name: 'Bash', tool_input: {} }));
    const bare = applyEvent(framed, ev('escalation.resolved', { escalation_id: 'esc-9', decision: 'deny', note: 'The user denied this step.' }));
    expect(bare.escalations['esc-9']).toMatchObject({ status: 'deny', note: null });
    const framed2 = applyEvent(s, ev('escalation.request', { escalation_id: 'esc-8', tool_name: 'Bash', tool_input: {} }));
    const noted = applyEvent(framed2, ev('escalation.resolved', { escalation_id: 'esc-8', decision: 'deny', note: 'The user denied this step. Note from the user: not yet' }));
    expect(noted.escalations['esc-8']).toMatchObject({ status: 'deny', note: 'not yet' });
    expect(s.escalations['esc-3'].status).toBe('cancelled');
    expect(openEscalationCount(s)).toBe(0);
  });

  it('never reopens a closed card: a reused id gets a new key', () => {
    let s = applyEvents(initialViewState(), [request('e'), ev('escalation.resolved', { escalation_id: 'e', decision: 'deny' }), request('e')]);
    expect(Object.keys(s.escalations)).toHaveLength(2);
    expect(s.escalations.e.status).toBe('deny');
    s = applyEvent(s, ev('escalation.resolved', { escalation_id: 'e', decision: 'allow' }));
    expect(Object.values(s.escalations).map((x) => x.status).sort()).toEqual(['allow', 'deny']);
  });

  it('requests the tab of a new escalation', () => {
    const s = applyEvents(initialViewState(), [request('e1'), request('e2', { agent_id: 'A1' })]);
    expect(s.focus).toEqual({ tab: 'A1', seq: 2 });
    expect(s.items.A1.at(-1)).toMatchObject({ kind: 'escalation', escalationKey: 'e2' });
    expect(s.agents.map((a) => a.agent_id)).toEqual(['A1']);
  });

  it('marks a decision as pending and takes it back when sending failed', () => {
    let s = applyEvent(initialViewState(), request('e'));
    s = applyEvent(s, ev('ui.decisionPending', { escalation_key: 'e' }));
    expect(s.escalations.e.pending).toBe(true);
    s = applyEvent(s, ev('ui.decisionFailed', { escalation_key: 'e' }));
    expect(s.escalations.e.pending).toBe(false);
    expect(applyEvent(s, ev('ui.decisionPending', { escalation_key: 'unknown' }))).toBe(s);
  });

  it('cancels open escalations when the session stops or is left', () => {
    const open = applyEvent(initialViewState(), request('e'));
    expect(applyEvent(open, ev('session.state', { state: 'stopped' })).escalations.e.status).toBe('cancelled');
    expect(applyEvent(open, ev('session.state', { state: 'failed' })).escalations.e.status).toBe('cancelled');
    expect(applyEvent(open, ev('ui.detached')).escalations.e.status).toBe('cancelled');
    expect(applyEvent(open, ev('session.state', { state: 'escalated' })).escalations.e.status).toBe('open');
  });

  it('keeps the cards open when the bridge or the connection is lost: that is no decision', () => {
    let open = applyEvents(initialViewState(), [
      ev('bridge.status', { status: 'running' }),
      ev('ui.attached', { session_id: 's1', snapshot: { state: 'escalated', workspace: '/w', escalations: [request('e').params] }, events: [request('e')] }),
    ]);
    open = applyEvent(open, ev('ui.decisionPending', { escalation_key: 'e' }));
    for (const loss of [ev('bridge.status', { status: 'exited', message: 'gone' }), ev('ui.link', { state: 'lost', message: 'gone' })]) {
      const lost = applyEvent(open, loss);
      // Still open, and a decision that was on its way no longer counts as sent.
      expect(lost.escalations.e).toMatchObject({ status: 'open', pending: false });
      expect(openEscalationCount(lost)).toBe(1);
      expect(isAttached(lost)).toBe(false);
      // The session it had stays known, so that the same one is attached again.
      expect(lost.session.sessionId).toBe('s1');
    }
  });

  it('continues the transcript when the same session is attached again without a gap', () => {
    const first = applyEvents(initialViewState(), [
      ev('bridge.status', { status: 'running' }),
      ev('ui.attached', {
        session_id: 's1',
        snapshot: { state: 'escalated', workspace: '/w', last_seq: 3, escalations: [request('e').params] },
        events: [ev('user.input', { text: 'go', seq: 1 }), ev('assistant.text', { text: 'on it', seq: 2 }), { ...request('e'), params: { ...request('e').params, seq: 3 } }],
      }),
      ev('ui.honestyStored', { mode: 'track' }),
      ev('bridge.status', { status: 'exited' }),
      ev('bridge.status', { status: 'running' }),
    ]);
    const kinds = (s: typeof first): string[] => s.items[MAIN].map((i) => i.kind);
    expect(kinds(first)).toEqual(['user', 'assistant', 'escalation']);
    // The decision was made elsewhere meanwhile; the replay after seq 3 says so.
    const resumed = applyEvent(
      first,
      ev('ui.attached', {
        session_id: 's1',
        resumed: true,
        snapshot: { state: 'running', workspace: '/w', last_seq: 5, escalations: [] },
        events: [ev('escalation.resolved', { escalation_id: 'e', decision: 'allow', seq: 4 }), ev('assistant.text', { text: 'done', seq: 5 })],
      }),
    );
    expect(kinds(resumed)).toEqual(['user', 'assistant', 'escalation', 'assistant']);
    expect(resumed.escalations.e.status).toBe('allow');
    expect(resumed.items[MAIN].slice(0, 3)).toEqual(first.items[MAIN]);
    expect(resumed).toMatchObject({ lastSeq: 5, honestyStored: 'track', link: { status: 'attached' } });
    // `resumed` for another session than the one shown is a full attach.
    const other = applyEvent(first, ev('ui.attached', { session_id: 's2', resumed: true, snapshot: { state: 'idle', workspace: '/w' }, events: [] }));
    expect(other.items[MAIN]).toEqual([]);
    expect(other.honestyStored).toBeNull();
  });
});

describe('questions of the agent', () => {
  const questions = [
    { question: 'Which database?', header: 'Database', options: [{ label: 'PostgreSQL', description: 'as now' }, { label: 'SQLite' }, { nolabel: 1 }], multiSelect: false },
    { question: 'Which checks?', options: [{ label: 'lint', description: '' }, { label: 'types', description: '' }], multiSelect: true },
    { header: 'no question text' },
  ];

  it('reads the questions defensively', () => {
    const parsed = parseQuestions(questions);
    expect(parsed).toEqual([
      { question: 'Which database?', header: 'Database', options: [{ label: 'PostgreSQL', description: 'as now' }, { label: 'SQLite', description: '' }], multiSelect: false },
      { question: 'Which checks?', header: '', options: [{ label: 'lint', description: '' }, { label: 'types', description: '' }], multiSelect: true },
    ]);
    expect(parseQuestions('nope')).toEqual([]);
  });

  it('formats the answer text', () => {
    const parsed = parseQuestions(questions);
    expect(formatAnswer(parsed.slice(0, 1), [{ labels: ['SQLite'], text: '' }])).toBe('SQLite');
    expect(formatAnswer(parsed, [{ labels: ['SQLite'], text: ' but only for tests ' }, { labels: ['lint', 'types'], text: '' }])).toBe(
      'Database: SQLite, but only for tests\nWhich checks?: lint, types',
    );
    expect(formatAnswer(parsed, [undefined, { labels: [], text: 'none' }])).toBe('Database: (no answer)\nWhich checks?: none');
    expect(formatAnswer(parsed, [])).toBe('');
  });

  it('shows an answered escalation as answered', () => {
    let s = applyEvent(initialViewState(), request('q', { tool_name: 'AskUserQuestion', questions }));
    expect(s.escalations.q.questions).toHaveLength(2);
    s = applyEvent(s, ev('ui.decisionPending', { escalation_key: 'q', answer: 'SQLite' }));
    expect(s.escalations.q).toMatchObject({ pending: true, answer: 'SQLite' });
    s = applyEvent(s, ev('escalation.resolved', { escalation_id: 'q', decision: 'allow' }));
    expect(s.escalations.q).toMatchObject({ status: 'answered', answer: 'SQLite', pending: false });
  });

  it('shows a question that was answered elsewhere, or replayed, as answered with its answer', () => {
    const note = 'The user answered your question: Database: SQLite\nWhich checks?: lint\nThe AskUserQuestion call itself was not run; this is the answer. Carry on with it.';
    let s = applyEvent(initialViewState(), request('q', { tool_name: 'AskUserQuestion', questions }));
    s = applyEvent(s, ev('escalation.resolved', { escalation_id: 'q', decision: 'deny', note }));
    expect(s.escalations.q).toMatchObject({ status: 'answered', answer: 'Database: SQLite\nWhich checks?: lint', note: null });
    expect(answerFromNote('another text')).toBe('another text');
    let cancelled = applyEvent(initialViewState(), request('q', { tool_name: 'AskUserQuestion', questions }));
    cancelled = applyEvent(cancelled, ev('escalation.resolved', { escalation_id: 'q', decision: 'cancelled' }));
    expect(cancelled.escalations.q.status).toBe('cancelled');
  });
});

describe('subagents', () => {
  const launch = ev('tool.use', {
    tool_use_id: 'L1',
    name: 'Agent',
    input: { description: 'Survey the tests', prompt: 'List every test file.', subagent_type: 'Explore', run_in_background: false },
  });

  it('records the task from the launching tool call and the result from its tool.result', () => {
    let s = applyEvents(initialViewState(), [
      launch,
      ev('assistant.text', { agent_id: 'L1', text: 'looking' }),
      ev('tool.result', { tool_use_id: 'L1', content: 'There are 12 test files.', is_error: false }),
    ]);
    expect(s.subagents.L1).toEqual({
      agentId: 'L1',
      description: 'Survey the tests',
      subagentType: 'Explore',
      background: false,
      task: 'List every test file.',
      result: { content: 'There are 12 test files.', isError: false, final: true },
    });
    // The main transcript has the launching call; the subagent's tab starts with the task.
    expect(s.items[MAIN]).toMatchObject([{ kind: 'tool', toolUseId: 'L1' }]);
    expect(s.items.L1.map((i) => i.kind)).toEqual(['launch', 'assistant']);
    expect(s.items.L1[0]).toMatchObject({ kind: 'launch', prompt: 'List every test file.', description: 'Survey the tests' });
    expect(s.agents.map((a) => a.agent_id)).toEqual(['L1']);
    s = applyEvent(s, ev('agents.update', { agents: [{ agent_id: 'L1', description: 'Survey the tests', status: 'completed' }] }));
    expect(s.items.L1).toHaveLength(2);
  });

  it('puts the task first even if events of the subagent came earlier', () => {
    const s = applyEvents(initialViewState(), [ev('assistant.text', { agent_id: 'L1', text: 'early' }), launch, launch]);
    expect(s.items.L1.map((i) => i.kind)).toEqual(['launch', 'assistant']);
  });

  it('marks the result of a background launch as not final', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 'B1', name: 'Task', input: { description: 'Watch CI', prompt: 'Poll.', run_in_background: true } }),
      ev('tool.result', { tool_use_id: 'B1', content: 'Launched in the background.' }),
    ]);
    expect(s.subagents.B1).toMatchObject({ background: true, result: { content: 'Launched in the background.', final: false } });
    expect(s.items.B1[0]).toMatchObject({ kind: 'launch', background: true });
  });

  it('keeps ordinary tool results out of the subagents', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 't', name: 'Bash', input: { command: 'ls' } }),
      ev('tool.result', { tool_use_id: 't', content: 'a' }),
    ]);
    expect(Object.keys(s.subagents)).toEqual([]);
  });
});

describe('attaching', () => {
  const result = {
    session_id: 'S1',
    initialize: { core_version: '1', workspace: '/w', project: 'demo' },
    snapshot: {
      state: 'escalated',
      workspace: '/w',
      claude_session_id: 'C1',
      model: 'opus',
      effort: 'high',
      last_seq: 9,
      agents: [{ agent_id: 'A1', description: 'worker', background: true, status: 'running' }],
      queue: [{ id: 'q1', text: 'later', delivery: 'queued', to: 'main' }],
      auto_escalate: { edits: true },
      honesty: { mode: 'warn', clean: true },
      rate_limits: [{ type: 'five_hour', status: 'allowed' }],
      escalations: [
        { escalation_id: 'e-open', tool_use_id: 't2', tool_name: 'Bash', tool_input: {}, reason: 'r', rendered: '' },
        { escalation_id: 'e-new', agent_id: 'A1', tool_use_id: null, tool_name: 'Edit', tool_input: {}, reason: 'r', rendered: '' },
      ],
    },
    events: [
      { method: 'session.state', params: { state: 'stopped', seq: 1 } },
      { method: 'user.input', params: { text: 'go', seq: 2 } },
      { method: 'assistant.delta', params: { message_id: 'm', text: 'wor', seq: 3 } },
      { method: 'escalation.request', params: { escalation_id: 'e-gone', tool_name: 'Bash', tool_input: {}, reason: '', rendered: '', seq: 4 } },
      { method: 'escalation.request', params: { escalation_id: 'e-open', tool_use_id: 't2', tool_name: 'Bash', tool_input: {}, reason: 'r', rendered: '', seq: 5 } },
      { method: 'log', params: { level: 'error', message: 'old noise', seq: 6 } },
      { method: 'ui.clear', params: {} },
      'junk',
    ],
    complete: true,
  };

  it('replays the events and takes what is true now from the snapshot', () => {
    const before = applyEvents(initialViewState(), [
      ev('bridge.status', { status: 'running' }),
      ev('assistant.text', { text: 'from the session shown before' }),
      ev('ui.serverHelp', { command: 'calu serve', tool: 'plain' }),
    ]);
    const s = applyEvent(before, ev('ui.attached', result));
    expect(s.link.status).toBe('attached');
    expect(s.session).toEqual({
      state: 'escalated',
      sessionId: 'S1',
      claudeSessionId: 'C1',
      workspace: '/w',
      project: 'demo',
      model: 'opus',
      effort: 'high',
      backgroundTasks: 0,
    });
    expect(s.sessions.attachedId).toBe('S1');
    expect(s.serverHelp).toBeNull();
    expect(s.items[MAIN].map((i) => i.kind)).toEqual(['user', 'assistant', 'escalation', 'escalation']);
    expect(s.items[MAIN][1]).toMatchObject({ text: 'wor', streaming: false });
    expect(s.escalations['e-gone'].status).toBe('cancelled');
    expect(s.escalations['e-open'].status).toBe('open');
    expect(s.escalations['e-new']).toMatchObject({ status: 'open', agentKey: 'A1' });
    expect(openEscalationCount(s)).toBe(2);
    expect(s.focus?.tab).toBe(MAIN);
    expect(s.agents).toHaveLength(1);
    expect(s.queue).toHaveLength(1);
    expect(s.autoEscalate).toEqual({ commands: false, edits: true, subagents: false });
    expect(s.honesty).toMatchObject({ mode: 'warn', clean: true });
    expect(s.usage.rateLimits).toHaveLength(1);
    expect(s.lastSeq).toBe(9);
  });

  it('continues a text that the replay of an attach cut off, instead of showing it twice', () => {
    const delta = (text: string) => ev('assistant.delta', { agent_id: null, message_id: 'm1', text });
    let s = applyAttach(initialViewState(), { session_id: 'S', snapshot: { state: 'running' }, events: [delta('Building, ')] });
    expect(s.items[MAIN]).toMatchObject([{ kind: 'assistant', text: 'Building, ', streaming: false, cut: true }]);
    s = applyEvent(s, delta('then '));
    expect(s.items[MAIN]).toEqual([{ id: 1, kind: 'assistant', text: 'Building, then ', messageId: 'm1', streaming: true }]);
    s = applyEvent(s, ev('assistant.text', { agent_id: null, message_id: 'm1', text: 'Building, then uploading.' }));
    expect(s.items[MAIN]).toEqual([{ id: 1, kind: 'assistant', text: 'Building, then uploading.', messageId: 'm1', streaming: false }]);
    // The final text alone also replaces the cut item.
    let t = applyAttach(initialViewState(), { session_id: 'S', snapshot: {}, events: [delta('Buil')] });
    t = applyEvent(t, ev('assistant.text', { agent_id: null, message_id: 'm1', text: 'Built.' }));
    expect(t.items[MAIN].map((i) => (i.kind === 'assistant' ? i.text : ''))).toEqual(['Built.']);
  });

  it('survives a result without members and ignores one without a session id', () => {
    const s0 = initialViewState();
    expect(applyAttach(s0, {})).toBe(s0);
    const s = applyAttach(s0, { session_id: 'S', snapshot: 'x', events: 5 });
    expect(s.session).toMatchObject({ sessionId: 'S', state: 'idle', workspace: null });
    expect(s.items).toEqual({ [MAIN]: [] });
  });
});

describe('small helpers', () => {
  it('names a session by its name, else its directory, else its id', () => {
    expect(sessionTitle({ id: 'abcdef123456', name: ' refactor ', directory: '/w/proj' })).toBe('refactor');
    expect(sessionTitle({ id: 'abcdef123456', name: '', directory: '/w/proj/' })).toBe('proj');
    expect(sessionTitle({ id: 'abcdef123456', name: '', directory: '' })).toBe('abcdef12');
  });

  it('summarises the honesty state', () => {
    expect(honestyChip(null)).toEqual({ kind: 'off', label: '' });
    expect(honestyChip({ mode: 'off' }).kind).toBe('off');
    expect(honestyChip({ mode: 'enforce', clean: true, baseline: 'abc' })).toEqual({ kind: 'clean', label: 'Honesty: clean' });
    expect(honestyChip({ mode: 'enforce', review_running: true }).kind).toBe('review');
    expect(honestyChip({ mode: 'warn', baseline: 'abc', marks: [{ id: 1, path: 'a', reason: 'r' }], unreviewed_count: 3 })).toEqual({
      kind: 'open',
      label: 'Honesty: 1 mark, 3 unreviewed',
    });
    expect(honestyChip({ mode: 'warn', unreviewed_all: true, review_required: true }).label).toBe(
      'Honesty: all files unreviewed, no baseline, review required',
    );
  });

  it('passes only answered side questions on as history', () => {
    const s = applyEvents(initialViewState(), [
      ev('ui.sideAsk', { id: 1, question: 'a?' }),
      ev('ui.sideAsk', { id: 2, question: 'b?' }),
      ev('ui.sideAsk', { id: 3, question: 'c?' }),
      ev('ui.sideAnswer', { id: 1, answer: 'A' }),
      ev('ui.sideAnswer', { id: 2, error: 'failed' }),
    ]);
    expect(sideHistory(s)).toEqual([{ question: 'a?', answer: 'A' }]);
  });
});
