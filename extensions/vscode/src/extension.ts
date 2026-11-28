/**
 * Entry point of the calu extension: creates the parts and wires them together.
 *
 *     activate
 *       ├─ Bridge (calu ide)       host/bridgeCore.ts + a child process (bridge.ts)
 *       ├─ ChatHost                host/chatHost.ts: what the chat's messages and the bridge's
 *       │                          events do (shared with the browser pane of `calu web`)
 *       │    ├─ ChatViewProvider   the sidebar view calu.chat (webview)
 *       │    ├─ HostUi             VSCode's dialogs, editors, diff views, settings panel (below)
 *       │    └─ Sessions           host/sessions.ts with VSCode's QuickPick and terminal
 *       ├─ SettingsPanel, CheckerFileSystem, ContextDocuments, DiffStore, Installer
 *       └─ commands calu.* (package.json)
 *
 * The extension starts these processes:
 *   - `calu ide`, as a child process (the bridge).
 *   - `calu serve`, only when the user chooses to start the server; the command is typed into
 *     an integrated terminal.
 *   - The uv installer and `uv tool install`, only after the user confirmed them in a modal.
 * Sessions run in `calu serve`; they keep running when the editor closes.
 */

import * as vscode from 'vscode';
import { Installer } from './bootstrap';
import { Bridge, bridgeWorkspaceFolder, errorText } from './bridge';
import type { AnyChecker, IdeStatusResult } from './bridgeProtocol';
import { CHAT_VIEW_ID, ChatViewProvider } from './chatView';
import { CHECKER_SCHEME, CHECKER_STORED_SCHEME, CheckerFileSystem, pickChecker } from './checkerFs';
import { addCommentCommand, inputBoxProvider } from './comment';
import { normaliseRelativePath } from './commentPayload';
import { CONTEXT_SCHEME, ContextDocuments } from './contextDoc';
import { DIFF_SCHEME, DiffStore } from './diffView';
import { ChatHost, type HostUi } from './host/chatHost';
import type { Sessions } from './host/sessions';
import { watchSaves } from './humanDiffs';
import { runItemAction } from './itemActions';
import type { EditDiff } from './protocol.generated';
import { vscodeInput, vscodeSessionsUi } from './sessions';
import { SettingsPanel } from './settingsPanel';
import { CLAUDE_DOCS_URL } from './shared/bootstrap';
import { statusBarEntry } from './shared/chatModel';
import type { ChatSettings, WebviewMessage } from './shared/messages';
import { rootInstallCommand } from './shared/serverCommand';
import type { FixAction } from './shared/settingsModel';
import { isSourceRef, splitLine } from './shared/sourceRefs';
import { isAttached, isKnownFile, own } from './shared/viewState';

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
  // The calu user password (spec sections 8 and 10): kept in this variable until the window
  // closes, so that a restarted bridge is unlocked again without asking. The password is never
  // logged and is sent only with `ide.unlock`.
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

  // ---------------------------------------------------------------- chat view

  // `host` is created below; the view only calls back after activation has finished.
  // eslint-disable-next-line prefer-const
  let host: ChatHost;
  const chat = new ChatViewProvider(
    context.extensionUri,
    (message) => void host.handle(message),
    () => void bridge.ensureStarted().catch(() => {}),
    (visible) => {
      if (visible) void host.sessions.refresh();
    },
  );

  // The "calu" item of the status bar: always visible, also while the view is hidden or folded
  // away behind the secondary side bar's "…". Clicking it opens the calu chat, which a new
  // profile does not show at first.
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
        host.sessions.startServer();
        return;
      case 'copyServerCommand':
        await vscode.env.clipboard.writeText(host.sessions.serverHelp().command);
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
    serverHelp: () => host.sessions.serverHelp(),
    rootInstallCommand: () => {
      try {
        return rootInstallCommand(installer.source());
      } catch {
        return '';
      }
    },
    statusLoaded: (status: IdeStatusResult) => {
      host.sessions.status = status;
      host.updateHints();
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
    const status = await host.sessions.refreshStatus();
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
      if (event.affectsConfiguration('calu.server')) host.updateHints();
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
   * of the workspace folder (a trailing ":<line>", "#L<line>" or "#<line>" puts the cursor
   * there, `splitLine`); other schemes and paths outside the folder are ignored.
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
      if ((/^[a-z][a-z0-9+.-]*:/i.test(href) && !isSourceRef(href)) || href.startsWith('#')) return;
      const folder = bridgeWorkspaceFolder();
      if (!folder) return;
      const { path, line } = splitLine(decodeURIComponent(href.split('?')[0]));
      const rel = normaliseRelativePath(path.split('#')[0]);
      await openFile(vscode.Uri.joinPath(folder.uri, ...rel.split('/')).fsPath, line);
    } catch (error) {
      output.appendLine(`[calu] link not opened (${href}): ${errorText(error)}`);
    }
  }

  // ---------------------------------------------------------------- the platform of the host

  /**
   * Handle the chat messages that only VSCode can serve (`HostUi.handle`).
   *
   * @param message The message.
   * @returns A promise that resolves when the message was handled.
   * @throws (rejects) Errors of opening a file or a diff; the host shows them as a notice.
   */
  async function handleVscodeMessage(message: WebviewMessage): Promise<void> {
    switch (message.type) {
      case 'why': {
        const escalation = own(chat.state.escalations, message.escalationId);
        if (escalation) await contextDocs.showEscalation(escalation.escalationId);
        return;
      }
      case 'copyServerCommand':
        await vscode.env.clipboard.writeText(host.sessions.serverHelp().command);
        void vscode.window.setStatusBarMessage('calu: server command copied.', 3000);
        return;
      case 'install':
        await installCalu();
        return;
      case 'openSettings':
        settingsPanel.show();
        return;
      case 'editChecker':
        await editChecker(message.name, message.route);
        return;
      case 'showContext':
        await contextDocs.show();
        return;
      case 'openFile':
        // Only paths that calu named as file links of a tool call are opened.
        if (isKnownFile(chat.state, message.path)) await openFile(message.path, message.line);
        return;
      case 'openToolFile': {
        const file = own(chat.state.tools, message.toolUseId)?.files?.[message.index];
        if (file) await openFile(file.path, message.line);
        return;
      }
      case 'openDiff':
        await openDiffOrFile(message.toolUseId);
        return;
      case 'openLink':
        await openLink(message.href);
        return;
      case 'showLog':
        output.show(true);
        return;
      case 'itemAction': {
        const folder = bridgeWorkspaceFolder();
        if (folder) await runItemAction(own(chat.state.tools, message.toolUseId), message.action, folder, diffs);
        return;
      }
      default:
        return;
    }
  }

  const ui: HostUi = {
    log: (line) => output.appendLine(line),
    confirm: async (question, detail, button) =>
      (await vscode.window.showWarningMessage(question, { modal: true, detail }, button)) === button,
    input: vscodeInput,
    notify: async (level, text, ...buttons) => {
      const show =
        level === 'error' ? vscode.window.showErrorMessage : level === 'warning' ? vscode.window.showWarningMessage : vscode.window.showInformationMessage;
      return show(text, ...buttons);
    },
    flash: (text) => void vscode.window.setStatusBarMessage(text, 3000),
    focused: () => vscode.window.state.focused,
    notificationMode: () => {
      const mode = vscode.workspace.getConfiguration('calu').get<string>('notifications', 'decisions');
      return mode === 'all' || mode === 'off' ? mode : 'decisions';
    },
    activeFile: () => {
      const active = vscode.window.activeTextEditor?.document.uri;
      return active?.scheme === 'file' ? vscode.workspace.asRelativePath(active, false) : '';
    },
    settingsChanged: () => void settingsPanel.reload(),
    offerInstall: () =>
      void vscode.window
        .showInformationMessage('calu is not installed on this machine.', 'Install calu')
        .then((choice) => (choice === 'Install calu' ? installCalu() : undefined)),
    sessionEvent: (method, params) => {
      if (method === 'edit.diff') diffs.record(params as unknown as EditDiff);
      contextDocs.onEvent(method);
    },
    attached: (result) => {
      for (const event of result.events ?? []) {
        if (event?.method === 'edit.diff') diffs.record(event.params as unknown as EditDiff);
      }
      // Refreshed after the view took the attach over (the document reads the session).
      queueMicrotask(() => contextDocs.refresh());
    },
    handle: handleVscodeMessage,
  };

  host = new ChatHost(
    bridge,
    chat,
    ui,
    vscodeSessionsUi(output),
    (id) => void context.workspaceState.update(LAST_SESSION_KEY, id ?? undefined),
    // A reloaded window (or a restarted VSCode) attaches to the session this workspace showed
    // last, if it still runs; otherwise to the session of the workspace, as at a first start.
    context.workspaceState.get<string>(LAST_SESSION_KEY) || undefined,
  );
  const sessions = host.sessions;

  // ---------------------------------------------------------------- registrations

  /**
   * Register a command that reports a failure as an error message.
   *
   * @param id The command id from package.json.
   * @param handler The command's body; it gets the file a context menu was opened on, if any.
   * @returns The registration.
   */
  const command = (id: string, handler: (uri?: vscode.Uri) => unknown): vscode.Disposable =>
    vscode.commands.registerCommand(id, async (arg?: unknown) => {
      try {
        // A context menu passes the file it was opened on; a palette or a key binding nothing.
        await handler(arg instanceof vscode.Uri ? arg : undefined);
      } catch (error) {
        void vscode.window.showErrorMessage(`calu: ${errorText(error)}`);
      }
    });

  context.subscriptions.push(
    output,
    bridge,
    host,
    contextDocs,
    settingsPanel,
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
    command('calu.forkConversation', () => host.forkConversation()),
    command('calu.honestyTrustCommit', () => host.trustHonesty({ type: 'honestyTrust', ask: 'rev' })),
    command('calu.honestyReview', () => host.handle({ type: 'honestyReview' })),
    // From the explorer's or the editor's context menu the file comes as an argument.
    command('calu.honestyTrustFile', (uri) => {
      const path = uri?.scheme === 'file' ? vscode.workspace.asRelativePath(uri, false) : undefined;
      return host.trustHonesty(path ? { type: 'honestyTrust', path } : { type: 'honestyTrust', ask: 'file' });
    }),
    command('calu.unlock', () => host.unlock()),
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

  host.start();
  // Start the bridge in the background; a missing calu shows the Install button in the chat.
  if (bridgeWorkspaceFolder()) void bridge.ensureStarted().catch(() => {});

  return { bridge, chat, sessions, handleWebviewMessage: (message) => host.handle(message) };
}

/**
 * Deactivate the extension. The parts are disposed through `context.subscriptions`; the bridge
 * process is ended there, and the sessions keep running in the calu server.
 *
 * @returns Nothing.
 */
export function deactivate(): void {}
