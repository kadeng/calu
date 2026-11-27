/**
 * The chat's host logic, shared by the VSCode extension (`src/extension.ts`) and the browser
 * pane of `calu web` (`pane/`): what a message of the chat view does, what an event of the
 * bridge does to the view, and the flows behind both (attach, send, decide, fork, honesty,
 * usage, a lost link, a stopped server).
 *
 *     chat view ──WebviewMessage──► ChatHost.handle ──► bridge (session.call, ide.*)
 *                                         │                    │
 *                                         └─ HostUi            └─ notifications ──► ChatHost
 *                                            (dialogs, files,      session.event ──► view
 *                                             links, settings)     ide.link      ──► view, lost link
 *
 *     platform     ChatView                      HostUi
 *     VSCode       ChatViewProvider (webview)    showInputBox, editors, diff views, settings panel
 *     web pane     the page itself               dialogs in the page, links into `calu web`
 *
 * Messages that only make sense on one platform (open a file in an editor, show the log, open
 * the settings) go to `HostUi.handle`. No `vscode` and no Node.js import.
 */

import type { IdeAttachResult } from '../bridgeProtocol';
import type { ChatSendParams } from '../protocol.generated';
import { buildHints } from '../shared/hints';
import { flagsForLevel } from '../shared/levels';
import type { HostMessage, WebviewMessage } from '../shared/messages';
import { lostCause, serverCheck, serverStoppedAfterLoss } from '../shared/serverLoss';
import { rulesNotice, trustArgs } from '../shared/slashCommands';
import { isAttached, own, sessionTitle, sideHistory, type ViewEvent, type ViewState } from '../shared/viewState';
import { errorText, type BridgeCore } from './bridgeCore';
import type { Disposable } from './emitter';
import { Sessions, type InputOptions, type SessionsUi } from './sessions';

/** Milliseconds between two refreshes of the session list while the chat view is visible. */
export const SESSIONS_REFRESH_MS = 10_000;

/** Most steps asked from `session.history` for the recorded turns above the transcript. */
const HISTORY_STEPS = 500;

/** Least milliseconds between two `usage.get` requests that events of a running turn cause. */
const USAGE_REFRESH_MS = 2000;

/** Milliseconds a session gets to report a honesty mode that was just stored for it. */
const MODE_ANNOUNCE_MS = 4000;

/** Longest reason of a mark made by hand (the limit of `honesty.mark`). */
const MAX_MARK_REASON = 500;

/** The chat view as the host sees it. */
export interface ChatView {
  /** The authoritative view state. */
  readonly state: ViewState;
  /** True while the user can see the view. */
  readonly isVisible: boolean;
  /**
   * Apply a view event to the state and the view.
   *
   * @param event The event.
   */
  dispatch(event: ViewEvent): void;
  /**
   * Send a message to the view's script (e.g. `restoreInput`).
   *
   * @param message The message.
   */
  send(message: HostMessage): void;
  /** Bring the view into sight. */
  reveal(): unknown;
}

/** A message with buttons; the answer is the chosen button. */
export type Notify = (level: 'info' | 'warning' | 'error', text: string, ...buttons: string[]) => Promise<string | undefined>;

/** What the host logic needs from the platform besides the view. */
export interface HostUi {
  /** Append a line to the log. */
  log(line: string): void;
  /**
   * Ask a yes/no question in a modal dialog.
   *
   * @param question The question.
   * @param detail What answering yes does.
   * @param button Label of the yes button.
   * @returns True if the user chose the button.
   */
  confirm(question: string, detail: string, button: string): Promise<boolean>;
  /** Ask for a text (see `InputOptions`); `undefined` if cancelled. */
  input(options: InputOptions): Promise<string | undefined>;
  /** Show a message, optionally with buttons (not modal). */
  notify: Notify;
  /**
   * Show a short-lived status text (VSCode: the status bar).
   *
   * @param text The text.
   */
  flash(text: string): void;
  /** True if the platform's window has the focus. */
  focused(): boolean;
  /** The user's choice of notifications: "all", "decisions" or "off". */
  notificationMode(): 'all' | 'decisions' | 'off';
  /**
   * The file the user looks at, relative to the workspace, to suggest for "Trust a file".
   *
   * @returns The path, or "" if there is none.
   */
  activeFile(): string;
  /**
   * Called after the host stored a setting (honesty mode, effort): an open settings view reloads.
   */
  settingsChanged(): void;
  /**
   * Offer to install calu after the bridge reported that it is missing (once per start).
   */
  offerInstall(): void;
  /**
   * Called with every event of the attached session before it is applied (VSCode keeps the
   * edit diffs and refreshes the context document).
   *
   * @param method The event's method.
   * @param params The event's params.
   */
  sessionEvent(method: string, params: Record<string, unknown>): void;
  /**
   * Called after a session was attached, with the attach result.
   *
   * @param result The result of `ide.attach` / `ide.start`.
   */
  attached(result: IdeAttachResult): void;
  /**
   * Handle a message that is not the host logic's (open a file, a diff or a link, the log, the
   * settings, the checker instructions, the context documents, copying the server command,
   * installing calu).
   *
   * @param message The message.
   * @returns Resolves when it was handled; rejections are shown as a notice.
   */
  handle(message: WebviewMessage): Promise<void>;
}

