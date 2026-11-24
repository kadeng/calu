/**
 * Supervision of the bridge process (`calu ide`) on the extension host's machine.
 *
 * The client itself -- JSON-RPC, `ide.initialize`, the password flow, the status -- is
 * `host/bridgeCore.ts`, which the browser pane of `calu web` uses as well. This file adds what
 * only the extension host can do: find calu (`calu.command`, PATH), start `calu ide` as a child
 * process in the workspace folder, pass its stderr to the "calu" output channel, and end the
 * process tree.
 *
 *     Bridge = BridgeCore + ProcessLauncher (spawn `calu ide`, end it with SIGTERM/SIGKILL)
 *
 * The password store that `extension.ts` passes in is a variable of the extension host (spec
 * section 10): the password lives until the window closes, so a restarted bridge is unlocked
 * again without asking. The bridge never starts sessions itself and never prompts.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { buildIdeArgv, looksLikeWsl, resolveCaluCommand, type HostFacts, type ResolveResult } from './caluCommand';
import { BridgeCore, LaunchError, STOP_TIMEOUT_MS, type Launched, type Launcher, type PasswordDeps } from './host/bridgeCore';
import { PROMPT_TITLE, promptText } from './shared/unlock';

export { errorCode, errorText, type PasswordDeps } from './host/bridgeCore';

/**
 * Check whether a path is an executable regular file.
 *
 * @param p Absolute path.
 * @returns True if the file exists, is a regular file and is executable by this process.
 *   Never throws.
 */
