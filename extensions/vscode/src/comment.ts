/**
 * The comment command `calu.addComment`: comment on the selected lines of a normal editor
 * ("file" comment) or of either side of a diff editor ("diff" comment).
 *
 *     active tab is TabInputTextDiff and the focused document is one of its sides?
 *       yes -> kind "diff", side = before (original) | after (modified), both full texts
 *       no  -> kind "file", after_text = document text
 *     ask the comment text -> session.call comment.add -> the session builds the Comment and
 *     relays it to Claude
 */

import * as vscode from 'vscode';
import {
  buildCommentParams,
  describeGitRef,
  normaliseRelativePath,
  relativePathWithin,
  parseGitQuery,
  type CommentInput,
} from './commentPayload';
import { bridgeWorkspaceFolder, errorText, type Bridge } from './bridge';
import { parseDiffUri } from './diffView';
import type { CommentAddParams, CommentAddResult } from './protocol.generated';

/**
 * Asks the user for the comment text.
 *
 * @param description Short description of the selection, e.g. "src/a.py, lines 3-5 (after)".
 * @returns The text, or `undefined` if the user cancelled.
 */
export type CommentTextProvider = (description: string) => Thenable<string | undefined>;

/**
 * Default `CommentTextProvider`: a single-line input box that stays open on focus loss.
 *
 * @param description Shown as the input box prompt.
 * @returns The entered text, or `undefined` on Escape.
 */
export const inputBoxProvider: CommentTextProvider = (description) =>
  vscode.window.showInputBox({
    title: 'Comment for Claude',
    prompt: description,
    placeHolder: 'Your comment on the selected lines',
    ignoreFocusOut: true,
    validateInput: (value) => (value.trim() ? undefined : 'Enter a comment.'),
  });

/**
 * Compute the path ("/" separators) of a document relative to the workspace folder.
 *
 * Accepted documents: `file:` URIs inside `folder`; `git:` URIs (the working-tree file named in
 * the query must be inside `folder`); `calu-diff:` URIs of Claude's edits (their path is already
 * workspace-relative, or absolute inside `folder`).
 *
 * @param uri The document URI.
 * @param folderPath Absolute file system path of the workspace folder the bridge runs in.
 * @returns The relative path.
 * @throws Error for other schemes (e.g. `untitled:`), files outside `folder` (including other
 *   workspace folders) and paths with "..".
 */
export function relativePathOf(uri: vscode.Uri, folderPath: string): string {
  const diff = parseDiffUri(uri);
  if (diff) {
    return diff.path.startsWith('/')
      ? relativePathWithin(folderPath, diff.path)
      : normaliseRelativePath(diff.path);
  }
  let fileUri = uri;
  if (uri.scheme === 'git') {
    const q = parseGitQuery(uri.query);
    if (!q) throw new Error('This git document has no file path.');
    fileUri = vscode.Uri.file(q.path);
  }
  if (fileUri.scheme !== 'file') {
    throw new Error(
      `Comments work only on files of the workspace folder (this document is "${uri.scheme}:").`,
    );
  }
  return relativePathWithin(folderPath, fileUri.fsPath);
}

/**
 * Return the ref stored with a diff comment for the original (left) side of a diff.
 *
 * @param original URI of the original document.
 * @returns "HEAD", "index", ... for `git:` URIs; "edit:<tool_use_id>" for Claude's edits;
 *   `undefined` otherwise.
 */
export function refOf(original: vscode.Uri): string | undefined {
  if (original.scheme === 'git') {
    const q = parseGitQuery(original.query);
    return q ? describeGitRef(q.ref) : undefined;
  }
  const diff = parseDiffUri(original);
  return diff ? `edit:${diff.toolUseId}` : undefined;
}

/**
 * Collect the data for a comment from the active editor (no user interaction).
 *
 * @param editor The focused text editor.
 * @param tabInput Input of the active tab (`window.tabGroups.activeTabGroup.activeTab?.input`).
 * @param folderPath Absolute path of the workspace folder.
 * @returns The comment input without `text` (filled in later) and a description for the prompt.
 * @throws Error if the file is not a file of the workspace folder (see `relativePathOf`) or a
 *   diff side cannot be read.
 */
export async function collectCommentInput(
  editor: vscode.TextEditor,
  tabInput: unknown,
  folderPath: string,
): Promise<{ input: Omit<CommentInput, 'text'>; description: string }> {
  const doc = editor.document;
  const selection = editor.selection;
  if (tabInput instanceof vscode.TabInputTextDiff) {
    const uri = doc.uri.toString();
    const side =
      uri === tabInput.modified.toString()
        ? 'after'
        : uri === tabInput.original.toString()
          ? 'before'
          : undefined;
    if (side) {
      const other = side === 'after' ? tabInput.original : tabInput.modified;
      const otherText = (await vscode.workspace.openTextDocument(other)).getText();
      const path = relativePathOf(tabInput.modified, folderPath);
      const input: Omit<CommentInput, 'text'> = {
        path,
        selection,
        diff: {
          side,
          beforeText: side === 'before' ? doc.getText() : otherText,
          afterText: side === 'after' ? doc.getText() : otherText,
          ref: refOf(tabInput.original),
        },
      };
      return { input, description: describe(path, editor, side) };
    }
  }
  const path = relativePathOf(doc.uri, folderPath);
  return {
    input: { path, selection, file: { text: doc.getText() } },
    description: describe(path, editor),
  };
}

/**
 * Format the prompt text for the input box.
 *
 * @param path Relative path.
 * @param editor The editor (for the selection).
 * @param side Diff side, if any.
 * @returns E.g. "src/a.py, lines 3-5 (before the change)".
 */
function describe(path: string, editor: vscode.TextEditor, side?: 'before' | 'after'): string {
  const s = editor.selection;
  const first = s.start.line + 1;
  const last = !s.isEmpty && s.end.character === 0 && s.end.line > s.start.line ? s.end.line : s.end.line + 1;
  const lines = first === last ? `line ${first}` : `lines ${first}-${last}`;
  const where = side === 'before' ? ' (before the change)' : side === 'after' ? ' (after the change)' : '';
  return `${path}, ${lines}${where}`;
}

/** Result of a completed comment command (returned for tests). */
export interface CommentCommandResult {
  params: CommentAddParams;
  result: CommentAddResult;
}

/**
 * Run the comment command: collect the selection, ask for the text, send `comment.add`.
 *
 * Side effects: shows an input box (via `askText`), sends one request to the attached session, shows an
 * information or error message.
 *
 * @param bridge The bridge; a session must be attached.
 * @param askText Asks the user for the comment text.
 * @returns The sent params and the session's result; `undefined` if there is no editor, the user
 *   cancelled, or the request failed (the error was shown to the user).
 */
export async function addCommentCommand(
  bridge: Bridge,
  askText: CommentTextProvider,
): Promise<CommentCommandResult | undefined> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    void vscode.window.showErrorMessage('calu: select lines in an editor first.');
    return undefined;
  }
  try {
    const tabInput = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
    const folder = bridgeWorkspaceFolder();
    if (!folder) throw new Error('Open a folder to use calu.');
    const { input, description } = await collectCommentInput(editor, tabInput, folder.uri.fsPath);
    const text = await askText(description);
    if (text === undefined || !text.trim()) return undefined;
    const params = buildCommentParams({ ...input, text });
    const result = await bridge.session('comment.add', params);
    void vscode.window.setStatusBarMessage('Comment sent to Claude.', 4000);
    return { params, result };
  } catch (error) {
    void vscode.window.showErrorMessage(`calu: comment not sent: ${errorText(error)}`);
    return undefined;
  }
}