/** The chat's host logic. Create one per window or browser tab. */
export class ChatHost implements Disposable {
  /** The session commands. */
  readonly sessions: Sessions;
  private nextSideId = 1;
  /** Time (ms) of the last `usage.get` request. */
  private lastUsageAt = 0;
  private lastHints = '';
  private lastServerHelp = '';
  /** True once the install offer for a missing calu was made. */
  private installOffered = false;
  /** True once the password prompt was opened by itself for the running bridge. */
  private askedAtStart = false;
  /**
   * True from a lost link whose server stopped (`serverStoppedAfterLoss`) until a server is
   * found again or a session is attached: the chat shows "The calu server stopped".
   */
  private serverStopped = false;
  /** Local time of the last Retry that found no server; null before the first. */
  private serverRetriedAt: string | null = null;
  /** True while a look for the stopped server runs, so looks do not overlap. */
  private lookingForServer = false;
  private readonly subscriptions: Disposable[] = [];
  private timer: ReturnType<typeof setInterval> | undefined;

  /**
   * @param bridge The bridge client.
   * @param chat The chat view.
   * @param ui The platform's dialogs and actions.
   * @param sessionsUi The platform's session picker, server help and progress.
   * @param remember Stores the id of the last attached session (or null) for the next start.
   * @param remembered The id stored by `remember` at the last run, if any.
   */
  constructor(
    readonly bridge: BridgeCore,
    readonly chat: ChatView,
    private readonly ui: HostUi,
    sessionsUi: SessionsUi,
    remember: (sessionId: string | null) => void = () => undefined,
    remembered?: string,
  ) {
    this.sessions = new Sessions(
      bridge,
      (event) => this.dispatch(event),
      (result, resumed) => this.onAttached(result, resumed),
      () => this.updateHints(),
      sessionsUi,
      (id) => {
        const entry = chat.state.sessions.list.find((s) => s.id === id);
        if (entry) return sessionTitle(entry);
        return id === chat.state.session.sessionId ? sessionTitle({ id, name: '', directory: chat.state.session.workspace ?? '' }) : undefined;
      },
      remember,
    );
    if (remembered) this.sessions.lastAttachedId = remembered;
  }

  /**
   * Subscribe to the bridge and start the refresh timer. Call once.
   *
   * Side effects: on "running" the setup is checked and the workspace's session attached; on
   * "missing" calu's installation is offered once; notifications go to the view.
   *
   * @returns Nothing.
   */
  start(): void {
    this.subscriptions.push(
      this.bridge.onStatus(({ status, message, version }) => {
        this.dispatch({
          method: 'bridge.status',
          params: { status, ...(message ? { message } : {}), ...(version ? { version } : {}) },
        });
        if (status !== 'running') this.sessions.attachedId = null;
        this.updateHints();
        if (status === 'missing' && !this.installOffered) {
          this.installOffered = true;
          this.ui.offerInstall();
        }
        if (status === 'running') {
          // Fresh bridge: check the setup, attach to the session of the workspace if one runs.
          this.askedAtStart = false;
          void this.connectWorkspace(true);
        }
      }),
      this.bridge.onLockedChange(() => this.updateHints()),
      this.bridge.onNotification((notification) => {
        if (notification.method === 'session.event') {
          const method = notification.params.method;
          const params = notification.params.params;
          if (typeof method !== 'string' || !params || typeof params !== 'object') return;
          this.onSessionEvent(method, params as Record<string, unknown>);
        } else if (notification.method === 'ide.link') {
          this.dispatch({ method: 'ui.link', params: notification.params });
          if (notification.params.state === 'lost') void this.onLinkLost(String(notification.params.message ?? ''), notification.params.cause);
        } else if (notification.method === 'ide.log') {
          this.ui.log(`[calu ide] ${String(notification.params.level ?? 'info')}: ${String(notification.params.message ?? '')}`);
        }
      }),
    );
    let ticks = 0;
    this.timer = setInterval(() => {
      ticks += 1;
      if (this.chat.isVisible) void this.sessions.refresh();
      // A stopped server is looked for at every tick while the chat is visible, else at every
      // third: `ide.status` starts a few programs (claude --version, a bwrap test).
      if (this.serverStopped && (this.chat.isVisible || ticks % 3 === 0)) void this.lookForServer(false);
    }, SESSIONS_REFRESH_MS);
    this.updateHints();
  }

  /**
   * Stop the timer and the subscriptions.
   *
   * @returns Nothing.
   */
  dispose(): void {
    if (this.timer !== undefined) clearInterval(this.timer);
    for (const s of this.subscriptions.splice(0)) s.dispose();
  }

  /**
   * Apply a view event to the chat view.
   *
   * @param event The event.
   * @returns Nothing.
   */
  dispatch(event: ViewEvent): void {
    this.chat.dispatch(event);
    // A session request that waits is shown in the chat: make sure the chat can be seen.
    if (event.method === 'ui.starting' && typeof event.params.title === 'string' && event.params.slow !== true) void this.chat.reveal();
  }

  /**
   * Show a message in the chat's main list.
   *
   * @param level "info", "warning" or "error".
   * @param text The message.
   * @returns Nothing.
   */
  notice(level: string, text: string): void {
    this.dispatch({ method: 'ui.notice', params: { level, text } });
  }

