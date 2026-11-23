/**
 * VSCode's dialogs for the session commands of `host/sessions.ts` (`vscodeSessionsUi`):
 *
 *       pickSession     QuickPick (a stored conversation has a "fork" button)
 *       input           showInputBox
 *       error / info    showErrorMessage / showInformationMessage
 *       serverHelp      the `calu.server.*` settings and what is installed (`shared/serverCommand.ts`)
 *       startServer     integrated terminal running the server command (tmux, else screen, else
 *                       plain); the user types passwords there
 *       showServerHelp  modal message with "Open terminal with this command" / "Copy command"
 *       progress        the window's progress entry in the status bar
 */

import * as vscode from 'vscode';
import { bridgeWorkspaceFolder, hostFacts, resolveCaluCommandForHost } from './bridge';
import type { IdeStatusResult } from './bridgeProtocol';
import { findExecutable } from './caluCommand';
import type { InputOptions, SessionsUi } from './host/sessions';
import { findingOk } from './shared/hints';
import { buildServerHelp, type ServerHelp } from './shared/serverCommand';
import type { SessionChoice, SessionPick } from './shared/sessionPick';

export { SLOW_START_MS, Sessions } from './host/sessions';

/** A QuickPick row that carries what choosing it does. */
interface ChoiceItem extends vscode.QuickPickItem {
  choice?: SessionChoice;
}

/** Button on a stored conversation: fork it instead of resuming it. */
const FORK_BUTTON: vscode.QuickInputButton = {
  iconPath: new vscode.ThemeIcon('repo-forked'),
  tooltip: 'Fork: start a new session from a copy of this conversation',
};

/**
 * Show an input box.
 *
 * @param options The box.
 * @returns The text, or `undefined` on Escape.
 */
export function vscodeInput(options: InputOptions): Promise<string | undefined> {
  return Promise.resolve(
    vscode.window.showInputBox({
      title: options.title,
      prompt: options.prompt,
      value: options.value,
      password: options.password,
      ignoreFocusOut: true,
      validateInput: options.validate,
    }),
  );
}

/**
 * Build VSCode's `SessionsUi`.
 *
 * @param output The "calu" output channel.
 * @returns The dialogs, picker, server terminal and progress of VSCode.
 */
export function vscodeSessionsUi(output: vscode.OutputChannel): SessionsUi {
  const serverHelp = (status: IdeStatusResult | null): ServerHelp => {
    const config = vscode.workspace.getConfiguration('calu');
    const resolved = resolveCaluCommandForHost();
    const facts = hostFacts();
    const installed = (name: string): boolean => (status ? findingOk(status, name) : findExecutable(name, facts, []) !== undefined);
    return buildServerHelp({
      calu: resolved.ok ? resolved.command : ['calu'],
      sudo: config.get<boolean>('server.sudo', false) === true,
      insecure: config.get<boolean>('server.insecure', false) === true,
      extraArgs: (config.get<unknown[]>('server.extraArgs') ?? []).filter((a): a is string => typeof a === 'string'),
      tmux: installed('tmux'),
      screen: installed('screen'),
    });
  };
  const startServer = (help: ServerHelp): void => {
    const terminal = vscode.window.createTerminal({ name: 'calu server', cwd: bridgeWorkspaceFolder()?.uri });
    terminal.show();
    terminal.sendText(help.command, true);
  };
  return {
    log: (line) => output.appendLine(line),
    error: (text) => void vscode.window.showErrorMessage(text),
    info: (text) => void vscode.window.showInformationMessage(text),
    input: vscodeInput,
    pickSession: (picks) => showPick(picks),
    serverHelp,
    startServer,
    showServerHelp: async (reason, help) => {
      const detach = help.detach ? ` Detach with ${help.detach}.` : '';
      const choice = await vscode.window.showWarningMessage(
        `calu: ${reason}`,
        { modal: true, detail: `Run in a terminal:\n\n${help.command}\n\n${help.note}${detach}` },
        'Open terminal with this command',
        'Copy command',
      );
      if (choice === 'Open terminal with this command') startServer(help);
      else if (choice === 'Copy command') await vscode.env.clipboard.writeText(help.command);
    },
    progress: (title, work) => Promise.resolve(vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: `calu: ${title}…` }, work)),
  };
}

/**
 * Show the QuickPick of `buildSessionPicks`.
 *
 * @param picks The rows.
 * @returns The chosen entry and whether its fork button was used; `undefined` on cancel.
 */
function showPick(picks: SessionPick[]): Promise<{ choice: SessionChoice; fork: boolean } | undefined> {
  const items: ChoiceItem[] = picks.map((p) =>
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
