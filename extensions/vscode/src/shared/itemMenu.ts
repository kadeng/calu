/**
 * Pure model of the chat view's tool items and their context menu.
 *
 *     right-click on a tool item (webview) -> menuFor(tool) -> info rows + actions
 *     action chosen -> {type: "itemAction", toolUseId, action} -> host
 *     host: resolveItemAction(state, toolUseId, action, folder) -> what to do, or an error
 *
 * The host decides from its own view state; paths never come from the webview.
 */

import type { ToolView } from './viewState';

/** Tools that change files and come with an `edit.diff`. */
export const EDIT_TOOLS: readonly string[] = ['Edit', 'Write', 'MultiEdit', 'NotebookEdit'];

/** Tools that read one file (`file_path` / `notebook_path`). */
export const READ_TOOLS: readonly string[] = ['Read', 'NotebookRead'];

/** Kind of a tool item for presentation and the context menu. */
export type ToolKind = 'edit' | 'read' | 'command' | 'other';

/** Actions of the context menu. */
export type ItemActionId =
  | 'openFile'
  | 'openFileAtEdit'
  | 'showUncommittedChanges'
  | 'showEdit'
  | 'copyPath'
  | 'copyCommand';

/** The context menu of one tool item. */
export interface ItemMenu {
  kind: Exclude<ToolKind, 'other'>;
  /** Information rows `[label, value]`, shown above the actions. */
  info: [string, string][];
  /** Actions in display order. */
  actions: { id: ItemActionId; label: string }[];
}

/**
 * Classify a tool call.
 *
 * @param tool The tool view.
 * @returns "edit" for file-changing tools (or any tool with a diff), "read" for tools that read
 *   one file, "command" for Bash, else "other".
 */
export function toolKind(tool: Pick<ToolView, 'name' | 'diffPath'>): ToolKind {
  if (EDIT_TOOLS.includes(tool.name) || tool.diffPath !== undefined) return 'edit';
  if (READ_TOOLS.includes(tool.name)) return 'read';
  if (tool.name === 'Bash') return 'command';
  return 'other';
}

/**
 * Shorten a commit id for display.
 *
 * @param head A commit id, or null/undefined.
 * @returns The first 8 characters, or "unknown".
 */
export function shortHead(head: string | null | undefined): string {
  return head ? head.slice(0, 8) : 'unknown';
}

/**
 * Describe an edit's line range.
 *
 * @param range The range from `edit.diff`, or null/undefined.
 * @returns "12-15", "12", "deleted before line 12" (end = start - 1), or "unknown".
 */
export function describeRange(range: ToolView['editRange']): string {
  if (!range) return 'unknown';
  if (range.end < range.start) return `deleted before line ${range.start}`;
  return range.end === range.start ? `${range.start}` : `${range.start}-${range.end}`;
}

/**
 * Return the file path of an edit or read: the `edit.diff` path, else the tool input's
 * `file_path` / `notebook_path`.
 *
 * @param tool The tool view.
 * @returns The path as calu reported it, or undefined.
 */
export function editPath(tool: ToolView): string | undefined {
  if (tool.diffPath) return tool.diffPath;
  const p = tool.input.file_path ?? tool.input.notebook_path;
  return typeof p === 'string' ? p : undefined;
}

/**
 * Build the context menu of a tool item.
 *
 * @param tool The tool view.
 * @returns The menu for edits and commands, or `undefined` for other tools.
 */