  /**
   * Run an action and report a failure in the chat and the log.
   *
   * @param what What was attempted, e.g. "interrupt"; start of the error message.
   * @param action The action.
   * @returns True if the action finished without error.
   */
  async run(what: string, action: () => Promise<unknown>): Promise<boolean> {
    try {
      await action();
      return true;
    } catch (error) {
      this.ui.log(`[calu] ${what} failed: ${errorText(error)}`);
      this.notice('error', `${what} failed: ${errorText(error)}`);
      return false;
    }
  }

  // ---------------------------------------------------------------- messages of the view

  /**
   * Do what a validated message of the chat view asks (`WEBVIEW_CONTRACT.md` lists every
   * message). Failures are shown in the chat; nothing is thrown.
   *
   * @param message The message.
   * @returns A promise that resolves when the message was handled.
   */
  async handle(message: WebviewMessage): Promise<void> {
    const state = this.chat.state;
    switch (message.type) {
      case 'ready':
      case 'stateReport':
        return; // handled by the view
      case 'send': {
        const params: ChatSendParams = {
          text: message.text,
          delivery: message.delivery,
          to: message.to,
          ...(message.images.length > 0
            ? { images: message.images.map((i) => ({ data: i.data, media_type: i.mediaType })) as ChatSendParams['images'] }
            : {}),
        };
        if (!(await this.run('Sending the message', () => this.bridge.session('chat.send', params)))) {
          this.chat.send({ type: 'restoreInput', text: message.text });
        }
        return;
      }
      case 'sendQueuedNow':
        await this.run('Sending the message now', () => this.bridge.session('chat.send_now', { id: message.id }));
        return;
      case 'retractQueued': {
        let text = '';
        const ok = await this.run('Retracting the message', async () => {
          text = String((await this.bridge.session('chat.retract', { id: message.id })).text ?? '');
        });
        if (ok && text) this.chat.send({ type: 'retracted', text });
        return;
      }
      case 'decide':
        if (message.rule) {
          // Added first, with the user login; a failure is said and the decision still goes.
          const { text, duration, scope } = message.rule;
          await this.run('Adding the temporary rule (the decision is sent anyway)', async () => {
            const { rule } = await this.bridge.request('ide.rules.add', { text, duration, scope });
            this.notice('info', `Temporary rule added: ${rule.line}`);
          });
        }
        await this.decide(message.escalationId, message.decision, message.note, message.answer);
        return;
      case 'ruleAdd':
        await this.run('Adding the temporary rule', async () => {
          const { rule } = await this.bridge.request('ide.rules.add', { command: message.command });
          this.notice('info', `Temporary rule added: ${rule.line}`);
        });
        return;
      case 'rulesList':
        await this.run('Listing the temporary rules', async () => {
          const { rules } = await this.bridge.request('ide.rules.list', {});
          this.notice('info', rulesNotice(rules));
        });
        return;
      case 'ruleRemove':
        await this.run('Removing the temporary rule', async () => {
          const { rule } = await this.bridge.request('ide.rules.remove', { id: message.id });
          this.notice('info', `Temporary rule removed: ${rule.line}`);
        });
        return;
      case 'sideAsk': {
        const id = this.nextSideId++;
        const history = sideHistory(state);
        this.dispatch({ method: 'ui.sideAsk', params: { id, question: message.question } });
        try {
          const result = await this.bridge.session('side.ask', {
            question: message.question,
            history: history as Parameters<typeof this.bridge.session<'side.ask'>>[1]['history'],
          });
          this.dispatch({ method: 'ui.sideAnswer', params: { id, answer: String(result.answer ?? '') } });
        } catch (error) {
          this.dispatch({ method: 'ui.sideAnswer', params: { id, error: errorText(error) } });
        }
        return;
      }
      case 'attach':
        await this.sessions.pick();
        return;
      case 'newSession':
        await this.sessions.newSession();
        return;
      case 'switchSession':
        if (state.sessions.list.some((s) => s.id === message.sessionId)) await this.sessions.attach(message.sessionId);
        return;
      case 'renameSession': {
        const entry = state.sessions.list.find((s) => s.id === message.sessionId);
        if (entry) await this.sessions.rename(entry.id, entry.name);
        return;
      }
      case 'refreshSessions':
        await this.bridge.ensureStarted().catch(() => {});
        await this.sessions.refresh(true);
        return;
      case 'detach':
        await this.sessions.detach();
        return;
      case 'startServer':
        this.sessions.startServer();
        return;
      case 'retryServer':
        await this.lookForServer(true);
        return;
      case 'restartBridge':
        await this.bridge.restart().catch(() => {});
        return;
      case 'setModel': {
        const model = message.model === 'default' ? null : message.model;
        if (await this.run('Changing the model', () => this.bridge.session('model.set', { model }))) {
          this.dispatch({ method: 'ui.model', params: { model } });
        }
        return;
      }
      case 'setEffort':
        // The effort of a running session is fixed; the setting applies to later sessions.
        await this.run('Changing the effort', async () => {
          if (message.effort === 'default') await this.bridge.request('ide.settings.unset', { key: 'claude.effort', project: null });
          else await this.bridge.request('ide.settings.set', { key: 'claude.effort', value: message.effort, project: null });
          this.notice('info', `Effort "${message.effort}" is stored; it applies to sessions started from now on.`);
          this.ui.settingsChanged();
        });
        return;
      case 'setLevel':
        await this.run('Changing the supervision level', () => this.bridge.session('auto_escalate.set', flagsForLevel(message.level)));
        return;
      case 'setPermissionMode':
        // The session answers every client with `permission_mode.changed`; the header follows it.
        await this.run("Switching Claude Code's permission mode", () => this.bridge.session('permission_mode.set', { mode: message.mode }));
        return;
      case 'refreshUsage':
        await this.refreshUsage();
        return;
      case 'usagePanel':
        await this.openUsagePanel();
        return;
      case 'interrupt':
        await this.run('Interrupting', () => this.bridge.session('interrupt', {}));
        return;
      case 'setPause':
        // The session answers every client with `pause.changed`; the button follows it.
        await this.run(message.paused ? 'Pausing' : 'Resuming', () => this.bridge.session('pause.set', { paused: message.paused }));
        return;
      case 'unlock':
        await this.unlock();
        return;
      case 'rewindBefore':
      case 'forkBefore':
        await this.forkBefore(message.turn, message.uuid, message.type === 'rewindBefore');
        return;
      case 'forkConversation':
        await this.forkConversation();
        return;
      case 'cancelStart':
        await this.sessions.cancelStart();
        return;
      case 'setHonestyMode':
        await this.run('Changing the honesty mode', () => this.setHonestyMode(message.mode));
        return;
      case 'honestyReview':
        await this.run('The honesty review', async () => {
          const result = await this.bridge.session('honesty.review', {});
          // Say what happened: the chip alone changes quietly, or not at all.
          if (!result.ran) this.notice('warning', `No honesty review ran${result.problem ? `: ${result.problem}` : '.'}`);
          else this.notice('info', `The honesty review ran${result.problem ? `; ${result.problem}` : ' and judged every file'}. The honesty chip shows the state now.`);
        });
        return;
      case 'honestyTrust':
        await this.trustHonesty(message);
        return;
      case 'honestyClear':
        await this.run('Clearing the mark', () => this.bridge.session('honesty.clear', { mark_id: message.markId }));
        return;
      case 'honestyGraphGet':
        await this.sendHonestyGraph();
        return;
      case 'honestyMark':
        await this.markDishonest(message.path, message.symbol);
        return;
      case 'honestyDismiss':
        await this.run('Dropping the code to judge again', () =>
          this.bridge.session('honesty.dismiss', { recheck_ids: message.recheckIds as [number, ...number[]] }),
        );
        return;
      default:
        await this.run('The action', () => this.ui.handle(message));
    }
  }

