// Configuration of `vscode-test` (@vscode/test-cli): the integration tests in a real VSCode.
// `npm run test:integration` (Linux; headless: `xvfb-run -a npm run test:integration`), or
// `scripts/test-in-docker.sh`. VSCODE_TEST_VERSION selects the VSCode version (default
// "stable"; "1.106.0" checks the oldest supported one).

import { defineConfig } from '@vscode/test-cli';
import { testUserDataDir, testWorkspace } from './scripts/test-workspace.mjs';

const launchArgs = [`--user-data-dir=${testUserDataDir}`, '--disable-workspace-trust', '--disable-gpu', '--skip-welcome', '--skip-release-notes'];
if (process.getuid?.() === 0) launchArgs.push('--no-sandbox');

export default defineConfig({
  label: 'integration',
  files: 'out/test/integration/**/*.test.js',
  version: process.env.VSCODE_TEST_VERSION ?? 'stable',
  workspaceFolder: testWorkspace,
  launchArgs,
  mocha: { ui: 'tdd', timeout: 60000, color: true },
});
