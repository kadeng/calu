/**
 * Entry point of the calu extension: creates the parts and wires them together.
 *
 *     activate
 *       ├─ Bridge (calu ide)  ──status──► chat view (`bridge.status`), then: ide.status, the one
 *       │        │                        password if locked (kept in memory, sections 8 and 10), attach
 *       │        └─notifications─► session.event ──► DiffStore, chat view, context document
 *       │                          ide.link      ──► chat view (`ui.link`); "lost" offers to attach
 *       │                          ide.log       ──► output channel "calu"
 *       ├─ ChatViewProvider (sidebar view calu.chat)  ◄──messages──► handleWebviewMessage
 *       ├─ Sessions (QuickPick, ide.attach / ide.start, server terminal)
 *       ├─ SettingsPanel, CheckerFileSystem, ContextDocuments, DiffStore, Installer
 *       └─ commands calu.* (package.json)
 *
 * The extension starts only `calu ide`. Sessions run in `calu serve`, which the user starts in
 * a terminal; they keep running when the editor closes.
 */

import * as vscode from 'vscode';
import { Installer } from './bootstrap';
import { Bridge, bridgeWorkspaceFolder, errorText } from './bridge';
import type { AnyChecker, IdeAttachResult, IdeStatusResult } from './bridgeProtocol';
import { CHAT_VIEW_ID, ChatViewProvider } from './chatView';
import { CHECKER_SCHEME, CHECKER_STORED_SCHEME, CheckerFileSystem, pickChecker } from './checkerFs';
import { addCommentCommand, inputBoxProvider } from './comment';
import { normaliseRelativePath } from './commentPayload';
import { CONTEXT_SCHEME, ContextDocuments } from './contextDoc';
import { DIFF_SCHEME, DiffStore } from './diffView';
import { watchSaves } from './humanDiffs';
import { runItemAction } from './itemActions';
import type { ChatSendParams, EditDiff } from './protocol.generated';
import { Sessions } from './sessions';
import { SettingsPanel } from './settingsPanel';
import { CLAUDE_DOCS_URL } from './shared/bootstrap';
import { statusBarEntry } from './shared/chatModel';
import { buildHints } from './shared/hints';
import { lostCause, serverCheck, serverStoppedAfterLoss } from './shared/serverLoss';
import { flagsForLevel } from './shared/levels';
import type { ChatSettings, WebviewMessage } from './shared/messages';
import { rootInstallCommand } from './shared/serverCommand';
import type { FixAction } from './shared/settingsModel';
import { isAttached, isKnownFile, own, sessionTitle, sideHistory, type ViewEvent } from './shared/viewState';

/** Milliseconds between two refreshes of the session list while the chat view is visible. */
const SESSIONS_REFRESH_MS = 10_000;

/** Most steps asked from `session.history` for the recorded turns above the transcript. */
const HISTORY_STEPS = 500;

/** Least milliseconds between two `usage.get` requests that events of a running turn cause. */
const USAGE_REFRESH_MS = 2000;

/** Milliseconds a session gets to report a honesty mode that was just stored for it. */
const MODE_ANNOUNCE_MS = 4000;

/** Key of the workspace state that holds the id of the session this workspace showed last. */
const LAST_SESSION_KEY = 'calu.lastAttachedSession';

/** What `activate` returns (used by integration tests). */
export interface CaluApi {
  bridge: Bridge;
  chat: ChatViewProvider;
  sessions: Sessions;
  /**
   * Handle a message as if the chat webview had posted it (the integration tests send chat
   * messages and decisions this way).
   *
   * @param message The message.
   * @returns A promise that resolves when the message was handled.
   */
  handleWebviewMessage(message: WebviewMessage): Promise<void>;
}

/**
 * Activate the extension (called by VSCode once, after start-up): create the parts, register
 * views, providers and commands, and start the bridge in the background.
 *
 * @param context The extension context; everything created here is disposed with it.
 * @returns The parts, for tests.
 */
