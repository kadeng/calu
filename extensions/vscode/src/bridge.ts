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
 *         error -32010, interactive ──► one prompt (shared by concurrent requests)
 *                                         ├─ password ──► ide.unlock ── ok ──► same request again
 *                                         │                           └─ refused ──► prompt again
 *                                         └─ Escape ──► the waiting requests fail, `locked` is true
 *         error -32010, background  ──► passed to the caller, never a prompt
 *     session(method, params) = request("session.call", {method, params})
 *
 *     start: ide.initialize ok ──► stored password? ──► ide.unlock ── refused ──► forget it
 *
 * The decisions of the password flow are in `shared/unlock.ts`; this file only connects them to
 * the input box, the password store (`PasswordDeps`) and the bridge. A password is passed from
 * the prompt or the store to `ide.unlock` and is never logged, never put into an event and never
 * kept in a field of this class. The store that `extension.ts` passes in is a variable of the
 * extension host (spec section 10): the password lives until the window closes, so a restarted
 * bridge is unlocked again without asking. The bridge holds the logins it opened until it exits.
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
  unlockedLogins,
  type BridgeMethod,
  type BridgeRequestParams,
  type BridgeRequestResults,
  type IdeInitializeResult,
  type IdeUnlockResult,
  type SessionParamsOf,
  type SessionResultOf,
} from './bridgeProtocol';
import { buildIdeArgv, looksLikeWsl, resolveCaluCommand, type HostFacts, type ResolveResult } from './caluCommand';
import { PROTOCOL_VERSION, type RequestMethod } from './protocol.generated';
import { RpcClient, RpcRequestError, type RpcEvent } from './rpc';
import {
  PROMPT_TITLE,
  initialUnlockState,
  onDismiss,
  onExplicitUnlock,
  onNeedsPassword,
  onRoundAborted,
  onStatusLocked,
  onStoredPassword,
  onStoredResult,
  onUnlockRefused,
  onUnlockResult,
  promptText,
  type PasswordNeed,
  type PromptKind,
  type RoundStep,
  type UnlockState,
} from './shared/unlock';
import type { BridgeStatus } from './shared/viewState';

/** Milliseconds to wait for `ide.shutdown` and for the process to exit before it is killed. */
const STOP_TIMEOUT_MS = 5000;

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
 * What the password flow needs from its surroundings. `Bridge` takes any subset; the missing
 * members come from `inputBoxPasswordDeps()`.
 */
