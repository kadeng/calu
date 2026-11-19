// Shared settings of the integration tests (scripts/prepare-test-workspace.mjs and
// .vscode-test.mjs read them).
//
// Environment variables (all optional):
//   CALU_TEST_WORKSPACE  the workspace VSCode opens (default <tmpdir>/calu-vscode-test/ws);
//                        deleted and created again by prepare-test-workspace.mjs
//   CALU_TEST_ROOT       the directory of everything the tests create (default <tmpdir>/calu-vscode-test)
//   CALU_TEST_CALU       JSON array: the command that runs calu (default: `uv run --project
//                        <repository> --frozen calu`); prepare-test-workspace.mjs wraps it so
//                        that calu sees only directories below CALU_TEST_ROOT
//   CALU_TEST_PYTHON     JSON array: the Python that runs test/integration/scripted_session.py
//                        (default: `uv run --project <repository> --frozen python`)

import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Root of the extension (extensions/vscode). */
export const extensionDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Root of the repository. */
export const repoRoot = resolve(extensionDir, '..', '..');

/** Directory of everything the tests create. */
export const testRoot = process.env.CALU_TEST_ROOT ?? join(tmpdir(), 'calu-vscode-test');

/** The workspace VSCode opens. */
export const testWorkspace = process.env.CALU_TEST_WORKSPACE ?? join(testRoot, 'ws');

/**
 * VSCode's user-data directory. `calu.command` is machine-scoped, so it goes to its
 * User/settings.json. Kept short: VSCode puts IPC sockets in it (about 108 bytes at most).
 */
export const testUserDataDir = process.env.CALU_TEST_USER_DATA ?? join(tmpdir(), 'calu-vsc-ud');

/** The command that runs calu (wrapped by prepare-test-workspace.mjs). */
export const caluCommand = process.env.CALU_TEST_CALU
  ? JSON.parse(process.env.CALU_TEST_CALU)
  : ['uv', 'run', '--project', repoRoot, '--frozen', 'calu'];

/** The Python that runs the scripted session. */
export const pythonCommand = process.env.CALU_TEST_PYTHON
  ? JSON.parse(process.env.CALU_TEST_PYTHON)
  : ['uv', 'run', '--project', repoRoot, '--frozen', 'python'];

/** The wrapper that `calu.command` names. */
export const caluWrapper = join(testRoot, 'calu');

/** The file that the integration tests read the scripted session's command from. */
export const testConfigFile = join(testRoot, 'test-config.json');