  // ---------------------------------------------------------------- attaching

  /**
   * Take over the result of `ide.attach` / `ide.start`: rebuild the chat view (or continue it,
   * with `resumed`), ask for the usage, and load what the replayed events do not hold: the
   * recorded earlier turns of the conversation and the stored honesty mode.
   *
   * @param result The result.
   * @param resumed True if `result.events` are only those after the ones the chat view has.
   * @returns Nothing.
   */
  private onAttached(result: IdeAttachResult, resumed: boolean): void {
    this.serverStopped = false;
    this.serverRetriedAt = null;
    this.ui.attached(result);
    this.dispatch({ method: 'ui.attached', params: { ...(result as unknown as Record<string, unknown>), resumed } });
    void this.refreshUsage();
    void this.chat.reveal();
    if (!resumed) {
      void this.loadHistory(result.session_id);
      void this.loadStoredHonesty(result.session_id);
    }
  }

  /**
   * Ask the attached session for its recorded conversation and put the turns that the replayed
   * events do not cover above the transcript (`ui.history`). Failures are logged only.
   *
   * @param sessionId The session the attach was for; the answer is dropped if another one is
   *   attached when it arrives.
   * @returns A promise that resolves when the answer was applied (never rejects).
   */
  private async loadHistory(sessionId: string): Promise<void> {
    try {
      const history = await this.bridge.session('session.history', { limit: HISTORY_STEPS });
      if (this.sessions.attachedId !== sessionId) return;
      this.dispatch({ method: 'ui.history', params: { session_id: sessionId, history: history as unknown as Record<string, unknown> } });
    } catch (error) {
      this.ui.log(`[calu] the recorded conversation was not loaded: ${errorText(error)}`);
    }
  }

  /**
   * After the bridge started or was unlocked: check the setup, attach again to the session
   * that was attached before the bridge went away (asking only for the events the chat has not
   * seen), else to the session of the workspace if one runs, and load the session list. If the
   * bridge reports that it is locked and the chat is visible, the calu user password is asked
   * for (once per bridge).
   *
   * @param mayAsk False to skip the password prompt (after the user dismissed it).
   * @returns A promise that resolves when all of it was tried (never rejects).
   */
  async connectWorkspace(mayAsk: boolean): Promise<void> {
    const status = await this.sessions.refreshStatus();
    this.bridge.noteStatusLocked(status?.locked === true);
    if (status?.locked && mayAsk && this.chat.isVisible && !this.askedAtStart) {
      this.askedAtStart = true;
      if (!(await this.bridge.unlock().catch(() => false))) return;
      await this.sessions.refreshStatus();
    }
    const before = this.sessions.lastAttachedId;
    const afterSeq = before !== null && this.chat.state.session.sessionId === before ? this.chat.state.lastSeq : 0;
    if (!(await this.sessions.reattach(afterSeq))) await this.sessions.attach(null, true);
    await this.sessions.refresh();
  }

