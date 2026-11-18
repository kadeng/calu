/**
 * Installs and updates calu on the machine the extension runs on (the remote host, WSL or dev
 * container), with uv. Nothing is installed without the user's answer to a modal dialog; the
 * commands themselves are built in `shared/bootstrap.ts`.
 *
 *     Installer.install()
 *       -> modal: install calu?                            (No -> false)
 *       -> uv missing? modal: install uv from astral.sh?   (No -> false)
 *            sh -c "<curl|wget installer> | sh"
 *       -> uv tool install --force --python 3.14 <calu.install.source>
 *     Installer.update()
 *       -> uv tool upgrade calu
 *     Installer.checkClaude()
 *       -> looks for `claude`, runs `claude --version`; reports, never installs
 *
 * Process output goes to the "calu" output channel; a progress notification shows the running
 * step. The root installation that `calu serve --sudo` needs is only shown as a command
 * (`showRootInstall`): it needs sudo and is never run by the extension.
 */

import { execFile, spawn } from 'node:child_process';
import * as vscode from 'vscode';
import { hostFacts } from './bridge';
import { findExecutable } from './caluCommand';
import {
  CLAUDE_DOCS_URL,
  caluInstallArgv,
  caluUpgradeArgv,
  claudeCheck,
  findUv,
  installSource,
  uvInstallCommand,
  UV_INSTALL_URL,
  type ClaudeCheck,
} from './shared/bootstrap';
import { rootInstallCommand } from './shared/serverCommand';

/** Installs calu with uv after asking the user (see the module comment). */
export class Installer {
  private running: Promise<boolean> | undefined;

  /**
   * @param output Receives the output of the install processes.
   */
  constructor(private readonly output: vscode.OutputChannel) {}

  /**
   * Ask the user (modal) and, on "Install", install calu (and uv first if it is missing).
   * Concurrent calls share one run, so the user is asked once.
   *
   * @returns True if calu was installed; false if the user declined or a step failed (an error
   *   message names the step and points to the log).
   */
  install(): Promise<boolean> {
    this.running ??= this.installOnce().finally(() => {
      this.running = undefined;
    });
    return this.running;
  }

  /**
   * Update a calu that was installed as a uv tool (`uv tool upgrade calu`).
   *
   * @returns True if the update ran without error; false if uv is missing or the step failed
   *   (an error message says which).
   */
  async update(): Promise<boolean> {
    const uv = findUv(hostFacts());
    if (!uv) return this.failed('uv was not found; calu was not installed with uv on this machine.');
    if (!(await this.step('Updating calu', caluUpgradeArgv(uv)))) return false;
    void vscode.window.showInformationMessage('calu: updated. Restart the bridge ("calu: Restart Bridge") to use the new version.');
    return true;
  }

  /**
   * Look for Claude Code and ask it for its version. Nothing is installed.
   *
   * @returns What was found (see `claudeCheck`); never throws.
   */
  async checkClaude(): Promise<ClaudeCheck> {
    const found = findExecutable('claude', hostFacts());
    if (!found) return claudeCheck(undefined, '');
    const version = await new Promise<string>((resolve) => {
      execFile(found.path, ['--version'], { timeout: 15000 }, (error, stdout) => resolve(error ? '' : String(stdout)));
    });
    return claudeCheck(found.path, version);
  }

  /**
   * Tell the user that Claude Code is missing and where its install instructions are.
   *
   * @param check The result of `checkClaude` with `found: false`.
   * @returns A promise that resolves after the user's choice was handled.
   */
  async reportClaudeMissing(check: Extract<ClaudeCheck, { found: false }>): Promise<void> {
    const choice = await vscode.window.showWarningMessage(`calu: ${check.message}`, 'Open Instructions');
    if (choice === 'Open Instructions') await vscode.env.openExternal(vscode.Uri.parse(CLAUDE_DOCS_URL, true));
  }

