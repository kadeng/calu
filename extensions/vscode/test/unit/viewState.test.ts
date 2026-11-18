// The chat view's reducer: session events, the extension's own events, malformed input.
import { describe, expect, it } from 'vitest';
import {
  MAIN,
  MAX_IMAGE_DATA,
  MAX_ITEMS_PER_AGENT,
  applyEvent,
  applyEvents,
  honestyStoredNote,
  initialViewState,
  isAttached,
  isKnownFile,
  messageTarget,
  openEscalationCount,
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

describe('applyEvent', () => {
  it('does not mutate the input state', () => {
    const s0 = initialViewState();
    const frozen = JSON.stringify(s0);
    applyEvents(s0, [ev('assistant.text', { text: 'hi' }), ev('session.state', { state: 'idle' })]);
    expect(JSON.stringify(s0)).toBe(frozen);
  });

  it('ignores unknown and malformed events', () => {
    const s0 = initialViewState();
    expect(applyEvent(s0, ev('nope'))).toBe(s0);
    expect(applyEvent(s0, ev('assistant.text', { text: 3 }))).toBe(s0);
    expect(applyEvent(s0, ev('tool.use', {}))).toBe(s0);
    expect(applyEvent(s0, ev('session.state', {}))).toBe(s0);
    expect(applyEvent(s0, ev('escalation.resolved', { escalation_id: 'x', decision: 'allow' }))).toBe(s0);
    expect(applyEvent(s0, { method: 'tool.use', params: null as unknown as Record<string, unknown> })).toBe(s0);
  });

  it('tracks bridge status, session state, sequence numbers and messages', () => {
    const s = applyEvents(initialViewState(), [
      ev('bridge.status', { status: 'running', version: '0.9' }),
      ev('session.state', { state: 'idle', session_id: 'C1', seq: 3 }),
      ev('user.input', { text: 'hello', seq: 4 }),
      ev('assistant.text', { agent_id: null, text: 'Hi **there**', seq: 5 }),
      ev('turn.result', { subtype: 'success', cost_usd: 0.01, num_turns: 2, background_tasks: 1, seq: 6 }),
      ev('ui.notice', { level: 'info', text: 'x', seq: 99 }),
    ]);
    expect(s.bridge).toEqual({ status: 'running', version: '0.9' });
    expect(s.session).toMatchObject({ state: 'idle', claudeSessionId: 'C1', backgroundTasks: 1 });
    expect(s.usage).toMatchObject({ costUsd: 0.01, numTurns: 2 });
    expect(s.lastSeq).toBe(6);
    expect(s.items[MAIN].map((i) => i.kind)).toEqual(['user', 'assistant', 'turn', 'notice']);
    expect(s.items[MAIN][2]).toMatchObject({ kind: 'turn', backgroundTasks: 1, costUsd: 0.01 });
    expect(new Set(s.items[MAIN].map((i) => i.id)).size).toBe(4);
  });

  it('merges tool.use, check.result, edit.diff and tool.result by tool_use_id', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 't1', name: 'Edit', input: { file_path: 'a.py' } }),
      ev('edit.diff', { tool_use_id: 't1', path: 'a.py', before: 'a', after: 'b' }),
      ev('check.result', { tool_use_id: 't1', tool_name: 'Edit', route: 'edit', decision: 'PASS', reason: '', step_summary: 'Edits a.py' }),
      ev('tool.result', { tool_use_id: 't1', content: 'ok', is_error: false }),
    ]);
    expect(s.items[MAIN]).toHaveLength(1);
    expect(s.tools.t1).toEqual({
      toolUseId: 't1',
      agentKey: MAIN,
      name: 'Edit',
      input: { file_path: 'a.py' },
      diffPath: 'a.py',
      editRange: null,
      gitHead: null,
      check: { route: 'edit', decision: 'PASS', reason: '', summary: 'Edits a.py', comment: '', checkerRoute: 'edit' },
      result: { content: 'ok', isError: false },
    });
  });

  it('creates a placeholder when check.result comes first', () => {
    const s = applyEvent(initialViewState(), ev('check.result', { tool_use_id: 't9', tool_name: 'Bash', route: 'command', decision: 'DENY' }));
    expect(s.tools.t9.name).toBe('Bash');
    expect(s.items[MAIN]).toEqual([{ id: 1, kind: 'tool', toolUseId: 't9' }]);
  });

  it('routes subagent events into per-agent lists', () => {
    const s = applyEvents(initialViewState(), [
      ev('agents.update', { agents: [{ agent_id: 'A1', description: 'worker', background: true, status: 'running' }] }),
      ev('assistant.text', { agent_id: 'A1', text: 'sub says' }),
      ev('tool.use', { agent_id: 'A1', tool_use_id: 't2', name: 'Bash', input: { command: 'ls' } }),
    ]);
    expect(s.items.A1.map((i) => i.kind)).toEqual(['assistant', 'tool']);
    expect(s.items[MAIN]).toEqual([]);
    expect(messageTarget(s, MAIN)).toEqual({ to: 'main', label: 'Claude (main agent)' });
    expect(messageTarget(s, 'A1')).toEqual({ to: 'A1', label: 'subagent "worker"' });
    expect(messageTarget(s, 'no-such-tab').to).toBe('main');
    const done = applyEvent(s, ev('agents.update', { agents: [{ agent_id: 'A1', background: true, status: 'completed' }] }));
    const finished = messageTarget(done, 'A1');
    expect(finished.to).toBeNull();
    expect('reason' in finished && finished.reason).toMatch(/has finished/);
    const fg = applyEvent(s, ev('agents.update', { agents: [{ agent_id: 'A1', background: false, status: 'running' }] }));
    expect(messageTarget(fg, 'A1').to).toBeNull();
    // The tab of a finished subagent stays readable.
    expect(done.items.A1).toHaveLength(2);
  });

  it('shows only warning and error logs, and ui notices', () => {
    const s = applyEvents(initialViewState(), [
      ev('log', { level: 'info', message: 'quiet' }),
      ev('log', { level: 'error', message: 'loud' }),
      ev('ui.notice', { level: 'error', text: 'request failed' }),
    ]);
    expect(s.items[MAIN]).toMatchObject([
      { kind: 'notice', level: 'error', text: 'loud' },
      { kind: 'notice', level: 'error', text: 'request failed' },
    ]);
  });

  it('caps list length', () => {
    const events = Array.from({ length: MAX_ITEMS_PER_AGENT + 5 }, (_, i) => ev('assistant.text', { text: `m${i}` }));
    const s = applyEvents(initialViewState(), events);
    expect(s.items[MAIN]).toHaveLength(MAX_ITEMS_PER_AGENT);
    expect(s.items[MAIN][0]).toMatchObject({ text: 'm5' });
  });

  it('ui.clear drops the transcript but keeps the session', () => {
    const s = applyEvents(initialViewState(), [
      ev('session.state', { state: 'idle' }),
      ev('assistant.text', { text: 'x' }),
      ev('ui.sideAsk', { id: 1, question: 'q' }),
      ev('ui.clear'),
    ]);
    expect(s.items).toEqual({ [MAIN]: [] });
    expect(s.side).toEqual([]);
    expect(s.session.state).toBe('idle');
  });

  it('stores ids such as __proto__ as ordinary keys', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: '__proto__', name: 'Bash', input: { command: 'ls', __proto__: 'x' } }),
      ev('assistant.text', { agent_id: 'constructor', text: 'x' }),
    ]);
    expect(Object.keys(s.tools)).toEqual(['__proto__']);
    expect(({} as Record<string, unknown>).name).toBeUndefined();
    expect(s.items.constructor).toHaveLength(1);
  });
});

