/**
 * Supervision of the bridge process (`calu ide`) on the extension host's machine.
 *
 *     status: stopped --ensureStarted()--> starting --ide.initialize ok--> running
 *                                             |                               |
 *                        calu not found ------+--> missing                    |
 *                        spawn/initialize error --> error        unexpected exit --> exited
 *     running --stop()--> stopped        exited/error/missing --ensureStarted()/restart()--> starting
 *
 *     request(method, params) ──► calu ide
 *         error -32010 {needs}  ──► askPassword(needs) ──► same request with `password` (not stored)
 *     session(method, params) = request("session.call", {method, params})
 *
 * The bridge's stderr goes to the "calu" output channel. Its notifications (`session.event`,
 * `ide.link`, `ide.log`) are re-emitted on `onNotification`. The bridge is started in the
 * workspace folder; it never starts sessions itself and never prompts.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {
  BridgeError,
  neededCredential,
  type BridgeMethod,
  type BridgeRequestParams,
  type BridgeRequestResults,
  type IdeInitializeResult,
  type SessionParamsOf,
  type SessionResultOf,
} from './bridgeProtocol';
import { buildIdeArgv, looksLikeWsl, resolveCaluCommand, type HostFacts, type ResolveResult } from './caluCommand';
import { PROTOCOL_VERSION, type RequestMethod } from './protocol.generated';
import { RpcClient, RpcRequestError, type RpcEvent } from './rpc';
import type { BridgeStatus } from './shared/viewState';

/** Milliseconds to wait for `ide.shutdown` and for the process to exit before it is killed. */
const STOP_TIMEOUT_MS = 5000;

/** How often a wrong password may be entered for one request. */
const PASSWORD_ATTEMPTS = 3;

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
  return resolveCaluCommand({ configured: config.get<string[]>('command') ?? [], ...hostFacts() });
}

/**
 * Asks the user for the password of a credential.
 *
 * @param credential Name of the credential, e.g. "admin".
 * @param wrong True if a password was just refused.
 * @returns The password, or `undefined` if the user cancelled. The caller does not keep it.
 */
export type PasswordPrompt = (credential: string, wrong: boolean) => Thenable<string | undefined>;

/**
 * Default `PasswordPrompt`: a masked input box.
 *
 * @param credential Name of the credential.
 * @param wrong True if a password was just refused.
 * @returns The entered password, or `undefined` on Escape.
 */
export const inputBoxPassword: PasswordPrompt = (credential, wrong) =>
  vscode.window.showInputBox({
    title: 'calu: password needed',
    prompt: wrong
      ? `Wrong password. Password of calu's "${credential}" credential:`
      : `Password of calu's "${credential}" credential (used for this request only, not stored):`,
    password: true,
    ignoreFocusOut: true,
  });

/** One running bridge process with its RPC connection. */
interface Running {
  proc: ChildProcessWithoutNullStreams;
  rpc: RpcClient;
  exited: Promise<void>;
  /** Set by `stop()` so that the exit is not reported as a crash. */
  stopping: boolean;
}

/** Owner of the bridge process. Create one per extension activation; dispose on deactivate. */
export class Bridge implements vscode.Disposable {
  private readonly notificationEmitter = new vscode.EventEmitter<RpcEvent>();
  private readonly statusEmitter = new vscode.EventEmitter<{ status: BridgeStatus; message?: string; version?: string }>();
  private running: Running | undefined;
  private starting: Promise<IdeInitializeResult> | undefined;
  private currentStatus: BridgeStatus = 'stopped';
  /** Result of the last successful `ide.initialize`; undefined while not running. */
  initializeResult: IdeInitializeResult | undefined;

  /** Fires for every notification of the bridge, in arrival order. */
  readonly onNotification = this.notificationEmitter.event;
  /** Fires when `status` changes. */
  readonly onStatus = this.statusEmitter.event;

  /**
   * @param output Channel that receives the bridge's stderr and lifecycle messages.
   * @param clientVersion Version string sent in `ide.initialize`.
   * @param askPassword Asks for a credential's password when the bridge reports error -32010.
   */
  constructor(
    private readonly output: vscode.OutputChannel,
    private readonly clientVersion: string,
    private readonly askPassword: PasswordPrompt = inputBoxPassword,
  ) {}

  /** Current process status. */
  get status(): BridgeStatus {
    return this.currentStatus;
  }

  /**
   * Process id of the bridge process (for diagnostics and tests). With a wrapper command such
   * as `uv run ... calu` this is the wrapper's pid.
   *
   * @returns The pid, or `undefined` if no bridge process runs.
   */
  get pid(): number | undefined {
    return this.running?.proc.pid;
  }

