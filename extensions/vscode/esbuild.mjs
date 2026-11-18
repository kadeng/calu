// Bundles the extension (Node, CommonJS) and the two webview scripts (browser, IIFE) into dist/:
// dist/extension.js, dist/webview.js (the chat) and dist/settings.js (the settings dialog).
//
// Usage: node esbuild.mjs [--production] [--watch]
//   --production  minify, no source maps
//   --watch       rebuild on change
// esbuild does not type-check; `npm run build` runs `tsc --noEmit` first.

import * as esbuild from 'esbuild';

const production = process.argv.includes('--production');
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
