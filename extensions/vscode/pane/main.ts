/**
 * Entry of the browser pane's bundle (`npm run build:pane` writes it to
 * `src/calu/web/static/pane/pane.js`): the host first (`setup.ts`), then the chat webview script
 * of the VSCode extension, unchanged.
 */

import './setup';
import '../webview/main';
