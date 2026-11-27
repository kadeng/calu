// The shared host logic (src/host/): the bridge client over a scripted bridge, and ChatHost's
// flows with a fake view and fake dialogs. Both the VSCode extension and the browser pane run it.
import { describe, expect, it } from 'vitest';
import { BridgeCore, LaunchError, errorCode, errorText, type Launched, type Launcher } from '../../src/host/bridgeCore';
import { ChatHost, type ChatView, type HostUi } from '../../src/host/chatHost';
import type { SessionsUi } from '../../src/host/sessions';
import type { HostMessage } from '../../src/shared/messages';
import { applyEvent, initialViewState, type ViewEvent, type ViewState } from '../../src/shared/viewState';

type Answer = (params: Record<string, unknown>) => unknown;

/** A bridge that answers JSON-RPC requests from a table, over a `Launcher`. */
class ScriptedBridge implements Launcher {
  readonly requests: { method: string; params: Record<string, unknown> }[] = [];
  private send: (chunk: string) => void = () => {};

  constructor(private readonly answers: Record<string, Answer>) {}

  launch(onData: (chunk: string) => void): Launched {
    this.send = onData;
    return {
      write: (line) => {
        const message = JSON.parse(line) as { id: number; method: string; params: Record<string, unknown> };
        this.requests.push({ method: message.method, params: message.params });
        const answer = this.answers[message.method];
        queueMicrotask(() => {
          try {
            const result = answer ? answer(message.params) : {};
            this.send(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result })}\n`);
          } catch (error) {
            const code = (error as { code?: number }).code ?? -32000;
            this.send(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, error: { code, message: (error as Error).message } })}\n`);
          }
        });
      },
      end: async () => undefined,
      kill: () => undefined,
    };
  }

  /** Send a notification as the bridge would. */
  notify(method: string, params: Record<string, unknown>): void {
    this.send(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`);
  }

  /** The params of the last request of a method. */
  last(method: string): Record<string, unknown> | undefined {
    return [...this.requests].reverse().find((r) => r.method === method)?.params;
  }
}

/** A chat view that keeps the state and what was sent to the view script. */
class FakeView implements ChatView {
  state: ViewState = initialViewState();
  readonly isVisible = true;
  readonly sent: HostMessage[] = [];
  dispatch(event: ViewEvent): void {
    this.state = applyEvent(this.state, event);
  }
  send(message: HostMessage): void {
    this.sent.push(message);
  }
  reveal(): void {}
}

/** Dialog answers and what the host asked. */
interface Script {
  confirm: boolean;
  inputs: (string | undefined)[];
  asked: string[];
}

function fakeUi(script: Script): HostUi & SessionsUi {
  return {
    log: () => undefined,
    confirm: async (question) => {
      script.asked.push(question);
      return script.confirm;
    },
    input: async (options) => {
      script.asked.push(options.title);
      return script.inputs.shift();
    },
    notify: async (_level, text) => {
      script.asked.push(text);
      return undefined;
    },
    flash: () => undefined,
    focused: () => true,
    notificationMode: () => 'decisions',
    activeFile: () => 'src/open.py',
    settingsChanged: () => undefined,
    offerInstall: () => undefined,
    sessionEvent: () => undefined,
    attached: () => undefined,
    handle: async () => undefined,
    error: () => undefined,
    info: () => undefined,
    pickSession: async () => undefined,
    serverHelp: () => ({ command: 'calu serve', tool: 'plain', detach: '', note: '' }) as never,
    startServer: () => undefined,
    showServerHelp: async () => undefined,
    progress: (_t, work) => work(),
  };
}

const ATTACH = { session_id: 's1', events: [], complete: true };

async function started(answers: Record<string, Answer> = {}, script: Partial<Script> = {}, remembered?: string) {
  const bridge = new ScriptedBridge({
    'ide.initialize': () => ({ bridge_version: '1', protocol_version: undefined, workspace: '/w' }),
    'ide.status': () => ({ findings: [], database: { reachable: true }, locked: false }),
    'ide.attach': () => ATTACH,
    'ide.sessions': () => ({ running: [] }),
    'session.call': () => ({}),
    ...answers,
  });
  const core = new BridgeCore({ line: () => undefined, append: () => undefined, warn: () => undefined }, 'test', '0', bridge, {
    ask: async () => undefined,
    stored: async () => undefined,
    store: async () => undefined,
    forget: async () => undefined,
    offerRemember: async () => false,
  });
  const view = new FakeView();
  const s: Script = { confirm: true, inputs: [], asked: [], ...script };
  const ui = fakeUi(s);
  const stored: (string | null)[] = [];
  const host = new ChatHost(core, view, ui, ui, (id) => stored.push(id), remembered);
  host.start();
  await core.ensureStarted();
  await new Promise((r) => setTimeout(r, 20));
  return { bridge, core, view, host, script: s, stored };
}

describe('BridgeCore', () => {
  it('initializes over the launcher and reports "running"', async () => {
    const { bridge, core, host } = await started();
    expect(core.status).toBe('running');
    expect(bridge.requests[0]).toEqual({ method: 'ide.initialize', params: { client: 'test', client_version: '0' } });
    host.dispose();
  });

  it('turns a launch failure into the status "missing" or "error"', async () => {
    const failing: Launcher = {
      launch: () => {
        throw new LaunchError('missing', 'calu was not found');
      },
    };
    const core = new BridgeCore({ line: () => undefined, append: () => undefined, warn: () => undefined }, 't', '0', failing, {
      ask: async () => undefined,
      stored: async () => undefined,
      store: async () => undefined,
      forget: async () => undefined,
      offerRemember: async () => false,
    });
    await expect(core.ensureStarted()).rejects.toThrow('calu was not found');
    expect(core.status).toBe('missing');
  });

  it('asks the calu user password once; Escape or refused passwords fail with what to do next', async () => {
    const needs = Object.assign(new Error('calu needs the calu user password (the one chosen at `calu setup`)'), {
      code: -32010,
    });
    const refused = Object.assign(new Error('That is not the calu user password.'), { code: -32010 });
    const bridge = new ScriptedBridge({
      'ide.initialize': () => ({ bridge_version: '1', workspace: '/w' }),
      'ide.sessions': () => {
        throw needs;
      },
      'ide.unlock': () => {
        throw refused;
      },
    });
    const answers: (string | undefined)[] = [undefined, 'a', 'b', 'c'];
    const asked: [string | undefined, number | undefined][] = [];
    const core = new BridgeCore({ line: () => undefined, append: () => undefined, warn: () => undefined }, 't', '0', bridge, {
      ask: async (problem, left) => {
        asked.push([problem, left]);
        return answers.shift();
      },
      stored: async () => undefined,
      store: async () => undefined,
      forget: async () => undefined,
      offerRemember: async () => false,
    });
    await core.ensureStarted();
    // Escape: the request fails with -32010 and says what did not happen.
    const dismissed = await core.request('ide.sessions', {}).catch((error: unknown) => error);
    expect(errorCode(dismissed)).toBe(-32010);
    expect(errorText(dismissed)).toBe(
      'Not done: no password was given. The next action that needs the database asks again.',
    );
    // A new action asks again; three refused passwords end the round.
    const gaveUp = await core.request('ide.sessions', {}).catch((error: unknown) => error);
    expect(errorText(gaveUp)).toBe(
      'Not done: the password was refused 3 times. The next action that needs the database asks again.',
    );
    expect(asked).toEqual([
      [undefined, undefined],
      [undefined, undefined],
      ['That is not the calu user password.', 2],
      ['That is not the calu user password.', 1],
    ]);
  });
});

describe('ChatHost', () => {
  it('attaches to the workspace session when the bridge runs, and shows its events', async () => {
    const { bridge, view, host } = await started();
    expect(bridge.last('ide.attach')).toMatchObject({ session: null, after_seq: 0 });
    expect(view.state.session.sessionId).toBe('s1');
    bridge.notify('session.event', { method: 'user.input', params: { text: 'hello', seq: 1 } });
    await new Promise((r) => setTimeout(r, 5));
    host.dispose();
  });

  it('attaches to the remembered session again; one the bridge resumed is loaded whole and remembered (D280)', async () => {
    // The owner, 2026-11-26: after a restart of `calu serve` the chat had to pick its session
    // by hand. The bridge resumes the named session and answers with the new one.
    const { bridge, view, host, stored } = await started(
      { 'ide.attach': (p) => (p.session === 'old' ? { session_id: 'new', events: [], complete: true } : ATTACH) },
      {},
      'old',
    );
    expect(bridge.requests.filter((r) => r.method === 'ide.attach').map((r) => r.params.session)).toEqual(['old']);
    expect(view.state.session.sessionId).toBe('new');
    expect(view.state.starting).toBeNull();
    expect(stored).toContain('new');
    host.dispose();
  });

  it('pauses and resumes the session with pause.set', async () => {
    const { bridge, host } = await started();
    await host.handle({ type: 'setPause', paused: true });
    expect(bridge.last('session.call')).toEqual({ method: 'pause.set', params: { paused: true } });
    await host.handle({ type: 'setPause', paused: false });
    expect(bridge.last('session.call')).toEqual({ method: 'pause.set', params: { paused: false } });
    host.dispose();
  });

  it('puts a message back into the input when it could not be sent', async () => {
    const { view, host } = await started({
      'session.call': (p) => {
        if (p.method === 'chat.send') throw new Error('the session is gone');
        return {};
      },
    });
    await host.handle({ type: 'send', text: 'hi', delivery: 'queued', to: 'main', images: [] });
    expect(view.sent).toContainEqual({ type: 'restoreInput', text: 'hi' });
    host.dispose();
  });

  it('sends a held message now or retracts it and gives its text back to the view (D283)', async () => {
    const calls: Record<string, unknown>[] = [];
    const { view, host } = await started({
      'session.call': (p) => {
        calls.push(p);
        if (p.method === 'chat.retract') return { text: 'take this back' };
        return {};
      },
    });
    await host.handle({ type: 'sendQueuedNow', id: 'q1' });
    await host.handle({ type: 'retractQueued', id: 'q2' });
    expect(calls.filter((c) => String(c.method).startsWith('chat.'))).toEqual([
      { method: 'chat.send_now', params: { id: 'q1' } },
      { method: 'chat.retract', params: { id: 'q2' } },
    ]);
    expect(view.sent).toContainEqual({ type: 'retracted', text: 'take this back' });
    host.dispose();
  });

  it('gives nothing back when a retract is refused (Claude Code has the message)', async () => {
    const { view, host } = await started({
      'session.call': (p) => {
        if (p.method === 'chat.retract') throw new Error('Claude Code has this message already');
        return {};
      },
    });
    await host.handle({ type: 'retractQueued', id: 'q2' });
    expect(view.sent.some((m) => (m as { type: string }).type === 'retracted')).toBe(false);
    host.dispose();
  });

  it('trusts a commit only after the confirmation, with the worktree and rev typed', async () => {
    const trusted: Record<string, unknown>[] = [];
    const answers = {
      'session.call': (p: Record<string, unknown>) => {
        if (p.method === 'honesty.trust') {
          trusted.push(p.params as Record<string, unknown>);
          return { commit_id: 'abcdef1234567890', recorded: true };
        }
        return {};
      },
    };
    const yes = await started(answers, { inputs: ['agent-a1 main'] });
    await yes.host.handle({ type: 'honestyTrust', ask: 'rev' });
    expect(trusted).toEqual([{ rev: 'main', checkout: 'agent-a1' }]);
    expect(yes.script.asked).toContain('calu: trust main in the worktree agent-a1?');
    yes.host.dispose();

    trusted.length = 0;
    const no = await started(answers, { confirm: false });
    await no.host.handle({ type: 'honestyTrust', path: 'src/x.py' });
    expect(trusted).toEqual([]);
    no.host.dispose();

    const file = await started(answers, { inputs: [undefined] });
    await file.host.handle({ type: 'honestyTrust', ask: 'file' });
    expect(trusted).toEqual([]);
    file.host.dispose();
  });

  it('answers honestyGraphGet with the graph of the attached session, or with the error', async () => {
    const graph = { nodes: [{ id: 'a.py:f', path: 'a.py', line: 3, name: 'f', kind: 'unit', state: 'dishonest' }], edges: [], mode: 'track' };
    let fail = false;
    const { host, view, bridge } = await started({
      'session.call': (p) => {
        if (p.method !== 'honesty.graph') return {};
        if (fail) throw new Error('no session is attached');
        return graph;
      },
    });
    const noticesBefore = JSON.stringify(view.state.items);
    await host.handle({ type: 'honestyGraphGet' });
    expect(bridge.last('session.call')).toEqual({ method: 'honesty.graph', params: {} });
    expect(view.sent.at(-1)).toEqual({ type: 'honestyGraph', sessionId: 's1', graph });

    fail = true;
    await host.handle({ type: 'honestyGraphGet' });
    expect(view.sent.at(-1)).toEqual({ type: 'honestyGraph', sessionId: 's1', graph: null, error: 'no session is attached' });
    // The graph view shows the error itself: the chat gets no notice.
    expect(JSON.stringify(view.state.items)).toBe(noticesBefore);
    host.dispose();
  });

  it('marks a unit as dishonest only with a typed reason, and drops code to judge again', async () => {
    const calls: { method: unknown; params: unknown }[] = [];
    const answers = {
      'session.call': (p: Record<string, unknown>) => {
        if (String(p.method).startsWith('honesty.')) calls.push({ method: p.method, params: p.params });
        return {};
      },
    };
    const typed = await started(answers, { inputs: ['  it also deletes the cache  '] });
    await typed.host.handle({ type: 'honestyMark', path: 'src/x.py', symbol: 'Parser.parse' });
    expect(typed.script.asked).toContain('calu: mark as dishonest');
    expect(calls).toEqual([{ method: 'honesty.mark', params: { path: 'src/x.py', symbol: 'Parser.parse', reason: 'it also deletes the cache' } }]);
    await typed.host.handle({ type: 'honestyDismiss', recheckIds: [4, 9] });
    expect(calls.at(-1)).toEqual({ method: 'honesty.dismiss', params: { recheck_ids: [4, 9] } });
    typed.host.dispose();

    calls.length = 0;
    for (const answer of [undefined, '   ']) {
      const cancelled = await started(answers, { inputs: [answer] });
      await cancelled.host.handle({ type: 'honestyMark', path: 'src/x.py', symbol: '' });
      expect(calls).toEqual([]);
      cancelled.host.dispose();
    }
  });

  it('leaves the platform its own messages', async () => {
    const handled: string[] = [];
    const { host } = await started();
    (host as unknown as { ui: HostUi }).ui.handle = async (m) => {
      handled.push(m.type);
    };
    await host.handle({ type: 'openSettings' });
    await host.handle({ type: 'showLog' });
    expect(handled).toEqual(['openSettings', 'showLog']);
    host.dispose();
  });
});
