/**
 * Choosing, starting, renaming and leaving sessions: the shared part of the VSCode extension
 * (`src/sessions.ts`) and the browser pane of `calu web` (`pane/`). Neither starts a session as
 * a child process; both ask the bridge:
 *
 *     pick()        the platform's session picker fed by ide.sessions
 *                     running session      ──► ide.attach {session}
 *                     stored conversation  ──► ide.start {kind: "resume" | "fork", claude_session_id}
 *                     New session…         ──► newSession()
 *     forkBefore()  user message of the chat ──► ide.start {kind: "resume", fork_before_turn}
 *     forkConversation()                     ──► ide.start {kind: "fork"}
 *     newSession()  nothing is asked ──► ide.start {kind: "new"}
 *     ide.start waits ──► `ui.starting {title, slow}`: the chat shows progress with "Cancel"
 *                     (cancelStart() ──► ide.start.cancel) and, after `SLOW_START_MS`, that no
 *                     server may be running, with "Start calu server"
 *     ide.start error -32013 (no server answered) ──► showServerHelp()
 *
 * Every successful attach or start is reported with `onAttached(result)`; the caller turns it
 * into the `ui.attached` view event. The list of running sessions is reported with `ui.sessions`.
 * What differs between the platforms (the picker, input boxes, messages, the server terminal,
 * the server command) is `SessionsUi`.
 *
 * No `vscode` and no Node.js import.
 */

import { BridgeError, type IdeAttachResult, type IdeSessionsResult, type IdeStartParams, type IdeStatusResult } from '../bridgeProtocol';
import type { ServerHelp } from '../shared/serverCommand';
import { buildSessionPicks, derivedSessionName, validateSessionName, type SessionChoice, type SessionPick } from '../shared/sessionPick';
import type { ViewEvent } from '../shared/viewState';
import { errorCode, errorText, type BridgeCore } from './bridgeCore';

/** Milliseconds a session request waits before the chat says that no server may be running. */
export const SLOW_START_MS = 3000;

/** Milliseconds `reattach` waits before the chat says that the session is being resumed. */
export const RESUME_NOTICE_MS = 1000;

/** An input box (or its browser equivalent). */
export interface InputOptions {
  /** Title of the box. */
  title: string;
  /** The question. */
  prompt: string;
  /** Start value. */
  value?: string;
  /** Masked input (passwords). */
  password?: boolean;
  /**
   * Check the text while it is typed.
   *
   * @param text The current text.
   * @returns A problem to show (the box cannot be accepted then), or `undefined`.
   */
  validate?: (text: string) => string | undefined;
}

/** What the session commands need from the platform. */
export interface SessionsUi {
  /** Append a line to the log. */
  log(line: string): void;
  /** Show an error message (not modal). */
  error(text: string): void;
  /** Show an information message (not modal). */
  info(text: string): void;
  /**
   * Ask for a text.
   *
   * @param options The box.
   * @returns The text, or `undefined` if cancelled.
   */
  input(options: InputOptions): Promise<string | undefined>;
  /**
   * Let the user choose among the rows of `buildSessionPicks`.
   *
   * @param picks The rows (separators included).
   * @returns The chosen row's choice and whether "fork" was chosen for a stored conversation;
   *   `undefined` if cancelled.
   */
  pickSession(picks: SessionPick[]): Promise<{ choice: SessionChoice; fork: boolean } | undefined>;
  /**
   * Build the command that starts the calu server on this machine.
   *
   * @param status The last `ide.status`, or null.
   * @returns The command with its tool, detach hint and note.
   */
  serverHelp(status: IdeStatusResult | null): ServerHelp;
  /**
   * Run the server command where the user can type its passwords (VSCode: an integrated
   * terminal). A platform without a terminal shows the command instead.
   *
   * @param help The command.
   * @returns Nothing.
   */
  startServer(help: ServerHelp): void;
  /**
   * Show how to start the server, with ways to run or copy the command.
   *
   * @param reason First sentence of the message.
   * @param help The command.
   * @returns Resolves after the user's choice was handled.
   */
  showServerHelp(reason: string, help: ServerHelp): Promise<void>;
  /**
   * Show that something runs while `work` runs (VSCode: the status bar's progress entry).
   *
   * @param title What runs, e.g. "Starting a session".
   * @param work The work.
   * @returns What `work` returns.
   */
  progress<T>(title: string, work: () => Promise<T>): Promise<T>;
}

