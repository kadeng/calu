/**
 * The host side of the browser pane: what the VSCode extension host is for the chat webview,
 * running in the same browser tab as the webview script (PLAN.md D261).
 *
 *     webview script (webview/main.ts)
 *        │ acquireVsCodeApi().postMessage ──► PaneView ──► ChatHost.handle (src/host/chatHost.ts)
 *        ◄── onHostMessage (reset, event, settings, bell, restoreInput)        │
 *                                                                               ▼
 *                                   BridgeCore ──WebSocket /pane/rpc──► calu web ──► Bridge
 *
 * What VSCode does with editors, the browser does like this:
 *
 *     open a file          read it with `ide.fs.read`, show it read-only at the line
 *     open an edit's diff  a unified diff of the edit (`diff.ts`)
 *     checker context,     the session's text, rendered as Markdown in a dialog
 *     escalation context
 *     settings, checkers   the full web interface in another window (`/settings`)
 *     the server terminal  the command to copy (a browser has no terminal)
 *
 * The login: the key that a login to `calu web` left in this origin's local storage (with the
 * cookie the browser sends itself). A pane in a frame of another site has neither; it asks for
 * the password of `calu web` and keeps it in memory only (`SocketLauncher`, PLAN.md D279).
 */

import { BridgeCore, LaunchError, STOP_TIMEOUT_MS, type Launched, type Launcher } from '../src/host/bridgeCore';
import { ChatHost, type ChatView, type HostUi } from '../src/host/chatHost';
import type { SessionsUi } from '../src/host/sessions';
import type { EditDiff } from '../src/protocol.generated';
import { formatCheckerContext, formatEscalationContext } from '../src/shared/contextText';
import { findingOk } from '../src/shared/hints';
import { resolveItemAction, workspaceRelative } from '../src/shared/itemMenu';
import { parseWebviewMessage, type ChatSettings, type HostMessage, type WebviewMessage } from '../src/shared/messages';
import { buildServerHelp } from '../src/shared/serverCommand';
import { isSourceRef, splitLine } from '../src/shared/sourceRefs';
import { PROMPT_TITLE, promptText } from '../src/shared/unlock';
import { applyEvent, initialViewState, isKnownFile, own, type ViewEvent, type ViewState } from '../src/shared/viewState';
import { renderMarkdownHtml } from '../webview/markdown';
import { loginProtocols, type PaneConfig } from './config';
import * as dialogs from './dialogs';
import { unifiedDiff } from './diff';

/** Version string sent in `ide.initialize`. */
const CLIENT_VERSION = 'pane';

/** Bytes of a file the file view reads at most. */
const FILE_VIEW_BYTES = 512 * 1024;

/** Edit diffs kept for "show the edit" (the newest). */
const MAX_DIFFS = 300;

/** Log lines kept for "Show log". */
const MAX_LOG = 500;

/** Where the pane's state for the webview is kept in the tab (`getState` / `setState`). */
const UI_STATE_KEY = 'calu-pane-ui';

/** The connection a `SocketLauncher` tried to open: open, or closed with or without a reason. */
type Attempt = { link: Launched } | { reason: string };

/**
 * Opens the WebSocket to the pane's bridge in `calu web`, and asks for the password when
 * `calu web` refuses the login.
 *
 *     launch ─► open with loginProtocols(config, password) ─► open: done
 *                 │ refused before it opened (no reason; the browser hides the server's 1008)
 *                 ▼
 *               serverUp()? ─ no ─► LaunchError "not running"
 *                 │ yes
 *                 ▼
 *               askPassword(wrong) ─ cancelled ─► LaunchError "did not accept this pane"
 *                 └─ typed: kept in memory, open again
 */
export class SocketLauncher implements Launcher {
  /** The password a person typed, kept for reconnects until the page closes; never stored. */
  private password: string | undefined;

  /**
   * @param url The WebSocket address, e.g. "ws://calu.localhost:8800/pane/rpc?dir=...".
   * @param config The pane's config (its key goes into the subprotocols).
   * @param askPassword Asks a person for the password of `calu web`; `wrong` is true when the
   *   last one was refused. Resolves to undefined when they cancel.
   * @param serverUp Resolves to whether `calu web` answers at all (a refused login and a
   *   stopped server look the same to a WebSocket).
   */
  constructor(
    private readonly url: string,
    private readonly config: PaneConfig,
    private readonly askPassword: (wrong: boolean) => Promise<string | undefined>,
    private readonly serverUp: () => Promise<boolean>,
  ) {}

