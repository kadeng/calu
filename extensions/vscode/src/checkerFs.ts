/**
 * Checker instructions as editable files of the scheme `calu-checker`:
 *
 *     calu-checker:/<name>.md
 *
 *     open   ──readFile──►  ide.checker.get {name}             remember `version` per name
 *     Ctrl+S ──writeFile──► ide.checker.save {name, instructions, base_version}
 *                             ok      -> remember the new version
 *                             -32016  -> somebody else saved meanwhile (data: version, instructions)
 *                                        message with "Compare" (diff: stored ↔ mine) and
 *                                        "Overwrite" (save again on the stored version)
 *
 * The instructions are Markdown, so VSCode highlights them and saving works as for any file.
 * Files cannot be created, renamed or deleted here: checkers are defined in calu's settings.
 * A save may need the calu user password; `Bridge.request` asks for it.
 */

import * as vscode from 'vscode';
import { errorCode, errorText, type Bridge } from './bridge';
import { BridgeError, checkerConflict, type AnyChecker } from './bridgeProtocol';
import { RpcRequestError } from './rpc';

/** URI scheme of the checker instruction files. */
export const CHECKER_SCHEME = 'calu-checker';
/** URI scheme of the read-only "stored version" side of a conflict diff. */
export const CHECKER_STORED_SCHEME = 'calu-checker-stored';

/**
 * Build the URI of a checker's instructions.
 *
 * @param name The checker's name.
 * @returns `calu-checker:/<name>.md`.
 */
export function checkerUri(name: string): vscode.Uri {
  return vscode.Uri.from({ scheme: CHECKER_SCHEME, path: `/${name}.md` });
}

/**
 * Read the checker name from a `calu-checker:` URI.
 *
 * @param uri The URI.
 * @returns The name (the path without the leading "/" and the ".md").
 * @throws vscode.FileSystemError (FileNotFound) if the path is not `/<name>.md`.
 */
export function checkerNameOf(uri: vscode.Uri): string {
  const match = /^\/([^/]+)\.md$/.exec(uri.path);
  if (!match) throw vscode.FileSystemError.FileNotFound(uri);
  return match[1];
}

/** What is known about one opened checker. */
interface Entry {
  /** The version the editor's text is based on. */
  version: number;
  /** Size of the text in bytes, for `stat`. */
  size: number;
  /** Time of the last read or save (ms), for `stat`. */
  mtime: number;
}

/** File system of the checker instructions. Register it for `CHECKER_SCHEME`. */
export class CheckerFileSystem implements vscode.FileSystemProvider, vscode.TextDocumentContentProvider {
  private readonly changed = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
  /** Fires after a save changed a file (FileSystemProvider contract). */
  readonly onDidChangeFile = this.changed.event;
  private readonly entries = new Map<string, Entry>();
  /** Stored texts of conflicts, by checker name, served under `CHECKER_STORED_SCHEME`. */
  private readonly stored = new Map<string, string>();
  private readonly storedChanged = new vscode.EventEmitter<vscode.Uri>();
  /** Fires when a stored text changed (TextDocumentContentProvider contract). */
  readonly onDidChange = this.storedChanged.event;

  /**
   * @param bridge The bridge (started on demand).
   */
  constructor(private readonly bridge: Bridge) {}

  /**
   * Watching is not supported: the provider reports its own saves only.
   *
   * @returns A disposable that does nothing.
   */
  watch(): vscode.Disposable {
    return new vscode.Disposable(() => {});
  }

