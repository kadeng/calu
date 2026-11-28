/**
 * Pure logic of installing calu (no `vscode` import): where uv is, which commands install uv
 * and calu, and how `claude` is reported. The dialogs and processes are in `src/bootstrap.ts`.
 *
 *     calu missing ──► ask ──► uv missing? ask, install uv (astral.sh script, ~/.local/bin)
 *                          ──► uv tool install --force --python 3.14 <calu.install.source>
 *     update       ──► uv tool upgrade calu
 *     claude       ──► only checked (`claude --version`) and reported with CLAUDE_DOCS_URL;
 *                      never installed by the extension
 */

import { findExecutable, type HostFacts } from '../caluCommand';

/** Default of the `calu.install.source` setting. */
export const DEFAULT_INSTALL_SOURCE = 'git+https://github.com/kadeng/calu';

/** Where the user reads how to install Claude Code. */
export const CLAUDE_DOCS_URL = 'https://docs.anthropic.com/en/docs/claude-code';

/** The official uv installer, as shown to the user before it runs. */
export const UV_INSTALL_URL = 'https://astral.sh/uv/install.sh';

/**
 * Find the `uv` executable: PATH (on WSL without Windows entries under /mnt/), then
 * ~/.local/bin/uv (default of the uv installer), then ~/.cargo/bin/uv (older installers).
 *
 * @param facts Facts about the machine.
 * @returns The path of `uv`, or undefined.
 */
export function findUv(facts: HostFacts): string | undefined {
  return findExecutable('uv', facts, [['.local', 'bin'], ['.cargo', 'bin']])?.path;
}

/**
 * Return the shell command that installs uv with the official installer.
 *
 * It downloads the installer with curl (or, without curl, with wget) and runs it with `sh`.
 * The installer puts `uv` into ~/.local/bin; `UV_NO_MODIFY_PATH=1` keeps it from editing shell
 * start-up files.
 *
 * @returns A command for `sh -c`.
 */
export function uvInstallCommand(): string {
  const fetch = `if command -v curl >/dev/null 2>&1; then curl -LsSf ${UV_INSTALL_URL}; else wget -qO- ${UV_INSTALL_URL}; fi`;
  return `(${fetch}) | env UV_NO_MODIFY_PATH=1 sh`;
}

/**
 * Check the `calu.install.source` setting.
 *
 * @param source The setting's value (any type).
 * @returns The trimmed source, or `DEFAULT_INSTALL_SOURCE` for a non-string or empty value.
 * @throws Error if the source starts with "-" (uv would read it as an option).
 */
export function installSource(source: unknown): string {
  const text = typeof source === 'string' ? source.trim() : '';
  if (!text) return DEFAULT_INSTALL_SOURCE;
  if (text.startsWith('-')) throw new Error(`calu.install.source must not start with "-": ${text}`);
  return text;
}

/**
 * Return the argv that installs (or replaces) calu as a uv tool.
 *
 * uv downloads Python 3.14 if needed and the dependencies from PyPI; `calu` lands in uv's tool
 * bin directory (default ~/.local/bin).
 *
 * @param uv Path of the uv executable.
 * @param source What to install (see `installSource`).
 * @returns `[uv, "tool", "install", "--force", "--python", "3.14", source]`.
 */
export function caluInstallArgv(uv: string, source: string): string[] {
  return [uv, 'tool', 'install', '--force', '--python', '3.14', source];
}

/**
 * Return the argv that updates a calu installed as a uv tool.
 *
 * @param uv Path of the uv executable.
 * @returns `[uv, "tool", "upgrade", "calu"]`.
 */
export function caluUpgradeArgv(uv: string): string[] {
  return [uv, 'tool', 'upgrade', 'calu'];
}

/** What is known about `claude` on the machine. */
export type ClaudeCheck =
  | { found: true; path: string; version: string }
  | { found: false; message: string };

/**
 * Build the report about `claude` from the lookup and the output of `claude --version`.
 *
 * @param path Path of `claude`, or undefined if it was not found.
 * @param versionOutput Standard output of `claude --version`; "" if it could not be run.
 * @returns `{found: true, path, version}` (version: the first line of the output, "unknown
 *   version" if empty), or `{found: false, message}` with the address of the install
 *   instructions. The extension never installs Claude Code.
 */
export function claudeCheck(path: string | undefined, versionOutput: string): ClaudeCheck {
  if (!path) {
    return {
      found: false,
      message:
        `Claude Code (\`claude\`) was not found on this machine. Install it as described at ${CLAUDE_DOCS_URL} ` +
        'and run `claude` once in a terminal in the project folder to sign in.',
    };
  }
  const version = versionOutput.split('\n')[0].trim() || 'unknown version';
  return { found: true, path, version };
}
