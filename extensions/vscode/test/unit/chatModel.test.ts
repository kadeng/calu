// Pure helpers of the chat webview: texts, session dropdown, tabs, setup cards, usage, links, images.
import { describe, expect, it } from 'vitest';
import {
  buildFileIndex,
  checkImage,
  decisionKind,
  formatTokens,
  imageSrc,
  linkMessage,
  lookupFile,
  middleTruncate,
  newestItemIds,
  otherOpenEscalations,
  parseImageDataUrl,
  previewLines,
  rateLimitLine,
  sessionActionMessage,
  sessionLabel,
  sessionMenu,
  setupCards,
  shortDirectory,
  showsErrorChip,
  splitByFiles,
  tabList,
  toolHead,
  turnCost,
  turnLabel,
  usageCostLine,
  usageMeter,
} from '../../src/shared/chatModel';
import { MAX_IMAGES } from '../../src/shared/messages';
import { applyEvents, initialViewState, type ViewEvent, type ViewState } from '../../src/shared/viewState';

/** Build a state from events. */
function stateOf(...events: ViewEvent[]): ViewState {
  return applyEvents(initialViewState(), events);
}

const running: ViewEvent = { method: 'bridge.status', params: { status: 'running' } };
const sessions: ViewEvent = {
  method: 'ui.sessions',
  params: {
    running: [
      { id: 'far-1', name: '', directory: '/srv/other/project', state: 'idle', open_escalations: 0, here: false, stale: true },
      { id: 'here-1', name: 'refactor', directory: '/home/me/work/app', state: 'running', open_escalations: 2, here: true, stale: false },
    ],
  },
};
const attached: ViewEvent = {
  method: 'ui.attached',
  params: { session_id: 'here-1', initialize: { workspace: '/home/me/work/app' }, snapshot: { state: 'idle', claude_session_id: 'c-1' }, events: [] },
};