  /**
   * Open the WebSocket, asking for the password as often as `calu web` refuses it.
   *
   * @param onData Called with each message, followed by a newline.
   * @param onEnd Called once when an open connection closes.
   * @returns The connection, once it is open.
   * @throws (rejects) LaunchError if calu web is not running, closes the connection with a
   *   reason before it opened, or the person cancels the password dialog.
   */
  async launch(onData: (chunk: string) => void, onEnd: (description: string) => void): Promise<Launched> {
    for (;;) {
      const attempt = await this.open(loginProtocols(this.config, this.password), onData, onEnd);
      if ('link' in attempt) return attempt.link;
      if (attempt.reason) throw new LaunchError('error', `calu web closed the connection: ${attempt.reason}.`);
      if (!(await this.serverUp())) {
        throw new LaunchError('error', 'calu web does not answer: is it running?');
      }
      const typed = await this.askPassword(this.password !== undefined);
      if (typed === undefined) {
        throw new LaunchError(
          'error',
          'calu web did not accept this pane: sign in to calu web in this browser, or type its password into the pane.',
        );
      }
      this.password = typed;
    }
  }

  /**
   * Open the WebSocket once.
   *
   * @param protocols The subprotocols that carry the login.
   * @param onData Called with each message, followed by a newline.
   * @param onEnd Called once when the connection closes after it opened.
   * @returns The open connection, or the reason ("" if none) it closed before it opened.
   */
  private open(protocols: string[], onData: (chunk: string) => void, onEnd: (description: string) => void): Promise<Attempt> {
    return new Promise((resolve) => {
      const socket = new WebSocket(this.url, protocols);
      let opened = false;
      let closed: () => void = () => {};
      const gone = new Promise<void>((r) => {
        closed = r;
      });
      socket.onopen = () => {
        opened = true;
        resolve({
          link: {
            write: (line) => socket.send(line.replace(/\n$/, '')),
            end: async () => {
              socket.close();
              await Promise.race([gone, new Promise((r) => setTimeout(r, STOP_TIMEOUT_MS))]);
            },
            kill: () => socket.close(),
          },
        });
      };
      socket.onmessage = (event: MessageEvent) => {
        if (typeof event.data === 'string') onData(`${event.data}\n`);
      };
      socket.onclose = (event: CloseEvent) => {
        closed();
        if (!opened) resolve({ reason: event.reason });
        else onEnd(`closed the connection${event.reason ? `: ${event.reason}` : ` (code ${event.code})`}`);
      };
    });
  }
}

/**
 * Tell whether `calu web` answers at all, by asking for the pane's own page.
 *
 * @returns True if the page answered with any HTTP status; false on a network error.
 */
async function caluWebAnswers(): Promise<boolean> {
  try {
    await fetch(location.pathname, { method: 'HEAD', cache: 'no-store' });
    return true;
  } catch {
    return false;
  }
}

/**
 * The chat view of the pane: the authoritative state, mirrored into the webview script of the
 * same page (what `ChatViewProvider` is in VSCode).
 */
export class PaneView implements ChatView {
  private viewState: ViewState = initialViewState();
  private chatSettings: ChatSettings = { showThinking: true, escalationSound: true };
  private deliver: ((message: HostMessage) => void) | undefined;
  /** Called for every message of the webview but "ready" and "stateReport". */
  onMessage: (message: WebviewMessage) => void = () => {};

  /** The current state. */
  get state(): ViewState {
    return this.viewState;
  }

  /** True while the tab is visible. */
  get isVisible(): boolean {
    return document.visibilityState === 'visible';
  }

  /**
   * Apply an event and forward it to the webview script.
   *
   * @param event The event.
   * @returns Nothing.
   */
  dispatch(event: ViewEvent): void {
    this.viewState = applyEvent(this.viewState, event);
    if (event.method === 'ui.attached' || event.method === 'ui.history') this.send({ type: 'reset', state: this.viewState });
    else this.send({ type: 'event', event });
    if (event.method === 'escalation.request' && this.chatSettings.escalationSound) this.send({ type: 'bell' });
    document.title = titleFor(this.viewState);
  }

