/**
 * Diff views of Claude's edits.
 *
 * calu sends `edit.diff {tool_use_id, path, before, after}` when it checks an edit. This
 * module keeps these texts in memory and serves them as read-only virtual documents:
 *
 *     calu-diff:/<path>?id=<tool_use_id>&side=before|after&path=<path>
 *
 * `open(id)` shows both documents in VSCode's diff editor (`vscode.diff`), revealed at the first
 * changed line. The URI path ends with the file name, so VSCode picks the language mode from
 * the extension.
 */

import * as vscode from 'vscode';
import type { EditDiff } from './protocol.generated';
import { firstChangedLine } from './shared/itemMenu';

/** URI scheme of the virtual before/after documents. */
export const DIFF_SCHEME = 'calu-diff';

/** Maximum number of diffs kept in memory; the oldest are dropped first. */
const MAX_DIFFS = 300;

/** Identity of a `calu-diff:` document. */
export interface DiffUriInfo {
  toolUseId: string;
  side: 'before' | 'after';
  /** The `path` of the `edit.diff` event (workspace-relative, or absolute outside it). */
  path: string;
}

/**
 * Read the identity of a `calu-diff:` URI.
 *
 * @param uri Any URI.
 * @returns The info, or `undefined` if the scheme is not `calu-diff` or the query is incomplete.
 */
export function parseDiffUri(uri: vscode.Uri): DiffUriInfo | undefined {
  if (uri.scheme !== DIFF_SCHEME) return undefined;
  const q = new URLSearchParams(uri.query);
  const toolUseId = q.get('id');
  const side = q.get('side');
  const path = q.get('path');
  if (!toolUseId || (side !== 'before' && side !== 'after') || path === null) return undefined;
  return { toolUseId, side, path };
}

/** Stores edit diffs and provides their documents. Register it for `DIFF_SCHEME`. */
export class DiffStore implements vscode.TextDocumentContentProvider {
  private readonly diffs = new Map<string, EditDiff>();

  /**
   * Remember the texts of an edit. Diffs without `tool_use_id` are ignored. A second diff with
   * the same id replaces the first.
   *
   * @param diff The `edit.diff` event params.
   * @returns Nothing.
   */
  record(diff: EditDiff): void {
    if (!diff.tool_use_id) return;
    this.diffs.delete(diff.tool_use_id);
    this.diffs.set(diff.tool_use_id, diff);
    while (this.diffs.size > MAX_DIFFS) {
      const oldest = this.diffs.keys().next().value as string;
      this.diffs.delete(oldest);
    }
  }

  /**
   * Look up a stored diff.
   *
   * @param toolUseId The tool_use_id of the edit.
   * @returns The diff, or `undefined` if unknown (never recorded or already dropped).
   */
  get(toolUseId: string): EditDiff | undefined {
    return this.diffs.get(toolUseId);
  }

  /**
   * Build the URI of one side of a stored diff.
   *
   * @param diff The diff (its `tool_use_id` must be set).
   * @param side "before" or "after".
   * @returns The `calu-diff:` URI.
   */
  uriFor(diff: EditDiff, side: 'before' | 'after'): vscode.Uri {
    const display = diff.path.replace(/\\/g, '/');
    const query = new URLSearchParams({ id: diff.tool_use_id ?? '', side, path: diff.path });
    return vscode.Uri.from({
      scheme: DIFF_SCHEME,
      path: display.startsWith('/') ? display : '/' + display,
      query: query.toString(),
    });
  }

  /**
   * Provide the text of a `calu-diff:` document (TextDocumentContentProvider contract).
   *
   * @param uri A URI built by `uriFor`.
   * @returns The before or after text; a placeholder line if the diff is no longer stored.
   */
  provideTextDocumentContent(uri: vscode.Uri): string {
    const info = parseDiffUri(uri);
    const diff = info && this.diffs.get(info.toolUseId);
    if (!info || !diff) return '(This edit is no longer available; it was made in an earlier session.)\n';
    return info.side === 'before' ? diff.before : diff.after;
  }

  /**
   * Open the diff editor for an edit, with the cursor on the first changed line (the event's
   * `start_line`, else the first line in which before and after differ).
   *
   * @param toolUseId The tool_use_id of the edit.
   * @returns A promise that resolves when the editor is open.
   * @throws (rejects) Error if no diff is stored for `toolUseId`.
   */
  async open(toolUseId: string): Promise<void> {
    const diff = this.diffs.get(toolUseId);
    if (!diff) throw new Error(`No diff is available for tool call ${toolUseId}.`);
    const name = diff.path.split(/[\\/]/).pop() ?? diff.path;
    const start = typeof diff.start_line === 'number' && diff.start_line >= 1 ? diff.start_line : undefined;
    const line = start ?? firstChangedLine(diff.before, diff.after);
    await vscode.commands.executeCommand(
      'vscode.diff',
      this.uriFor(diff, 'before'),
      this.uriFor(diff, 'after'),
      `${name} (Claude's edit)`,
      { preview: true, selection: new vscode.Range(line - 1, 0, line - 1, 0) },
    );
  }
}
