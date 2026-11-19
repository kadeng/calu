// Steps calu holds back while a decision is open (`step.held`, PLAN D237 (6)), the tab bar's
// folding of finished subagents, and the session dropdown's second line.
import { describe, expect, it } from 'vitest';
import { foldTabs, heldByAgent, heldTools, sessionMenu, tabList, type TabView } from '../../src/shared/chatModel';
import { applyEvents, initialViewState, type ViewEvent, type ViewState } from '../../src/shared/viewState';

/** Build a state from events. */
function stateOf(...events: ViewEvent[]): ViewState {
  return applyEvents(initialViewState(), events);
}

const attached: ViewEvent = {
  method: 'ui.attached',
  params: { session_id: 's1', initialize: { workspace: '/w' }, snapshot: { state: 'running', claude_session_id: 'c-1' }, events: [] },
};
const use = (id: string, agent: string | null, name = 'Bash'): ViewEvent => ({
  method: 'tool.use',
  params: { tool_use_id: id, name, input: { command: `job ${id}` }, agent_id: agent },
});
const check = (id: string, agent: string | null, decision = 'PASS'): ViewEvent => ({
  method: 'check.result',
  params: { tool_use_id: id, tool_name: 'Bash', route: 'command', decision, agent_id: agent },
});
const held = (id: string, agent: string | null, on: boolean): ViewEvent => ({
  method: 'step.held',
  params: { tool_use_id: id, agent_id: agent, tool_name: 'Bash', held: on },
});
const escalate = (id: string, agent: string | null, questions = false): ViewEvent => ({
  method: 'escalation.request',
  params: {
    escalation_id: `e-${id}`,
    tool_use_id: id,
    tool_name: questions ? 'AskUserQuestion' : 'Bash',
    tool_input: questions ? {} : { command: 'deploy' },
    ...(questions ? { questions: [{ question: 'Which?', options: [{ label: 'a' }] }] } : {}),
    agent_id: agent,
  },
});
const agents: ViewEvent = {
  method: 'agents.update',
  params: {
    agents: [
      { agent_id: 'A', description: 'lint', status: 'running' },
      { agent_id: 'B', description: 'deploy', status: 'running' },
    ],
  },
};

describe('held steps', () => {
  it('follows step.held: begins, ends, also for an approved step', () => {
    const s = stateOf(attached, agents, use('a1', 'A'), check('a1', 'A'), use('b1', 'B'), escalate('b1', 'B'), held('a1', 'A', true));
    expect([...heldTools(s)]).toEqual(['a1']);
    expect(heldByAgent(s)).toEqual({ A: 1 });
    const after = applyEvents(s, [held('a1', 'A', false), { method: 'escalation.resolved', params: { escalation_id: 'e-b1', decision: 'allow' } }]);
    expect(heldTools(after).size).toBe(0);
  });

  it('derives a hold for a session without step.held: an unchecked call while a decision is open', () => {
    const s = stateOf(attached, agents, use('a1', 'A'), use('b1', 'B'), escalate('b1', 'B'));
    expect([...heldTools(s)]).toEqual(['a1']);
    // The escalated call itself waits for the user, not for calu.
    expect(heldTools(s).has('b1')).toBe(false);
  });

  it('holds nothing for a question, a running launch, a call with a result, or a checked call without the event', () => {
    const s = stateOf(
      attached,
      use('A', null, 'Agent'),
      agents,
      use('q', null, 'AskUserQuestion'),
      escalate('q', null, true),
      use('a1', 'A'),
    );
    expect(heldTools(s).size).toBe(0);
    const withStep = applyEvents(s, [use('b1', 'B'), escalate('b1', 'B'), use('a2', 'A'), check('a2', 'A'), use('a3', 'A')]);
    expect([...heldTools(withStep)].sort()).toEqual(['a1', 'a3']);
    const done = applyEvents(withStep, [{ method: 'tool.result', params: { tool_use_id: 'a1', content: 'ok' } }]);
    expect([...heldTools(done)]).toEqual(['a3']);
  });

  it('forgets held marks with the transcript', () => {
    const s = stateOf(attached, use('a1', null), held('a1', null, true));
    expect(heldTools(s).size).toBe(1);
    expect(heldTools(applyEvents(s, [{ method: 'ui.clear', params: {} }])).size).toBe(0);
  });

  it('counts held steps per tab', () => {
    const s = stateOf(attached, agents, use('a1', 'A'), use('b1', 'B'), escalate('b1', 'B'), held('a1', 'A', true));
    const tabs = tabList(s, 'main', {});
    expect(tabs.map((t) => [t.key, t.held, t.openEscalations])).toEqual([
      ['main', 0, 0],
      ['A', 1, 0],
      ['B', 0, 1],
    ]);
  });
});

describe('folded tabs', () => {
  const tab = (key: string, status: TabView['status'], more: Partial<TabView> = {}): TabView => ({
    key,
    label: key,
    status,
    background: false,
    openEscalations: 0,
    held: 0,
    attention: false,
    ...more,
  });

  it('keeps every tab while there are few subagents', () => {
    const tabs = [tab('main', ''), tab('a', 'completed'), tab('b', 'running')];
    expect(foldTabs(tabs, 'main')).toEqual({ shown: tabs, folded: [] });
  });

  it('folds finished subagents beyond the limit, but not the visible, waiting or held ones', () => {
    const tabs = [
      tab('main', ''),
      tab('a', 'completed'),
      tab('b', 'running'),
      tab('c', 'failed'),
      tab('d', 'completed', { openEscalations: 1 }),
      tab('e', 'completed'),
      tab('f', 'stopped'),
      tab('g', 'completed', { held: 1 }),
    ];
    const { shown, folded } = foldTabs(tabs, 'e', 6);
    expect(shown.map((t) => t.key)).toEqual(['main', 'b', 'd', 'e', 'g']);
    expect(folded.map((t) => t.key)).toEqual(['a', 'c', 'f']);
  });
});

describe('session dropdown rows', () => {
  it('says when a session started and tells rows of the same name apart by their id', () => {
    const now = Date.parse('2026-11-19T10:00:00Z');
    const s = stateOf({
      method: 'ui.sessions',
      params: {
        running: [
          { id: 'aaaaaaaa-1', name: '', directory: '/home/me/demo', state: 'idle', open_escalations: 0, here: true, stale: false, started_at: '2026-11-19T09:55:00Z' },
          { id: 'bbbbbbbb-2', name: '', directory: '/home/me/demo', state: 'idle', open_escalations: 0, here: true, stale: false, started_at: '2026-11-19T07:00:00Z' },
          { id: 'cccccccc-3', name: 'solo', directory: '/home/me/demo', state: 'idle', open_escalations: 0, here: true, stale: false },
        ],
      },
    });
    const rows = sessionMenu(s, now).filter((e) => e.kind === 'session');
    expect(rows.map((r) => (r.kind === 'session' ? r.directory : ''))).toEqual([
      '~/demo · started 5 min ago · aaaaaaaa',
      '~/demo · started 3 h ago · bbbbbbbb',
      '~/demo',
    ]);
  });
});
