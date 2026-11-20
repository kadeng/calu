/**
 * Sends the user's saves in the editor to the attached session, which records their diffs
 * while `human_diffs.enabled` is on (agent/records/PLAN.md D253). The logic without `vscode`
 * is in `shared/humanDiffs.ts`.
 *
 * Only saves through VSCode's editor reach these events. Claude's edits, other programs and
 * the terminal write the file on disk; VSCode reloads it without a save.
 *
 * `before` is the text the document started from (`SaveBases`: last open, save, reload or
 * revert), not the file on disk at the save. So a save that overwrites a version Claude wrote
 * meanwhile records only the user's own edits. Only a document whose base is unknown (opened
 * with unsaved edits) falls back to the file on disk.
 */

import * as vscode from 'vscode';
import { bridgeWorkspaceFolder, type Bridge } from './bridge';
import { HumanDiffGate, MAX_CHARS, SaveBases, humanDiffParams } from './shared/humanDiffs';

/** Milliseconds a save waits at most for the file's old text to be read. */
const READ_WAIT_MS = 1000;

/**
 * Listen to saves and send `human_diffs.add` for each one while a session is attached.
 *
 * Side effects: keeps the base text of each open `file` document in memory (see `SaveBases`);
 * before a save of a document without a known base, the file's text on disk is read (the save
 * waits for it, at most `READ_WAIT_MS`); after the save, one `session.call` goes to the bridge.
 * Errors are logged and never shown: a save never fails because of this.
 *
 * @param bridge The bridge.
 * @param attached Tells whether a session is attached now.
 * @param log Receives one line per failed request.
 * @returns The listeners, to dispose with the extension.
 */
export function watchSaves(bridge: Bridge, attached: () => boolean, log: (line: string) => void): vscode.Disposable {
  const gate = new HumanDiffGate();
  const before = new Map<string, string>();
  const bases = new SaveBases();
  for (const document of vscode.workspace.textDocuments) {
    if (document.uri.scheme === 'file') bases.opened(document.uri.toString(), document.getText(), document.isDirty);
  }
  const opened = vscode.workspace.onDidOpenTextDocument((document) => {
    if (document.uri.scheme === 'file') bases.opened(document.uri.toString(), document.getText(), document.isDirty);
  });
  const changed = vscode.workspace.onDidChangeTextDocument((event) => {
    const document = event.document;
    // Typing leaves the document dirty and keeps the base: no getText() per keystroke.
    if (document.uri.scheme === 'file' && !document.isDirty) bases.changed(document.uri.toString(), document.getText(), document.isDirty);
  });
  const closed = vscode.workspace.onDidCloseTextDocument((document) => bases.closed(document.uri.toString()));

  /**
   * Read the text a document's file has on disk.
   *
   * @param uri The file.
   * @returns The text, or undefined if it cannot be read or is larger than `MAX_CHARS` bytes.
   */
  async function onDisk(uri: vscode.Uri): Promise<string | undefined> {
    try {
      const stat = await vscode.workspace.fs.stat(uri);
      if (stat.size > MAX_CHARS) return undefined;
      return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
    } catch {
      return undefined;
    }
  }

  const willSave = vscode.workspace.onWillSaveTextDocument((event) => {
    const uri = event.document.uri;
    if (uri.scheme !== 'file' || !gate.wanted(attached(), Date.now())) return;
    const base = bases.base(uri.toString());
    if (base !== undefined) {
      before.set(uri.toString(), base);
      return;
    }
    const read = onDisk(uri).then((text) => {
      if (text === undefined) before.delete(uri.toString());
      else before.set(uri.toString(), text);
    });
    const limit = new Promise<void>((resolve) => setTimeout(resolve, READ_WAIT_MS));
    event.waitUntil(Promise.race([read, limit]));
  });

  const didSave = vscode.workspace.onDidSaveTextDocument((document) => {
    const key = document.uri.toString();
    const old = before.get(key);
    before.delete(key);
    if (document.uri.scheme === 'file') bases.saved(key, document.getText());
    const folder = bridgeWorkspaceFolder();
    if (!folder || old === undefined || !gate.wanted(attached(), Date.now())) return;
    const params = humanDiffParams(folder.uri.fsPath, document.uri.fsPath, old, document.getText());
    if (!params) return;
    bridge.session('human_diffs.add', params).then(
      (answer) => gate.heard(answer.enabled, Date.now()),
      (error: unknown) => log(`[calu] human_diffs.add failed: ${error instanceof Error ? error.message : String(error)}`),
    );
  });

  return vscode.Disposable.from(opened, changed, closed, willSave, didSave);
}
