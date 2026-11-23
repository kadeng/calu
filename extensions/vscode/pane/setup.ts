/**
 * First module of the pane's bundle: reads the address, starts the host and defines
 * `acquireVsCodeApi` before the webview script (`main.ts` imports it next) asks for it.
 *
 * Side effects: the token is taken out of the address (`history.replaceState`), so that it is
 * neither shown nor bookmarked; the bridge connection is opened.
 */

import { addressWithoutToken, readPaneConfig } from './config';
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
  storage(() => sessionStorage),
  storage(() => localStorage),
);
history.replaceState(null, '', `${location.pathname}${location.search}${addressWithoutToken(config)}`);
const { view } = startPane(config);
(globalThis as unknown as { acquireVsCodeApi: () => unknown }).acquireVsCodeApi = () => view.api();