  /**
   * Ask the calu user password now (the chat's "Unlock" button), then connect.
   *
   * @returns A promise that resolves when the prompt was answered and the result applied.
   */
  async unlock(): Promise<void> {
    try {
      if (await this.bridge.unlock()) await this.connectWorkspace(false);
    } catch (error) {
      void this.ui.notify('error', `calu: ${errorText(error)}`);
    }
  }

  // ---------------------------------------------------------------- honesty

  /**
   * Find the scope that decides `honesty.mode` for the attached session and the value stored
   * there, as calu's terminal client does: the project's own setting wins over the global one.
   *
   * @param interactive True if the user asked for a change (a needed password is then asked).
   * @returns `project`: the session's project if its settings name `honesty.mode`, else `null`
   *   (the global scope); `mode`: the stored value, or `null` if it is unknown.
   * @throws (rejects) Error if the settings cannot be read.
   */
  private async storedHonesty(interactive: boolean): Promise<{ project: string | null; mode: string | null }> {
    const mode = (rows: readonly { key: string; value: unknown }[]): string | null => {
      const value = rows.find((r) => r.key === 'honesty.mode')?.value;
      return typeof value === 'string' ? value : null;
    };
    const project = this.chat.state.session.project;
    if (project) {
      const own = await this.bridge.request('ide.settings.get', { project }, interactive);
      if (own.rows.find((r) => r.key === 'honesty.mode')?.is_set) return { project, mode: mode(own.rows) };
    }
    return { project: null, mode: mode((await this.bridge.request('ide.settings.get', { project: null }, interactive)).rows) };
  }

  /**
   * Read the stored honesty mode for the header chip (`ui.honestyStored`). No password prompt;
   * failures are logged only.
   *
   * @param sessionId The session the attach was for; the answer is dropped if another one is
   *   attached when it arrives.
   * @returns A promise that resolves when the answer was applied (never rejects).
   */
  private async loadStoredHonesty(sessionId: string): Promise<void> {
    try {
      const stored = await this.storedHonesty(false);
      if (this.sessions.attachedId === sessionId) this.dispatch({ method: 'ui.honestyStored', params: { mode: stored.mode } });
    } catch (error) {
      this.ui.log(`[calu] the stored honesty mode was not read: ${errorText(error)}`);
    }
  }

  /**
   * Store `honesty.mode` where the attached session reads it from, and say what happened: a
   * running session reads the change by itself and reports it (`honesty.update`); if it has
   * not done so after `MODE_ANNOUNCE_MS`, the chat says that the session still uses its mode.
   *
   * @param mode "off", "track" or "enforce".
   * @returns A promise that resolves when the mode was stored and the notice was shown.
   * @throws (rejects) Error if the setting cannot be stored.
   */
  private async setHonestyMode(mode: string): Promise<void> {
    const attached = isAttached(this.chat.state);
    const scope = attached ? (await this.storedHonesty(true)).project : null;
    await this.bridge.request('ide.settings.set', { key: 'honesty.mode', value: mode, project: scope });
    this.dispatch({ method: 'ui.honestyStored', params: { mode } });
    this.ui.settingsChanged();
    const where = scope ? `the settings of the project ${scope}` : "calu's global settings";
    if (!attached) {
      this.notice('info', `Honesty mode "${mode}" is stored in ${where}.`);
      return;
    }
    const sessionId = this.sessions.attachedId;
    const until = Date.now() + MODE_ANNOUNCE_MS;
    while (Date.now() < until && this.sessions.attachedId === sessionId && this.chat.state.honesty?.mode !== mode) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (this.sessions.attachedId !== sessionId) return;
    if (this.chat.state.honesty?.mode === mode) this.notice('info', `Honesty mode is now "${mode}" (stored in ${where}); the session uses it.`);
    else {
      this.notice(
        'warning',
        `Honesty mode "${mode}" is stored in ${where}, but this session still reports "${this.chat.state.honesty?.mode ?? 'off'}": ` +
          'it has not read the change. A running session normally reads it within seconds. If it does not, ' +
          'its settings could not be reloaded (the chat then shows an error line; after an update of calu, ' +
          'restart `calu serve`). Sessions started from now on use the stored mode.',
      );
    }
  }

