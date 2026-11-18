/**
 * The read-only context documents of the scheme `calu-context`:
 *
 *     calu-context:/Checker context.md     session.call context.get        (Show Checker Context)
 *     calu-context:/escalation-<id>.md     session.call escalation.context ("Why?" on a card)
 *
 * VSCode opens them as Markdown in an editor tab and allows no edits. The text is fetched each
 * time VSCode asks for it. While the checker context is open, session events that can change
 * it reload it (once, 800 ms after the last one):
 *
 *     session event ──onEvent──► relevant and document open? ──► timer 800 ms ──► onDidChange
 */

import * as vscode from 'vscode';
import type { CheckerContextResult, EscalationContextResult } from './protocol.generated';
import { CONTEXT_EVENTS, formatCheckerContext, formatEscalationContext } from './shared/contextText';

/** URI scheme of the context documents. */
export const CONTEXT_SCHEME = 'calu-context';
/** The checker context document; the ".md" makes VSCode treat it as Markdown. */
export const CONTEXT_URI = vscode.Uri.from({ scheme: CONTEXT_SCHEME, path: '/Checker context.md' });
const RELOAD_DELAY_MS = 800;

/**
 * Build the URI of an escalation's context document.
 *
 * @param escalationId calu's escalation id.
 * @returns `calu-context:/escalation-<id>.md` (the id is percent-encoded by VSCode).
 */
export function escalationContextUri(escalationId: string): vscode.Uri {
  return vscode.Uri.from({ scheme: CONTEXT_SCHEME, path: `/escalation-${escalationId}.md` });
}

/**
 * Read the escalation id from a context document URI.
 *
 * @param uri A `calu-context:` URI.
 * @returns The id for `/escalation-<id>.md`, or `undefined` for the checker context document.
 */
export function escalationIdOf(uri: vscode.Uri): string | undefined {
  return /^\/escalation-(.+)\.md$/.exec(uri.path)?.[1];
}

/** Provider of the context documents. Register it for `CONTEXT_SCHEME`. */
export class ContextDocuments implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  /** Fires when the checker context should be read again (TextDocumentContentProvider contract). */
  readonly onDidChange = this.changed.event;
  private timer: ReturnType<typeof setTimeout> | undefined;

  /**
   * @param loadContext Fetches the checker context (`context.get`); may throw (no session).
   * @param loadEscalation Fetches one escalation's context (`escalation.context`); may throw.
   * @param describeError Turns a thrown value into a message for the document.
   */
  constructor(
    private readonly loadContext: () => Promise<CheckerContextResult>,
    private readonly loadEscalation: (escalationId: string) => Promise<EscalationContextResult>,
    private readonly describeError: (error: unknown) => string,
  ) {}

  /**
   * Return the text of a context document (called by VSCode when the document opens or after
   * `onDidChange`).
   *
   * @param uri The document's URI.
   * @returns The formatted context, or a short Markdown text naming the error; never throws.
   */
  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const escalationId = escalationIdOf(uri);
    try {
      if (escalationId !== undefined) return formatEscalationContext(await this.loadEscalation(escalationId));
      return formatCheckerContext(await this.loadContext(), new Date());
    } catch (error) {
      const title = escalationId !== undefined ? `Escalation ${escalationId}` : 'Checker context';
      return `# ${title}\n\nCould not load the context: ${this.describeError(error)}\n`;
    }
  }

  /**
   * Open the checker context in an editor tab (or show it again), with freshly loaded content.
   *
   * @returns Resolves when the editor shows the document.
   * @throws Error if VSCode cannot open the document.
   */
  async show(): Promise<void> {
    await this.open(CONTEXT_URI);
  }

  /**
   * Open the stored context of one escalation in an editor tab, freshly loaded.
   *
   * @param escalationId calu's escalation id.
   * @returns Resolves when the editor shows the document.
   * @throws Error if VSCode cannot open the document.
   */
  async showEscalation(escalationId: string): Promise<void> {
    await this.open(escalationContextUri(escalationId));
  }

  /**
   * Note a session event: if it can change the checker context and the document is open,
   * schedule a reload (several events within 800 ms -> one reload).
   *
   * @param method The event's method, e.g. "check.result".
   * @returns Nothing.
   */
  onEvent(method: string): void {
    if (!CONTEXT_EVENTS.has(method) || !this.isOpen(CONTEXT_URI)) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.changed.fire(CONTEXT_URI);
    }, RELOAD_DELAY_MS);
  }

  /**
   * Reload the checker context now if it is open (after attaching to another session).
   *
   * @returns Nothing.
   */
  refresh(): void {
    if (this.isOpen(CONTEXT_URI)) this.changed.fire(CONTEXT_URI);
  }

  /**
   * Stop the pending reload and release the event emitter.
   *
   * @returns Nothing.
   */
  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    this.changed.dispose();
  }

  /**
   * Open a context document with fresh content.
   *
   * @param uri The document's URI.
   * @returns Resolves when the editor shows the document.
   * @throws Error if VSCode cannot open the document.
   */
  private async open(uri: vscode.Uri): Promise<void> {
    if (this.isOpen(uri)) this.changed.fire(uri);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: false });
  }

  /**
   * Tell whether VSCode holds a document.
   *
   * @param uri The document's URI.
   * @returns True if it is open in a tab, or recently closed and cached.
   */
  private isOpen(uri: vscode.Uri): boolean {
    return vscode.workspace.textDocuments.some((d) => d.uri.toString() === uri.toString());
  }
}