/** Session commands (see the module comment). Create one per window or browser tab. */
export class Sessions {
  /** Id of the attached session, or null. */
  attachedId: string | null = null;
  private last: string | null = null;
  /**
   * The address and token of the last `attachEndpoint`, while that session is the one to
   * attach to again: such a session may be found neither by a local socket nor in the
   * database, so `reattach` uses them. Kept in memory only (the token is a secret).
   */
  private endpoint: { sessionId: string; address: string; token: string } | null = null;
  /** The last answer of `ide.status`, or null if it was not asked yet or failed. */
  status: IdeStatusResult | null = null;
  /** True if the last `ide.start` failed because no server answered. */
  noServer = false;
  private refreshing: Promise<void> | undefined;

  /**
   * @param bridge The bridge (started on demand).
   * @param dispatch Applies a view event (`ui.sessions`, `ui.serverHelp`, `ui.detached`,
   *   `ui.starting`, `ui.notice`) to the chat view.
   * @param onAttached Called with the result of every successful `ide.attach` / `ide.start`.
   * @param onChanged Called when `status`, `noServer` or `attachedId` changed (the caller
   *   recomputes the setup hints).
   * @param ui The platform's dialogs, picker and server help.
   * @param sessionName Returns the display name of a running session by its id (its name, else
   *   its directory's last segment), or `undefined` if the session is not known.
   * @param remember Stores the id of the last attached session (or null) where the next
   *   start finds it; called whenever `lastAttachedId` changes.
   */
  constructor(
    protected readonly bridge: BridgeCore,
    protected readonly dispatch: (event: ViewEvent) => void,
    private readonly onAttached: (result: IdeAttachResult, resumed: boolean) => void,
    private readonly onChanged: () => void,
    protected readonly ui: SessionsUi,
    private readonly sessionName: (sessionId: string) => string | undefined = () => undefined,
    private readonly remember: (sessionId: string | null) => void = () => undefined,
  ) {}

  /**
   * Id of the session that was attached last and was not left on purpose: the one to attach
   * to again after the bridge was restarted or the view was reloaded. Cleared by `detach()`
   * and by a failed `reattach()`. Side effect of setting it: `remember` stores it.
   *
   * @returns The id, or null.
   */
  get lastAttachedId(): string | null {
    return this.last;
  }
  set lastAttachedId(id: string | null) {
    if (id === this.last) return;
    this.last = id;
    this.remember(id);
  }

  /**
   * Show the session picker and do what the user chooses (see the module comment).
   *
   * @returns A promise that resolves when the choice was handled or the pick was cancelled.
   *   Failures are shown to the user; the promise does not reject.
   */
  async pick(): Promise<void> {
    let sessions: IdeSessionsResult;
    try {
      await this.bridge.ensureStarted();
      sessions = await this.bridge.request('ide.sessions', {});
    } catch (error) {
      this.dispatch({ method: 'ui.sessions', params: { error: errorText(error) } });
      this.ui.error(`calu: the sessions could not be listed: ${errorText(error)}`);
      return;
    }
    this.dispatch({ method: 'ui.sessions', params: { running: sessions.running } });
    const chosen = await this.ui.pickSession(buildSessionPicks(sessions, this.attachedId));
    if (!chosen) return;
    if (chosen.choice.kind === 'new') await this.newSession();
    else if (chosen.choice.kind === 'attach') await this.attach(chosen.choice.sessionId);
    else if (chosen.fork) await this.fork(chosen.choice.claudeSessionId);
    else await this.start({ kind: 'resume', claude_session_id: chosen.choice.claudeSessionId }, 'Resuming the conversation');
  }

  /**
   * Start a new session in the workspace at once, without asking anything: the first message is
   * typed into the chat's input like every other one, and a name is given later with "Rename
   * session". This way the user learns where the chat input is.
   *
   * @returns True if the session started and is attached; false if the start failed (the
   *   failure was shown).
   */
  newSession(): Promise<boolean> {
    return this.start({ kind: 'new' }, 'Starting a session');
  }

  /**
   * Start a new session that continues a conversation from before one of its turns (the chat's
   * "Rewind to before this message" and "Fork from here"). The session it was cut from keeps
   * running; files are not reverted.
   *
   * @param claudeSessionId Claude Code's id of the conversation.
   * @param turn Number of the first turn the new session does not keep (at least 2).
   * @param rewind True for "Rewind" (the session is named "rewind of <name>"), false for
   *   "Fork from here" ("fork of <name>"); `<name>` is the attached session's.
   * @returns True if the session started and is attached; failures are shown to the user.
   */
  forkBefore(claudeSessionId: string, turn: number, rewind: boolean): Promise<boolean> {
    return this.start(
      { kind: 'resume', claude_session_id: claudeSessionId, fork_before_turn: turn, ...this.derivedName(rewind ? 'rewind' : 'fork') },
      rewind ? 'Rewinding the conversation' : 'Forking the conversation',
    );
  }