  /**
   * Start the bridge if it is not running, and wait until `ide.initialize` succeeded.
   *
   * Concurrent calls share one start. Postcondition on success: `status === "running"` and
   * `initializeResult` is set.
   *
   * @returns The `ide.initialize` result.
   * @throws (rejects) Error if no workspace folder is open, calu is not found (`status` is then
   *   "missing"), the process cannot be spawned, or `ide.initialize` fails (`status` "error").
   *   The message is on `onStatus` and in the output channel; no dialog is shown.
   */
  ensureStarted(): Promise<IdeInitializeResult> {
    if (this.running && this.initializeResult && this.currentStatus === 'running') {
      return Promise.resolve(this.initializeResult);
    }
    this.starting ??= this.startProcess().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  /**
   * Stop the bridge, then start it again.
   *
   * @returns The new `ide.initialize` result.
   * @throws (rejects) Like `ensureStarted`.
   */
  async restart(): Promise<IdeInitializeResult> {
    await this.stop();
    return this.ensureStarted();
  }

  /**
   * Send a request to the running bridge. If the bridge answers that a credential needs its
   * password (error -32010), the user is asked and the request is repeated with `password`,
   * at most `PASSWORD_ATTEMPTS` times; the password is not kept.
   *
   * @param method A bridge request method.
   * @param params Its params (without `password`).
   * @param interactive False for requests the user did not ask for (start-up, timers): no
   *   password prompt is shown and error -32010 is passed to the caller.
   * @returns The response's `result`.
   * @throws (rejects) Error if the bridge is not running; RpcRequestError for an error answer
   *   (also -32010 if the user cancelled the password prompt or ran out of attempts);
   *   RpcClosedError if the bridge exits before answering.
   */
  async request<M extends BridgeMethod>(
    method: M,
    params: BridgeRequestParams[M],
    interactive = true,
  ): Promise<BridgeRequestResults[M]> {
    let password: string | undefined;
    for (let attempt = 0; ; attempt++) {
      const running = this.running;
      if (!running || running.rpc.closed) throw new Error('The calu bridge is not running.');
      try {
        const sent = password === undefined ? params : { ...params, password };
        return await running.rpc.request<BridgeRequestResults[M]>(method, sent);
      } catch (error) {
        const credential =
          error instanceof RpcRequestError && error.code === BridgeError.NEEDS_PASSWORD
            ? neededCredential(error.data)
            : undefined;
        if (!interactive || !credential || attempt >= PASSWORD_ATTEMPTS) throw error;
        password = await this.askPassword(credential, password !== undefined);
        if (password === undefined) throw error;
      }
    }
  }

  /**
   * Send a request of the session protocol to the attached session (`session.call`).
   *
   * @param method A session request method (see `protocol.generated.ts`).
   * @param params Its params.
   * @returns The session's result.
   * @throws (rejects) Like `request`; RpcRequestError -32015 if no session is attached; the
   *   session's own errors are passed through with their code.
   */
  async session<M extends RequestMethod>(method: M, params: SessionParamsOf<M>): Promise<SessionResultOf<M>> {
    return (await this.request('session.call', { method, params })) as SessionResultOf<M>;
  }

  /**
   * Stop the bridge: send `ide.shutdown`, close stdin, wait for the exit and kill the process
   * after `STOP_TIMEOUT_MS`. Does nothing if no process runs. Postcondition: `status ===
   * "stopped"`. Sessions keep running: they belong to `calu serve`, not to the bridge.
   *
   * @returns A promise that resolves once the process has exited (never rejects).
   */
  async stop(): Promise<void> {
    const running = this.running;
    if (!running) {
      this.setStatus('stopped');
      return;
    }
    running.stopping = true;
    const timeout = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
    try {
      await Promise.race([running.rpc.request('ide.shutdown', {}), timeout(STOP_TIMEOUT_MS)]);
    } catch {
      // The process may already be gone; the exit wait below decides.
    }
    running.proc.stdin.end();
    await Promise.race([running.exited, timeout(STOP_TIMEOUT_MS)]);
    if (running.proc.exitCode === null && running.proc.signalCode === null) {
      killProcessTree(running.proc, 'SIGKILL');
      await running.exited;
    }
    this.setStatus('stopped');
  }

  /**
   * Kill the process without the shutdown handshake (extension deactivation) and release the
   * emitters.
   *
   * @returns Nothing.
   */
  dispose(): void {
    if (this.running) {
      this.running.stopping = true;
      const proc = this.running.proc;
      killProcessTree(proc, 'SIGTERM');
      setTimeout(() => killProcessTree(proc, 'SIGKILL'), STOP_TIMEOUT_MS).unref();
    }
    this.notificationEmitter.dispose();
    this.statusEmitter.dispose();
  }

  /**
   * Spawn the process and run `ide.initialize` (body of `ensureStarted`).
   *
   * @returns The `ide.initialize` result.
   * @throws (rejects) See `ensureStarted`.
   */
  private async startProcess(): Promise<IdeInitializeResult> {
    this.setStatus('starting');
    const folder = bridgeWorkspaceFolder();
    if (!folder) return this.fail('error', 'Open a folder to use calu.');
    const resolved = resolveCaluCommandForHost();
    if (!resolved.ok) return this.fail('missing', resolved.message);
    const argv = buildIdeArgv(resolved.command);
    this.output.appendLine(`[calu] starting the bridge (${resolved.source}): ${argv.join(' ')}`);
    let running: Running;
    try {
      running = this.spawnBridge(argv, folder.uri.fsPath);
    } catch (error) {
      return this.fail('error', `Cannot start calu: ${errorText(error)}`);
    }
    this.running = running;
    try {
      const result = await running.rpc.request<IdeInitializeResult>('ide.initialize', {
        client: 'vscode',
        client_version: this.clientVersion,
      });
      if (result.protocol_version !== undefined && result.protocol_version !== PROTOCOL_VERSION) {
        void vscode.window.showWarningMessage(
          `calu speaks protocol ${result.protocol_version}, the extension ${PROTOCOL_VERSION}. ` +
            'Update both to the same version ("calu: Update calu").',
        );
      }
      this.initializeResult = result;
      this.setStatus('running', undefined, result.bridge_version);
      return result;
    } catch (error) {
      running.stopping = true;
      killProcessTree(running.proc, 'SIGKILL');
      return this.fail('error', `calu ide did not initialize: ${errorText(error)}`);
    }
  }

  /**
   * Spawn the process and connect an RpcClient to its pipes.
   *
   * @param argv Full command line (argv[0] is the executable).
   * @param cwd Working directory (the workspace folder).
   * @returns The running record. Exit handling is installed: an exit closes the RpcClient and
   *   sets the status to "exited" (unexpected) or leaves it to `stop()`.
   * @throws Error if `spawn` throws synchronously (e.g. invalid arguments).
   */
  private spawnBridge(argv: string[], cwd: string): Running {
    // On Linux/macOS the bridge leads its own process group, so killProcessTree can end it
    // together with a wrapper such as `uv run`.
    const proc = spawn(argv[0], argv.slice(1), {
      cwd,
      env: process.env,
      stdio: 'pipe',
      detached: process.platform !== 'win32',
    });
    const rpc = new RpcClient(
      (line) => {
        proc.stdin.write(line);
      },
      (event) => this.notificationEmitter.fire(event),
      (message) => this.output.appendLine(`[calu] protocol: ${message}`),
    );
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (chunk: string) => rpc.feed(chunk));
    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', (chunk: string) => this.output.append(chunk));
    proc.stdin.on('error', (error) => this.output.appendLine(`[calu] stdin: ${error.message}`));
    const running: Running = {
      proc,
      rpc,
      stopping: false,
      exited: new Promise<void>((resolve) => {
        let done = false;
        const onExit = (description: string) => {
          if (done) return;
          done = true;
          rpc.close(`calu ide ${description}`);
          this.output.appendLine(`[calu] the bridge ${description}`);
          if (this.running === running) {
            this.running = undefined;
            this.initializeResult = undefined;
            if (!running.stopping) this.setStatus('exited', `calu ide ${description}.`);
          }
          resolve();
        };
        proc.on('error', (error) => onExit(`could not run: ${error.message}`));
        proc.on('exit', (code, signal) => onExit(signal ? `was killed by ${signal}` : `exited with code ${code}`));
      }),
    };
    return running;
  }

