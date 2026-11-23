// Bundles the extension (Node, CommonJS) and the two webview scripts (browser, IIFE) into dist/:
// dist/extension.js, dist/webview.js (the chat) and dist/settings.js (the settings dialog).
//
// With --pane it builds the chat pane of `calu web` instead (PLAN.md D261): pane/main.ts, minified,
// to src/calu/web/static/pane/pane.js in the Python package, with the three style sheets it needs
// (media/chat.css, pane/theme.css, pane/pane.css). That output is committed, because the Python
// package ships it; `test/unit/paneBundle.test.ts` checks that it is up to date.
//
// Usage: node esbuild.mjs [--production] [--watch] | node esbuild.mjs --pane
//   --production  minify, no source maps
//   --watch       rebuild on change
//   --pane        build the chat pane of `calu web`
// esbuild does not type-check; `npm run build` runs `tsc --noEmit` first.

import * as esbuild from 'esbuild';
import { copyFileSync, mkdirSync } from 'node:fs';
import { PANE_OPTIONS, PANE_OUT } from './scripts/pane-options.mjs';

const production = process.argv.includes('--production');

if (process.argv.includes('--pane')) {
  mkdirSync(PANE_OUT, { recursive: true });
  await esbuild.build({ ...PANE_OPTIONS, outfile: `${PANE_OUT}/pane.js` });
  copyFileSync('media/chat.css', `${PANE_OUT}/chat.css`);
  copyFileSync('pane/theme.css', `${PANE_OUT}/theme.css`);
  copyFileSync('pane/pane.css', `${PANE_OUT}/pane.css`);
  process.exit(0);
}
const watch = process.argv.includes('--watch');

const common = {
  bundle: true,
  minify: production,
  sourcemap: !production,
  sourcesContent: false,
  logLevel: 'warning',
};

const contexts = await Promise.all([
  esbuild.context({
    ...common,
    entryPoints: ['src/extension.ts'],
    outfile: 'dist/extension.js',
    format: 'cjs',
    platform: 'node',
    target: 'node20',
    external: ['vscode'],
  }),
  esbuild.context({
    ...common,
    entryPoints: ['webview/main.ts'],
    outfile: 'dist/webview.js',
    format: 'iife',
    platform: 'browser',
    target: 'es2022',
  }),
  esbuild.context({
    ...common,
    entryPoints: ['webview/settings.ts'],
    outfile: 'dist/settings.js',
    format: 'iife',
    platform: 'browser',
    target: 'es2022',
  }),
]);

if (watch) {
  await Promise.all(contexts.map((c) => c.watch()));
  console.log('esbuild: watching for changes');
} else {
  await Promise.all(contexts.map((c) => c.rebuild()));
  await Promise.all(contexts.map((c) => c.dispose()));
}