export function menuFor(tool: ToolView): ItemMenu | undefined {
  const kind = toolKind(tool);
  const decision = tool.check?.decision ?? 'not checked';
  if (kind === 'edit') {
    const actions: ItemMenu['actions'] = [
      { id: 'openFileAtEdit', label: 'Open file at edit' },
      { id: 'showUncommittedChanges', label: 'Show uncommitted changes of this file' },
    ];
    if (tool.diffPath !== undefined) actions.push({ id: 'showEdit', label: 'Show this edit' });
    actions.push({ id: 'copyPath', label: 'Copy path' });
    return {
      kind,
      info: [
        ['File', editPath(tool) ?? 'unknown'],
        ['Tool', tool.name],
        ['Lines', describeRange(tool.editRange)],
        ['Decision', decision],
        ['Git HEAD', shortHead(tool.gitHead)],
      ],
      actions,
    };
  }
  if (kind === 'read') {
    return {
      kind,
      info: [
        ['File', editPath(tool) ?? 'unknown'],
        ['Tool', tool.name],
        ['From line', String(readLine(tool))],
        ['Decision', decision],
      ],
      actions: [
        { id: 'openFile', label: 'Open file' },
        { id: 'copyPath', label: 'Copy path' },
      ],
    };
  }
  if (kind === 'command') {
    const command = typeof tool.input.command === 'string' ? tool.input.command : '';
    return {
      kind,
      info: [
        ['Command', command],
        ['Decision', decision],
        ['Git HEAD', shortHead(tool.gitHead)],
      ],
      actions: [{ id: 'copyCommand', label: 'Copy command' }],
    };
  }
  return undefined;
}

/**
 * Return the first line a read tool call read.
 *
 * @param tool The tool view.
 * @returns The tool input's `offset` if it is a positive integer, else 1.
 */
export function readLine(tool: ToolView): number {
  const offset = tool.input.offset;
  return typeof offset === 'number' && Number.isInteger(offset) && offset > 0 ? offset : 1;
}

/**
 * Return the action that the clickable file name of a tool item runs.
 *
 * @param tool The tool view.
 * @returns "openFileAtEdit" for edits, "openFile" for reads, undefined for other tools or a
 *   tool without a file path.
 */
export function fileLinkAction(tool: ToolView): ItemActionId | undefined {
  if (!editPath(tool)) return undefined;
  const kind = toolKind(tool);
  return kind === 'edit' ? 'openFileAtEdit' : kind === 'read' ? 'openFile' : undefined;
}

/** What the host does for a validated action. */
export type ResolvedAction =
  | { action: 'openFileAtEdit' | 'showUncommittedChanges'; path: string; line: number }
  | { action: 'showEdit'; toolUseId: string }
  | { action: 'copy'; text: string };

/**
 * Validate a context-menu action against the host's state and turn it into a concrete task.
 *
 * Paths: the edit's path must be workspace-relative (as calu reports files inside the
 * workspace) or absolute inside `folder`; it must not contain "..". The line is the edit's start
 * line (1 if unknown).
 *
 * @param tool The tool view from the host's state (undefined if the id is unknown).
 * @param action The requested action (untrusted).
 * @param folder Absolute path of the workspace folder ("/" or "\" separators).
 * @returns The task.
 * @throws Error if the tool is unknown, the action is not offered for it, or the path is
 *   outside the workspace.
 */
export function resolveItemAction(tool: ToolView | undefined, action: unknown, folder: string): ResolvedAction {
  if (!tool) throw new Error('This tool call is no longer in the chat.');
  const menu = menuFor(tool);
  if (!menu || !menu.actions.some((a) => a.id === action)) throw new Error('This action is not available here.');
  switch (action as ItemActionId) {
    case 'copyCommand':
      return { action: 'copy', text: typeof tool.input.command === 'string' ? tool.input.command : '' };
    case 'showEdit':
      return { action: 'showEdit', toolUseId: tool.toolUseId };
    default: {
      const rel = workspaceRelative(editPath(tool) ?? '', folder);
      if (action === 'copyPath') return { action: 'copy', text: rel };
      if (action === 'openFile') return { action: 'openFileAtEdit', path: rel, line: readLine(tool) };
      return {
        action: action as 'openFileAtEdit' | 'showUncommittedChanges',
        path: rel,
        line: tool.editRange?.start ?? 1,
      };
    }
  }
}

/**
 * Convert a path from calu into a workspace-relative path.
 *
 * @param path Workspace-relative, or absolute inside `folder`.
 * @param folder Absolute workspace folder path.
 * @returns The relative path with "/" separators.
 * @throws Error if the path is empty, outside `folder`, or contains "..".
 */