describe('checker messages from check.result', () => {
  const check = (extra: Record<string, unknown>) =>
    ev('check.result', { tool_use_id: 't1', tool_name: 'Bash', route: 'command', decision: 'PASS', reason: '', step_summary: 's', comment: '', ...extra });

  it('adds no message for a plain PASS', () => {
    const s = applyEvent(initialViewState(), check({}));
    expect(s.items[MAIN].map((i) => i.kind)).toEqual(['tool']);
  });

  it('posts a remark made while approving, after the tool item', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 't1', name: 'Bash', input: {} }),
      check({ comment: 'Fine, but add a docstring next time.' }),
    ]);
    expect(s.items[MAIN]).toEqual([
      { id: 1, kind: 'tool', toolUseId: 't1' },
      { id: 2, kind: 'checker', label: 'Command checker', checkerRoute: 'command', decision: 'PASS', text: 'Fine, but add a docstring next time.', toolUseId: 't1' },
    ]);
    expect(s.tools.t1.check?.comment).toBe('Fine, but add a docstring next time.');
  });

  it('posts DENY reasons, labelled by route, into the calling agent list', () => {
    const s = applyEvent(initialViewState(), check({ agent_id: 'A1', route: 'changeset', decision: 'DENY', reason: 'Breaks the style rules.' }));
    expect(s.items.A1.at(-1)).toMatchObject({ kind: 'checker', label: 'Changeset checker', checkerRoute: 'changeset', decision: 'DENY' });
    expect(s.items[MAIN]).toEqual([]);
  });

  it('labels decisions of the rules and ignores ESCALATE reasons', () => {
    const denied = applyEvent(initialViewState(), check({ route: 'deny', decision: 'DENY', reason: 'rule' }));
    expect(denied.items[MAIN].at(-1)).toMatchObject({ kind: 'checker', label: 'Rules', checkerRoute: '' });
    const escalated = applyEvent(initialViewState(), check({ decision: 'ESCALATE', reason: 'ask the human' }));
    expect(escalated.items[MAIN].map((i) => i.kind)).toEqual(['tool']);
  });
});