describe('texts', () => {
  it('cuts long texts in the middle and keeps both ends', () => {
    const cut = middleTruncate('/home/me/project/src/module/file.ts', 20);
    expect(cut).toHaveLength(20);
    expect(cut.startsWith('/home/me/')).toBe(true);
    expect(cut.endsWith('file.ts')).toBe(true);
    expect(middleTruncate('short', 20)).toBe('short');
    expect(middleTruncate('abcdefghij', 1)).toHaveLength(5);
  });

  it('shortens the home directory', () => {
    expect(shortDirectory('/home/me/projects/calu')).toBe('~/projects/calu');
    expect(shortDirectory('/Users/me')).toBe('~');
    expect(shortDirectory('/srv/x')).toBe('/srv/x');
  });

  it('previews the head or the tail of a text', () => {
    expect(previewLines('a\nb\nc', 2, 'tail')).toEqual({ text: 'b\nc', truncated: true, lines: 3 });
    expect(previewLines('a\nb\nc', 2, 'head')).toEqual({ text: 'a\nb', truncated: true, lines: 3 });
    expect(previewLines('a\nb\n', 2, 'head')).toEqual({ text: 'a\nb\n', truncated: false, lines: 2 });
  });

  it('describes a tool input in one line', () => {
    expect(toolHead({ input: { command: 'ls\n  -la' } })).toEqual({ text: 'ls -la', isPath: false });
    expect(toolHead({ input: { file_path: '/w/a.py' } })).toEqual({ text: '/w/a.py', isPath: true });
    expect(toolHead({ input: {}, diffPath: 'b.py' })).toEqual({ text: 'b.py', isPath: true });
    expect(toolHead({ input: { pattern: 'TODO' } }).text).toBe('TODO');
    expect(toolHead({ input: { questions: [{ question: 'Which\n runner?', options: [] }] } })).toEqual({ text: 'Which runner?', isPath: false });
    expect(toolHead({ input: { questions: 'nope' } }).text).toBe('{"questions":"nope"}');
  });

  it('words the outcome of a turn', () => {
    expect(turnLabel({ subtype: 'success', isError: false })).toBe('turn done');
    expect(turnLabel({ subtype: 'interrupted', isError: false })).toBe('turn interrupted');
    expect(turnLabel({ subtype: 'error_max_turns', isError: true })).toBe('turn ended: error max turns');
    expect(turnLabel({ subtype: 'success', isError: true })).toBe('turn ended: success');
    expect(turnLabel({ subtype: '', isError: true })).toBe('turn ended: error');
  });

  it('words the cost of a turn next to the cost of the session', () => {
    expect(turnCost({ costUsd: 0.005, costDelta: 0.0003 })).toBe('$0.0003 · session $0.0050');
    expect(turnCost({ costUsd: 0.0027, costDelta: null })).toBe('session $0.0027');
    expect(turnCost({ costUsd: 0.0027 })).toBe('session $0.0027');
    expect(turnCost({ costUsd: null, costDelta: 0.1 })).toBe('');
    expect(usageCostLine(0.005, 2)).toBe('Cost $0.0050 (Claude Code, whole session) · last turn: 2 model calls');
    expect(usageCostLine(0.005, 1)).toBe('Cost $0.0050 (Claude Code, whole session) · last turn: 1 model call');
    expect(usageCostLine(null, 0)).toBe('Cost unknown');
  });

  it('shows the error chip only where the decision chip does not already say it', () => {
    const check = (decision: string) => ({ route: 'command', decision, reason: '', summary: '', comment: '', checkerRoute: 'command' as const });
    const failed = { content: 'x', isError: true };
    expect(showsErrorChip({ name: 'Bash', check: check('PASS'), result: failed })).toBe(true);
    expect(showsErrorChip({ name: 'Bash', check: check('ESCALATE'), result: failed })).toBe(true);
    expect(showsErrorChip({ name: 'Bash', check: check('DENY'), result: failed })).toBe(false);
    expect(showsErrorChip({ name: 'AskUserQuestion', check: check('ESCALATE'), result: failed })).toBe(false);
    expect(showsErrorChip({ name: 'Bash', check: check('PASS'), result: { content: 'x', isError: false } })).toBe(false);
    expect(showsErrorChip({ name: 'Bash' })).toBe(false);
    expect(toolHead({ input: {} }).text).toBe('');
    expect(toolHead({ input: { n: 1 } }).text).toBe('{"n":1}');
  });

  it('splits a label into text and file links, longest match first', () => {
    const files = [
      { raw: 'a.py', path: '/w/a.py' },
      { raw: 'src/a.py', path: '/w/src/a.py' },
    ];
    expect(splitByFiles('cat src/a.py a.py', files)).toEqual([
      { text: 'cat ' },
      { text: 'src/a.py', file: 1 },
      { text: ' ' },
      { text: 'a.py', file: 0 },
    ]);
    expect(splitByFiles('ls', undefined)).toEqual([{ text: 'ls' }]);
  });

  it('names the look of a decision', () => {
    expect(['PASS', 'allow', 'DENY', 'ESCALATE', undefined, 'other'].map(decisionKind)).toEqual(['pass', 'allow', 'deny', 'escalate', 'pending', 'pending']);
  });
});

