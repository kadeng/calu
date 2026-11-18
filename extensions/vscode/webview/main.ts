/**
 * PLACEHOLDER of the chat webview: shows "loading…" and the bridge status, and posts `ready`.
 * The real chat UI replaces this file; its contract is `WEBVIEW_CONTRACT.md`.
 *
 *     load ──► post {type: "ready"} ──► host answers {type: "reset", state} ──► render one line
 */

import type { HostMessage, WebviewMessage } from '../src/shared/messages';
import { applyEvent, initialViewState, type ViewState } from '../src/shared/viewState';

/** The part of the VSCode webview API used here. */
interface VsCodeApi {
  postMessage(message: WebviewMessage): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

const vscode = acquireVsCodeApi();
let state: ViewState = initialViewState();

/**
 * Render the placeholder line into `#app`.
 *
 * @returns Nothing.
 */
function render(): void {
  const app = document.getElementById('app');
  if (!app) return;
  app.textContent = '';
  const line = document.createElement('p');
  line.className = 'placeholder';
  line.dataset.testid = 'placeholder';
  line.textContent = `calu: loading… (bridge ${state.bridge.status}, link ${state.link.status})`;
  app.append(line);
}

window.addEventListener('message', (event: MessageEvent<HostMessage>) => {
  const message = event.data;
  if (message.type === 'reset') state = message.state;
  else if (message.type === 'event') state = applyEvent(state, message.event);
  else if (message.type === 'reportState') vscode.postMessage({ type: 'stateReport', requestId: message.requestId, state });
  render();
});

render();
vscode.postMessage({ type: 'ready' });