export function workspaceRelative(path: string, folder: string): string {
  let p = path.replace(/\\/g, '/');
  const root = folder.replace(/\\/g, '/').replace(/\/+$/, '');
  if (p.startsWith('/') || /^[a-zA-Z]:\//.test(p)) {
    // Windows drive letters differ in case between VSCode ("c:") and Claude ("C:").
    const windows = /^[a-zA-Z]:\//.test(root);
    const inside = windows
      ? p.toLowerCase().startsWith(root.toLowerCase() + '/')
      : p.startsWith(root + '/');
    if (!inside) throw new Error(`The file is outside the workspace: ${path}`);
    p = p.slice(root.length + 1);
  }
  while (p.startsWith('./')) p = p.slice(2);
  if (!p || p.split('/').includes('..')) throw new Error(`The file is outside the workspace: ${path}`);
  return p;
}

// ------------------------------------------------------------------ what a click on a row does

/**
 * What a click (or Enter) on a tool row does (spec section 11). `title` is the row's tooltip.
 *
 * - "openDiff": the native diff editor of this edit (`openDiff {toolUseId}`);
 * - "openFile": the file at `line`, through `itemAction {action}`;
 * - "openTab": the tab of the subagent that this call launched;
 * - "expand": show or hide the row's details in place.
 */
export type RowClick =
  | { kind: 'openDiff'; title: string }
  | { kind: 'openFile'; action: ItemActionId; line: number; title: string }
  | { kind: 'openTab'; tab: string; title: string }
  | { kind: 'expand'; title: string };

/**
 * Decide what a click on the row of a tool call does.
 *
 *     edit with a recorded diff   -> openDiff      "Open diff"
 *     edit without a diff         -> openFile      "Open file at line N (no diff was recorded)"
 *     Read                        -> openFile      "Open file at line N"
 *     Agent / Task                -> openTab       "Open the subagent's tab"
 *     Bash and every other tool   -> expand        "Show the command and its output" / "Show details"
 *
 * @param tool The tool view.
 * @returns The action with its tooltip. The host may still find that it no longer holds the
 *   diff of an old edit; it then opens the file instead and says so.
 */
export function rowClick(tool: ToolView): RowClick {
  if (tool.name === 'Agent' || tool.name === 'Task') {
    return { kind: 'openTab', tab: tool.toolUseId, title: "Open the subagent's tab" };
  }
  const kind = toolKind(tool);
  const file = fileClick(tool);
  if (kind === 'edit' && tool.diffPath !== undefined) return { kind: 'openDiff', title: 'Open diff' };
  if (kind === 'edit' && file) {
    return { kind: 'openFile', action: file.action, line: file.line, title: `${file.title} (no diff was recorded)` };
  }
  if (kind === 'read' && file) return { kind: 'openFile', ...file };
  return { kind: 'expand', title: kind === 'command' ? 'Show the command and its output' : 'Show details' };
}

/**
 * Decide what a click on the file name inside a tool row does.
 *
 * @param tool The tool view.
 * @returns The context-menu action that opens the file, the 1-based line the cursor is put on
 *   (the edit's first changed line, or the first line read; 1 if unknown) and the tooltip, e.g.
 *   "Open file at line 42"; `undefined` for tools without one file (see `fileLinkAction`).
 */
export function fileClick(tool: ToolView): { action: ItemActionId; line: number; title: string } | undefined {
  const action = fileLinkAction(tool);
  if (!action) return undefined;
  const line = action === 'openFile' ? readLine(tool) : (tool.editRange?.start ?? 1);
  return { action, line, title: `Open file at line ${line}` };
}

/**
 * Find the first line in which two texts differ (for revealing a diff at its first change).
 *
 * @param before The text before the edit.
 * @param after The text after the edit.
 * @returns The 1-based number of the first line that differs or exists in only one text; 1 if
 *   the texts are equal.
 */
export function firstChangedLine(before: string, after: string): number {
  const a = before.split('\n');
  const b = after.split('\n');
  const shared = Math.min(a.length, b.length);
  for (let i = 0; i < shared; i++) if (a[i] !== b[i]) return i + 1;
  return a.length === b.length ? 1 : shared + 1;
}
