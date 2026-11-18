/**
 * PLACEHOLDER of the settings webview: shows "loading…", asks for the data and shows how many
 * settings arrived. The real settings dialog replaces this file; its contract is
 * `WEBVIEW_CONTRACT.md`.
 *
 *     load ──► post {type: "load", project: null} ──► host answers {type: "data", ...}
 */

import type { SettingsHostMessage, SettingsWebviewMessage } from '../src/shared/settingsMessages';
import { buildSections } from '../src/shared/settingsModel';

/** The part of the VSCode webview API used here. */
interface VsCodeApi {
  postMessage(message: SettingsWebviewMessage): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

const vscode = acquireVsCodeApi();

/**
 * Show one line of text in `#app`.
 *
 * @param text The text.
 * @returns Nothing.
 */
function show(text: string): void {
  const app = document.getElementById('app');
  if (!app) return;
  app.textContent = '';
  const line = document.createElement('p');
  line.className = 'placeholder';
  line.dataset.testid = 'placeholder';
  line.textContent = text;
  app.append(line);
}

window.addEventListener('message', (event: MessageEvent<SettingsHostMessage>) => {
  const message = event.data;
  if (message.type !== 'data') return;
  const sections = buildSections(message.settings?.rows ?? []);
  const count = sections.reduce((n, s) => n + s.rows.length, 0);
  show(`calu Settings: ${count} settings in ${sections.length} sections. ${message.problems.join(' ')}`);
});

show('calu Settings: loading…');
vscode.postMessage({ type: 'load', project: null });
