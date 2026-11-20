/**
 * The settings dialog "calu Settings": a WebviewPanel in the editor area that edits calu's own
 * settings (stored in calu's database) through the bridge. This class only relays; the form is
 * rendered by `webview/settings.ts` from `shared/settingsModel.ts`.
 *
 *     webview message (validated by parseSettingsMessage)      host action
 *     load {project}            ──► ide.settings.get + ide.models.presets + ide.status ──► data
 *     set {key, value, project} ──► ide.settings.set    ──► saved {key, value} | error {key, message}
 *     unset {key, project}      ──► ide.settings.unset  ──► saved {key, unset} + data | error
 *     setAllModels / setRoleModel ► ide.models.set_all / ide.models.set ──► data | error {key: "models"}
 *     editChecker {name}        ──► opens calu-checker:/<name>.md
 *     runStatus                 ──► ide.status          ──► status | error {key: "status"}
 *     fix {action}              ──► the `fix` callback (install, update, terminal, clipboard, browser)
 *
 * Requests that change settings may need the `admin` credential's password; `Bridge.request`
 * asks for it and does not keep it.
 */

import * as vscode from 'vscode';
import { errorText, type Bridge } from './bridge';
import type { IdeModelsResult, IdeSettingsResult, IdeStatusResult } from './bridgeProtocol';
import { renderHtml } from './chatView';
import type { ServerHelp } from './shared/serverCommand';
import { errorKey, parseSettingsMessage, type SettingsHostMessage, type SettingsWebviewMessage } from './shared/settingsMessages';
import { enableOffer } from './shared/humanDiffs';
import type { FixAction } from './shared/settingsModel';

/** View type of the panel. */
export const SETTINGS_VIEW_TYPE = 'calu.settings';

/** What the panel needs from the rest of the extension. */
export interface SettingsPanelHost {
  /** Open a checker's instructions in an editor. */
  editChecker(name: string): Promise<void>;
  /** Run a fix of the Setup card; resolves when it is done. */
  fix(action: FixAction): Promise<void>;
  /** The server command for the Setup card. */
  serverHelp(): ServerHelp;
  /** The root installation command for the Setup card (shown, never run). */
  rootInstallCommand(): string;
  /** Called with every fresh `ide.status` answer, so other views use it too. */
  statusLoaded(status: IdeStatusResult): void;
}