  /**
   * Certify a commit or a file's present content as honest for the user (`honesty.trust`).
   *
   * Side effects: with `ask`, an input asks for the commit ("REV" or "WORKTREE REV", as `/trust`
   * in `calu attach`) or the file (`HostUi.activeFile` is suggested). A confirmation then asks;
   * only its button sends the request, and a notice in the chat says what was recorded. Errors
   * (no attached session, a rev that names no commit, an unreadable file) are shown as a notice.
   *
   * @param target The `honestyTrust` message: no fields for HEAD, `rev` and `checkout` for a
   *   commit, `path` (relative to the workspace, or absolute) for a file, `ask` to ask first.
   * @returns Nothing; cancelling does nothing.
   */
  async trustHonesty(target: Extract<WebviewMessage, { type: 'honestyTrust' }>): Promise<void> {
    let { rev, checkout, path } = target;
    if (target.ask === 'rev') {
      const typed = await this.ui.input({
        title: 'calu: trust a commit as honest',
        prompt: 'A commit id, branch or tag. For a commit of a worktree, its name first: "agent-a1b2 main".',
        value: 'HEAD',
        validate: (text) => (trustArgs(text) ? undefined : 'At most a worktree and a commit, branch or tag.'),
      });
      const words = typed === undefined ? undefined : trustArgs(typed);
      if (!words) return;
      ({ rev, checkout } = words);
    } else if (target.ask === 'file') {
      const typed = await this.ui.input({
        title: 'calu: trust a file as honest',
        prompt: 'The file, relative to the workspace. Its present content is recorded as honest.',
        value: this.ui.activeFile(),
      });
      if (!typed?.trim()) return;
      path = typed.trim();
    }
    const commit = `${rev ?? 'HEAD'}${checkout ? ` in the worktree ${checkout}` : ''}`;
    const yes = await this.ui.confirm(
      path !== undefined ? `calu: trust the present content of ${path}?` : `calu: trust ${commit}?`,
      path !== undefined
        ? 'The file as it is now is recorded as honest; a later change makes it unreviewed again.'
        : 'The commit it names now is recorded as honest; it becomes the baseline of the honesty state.',
      'Trust',
    );
    if (!yes) return;
    await this.run(path !== undefined ? `Trusting ${path}` : `Trusting ${commit}`, async () => {
      const result = await this.bridge.session(
        'honesty.trust',
        path !== undefined ? { path } : { rev: rev ?? 'HEAD', ...(checkout ? { checkout } : {}) },
      );
      const what = result.commit_id ? `Commit ${result.commit_id.slice(0, 12)} (${commit})` : (result.path ?? path ?? commit);
      const now = result.commit_id ? 'is trusted now; it is the baseline of the honesty state.' : 'is trusted now.';
      this.notice('info', result.recorded ? `${what} ${now}` : `${what} was trusted already.`);
    });
  }

  /**
   * Ask the attached session for its honesty graph (`honesty.graph`) and send the answer to
   * the chat view as `honestyGraph`. A failure is sent as the message's `error` and logged; it
   * is not shown as a notice, because the graph view shows it.
   *
   * @returns A promise that resolves when the answer was sent (never rejects).
   */
  private async sendHonestyGraph(): Promise<void> {
    const sessionId = this.sessions.attachedId;
    try {
      const graph = await this.bridge.session('honesty.graph', {});
      this.chat.send({ type: 'honestyGraph', sessionId, graph });
    } catch (error) {
      this.ui.log(`[calu] the honesty graph was not loaded: ${errorText(error)}`);
      this.chat.send({ type: 'honestyGraph', sessionId, graph: null, error: errorText(error) });
    }
  }

  /**
   * Record a unit or a whole file as dishonest for the user (`honesty.mark`).
   *
   * Side effects: an input asks for the reason in one line. Only an accepted, non-blank reason
   * sends the request. An error (no attached session, a file that is not in the project) is
   * shown as a notice.
   *
   * @param path The file, relative to the workspace.
   * @param symbol The dotted name of the unit; "" for the whole file.
   * @returns Nothing; cancelling does nothing.
   */
  private async markDishonest(path: string, symbol: string): Promise<void> {
    const what = symbol ? `${symbol} in ${path}` : path;
    const typed = await this.ui.input({
      title: 'calu: mark as dishonest',
      prompt: `What is wrong with ${what}? One sentence; the mark stays open until the code is fixed or the mark is cleared.`,
      validate: (text) => (!text.trim() ? 'A reason is needed.' : text.trim().length > MAX_MARK_REASON ? `At most ${MAX_MARK_REASON} characters.` : undefined),
    });
    const reason = typed?.trim();
    if (!reason || reason.length > MAX_MARK_REASON) return;
    await this.run(`Marking ${what} as dishonest`, () => this.bridge.session('honesty.mark', { path, symbol, reason }));
  }

  // ---------------------------------------------------------------- hints and usage

  /**
   * Recompute the setup hints and the server help of the chat view, and send them if they
   * changed.
   *
   * @returns Nothing.
   */
  updateHints(): void {
    const sessions = this.sessions;
    const attached = sessions.attachedId !== null;
    const hints = buildHints({
      caluMissing: this.bridge.status === 'missing',
      status: sessions.status,
      attached,
      noServer: sessions.noServer,
      locked: this.bridge.locked && !attached,
      serverStopped: this.serverStopped,
      serverRetriedAt: this.serverRetriedAt,
    });
    const hintsText = JSON.stringify(hints);
    if (hintsText !== this.lastHints) {
      this.lastHints = hintsText;
      this.dispatch({ method: 'ui.hints', params: { hints } });
    }
    // The empty state shows how to start the server; an attached view does not need it.
    const help = !attached && this.bridge.status === 'running' ? sessions.serverHelp() : null;
    const helpText = JSON.stringify(help);
    if (helpText !== this.lastServerHelp) {
      this.lastServerHelp = helpText;
      this.dispatch({ method: 'ui.serverHelp', params: help ? { ...help } : {} });
    }
  }