describe('session dropdown', () => {
  it('lists workspace sessions first and marks the attached one', () => {
    const entries = sessionMenu(stateOf(running, sessions, attached));
    expect(entries.map((e) => (e.kind === 'session' ? e.id : e.kind === 'action' ? e.id : e.label))).toEqual([
      'This workspace', 'here-1', 'Other sessions', 'far-1', 'new', 'resume', 'fork', 'rename', 'refresh', 'detach',
    ]);
    const here = entries.find((e) => e.kind === 'session' && e.id === 'here-1');
    expect(here).toMatchObject({ title: 'refactor', directory: '~/work/app', attached: true, openEscalations: 2, phase: 'busy' });
    const far = entries.find((e) => e.kind === 'session' && e.id === 'far-1');
    expect(far).toMatchObject({ title: 'project', stale: true, attached: false });
    expect(entries.filter((e) => e.kind === 'action').every((e) => e.kind === 'action' && e.enabled)).toBe(true);
  });

  it('disables what needs a session and says why the list is empty', () => {
    const none = sessionMenu(stateOf(running, { method: 'ui.sessions', params: { running: [] } }));
    expect(none[0]).toEqual({ kind: 'note', label: 'No session is running.' });
    const enabled = Object.fromEntries(none.filter((e) => e.kind === 'action').map((e) => (e.kind === 'action' ? [e.id, e.enabled] : ['', false])));
    expect(enabled).toEqual({ new: true, resume: true, fork: false, rename: false, refresh: true, detach: false });
    expect(sessionMenu(stateOf(running))[0]).toEqual({ kind: 'note', label: 'Loading the sessions…' });
    expect(sessionMenu(stateOf(running, { method: 'ui.sessions', params: { error: 'locked' } }))[0]).toMatchObject({ kind: 'note', label: expect.stringContaining('locked') });
  });

  it('maps actions to messages', () => {
    expect(sessionActionMessage('new', null)).toEqual({ type: 'newSession' });
    expect(sessionActionMessage('resume', null)).toEqual({ type: 'attach' });
    expect(sessionActionMessage('rename', 's1')).toEqual({ type: 'renameSession', sessionId: 's1' });
    expect(sessionActionMessage('fork', 's1')).toEqual({ type: 'forkConversation' });
    expect(sessionActionMessage('detach', null)).toBeUndefined();
  });

  it('labels the button', () => {
    expect(sessionLabel(stateOf())).toMatchObject({ label: 'calu', dot: 'off' });
    expect(sessionLabel(stateOf(running))).toMatchObject({ label: 'No session', dot: 'detached' });
    expect(sessionLabel(stateOf(running, sessions, attached))).toMatchObject({ label: 'refactor', dot: 'idle' });
    const noName = stateOf(running, { ...attached, params: { ...attached.params, session_id: 'zzz' } });
    expect(sessionLabel(noName).label).toBe('app');
    const reconnecting = stateOf(running, sessions, attached, { method: 'ui.link', params: { state: 'reconnecting', message: 'retry' } });
    expect(sessionLabel(reconnecting)).toMatchObject({ dot: 'reconnecting', title: expect.stringContaining('retry') });
  });
});

describe('tabs', () => {
  const withAgent = stateOf(
    running,
    attached,
    { method: 'tool.use', params: { tool_use_id: 'A1', name: 'Agent', input: { description: 'tests', prompt: 'run', run_in_background: true } } },
    { method: 'agents.update', params: { agents: [{ agent_id: 'A1', description: 'tests', background: true, status: 'running' }] } },
    { method: 'assistant.text', params: { text: 'hi', agent_id: 'A1' } },
    { method: 'escalation.request', params: { escalation_id: 'e1', tool_name: 'Bash', tool_input: {}, agent_id: 'A1' } },
  );

  it('has Main and one tab per subagent with attention marks', () => {
    const tabs = tabList(withAgent, 'main', {});
    expect(tabs.map((t) => [t.key, t.label, t.status, t.background])).toEqual([
      ['main', 'Main', '', false],
      ['A1', 'tests', 'running', true],
    ]);
    expect(tabs[1]).toMatchObject({ openEscalations: 1, attention: true });
    expect(tabs[0].attention).toBe(false);
  });

  it('marks unseen output until it was seen, never on the active tab', () => {
    const seen = newestItemIds(withAgent.items);
    const resolved = applyEvents(withAgent, [{ method: 'escalation.resolved', params: { escalation_id: 'e1', decision: 'allow' } }]);
    expect(tabList(resolved, 'main', seen)[1].attention).toBe(false);
    expect(tabList(resolved, 'main', {})[1].attention).toBe(true);
    expect(tabList(resolved, 'A1', {})[1].attention).toBe(false);
    expect(tabList(resolved, 'A1', {})[0].attention).toBe(true);
  });

  it('lists the open escalations of other tabs', () => {
    expect(otherOpenEscalations(withAgent, 'main')).toEqual([{ key: 'e1', tab: 'A1', label: 'tests: Bash' }]);
    expect(otherOpenEscalations(withAgent, 'A1')).toEqual([]);
  });
});