  /**
   * Send a message to the webview script (dropped before it is connected).
   *
   * @param message The message.
   * @returns Nothing.
   */
  send(message: HostMessage): void {
    this.deliver?.(message);
  }

  /**
   * Bring the pane into sight: focus its window.
   *
   * @returns Nothing.
   */
  reveal(): void {
    window.focus();
  }

  /**
   * The object the webview script gets from `acquireVsCodeApi()`.
   *
   * @returns `postMessage` (validated, then to the host), `getState` / `setState` (the tab's
   *   session storage), and `onHostMessage`, through which this view delivers its messages.
   */
  api(): {
    postMessage(message: unknown): void;
    getState(): unknown;
    setState(state: unknown): void;
    onHostMessage(handler: (message: HostMessage) => void): void;
  } {
    return {
      postMessage: (raw) => {
        const message = parseWebviewMessage(raw);
        if (!message) return;
        if (message.type === 'ready') {
          this.send({ type: 'reset', state: this.viewState });
          this.send({ type: 'settings', settings: this.chatSettings });
          return;
        }
        if (message.type === 'stateReport') return;
        this.onMessage(message);
      },
      getState: () => {
        try {
          return JSON.parse(sessionStorage.getItem(UI_STATE_KEY) ?? 'null') as unknown;
        } catch {
          return null;
        }
      },
      setState: (state) => {
        try {
          sessionStorage.setItem(UI_STATE_KEY, JSON.stringify(state));
        } catch {
          // Storage blocked or full: the drafts are then not kept over a reload.
        }
      },
      onHostMessage: (handler) => {
        // Delivered asynchronously, as a VSCode webview receives them.
        this.deliver = (message) => queueMicrotask(() => handler(message));
      },
    };
  }
}

/**
 * The tab's title: the open decisions first, then the session.
 *
 * @param state The view state.
 * @returns E.g. "(2) calu chat · my-project" or "calu chat".
 */
function titleFor(state: ViewState): string {
  const open = Object.values(state.escalations).filter((e) => e.status === 'open').length;
  const where = state.session.workspace ? ` · ${state.session.workspace.split(/[\\/]/).pop()}` : '';
  return `${open ? `(${open}) ` : ''}calu chat${where}`;
}

/**
 * Open a page of the full web interface in its own window (never inside the pane's frame).
 * The window has the browser's login to `calu web`, or asks for the password.
 *
 * @param path The path, e.g. "/settings".
 * @returns Nothing.
 */
export function openWeb(path: string): void {
  window.open(new URL(path, location.origin).toString(), 'calu-web', 'noopener');
}

/**
 * Start the pane: the bridge connection, the host and the view.
 *
 * @param config What the pane was opened with (`readPaneConfig`).
 * @returns The view, whose `api()` the webview script gets, and the host (for tests).
 */
