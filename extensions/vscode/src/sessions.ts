/**
 * Choosing, starting, renaming and leaving sessions, and starting the calu server.
 *
 * The extension never starts a session as a child process. It asks the bridge:
 *
 *     pick()        QuickPick fed by ide.sessions
 *                     running session      ──► ide.attach {session}
 *                     stored conversation  ──► ide.start {kind: "resume" | "fork", claude_session_id}
 *                     New session…         ──► newSession()
 *     forkBefore()  user message of the chat ──► ide.start {kind: "resume", fork_before_turn}
 *     forkConversation()                     ──► ide.start {kind: "fork"}
 *     newSession()  nothing is asked ──► ide.start {kind: "new"}
 *     ide.start waits ──► `ui.starting {title, slow}`: the chat shows progress with "Cancel"
 *                     (cancelStart() ──► ide.start.cancel) and, after `SLOW_START_MS`, that no
 *                     server may be running, with "Start calu server"
 *     ide.start error -32013 (no server answered) ──► showServerHelp(): what to run, how to detach,
 *                     buttons "Open terminal with this command" / "Copy command"
 *     startServer() integrated terminal running the command of `shared/serverCommand.ts`
 *                   (tmux, else screen, else plain); the user types passwords there
 *
 * Every successful attach or start is reported with `onAttached(result)`; the caller turns it
 * into the `ui.attached` view event. The list of running sessions is reported with `ui.sessions`.
 */

import * as vscode from 'vscode';
import { bridgeWorkspaceFolder, errorCode, errorText, hostFacts, resolveCaluCommandForHost, type Bridge } from './bridge';
import { BridgeError, type IdeAttachResult, type IdeSessionsResult, type IdeStartParams, type IdeStatusResult } from './bridgeProtocol';
import { findExecutable } from './caluCommand';
import { findingOk } from './shared/hints';
import { buildServerHelp, type ServerHelp } from './shared/serverCommand';
import { buildSessionPicks, derivedSessionName, validateSessionName, type SessionChoice } from './shared/sessionPick';
import type { ViewEvent } from './shared/viewState';

/** A QuickPick row that carries what choosing it does. */
interface ChoiceItem extends vscode.QuickPickItem {
  choice?: SessionChoice;
}

/** Button on a stored conversation: fork it instead of resuming it. */
const FORK_BUTTON: vscode.QuickInputButton = {
  iconPath: new vscode.ThemeIcon('repo-forked'),
  tooltip: 'Fork: start a new session from a copy of this conversation',
};

/** Milliseconds a session request waits before the chat says that no server may be running. */
export const SLOW_START_MS = 3000;

/** Session commands of the extension (see the module comment). Create one per activation. */
export class Sessions {
  /** Id of the attached session, or null. */
  attachedId: string | null = null;
  private last: string | null = null;
  /**
   * Id of the session that was attached last and was not left on purpose: the one to attach
   * to again after the bridge was restarted or the window was reloaded. Cleared by `detach()`
   * and by a failed `reattach()`. Side effect of setting it: `remember` stores it (the
   * workspace state), so that a reloaded window attaches to the same session.
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
  /** The last answer of `ide.status`, or null if it was not asked yet or failed. */
  status: IdeStatusResult | null = null;
  /** True if the last `ide.start` failed because no server answered. */
  noServer = false;
  private refreshing: Promise<void> | undefined;

  /**
   * @param bridge The bridge (started on demand).
   * @param dispatch Applies a view event (`ui.sessions`, `ui.serverHelp`, `ui.detached`,
   *   `ui.starting`, `ui.notice`) to the chat view.
   * @param sessionName Returns the display name of a running session by its id (its name, else
   *   its directory's last segment), or `undefined` if the session is not known.
   * @param onAttached Called with the result of every successful `ide.attach` / `ide.start`.
   * @param onChanged Called when `status`, `noServer` or `attachedId` changed (the caller
   *   recomputes the setup hints).
   * @param output The "calu" output channel.
   * @param remember Stores the id of the last attached session (or null) where the next
   *   activation finds it; called whenever `lastAttachedId` changes.
   */
  constructor(
    private readonly bridge: Bridge,
    private readonly dispatch: (event: ViewEvent) => void,
    private readonly onAttached: (result: IdeAttachResult, resumed: boolean) => void,
    private readonly onChanged: () => void,
    private readonly output: vscode.OutputChannel,
    private readonly sessionName: (sessionId: string) => string | undefined = () => undefined,
    private readonly remember: (sessionId: string | null) => void = () => undefined,
  ) {}