describe('setup cards', () => {
  const hint = (id: string, action: string, extra: object = {}) => ({ id, level: 'info', message: `${id}.`, action, actionLabel: `Do ${id}`, ...extra });

  it('offers one primary action per bridge problem', () => {
    expect(setupCards(stateOf({ method: 'bridge.status', params: { status: 'missing', message: 'not found' } }))[0]).toMatchObject({
      id: 'calu-missing', primary: { label: 'Install calu', message: { type: 'install' } },
    });
    const crashed = setupCards(stateOf({ method: 'bridge.status', params: { status: 'exited', message: 'calu ide exited with code 1.' } }))[0];
    expect(crashed).toMatchObject({ id: 'bridge', level: 'error', message: 'calu ide exited with code 1.', primary: { message: { type: 'restartBridge' } } });
    expect(crashed.secondary).toEqual([{ label: 'Show log', message: { type: 'showLog' } }]);
    expect(setupCards(stateOf({ method: 'bridge.status', params: { status: 'starting' } }))[0].primary).toBeUndefined();
    expect(setupCards(stateOf())[0]).toMatchObject({ id: 'bridge', primary: { label: 'Start' } });
  });

  it('turns the hints into cards: server help, session list, links', () => {
    const state = stateOf(
      running,
      { method: 'ui.serverHelp', params: { command: "tmux new-session -A -s calu 'calu serve'", tool: 'tmux', detach: 'Ctrl+B, then D', note: 'n' } },
      { method: 'ui.hints', params: { hints: [hint('claude-missing', 'openLink', { href: 'https://example.org' }), hint('locked', 'unlock'), hint('no-server', 'startServer'), hint('no-session', 'newSession')] } },
    );
    const cards = setupCards(state);
    expect(cards.map((c) => c.id)).toEqual(['claude-missing', 'locked', 'no-server', 'no-session']);
    expect(cards[0].primary).toEqual({ label: 'Do claude-missing', message: { type: 'openLink', href: 'https://example.org' } });
    expect(cards[1].primary?.message).toEqual({ type: 'unlock' });
    expect(cards[2]).toMatchObject({ serverHelp: true, primary: { label: 'Open terminal with this command', message: { type: 'startServer' } } });
    expect(cards[2].secondary).toEqual([{ label: 'Copy command', message: { type: 'copyServerCommand' } }]);
    expect(cards[3]).toMatchObject({ sessionList: true, primary: { message: { type: 'newSession' } } });
  });

  it('is empty once a session is attached and nothing is missing', () => {
    expect(setupCards(stateOf(running, attached))).toEqual([]);
  });
});

describe('usage', () => {
  it('formats tokens', () => {
    expect([950, 1000, 1250, 84_000, 1_200_000, 12_000_000].map(formatTokens)).toEqual(['950', '1k', '1.3k', '84k', '1.2M', '12M']);
  });

  it('shows percent and tokens, or that nothing is known', () => {
    const usage = { rateLimits: [], costUsd: null, numTurns: 0, context: null };
    expect(usageMeter(usage)).toMatchObject({ percent: null, label: 'ctx –', level: 'unknown' });
    const known = usageMeter({ ...usage, context: { tokens: 84_000, maxTokens: 200_000, percent: 42, model: 'opus' } });
    expect(known).toMatchObject({ percent: 42, label: '42% · 84k', level: 'ok' });
    expect(known.title).toContain('84,000 of 200,000');
    expect(usageMeter({ ...usage, context: { tokens: 190_000, maxTokens: 200_000, percent: 95, model: '' } }).level).toBe('high');
    expect(usageMeter({ ...usage, context: { tokens: 150_000, maxTokens: 200_000, percent: 75, model: '' } }).level).toBe('warn');
  });

  it('describes a rate limit', () => {
    expect(rateLimitLine({ type: 'five_hour', status: 'allowed_warning', utilization: 0.63, resets_at: '2026-11-18T14:30:00Z' })).toBe(
      'five hour: 63% used, allowed warning, resets 2026-11-18 14:30 UTC',
    );
    expect(rateLimitLine({})).toBe('limit: no data');
  });
});