describe('assistant.delta streaming', () => {
  it('appends fragments to one item and finalises it with assistant.text', () => {
    let s = applyEvents(initialViewState(), [
      ev('assistant.delta', { agent_id: null, message_id: 'm1', text: 'Hel' }),
      ev('assistant.delta', { agent_id: null, message_id: 'm1', text: 'lo **wo' }),
    ]);
    expect(s.items[MAIN]).toEqual([{ id: 1, kind: 'assistant', text: 'Hello **wo', messageId: 'm1', streaming: true }]);
    s = applyEvent(s, ev('assistant.text', { agent_id: null, message_id: 'm1', text: 'Hello **world**' }));
    expect(s.items[MAIN]).toEqual([{ id: 1, kind: 'assistant', text: 'Hello **world**', messageId: 'm1', streaming: false }]);
  });

  it('keeps thinking and text of one message apart', () => {
    const s = applyEvents(initialViewState(), [
      ev('assistant.delta', { message_id: 'm1', text: 'hm', thinking: true }),
      ev('assistant.delta', { message_id: 'm1', text: 'a' }),
      ev('assistant.thinking', { message_id: 'm1', text: 'hmm' }),
      ev('assistant.delta', { agent_id: 'A', message_id: 'm1', text: 'c' }),
    ]);
    expect(s.items[MAIN]).toMatchObject([
      { kind: 'thinking', text: 'hmm', streaming: false },
      { kind: 'assistant', text: 'a', streaming: true },
    ]);
    expect(s.items.A).toMatchObject([{ text: 'c' }]);
    // A turn end finishes what still streams.
    expect(applyEvent(s, ev('turn.result', {})).items[MAIN][1]).toMatchObject({ streaming: false });
  });

  it('ignores deltas without message_id', () => {
    const s0 = initialViewState();
    expect(applyEvent(s0, ev('assistant.delta', { text: 'x' }))).toBe(s0);
  });
});

describe('user input', () => {
  it('keeps the source and the images', () => {
    // Larger than any image a session accepts: not kept.
    const big = 'A'.repeat(MAX_IMAGE_DATA + 1);
    const s = applyEvents(initialViewState(), [
      ev('user.input', { text: 'Fix it.', images: [{ data: 'aGk=', media_type: 'image/png' }, { data: big, media_type: 'image/jpeg' }, { nope: 1 }] }),
      ev('user.input', { text: '<cross-session-message>push</cross-session-message>', source: 'cross_session' }),
      ev('user.input', { text: 'A background task finished.', source: 'system' }),
      ev('user.input', { text: 'x', source: 'martian' }),
    ]);
    expect(s.items[MAIN]).toMatchObject([
      {
        kind: 'user',
        source: 'user',
        images: [
          { mediaType: 'image/png', data: 'aGk=', omitted: false },
          { mediaType: 'image/jpeg', data: '', omitted: true },
        ],
      },
      { kind: 'user', source: 'cross_session', images: [] },
      { kind: 'user', source: 'system' },
      { kind: 'user', source: 'user' },
    ]);
  });
});

describe('chat.queue', () => {
  it('replaces the queue, validates entries, and clears it when the bridge stops', () => {
    const s = applyEvent(initialViewState(), ev('chat.queue', { messages: [{ id: 'q1', text: 'hi', delivery: 'queued', to: 'main' }, { id: 'q2' }] }));
    expect(s.queue).toEqual([{ id: 'q1', text: 'hi', delivery: 'queued', to: 'main' }]);
    const injected = applyEvent(s, ev('chat.queue', { messages: [{ id: 'q3', text: 'now', delivery: 'inject', to: 'A1' }] }));
    expect(injected.queue).toEqual([{ id: 'q3', text: 'now', delivery: 'inject', to: 'A1' }]);
    expect(applyEvent(injected, ev('ui.clear')).queue).toEqual([]);
    expect(applyEvent(injected, ev('bridge.status', { status: 'exited' })).queue).toEqual([]);
  });
});

