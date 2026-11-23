// The shared host logic (src/host/): the bridge client over a scripted bridge, and ChatHost's
// flows with a fake view and fake dialogs. Both the VSCode extension and the browser pane run it.
import { describe, expect, it } from 'vitest';
import { BridgeCore, LaunchError, type Launched, type Launcher } from '../../src/host/bridgeCore';
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

async function started(answers: Record<string, Answer> = {}, script: Partial<Script> = {}) {
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
  const host = new ChatHost(core, view, ui, ui);
  host.start();
  await core.ensureStarted();
  await new Promise((r) => setTimeout(r, 20));
  return { bridge, core, view, host, script: s };
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