describe('links and files', () => {
  it('sends http(s) and relative links to the host and drops the rest', () => {
    expect(linkMessage('https://example.org/a')).toEqual({ type: 'openLink', href: 'https://example.org/a' });
    expect(linkMessage('src/app.py:12')).toEqual({ type: 'openLink', href: 'src/app.py:12' });
    for (const bad of ['', '#top', 'javascript:alert(1)', 'file:///etc/passwd', 'data:text/html,x', '//evil.example', null, undefined, 'vscode://x']) {
      expect(linkMessage(bad), String(bad)).toBeUndefined();
    }
  });

  it('finds files that tool calls named, with a line', () => {
    const state = stateOf(
      { method: 'tool.use', params: { tool_use_id: 't1', name: 'Bash', input: { command: 'cat src/app.py' } } },
      { method: 'tool.files', params: { tool_use_id: 't1', files: [{ raw: 'src/app.py', path: '/w/src/app.py' }, { raw: 'lib/util.py', path: '/w/lib/util.py' }] } },
      { method: 'tool.use', params: { tool_use_id: 't2', name: 'Read', input: {} } },
      { method: 'tool.files', params: { tool_use_id: 't2', files: [{ raw: 'other/util.py', path: '/w/other/util.py' }] } },
    );
    const index = buildFileIndex(state.tools);
    expect(lookupFile(index, 'src/app.py')).toEqual({ path: '/w/src/app.py' });
    expect(lookupFile(index, 'app.py:42')).toEqual({ path: '/w/src/app.py', line: 42 });
    expect(lookupFile(index, '/w/src/app.py:42:7')).toEqual({ path: '/w/src/app.py', line: 42 });
    expect(lookupFile(index, './src/app.py')).toEqual({ path: '/w/src/app.py' });
    expect(lookupFile(index, 'util.py')).toBeUndefined(); // two files have this name
    expect(lookupFile(index, 'unknown.py')).toBeUndefined();
    expect(lookupFile(index, '__proto__')).toBeUndefined();
  });
});

describe('images', () => {
  it('accepts raster images within the limits', () => {
    expect(checkImage('image/png', 1000, 0, true)).toBeUndefined();
    expect(checkImage('image/svg+xml', 1000, 0, true)).toContain('PNG');
    expect(checkImage('image/png', 1000, MAX_IMAGES, true)).toContain(String(MAX_IMAGES));
    expect(checkImage('image/png', 8_000_000, 0, true)).toContain('too large');
    expect(checkImage('image/png', 1000, 0, false)).toContain('main agent');
  });

  it('parses data URLs and builds safe image sources', () => {
    expect(parseImageDataUrl('data:image/png;base64,aGk=')).toEqual({ mediaType: 'image/png', data: 'aGk=' });
    expect(parseImageDataUrl('data:image/svg+xml;base64,aGk=')).toBeUndefined();
    expect(parseImageDataUrl('data:image/png,plain')).toBeUndefined();
    expect(imageSrc('image/jpeg', 'aGk=')).toBe('data:image/jpeg;base64,aGk=');
    expect(imageSrc('image/svg+xml', 'aGk=')).toBeUndefined();
    expect(imageSrc('image/png', '"><script>')).toBeUndefined();
    expect(imageSrc('image/png', '')).toBeUndefined();
  });
});

import { splitPath } from '../../src/shared/chatModel';

describe('splitPath', () => {
  it('separates the directory from the file name', () => {
    expect(splitPath('src/app/main.py')).toEqual({ dir: 'src/app/', base: 'main.py' });
    expect(splitPath('main.py')).toEqual({ dir: '', base: 'main.py' });
    expect(splitPath('C:\\work\\a.ts')).toEqual({ dir: 'C:\\work\\', base: 'a.ts' });
  });
});
