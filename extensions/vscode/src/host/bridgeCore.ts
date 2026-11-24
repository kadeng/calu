/**
 * The client of the bridge (`calu ide`): JSON-RPC, the start-up handshake and the password flow,
 * independent of how the bridge is reached. The VSCode extension starts `calu ide` as a child
 * process (`src/bridge.ts`); the browser pane of `calu web` talks to a bridge that `calu web`
 * runs for it over a WebSocket (`pane/`). Both pass a `Launcher`; everything else is here.
 *
 *     status: stopped --ensureStarted()--> starting --ide.initialize ok--> running
 *                                             |                               |
 *                        calu not found ------+--> missing                    |
 *                        launch/initialize error --> error   unexpected end --> exited
 *     running --stop()--> stopped        exited/error/missing --ensureStarted()/restart()--> starting
 *
 *     request(method, params) ──► calu ide
 *         error -32010, interactive ──► one prompt for the calu user password (shared by
 *                                       concurrent requests)
 *                                         ├─ password ──► ide.unlock ── ok ──► same request again
 *                                         │                           └─ refused ──► prompt again
 *                                         └─ Escape ──► the waiting requests fail with
 *                                                       NOT_DONE_NO_PASSWORD, `locked` is true
 *         error -32010, background  ──► passed to the caller, never a prompt
 *     session(method, params) = request("session.call", {method, params})
 *
 *     start: ide.initialize ok ──► stored password? ──► ide.unlock ── refused ──► forget it
 *
 * The decisions of the password flow are in `shared/unlock.ts`; this file connects them to the
 * prompt and the password store (`PasswordDeps`). A password goes from the prompt or the store to
 * `ide.unlock` only: it is never logged, never put into an event and never kept in a field.
 *
 * No `vscode` and no Node.js import: the module runs in the extension host and in a browser.
 */

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
} from '../bridgeProtocol';
import { PROTOCOL_VERSION, type RequestMethod } from '../protocol.generated';
import { RpcClient, RpcRequestError, type RpcEvent } from '../rpc';
import {
  initialUnlockState,
  notDoneText,
  onDismiss,
  onExplicitUnlock,
  onNeedsPassword,
  onRoundAborted,
  onStatusLocked,
  onStoredPassword,
  onStoredResult,
  onUnlockRefused,
  onUnlockResult,
  type PasswordNeed,
  type RoundEnd,
  type RoundStep,
  type UnlockState,
} from '../shared/unlock';
import type { BridgeStatus } from '../shared/viewState';
import { Emitter, type Listen } from './emitter';

/** Milliseconds to wait for `ide.shutdown` before the connection is ended anyway. */
export const STOP_TIMEOUT_MS = 5000;

/** What the password flow needs from its surroundings. */
export interface PasswordDeps {
  /**
   * Ask the user for the calu user password (the text is `promptText(problem, left)`).
   *
   * @param problem Why the previous entry was not accepted (the bridge's message); `undefined`
   *   for the first entry of a prompt round.
   * @param left How many entries the round still allows after a refused one; `undefined` for
   *   the first entry.
   * @returns The password, or `undefined` if the user cancelled.
   */
  ask(problem: string | undefined, left: number | undefined): PromiseLike<string | undefined>;
  /**
   * Read the remembered password.
   *
   * @returns The password, or `undefined` if none is remembered.
   */
  stored(): PromiseLike<string | undefined>;
  /**
   * Remember a password.
   *
   * @param password The password that `ide.unlock` just accepted.
   * @returns Resolves once it is stored.
   */
  store(password: string): PromiseLike<void>;
  /**
   * Remove the remembered password.
   *
   * @returns Resolves once it is removed (also if none was remembered).
   */
  forget(): PromiseLike<void>;
  /**
   * Offer to remember the password after the first successful interactive unlock of a bridge.
   *
   * @returns True if the password is to be remembered.
   */
  offerRemember(): PromiseLike<boolean>;
}

/** A connection to one bridge, as a `Launcher` opened it. */
export interface Launched {
  /**
   * Send one complete protocol line ("\n" included).
   *
   * @param line The line.
   * @throws Error if the connection cannot take it (the request that sent it then fails).
   */
  write(line: string): void;
  /**
   * End the connection after `ide.shutdown` was answered or timed out: close the sending side
   * and wait until the bridge is gone, ending it by force after `STOP_TIMEOUT_MS`.
   *
   * @returns Resolves once the bridge is gone (never rejects).
   */
  end(): Promise<void>;
  /**
   * End the bridge at once, without the shutdown handshake.
   *
   * @returns Nothing.
   */
  kill(): void;
  /** Process id of the bridge, if it is a local process (diagnostics and tests). */
  readonly pid?: number;
}