  /**
   * Show the session QuickPick and do what the user chooses (see the module comment).
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
      void vscode.window.showErrorMessage(`calu: the sessions could not be listed: ${errorText(error)}`);
      return;
    }
    this.dispatch({ method: 'ui.sessions', params: { running: sessions.running } });
    const chosen = await this.showPick(sessions);
    if (!chosen) return;
    if (chosen.choice.kind === 'new') await this.newSession();
    else if (chosen.choice.kind === 'attach') await this.attach(chosen.choice.sessionId);
    else if (chosen.fork) await this.fork(chosen.choice.claudeSessionId);
    else await this.start({ kind: 'resume', claude_session_id: chosen.choice.claudeSessionId }, 'Resuming the conversation');
  }

  /**
   * Start a new session in the workspace at once, without asking anything: the first message is
   * typed into the chat's input like every other one, and a name is given with "Rename session"
   * (the owner, 2026-11-18: "everyone should know how to find the chat input box").
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
      this.output.appendLine(`[calu] the session request was not taken back: ${errorText(error)}`);
    }
  }

  /**
   * After the bridge started again: attach to the session that was attached before, asking
   * only for the events this view has not seen. Nothing is asked of the user and no error is
   * shown: the caller falls back to the session of the workspace.
   *
   * @param afterSeq Sequence number of the last event the chat view applied for that session.
   * @returns True if that session is attached again; false if there was none or it is gone.
   */
  async reattach(afterSeq: number): Promise<boolean> {
    const session = this.lastAttachedId;
    if (!session) return false;
    try {
      await this.bridge.ensureStarted();
      let result = await this.bridge.request('ide.attach', { session, after_seq: afterSeq }, false);
      // A replay with a gap cannot continue the transcript: take the whole session again.
      const resumed = afterSeq > 0 && result.complete === true;
      if (afterSeq > 0 && !resumed) result = await this.bridge.request('ide.attach', { session, after_seq: 0 }, false);
      this.attached(result, resumed);
      return true;
    } catch (error) {
      this.output.appendLine(`[calu] the session ${session.slice(0, 8)} was not attached again: ${errorText(error)}`);
      this.lastAttachedId = null;
      return false;
    }
  }

  /**
   * Attach to a running session.
   *
   * @param session Id or id prefix of the session; null = the session of the workspace.
   * @param quiet True for the attempt at activation: no password prompt, and no error message
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
      if (!(quiet && expected.includes(code))) {
        void vscode.window.showErrorMessage(`calu: not attached: ${errorText(error)}`);
      }
      this.output.appendLine(`[calu] attach failed: ${errorText(error)}`);
      return false;
    }
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
      this.output.appendLine(`[calu] detach: ${errorText(error)}`);
    }
    this.lastAttachedId = null;
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
      void vscode.window.showInformationMessage('calu: attach to a session first.');
      return;
    }
    const name = await vscode.window.showInputBox({
      title: 'calu: rename session',
      prompt: 'Name of the session (at most 80 characters; empty removes the name)',
      value: currentName,
      ignoreFocusOut: true,
      validateInput: validateSessionName,
    });
    if (name === undefined) return;
    try {
      await this.bridge.request('ide.session.rename', { session_id: sessionId, name: name.trim() });
    } catch (error) {
      void vscode.window.showErrorMessage(`calu: the session was not renamed: ${errorText(error)}`);
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
      this.output.appendLine(`[calu] status: ${errorText(error)}`);
      this.status = null;
    }
    this.onChanged();
    return this.status;
  }

  /**
   * Build the server command from the `calu.*` settings and what is installed. tmux and screen
   * are taken from the last `ide.status`; without one, from a lookup in PATH.
   *
   * @returns The command with its tool, detach hint and note.
   */
  serverHelp(): ServerHelp {
    const config = vscode.workspace.getConfiguration('calu');
    const resolved = resolveCaluCommandForHost();
    const facts = hostFacts();
    const installed = (name: string): boolean =>
      this.status ? findingOk(this.status, name) : findExecutable(name, facts, []) !== undefined;
    return buildServerHelp({
      calu: resolved.ok ? resolved.command : ['calu'],
      sudo: config.get<boolean>('server.sudo', false) === true,
      insecure: config.get<boolean>('server.insecure', false) === true,
      extraArgs: (config.get<unknown[]>('server.extraArgs') ?? []).filter((a): a is string => typeof a === 'string'),
      tmux: installed('tmux'),
      screen: installed('screen'),
    });
  }