describe('header state', () => {
  it('follows auto-escalation, honesty, usage limits and the model', () => {
    const s = applyEvents(initialViewState(), [
      ev('auto_escalate.changed', { settings: { edits: true, commands: 'yes' } }),
      ev('honesty.update', { mode: 'enforce', clean: false, marks: [{ id: 3, path: 'a.py', reason: 'stub' }, { path: 'no id' }], unreviewed: ['b.py', 7], unreviewed_count: 1 }),
      ev('usage.update', { rate_limits: [{ type: 'five_hour', status: 'allowed_warning', utilization: 0.8, resets_at: '2026-11-18T12:00:00Z' }, 'junk'] }),
      ev('ui.model', { model: 'sonnet' }),
      ev('ui.usage', { context: { tokens: 50, max_tokens: 200, model: 'm' }, cost_usd: 1.5, num_turns: 4 }),
    ]);
    expect(s.autoEscalate).toEqual({ commands: false, edits: true, subagents: false });
    expect(s.honesty).toMatchObject({
      mode: 'enforce',
      clean: false,
      marks: [{ id: 3, path: 'a.py', reason: 'stub', symbol: '', opened_by: null }],
      unreviewed: ['b.py'],
      unreviewed_count: 1,
      review_running: false,
    });
    expect(s.usage.rateLimits).toEqual([{ type: 'five_hour', status: 'allowed_warning', utilization: 0.8, resets_at: '2026-11-18T12:00:00Z' }]);
    expect(s.usage.context).toEqual({ tokens: 50, maxTokens: 200, percent: 25, model: 'm' });
    expect(s.usage).toMatchObject({ costUsd: 1.5, numTurns: 4 });
    expect(s.session.model).toBe('sonnet');
    expect(applyEvent(s, ev('ui.model', { model: null })).session.model).toBeNull();
    expect(applyEvent(s, ev('honesty.update', [] as unknown as Record<string, unknown>)).honesty).toBeNull();
  });

  it('keeps the session list, the server help and the hints', () => {
    const s = applyEvents(initialViewState(), [
      ev('ui.sessions', {
        running: [
          { id: 's-other', directory: '/x', state: 'idle', open_escalations: 0, here: false, stale: false },
          { id: 's-here', name: 'refactor', directory: '/w', state: 'escalated', open_escalations: 2, here: true, stale: false },
          { nope: true },
        ],
      }),
      ev('ui.serverHelp', { command: "tmux new-session -A -s calu 'calu serve'", tool: 'tmux', detach: 'Ctrl+B, then D', note: 'n' }),
      ev('ui.hints', { hints: [{ id: 'no-session', level: 'info', message: 'm', action: 'newSession', actionLabel: 'New session' }, { id: 5 }] }),
    ]);
    expect(s.sessions.loaded).toBe(true);
    expect(s.sessions.list.map((x) => x.id)).toEqual(['s-here', 's-other']);
    expect(s.sessions.list[0]).toEqual({ id: 's-here', name: 'refactor', directory: '/w', state: 'escalated', openEscalations: 2, here: true, stale: false });
    expect(s.serverHelp?.tool).toBe('tmux');
    expect(s.hints).toHaveLength(1);
    // An error keeps the last list; an empty ui.serverHelp clears the help.
    const failed = applyEvent(s, ev('ui.sessions', { error: 'the database is not reachable' }));
    expect(failed.sessions).toMatchObject({ error: 'the database is not reachable' });
    expect(failed.sessions.list).toHaveLength(2);
    expect(applyEvent(s, ev('ui.serverHelp', {})).serverHelp).toBeNull();
  });

  it('records side questions and their answers', () => {
    let s = applyEvents(initialViewState(), [ev('ui.sideAsk', { id: 1, question: 'why?' }), ev('ui.sideAsk', { id: 2, question: 'how?' })]);
    s = applyEvents(s, [ev('ui.sideAnswer', { id: 1, answer: 'because' }), ev('ui.sideAnswer', { id: 2, error: 'no session' })]);
    expect(s.side).toEqual([
      { id: 1, question: 'why?', answer: 'because' },
      { id: 2, question: 'how?', error: 'no session' },
    ]);
  });
});