/** Owner of the settings panel; at most one panel exists. */
export class SettingsPanel implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  /** The scope the webview last loaded (`null` = global). */
  private project: string | null = null;

  /**
   * @param extensionUri Root of the extension (for `media/` and `dist/`).
   * @param bridge The bridge (started on demand).
   * @param host Actions outside the panel.
   */
  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly bridge: Bridge,
    private readonly host: SettingsPanelHost,
  ) {}

  /** True while the panel exists (it may be hidden behind another tab). */
  get isOpen(): boolean {
    return this.panel !== undefined;
  }

  /**
   * Open the panel, or bring the existing one to the front.
   *
   * @returns Nothing.
   */
  show(): void {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    const media = vscode.Uri.joinPath(this.extensionUri, 'media');
    const dist = vscode.Uri.joinPath(this.extensionUri, 'dist');
    const panel = vscode.window.createWebviewPanel(SETTINGS_VIEW_TYPE, 'calu Settings', vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media, dist],
    });
    panel.iconPath = vscode.Uri.joinPath(media, 'icon.svg');
    panel.webview.html = renderHtml(panel.webview, this.extensionUri, 'settings.js', ['settings.css'], 'calu Settings');
    panel.webview.onDidReceiveMessage((raw: unknown) => void this.receive(raw));
    panel.onDidDispose(() => {
      if (this.panel === panel) this.panel = undefined;
    });
    this.panel = panel;
  }

  /**
   * Load everything again and send it to the webview (after a change made elsewhere, e.g. a
   * saved checker or a finished installation). Does nothing if the panel is closed.
   *
   * @returns A promise that resolves when the data was sent (never rejects).
   */
  async reload(): Promise<void> {
    if (this.panel) await this.sendData(this.project);
  }

  /**
   * Close the panel.
   *
   * @returns Nothing.
   */
  dispose(): void {
    this.panel?.dispose();
    this.panel = undefined;
  }

  /**
   * Handle a raw message from the webview (see the module comment). Errors of the bridge are
   * answered with an `error` message; nothing is thrown.
   *
   * @param raw Any value; invalid messages are dropped.
   * @returns A promise that resolves when the message was handled.
   */
  private async receive(raw: unknown): Promise<void> {
    const message = parseSettingsMessage(raw);
    if (!message) return;
    try {
      await this.handle(message);
    } catch (error) {
      this.post({ type: 'error', key: errorKey(message), message: errorText(error) });
    }
  }

  /**
   * Do what a validated message asks.
   *
   * @param message The message.
   * @returns A promise that resolves when the answer was posted.
   * @throws (rejects) Error of the bridge request; `receive` turns it into an `error` message.
   */
  private async handle(message: SettingsWebviewMessage): Promise<void> {
    switch (message.type) {
      case 'load':
        this.project = message.project;
        await this.sendData(message.project);
        return;
      case 'set': {
        await this.bridge.ensureStarted();
        const result = await this.bridge.request('ide.settings.set', {
          key: message.key,
          value: message.value,
          project: message.project,
        });
        this.post({ type: 'saved', key: message.key, value: result.value });
        await this.offerRelated(result, message.project);
        return;
      }
      case 'unset':
        await this.bridge.ensureStarted();
        await this.bridge.request('ide.settings.unset', { key: message.key, project: message.project });
        this.post({ type: 'saved', key: message.key, unset: true });
        await this.sendData(message.project);
        return;
      case 'setAllModels':
        await this.busy('Setting the model of every role', async () => {
          await this.bridge.ensureStarted();
          await this.bridge.request('ide.models.set_all', {
            model: message.model,
            ...(message.effort !== undefined ? { effort: message.effort } : {}),
            project: message.project,
          });
        });
        await this.sendData(message.project);
        return;
      case 'setRoleModel':
        await this.bridge.ensureStarted();
        await this.bridge.request('ide.models.set', { role: message.role, model: message.model, project: message.project });
        await this.sendData(message.project);
        return;
      case 'editChecker':
        await this.host.editChecker(message.name);
        return;
      case 'runStatus': {
        await this.bridge.ensureStarted();
        const status = await this.busy('Checking the setup', () => this.bridge.request('ide.status', {}));
        this.host.statusLoaded(status);
        this.post({ type: 'status', status });
        return;
      }
      case 'fix':
        await this.host.fix(message.action);
        await this.sendData(this.project);
        return;
    }
  }

  /**
   * Ask whether to switch a related setting on as well, if the answer of a set offers one
   * (`offer`, D253), and set it on a yes. Nothing is switched without the user's yes.
   *
   * @param result The answer of `ide.settings.set`.
   * @param project The scope of the set.
   * @returns A promise that resolves when the question is answered and the value stored.
   * @throws (rejects) Error of the bridge request for the related setting.
   */
  private async offerRelated(result: unknown, project: string | null): Promise<void> {
    const offer = enableOffer(result);
    if (!offer) return;
    const yes = `Switch on ${offer.key}`;
    const answer = await vscode.window.showInformationMessage(offer.text, { modal: true }, yes);
    if (answer !== yes) return;
    const stored = await this.bridge.request('ide.settings.set', { key: offer.key, value: true, project });
    this.post({ type: 'saved', key: offer.key, value: stored.value });
    await this.sendData(project);
  }

  /**
   * Load settings, model presets and status, and post one `data` message. A request that fails
   * leaves its part `null` and adds a line to `problems`; the others are still shown.
   *
   * @param project The scope (`null` = global).
   * @returns A promise that resolves when the message was posted (never rejects).
   */
  private async sendData(project: string | null): Promise<void> {
    const problems: string[] = [];
    /**
     * Run one request and note its failure.
     *
     * @param what Name of the part for the problem line.
     * @param request The request.
     * @returns The result, or null if it failed.
     */
    const part = async <T>(what: string, request: () => Promise<T>): Promise<T | null> => {
      try {
        return await request();
      } catch (error) {
        problems.push(`${what}: ${errorText(error)}`);
        return null;
      }
    };
    let settings: IdeSettingsResult | null = null;
    let presets: IdeModelsResult | null = null;
    let status: IdeStatusResult | null = null;
    const started = await part('calu', () => this.bridge.ensureStarted());
    if (started) {
      // One after the other: a password prompt of the first must not overlap one of the second.
      settings = await part('Settings', () => this.bridge.request('ide.settings.get', { project }));
      presets = await part('Models', () => this.bridge.request('ide.models.presets', { project }));
      status = await part('Setup', () => this.bridge.request('ide.status', {}));
      if (status) this.host.statusLoaded(status);
    }
    this.post({
      type: 'data',
      project,
      settings,
      presets,
      status,
      serverHelp: this.host.serverHelp(),
      rootInstallCommand: this.host.rootInstallCommand(),
      problems,
    });
  }

  /**
   * Run a long request between two `busy` messages.
   *
   * @param what Text for the webview's progress line.
   * @param work The request.
   * @returns What `work` returned.
   * @throws (rejects) Whatever `work` throws; the closing `busy` message is posted first.
   */
  private async busy<T>(what: string, work: () => Promise<T>): Promise<T> {
    this.post({ type: 'busy', busy: true, what });
    try {
      return await work();
    } finally {
      this.post({ type: 'busy', busy: false, what });
    }
  }

  /**
   * Send a message to the webview if the panel exists; drop it otherwise.
   *
   * @param message The message.
   * @returns Nothing.
   */
  private post(message: SettingsHostMessage): void {
    void this.panel?.webview.postMessage(message);
  }
}