  /**
   * Open an integrated terminal in the workspace folder and run the server command in it. The
   * user types the sudo and credential passwords there; the extension sees none of it.
   *
   * @returns The help whose command was sent to the terminal.
   */
  startServer(): ServerHelp {
    const help = this.serverHelp();
    const terminal = vscode.window.createTerminal({ name: 'calu server', cwd: bridgeWorkspaceFolder()?.uri });
    terminal.show();
    terminal.sendText(help.command, true);
    this.dispatch({ method: 'ui.serverHelp', params: { ...help } });
    this.output.appendLine(`[calu] server terminal: ${help.command}`);
    return help;
  }

  /**
   * Show how to start the server (what to run, how to detach, that it keeps running after
   * VSCode closes) with the buttons "Open terminal with this command" and "Copy command", and
   * put the same help into the chat view's state.
   *
   * @param reason First sentence of the message, e.g. the bridge's error text.
   * @returns A promise that resolves after the user's choice was handled.
   */
  async showServerHelp(reason: string): Promise<void> {
    const help = this.serverHelp();
    this.dispatch({ method: 'ui.serverHelp', params: { ...help } });
    const detach = help.detach ? ` Detach with ${help.detach}.` : '';
    const choice = await vscode.window.showWarningMessage(
      `calu: ${reason}`,
      { modal: true, detail: `Run in a terminal:\n\n${help.command}\n\n${help.note}${detach}` },
      'Open terminal with this command',
      'Copy command',
    );
    if (choice === 'Open terminal with this command') this.startServer();
    else if (choice === 'Copy command') await vscode.env.clipboard.writeText(help.command);
  }

  /**
   * Show the QuickPick of `buildSessionPicks`.
   *
   * @param sessions The result of `ide.sessions`.
   * @returns The chosen entry and whether its fork button was used; `undefined` on cancel.
   */
  private showPick(sessions: IdeSessionsResult): Promise<{ choice: SessionChoice; fork: boolean } | undefined> {
    const items: ChoiceItem[] = buildSessionPicks(sessions, this.attachedId).map((p) =>
      p.separator
        ? { label: p.label, kind: vscode.QuickPickItemKind.Separator }
        : {
            label: p.label,
            description: p.description,
            detail: p.detail,
            choice: p.choice,
            buttons: p.choice.kind === 'stored' ? [FORK_BUTTON] : [],
          },
    );
    return new Promise((resolve) => {
      const pick = vscode.window.createQuickPick<ChoiceItem>();
      pick.title = 'calu: sessions';
      pick.placeholder = 'Attach to a running session, resume or fork a stored conversation, or start a new one';
      pick.matchOnDescription = true;
      pick.matchOnDetail = true;
      pick.items = items;
      let result: { choice: SessionChoice; fork: boolean } | undefined;
      pick.onDidAccept(() => {
        const choice = pick.selectedItems[0]?.choice;
        if (choice) result = { choice, fork: false };
        pick.hide();
      });
      pick.onDidTriggerItemButton((event) => {
        if (event.item.choice) result = { choice: event.item.choice, fork: true };
        pick.hide();
      });
      pick.onDidHide(() => {
        pick.dispose();
        resolve(result);
      });
      pick.show();
    });
  }

  /**
   * Ask where to cut a stored conversation, then start a fork of it.
   *
   * @param claudeSessionId The conversation to fork.
   * @returns A promise that resolves when the fork started, failed or was cancelled.
   */
  private async fork(claudeSessionId: string): Promise<void> {
    const turn = await vscode.window.showInputBox({
      title: 'calu: fork conversation',
      prompt: 'Fork before which turn? (a turn number; leave empty to keep the whole conversation)',
      ignoreFocusOut: true,
      validateInput: (value) => (value.trim() === '' || /^[1-9]\d*$/.test(value.trim()) ? undefined : 'Enter a turn number, or nothing.'),
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
   * server may be running) and the status bar shows a progress entry. On error -32013 (no
   * server answered) the server help is shown; a request the user took back (`cancelStart`)
   * ends without a message.
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
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `calu: ${title}…` },
        () => this.bridge.request('ide.start', params),
      );
      this.noServer = false;
      clearTimeout(slow);
      this.attached(result);
      return true;
    } catch (error) {
      clearTimeout(slow);
      this.dispatch({ method: 'ui.starting', params: {} });
      if (errorCode(error) === BridgeError.START_CANCELLED) {
        this.output.appendLine('[calu] the request for a session was cancelled.');
        return false;
      }
      this.output.appendLine(`[calu] start failed: ${errorText(error)}`);
      if (errorCode(error) === BridgeError.NO_SERVER) {
        this.noServer = true;
        this.onChanged();
        await this.showServerHelp(`${errorText(error)}`);
      } else {
        void vscode.window.showErrorMessage(`calu: the session was not started: ${errorText(error)}`);
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