describe('link and files', () => {
  it('knows when requests can reach a session', () => {
    const running = applyEvent(initialViewState(), ev('bridge.status', { status: 'running' }));
    expect(isAttached(running)).toBe(false);
    const attached = applyEvent(running, ev('ui.attached', { session_id: 's1', snapshot: { state: 'idle', workspace: '/w' }, events: [] }));
    expect(isAttached(attached)).toBe(true);
    expect(isAttached(applyEvent(attached, ev('ui.link', { state: 'reconnecting', message: 'retrying' })))).toBe(true);
    const lost = applyEvent(attached, ev('ui.link', { state: 'lost', message: 'gone' }));
    expect(isAttached(lost)).toBe(false);
    expect(lost.link).toEqual({ status: 'lost', message: 'gone' });
    expect(lost.sessions.attachedId).toBeNull();
    expect(applyEvent(attached, ev('ui.link', { state: 'bogus' }))).toBe(attached);
    const detached = applyEvent(attached, ev('ui.detached'));
    expect(detached.link.status).toBe('detached');
    expect(detached.session.sessionId).toBeNull();
    expect(applyEvent(attached, ev('bridge.status', { status: 'exited', message: 'gone' })).link.status).toBe('detached');
  });

  it('shows a message for a subagent in its tab, and in Main only that it was sent', () => {
    const s = applyEvents(initialViewState(), [
      ev('agents.update', { agents: [{ agent_id: 'a1', description: 'sleeper', background: true, status: 'running' }] }),
      ev('user.input', { text: 'start', uuid: 'u1' }),
      ev('user.input', { text: 'psst', to: 'a1', uuid: 'u2' }),
      // Not the user's words: `to` is ignored.
      ev('user.input', { text: 'task finished', source: 'system', to: 'a1' }),
    ]);
    expect(s.items[MAIN].map((i) => i.kind)).toEqual(['user', 'relay', 'user']);
    expect(s.items[MAIN][1]).toMatchObject({ kind: 'relay', to: 'a1' });
    expect(s.items.a1).toEqual([expect.objectContaining({ kind: 'user', text: 'psst', to: 'a1', uuid: 'u2', source: 'user' })]);
    expect(JSON.stringify(s.items[MAIN])).not.toContain('psst');
    expect(new Set([...s.items[MAIN], ...s.items.a1].map((i) => i.id)).size).toBe(4);
  });

  it('tracks a waiting session request and the stored honesty mode', () => {
    let s = applyEvent(initialViewState(), ev('bridge.status', { status: 'running' }));
    s = applyEvent(s, ev('ui.starting', { title: 'Starting a session', slow: false }));
    expect(s.starting).toEqual({ title: 'Starting a session', slow: false });
    expect(applyEvent(s, ev('ui.starting', { title: 'Starting a session', slow: true })).starting?.slow).toBe(true);
    expect(applyEvent(s, ev('ui.starting', {})).starting).toBeNull();
    expect(applyEvent(s, ev('bridge.status', { status: 'exited' })).starting).toBeNull();
    expect(applyEvent(s, ev('ui.attached', { session_id: 's1', snapshot: { state: 'idle', workspace: '/w' }, events: [] })).starting).toBeNull();

    expect(honestyStoredNote(null, null)).toBe('');
    expect(honestyStoredNote(null, 'off')).toBe('');
    expect(honestyStoredNote(null, 'track')).toBe('stored: track');
    s = applyEvents(s, [ev('ui.honestyStored', { mode: 'track' }), ev('honesty.update', { mode: 'off' })]);
    expect(honestyStoredNote(s.honesty, s.honestyStored)).toBe('stored: track');
    s = applyEvent(s, ev('honesty.update', { mode: 'track', clean: true }));
    expect(honestyStoredNote(s.honesty, s.honestyStored)).toBe('');
    expect(applyEvent(s, ev('ui.honestyStored', { mode: 7 })).honestyStored).toBeNull();
  });

  it('opens only files that a tool call listed', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 't', name: 'Bash', input: { command: 'cat a.py' } }),
      ev('tool.files', { tool_use_id: 't', files: [{ raw: 'a.py', path: '/w/a.py' }, { raw: '', path: '/w/b.py' }] }),
    ]);
    expect(s.tools.t.files).toEqual([{ raw: 'a.py', path: '/w/a.py' }]);
    expect(isKnownFile(s, '/w/a.py')).toBe(true);
    expect(isKnownFile(s, '/etc/passwd')).toBe(false);
  });

  it('counts open escalations', () => {
    const s = applyEvent(initialViewState(), ev('escalation.request', { escalation_id: 'e', tool_name: 'Bash', tool_input: {}, reason: '', rendered: '' }));
    expect(openEscalationCount(s)).toBe(1);
  });
});
