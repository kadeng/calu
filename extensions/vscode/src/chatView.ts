/**
 * The sidebar chat view (`calu.chat`): a WebviewViewProvider that owns the authoritative
 * `ViewState` and mirrors it into the webview.
 *
 *     dispatch(event) --> state = applyEvent(state, event) --> postMessage {type: "event"}
 *     webview "ready"  --> postMessage {type: "reset", state}   (after (re)load of the webview)
 *     webview actions  --> onMessage handler (set by the extension)
 *
 * Events that arrive while the webview is hidden are not lost: the state is kept here and sent
 * with the next "reset".
 */

import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';
import { parseWebviewMessage, type ChatSettings, type HostMessage, type WebviewMessage } from './shared/messages';
import {
  applyEvent,
  initialViewState,
  openEscalationCount,
  type ViewEvent,
  type ViewState,
} from './shared/viewState';

/** Id of the webview view (package.json `contributes.views`). */
export const CHAT_VIEW_ID = 'calu.chat';

/** Provider of the chat view. Register with `window.registerWebviewViewProvider`. */
export class ChatViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private viewState: ViewState = initialViewState();
  private chatSettings: ChatSettings = { showThinking: true, escalationSound: true };
  /** True once the current webview sent "ready" (messages posted earlier may be lost). */
  private webviewReady = false;
  /** A chime requested while no webview was ready; played after the next "ready". */
  private bellPending = false;
  private nextReportId = 1;
  private readonly reports = new Map<number, (state: unknown) => void>();
  private readyEmitter = new vscode.EventEmitter<void>();

  /** Fires each time the webview has loaded its script and received the state. */
  readonly onDidBecomeReady = this.readyEmitter.event;

  /**
   * @param extensionUri Root of the extension (for `media/`).
   * @param onMessage Called for every valid message from the webview (except "ready" and
   *   "stateReport", which this class handles).
   * @param onResolved Called when the view is resolved (shown for the first time or again).
   * @param onVisibility Called when the view becomes visible or hidden.
   */
  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly onMessage: (message: WebviewMessage) => void,
    private readonly onResolved: () => void = () => {},
    private readonly onVisibility: (visible: boolean) => void = () => {},
  ) {}

  /** The current state (the same object until the next event). */
  get state(): ViewState {
    return this.viewState;
  }

  /** True while a webview is resolved (it may still be hidden). */
  get isResolved(): boolean {
    return this.view !== undefined;
  }

  /** True if the view is resolved and currently visible on screen. */
  get isVisible(): boolean {
    return this.view?.visible === true;
  }

  /**
   * Send a message to the webview that is not a state change (e.g. `restoreInput`).
   *
   * @param message The message; dropped if no webview is resolved.
   * @returns Nothing.
   */
  send(message: HostMessage): void {
    this.post(message);
  }

  /**
   * Make the chat view visible without moving the keyboard focus if possible.
   *
   * A resolved view is shown with `WebviewView.show(preserveFocus = true)`. A view that was never
   * resolved is opened with the `calu.chat.focus` command (VSCode then moves the focus to it).
   *
   * @returns A promise that resolves when the request was made (never rejects).
   */
  async reveal(): Promise<void> {
    try {
      if (this.view) this.view.show(true);
      else await vscode.commands.executeCommand(`${CHAT_VIEW_ID}.focus`);
    } catch {
      // The view container may be unavailable (e.g. during shutdown).
    }
  }

  /**
   * Apply an event to the state and forward it to the webview, if one is resolved.
   *
   * @param event A session or extension event (see `ViewEvent`).
   * @returns Nothing.
   */
  dispatch(event: ViewEvent): void {
    this.viewState = applyEvent(this.viewState, event);
    this.onStateChange?.(this.viewState);
    // An attach replaces the whole transcript, and the recorded turns are put above it: send
    // the result, not the (large) event.
    if (event.method === 'ui.attached' || event.method === 'ui.history') this.post({ type: 'reset', state: this.viewState });
    else this.post({ type: 'event', event });
    this.updateBadge();
    if (event.method === 'escalation.request' && this.chatSettings.escalationSound) this.ringBell();
  }

  /**
   * Called with the new state after every `dispatch` and `replaceState` (the status bar item
   * follows it); `undefined` = nobody listens.
   */
  onStateChange: ((state: ViewState) => void) | undefined;

  /**
   * Ask the webview to play the escalation chime; if no webview is ready (hidden view being
   * restored, not opened yet), the request waits for the next "ready".
   *
   * @returns Nothing.
   */
  private ringBell(): void {
    if (this.view && this.webviewReady) this.post({ type: 'bell' });
    else this.bellPending = true;
  }

  /**
   * Change the display settings and send them to the webview (takes effect immediately).
   *
   * @param settings The new settings.
   * @returns Nothing.
   */
  setSettings(settings: ChatSettings): void {
    this.chatSettings = settings;
    this.post({ type: 'settings', settings });
  }

  /** The current display settings. */
  get settings(): ChatSettings {
    return this.chatSettings;
  }

  /**
   * Replace the whole state (used by tests) and send it to the webview.
   *
   * @param state The new state.
   * @returns Nothing.
   */
  replaceState(state: ViewState): void {
    this.viewState = state;
    this.onStateChange?.(state);
    this.post({ type: 'reset', state });
    this.updateBadge();
  }

  /**
   * Set up a webview for the view (WebviewViewProvider contract). Called by VSCode when the
   * view becomes visible for the first time and after it was disposed and shown again.
   *
   * @param view The view to fill.
   * @returns Nothing.
   */
  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.webviewReady = false;
    const media = vscode.Uri.joinPath(this.extensionUri, 'media');
    const dist = vscode.Uri.joinPath(this.extensionUri, 'dist');
    view.webview.options = { enableScripts: true, localResourceRoots: [media, dist] };
    view.webview.html = renderHtml(view.webview, this.extensionUri);
    view.webview.onDidReceiveMessage((raw: unknown) => this.receive(raw));
    view.onDidChangeVisibility(() => this.onVisibility(view.visible));
    view.onDidDispose(() => {
      if (this.view === view) this.view = undefined;
      this.onVisibility(false);
    });
    this.updateBadge();
    this.onResolved();
  }

  /**
   * Ask the webview for its own copy of the state (integration tests use this to check what the
   * webview rendered from).
   *
   * @param timeoutMs Maximum wait.
   * @returns The webview's state object.
   * @throws (rejects) Error if no webview is resolved or it does not answer in time.
   */
  requestWebviewState(timeoutMs = 5000): Promise<unknown> {
    if (!this.view) return Promise.reject(new Error('the chat view is not resolved'));
    const requestId = this.nextReportId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.reports.delete(requestId);
        reject(new Error('the chat webview did not report its state'));
      }, timeoutMs);
      this.reports.set(requestId, (state) => {
        clearTimeout(timer);
        resolve(state);
      });
      this.post({ type: 'reportState', requestId });
    });
  }

  /**
   * Handle a raw message from the webview.
   *
   * @param raw Any value; invalid messages are dropped.
   * @returns Nothing.
   */
  private receive(raw: unknown): void {
    const message = parseWebviewMessage(raw);
    if (!message) return;
    if (message.type === 'ready') {
      this.webviewReady = true;
      this.post({ type: 'reset', state: this.viewState });
      this.post({ type: 'settings', settings: this.chatSettings });
      if (this.bellPending && this.chatSettings.escalationSound) this.post({ type: 'bell' });
      this.bellPending = false;
      this.readyEmitter.fire();
      return;
    }
    if (message.type === 'stateReport') {
      this.reports.get(message.requestId)?.(message.state);
      this.reports.delete(message.requestId);
      return;
    }
    this.onMessage(message);
  }

  /**
   * Send a message to the webview if one is resolved; drop it otherwise.
   *
   * @param message The message.
   * @returns Nothing.
   */
  private post(message: HostMessage): void {
    void this.view?.webview.postMessage(message);
  }

  /**
   * Show the number of open escalations as the view's badge.
   *
   * @returns Nothing.
   */
  private updateBadge(): void {
    if (!this.view) return;
    const open = openEscalationCount(this.viewState);
    this.view.badge = open
      ? { value: open, tooltip: `${open} step(s) wait for your decision` }
      : undefined;
  }
}

/**
 * Build the webview HTML with a strict Content Security Policy: no inline code, scripts only
 * with a per-load nonce, resources only from the extension's `media/` and `dist/` folders.
 *
 * @param webview The webview (for `asWebviewUri` and `cspSource`).
 * @param extensionUri Root of the extension.
 * @param scriptName File name of the bundled script in `dist/` (default: the chat view's).
 * @param styleNames File names of stylesheets in `media/` (default: the chat view's).
 * @param title The document title.
 * @returns The HTML document: the stylesheets, an empty `<div id="app">` and the script.
 */
export function renderHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  scriptName = 'webview.js',
  styleNames: readonly string[] = ['chat.css'],
  title = 'calu',
): string {
  const nonce = randomBytes(16).toString('base64');
  const script = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'dist', scriptName));
  const styles = styleNames
    .map((name) => `<link href="${webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'media', name))}" rel="stylesheet">`)
    .join('\n');
  const csp = [
    "default-src 'none'",
    `img-src ${webview.cspSource} data:`,
    `media-src ${webview.cspSource} data:`,
    `style-src ${webview.cspSource}`,
    `font-src ${webview.cspSource}`,
    `script-src 'nonce-${nonce}'`,
  ].join('; ');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
${styles}
<title>${title}</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
}