  /**
   * Set an error status, log the message, and throw it.
   *
   * @param status "error" or "missing".
   * @param message User-facing message.
   * @returns Never returns normally.
   * @throws Error with `message`, always.
   */
  private fail(status: BridgeStatus, message: string): never {
    this.setStatus(status, message);
    this.output.appendLine(`[calu] ${message}`);
    throw new Error(message);
  }

  /**
   * Update the status and fire `onStatus` if it changed (or a message is attached).
   *
   * @param status The new status.
   * @param message Optional explanation (shown in the chat view).
   * @param version calu's version, with "running".
   * @returns Nothing.
   */
  private setStatus(status: BridgeStatus, message?: string, version?: string): void {
    if (status === this.currentStatus && message === undefined) return;
    this.currentStatus = status;
    this.statusEmitter.fire({ status, message, version });
  }
}

/**
 * Return a printable message for any thrown value.
 *
 * @param error The caught value.
 * @returns For an RpcRequestError the bridge's message without the method prefix and code; for
 *   other Errors `error.message`; else `String(error)`.
 */
export function errorText(error: unknown): string {
  if (error instanceof RpcRequestError) {
    return error.message.replace(/^[\w.]+: /, '').replace(/ \(code -?\d+\)$/, '');
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * Return the error code of a failed bridge request.
 *
 * @param error The caught value.
 * @returns The JSON-RPC error code, or `undefined` if the value is not an RpcRequestError.
 */
export function errorCode(error: unknown): number | undefined {
  return error instanceof RpcRequestError ? error.code : undefined;
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