  /**
   * Build the `name` parameter of a session that is cut from the attached one.
   *
   * @param how "fork" or "rewind".
   * @returns `{name: "fork of <name>"}` (see `derivedSessionName`), or `{}` if no session is
   *   attached or it is not known by name.
   */
  private derivedName(how: 'fork' | 'rewind'): { name?: string } {
    const from = this.attachedId ? this.sessionName(this.attachedId) : undefined;
    return from ? { name: derivedSessionName(how, from) } : {};
  }

  /**
   * Start a new session from a copy of a whole conversation; the original stays as it is.
   *
   * @param claudeSessionId Claude Code's id of the conversation.
   * @returns True if the session started and is attached; failures are shown to the user.
   */
  forkConversation(claudeSessionId: string): Promise<boolean> {
    return this.start({ kind: 'fork', claude_session_id: claudeSessionId, ...this.derivedName('fork') }, 'Forking the conversation');
  }

  /**
   * Take back the session request that waits for the server (the chat's "Cancel"). The
   * waiting `start()` then ends without a message.
   *
   * @returns A promise that resolves when the bridge answered (failures are logged only).
   */
  async cancelStart(): Promise<void> {
    try {
      await this.bridge.request('ide.start.cancel', {}, false);
    } catch (error) {
      this.ui.log(`[calu] the session request was not taken back: ${errorText(error)}`);
    }
  }

  /**
   * After the bridge started again: attach to the session that was attached before, asking
   * only for the events this view has not seen. Nothing is asked of the user and no error is
   * shown: the caller falls back to the session of the workspace. A session attached by
   * `attachEndpoint` is asked at its address with its token again.
   *
   * A session that no longer runs (its `calu serve` was stopped) is resumed by the bridge
   * through the running server (D280): the result then names another session, whose
   * transcript is loaded whole. While the bridge waits longer than `RESUME_NOTICE_MS`, the
   * chat shows "Resuming the session".
   *
   * @param afterSeq Sequence number of the last event the chat view applied for that session.
   * @returns True if that session, or the session that resumed it, is attached; false if
   *   there was none, it is gone and could not be resumed (e.g. no server runs).
   */
  async reattach(afterSeq: number): Promise<boolean> {
    const session = this.lastAttachedId;
    if (!session) return false;
    const notice = setTimeout(
      () => this.dispatch({ method: 'ui.starting', params: { title: 'Resuming the session', slow: false } }),
      RESUME_NOTICE_MS,
    );
    try {
      await this.bridge.ensureStarted();
      const where = this.endpoint?.sessionId === session ? { address: this.endpoint.address, token: this.endpoint.token } : {};
      let result = await this.bridge.request('ide.attach', { session, after_seq: afterSeq, ...where }, false);
      // Another session id: the bridge resumed the conversation in a new session, whose
      // events start anew. A replay with a gap cannot continue the transcript either: take
      // the whole session again.
      const same = result.session_id === session;
      const resumed = same && afterSeq > 0 && result.complete === true;
      if (same && afterSeq > 0 && !resumed) result = await this.bridge.request('ide.attach', { session, after_seq: 0, ...where }, false);
      clearTimeout(notice);
      this.attached(result, resumed);
      return true;
    } catch (error) {
      clearTimeout(notice);
      this.dispatch({ method: 'ui.starting', params: {} });
      this.ui.log(`[calu] the session ${session.slice(0, 8)} was not attached again: ${errorText(error)}`);
      this.lastAttachedId = null;
      this.endpoint = null;
      return false;
    }
  }