/** A launch that failed before a connection existed. */
export class LaunchError extends Error {
  /**
   * @param status "missing" if calu is not installed, "error" for any other failure.
   * @param message What failed, for the user.
   */
  constructor(
    readonly status: 'missing' | 'error',
    message: string,
  ) {
    super(message);
  }
}

/** Opens connections to a bridge. */
export interface Launcher {
  /**
   * Open a connection.
   *
   * @param onData Called with each chunk the bridge sends (any split into lines).
   * @param onEnd Called once when the connection ends, with a description such as "exited with
   *   code 1" (it completes "calu ide ...").
   * @returns The connection, or a promise of it.
   * @throws LaunchError (or rejects with it) if no connection can be opened.
   */
  launch(onData: (chunk: string) => void, onEnd: (description: string) => void): Launched | Promise<Launched>;
}

/** Logging and the one warning the client shows. */
export interface BridgeLog {
  /** Append a line to the log. */
  line(text: string): void;
  /** Append text as it came (the bridge's stderr). */
  append(text: string): void;
  /** Show a warning to the user (a protocol version mismatch). */
  warn(text: string): void;
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

/** One open connection with its RPC client. */
interface Running {
  link: Launched;
  rpc: RpcClient;
  ended: Promise<void>;
  /** Set by `stop()` so that the end is not reported as a crash. */
  stopping: boolean;
  /** Password state of this bridge (`shared/unlock.ts`); a new bridge starts with a new one. */
  unlock: UnlockState;
  /** The open prompt round; resolves to how it ended ("unlocked" after a successful `ide.unlock`). */
  round: Promise<RoundEnd> | undefined;
}

/** The status event of `BridgeCore.onStatus`. */
export interface BridgeStatusEvent {
  status: BridgeStatus;
  message?: string;
  version?: string;
}

/** The bridge client. Create one per window or browser tab. */
export class BridgeCore {
  private readonly notificationEmitter = new Emitter<RpcEvent>();
  private readonly statusEmitter = new Emitter<BridgeStatusEvent>();
  private readonly lockedEmitter = new Emitter<boolean>();
  private running: Running | undefined;
  private starting: Promise<IdeInitializeResult> | undefined;
  private currentStatus: BridgeStatus = 'stopped';
  /** The value of `locked` that `onLockedChange` reported last. */
  private reportedLocked = false;
  /** Result of the last successful `ide.initialize`; undefined while not running. */
  initializeResult: IdeInitializeResult | undefined;

  /** Fires for every notification of the bridge, in arrival order. */
  readonly onNotification: Listen<RpcEvent> = this.notificationEmitter.event;
  /** Fires when `status` changes. */
  readonly onStatus: Listen<BridgeStatusEvent> = this.statusEmitter.event;
  /** Fires with the new value when `locked` changes. The payload never contains a password. */
  readonly onLockedChange: Listen<boolean> = this.lockedEmitter.event;

  /**
   * @param log Where lifecycle messages and the bridge's stderr go.
   * @param client The client name sent in `ide.initialize` ("vscode", "web").
   * @param clientVersion Version string sent in `ide.initialize`.
   * @param launcher Opens the connection to the bridge.
   * @param passwords The password prompt and store.
   */
  constructor(
    protected readonly log: BridgeLog,
    private readonly client: string,
    private readonly clientVersion: string,
    private readonly launcher: Launcher,
    private readonly passwords: PasswordDeps,
  ) {}

  /**
   * True if a protected credential exists and the bridge holds no login for it: after a
   * cancelled password prompt, after a refused stored password, after error -32010 without a
   * held login, or as last reported through `noteStatusLocked`. False after a successful unlock
   * and while no bridge runs.
   */
  get locked(): boolean {
    return this.running?.unlock.locked ?? false;
  }

  /** The current status. */
  get status(): BridgeStatus {
    return this.currentStatus;
  }

  /**
   * Process id of the bridge (for diagnostics and tests).
   *
   * @returns The pid, or `undefined` if no local bridge process runs.
   */
  get pid(): number | undefined {
    return this.running?.link.pid;
  }