export function activate(context: vscode.ExtensionContext): CaluApi {
  const output = vscode.window.createOutputChannel('calu');
  const version = String((context.extension.packageJSON as { version?: unknown }).version ?? '0');
  // The one password (spec sections 8 and 10): kept in this variable until the window closes,
  // so that a restarted bridge is unlocked again without asking. Never logged or sent anywhere
  // but to `ide.unlock`.
  let heldPassword: string | undefined;
  const bridge = new Bridge(output, version, {
    stored: async () => heldPassword,
    store: async (password) => {
      heldPassword = password;
    },
    forget: async () => {
      heldPassword = undefined;
    },
    offerRemember: async () => true,
  });
  const installer = new Installer(output);
  const diffs = new DiffStore();
  let nextSideId = 1;
  /** Time (ms) of the last `usage.get` request. */
  let lastUsageAt = 0;
  let lastHints = '';
  let lastServerHelp = '';
  /** True once the install offer for a missing calu was shown in this activation. */
  let installOffered = false;
  /** True once the password prompt was opened by itself for the running bridge process. */
  let askedAtStart = false;
  /**
   * True from a lost link whose server stopped (`serverStoppedAfterLoss`) until a server is
   * found again or a session is attached: the chat shows "The calu server stopped".
   */
  let serverStopped = false;
  /** Local time of the last Retry that found no server; null before the first. */
  let serverRetriedAt: string | null = null;
  /** True while a look for the stopped server runs (Retry or the timer), so looks do not overlap. */
  let lookingForServer = false;

  // ---------------------------------------------------------------- chat view and sessions

  const chat = new ChatViewProvider(
    context.extensionUri,
    (message) => void handleWebviewMessage(message),
    () => void bridge.ensureStarted().catch(() => {}),
    (visible) => {
      if (visible) void sessions.refresh();
    },
  );

  // The "calu" item of the status bar: always visible, also while the view is hidden or folded
  // away behind the secondary side bar's "…" (a new profile shows the editor's own chat first).
  const statusItem = vscode.window.createStatusBarItem('calu.status', vscode.StatusBarAlignment.Right, 100);
  statusItem.name = 'calu';
  statusItem.command = 'calu.openChat';
  let statusShown = '';
  chat.onStateChange = (state) => {
    const entry = statusBarEntry(state);
    const key = `${entry.text}\n${entry.tooltip}\n${entry.warning}`;
    if (key === statusShown) return;
    statusShown = key;
    statusItem.text = entry.text;
    statusItem.tooltip = entry.tooltip;
    statusItem.backgroundColor = entry.warning ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
  };
  chat.onStateChange(chat.state);
  statusItem.show();
  context.subscriptions.push(statusItem);

  /**
   * Apply a view event to the chat view.
   *
   * @param event The event.
   * @returns Nothing.
   */
  const dispatch = (event: ViewEvent): void => {
    chat.dispatch(event);
    // A session request that waits is shown in the chat: make sure the chat can be seen.
    if (event.method === 'ui.starting' && typeof event.params.title === 'string' && event.params.slow !== true) void chat.reveal();
  };

  const sessions: Sessions = new Sessions(
    bridge,
    dispatch,
    onAttached,
    updateHints,
    output,
    (id) => {
      const entry = chat.state.sessions.list.find((s) => s.id === id);
      if (entry) return sessionTitle(entry);
      return id === chat.state.session.sessionId ? sessionTitle({ id, name: '', directory: chat.state.session.workspace ?? '' }) : undefined;
    },
    (id) => void context.workspaceState.update(LAST_SESSION_KEY, id ?? undefined),
  );
  // A reloaded window (or a restarted VSCode) attaches to the session this workspace showed last,
  // if it still runs; otherwise to the session of the workspace, as at a first start.
  const remembered = context.workspaceState.get<string>(LAST_SESSION_KEY);
  if (typeof remembered === 'string' && remembered) sessions.lastAttachedId = remembered;

  /**
   * Show a message in the chat's main list.
   *
   * @param level "info", "warning" or "error".
   * @param text The message.
   * @returns Nothing.
   */
  function notice(level: string, text: string): void {
    dispatch({ method: 'ui.notice', params: { level, text } });
  }

  /**
   * Run an action and report a failure in the chat and the log.
   *
   * @param what What was attempted, e.g. "interrupt"; start of the error message.
   * @param action The action.
   * @returns True if the action finished without error.
   */
  async function run(what: string, action: () => Promise<unknown>): Promise<boolean> {
    try {
      await action();
      return true;
    } catch (error) {
      output.appendLine(`[calu] ${what} failed: ${errorText(error)}`);
      notice('error', `${what} failed: ${errorText(error)}`);
      return false;
    }
  }

  /**
   * Take over the result of `ide.attach` / `ide.start`: remember the diffs of recorded edits,
   * rebuild the chat view (or continue it, with `resumed`), reload the context document, ask
   * for the usage, and load what the replayed events do not hold: the recorded earlier turns of
   * the conversation and the stored honesty mode.
   *
   * @param result The result.
   * @param resumed True if `result.events` are only those after the ones the chat view has.
   * @returns Nothing.
   */
  function onAttached(result: IdeAttachResult, resumed: boolean): void {
    serverStopped = false;
    serverRetriedAt = null;
    for (const event of result.events ?? []) {
      if (event?.method === 'edit.diff') diffs.record(event.params as unknown as EditDiff);
    }
    dispatch({ method: 'ui.attached', params: { ...(result as unknown as Record<string, unknown>), resumed } });
    contextDocs.refresh();
    void refreshUsage();
    void chat.reveal();
    if (!resumed) {
      void loadHistory(result.session_id);
      void loadStoredHonesty(result.session_id);
    }
  }

  /**
   * Ask the attached session for its recorded conversation and put the turns that the replayed
   * events do not cover above the transcript (`ui.history`): the earlier turns of a resumed,
   * forked or rewound conversation, or of a session whose replay buffer moved on. Failures are
   * logged only; the transcript then shows the session's own events.
   *
   * @param sessionId The session the attach was for; the answer is dropped if another one is
   *   attached when it arrives.
   * @returns A promise that resolves when the answer was applied (never rejects).
   */
  async function loadHistory(sessionId: string): Promise<void> {
    try {
      const history = await bridge.session('session.history', { limit: HISTORY_STEPS });
      if (sessions.attachedId !== sessionId) return;
      dispatch({ method: 'ui.history', params: { session_id: sessionId, history: history as unknown as Record<string, unknown> } });
    } catch (error) {
      output.appendLine(`[calu] the recorded conversation was not loaded: ${errorText(error)}`);
    }
  }

  /**
   * Find the scope that decides `honesty.mode` for the attached session and the value stored
   * there, as calu's terminal client does: the project's own setting wins over the global one.
   *
   * @param interactive True if the user asked for a change (a needed password is then asked).
   * @returns `project`: the session's project if its settings name `honesty.mode`, else `null`
   *   (the global scope); `mode`: the stored value, or `null` if it is unknown.
   * @throws (rejects) Error if the settings cannot be read.
   */
  async function storedHonesty(interactive: boolean): Promise<{ project: string | null; mode: string | null }> {
    const mode = (rows: readonly { key: string; value: unknown }[]): string | null => {
      const value = rows.find((r) => r.key === 'honesty.mode')?.value;
      return typeof value === 'string' ? value : null;
    };
    const project = chat.state.session.project;
    if (project) {
      const own = await bridge.request('ide.settings.get', { project }, interactive);
      if (own.rows.find((r) => r.key === 'honesty.mode')?.is_set) return { project, mode: mode(own.rows) };
    }
    return { project: null, mode: mode((await bridge.request('ide.settings.get', { project: null }, interactive)).rows) };
  }

  /**
   * Read the stored honesty mode for the header chip (`ui.honestyStored`), which shows it next
   * to the session's mode when they differ. No password prompt; failures are logged only.
   *
   * @param sessionId The session the attach was for; the answer is dropped if another one is
   *   attached when it arrives.
   * @returns A promise that resolves when the answer was applied (never rejects).
   */
  async function loadStoredHonesty(sessionId: string): Promise<void> {
    try {
      const stored = await storedHonesty(false);
      if (sessions.attachedId === sessionId) dispatch({ method: 'ui.honestyStored', params: { mode: stored.mode } });
    } catch (error) {
      output.appendLine(`[calu] the stored honesty mode was not read: ${errorText(error)}`);
    }
  }

  /**
   * Store `honesty.mode` where the attached session reads it from, and say what happened: a
   * running session reads the change by itself and reports it (`honesty.update`); if it has
   * not done so after `MODE_ANNOUNCE_MS`, the chat says that the session still uses its mode
   * (the header then shows both).
   *
   * @param mode "off", "track" or "enforce".
   * @returns A promise that resolves when the mode was stored and the notice was shown.
   * @throws (rejects) Error if the setting cannot be stored.
   */
  async function setHonestyMode(mode: string): Promise<void> {
    const attached = isAttached(chat.state);
    const scope = attached ? (await storedHonesty(true)).project : null;
    await bridge.request('ide.settings.set', { key: 'honesty.mode', value: mode, project: scope });
    dispatch({ method: 'ui.honestyStored', params: { mode } });
    void settingsPanel.reload();
    const where = scope ? `the settings of the project ${scope}` : "calu's global settings";
    if (!attached) {
      notice('info', `Honesty mode "${mode}" is stored in ${where}.`);
      return;
    }
    const sessionId = sessions.attachedId;
    const until = Date.now() + MODE_ANNOUNCE_MS;
    while (Date.now() < until && sessions.attachedId === sessionId && chat.state.honesty?.mode !== mode) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (sessions.attachedId !== sessionId) return;
    if (chat.state.honesty?.mode === mode) notice('info', `Honesty mode is now "${mode}" (stored in ${where}); the session uses it.`);
    else {
      notice(
        'warning',
        `Honesty mode "${mode}" is stored in ${where}, but this session still reports "${chat.state.honesty?.mode ?? 'off'}": ` +
          'it has not read the change. Sessions started from now on use the stored mode.',
      );
    }
  }

  /**
   * Recompute the setup hints and the server help of the chat view, and send them if they
   * changed.
   *
   * @returns Nothing.
   */
  function updateHints(): void {
    const attached = sessions.attachedId !== null;
    const hints = buildHints({
      caluMissing: bridge.status === 'missing',
      status: sessions.status,
      attached,
      noServer: sessions.noServer,
      locked: bridge.locked && !attached,
      serverStopped,
      serverRetriedAt,
    });
    const hintsText = JSON.stringify(hints);
    if (hintsText !== lastHints) {
      lastHints = hintsText;
      dispatch({ method: 'ui.hints', params: { hints } });
    }
    // The empty state shows how to start the server; an attached view does not need it.
    const help = !attached && bridge.status === 'running' ? sessions.serverHelp() : null;
    const helpText = JSON.stringify(help);
    if (helpText !== lastServerHelp) {
      lastServerHelp = helpText;
      dispatch({ method: 'ui.serverHelp', params: help ? { ...help } : {} });
    }
  }

  /**
   * Ask the attached session for the usage panel (`usage.panel`): `ui.usagePanel` "asking"
   * first, then the answer or the refusal. The request starts no turn and sends nothing to the
   * model. Without an attached session nothing is asked.
   *
   * @returns A promise that resolves when the answer or the refusal was applied.
   */
  async function openUsagePanel(): Promise<void> {
    if (!isAttached(chat.state)) return;
    dispatch({ method: 'ui.usagePanel', params: { phase: 'asking' } });
    try {
      const result = await bridge.session('usage.panel', {});
      dispatch({ method: 'ui.usagePanel', params: { result: result as unknown as Record<string, unknown> } });
    } catch (error) {
      dispatch({ method: 'ui.usagePanel', params: { error: errorText(error) } });
    }
  }

  /**
   * Ask the attached session for its usage and put the answer into the chat view. Failures
   * are logged only.
   *
   * @returns A promise that resolves when the answer was applied.
   */
  async function refreshUsage(): Promise<void> {
    if (!isAttached(chat.state)) return;
    lastUsageAt = Date.now();
    try {
      const usage = await bridge.session('usage.get', {});
      dispatch({ method: 'ui.usage', params: usage as unknown as Record<string, unknown> });
    } catch (error) {
      output.appendLine(`[calu] the usage of the session was not read: ${errorText(error)}`);
    }
  }

  /**
   * After the bridge started or was unlocked: check the setup, attach again to the session
   * that was attached before the bridge went away (asking only for the events the chat has not
   * seen), else to the session of the workspace if one runs, and load the session list. If the bridge reports that it is locked
   * and the chat view is visible, the one password is asked for (once per bridge process).
   *
   * @param mayAsk False to skip the password prompt (after the user dismissed it).
   * @returns A promise that resolves when all of it was tried (never rejects).
   */
  async function connectWorkspace(mayAsk: boolean): Promise<void> {
    const status = await sessions.refreshStatus();
    bridge.noteStatusLocked(status?.locked === true);
    if (status?.locked && mayAsk && chat.isVisible && !askedAtStart) {
      askedAtStart = true;
      if (!(await bridge.unlock().catch(() => false))) return;
      await sessions.refreshStatus();
    }
    const before = sessions.lastAttachedId;
    const afterSeq = before !== null && chat.state.session.sessionId === before ? chat.state.lastSeq : 0;
    if (!(await sessions.reattach(afterSeq))) await sessions.attach(null, true);
    await sessions.refresh();
  }

  /**
   * Ask the one password now (the chat's "Unlock" button and "calu: Unlock"), then connect.
   *
   * @returns A promise that resolves when the prompt was answered and the result applied.
   */
  async function unlock(): Promise<void> {
    try {
      if (await bridge.unlock()) await connectWorkspace(false);
    } catch (error) {
      void vscode.window.showErrorMessage(`calu: ${errorText(error)}`);
    }
  }

  // ---------------------------------------------------------------- undo, fork, honesty

  /**
   * Find the conversation's number of the turn a user message of the chat belongs to.
   *
   * @param turn The turn as the view counted it (`UserItem.turn`).
   * @param uuid Claude Code's id of the message, if known.
   * @returns The `index` of the recorded turn whose prompt has `uuid` (`session.history`); if
   *   none is found or the history cannot be read, `turn`.
   */
  async function conversationTurn(turn: number, uuid: string | undefined): Promise<number> {
    if (!uuid) return turn;
    try {
      const history = await bridge.session('session.history', { limit: 1000 });
      const found = (history.turns ?? []).find((t) => t.prompt_uuid === uuid);
      return found ? found.index : turn;
    } catch (error) {
      output.appendLine(`[calu] the recorded turns of the conversation were not read: ${errorText(error)}`);
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
  async function forkBefore(turn: number, uuid: string | undefined, rewind: boolean): Promise<void> {
    const conversation = chat.state.session.claudeSessionId;
    if (!isAttached(chat.state) || !conversation) {
      void vscode.window.showInformationMessage('calu: attach to a session with a conversation first.');
      return;
    }
    const message = (own(chat.state.items, 'main') ?? []).find(
      (i) => i.kind === 'user' && (uuid ? i.uuid === uuid : i.turn === turn),
    );
    const number = await conversationTurn(turn, uuid);
    if (number < 2) {
      void vscode.window.showInformationMessage(
        'calu: this is the first message of the conversation, so nothing would be kept. Start a new session instead.',
      );
      return;
    }
    if (rewind) {
      const choice = await vscode.window.showWarningMessage(
        'calu: rewind to before this message?',
        {
          modal: true,
          detail:
            'A new session starts that continues the conversation from before that turn. Files are not ' +
            'reverted: what Claude changed since then stays on disk. The current session keeps running; ' +
            'you can switch back to it in the session menu.',
        },
        'Rewind',
      );
      if (choice !== 'Rewind') return;
    }
    const started = await sessions.forkBefore(conversation, number, rewind);
    if (started && rewind && message?.kind === 'user') chat.send({ type: 'restoreInput', text: message.text });
  }

  /**
   * Start a new session from a copy of the attached conversation.
   *
   * @returns A promise that resolves when the session started or the failure was shown.
   */
  async function forkConversation(): Promise<void> {
    const conversation = chat.state.session.claudeSessionId;
    if (!conversation) {
      void vscode.window.showInformationMessage('calu: attach to a session with a conversation first.');
      return;
    }
    await sessions.forkConversation(conversation);
  }

  // ---------------------------------------------------------------- notifications

  /**
   * Read the `calu.notifications` setting.
   *
   * @returns "all", "decisions" (the default) or "off".
   */
  function notificationMode(): 'all' | 'decisions' | 'off' {
    const mode = vscode.workspace.getConfiguration('calu').get<string>('notifications', 'decisions');
    return mode === 'all' || mode === 'off' ? mode : 'decisions';
  }

  /**
   * Tell whether the user cannot see the chat right now.
   *
   * @returns True if the chat view is hidden or the window does not have the focus.
   */
  function chatUnseen(): boolean {
    return !chat.isVisible || !vscode.window.state.focused;
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
  async function decide(key: string, decision: 'allow' | 'deny', note?: string, answer?: string): Promise<void> {
    const escalation = own(chat.state.escalations, key);
    if (!escalation || escalation.status !== 'open') return;
    dispatch({
      method: 'ui.decisionPending',
      params: { escalation_key: escalation.key, ...(answer !== undefined ? { answer } : {}) },
    });
    const sent = await run('Sending the decision', () =>
      bridge.session('escalation.decide', {
        escalation_id: escalation.escalationId,
        decision,
        note: note ?? null,
        answer: answer ?? null,
      }),
    );
    if (!sent) dispatch({ method: 'ui.decisionFailed', params: { escalation_key: escalation.key } });
  }

  /**
   * Show a VSCode notification for an escalation or a question that arrived while the chat
   * cannot be seen: "approval needed" with Allow / Deny / Open, or "Claude asks" with Open.
   *
   * @param params The params of `escalation.request` (already applied to the state).
   * @returns A promise that resolves after the user's choice was handled.
   */
  async function announceEscalation(params: Record<string, unknown>): Promise<void> {
    const id = typeof params.escalation_id === 'string' ? params.escalation_id : '';
    const card = Object.values(chat.state.escalations).filter((e) => e.escalationId === id && e.status === 'open').pop();
    if (!card) return;
    if (card.questions.length > 0) {
      const question = card.questions[0].question.replace(/\s+/g, ' ').slice(0, 200);
      const choice = await vscode.window.showInformationMessage(`calu: Claude asks: ${question}`, 'Open');
      if (choice === 'Open') await chat.reveal();
      return;
    }
    const choice = await vscode.window.showWarningMessage(`calu: approval needed: ${card.toolName}`, 'Allow', 'Deny', 'Open');
    if (choice === 'Open') await chat.reveal();
    else if (choice === 'Allow' || choice === 'Deny') await decide(card.key, choice === 'Allow' ? 'allow' : 'deny');
  }

  /**
   * Show "turn finished" with the button Open.
   *
   * @returns A promise that resolves after the user's choice was handled.
   */
  async function announceTurnEnd(): Promise<void> {
    const choice = await vscode.window.showInformationMessage('calu: turn finished.', 'Open');
    if (choice === 'Open') await chat.reveal();
  }

  // ---------------------------------------------------------------- documents and panels

  const contextDocs = new ContextDocuments(
    () => bridge.session('context.get', {}),
    (escalationId) => bridge.session('escalation.context', { escalation_id: escalationId }),
    errorText,
  );
  const checkerFs = new CheckerFileSystem(bridge);

  /**
   * List the checkers: those of the attached session, else those of calu's settings.
   *
   * @returns Name, route and model per checker.
   * @throws (rejects) Error if neither source can be asked.
   */
  async function listCheckers(): Promise<AnyChecker[]> {
    await bridge.ensureStarted();
    if (isAttached(chat.state)) {
      try {
        return (await bridge.session('checkers.get', {})).checkers ?? [];
      } catch (error) {
        output.appendLine(`[calu] the checkers of the session were not listed (calu's settings are asked instead): ${errorText(error)}`);
      }
    }
    return (await bridge.request('ide.settings.get', { project: null })).checkers ?? [];
  }

  /**
   * Open a checker's instructions in an editor.
   *
   * @param name The checker's name; if missing, the user chooses among the checkers of
   *   `route`, or among all checkers without a route.
   * @param route A checker route ("command", "edit", "changeset", "subagent").
   * @returns A promise that resolves when the editor is open or the user cancelled. Failures
   *   are shown to the user.
   */
  async function editChecker(name?: string, route?: string): Promise<void> {
    try {
      let chosen = name;
      if (!chosen) {
        const all = await listCheckers();
        const ofRoute = route ? all.filter((c) => c.route === route) : all;
        chosen = await pickChecker(ofRoute.length > 0 ? ofRoute : all);
        if (!chosen) {
          if (all.length === 0) void vscode.window.showInformationMessage('calu: no checkers are defined.');
          return;
        }
      }
      await checkerFs.open(chosen);
    } catch (error) {
      void vscode.window.showErrorMessage(`calu: the checker instructions could not be opened: ${errorText(error)}`);
    }
  }

  /**
   * Run a fix of the settings dialog's Setup card.
   *
   * @param action The fix.
   * @returns A promise that resolves when the fix is done.
   */
  async function fix(action: FixAction): Promise<void> {
    switch (action) {
      case 'installCalu':
        await installCalu();
        return;
      case 'updateCalu':
        await updateCalu();
        return;
      case 'startServer':
        sessions.startServer();
        return;
      case 'copyServerCommand':
        await vscode.env.clipboard.writeText(sessions.serverHelp().command);
        return;
      case 'copyRootInstall':
        await vscode.env.clipboard.writeText(rootInstallCommand(installer.source()));
        return;
      case 'openClaudeDocs':
        await vscode.env.openExternal(vscode.Uri.parse(CLAUDE_DOCS_URL, true));
        return;
      case 'restartBridge':
        await bridge.restart().catch(() => {});
        return;
    }
  }

  const settingsPanel = new SettingsPanel(context.extensionUri, bridge, {
    editChecker: (name) => editChecker(name),
    fix,
    serverHelp: () => sessions.serverHelp(),
    rootInstallCommand: () => {
      try {
        return rootInstallCommand(installer.source());
      } catch {
        return '';
      }
    },
    statusLoaded: (status: IdeStatusResult) => {
      sessions.status = status;
      updateHints();
    },
  });

  // ---------------------------------------------------------------- installing

  /**
   * Install calu after a confirmation, then start the bridge.
   *
   * @returns A promise that resolves when the installation ended (in either way).
   */
  async function installCalu(): Promise<void> {
    if (await installer.install()) {
      await bridge.restart().catch(() => {});
      void settingsPanel.reload();
    }
  }

  /**
   * Update calu with uv, then restart the bridge so it runs the new version. Attached sessions
   * keep running in the server; the chat attaches again.
   *
   * @returns A promise that resolves when the update ended (in either way).
   */
  async function updateCalu(): Promise<void> {
    if (await installer.update()) {
      await bridge.restart().catch(() => {});
      void settingsPanel.reload();
    }
  }

  /**
   * Run the environment check and show its result: missing parts as a warning (with the
   * address of Claude Code's instructions if `claude` is missing), else a short confirmation.
   *
   * @returns A promise that resolves when the result was shown.
   */
  async function checkSetup(): Promise<void> {
    if (bridge.status === 'missing' || !(await bridge.ensureStarted().then(() => true, () => false))) {
      if (bridge.status === 'missing') {
        const choice = await vscode.window.showWarningMessage('calu is not installed on this machine.', 'Install calu');
        if (choice === 'Install calu') await installCalu();
      }
      const claude = await installer.checkClaude();
      if (!claude.found) await installer.reportClaudeMissing(claude);
      return;
    }
    const status = await sessions.refreshStatus();
    if (!status) {
      void vscode.window.showErrorMessage('calu: the setup could not be checked; see the log ("calu: Show Log").');
      return;
    }
    const missing = status.findings.filter((f) => !f.ok && f.level === 'missing');
    if (missing.some((f) => f.name === 'claude')) {
      const claude = await installer.checkClaude();
      if (!claude.found) await installer.reportClaudeMissing(claude);
    }
    const lines = missing.map((f) => `${f.name}: ${f.message}${f.advice ? ` ${f.advice}` : ''}`);
    if (!status.database.reachable) lines.push(`database: ${status.database.problem}`);
    if (lines.length === 0) {
      void vscode.window.showInformationMessage('calu: the setup is complete.');
      return;
    }
    const choice = await vscode.window.showWarningMessage(`calu: ${lines.join(' · ')}`, 'Open Settings');
    if (choice === 'Open Settings') settingsPanel.show();
  }

  // ---------------------------------------------------------------- bridge events

  context.subscriptions.push(
    bridge.onStatus(({ status, message, version: caluVersion }) => {
      dispatch({
        method: 'bridge.status',
        params: { status, ...(message ? { message } : {}), ...(caluVersion ? { version: caluVersion } : {}) },
      });
      if (status !== 'running') sessions.attachedId = null;
      updateHints();
      if (status === 'missing' && !installOffered) {
        // Once per activation; afterwards the chat view's hint keeps offering it.
        installOffered = true;
        void vscode.window
          .showInformationMessage('calu is not installed on this machine.', 'Install calu')
          .then((choice) => (choice === 'Install calu' ? installCalu() : undefined));
      }
      if (status === 'running') {
        // Fresh bridge: check the setup, attach to the session of the workspace if one runs.
        askedAtStart = false;
        void connectWorkspace(true);
      }
    }),
    bridge.onLockedChange(() => updateHints()),
    bridge.onNotification((notification) => {
      if (notification.method === 'session.event') {
        const method = notification.params.method;
        const params = notification.params.params;
        if (typeof method !== 'string' || !params || typeof params !== 'object') return;
        onSessionEvent(method, params as Record<string, unknown>);
      } else if (notification.method === 'ide.link') {
        dispatch({ method: 'ui.link', params: notification.params });
        if (notification.params.state === 'lost') void onLinkLost(String(notification.params.message ?? ''), notification.params.cause);
      } else if (notification.method === 'ide.log') {
        output.appendLine(`[calu ide] ${String(notification.params.level ?? 'info')}: ${String(notification.params.message ?? '')}`);
      }
    }),
  );

  /**
   * Handle one event of the attached session.
   *
   * @param method The event's method.
   * @param params The event's params.
   * @returns Nothing.
   */
  function onSessionEvent(method: string, params: Record<string, unknown>): void {
    if (method === 'edit.diff') diffs.record(params as unknown as EditDiff);
    dispatch({ method, params });
    contextDocs.onEvent(method);
    const mode = notificationMode();
    if (method === 'escalation.request' && mode !== 'off' && chatUnseen()) void announceEscalation(params);
    if (method === 'turn.result') {
      void refreshUsage();
      if (mode === 'all' && chatUnseen()) void announceTurnEnd();
    }
    // Claude Code reports its limits while it works: the context meter is asked again then
    // (not more often than every `USAGE_REFRESH_MS`), so that it moves during a long turn.
    if (method === 'usage.update' && Date.now() - lastUsageAt >= USAGE_REFRESH_MS) void refreshUsage();
  }

  /**
   * React to a lost connection to the session (`shared/serverLoss.ts`). Nothing answers at the
   * session's address and no server runs: say "The calu server stopped" with "Start server" and
   * "Retry" (the chat's card and a notification). Nothing answers but a server runs: the
   * session ended; the chat shows the session list, no notification. Any other loss: offer to
   * attach again; "Attach again" tries the same session first (with the events the chat has
   * not seen) and shows the session list only if that session is gone.
   *
   * @param message The bridge's explanation.
   * @param cause The bridge's `cause` (untrusted; missing from an older calu).
   * @returns A promise that resolves after the user's choice was handled.
   */
  async function onLinkLost(message: string, cause: unknown): Promise<void> {
    sessions.attachedId = null;
    const why = lostCause(cause, message);
    if (why === 'unreachable') {
      const status = await sessions.refreshStatus();
      void sessions.refresh();
      if (!serverStoppedAfterLoss(why, serverCheck(status))) {
        updateHints();
        return;
      }
      serverStopped = true;
      serverRetriedAt = null;
      updateHints();
      const choice = await vscode.window.showWarningMessage('calu: the calu server stopped; the session ended with it.', 'Start server', 'Retry');
      if (choice === 'Start server') sessions.startServer();
      else if (choice === 'Retry') await lookForServer(true);
      return;
    }
    updateHints();
    void sessions.refresh();
    const choice = await vscode.window.showWarningMessage(
      `calu: the connection to the session was lost${message ? `: ${message}` : '.'}`,
      'Attach again',
    );
    if (choice !== 'Attach again') return;
    const same = sessions.lastAttachedId !== null && chat.state.session.sessionId === sessions.lastAttachedId;
    if (!(await sessions.reattach(same ? chat.state.lastSeq : 0))) await sessions.pick();
  }

  /**
   * While "The calu server stopped" is shown: ask `ide.status` whether a server runs again; if
   * one runs, or (for Retry) a session can be attached anyway, attach as at the start
   * (`connectWorkspace`: the same session, else the workspace's) and drop the card.
   *
   * @param retry True for the Retry button: the attach is tried even if the status cannot
   *   tell, and a failed look is shown with its time on the card.
   * @returns A promise that resolves when the look is over (never rejects).
   */
  async function lookForServer(retry: boolean): Promise<void> {
    if (!serverStopped || lookingForServer) return;
    lookingForServer = true;
    try {
      const check = serverCheck(await sessions.refreshStatus());
      if (check === 'running' || (retry && check === 'unknown')) {
        await connectWorkspace(false);
        if (check === 'running' || sessions.attachedId !== null) {
          serverStopped = false;
          serverRetriedAt = null;
        }
      }
      if (serverStopped && retry) serverRetriedAt = new Date().toLocaleTimeString();
      updateHints();
    } catch (error) {
      output.appendLine(`[calu] looking for the calu server failed: ${errorText(error)}`);
    } finally {
      lookingForServer = false;
    }
  }

  // ---------------------------------------------------------------- settings of the editor

  /**
   * Read the display settings of the chat view.
   *
   * @returns The `calu.chat.*` settings.
   */
  function readChatSettings(): ChatSettings {
    const config = vscode.workspace.getConfiguration('calu');
    return {
      showThinking: config.get<boolean>('chat.showThinking', true) !== false,
      escalationSound: config.get<boolean>('chat.escalationSound', true) !== false,
    };
  }
  chat.setSettings(readChatSettings());

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration('calu.chat')) chat.setSettings(readChatSettings());
      if (event.affectsConfiguration('calu.server')) updateHints();
      if (event.affectsConfiguration('calu.command')) void bridge.restart().catch(() => {});
    }),
  );

  // ---------------------------------------------------------------- opening files and links

  /**
   * Open a file in an editor.
   *
   * @param path Absolute path on the workspace machine.
   * @param line 1-based line to put the cursor on (clamped to the file); default: unchanged.
   * @returns A promise that resolves when the editor shows the file.
   * @throws (rejects) Error if the file cannot be opened.
   */
  async function openFile(path: string, line?: number): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(path));
    const at = line !== undefined ? new vscode.Position(Math.min(line, doc.lineCount) - 1, 0) : undefined;
    await vscode.window.showTextDocument(doc, { preview: false, ...(at ? { selection: new vscode.Range(at, at) } : {}) });
  }

  /**
   * Open the diff editor of an edit. If the diff is no longer held (the store keeps the newest
   * ones), the file is opened at the edited line instead and a notice says so.
   *
   * @param toolUseId The edit's tool call.
   * @returns A promise that resolves when an editor is shown.
   * @throws (rejects) Error if neither the diff nor the file can be opened.
   */
  async function openDiffOrFile(toolUseId: string): Promise<void> {
    if (diffs.get(toolUseId)) {
      await diffs.open(toolUseId);
      return;
    }
    const folder = bridgeWorkspaceFolder();
    if (!folder) throw new Error('No workspace folder is open.');
    await runItemAction(own(chat.state.tools, toolUseId), 'openFileAtEdit', folder, diffs);
    void vscode.window.setStatusBarMessage('calu: the diff of this edit is no longer held; the file was opened instead.', 5000);
  }

  /**
   * Open a link from rendered Markdown: http(s) links in the browser, relative links as files
   * of the workspace folder (a trailing ":<line>" puts the cursor there); other schemes and
   * paths outside the folder are ignored.
   *
   * @param href The link target (untrusted).
   * @returns A promise that resolves when the link was handled (never rejects).
   */
  async function openLink(href: string): Promise<void> {
    try {
      if (/^https?:\/\//i.test(href)) {
        await vscode.env.openExternal(vscode.Uri.parse(href, true));
        return;
      }
      if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('#')) return;
      const folder = bridgeWorkspaceFolder();
      if (!folder) return;
      const target = decodeURIComponent(href.split(/[?#]/)[0]);
      const lined = /^(.*?):(\d+)(?::\d+)?$/.exec(target);
      const rel = normaliseRelativePath(lined ? lined[1] : target);
      await openFile(vscode.Uri.joinPath(folder.uri, ...rel.split('/')).fsPath, lined ? Math.max(1, Number(lined[2])) : undefined);
    } catch (error) {
      output.appendLine(`[calu] link not opened (${href}): ${errorText(error)}`);
    }
  }

  // ---------------------------------------------------------------- webview messages

  /**
   * Do what a validated message of the chat webview asks (`WEBVIEW_CONTRACT.md` lists every
   * message). Failures are shown in the chat; nothing is thrown.
   *
   * @param message The message.
   * @returns A promise that resolves when the message was handled.
   */
  async function handleWebviewMessage(message: WebviewMessage): Promise<void> {
    switch (message.type) {
      case 'ready':
      case 'stateReport':
        return; // handled by ChatViewProvider
      case 'send': {
        const params: ChatSendParams = {
          text: message.text,
          delivery: message.delivery,
          to: message.to,
          ...(message.images.length > 0
            ? { images: message.images.map((i) => ({ data: i.data, media_type: i.mediaType })) as ChatSendParams['images'] }
            : {}),
        };
        if (!(await run('Sending the message', () => bridge.session('chat.send', params)))) {
          chat.send({ type: 'restoreInput', text: message.text });
        }
        return;
      }
      case 'decide':
        await decide(message.escalationId, message.decision, message.note, message.answer);
        return;
      case 'why': {
        const escalation = own(chat.state.escalations, message.escalationId);
        if (escalation) await run('Opening the escalation context', () => contextDocs.showEscalation(escalation.escalationId));
        return;
      }
      case 'sideAsk': {
        const id = nextSideId++;
        const history = sideHistory(chat.state);
        dispatch({ method: 'ui.sideAsk', params: { id, question: message.question } });
        try {
          const result = await bridge.session('side.ask', {
            question: message.question,
            history: history as Parameters<typeof bridge.session<'side.ask'>>[1]['history'],
          });
          dispatch({ method: 'ui.sideAnswer', params: { id, answer: String(result.answer ?? '') } });
        } catch (error) {
          dispatch({ method: 'ui.sideAnswer', params: { id, error: errorText(error) } });
        }
        return;
      }
      case 'attach':
        await sessions.pick();
        return;
      case 'newSession':
        await sessions.newSession();
        return;
      case 'switchSession':
        if (chat.state.sessions.list.some((s) => s.id === message.sessionId)) await sessions.attach(message.sessionId);
        return;
      case 'renameSession': {
        const entry = chat.state.sessions.list.find((s) => s.id === message.sessionId);
        if (entry) await sessions.rename(entry.id, entry.name);
        return;
      }
      case 'refreshSessions':
        await bridge.ensureStarted().catch(() => {});
        await sessions.refresh(true);
        return;
      case 'detach':
        await sessions.detach();
        return;
      case 'startServer':
        sessions.startServer();
        return;
      case 'retryServer':
        await lookForServer(true);
        return;
      case 'copyServerCommand':
        await vscode.env.clipboard.writeText(sessions.serverHelp().command);
        void vscode.window.setStatusBarMessage('calu: server command copied.', 3000);
        return;
      case 'install':
        await installCalu();
        return;
      case 'restartBridge':
        await bridge.restart().catch(() => {});
        return;
      case 'openSettings':
        settingsPanel.show();
        return;
      case 'editChecker':
        await editChecker(message.name, message.route);
        return;
      case 'showContext':
        await run('Opening the checker context', () => contextDocs.show());
        return;
      case 'setModel': {
        const model = message.model === 'default' ? null : message.model;
        if (await run('Changing the model', () => bridge.session('model.set', { model }))) {
          dispatch({ method: 'ui.model', params: { model } });
        }
        return;
      }
      case 'setEffort':
        // The effort of a running session is fixed; the setting applies to later sessions.
        await run('Changing the effort', async () => {
          if (message.effort === 'default') await bridge.request('ide.settings.unset', { key: 'claude.effort', project: null });
          else await bridge.request('ide.settings.set', { key: 'claude.effort', value: message.effort, project: null });
          notice('info', `Effort "${message.effort}" is stored; it applies to sessions started from now on.`);
          void settingsPanel.reload();
        });
        return;
      case 'setLevel':
        await run('Changing the supervision level', () => bridge.session('auto_escalate.set', flagsForLevel(message.level)));
        return;
      case 'setPermissionMode':
        // The session answers every client with `permission_mode.changed`; the header follows it.
        await run("Switching Claude Code's permission mode", () => bridge.session('permission_mode.set', { mode: message.mode }));
        return;
      case 'refreshUsage':
        await refreshUsage();
        return;
      case 'usagePanel':
        await openUsagePanel();
        return;
      case 'interrupt':
        await run('Interrupting', () => bridge.session('interrupt', {}));
        return;
      case 'openFile':
        // Only paths that calu named as file links of a tool call are opened.
        if (isKnownFile(chat.state, message.path)) await run('Opening the file', () => openFile(message.path, message.line));
        return;
      case 'openToolFile': {
        const file = own(chat.state.tools, message.toolUseId)?.files?.[message.index];
        if (file) await run('Opening the file', () => openFile(file.path, message.line));
        return;
      }
      case 'openDiff':
        await run('Opening the diff', () => openDiffOrFile(message.toolUseId));
        return;
      case 'openLink':
        await openLink(message.href);
        return;
      case 'showLog':
        output.show(true);
        return;
      case 'unlock':
        await unlock();
        return;
      case 'rewindBefore':
      case 'forkBefore':
        await forkBefore(message.turn, message.uuid, message.type === 'rewindBefore');
        return;
      case 'forkConversation':
        await forkConversation();
        return;
      case 'cancelStart':
        await sessions.cancelStart();
        return;
      case 'setHonestyMode':
        await run('Changing the honesty mode', () => setHonestyMode(message.mode));
        return;
      case 'honestyReview':
        await run('The honesty review', async () => {
          const result = await bridge.session('honesty.review', {});
          // Say what happened: the chip alone changes quietly, or not at all.
          if (!result.ran) notice('warning', `No honesty review ran${result.problem ? `: ${result.problem}` : '.'}`);
          else notice('info', `The honesty review ran${result.problem ? `; ${result.problem}` : ' and judged every file'}. The honesty chip shows the state now.`);
        });
        return;
      case 'honestyTrust': {
        const choice = await vscode.window.showWarningMessage(
          'calu: trust the current git HEAD?',
          { modal: true, detail: 'The commit that HEAD names now is recorded as honest; it becomes the baseline of the honesty state.' },
          'Trust HEAD',
        );
        if (choice === 'Trust HEAD') {
          await run('Trusting HEAD', async () => {
            const result = await bridge.session('honesty.trust', { rev: 'HEAD' });
            const commit = result.commit_id ? `Commit ${result.commit_id.slice(0, 12)} (HEAD)` : 'HEAD';
            notice('info', result.recorded ? `${commit} is trusted now; it is the baseline of the honesty state.` : `${commit} was trusted already.`);
          });
        }
        return;
      }
      case 'honestyClear':
        await run('Clearing the mark', () => bridge.session('honesty.clear', { mark_id: message.markId }));
        return;
      case 'itemAction': {
        const folder = bridgeWorkspaceFolder();
        if (!folder) return;
        await run('The action', () => runItemAction(own(chat.state.tools, message.toolUseId), message.action, folder, diffs));
        return;
      }
    }
  }

  // ---------------------------------------------------------------- registrations

  /**
   * Register a command that reports a failure as an error message.
   *
   * @param id The command id from package.json.
   * @param handler The command's body.
   * @returns The registration.
   */
  const command = (id: string, handler: () => unknown): vscode.Disposable =>
    vscode.commands.registerCommand(id, async () => {
      try {
        await handler();
      } catch (error) {
        void vscode.window.showErrorMessage(`calu: ${errorText(error)}`);
      }
    });

  let ticks = 0;
  const timer = setInterval(() => {
    ticks += 1;
    if (chat.isVisible) void sessions.refresh();
    // A stopped server is looked for at every tick while the chat is visible, else at every
    // third: `ide.status` starts a few programs (claude --version, a bwrap test).
    if (serverStopped && (chat.isVisible || ticks % 3 === 0)) void lookForServer(false);
  }, SESSIONS_REFRESH_MS);

  context.subscriptions.push(
    output,
    bridge,
    contextDocs,
    settingsPanel,
    new vscode.Disposable(() => clearInterval(timer)),
    // The user's saves, for the recorded human diffs (D253); the session decides whether to record.
    watchSaves(bridge, () => isAttached(chat.state), (line) => output.appendLine(line)),
    vscode.window.registerWebviewViewProvider(CHAT_VIEW_ID, chat, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, diffs),
    vscode.workspace.registerTextDocumentContentProvider(CONTEXT_SCHEME, contextDocs),
    vscode.workspace.registerTextDocumentContentProvider(CHECKER_STORED_SCHEME, checkerFs),
    vscode.workspace.registerFileSystemProvider(CHECKER_SCHEME, checkerFs, { isCaseSensitive: true }),
    command('calu.openChat', () => chat.reveal()),
    command('calu.newSession', () => sessions.newSession()),
    command('calu.attach', () => sessions.pick()),
    command('calu.switchSession', () => sessions.pick()),
    command('calu.renameSession', () => {
      const entry = chat.state.sessions.list.find((s) => s.id === sessions.attachedId);
      return sessions.rename(sessions.attachedId, entry?.name ?? '');
    }),
    command('calu.detach', () => sessions.detach()),
    command('calu.forkConversation', () => forkConversation()),
    command('calu.unlock', () => unlock()),
    command('calu.interrupt', () => bridge.session('interrupt', {})),
    command('calu.addComment', () => addCommentCommand(bridge, inputBoxProvider)),
    command('calu.editCheckerInstructions', () => editChecker()),
    command('calu.showCheckerContext', () => contextDocs.show()),
    command('calu.openSettings', () => settingsPanel.show()),
    command('calu.startServer', () => {
      sessions.startServer();
    }),
    command('calu.showServerHelp', () => sessions.showServerHelp('How to start the calu server.')),
    command('calu.showRootInstall', () => installer.showRootInstall()),
    command('calu.restartBridge', () => bridge.restart()),
    command('calu.showLog', () => output.show(true)),
    command('calu.install', () => installCalu()),
    command('calu.update', () => updateCalu()),
    command('calu.checkSetup', () => checkSetup()),
  );

  // Start the bridge in the background; a missing calu shows the Install button in the chat.
  if (bridgeWorkspaceFolder()) void bridge.ensureStarted().catch(() => {});
  updateHints();

  return { bridge, chat, sessions, handleWebviewMessage };
}

/**
 * Deactivate the extension. The parts are disposed through `context.subscriptions`; the bridge
 * process is ended there, and the sessions keep running in the calu server.
 *
 * @returns Nothing.
 */
export function deactivate(): void {}