  /**
   * Ask the attached session for the usage panel (`usage.panel`): `ui.usagePanel` "asking"
   * first, then the answer or the refusal. Without an attached session nothing is asked.
   *
   * @returns A promise that resolves when the answer or the refusal was applied.
   */
  private async openUsagePanel(): Promise<void> {
    if (!isAttached(this.chat.state)) return;
    this.dispatch({ method: 'ui.usagePanel', params: { phase: 'asking' } });
    try {
      const result = await this.bridge.session('usage.panel', {});
      this.dispatch({ method: 'ui.usagePanel', params: { result: result as unknown as Record<string, unknown> } });
    } catch (error) {
      this.dispatch({ method: 'ui.usagePanel', params: { error: errorText(error) } });
    }
  }

  /**
   * Ask the attached session for its usage and put the answer into the chat view. Failures
   * are logged only.
   *
   * @returns A promise that resolves when the answer was applied.
   */
  async refreshUsage(): Promise<void> {
    if (!isAttached(this.chat.state)) return;
    this.lastUsageAt = Date.now();
    try {
      const usage = await this.bridge.session('usage.get', {});
      this.dispatch({ method: 'ui.usage', params: usage as unknown as Record<string, unknown> });
    } catch (error) {
      this.ui.log(`[calu] the usage of the session was not read: ${errorText(error)}`);
    }
  }

  // ---------------------------------------------------------------- undo, fork, decisions

  /**
   * Find the conversation's number of the turn a user message of the chat belongs to.
   *
   * @param turn The turn as the view counted it (`UserItem.turn`).
   * @param uuid Claude Code's id of the message, if known.
   * @returns The `index` of the recorded turn whose prompt has `uuid` (`session.history`); if
   *   none is found or the history cannot be read, `turn`.
   */
  private async conversationTurn(turn: number, uuid: string | undefined): Promise<number> {
    if (!uuid) return turn;
    try {
      const history = await this.bridge.session('session.history', { limit: 1000 });
      const found = (history.turns ?? []).find((t) => t.prompt_uuid === uuid);
      return found ? found.index : turn;
    } catch (error) {
      this.ui.log(`[calu] the recorded turns of the conversation were not read: ${errorText(error)}`);
      return turn;
    }
  }

  /**
   * Start a session that continues the attached conversation from before one of its turns.
   *
   * @param turn The turn as the view counted it.
   * @param uuid Claude Code's id of the user message, if known.
   * @param rewind True for "Rewind": asks for a confirmation first and puts the message's text
   *   back into the input afterwards. False for "Fork from here".
   * @returns A promise that resolves when the session started, failed or the user cancelled.
   */
  private async forkBefore(turn: number, uuid: string | undefined, rewind: boolean): Promise<void> {
    const state = this.chat.state;
    const conversation = state.session.claudeSessionId;
    if (!isAttached(state) || !conversation) {
      void this.ui.notify('info', 'calu: attach to a session with a conversation first.');
      return;
    }
    const message = (own(state.items, 'main') ?? []).find((i) => i.kind === 'user' && (uuid ? i.uuid === uuid : i.turn === turn));
    const number = await this.conversationTurn(turn, uuid);
    if (number < 2) {
      void this.ui.notify('info', 'calu: this is the first message of the conversation, so nothing would be kept. Start a new session instead.');
      return;
    }
    if (rewind) {
      const yes = await this.ui.confirm(
        'calu: rewind to before this message?',
        'A new session starts that continues the conversation from before that turn. Files are not ' +
          'reverted: what Claude changed since then stays on disk. The current session keeps running; ' +
          'you can switch back to it in the session menu.',
        'Rewind',
      );
      if (!yes) return;
    }
    const started = await this.sessions.forkBefore(conversation, number, rewind);
    if (started && rewind && message?.kind === 'user') this.chat.send({ type: 'restoreInput', text: message.text });
  }

  /**
   * Start a new session from a copy of the attached conversation.
   *
   * @returns A promise that resolves when the session started or the failure was shown.
   */
  async forkConversation(): Promise<void> {
    const conversation = this.chat.state.session.claudeSessionId;
    if (!conversation) {
      void this.ui.notify('info', 'calu: attach to a session with a conversation first.');
      return;
    }
    await this.sessions.forkConversation(conversation);
  }

  /**
   * Tell whether the user cannot see the chat right now.
   *
   * @returns True if the chat is hidden or the window does not have the focus.
   */
  private chatUnseen(): boolean {
    return !this.chat.isVisible || !this.ui.focused();
  }

  /**
   * Send the user's decision on an open escalation and mark the card pending.
   *
   * @param key The escalation's state key (`EscalationView.key`).
   * @param decision "allow" or "deny".
   * @param note Optional note for Claude.
   * @param answer Optional answer to the agent's questions.
   * @returns A promise that resolves when the decision was sent or the failure was shown.
   */
  private async decide(key: string, decision: 'allow' | 'deny', note?: string, answer?: string): Promise<void> {
    const escalation = own(this.chat.state.escalations, key);
    if (!escalation || escalation.status !== 'open') return;
    this.dispatch({
      method: 'ui.decisionPending',
      params: { escalation_key: escalation.key, ...(answer !== undefined ? { answer } : {}) },
    });
    const sent = await this.run('Sending the decision', () =>
      this.bridge.session('escalation.decide', {
        escalation_id: escalation.escalationId,
        decision,
        note: note ?? null,
        answer: answer ?? null,
      }),
    );
    if (!sent) this.dispatch({ method: 'ui.decisionFailed', params: { escalation_key: escalation.key } });
  }