export function isExecutable(p: string): boolean {
  try {
    if (!fs.statSync(p).isFile()) return false;
    if (process.platform !== 'win32') fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Detect whether the extension host runs inside WSL.
 *
 * @returns True on Linux if `WSL_DISTRO_NAME` is set or the kernel release mentions Microsoft.
 */
export function runningInWsl(): boolean {
  if (process.platform !== 'linux') return false;
  return Boolean(process.env.WSL_DISTRO_NAME) || looksLikeWsl(os.release());
}

/**
 * Collect the facts about this machine that the program lookups need.
 *
 * @returns PATH, home directory, WSL and Windows flags, and the executable check.
 */
export function hostFacts(): HostFacts {
  return {
    pathEnv: process.env.PATH ?? '',
    pathDelimiter: path.delimiter,
    home: os.homedir(),
    isWsl: runningInWsl(),
    isWindows: process.platform === 'win32',
    isExecutable,
  };
}

/**
 * Resolve the calu command from the `calu.command` setting and the environment of this machine.
 *
 * @returns See `resolveCaluCommand`.
 */
export function resolveCaluCommandForHost(): ResolveResult {
  const config = vscode.workspace.getConfiguration('calu');
  return resolveCaluCommand({ configured: config.get<unknown>('command'), ...hostFacts() });
}

/**
 * Starts `calu ide` as a child process in the workspace folder (see the module comment).
 */
class ProcessLauncher implements Launcher {
  /**
   * @param output Receives the bridge's stderr and the start line.
   */
  constructor(private readonly output: vscode.OutputChannel) {}

  /**
   * Find calu and spawn `calu ide` in the workspace folder.
   *
   * @param onData Called with each chunk of the process's stdout.
   * @param onEnd Called once when the process exits or cannot run.
   * @returns The connection to the process.
   * @throws LaunchError "error" without a workspace folder or when spawning fails; "missing"
   *   when calu is not found.
   */
  launch(onData: (chunk: string) => void, onEnd: (description: string) => void): Launched {
    const folder = bridgeWorkspaceFolder();
    if (!folder) throw new LaunchError('error', 'Open a folder to use calu.');
    const resolved = resolveCaluCommandForHost();
    if (!resolved.ok) throw new LaunchError('missing', resolved.message);
    const argv = buildIdeArgv(resolved.command);
    this.output.appendLine(`[calu] starting the bridge (${resolved.source}): ${argv.join(' ')}`);
    let proc: ChildProcessWithoutNullStreams;
    try {
      // On Linux/macOS the bridge leads its own process group, so killProcessTree can end it
      // together with a wrapper such as `uv run`.
      proc = spawn(argv[0], argv.slice(1), {
        cwd: folder.uri.fsPath,
        env: process.env,
        stdio: 'pipe',
        detached: process.platform !== 'win32',
      });
    } catch (error) {
      throw new LaunchError('error', `Cannot start calu: ${error instanceof Error ? error.message : String(error)}`);
    }
    const exited = new Promise<void>((resolve) => {
      proc.on('error', (error) => {
        onEnd(`could not run: ${error.message}`);
        resolve();
      });
      proc.on('exit', (code, signal) => {
        onEnd(signal ? `was killed by ${signal}` : `exited with code ${code}`);
        resolve();
      });
    });
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (chunk: string) => onData(chunk));
    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', (chunk: string) => this.output.append(chunk));
    proc.stdin.on('error', (error) => this.output.appendLine(`[calu] stdin: ${error.message}`));
    return {
      pid: proc.pid,
      write: (line) => {
        proc.stdin.write(line);
      },
      end: async () => {
        proc.stdin.end();
        const timeout = new Promise<void>((resolve) => setTimeout(resolve, STOP_TIMEOUT_MS));
        await Promise.race([exited, timeout]);
        if (proc.exitCode === null && proc.signalCode === null) {
          killProcessTree(proc, 'SIGKILL');
          await exited;
        }
      },
      kill: () => {
        killProcessTree(proc, 'SIGTERM');
        setTimeout(() => killProcessTree(proc, 'SIGKILL'), STOP_TIMEOUT_MS).unref();
      },
    };
  }
}

/** Owner of the bridge process. Create one per extension activation; dispose on deactivate. */
export class Bridge extends BridgeCore implements vscode.Disposable {
  /**
   * @param output Channel that receives the bridge's stderr and lifecycle messages.
   * @param clientVersion Version string sent in `ide.initialize`.
   * @param passwords The password prompt and the secret storage; members that are left out
   *   come from `inputBoxPasswordDeps()` (input box, nothing remembered).
   */
  constructor(output: vscode.OutputChannel, clientVersion: string, passwords: Partial<PasswordDeps> = {}) {
    super(
      {
        line: (text) => output.appendLine(text),
        append: (text) => output.append(text),
        warn: (text) => void vscode.window.showWarningMessage(`${text} ("calu: Update calu")`),
      },
      'vscode',
      clientVersion,
      new ProcessLauncher(output),
      { ...inputBoxPasswordDeps(), ...passwords },
    );
  }
}

/**
 * Default `PasswordDeps`: a masked input box, and nothing is remembered.
 *
 * @returns `ask` shows `showInputBox` (masked, stays open on focus loss) with the text of
 *   `promptText`; `stored` resolves `undefined`; `store` and `forget` do nothing;
 *   `offerRemember` resolves false.
 */
export function inputBoxPasswordDeps(): PasswordDeps {
  return {
    ask: (problem, left) =>
      vscode.window.showInputBox({
        title: PROMPT_TITLE,
        prompt: promptText(problem, left),
        password: true,
        ignoreFocusOut: true,
      }),
    stored: () => Promise.resolve(undefined),
    store: () => Promise.resolve(),
    forget: () => Promise.resolve(),
    offerRemember: () => Promise.resolve(false),
  };
}

/**
 * Return the workspace folder the bridge runs in: the first folder with a `file:` URI.
 *
 * @returns The folder, or `undefined` if no such folder is open.
 */
export function bridgeWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  return vscode.workspace.workspaceFolders?.find((f) => f.uri.scheme === 'file');
}

/**
 * Send a signal to a child process and all its descendants.
 *
 * On Linux/macOS the child was spawned with `detached: true` and leads a process group; the
 * signal goes to the whole group (`kill(-pid)`). On Windows `taskkill /T /F` ends the tree.
 * Errors (e.g. the group no longer exists) are ignored.
 *
 * @param proc The spawned child process.
 * @param signal "SIGTERM" or "SIGKILL" (Windows always force-kills).
 * @returns Nothing.
 */
export function killProcessTree(proc: ChildProcessWithoutNullStreams, signal: NodeJS.Signals): void {
  if (proc.pid === undefined) return;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { stdio: 'ignore' }).on('error', () => {});
    } else {
      process.kill(-proc.pid, signal);
    }
  } catch {
    try {
      proc.kill(signal);
    } catch {
      // Already gone.
    }
  }
}