  /**
   * Start the bridge if it is not running, and wait until `ide.initialize` succeeded.
   *
   * Concurrent calls share one start. Postcondition on success: `status === "running"` and
   * `initializeResult` is set.
   *
   * @returns The `ide.initialize` result.
   * @throws (rejects) Error if the launcher fails (`status` "missing" or "error") or
   *   `ide.initialize` fails (`status` "error"). The message is on `onStatus` and in the log.
   */
  ensureStarted(): Promise<IdeInitializeResult> {
    if (this.running && this.initializeResult && this.currentStatus === 'running') {
      return Promise.resolve(this.initializeResult);
    }
    // An error that `startBridge` does not turn into a status itself must not leave the view
    // at "Starting calu…" for ever.
    this.starting ??= this.startBridge()
      .catch((error: unknown) => (this.currentStatus === 'starting' ? this.fail('error', `Cannot start calu: ${errorText(error)}`) : Promise.reject(error)))
      .finally(() => {
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
   * @throws (rejects) Error if the bridge is not running; RpcRequestError for an error answer,
   *   also -32010 if no password unlocked the bridge: its message is then `notDoneText`'s (the
   *   user cancelled, before or after the request started; every password of the round was
   *   refused) or else the bridge's own; RpcClosedError if the bridge ends before answering.
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
        const next = await this.unlockFor(running, need);
        if (next === 'retry') continue;
        const text = notDoneText(running.unlock, need, next === 'fail' ? undefined : next);
        if (text === undefined) throw error;
        throw new RpcRequestError(method, { code: error.code, message: text, data: error.data });
      }
    }
  }

  /**
   * Open the password prompt now (the chat's "Unlock" button), also after an earlier cancel.
   * Starts the bridge if it does not run. Joins a prompt that is already open. Asks nothing if
   * a login is held.
   *
   * @returns True if the bridge holds a login afterwards, false after a cancel or after too
   *   many wrong passwords.
   * @throws (rejects) Like `ensureStarted` if the bridge cannot be started.
   */
  async unlock(): Promise<boolean> {
    await this.ensureStarted();
    const running = this.requireRunning();
    const decision = onExplicitUnlock(running.unlock);
    this.setUnlock(running, decision.state);
    let end: RoundEnd | undefined;
    if (decision.action === 'join') end = await running.round;
    if (decision.action === 'prompt') end = await this.startRound(running);
    return end === 'unlocked' || running.unlock.unlocked.length > 0;
  }

  /**
   * Remove the remembered password. The logins the running bridge holds are kept until it ends.
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
   * nothing while no bridge runs.
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
   * Stop the bridge: send `ide.shutdown`, then end the connection (`Launched.end`). Does nothing
   * if no bridge runs. Postcondition: `status === "stopped"`. Sessions keep running: they belong
   * to `calu serve`, not to the bridge.
   *
   * @returns A promise that resolves once the bridge is gone (never rejects).
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
      // The bridge may already be gone; `end` decides.
    }
    await running.link.end();
    await Promise.race([running.ended, timeout(STOP_TIMEOUT_MS)]);
    this.setStatus('stopped');
  }

  /**
   * End the bridge without the shutdown handshake and release the emitters.
   *
   * @returns Nothing.
   */
  dispose(): void {
    if (this.running) {
      this.running.stopping = true;
      this.running.link.kill();
    }
    this.notificationEmitter.dispose();
    this.statusEmitter.dispose();
    this.lockedEmitter.dispose();
  }

  /**
   * Open the connection and run `ide.initialize` (body of `ensureStarted`).
   *
   * @returns The `ide.initialize` result.
   * @throws (rejects) See `ensureStarted`.
   */
  private async startBridge(): Promise<IdeInitializeResult> {
    this.setStatus('starting');
    let running: Running;
    try {
      running = await this.connect();
    } catch (error) {
      if (error instanceof LaunchError) return this.fail(error.status, error.message);
      return this.fail('error', `Cannot start calu: ${errorText(error)}`);
    }
    this.running = running;
    let result: IdeInitializeResult;
    try {
      result = await running.rpc.request<IdeInitializeResult>('ide.initialize', {
        client: this.client,
        client_version: this.clientVersion,
      });
      if (result.protocol_version !== undefined && result.protocol_version !== PROTOCOL_VERSION) {
        this.log.warn(
          `calu speaks protocol ${result.protocol_version}, this client ${PROTOCOL_VERSION}. ` +
            'Update both to the same version.',
        );
      }
      this.initializeResult = result;
    } catch (error) {
      running.stopping = true;
      running.link.kill();
      return this.fail('error', `calu ide did not initialize: ${errorText(error)}`);
    }
    await this.unlockWithStored(running);
    if (this.running !== running) return this.fail('error', 'calu ide ended while it started.');
    this.setStatus('running', undefined, result.bridge_version);
    return result;
  }