  /**
   * Attach to a running session.
   *
   * @param session Id or id prefix of the session; null = the session of the workspace.
   * @param quiet True for the attempt at start-up: no password prompt, and no error message
   *   when no session matches, several match or the database cannot be asked.
   * @returns True if a session is attached afterwards.
   */
  async attach(session: string | null, quiet = false): Promise<boolean> {
    try {
      await this.bridge.ensureStarted();
      const result = await this.bridge.request('ide.attach', { session, after_seq: 0 }, !quiet);
      this.attached(result);
      return true;
    } catch (error) {
      const code = errorCode(error);
      if (code === BridgeError.AMBIGUOUS_SESSION && session === null) {
        // Several sessions run in the workspace: let the user choose.
        if (!quiet) await this.pick();
        return this.attachedId !== null;
      }
      const expected: (number | undefined)[] = [
        BridgeError.NO_SESSION,
        BridgeError.AMBIGUOUS_SESSION,
        BridgeError.NO_DATABASE,
        BridgeError.NEEDS_PASSWORD,
      ];
      if (!(quiet && expected.includes(code))) this.ui.error(`calu: not attached: ${errorText(error)}`);
      this.ui.log(`[calu] attach failed: ${errorText(error)}`);
      return false;
    }
  }

  /**
   * Attach to a session at an explicit address with a client token (`ide.attach {address,
   * token}`): no local socket and no database is asked. For a session reached through a
   * forwarded port, and for the integration tests, which serve a scripted session without a
   * database.
   *
   * @param address The session's WebSocket address, e.g. "ws://127.0.0.1:40123".
   * @param token A client token the session accepts.
   * @returns A promise that resolves when the session is attached.
   * @throws (rejects) The bridge's error if the address cannot be reached (-32011) or the
   *   session refuses the token (its own code); nothing is shown to the user.
   */
  async attachEndpoint(address: string, token: string): Promise<void> {
    await this.bridge.ensureStarted();
    const result = await this.bridge.request('ide.attach', { session: null, after_seq: 0, address, token }, false);
    this.attached(result);
    this.endpoint = { sessionId: result.session_id, address, token };
  }

  /**
   * Detach from the attached session; the session keeps running.
   *
   * @returns A promise that resolves when the bridge detached (failures are logged only).
   */
  async detach(): Promise<void> {
    try {
      await this.bridge.request('ide.detach', {});
    } catch (error) {
      this.ui.log(`[calu] detach: ${errorText(error)}`);
    }
    this.lastAttachedId = null;
    this.endpoint = null;
    this.markDetached();
    void this.refresh();
  }

  /**
   * Record that no session is attached any more (after a detach, a lost link or a bridge exit).
   *
   * @returns Nothing.
   */
  markDetached(): void {
    this.attachedId = null;
    this.dispatch({ method: 'ui.detached', params: {} });
    this.onChanged();
  }

  /**
   * Ask for a new name of a session and store it (`ide.session.rename`).
   *
   * @param sessionId The session; default: the attached one.
   * @param currentName The name shown as the input's start value.
   * @returns A promise that resolves when the name was stored or the user cancelled. Failures
   *   are shown to the user.
   */
  async rename(sessionId: string | null = this.attachedId, currentName = ''): Promise<void> {
    if (!sessionId) {
      this.ui.info('calu: attach to a session first.');
      return;
    }
    const name = await this.ui.input({
      title: 'calu: rename session',
      prompt: 'Name of the session (at most 80 characters; empty removes the name)',
      value: currentName,
      validate: validateSessionName,
    });
    if (name === undefined) return;
    try {
      await this.bridge.request('ide.session.rename', { session_id: sessionId, name: name.trim() });
    } catch (error) {
      this.ui.error(`calu: the session was not renamed: ${errorText(error)}`);
    }
    await this.refresh();
  }

  /**
   * Load the running sessions and report them with `ui.sessions`. Concurrent calls share one
   * request. Does nothing while the bridge does not run (it is not started for this).
   *
   * @param interactive True if the user asked for the list: a needed password is then asked
   *   for. False (timer, after an attach): the need is reported as the list's `error`.
   * @returns A promise that resolves when the list was reported (never rejects).
   */
  refresh(interactive = false): Promise<void> {
    if (this.bridge.status !== 'running') return Promise.resolve();
    this.refreshing ??= this.bridge
      .request('ide.sessions', {}, interactive)
      .then(
        (sessions) => this.dispatch({ method: 'ui.sessions', params: { running: sessions.running } }),
        (error: unknown) => this.dispatch({ method: 'ui.sessions', params: { error: errorText(error) } }),
      )
      .finally(() => {
        this.refreshing = undefined;
      });
    return this.refreshing;
  }