  /**
   * Show the command of the root installation that `calu serve --sudo` needs, with a button
   * that copies it. The extension does not run it.
   *
   * @returns A promise that resolves after the user's choice was handled.
   */
  async showRootInstall(): Promise<void> {
    const command = rootInstallCommand(this.source());
    const choice = await vscode.window.showInformationMessage(
      '`calu serve --sudo` needs calu installed for root. Run this command in a terminal (it asks for your sudo password):',
      { modal: true, detail: command },
      'Copy Command',
    );
    if (choice === 'Copy Command') await vscode.env.clipboard.writeText(command);
  }

  /**
   * Read the install source from the `calu.install.source` setting.
   *
   * @returns The source (see `installSource`).
   * @throws Error if the setting starts with "-".
   */
  source(): string {
    return installSource(vscode.workspace.getConfiguration('calu').get('install.source'));
  }

  /**
   * Body of `install`.
   *
   * @returns See `install`.
   */
  private async installOnce(): Promise<boolean> {
    let source: string;
    try {
      source = this.source();
    } catch (error) {
      return this.failed(error instanceof Error ? error.message : String(error));
    }
    const where = vscode.env.remoteName ? `on this ${vscode.env.remoteName} machine` : 'on this machine';
    const detail =
      `uv installs calu as a tool (\`uv tool install --python 3.14 ${source}\`), with Python 3.14 and its ` +
      'dependencies from PyPI (internet access needed). The command `calu` goes to ~/.local/bin.';
    const answer = await vscode.window.showInformationMessage(`Install calu ${where}?`, { modal: true, detail }, 'Install');
    if (answer !== 'Install') return false;
    let uv = findUv(hostFacts());
    if (!uv) {
      const uvAnswer = await vscode.window.showWarningMessage(
        'uv is needed to install calu, but it was not found. Install uv now?',
        {
          modal: true,
          detail:
            `This downloads and runs the official uv installer from ${UV_INSTALL_URL} ` +
            `(\`curl -LsSf ${UV_INSTALL_URL} | sh\`, or wget without curl). It puts uv into ~/.local/bin and ` +
            'does not change your shell start-up files.',
        },
        'Install uv',
      );
      if (uvAnswer !== 'Install uv') return false;
      if (!(await this.step('Installing uv', ['sh', '-c', uvInstallCommand()]))) return false;
      uv = findUv(hostFacts());
      if (!uv) return this.failed('uv was installed, but cannot be found in ~/.local/bin.');
    }
    if (!(await this.step('Installing calu', caluInstallArgv(uv, source)))) return false;
    void vscode.window.showInformationMessage('calu: installed. Next: run `calu setup` once in a terminal if this machine has no calu database yet.');
    return true;
  }

  /**
   * Run one install step with a progress notification; its output goes to the output channel.
   *
   * @param title Shown in the notification and the log, e.g. "Installing uv".
   * @param argv The command.
   * @returns True if it exited with code 0 (otherwise an error message is shown).
   */
  private async step(title: string, argv: string[]): Promise<boolean> {
    this.output.appendLine(`[calu] ${title}: ${argv.join(' ')}`);
    const code = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `calu: ${title}…` },
      () =>
        new Promise<number>((resolve) => {
          const proc = spawn(argv[0], argv.slice(1), { stdio: ['ignore', 'pipe', 'pipe'], env: process.env });
          proc.stdout.on('data', (chunk: Buffer) => this.output.append(chunk.toString()));
          proc.stderr.on('data', (chunk: Buffer) => this.output.append(chunk.toString()));
          proc.on('error', (error) => {
            this.output.appendLine(`[calu] ${title} failed to start: ${error.message}`);
            resolve(-1);
          });
          proc.on('close', (exit) => resolve(exit ?? -1));
        }),
    );
    if (code === 0) return true;
    return this.failed(`${title} failed (exit code ${code}).`);
  }

  /**
   * Show an error with a button that opens the log.
   *
   * @param message What failed.
   * @returns False (for `return this.failed(...)`).
   */
  private failed(message: string): boolean {
    this.output.appendLine(`[calu] ${message}`);
    void vscode.window.showErrorMessage(`calu: ${message}`, 'Show Log').then((choice) => {
      if (choice === 'Show Log') this.output.show(true);
    });
    return false;
  }
}