  /**
   * Show a notification for an escalation or a question that arrived while the chat cannot be
   * seen: "approval needed" with Allow / Deny / Open, or "Claude asks" with Open.
   *
   * @param params The params of `escalation.request` (already applied to the state).
   * @returns A promise that resolves after the user's choice was handled.
   */
  private async announceEscalation(params: Record<string, unknown>): Promise<void> {
    const id = typeof params.escalation_id === 'string' ? params.escalation_id : '';
    const card = Object.values(this.chat.state.escalations)
      .filter((e) => e.escalationId === id && e.status === 'open')
      .pop();
    if (!card) return;
    if (card.questions.length > 0) {
      const question = card.questions[0].question.replace(/\s+/g, ' ').slice(0, 200);
      const choice = await this.ui.notify('info', `calu: Claude asks: ${question}`, 'Open');
      if (choice === 'Open') await this.chat.reveal();
      return;
    }
    const choice = await this.ui.notify('warning', `calu: approval needed: ${card.toolName}`, 'Allow', 'Deny', 'Open');
    if (choice === 'Open') await this.chat.reveal();
    else if (choice === 'Allow' || choice === 'Deny') await this.decide(card.key, choice === 'Allow' ? 'allow' : 'deny');
  }

  // ---------------------------------------------------------------- bridge events

  /**
   * Handle one event of the attached session.
   *
   * @param method The event's method.
   * @param params The event's params.
   * @returns Nothing.
   */
  private onSessionEvent(method: string, params: Record<string, unknown>): void {
    this.ui.sessionEvent(method, params);
    this.dispatch({ method, params });
    const mode = this.ui.notificationMode();
    if (method === 'escalation.request' && mode !== 'off' && this.chatUnseen()) void this.announceEscalation(params);
    if (method === 'turn.result') {
      void this.refreshUsage();
      if (mode === 'all' && this.chatUnseen()) {
        void this.ui.notify('info', 'calu: turn finished.', 'Open').then((choice) => (choice === 'Open' ? this.chat.reveal() : undefined));
      }
    }
    // Claude Code reports its limits while it works: the context meter is asked again then
    // (not more often than every `USAGE_REFRESH_MS`), so that it moves during a long turn.
    if (method === 'usage.update' && Date.now() - this.lastUsageAt >= USAGE_REFRESH_MS) void this.refreshUsage();
  }

  /**
   * React to a lost connection to the session (`shared/serverLoss.ts`). Nothing answers at the
   * session's address and no server runs: say "The calu server stopped" with "Start server" and
   * "Retry". Nothing answers but a server runs: the session ended; the chat shows the session
   * list, no notification. Any other loss: offer to attach again; "Attach again" tries the same
   * session first and shows the session list only if that session is gone.
   *
   * @param message The bridge's explanation.
   * @param cause The bridge's `cause` (untrusted; missing from an older calu).
   * @returns A promise that resolves after the user's choice was handled.
   */
  private async onLinkLost(message: string, cause: unknown): Promise<void> {
    const sessions = this.sessions;
    sessions.attachedId = null;
    const why = lostCause(cause, message);
    if (why === 'unreachable') {
      const status = await sessions.refreshStatus();
      void sessions.refresh();
      if (!serverStoppedAfterLoss(why, serverCheck(status))) {
        this.updateHints();
        return;
      }
      this.serverStopped = true;
      this.serverRetriedAt = null;
      this.updateHints();
      const choice = await this.ui.notify('warning', 'calu: the calu server stopped; the session ended with it.', 'Start server', 'Retry');
      if (choice === 'Start server') sessions.startServer();
      else if (choice === 'Retry') await this.lookForServer(true);
      return;
    }
    this.updateHints();
    void sessions.refresh();
    const choice = await this.ui.notify('warning', `calu: the connection to the session was lost${message ? `: ${message}` : '.'}`, 'Attach again');
    if (choice !== 'Attach again') return;
    const same = sessions.lastAttachedId !== null && this.chat.state.session.sessionId === sessions.lastAttachedId;
    if (!(await sessions.reattach(same ? this.chat.state.lastSeq : 0))) await sessions.pick();
  }

  /**
   * While "The calu server stopped" is shown: ask `ide.status` whether a server runs again; if
   * one runs, or (for Retry) a session can be attached anyway, attach as at the start and drop
   * the card.
   *
   * @param retry True for the Retry button: the attach is tried even if the status cannot
   *   tell, and a failed look is shown with its time on the card.
   * @returns A promise that resolves when the look is over (never rejects).
   */
  async lookForServer(retry: boolean): Promise<void> {
    if (!this.serverStopped || this.lookingForServer) return;
    this.lookingForServer = true;
    try {
      const check = serverCheck(await this.sessions.refreshStatus());
      if (check === 'running' || (retry && check === 'unknown')) {
        await this.connectWorkspace(false);
        if (check === 'running' || this.sessions.attachedId !== null) {
          this.serverStopped = false;
          this.serverRetriedAt = null;
        }
      }
      if (this.serverStopped && retry) this.serverRetriedAt = new Date().toLocaleTimeString();
      this.updateHints();
    } catch (error) {
      this.ui.log(`[calu] looking for the calu server failed: ${errorText(error)}`);
    } finally {
      this.lookingForServer = false;
    }
  }
}
