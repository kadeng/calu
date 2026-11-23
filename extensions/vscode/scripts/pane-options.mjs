// The esbuild options of the chat pane of `calu web` (PLAN.md D261), shared by `esbuild.mjs --pane`
// and the test that checks the committed bundle (`test/unit/paneBundle.test.ts`). No side effects.

/** Where the pane's files go: the static files of `calu web` in the Python package. */
export const PANE_OUT = '../../src/calu/web/static/pane';

/** The esbuild options of the pane bundle (minified, no source map, deterministic). */
export const PANE_OPTIONS = {
  bundle: true,
  minify: true,
  sourcemap: false,
  legalComments: 'none',
  logLevel: 'warning',
  entryPoints: ['pane/main.ts'],
  format: 'iife',
  platform: 'browser',
  target: 'es2022',
};