  /**
   * Open a connection through the launcher and put an RPC client on it.
   *
   * @returns The record of the connection. Its end is handled: the RPC client is closed and the
   *   status becomes "exited" (unexpected) or is left to `stop()`.
   * @throws LaunchError or any error of the launcher.
   */
  private async connect(): Promise<Running> {
    let ready: Running | undefined;
    let endedEarly: string | undefined;
    let resolveEnded: () => void = () => {};
    const ended = new Promise<void>((resolve) => {
      resolveEnded = resolve;
    });
    let done = false;
    const onEnd = (description: string): void => {
      if (done) return;
      done = true;
      const running = ready;
      if (!running) {
        endedEarly = description;
        resolveEnded();
        return;
      }
      running.rpc.close(`calu ide ${description}`);
      this.log.line(`[calu] the bridge ${description}`);
      if (this.running === running) {
        this.running = undefined;
        this.initializeResult = undefined;
        this.reportLocked();
        if (!running.stopping) this.setStatus('exited', `calu ide ${description}.`);
      }
      resolveEnded();
    };
    const pending: string[] = [];
    let rpc: RpcClient | undefined;
    const link = await this.launcher.launch((chunk) => (rpc ? rpc.feed(chunk) : pending.push(chunk)), onEnd);
    rpc = new RpcClient(
      (line) => link.write(line),
      (event) => this.notificationEmitter.fire(event),
      (message) => this.log.line(`[calu] protocol: ${message}`),
    );
    for (const chunk of pending) rpc.feed(chunk);
    ready = { link, rpc, ended, stopping: false, unlock: initialUnlockState(), round: undefined };
    if (endedEarly !== undefined) {
      rpc.close(`calu ide ${endedEarly}`);
      throw new LaunchError('error', `calu ide ${endedEarly}`);
    }
    return ready;
  }

  /**
   * Return the running bridge.
   *
   * @returns The record of the connection.
   * @throws Error if no bridge runs or its connection is closed.
   */
  private requireRunning(): Running {
    const running = this.running;
    if (!running || running.rpc.closed) throw new Error('The calu bridge is not running.');
    return running;
  }

  /**
   * Replace the password state of a bridge and fire `onLockedChange` if `locked` changed.
   *
   * @param running The bridge the state belongs to.
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
   * @param running The bridge that answered.
   * @param need The request's facts.
   * @returns "retry" if the request is to be sent again; else why its caller gets the error:
   *   "fail" (no prompt was opened or joined) or how the round it waited for ended
   *   ("dismissed", "refused", "aborted"). Never rejects.
   */
  private async unlockFor(running: Running, need: PasswordNeed): Promise<'retry' | 'fail' | Exclude<RoundEnd, 'unlocked'>> {
    const decision = onNeedsPassword(running.unlock, need);
    this.setUnlock(running, decision.state);
    let end: RoundEnd | undefined;
    switch (decision.action) {
      case 'fail':
        return 'fail';
      case 'retry':
        return 'retry';
      case 'join':
        end = await running.round;
        break;
      case 'prompt':
        end = await this.startRound(running);
        break;
    }
    if (end === undefined) return 'fail';
    return end === 'unlocked' ? 'retry' : end;
  }

  /**
   * Start a prompt round and register it as the open one, so that other requests can wait for
   * it. Precondition: `running.unlock.promptOpen` is true and no round is registered.
   *
   * @param running The bridge to unlock.
   * @returns The round's promise: how it ended; "aborted" for any failure. Never rejects.
   */
  private startRound(running: Running): Promise<RoundEnd> {
    const round = this.runRound(running)
      .catch((error: unknown): RoundEnd => {
        this.log.line(`[calu] unlocking failed: ${errorText(error)}`);
        return 'aborted';
      })
      .then((end) => {
        running.round = undefined;
        if (running.unlock.promptOpen) this.setUnlock(running, onRoundAborted(running.unlock));
        return end;
      });
    running.round = round;
    return round;
  }

