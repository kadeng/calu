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
import { runItemAction } from './itemActions';
import type { ChatSendParams, EditDiff } from './protocol.generated';
import { Sessions } from './sessions';
import { SettingsPanel } from './settingsPanel';
import { CLAUDE_DOCS_URL } from './shared/bootstrap';
import { buildHints } from './shared/hints';
import { flagsForLevel } from './shared/levels';
import type { ChatSettings, WebviewMessage } from './shared/messages';
import { rootInstallCommand } from './shared/serverCommand';
import type { FixAction } from './shared/settingsModel';
import { isAttached, isKnownFile, own, sideHistory, type ViewEvent } from './shared/viewState';

/** Milliseconds between two refreshes of the session list while the chat view is visible. */
const SESSIONS_REFRESH_MS = 10_000;

/** What `activate` returns (used by integration tests). */
export interface CaluApi {
  bridge: Bridge;
  chat: ChatViewProvider;
  sessions: Sessions;
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
  let lastHints = '';
  let lastServerHelp = '';
  /** True once the install offer for a missing calu was shown in this activation. */
  let installOffered = false;
  /** True once the password prompt was opened by itself for the running bridge process. */
  let askedAtStart = false;

  // ---------------------------------------------------------------- chat view and sessions

  const chat = new ChatViewProvider(
    context.extensionUri,
    (message) => void handleWebviewMessage(message),
    () => void bridge.ensureStarted().catch(() => {}),
    (visible) => {
      if (visible) void sessions.refresh();
    },
  );

  /**
   * Apply a view event to the chat view.
   *
   * @param event The event.
   * @returns Nothing.
   */
  const dispatch = (event: ViewEvent): void => chat.dispatch(event);

  const sessions: Sessions = new Sessions(bridge, dispatch, onAttached, updateHints, output);

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
   * rebuild the chat view, reload the context document and ask for the usage.
   *
   * @param result The result.
   * @returns Nothing.
   */
  function onAttached(result: IdeAttachResult): void {
    for (const event of result.events ?? []) {
      if (event?.method === 'edit.diff') diffs.record(event.params as unknown as EditDiff);
    }
    dispatch({ method: 'ui.attached', params: result as unknown as Record<string, unknown> });
    contextDocs.refresh();
    void refreshUsage();
    void chat.reveal();
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
   * Ask the attached session for its usage and put the answer into the chat view. Failures
   * are logged only.
   *
   * @returns A promise that resolves when the answer was applied.
   */
  async function refreshUsage(): Promise<void> {
    if (!isAttached(chat.state)) return;
    try {
      const usage = await bridge.session('usage.get', {});
      dispatch({ method: 'ui.usage', params: usage as unknown as Record<string, unknown> });
    } catch (error) {
      output.appendLine(`[calu] usage.get: ${errorText(error)}`);
    }
  }

  /**
   * After the bridge started or was unlocked: check the setup, attach to the session of the
   * workspace if one runs, and load the session list. If the bridge reports that it is locked
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
    await sessions.attach(null, true);
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
      output.appendLine(`[calu] session.history: ${errorText(error)}`);
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
    const started = await sessions.forkBefore(conversation, number, rewind ? 'Rewinding the conversation' : 'Forking the conversation');
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
        output.appendLine(`[calu] checkers.get: ${errorText(error)}`);
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
        if (notification.params.state === 'lost') void onLinkLost(String(notification.params.message ?? ''));
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
  }

  /**
   * React to a lost connection to the session: offer to attach again.
   *
   * @param message The bridge's explanation.
   * @returns A promise that resolves after the user's choice was handled.
   */
  async function onLinkLost(message: string): Promise<void> {
    sessions.attachedId = null;
    updateHints();
    void sessions.refresh();
    const choice = await vscode.window.showWarningMessage(
      `calu: the connection to the session was lost${message ? `: ${message}` : '.'}`,
      'Attach again',
    );
    if (choice === 'Attach again') await sessions.pick();
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
      case 'refreshUsage':
        await refreshUsage();
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
      case 'setHonestyMode':
        await run('Changing the honesty mode', async () => {
          await bridge.request('ide.settings.set', { key: 'honesty.mode', value: message.mode, project: null });
          notice('info', `Honesty mode "${message.mode}" is stored in calu's settings.`);
          void settingsPanel.reload();
        });
        return;
      case 'honestyReview':
        await run('The honesty review', async () => {
          const result = await bridge.session('honesty.review', {});
          if (!result.ran) notice('warning', `No honesty review ran${result.problem ? `: ${result.problem}` : '.'}`);
        });
        return;
      case 'honestyTrust': {
        const choice = await vscode.window.showWarningMessage(
          'calu: trust the current git HEAD?',
          { modal: true, detail: 'The commit that HEAD names now is recorded as honest; it becomes the baseline of the honesty state.' },
          'Trust HEAD',
        );
        if (choice === 'Trust HEAD') await run('Trusting HEAD', () => bridge.session('honesty.trust', { rev: 'HEAD' }));
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

  const timer = setInterval(() => {
    if (chat.isVisible) void sessions.refresh();
  }, SESSIONS_REFRESH_MS);

  context.subscriptions.push(
    output,
    bridge,
    contextDocs,
    settingsPanel,
    new vscode.Disposable(() => clearInterval(timer)),
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

  return { bridge, chat, sessions };
}

/**
 * Deactivate the extension. The parts are disposed through `context.subscriptions`; the bridge
 * process is ended there, and the sessions keep running in the calu server.
 *
 * @returns Nothing.
 */
export function deactivate(): void {}
