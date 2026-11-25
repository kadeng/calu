/**
 * First module of the pane's bundle: reads the address, starts the host and defines
 * `acquireVsCodeApi` before the webview script (`main.ts` imports it next) asks for it.
 *
 * Side effects: the address keeps only the known parts of its fragment (`history.replaceState`;
 * an old `#token=` goes); the bridge connection is opened.
 */

import { keptFragment, readPaneConfig } from './config';
import { startPane } from './webHost';

/**
 * Read a storage without failing where the browser blocks it (some iframes).
 *
 * @param get Returns the storage.
 * @returns The storage, or `undefined` if reading it throws.
 */
function storage(get: () => Storage): Storage | undefined {
  try {
    return get();
  } catch {
    return undefined;
  }
}

const config = readPaneConfig(
  location.hash,
  storage(() => localStorage),
);
history.replaceState(null, '', `${location.pathname}${location.search}${keptFragment(config)}`);
const { view } = startPane(config);
(globalThis as unknown as { acquireVsCodeApi: () => unknown }).acquireVsCodeApi = () => view.api();