  /**
   * Ask for a password and send it with `ide.unlock` until it is accepted, the user cancels, or
   * the attempts of a round are used up. After the first accepted password of a bridge, keeping
   * it is offered without waiting for the answer.
   *
   * @param running The bridge to unlock.
   * @returns "unlocked" after an accepted password, "dismissed" after a cancel, "refused" after
   *   `MAX_ATTEMPTS_PER_ROUND` refused passwords.
   * @throws (rejects) What `PasswordDeps.ask` rejects with, and errors of `ide.unlock` other
   *   than -32010. `startRound` handles both.
   */
  private async runRound(running: Running): Promise<RoundEnd> {
    let problem: string | undefined;
    let left: number | undefined;
    for (let attempt = 1; ; attempt++) {
      const password = await this.passwords.ask(problem, left);
      if (password === undefined) {
        this.setUnlock(running, onDismiss(running.unlock));
        return 'dismissed';
      }
      const step = await this.tryPassword(running, password, attempt);
      this.setUnlock(running, step.state);
      if (step.offerRemember) this.offerRemember(password);
      if (step.outcome === 'done') return 'unlocked';
      if (step.outcome === 'give-up') return 'refused';
      problem = step.problem;
      left = step.left;
    }
  }

  /**
   * Send one entered password with `ide.unlock` and let `shared/unlock.ts` judge the answer.
   *
   * @param running The bridge to unlock.
   * @param password The entered password; not logged and not kept.
   * @param attempt Number of entries in this round so far, this one included.
   * @returns The step for an accepted or a refused (-32010) password.
   * @throws (rejects) Every other error of the request.
   */
  private async tryPassword(running: Running, password: string, attempt: number): Promise<RoundStep> {
    try {
      const result = await running.rpc.request<IdeUnlockResult>('ide.unlock', { password });
      return onUnlockResult(running.unlock, unlockFacts(result));
    } catch (error) {
      if (!isNeedsPassword(error)) throw error;
      return onUnlockRefused(running.unlock, errorText(error), attempt);
    }
  }

  /**
   * Offer to remember the password and store it if the user accepts. Runs in the background.
   * Failures are logged without the password.
   *
   * @param password The password that `ide.unlock` just accepted.
   * @returns Nothing.
   */
  private offerRemember(password: string): void {
    const offer = async (): Promise<void> => {
      if (await this.passwords.offerRemember()) await this.passwords.store(password);
    };
    offer().catch((error: unknown) => {
      this.log.line(`[calu] the password could not be remembered: ${errorText(error)}`);
    });
  }

  /**
   * Send the remembered password with `ide.unlock`, right after `ide.initialize`. A refused
   * password (error -32010) is forgotten; other failures are logged and change nothing.
   *
   * @param running The new bridge.
   * @returns Resolves when done. Never rejects.
   */
  private async unlockWithStored(running: Running): Promise<void> {
    try {
      const password = await this.passwords.stored();
      if (password === undefined || onStoredPassword(password) === 'skip') return;
      const step = await this.tryStored(running, password);
      this.setUnlock(running, step.state);
      if (!step.forget) return;
      this.log.line('[calu] the remembered password was refused; it is removed.');
      await this.passwords.forget();
    } catch (error) {
      this.log.line(`[calu] the remembered password was not used: ${errorText(error)}`);
    }
  }

  /**
   * Send the remembered password with `ide.unlock` and let `onStoredResult` judge the answer.
   *
   * @param running The new bridge.
   * @param password The remembered password; not logged and not kept.
   * @returns Whether to forget the password, and the new state. Never rejects.
   */
  private async tryStored(running: Running, password: string): Promise<{ forget: boolean; state: UnlockState }> {
    try {
      const result = await running.rpc.request<IdeUnlockResult>('ide.unlock', { password });
      return onStoredResult(running.unlock, { ok: true, result: unlockFacts(result) });
    } catch (error) {
      const refused = isNeedsPassword(error);
      if (!refused) this.log.line(`[calu] unlocking with the remembered password failed: ${errorText(error)}`);
      return onStoredResult(running.unlock, { ok: false, refused });
    }
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
    this.log.line(`[calu] ${message}`);
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
 * Reduce an `ide.unlock` result to the fact the decisions use, tolerating a missing member.
 *
 * @param result The result as received (any JSON value).
 * @returns The login names in `result.unlocked` (`[]` if missing).
 */
function unlockFacts(result: unknown): { unlocked: string[] } {
  return { unlocked: unlockedLogins(result) };
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