export function startPane(config: PaneConfig): { view: PaneView; host: ChatHost } {
  const view = new PaneView();
  const logLines: string[] = [];
  const log = (line: string): void => {
    logLines.push(`${new Date().toLocaleTimeString()} ${line}`);
    if (logLines.length > MAX_LOG) logLines.splice(0, logLines.length - MAX_LOG);
  };
  const diffs = new Map<string, EditDiff>();
  const keepDiff = (diff: EditDiff): void => {
    if (!diff.tool_use_id) return;
    diffs.delete(diff.tool_use_id);
    diffs.set(diff.tool_use_id, diff);
    while (diffs.size > MAX_DIFFS) diffs.delete(diffs.keys().next().value as string);
  };
  // The calu user password, as in VSCode: kept in this page until it closes, never stored.
  let heldPassword: string | undefined;
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const query = config.dir ? `?${new URLSearchParams({ dir: config.dir }).toString()}` : '';
  const bridge = new BridgeCore(
    { line: log, append: (text) => log(text.trimEnd()), warn: (text) => void dialogs.notify('warning', text) },
    'web',
    CLIENT_VERSION,
    new SocketLauncher(
      `${scheme}//${location.host}/pane/rpc${query}`,
      config,
      (wrong) =>
        dialogs.input({
          title: 'Sign in to calu web',
          prompt: wrong
            ? 'calu web did not accept that password. The password of calu web:'
            : 'The password of calu web (the calu user password, or the one chosen with `calu web --set-password`):',
          password: true,
        }),
      caluWebAnswers,
    ),
    {
      ask: (problem, left) => dialogs.input({ title: PROMPT_TITLE, prompt: promptText(problem, left), password: true }),
      stored: async () => heldPassword,
      store: async (password) => {
        heldPassword = password;
      },
      forget: async () => {
        heldPassword = undefined;
      },
      offerRemember: async () => true,
    },
  );

  /**
   * The bridge's workspace folder, which `ide.fs.read` paths are relative to.
   *
   * @returns The folder, or "" before the bridge answered `ide.initialize`.
   */
  const workspace = (): string => bridge.initializeResult?.workspace ?? '';

  /**
   * Show a project file read-only, at a line.
   *
   * @param path Relative to the bridge's workspace, or absolute inside it.
   * @param line 1-based line to mark, if any.
   * @returns Resolves when the view was closed.
   * @throws (rejects) Error if the path is outside the workspace or cannot be read.
   */
  const viewFile = async (path: string, line?: number): Promise<void> => {
    const rel = workspaceRelative(path, workspace());
    const answer = await bridge.request('ide.fs.read', { path: rel, length: FILE_VIEW_BYTES });
    const bytes = Uint8Array.from(atob(answer.content_b64), (c) => c.charCodeAt(0));
    let text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    if (!answer.eof) text += `\n[… the file has ${answer.size} bytes; the first ${bytes.length} are shown]`;
    await dialogs.showText(rel, { text, ...(line ? { line } : {}) });
  };

  /**
   * Show the diff of an edit.
   *
   * @param toolUseId The edit's tool call.
   * @returns Resolves when the view was closed.
   * @throws (rejects) Error if the diff is no longer held.
   */
  const viewDiff = async (toolUseId: string): Promise<void> => {
    const diff = diffs.get(toolUseId);
    if (!diff) throw new Error('the diff of this edit is no longer held');
    await dialogs.showText(`Edit of ${diff.path}`, { diff: unifiedDiff(diff.before, diff.after, diff.path) || '(no change)' });
  };

  /**
   * Copy a text to the clipboard and say so.
   *
   * @param text The text.
   * @param what What was copied, for the notice.
   * @returns Resolves when copied.
   * @throws (rejects) If the browser refuses the clipboard (e.g. a frame without the permission).
   */
  const copy = async (text: string, what: string): Promise<void> => {
    await navigator.clipboard.writeText(text);
    void dialogs.notify('info', `${what} copied.`);
  };

  /**
   * Handle the messages that `ChatHost` leaves to the platform (`HostUi.handle`).
   *
   * @param message The message.
   * @returns Resolves when handled.
   * @throws (rejects) Errors of reading files, of the clipboard and of the bridge.
   */
  const handle = async (message: WebviewMessage): Promise<void> => {
    const state = view.state;
    switch (message.type) {
      case 'why': {
        const escalation = own(state.escalations, message.escalationId);
        if (!escalation) return;
        const context = await bridge.session('escalation.context', { escalation_id: escalation.escalationId });
        await dialogs.showText('What the checker saw', { html: renderMarkdownHtml(formatEscalationContext(context)) });
        return;
      }
      case 'showContext': {
        const context = await bridge.session('context.get', {});
        await dialogs.showText('The conversation as the checkers see it', { html: renderMarkdownHtml(formatCheckerContext(context, new Date())) });
        return;
      }
      case 'openSettings':
      case 'editChecker':
        openWeb('/settings');
        return;
      case 'copyServerCommand':
        await copy(host.sessions.serverHelp().command, 'The server command');
        return;
      case 'install':
        void dialogs.notify('info', 'calu is installed where calu web runs; nothing to install here.');
        return;
      case 'openFile':
        if (isKnownFile(state, message.path)) await viewFile(message.path, message.line);
        return;
      case 'openToolFile': {
        const file = own(state.tools, message.toolUseId)?.files?.[message.index];
        if (file) await viewFile(file.path, message.line);
        return;
      }
      case 'openDiff':
        await viewDiff(message.toolUseId);
        return;
      case 'openLink': {
        const href = message.href;
        if (/^https?:\/\//i.test(href)) {
          window.open(href, '_blank', 'noopener');
          return;
        }
        if ((/^[a-z][a-z0-9+.-]*:/i.test(href) && !isSourceRef(href)) || href.startsWith('#')) return;
        const { path, line } = splitLine(decodeURIComponent(href.split('?')[0]));
        await viewFile(path.split('#')[0], line);
        return;
      }
      case 'showLog':
        await dialogs.showText('calu chat pane: log', { text: logLines.join('\n') || '(empty)' });
        return;
      case 'itemAction': {
        const resolved = resolveItemAction(own(state.tools, message.toolUseId), message.action, workspace());
        if (resolved.action === 'copy') await copy(resolved.text, 'The text');
        else if (resolved.action === 'showEdit') await viewDiff(resolved.toolUseId);
        else if (resolved.action === 'openFileAtEdit') await viewFile(resolved.path, resolved.line);
        else void dialogs.notify('info', 'The uncommitted changes of a file are shown in an editor; the chat pane cannot show them.');
        return;
      }
      default:
        return;
    }
  };

  const ui: HostUi = {
    log,
    confirm: dialogs.confirm,
    input: dialogs.input,
    notify: dialogs.notify,
    flash: (text) => void dialogs.notify('info', text),
    focused: () => document.hasFocus(),
    notificationMode: () => 'decisions',
    activeFile: () => '',
    settingsChanged: () => undefined,
    offerInstall: () => undefined,
    sessionEvent: (method, params) => {
      if (method === 'edit.diff') keepDiff(params as unknown as EditDiff);
    },
    attached: (result) => {
      for (const event of result.events ?? []) {
        if (event?.method === 'edit.diff') keepDiff(event.params as unknown as EditDiff);
      }
    },
    handle,
  };

  const sessionsUi: SessionsUi = {
    log,
    error: (text) => void dialogs.notify('error', text),
    info: (text) => void dialogs.notify('info', text),
    input: dialogs.input,
    pickSession: async (picks) => {
      const rows = picks.map((p) =>
        p.separator
          ? { label: p.label }
          : { label: p.label, description: p.description, detail: p.detail, value: p.choice, ...(p.choice.kind === 'stored' ? { secondary: 'Fork' } : {}) },
      );
      const chosen = await dialogs.choose('Sessions', rows);
      return chosen ? { choice: chosen.value, fork: chosen.secondary } : undefined;
    },
    serverHelp: (status) =>
      buildServerHelp({
        calu: ['calu'],
        sudo: false,
        insecure: false,
        extraArgs: [],
        tmux: status ? findingOk(status, 'tmux') : false,
        screen: status ? findingOk(status, 'screen') : false,
      }),
    // A browser has no terminal: show the command to run in one.
    startServer: (help) => void showServerCommand('Start the calu server in a terminal', help.command, help.note),
    showServerHelp: (reason, help) => showServerCommand(reason, help.command, help.note),
    progress: (_title, work) => work(),
  };

  /**
   * Show the server command with a copy button.
   *
   * @param title Why it is shown.
   * @param command The command.
   * @param note What it does.
   * @returns Resolves when the dialog was closed.
   */
  const showServerCommand = (title: string, command: string, note: string): Promise<void> =>
    dialogs.showText(title.replace(/^calu: /, ''), { text: `Run in a terminal on the machine calu web runs on:\n\n${command}\n\n${note}` }, [
      { label: 'Copy command', run: () => void copy(command, 'The server command').catch(() => undefined) },
    ]);

  // A link of `calu web`'s sessions list names the session to attach to (`#session=`).
  const host = new ChatHost(bridge, view, ui, sessionsUi, () => undefined, config.session || undefined);
  view.onMessage = (message) => void host.handle(message);
  host.start();
  document.addEventListener('visibilitychange', () => {
    if (view.isVisible) void host.sessions.refresh();
  });
  window.addEventListener('pagehide', () => bridge.dispose());
  void bridge.ensureStarted().catch(() => undefined);
  return { view, host };
}