export interface PasswordDeps {
  /**
   * Ask the user for a password.
   *
   * @param kind "first" for the one password, "admin" for the admin password that changing
   *   settings needs.
   * @param problem Why the previous entry was not accepted (the bridge's message); `undefined`
   *   for the first entry of a prompt round.
   * @returns The password, or `undefined` if the user pressed Escape.
   */
  ask(kind: PromptKind, problem: string | undefined): Thenable<string | undefined>;
  /**
   * Read the password remembered on this machine (VSCode secret storage).
   *
   * @returns The password, or `undefined` if none is stored.
   */
  stored(): Thenable<string | undefined>;
  /**
   * Remember a password on this machine.
   *
   * @param password The password that `ide.unlock` just accepted.
   * @returns Resolves once it is stored.
   */
  store(password: string): Thenable<void>;
  /**
   * Remove the remembered password.
   *
   * @returns Resolves once it is removed (also if none was stored).
   */
  forget(): Thenable<void>;
  /**
   * Offer "Remember on this machine" after the first successful interactive unlock of a bridge
   * process. The implementation decides whether it shows anything (e.g. not after "Never ask").
   *
   * @returns True if the password is to be stored.
   */
  offerRemember(): Thenable<boolean>;
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
    ask: (kind, problem) =>
      vscode.window.showInputBox({
        title: PROMPT_TITLE,
        prompt: promptText(kind, problem),
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
 * Check whether a thrown value is the bridge's error -32010 (a password is needed or wrong).
 *
 * @param error The caught value.
 * @returns True for an RpcRequestError with code `BridgeError.NEEDS_PASSWORD`.
 */
function isNeedsPassword(error: unknown): error is RpcRequestError {
  return error instanceof RpcRequestError && error.code === BridgeError.NEEDS_PASSWORD;
}

/** One running bridge process with its RPC connection. */
interface Running {
  proc: ChildProcessWithoutNullStreams;
  rpc: RpcClient;
  exited: Promise<void>;
  /** Set by `stop()` so that the exit is not reported as a crash. */
  stopping: boolean;
  /** Password state of this process (`shared/unlock.ts`); a new process starts with a new one. */
  unlock: UnlockState;
  /** The open prompt round; resolves true if it ended with a successful `ide.unlock`. */
  round: Promise<boolean> | undefined;
}

/** Owner of the bridge process. Create one per extension activation; dispose on deactivate. */
export class Bridge implements vscode.Disposable {
  private readonly notificationEmitter = new vscode.EventEmitter<RpcEvent>();
  private readonly statusEmitter = new vscode.EventEmitter<{ status: BridgeStatus; message?: string; version?: string }>();
  private running: Running | undefined;
  private starting: Promise<IdeInitializeResult> | undefined;
  private currentStatus: BridgeStatus = 'stopped';
  private readonly lockedEmitter = new vscode.EventEmitter<boolean>();
  private readonly passwords: PasswordDeps;
  /** The value of `locked` that `onLockedChange` reported last. */
  private reportedLocked = false;
  /** Result of the last successful `ide.initialize`; undefined while not running. */
  initializeResult: IdeInitializeResult | undefined;

  /** Fires for every notification of the bridge, in arrival order. */
  readonly onNotification = this.notificationEmitter.event;
  /** Fires when `status` changes. */
  readonly onStatus = this.statusEmitter.event;
  /** Fires with the new value when `locked` changes. The payload never contains a password. */
  readonly onLockedChange: vscode.Event<boolean> = this.lockedEmitter.event;

  /**
   * @param output Channel that receives the bridge's stderr and lifecycle messages.
   * @param clientVersion Version string sent in `ide.initialize`.
   * @param passwords The password prompt and the secret storage; members that are left out
   *   come from `inputBoxPasswordDeps()` (input box, nothing remembered).
   */
  constructor(
    private readonly output: vscode.OutputChannel,
    private readonly clientVersion: string,
    passwords: Partial<PasswordDeps> = {},
  ) {
    this.passwords = { ...inputBoxPasswordDeps(), ...passwords };
  }

  /**
   * True if a protected credential exists and the bridge holds no login for it: after Escape in
   * the password prompt, after a refused stored password, after error -32010 without a held
   * login, or as last reported through `noteStatusLocked`. False after a successful unlock and
   * while no bridge process runs.
   */
  get locked(): boolean {
    return this.running?.unlock.locked ?? false;
  }

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
   * Send a request to the running bridge. If the bridge answers that a password is needed
   * (error -32010) and the request is interactive, the user is asked once (one prompt for all
   * concurrent requests), the password goes to the bridge with `ide.unlock`, and the request is
   * sent again without a password. The rules are those of `onNeedsPassword` in
   * `shared/unlock.ts`. `ide.unlock` itself is sent as it is.
   *
   * @param method A bridge request method.
   * @param params Its params (without `password`).
   * @param interactive False for requests the user did not ask for (start-up, timers): no
   *   password prompt is shown and error -32010 is passed to the caller.
   * @returns The response's `result`.
   * @throws (rejects) Error if the bridge is not running; RpcRequestError for an error answer
   *   (also -32010 if the user pressed Escape, ran out of attempts, or the request started
   *   before an Escape); RpcClosedError if the bridge exits before answering.
   */
  async request<M extends BridgeMethod>(
    method: M,
    params: BridgeRequestParams[M],
    interactive = true,
  ): Promise<BridgeRequestResults[M]> {
    const startedAtDismissals = this.running?.unlock.dismissals ?? 0;
    for (let retries = 0; ; retries++) {
      const running = this.requireRunning();
      const sentAtUnlocks = running.unlock.unlocks;
      try {
        return await running.rpc.request<BridgeRequestResults[M]>(method, params);
      } catch (error) {
        if (!isNeedsPassword(error) || method === 'ide.unlock') throw error;
        const unlockedReported = unlockedLogins(error.data);
        const need = { interactive, unlockedReported, startedAtDismissals, sentAtUnlocks, retries };
        if (!(await this.unlockFor(running, need))) throw error;
      }
    }
  }

  /**
   * Open the password prompt now (the chat's "Unlock" button, the command "calu: Unlock"),
   * also after an earlier Escape. Starts the bridge if it does not run. Joins a prompt that is
   * already open. Asks the admin password if a login is held that may not change settings, and
   * nothing if a login that may change settings is held.
   *
   * @returns True if the bridge holds a login afterwards, false after Escape or after too many
   *   wrong passwords.
   * @throws (rejects) Like `ensureStarted` if the bridge cannot be started.
   */
  async unlock(): Promise<boolean> {
    await this.ensureStarted();
    const running = this.requireRunning();
    const decision = onExplicitUnlock(running.unlock);
    this.setUnlock(running, decision.state);
    let opened = false;
    if (decision.action === 'join') opened = await (running.round ?? Promise.resolve(false));
    if (decision.action === 'prompt-first') opened = await this.startRound(running, 'first');
    if (decision.action === 'prompt-admin') opened = await this.startRound(running, 'admin');
    return opened || running.unlock.unlocked.length > 0;
  }

  /**
   * Remove the password remembered on this machine ("calu: Forget stored password"). The
   * logins the running bridge holds are kept until it exits.
   *
   * @returns Resolves once `PasswordDeps.forget` finished.
   * @throws (rejects) What `PasswordDeps.forget` rejects with.
   */
  async forgetPassword(): Promise<void> {
    await this.passwords.forget();
  }

  /**
   * Take over the bridge's own report of `locked` (`ide.status.locked`), so that `locked` is
   * right before any prompt was shown. Fires `onLockedChange` if the value changes. Does
   * nothing while no bridge process runs.
   *
   * @param locked The reported value; pass false if the member is missing.
   * @returns Nothing.
   */
  noteStatusLocked(locked: boolean): void {
    const running = this.running;
    if (running) this.setUnlock(running, onStatusLocked(running.unlock, locked));
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
    this.lockedEmitter.dispose();
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
    let result: IdeInitializeResult;
    try {
      result = await running.rpc.request<IdeInitializeResult>('ide.initialize', {
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
    } catch (error) {
      running.stopping = true;
      killProcessTree(running.proc, 'SIGKILL');
      return this.fail('error', `calu ide did not initialize: ${errorText(error)}`);
    }
    await this.unlockWithStored(running);
    if (this.running !== running) return this.fail('error', 'calu ide exited while it started.');
    this.setStatus('running', undefined, result.bridge_version);
    return result;
  }

  /**
   * Return the running bridge process.
   *
   * @returns The record of the process.
   * @throws Error if no bridge process runs or its connection is closed.
   */
  private requireRunning(): Running {
    const running = this.running;
    if (!running || running.rpc.closed) throw new Error('The calu bridge is not running.');
    return running;
  }

  /**
   * Replace the password state of a bridge process and fire `onLockedChange` if `locked`
   * changed. The state of a process that is no longer the current one is stored but not
   * reported.
   *
   * @param running The process the state belongs to.
   * @param state The new state.
   * @returns Nothing.
   */
  private setUnlock(running: Running, state: UnlockState): void {
    running.unlock = state;
    this.reportLocked();
  }

  /**
   * Fire `onLockedChange` if `locked` differs from the value reported last.
   *
   * @returns Nothing.
   */
  private reportLocked(): void {
    const locked = this.locked;
    if (locked === this.reportedLocked) return;
    this.reportedLocked = locked;
    this.lockedEmitter.fire(locked);
  }

  /**
   * Handle error -32010 of one request: fail, retry, wait for the open prompt, or open one
   * (`onNeedsPassword`).
   *
   * @param running The process that answered.
   * @param need The request's facts.
   * @returns True if the request is to be sent again, false if its caller gets the error.
   *   Never rejects.
   */
  private async unlockFor(running: Running, need: PasswordNeed): Promise<boolean> {
    const decision = onNeedsPassword(running.unlock, need);
    this.setUnlock(running, decision.state);
    switch (decision.action) {
      case 'fail':
        return false;
      case 'retry':
        return true;
      case 'join':
        return running.round ?? false;
      case 'prompt-first':
        return this.startRound(running, 'first');
      case 'prompt-admin':
        return this.startRound(running, 'admin');
    }
  }

  /**
   * Start a prompt round and register it as the open one, so that other requests can wait for
   * it. Precondition: `running.unlock.promptOpen === kind` and no round is registered.
   *
   * @param running The process to unlock.
   * @param kind Which password is asked.
   * @returns The round's promise: true after a successful `ide.unlock`, false after Escape,
   *   too many wrong passwords or any failure. Never rejects. Postcondition when it resolves:
   *   no prompt is open in the state and no round is registered.
   */
  private startRound(running: Running, kind: PromptKind): Promise<boolean> {
    const round = this.runRound(running, kind)
      .catch((error: unknown) => {
        this.output.appendLine(`[calu] unlocking failed: ${errorText(error)}`);
        return false;
      })
      .then((opened) => {
        running.round = undefined;
        if (running.unlock.promptOpen) this.setUnlock(running, onRoundAborted(running.unlock));
        return opened;
      });
    running.round = round;
    return round;
  }

  /**
   * Ask for a password and send it with `ide.unlock` until it is accepted, the user presses
   * Escape, or `MAX_ATTEMPTS_PER_ROUND` entries were refused. After the first accepted
   * password of a process, "Remember on this machine" is offered without waiting for the answer.
   *
   * @param running The process to unlock.
   * @param kind Which password is asked.
   * @returns True if the round ended with an accepted password.
   * @throws (rejects) What `PasswordDeps.ask` rejects with, and errors of `ide.unlock` other
   *   than -32010 (the bridge exited, the store cannot be read). `startRound` handles both.
   */
  private async runRound(running: Running, kind: PromptKind): Promise<boolean> {
    let problem: string | undefined;
    for (let attempt = 1; ; attempt++) {
      const password = await this.passwords.ask(kind, problem);
      if (password === undefined) {
        this.setUnlock(running, onDismiss(running.unlock));
        return false;
      }
      const step = await this.tryPassword(running, kind, password, attempt);
      this.setUnlock(running, step.state);
      if (step.offerRemember) this.offerRemember(password);
      if (step.outcome !== 'ask-again') return step.outcome === 'done';
      problem = step.problem;
    }
  }

  /**
   * Send one entered password with `ide.unlock` and let `shared/unlock.ts` judge the answer.
   *
   * @param running The process to unlock.
   * @param kind The kind of the open prompt.
   * @param password The entered password; not logged and not kept.
   * @param attempt Number of entries in this round so far, this one included.
   * @returns The step for an accepted or a refused (-32010) password.
   * @throws (rejects) Every other error of the request.
   */
  private async tryPassword(running: Running, kind: PromptKind, password: string, attempt: number): Promise<RoundStep> {
    try {
      const result = await running.rpc.request<IdeUnlockResult>('ide.unlock', { password });
      return onUnlockResult(running.unlock, kind, unlockFacts(result), attempt);
    } catch (error) {
      if (!isNeedsPassword(error)) throw error;
      return onUnlockRefused(running.unlock, errorText(error), attempt);
    }
  }

  /**
   * Offer "Remember on this machine" and store the password if the user accepts. Runs in the
   * background: the caller does not wait. Failures are logged without the password.
   *
   * @param password The password that `ide.unlock` just accepted. It lives in this call until
   *   the offer is answered, and nowhere else.
   * @returns Nothing.
   */
  private offerRemember(password: string): void {
    const offer = async (): Promise<void> => {
      if (await this.passwords.offerRemember()) await this.passwords.store(password);
    };
    offer().catch((error: unknown) => {
      this.output.appendLine(`[calu] the password could not be remembered: ${errorText(error)}`);
    });
  }

  /**
   * Send the password remembered on this machine with `ide.unlock`, right after
   * `ide.initialize`. A refused password (error -32010) is removed from the secret storage;
   * the user is then asked by the next interactive request that needs a password. Other
   * failures (database unreachable, secret storage unavailable) are logged and change nothing.
   *
   * @param running The new process.
   * @returns Resolves when done. Never rejects.
   */
  private async unlockWithStored(running: Running): Promise<void> {
    try {
      const password = await this.passwords.stored();
      if (password === undefined || onStoredPassword(password) === 'skip') return;
      const step = await this.tryStored(running, password);
      this.setUnlock(running, step.state);
      if (!step.forget) return;
      this.output.appendLine('[calu] the remembered password was refused; it is removed.');
      await this.passwords.forget();
    } catch (error) {
      this.output.appendLine(`[calu] the remembered password was not used: ${errorText(error)}`);
    }
  }

  /**
   * Send the remembered password with `ide.unlock` and let `onStoredResult` judge the answer.
   *
   * @param running The new process.
   * @param password The remembered password; not logged and not kept.
   * @returns Whether to forget the password, and the new state. Errors other than -32010 are
   *   logged (message only) and answer `forget: false`. Never rejects.
   */
  private async tryStored(running: Running, password: string): Promise<{ forget: boolean; state: UnlockState }> {
    try {
      const result = await running.rpc.request<IdeUnlockResult>('ide.unlock', { password });
      return onStoredResult(running.unlock, { ok: true, result: unlockFacts(result) });
    } catch (error) {
      const refused = isNeedsPassword(error);
      if (!refused) this.output.appendLine(`[calu] unlocking with the remembered password failed: ${errorText(error)}`);
      return onStoredResult(running.unlock, { ok: false, refused });
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
      unlock: initialUnlockState(),
      round: undefined,
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
            this.reportLocked();
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
 * Reduce an `ide.unlock` result to the two facts the decisions use, tolerating missing members.
 *
 * @param result The result as received (any JSON value).
 * @returns The login names in `result.unlocked` (`[]` if missing) and `changing === true`.
 */
function unlockFacts(result: unknown): { unlocked: string[]; changing: boolean } {
  const changing = typeof result === 'object' && result !== null && (result as IdeUnlockResult).changing === true;
  return { unlocked: unlockedLogins(result), changing };
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
