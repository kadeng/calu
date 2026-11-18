/**
 * Host side of the chat view's context-menu actions on tool items (see `shared/itemMenu.ts`).
 *
 *     openFileAtEdit          -> open <workspace>/<path>, cursor at the edit's start line
 *     openFile (reads)        -> the same, cursor at the first line read
 *     showUncommittedChanges  -> vscode.diff(git HEAD version, working file), cursor at the line
 *     showEdit                -> the edit's before/after diff (DiffStore)
 *     copy                    -> clipboard
 */

import * as vscode from 'vscode';
import type { DiffStore } from './diffView';
import { resolveItemAction, type ItemActionId } from './shared/itemMenu';
import type { ToolView } from './shared/viewState';

/** The part of the built-in git extension's API used here (extensions/git/src/api/git.d.ts). */
interface GitApi {
  toGitUri(uri: vscode.Uri, ref: string): vscode.Uri;
}

/**
 * Return the API of the built-in git extension.
 *
 * @returns The API (version 1).
 * @throws Error if the git extension is not installed or disabled.
 */
async function gitApi(): Promise<GitApi> {
  const ext = vscode.extensions.getExtension<{ getAPI(version: 1): GitApi }>('vscode.git');
  if (!ext) throw new Error('The built-in Git extension is not available.');
  const exports = ext.isActive ? ext.exports : await ext.activate();
  return exports.getAPI(1);
}

/**
 * Build editor options that put the cursor at the start of a 1-based line.
 *
 * @param line 1-based line number (values < 1 are treated as 1).
 * @returns `TextDocumentShowOptions` with `selection` at that line and `preview: false`.
 */
function atLine(line: number): vscode.TextDocumentShowOptions {
  const position = new vscode.Position(Math.max(line, 1) - 1, 0);
  return { selection: new vscode.Range(position, position), preview: false };
}

/**
 * Run a context-menu action of a tool item.
 *
 * Side effects: opens editors or a diff editor, or writes to the clipboard.
 *
 * @param tool The tool view from the host's state (undefined if unknown).
 * @param action The requested action (validated by `resolveItemAction`).
 * @param folder The workspace folder the session works in.
 * @param diffs The store of Claude's edit diffs (for "Show this edit").
 * @returns A promise that resolves when the editor is shown or the text is copied.
 * @throws (rejects) Error for invalid actions, paths outside the workspace, a missing git
 *   extension, or files that cannot be opened.
 */
export async function runItemAction(
  tool: ToolView | undefined,
  action: ItemActionId,
  folder: vscode.WorkspaceFolder,
  diffs: DiffStore,
): Promise<void> {
  const task = resolveItemAction(tool, action, folder.uri.fsPath);
  switch (task.action) {
    case 'copy':
      await vscode.env.clipboard.writeText(task.text);
      return;
    case 'showEdit':
      await diffs.open(task.toolUseId);
      return;
    case 'openFileAtEdit': {
      const uri = vscode.Uri.joinPath(folder.uri, ...task.path.split('/'));
      await vscode.window.showTextDocument(uri, atLine(task.line));
      return;
    }
    case 'showUncommittedChanges': {
      const uri = vscode.Uri.joinPath(folder.uri, ...task.path.split('/'));
      const head = (await gitApi()).toGitUri(uri, 'HEAD');
      await vscode.commands.executeCommand(
        'vscode.diff',
        head,
        uri,
        `${task.path} (HEAD ↔ working tree)`,
        atLine(task.line),
      );
      return;
    }
  }
}
