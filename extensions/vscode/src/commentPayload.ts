/**
 * Pure helpers for the comment command (`calu.addComment`): selection -> 1-based line range,
 * `git:` URI query parsing and building the `comment.add` request params.
 *
 * No `vscode` import: the command handler in comment.ts extracts plain data from the editor and
 * passes it here. calu maps the selection to the other diff side (difflib); this module does
 * no diffing.
 */

import type { CommentAddParams } from './protocol.generated';

/** A selection in VSCode's 0-based coordinates (`vscode.Selection` fits this shape). */
export interface PlainSelection {
  start: { line: number; character: number };
  end: { line: number; character: number };
  /** Line of the cursor; used for an empty selection. */
  active: { line: number; character: number };
  isEmpty: boolean;
}

/** A 1-based, inclusive line range. Invariant: 1 <= startLine <= endLine. */
export interface LineRange {
  startLine: number;
  endLine: number;
}

/**
 * Convert a VSCode selection to the 1-based inclusive line range sent to calu.
 *
 * Rules: an empty selection covers the cursor line. A non-empty selection that ends at column 0
 * of a later line does not include that line (whole-line selections made with Shift+Down).
 *
 * @param selection The selection (0-based lines and characters).
 * @returns The line range; always at least one line.
 *
 * Example: start (4, 2), end (7, 0) -> { startLine: 5, endLine: 7 }.
 */
export function selectionToLines(selection: PlainSelection): LineRange {
  if (selection.isEmpty) {
    const line = selection.active.line + 1;
    return { startLine: line, endLine: line };
  }
  let endLine0 = selection.end.line;
  if (selection.end.character === 0 && selection.end.line > selection.start.line) {
    endLine0 -= 1;
  }
  return { startLine: selection.start.line + 1, endLine: endLine0 + 1 };
}

/** Parsed query of a URI of the built-in git extension (`scheme: "git"`). */
export interface GitUriQuery {
  /** Absolute file system path of the working-tree file. */
  path: string;
  /** Git ref: "HEAD", "~" (index if staged, else HEAD), "" (index), a commit, ... */
  ref: string;
}

/**
 * Parse the JSON query of a `git:` URI (`{"path": ..., "ref": ...}`).
 *
 * @param query The URI's query string (already decoded, as in `vscode.Uri.query`).
 * @returns The parsed path and ref, or `undefined` if the query is not such a JSON object.
 */
export function parseGitQuery(query: string): GitUriQuery | undefined {
  try {
    const parsed: unknown = JSON.parse(query);
    if (typeof parsed !== 'object' || parsed === null) return undefined;
    const { path, ref } = parsed as Record<string, unknown>;
    if (typeof path !== 'string') return undefined;
    return { path, ref: typeof ref === 'string' ? ref : '' };
  } catch {
    return undefined;
  }
}

/**
 * Turn a git ref of a diff's left side into the `ref` value stored with a comment.
 *
 * @param ref The ref from the `git:` URI query.
 * @returns "index" for "" (the staged version), "index or HEAD" for "~", else `ref` unchanged.
 */
export function describeGitRef(ref: string): string {
  if (ref === '') return 'index';
  if (ref === '~') return 'index or HEAD';
  return ref;
}

/**
 * Normalise a workspace-relative path for calu.
 *
 * @param path A path relative to the workspace folder (any separator style).
 * @returns The path with "/" separators and without a leading "./".
 * @throws Error if the path is empty, absolute, or contains a ".." segment (it would leave the
 *   workspace folder).
 */
export function normaliseRelativePath(path: string): string {
  let p = path.replace(/\\/g, '/');
  while (p.startsWith('./')) p = p.slice(2);
  if (!p || p.startsWith('/') || /^[a-zA-Z]:\//.test(p) || p.split('/').includes('..')) {
    throw new Error(`The file is not inside the workspace folder: ${path}`);
  }
  return p;
}

/**
 * Compute the path of a file relative to a folder, both given as absolute file system paths.
 *
 * Comparison is textual after converting "\" to "/" and removing trailing "/" from the folder;
 * it is case-sensitive (Linux semantics).
 *
 * @param folder Absolute path of the workspace folder of the session.
 * @param file Absolute path of the commented file.
 * @returns The relative path with "/" separators (see `normaliseRelativePath`).
 * @throws Error if `file` is not strictly inside `folder`, or the relative path contains "..".
 *
 * Example: ("/w/proj", "/w/proj/src/a.py") -> "src/a.py".
 */
export function relativePathWithin(folder: string, file: string): string {
  const root = folder.replace(/\\/g, '/').replace(/\/+$/, '');
  const target = file.replace(/\\/g, '/');
  if (!target.startsWith(root + '/')) {
    throw new Error(`The file is not inside the workspace folder ${folder}: ${file}`);
  }
  return normaliseRelativePath(target.slice(root.length + 1));
}

/** Everything the comment command collected from the editor. */
export interface CommentInput {
  /** Workspace-relative path ("/" separators) of the commented file (modified side for diffs). */
  path: string;
  /** The selection in the focused editor (0-based). */
  selection: PlainSelection;
  /** The user's comment; must contain non-whitespace characters. */
  text: string;
  /** For a normal editor: the full text of the document. */
  file?: { text: string };
  /**
   * For a diff editor: the side that has focus ("before" = original/left, "after" =
   * modified/right), both full texts and an optional ref of the original side.
   */
  diff?: { side: 'before' | 'after'; beforeText: string; afterText: string; ref?: string };
}

/**
 * Build the params of a `comment.add` request.
 *
 * Preconditions: exactly one of `input.file` and `input.diff` is set; `input.text.trim()` is
 * non-empty. The line range refers to the focused document (for diffs: the `side` document).
 *
 * @param input The collected editor data.
 * @returns `CommentAddParams` with kind "file" (side "after", no before_text) or "diff".
 * @throws Error if the preconditions do not hold.
 */
export function buildCommentParams(input: CommentInput): CommentAddParams {
  const text = input.text.trim();
  if (!text) throw new Error('The comment is empty.');
  if (Boolean(input.file) === Boolean(input.diff)) {
    throw new Error('buildCommentParams needs exactly one of `file` and `diff`.');
  }
  const { startLine, endLine } = selectionToLines(input.selection);
  if (input.diff) {
    const params: CommentAddParams = {
      kind: 'diff',
      path: input.path,
      side: input.diff.side,
      start_line: startLine,
      end_line: endLine,
      before_text: input.diff.beforeText,
      after_text: input.diff.afterText,
      text,
    };
    if (input.diff.ref !== undefined) params.ref = input.diff.ref;
    return params;
  }
  return {
    kind: 'file',
    path: input.path,
    side: 'after',
    start_line: startLine,
    end_line: endLine,
    after_text: input.file!.text,
    text,
  };
}