  /**
   * Run the environment check (`ide.status`) and remember its answer. No password prompt is
   * shown (the check never needs one).
   *
   * @returns The answer, or null if the request failed (logged).
   */
  async refreshStatus(): Promise<IdeStatusResult | null> {
    try {
      await this.bridge.ensureStarted();
      this.status = await this.bridge.request('ide.status', {}, false);
    } catch (error) {
      this.ui.log(`[calu] status: ${errorText(error)}`);
      this.status = null;
    }
    this.onChanged();
    return this.status;
  }

  /**
   * Build the server command (`SessionsUi.serverHelp` with the last `ide.status`).
   *
   * @returns The command with its tool, detach hint and note.
   */
  serverHelp(): ServerHelp {
    return this.ui.serverHelp(this.status);
  }

  /**
   * Run the server command (`SessionsUi.startServer`) and put its help into the chat view.
   *
   * @returns The help whose command was run.
   */
  startServer(): ServerHelp {
    const help = this.serverHelp();
    this.ui.startServer(help);
    this.dispatch({ method: 'ui.serverHelp', params: { ...help } });
    this.ui.log(`[calu] server command: ${help.command}`);
    return help;
  }

  /**
   * Show how to start the server and put the same help into the chat view's state.
   *
   * @param reason First sentence of the message, e.g. the bridge's error text.
   * @returns A promise that resolves after the user's choice was handled.
   */
  async showServerHelp(reason: string): Promise<void> {
    const help = this.serverHelp();
    this.dispatch({ method: 'ui.serverHelp', params: { ...help } });
    await this.ui.showServerHelp(reason, help);
  }

  /**
   * Ask where to cut a stored conversation, then start a fork of it.
   *
   * @param claudeSessionId The conversation to fork.
   * @returns A promise that resolves when the fork started, failed or was cancelled.
   */
  private async fork(claudeSessionId: string): Promise<void> {
    const turn = await this.ui.input({
      title: 'calu: fork conversation',
      prompt: 'Fork before which turn? (a turn number; leave empty to keep the whole conversation)',
      validate: (value) => (value.trim() === '' || /^[1-9]\d*$/.test(value.trim()) ? undefined : 'Enter a turn number, or nothing.'),
    });
    if (turn === undefined) return;
    await this.start(
      { kind: 'fork', claude_session_id: claudeSessionId, ...(turn.trim() ? { fork_before_turn: Number(turn.trim()) } : {}) },
      'Forking the conversation',
    );
  }

  /**
   * Ask the server for a session (`ide.start`) and attach to it. While the request waits, the
   * chat view shows it (`ui.starting`: at once, and after `SLOW_START_MS` with the hint that no
   * server may be running) and the platform shows progress. On error -32013 (no server
   * answered) the server help is shown; a request the user took back (`cancelStart`) ends
   * without a message.
   *
   * @param params The params of `ide.start`.
   * @param title What is being done, e.g. "Starting a session" (shown while waiting).
   * @returns True if the session started and is attached.
   */
  private async start(params: IdeStartParams, title: string): Promise<boolean> {
    this.dispatch({ method: 'ui.starting', params: { title, slow: false } });
    const slow = setTimeout(() => this.dispatch({ method: 'ui.starting', params: { title, slow: true } }), SLOW_START_MS);
    try {
      await this.bridge.ensureStarted();
      const result = await this.ui.progress(title, () => this.bridge.request('ide.start', params));
      this.noServer = false;
      clearTimeout(slow);
      this.attached(result);
      return true;
    } catch (error) {
      clearTimeout(slow);
      this.dispatch({ method: 'ui.starting', params: {} });
      if (errorCode(error) === BridgeError.START_CANCELLED) {
        this.ui.log('[calu] the request for a session was cancelled.');
        return false;
      }
      this.ui.log(`[calu] start failed: ${errorText(error)}`);
      if (errorCode(error) === BridgeError.NO_SERVER) {
        this.noServer = true;
        this.onChanged();
        await this.showServerHelp(`${errorText(error)}`);
      } else {
        this.ui.error(`calu: the session was not started: ${errorText(error)}`);
      }
      return false;
    }
  }

  /**
   * Record a successful attach and report it.
   *
   * @param result The result of `ide.attach` / `ide.start`.
   * @param resumed True if the result holds only the events after those the chat view has
   *   (`reattach`), so the transcript is continued instead of rebuilt.
   * @returns Nothing.
   */
  private attached(result: IdeAttachResult, resumed = false): void {
    this.attachedId = result.session_id;
    this.lastAttachedId = result.session_id;
    this.noServer = false;
    this.onAttached(result, resumed);
    this.onChanged();
    void this.refresh();
  }
}