  /**
   * Describe a file (FileSystemProvider contract). The root is a directory; a checker file is
   * loaded first if it was not read yet, so `stat` also says whether the checker exists.
   *
   * @param uri The file's URI.
   * @returns Type, size and times.
   * @throws vscode.FileSystemError FileNotFound if the checker does not exist; Unavailable if
   *   the bridge cannot be asked.
   */
  async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
    if (uri.path === '/' || uri.path === '') return { type: vscode.FileType.Directory, ctime: 0, mtime: 0, size: 0 };
    const name = checkerNameOf(uri);
    if (!this.entries.has(name)) await this.readFile(uri);
    const entry = this.entries.get(name)!;
    return { type: vscode.FileType.File, ctime: 0, mtime: entry.mtime, size: entry.size };
  }

  /**
   * Read a checker's instructions from calu and remember their version.
   *
   * @param uri The file's URI.
   * @returns The instructions as UTF-8.
   * @throws vscode.FileSystemError FileNotFound if the checker does not exist; Unavailable for
   *   every other failure (bridge not running, database not reachable).
   */
  async readFile(uri: vscode.Uri): Promise<Uint8Array> {
    const name = checkerNameOf(uri);
    try {
      await this.bridge.ensureStarted();
      const checker = await this.bridge.request('ide.checker.get', { name });
      const bytes = new TextEncoder().encode(checker.instructions ?? '');
      this.entries.set(name, { version: checker.version, size: bytes.length, mtime: Date.now() });
      return bytes;
    } catch (error) {
      if (errorCode(error) === BridgeError.INVALID_PARAMS) throw vscode.FileSystemError.FileNotFound(uri);
      throw vscode.FileSystemError.Unavailable(`calu: ${errorText(error)}`);
    }
  }

  /**
   * Save a checker's instructions (see the module comment for the conflict handling).
   *
   * Side effects: `ide.checker.save`; on a conflict a warning with "Compare" and "Overwrite".
   *
   * @param uri The file's URI.
   * @param content The new instructions as UTF-8.
   * @returns Resolves when calu stored the text.
   * @throws vscode.FileSystemError NoPermissions if the file was never read (creating checkers
   *   is not possible here) or the stored version differs and the user did not overwrite (the
   *   editor stays dirty); Unavailable for every other failure.
   */
  async writeFile(uri: vscode.Uri, content: Uint8Array): Promise<void> {
    const name = checkerNameOf(uri);
    const entry = this.entries.get(name);
    if (!entry) throw vscode.FileSystemError.NoPermissions('calu: checkers are created in the calu settings, not here.');
    const instructions = new TextDecoder().decode(content);
    await this.save(uri, name, instructions, entry.version, content.length);
  }

  /**
   * Send one save and handle a version conflict.
   *
   * @param uri The file's URI.
   * @param name The checker's name.
   * @param instructions The text to store.
   * @param baseVersion The version the text is based on.
   * @param size Size of the text in bytes.
   * @returns Resolves when calu stored the text.
   * @throws vscode.FileSystemError See `writeFile`.
   */
  private async save(uri: vscode.Uri, name: string, instructions: string, baseVersion: number, size: number): Promise<void> {
    try {
      const result = await this.bridge.request('ide.checker.save', { name, instructions, base_version: baseVersion });
      this.entries.set(name, { version: result.version, size, mtime: Date.now() });
      this.changed.fire([{ type: vscode.FileChangeType.Changed, uri }]);
      void vscode.window.setStatusBarMessage(`calu: checker "${name}" saved (version ${result.version}).`, 4000);
      return;
    } catch (error) {
      const conflict = error instanceof RpcRequestError && error.code === BridgeError.CHECKER_CONFLICT ? checkerConflict(error.data) : undefined;
      if (!conflict) throw vscode.FileSystemError.Unavailable(`calu: ${errorText(error)}`);
      this.stored.set(name, conflict.instructions);
      this.storedChanged.fire(this.storedUri(name));
      const choice = await vscode.window.showWarningMessage(
        `The instructions of checker "${name}" were changed elsewhere (stored version ${conflict.version}, ` +
          `yours is based on ${baseVersion}).`,
        { modal: true, detail: '"Compare" shows the stored text next to yours. "Overwrite" stores your text.' },
        'Compare',
        'Overwrite',
      );
      if (choice === 'Overwrite') return this.save(uri, name, instructions, conflict.version, size);
      if (choice === 'Compare') {
        // A later save is based on the stored version the user has now seen.
        this.entries.set(name, { ...this.entries.get(name)!, version: conflict.version });
        void vscode.commands.executeCommand(
          'vscode.diff',
          this.storedUri(name),
          uri,
          `${name}.md (stored version ${conflict.version} ↔ yours)`,
          { preview: true },
        );
      }
      throw vscode.FileSystemError.NoPermissions(`calu: checker "${name}" was not saved: the stored version differs.`);
    }
  }

  /**
   * Provide the stored text of a conflict (TextDocumentContentProvider contract for
   * `CHECKER_STORED_SCHEME`).
   *
   * @param uri A URI built by `storedUri`.
   * @returns The stored instructions; "" if no conflict is known for the checker.
   */
  provideTextDocumentContent(uri: vscode.Uri): string {
    const match = /^\/([^/]+)\.md$/.exec(uri.path);
    return (match && this.stored.get(match[1])) ?? '';
  }

  /**
   * Build the URI of the stored side of a conflict diff.
   *
   * @param name The checker's name.
   * @returns `calu-checker-stored:/<name>.md`.
   */
  private storedUri(name: string): vscode.Uri {
    return vscode.Uri.from({ scheme: CHECKER_STORED_SCHEME, path: `/${name}.md` });
  }

  /**
   * List the root directory: the checkers that were opened so far.
   *
   * @returns `[<name>.md, File]` pairs.
   */
  readDirectory(): [string, vscode.FileType][] {
    return [...this.entries.keys()].map((name) => [`${name}.md`, vscode.FileType.File]);
  }

  /**
   * Not supported: checkers are defined in calu's settings.
   *
   * @throws vscode.FileSystemError NoPermissions, always.
   */
  createDirectory(): void {
    throw vscode.FileSystemError.NoPermissions('calu: checker instructions have no directories.');
  }

  /**
   * Not supported: checkers are removed in calu's settings.
   *
   * @throws vscode.FileSystemError NoPermissions, always.
   */
  delete(): void {
    throw vscode.FileSystemError.NoPermissions('calu: checkers are removed in the calu settings, not here.');
  }

  /**
   * Not supported: a checker's name is part of its definition.
   *
   * @throws vscode.FileSystemError NoPermissions, always.
   */
  rename(): void {
    throw vscode.FileSystemError.NoPermissions('calu: checkers cannot be renamed here.');
  }

  /**
   * Open a checker's instructions in an editor, read again from calu.
   *
   * @param name The checker's name.
   * @returns Resolves when the editor shows the file.
   * @throws Error if the checker does not exist or calu cannot be asked.
   */
  async open(name: string): Promise<void> {
    const uri = checkerUri(name);
    const open = vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString());
    if (!open?.isDirty) {
      // Forget the cached version, so `stat` reads the stored text and version again.
      this.entries.delete(name);
      if (open) this.changed.fire([{ type: vscode.FileChangeType.Changed, uri }]);
    }
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.languages.setTextDocumentLanguage(doc, 'markdown').then(undefined, () => undefined);
    await vscode.window.showTextDocument(doc, { preview: false });
  }
}

/**
 * Let the user choose a checker.
 *
 * @param checkers The checkers to offer (name, route, model).
 * @param title Title of the QuickPick.
 * @returns The chosen name; the only name without asking if there is exactly one; `undefined`
 *   if the list is empty or the user cancelled.
 */
export async function pickChecker(checkers: readonly AnyChecker[], title = 'Edit checker instructions'): Promise<string | undefined> {
  if (checkers.length === 0) return undefined;
  if (checkers.length === 1) return checkers[0].name;
  const picked = await vscode.window.showQuickPick(
    checkers.map((c) => ({ label: c.name, description: `route: ${c.route}`, detail: c.model ? `model: ${c.model}` : 'default model' })),
    { title, placeHolder: 'Which checker?' },
  );
  return picked?.label;
}
