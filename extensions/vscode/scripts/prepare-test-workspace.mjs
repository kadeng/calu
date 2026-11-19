// Creates what the integration tests need (see scripts/test-workspace.mjs for the places):
//
//   <root>/ws/greeter.py           a git repository with one commit
//   <root>/calu                    a wrapper: calu with XDG directories below <root> and
//                                  CALU_PLACE_FILE=/nonexistent, so the tests never read the
//                                  tester's own calu configuration, credentials or database
//   <root>/test-config.json        how the tests start test/integration/scripted_session.py
//   <user data>/User/settings.json calu.command = [<root>/calu]
//
// Usage: node scripts/prepare-test-workspace.mjs   (needs `git` on PATH)

import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { caluCommand, caluWrapper, extensionDir, pythonCommand, repoRoot, testConfigFile, testRoot, testUserDataDir, testWorkspace } from './test-workspace.mjs';

/**
 * Quote one word for a POSIX shell.
 *
 * @param word Any text.
 * @returns The word in single quotes, with each single quote written as '\''.
 */
function shellQuote(word) {
  return `'${String(word).replaceAll("'", "'\\''")}'`;
}

/**
 * Run git in the test workspace with a fixed identity.
 *
 * @param args Git arguments.
 * @returns Nothing. Throws if git fails.
 */
function git(...args) {
  execFileSync('git', ['-c', 'user.name=calu test', '-c', 'user.email=calu-test@example.invalid', ...args], { cwd: testWorkspace, stdio: 'ignore' });
}

rmSync(testRoot, { recursive: true, force: true });
for (const dir of ['config', 'state', 'data', 'runtime', 'ws']) mkdirSync(join(testRoot, dir), { recursive: true, mode: 0o700 });
mkdirSync(testWorkspace, { recursive: true });

writeFileSync(join(testWorkspace, 'greeter.py'), 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n');
git('init', '-q', '-b', 'main');
git('add', '.');
git('commit', '-q', '-m', 'initial');

const env = {
  XDG_CONFIG_HOME: join(testRoot, 'config'),
  XDG_STATE_HOME: join(testRoot, 'state'),
  XDG_DATA_HOME: join(testRoot, 'data'),
  XDG_RUNTIME_DIR: join(testRoot, 'runtime'),
  CALU_PLACE_FILE: '/nonexistent',
};
const exports = Object.entries(env).map(([k, v]) => `export ${k}=${shellQuote(v)}`).join('\n');
writeFileSync(caluWrapper, `#!/bin/sh\n# calu for the integration tests: nothing of the tester's own calu is read.\n${exports}\nexec ${caluCommand.map(shellQuote).join(' ')} "$@"\n`);
chmodSync(caluWrapper, 0o755);

writeFileSync(
  testConfigFile,
  JSON.stringify({ python: pythonCommand, script: join(extensionDir, 'test', 'integration', 'scripted_session.py'), cwd: repoRoot, env, root: testRoot }, null, 2),
);

rmSync(testUserDataDir, { recursive: true, force: true });
mkdirSync(join(testUserDataDir, 'User'), { recursive: true });
writeFileSync(
  join(testUserDataDir, 'User', 'settings.json'),
  JSON.stringify({ 'calu.command': [caluWrapper], 'git.openRepositoryInParentFolders': 'never', 'extensions.ignoreRecommendations': true }, null, 2),
);
console.log(`test workspace: ${testWorkspace}`);
