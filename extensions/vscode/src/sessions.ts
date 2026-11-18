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
 *     newSession()  input boxes (first message, name; both optional) ──► ide.start {kind: "new"}
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
import { buildSessionPicks, validateSessionName, type SessionChoice } from './shared/sessionPick';
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

/** Session commands of the extension (see the module comment). Create one per activation. */
export class Sessions {
  /** Id of the attached session, or null. */
  attachedId: string | null = null;
  /** The last answer of `ide.status`, or null if it was not asked yet or failed. */
  status: IdeStatusResult | null = null;
  /** True if the last `ide.start` failed because no server answered. */
  noServer = false;
  private refreshing: Promise<void> | undefined;

  /**
   * @param bridge The bridge (started on demand).
   * @param dispatch Applies a view event (`ui.sessions`, `ui.serverHelp`, `ui.detached`,
   *   `ui.notice`) to the chat view.
   * @param onAttached Called with the result of every successful `ide.attach` / `ide.start`.
   * @param onChanged Called when `status`, `noServer` or `attachedId` changed (the caller
   *   recomputes the setup hints).
   * @param output The "calu" output channel.
   */
  constructor(
    private readonly bridge: Bridge,
    private readonly dispatch: (event: ViewEvent) => void,
    private readonly onAttached: (result: IdeAttachResult) => void,
    private readonly onChanged: () => void,
    private readonly output: vscode.OutputChannel,
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
   * Ask for an optional first message and name, then start a new session in the workspace.
   *
   * @returns True if the session started and is attached; false if the user cancelled or the
   *   start failed (the failure was shown).
   */
  async newSession(): Promise<boolean> {
    const prompt = await vscode.window.showInputBox({
      title: 'calu: new session (1/2)',
      prompt: 'First message for Claude (optional; leave empty to start without one)',
      ignoreFocusOut: true,
    });
    if (prompt === undefined) return false;
    const name = await vscode.window.showInputBox({
      title: 'calu: new session (2/2)',
      prompt: 'Name of the session (optional, at most 80 characters)',
      ignoreFocusOut: true,
      validateInput: validateSessionName,
    });
    if (name === undefined) return false;
    return this.start(
      { kind: 'new', ...(prompt.trim() ? { prompt: prompt.trim() } : {}), ...(name.trim() ? { name: name.trim() } : {}) },
      'Starting a session',
    );
  }

  /**
   * Start a new session that continues a conversation from before one of its turns (the chat's
   * "Rewind to before this message" and "Fork from here"). The session it was cut from keeps
   * running; files are not reverted.
   *
   * @param claudeSessionId Claude Code's id of the conversation.
   * @param turn Number of the first turn the new session does not keep (at least 2).
   * @param title Text of the progress notification.
   * @returns True if the session started and is attached; failures are shown to the user.
   */
  forkBefore(claudeSessionId: string, turn: number, title: string): Promise<boolean> {
    return this.start({ kind: 'resume', claude_session_id: claudeSessionId, fork_before_turn: turn }, title);
  }

  /**
   * Start a new session from a copy of a whole conversation; the original stays as it is.
   *
   * @param claudeSessionId Claude Code's id of the conversation.
   * @returns True if the session started and is attached; failures are shown to the user.
   */
  forkConversation(claudeSessionId: string): Promise<boolean> {
    return this.start({ kind: 'fork', claude_session_id: claudeSessionId }, 'Forking the conversation');
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
   * Ask the server for a session (`ide.start`) and attach to it, with a progress notification.
   * On error -32013 (no server answered) the server help is shown.
   *
   * @param params The params of `ide.start`.
   * @param title Text of the progress notification.
   * @returns True if the session started and is attached.
   */
  private async start(params: IdeStartParams, title: string): Promise<boolean> {
    try {
      await this.bridge.ensureStarted();
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `calu: ${title}…` },
        () => this.bridge.request('ide.start', params),
      );
      this.noServer = false;
      this.attached(result);
      return true;
    } catch (error) {
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
   * @returns Nothing.
   */
  private attached(result: IdeAttachResult): void {
    this.attachedId = result.session_id;
    this.noServer = false;
    this.onAttached(result);
    this.onChanged();
    void this.refresh();
  }
}
