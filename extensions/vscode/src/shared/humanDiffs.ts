/**
 * Recording the user's own saves as diffs (agent/records/PLAN.md D253): the parts without
 * `vscode`, shared with the tests.
 *
 *     open / save / reload or revert (document not dirty) ──► SaveBases: base = document text
 *     onWillSaveTextDocument ── gate.wanted()? ── base (or, if unknown, the file on disk)
 *                                                 ──► before (kept per uri)
 *     onDidSaveTextDocument  ── before + document text ──► humanDiffParams ──► session.call
 *                                                          human_diffs.add {path, before, after}
 *     answer {enabled: false} ──► gate.heard(false): nothing is sent for OFF_PAUSE_MS
 *
 * The base is the text the user's buffer started from, not the file on disk at the save. If
 * Claude wrote the file while the buffer had unsaved edits, and the user overwrote it, the diff
 * shows only the user's own edits (D253).
 *
 * The session decides everything else (the setting, git-tracked files only, the file layout).
 */

import { relativePathWithin } from '../commentPayload';

/** Milliseconds the extension sends nothing after the session said the feature is off. */
export const OFF_PAUSE_MS = 60_000;

/** The longest text the extension sends as `before` or `after` (the session's own limit). */
export const MAX_CHARS = 1_000_000;

/** The params of `human_diffs.add`. */
export interface HumanDiffParams {
  /** The file relative to the workspace folder, with "/" separators. */
  path: string;
  /** The text before the save, line ends as "\n". */
  before: string;
  /** The saved text, line ends as "\n". */
  after: string;
}

/**
 * Whether saves are worth sending: not while the session said the feature is off.
 */
export class HumanDiffGate {
  private offUntil = 0;

  /**
   * Tell whether a save should be sent now.
   *
   * @param attached Whether a session is attached.
   * @param now The current time in milliseconds.
   * @returns True if a session is attached and no "off" answer arrived in the last
   *   `OFF_PAUSE_MS`.
   */
  wanted(attached: boolean, now: number): boolean {
    return attached && now >= this.offUntil;
  }

  /**
   * Take note of the session's answer.
   *
   * @param enabled `enabled` of the answer.
   * @param now The current time in milliseconds.
   */
  heard(enabled: boolean, now: number): void {
    this.offUntil = enabled ? 0 : now + OFF_PAUSE_MS;
  }

  /** Forget an "off" answer, e.g. after an attach to another session. */
  reset(): void {
    this.offUntil = 0;
  }
}

/**
 * Replace "\r\n" and lone "\r" by "\n".
 *
 * @param text Any text.
 * @returns The text with "\n" line ends only.
 */
export function normaliseLineEnds(text: string): string {
  return text.replace(/\r\n?/g, '\n');
}

/**
 * Build the params of `human_diffs.add` for one save, or decide that it is not sent.
 *
 * @param folder Absolute path of the workspace folder.
 * @param file Absolute path of the saved file.
 * @param before The text before the save (as read from disk), or undefined if unknown.
 * @param after The saved text.
 * @returns The params; null if the file is outside the folder, `before` is unknown, either
 *   text is longer than `MAX_CHARS`, or the texts are equal after normalising the line ends.
 *
 * Example: ("/w", "/w/doc/a.md", "a\r\n", "b\n") -> {path: "doc/a.md", before: "a\n", after: "b\n"}.
 */
export function humanDiffParams(folder: string, file: string, before: string | undefined, after: string): HumanDiffParams | null {
  if (before === undefined || before.length > MAX_CHARS || after.length > MAX_CHARS) return null;
  let path: string;
  try {
    path = relativePathWithin(folder, file);
  } catch {
    return null;
  }
  const old = normaliseLineEnds(before);
  const saved = normaliseLineEnds(after);
  if (old === saved) return null;
  return { path, before: old, after: saved };
}

/**
 * The text each open document started from: the text it had when it was last opened, saved,
 * reloaded from disk or reverted, without the user's unsaved edits since.
 *
 * Keys are document URIs as strings. A document opened with unsaved edits (e.g. restored by hot
 * exit) or longer than `MAX_CHARS` has no known base until its next save or reload. Example:
 *
 *     opened(k, "v1", false); changed(k, "v1+mine", true)   // Claude writes "v2" on disk meanwhile
 *     base(k) == "v1"                                       // the user overwrites: diff v1 -> v1+mine
 *     changed(k, "v2", false)                               // the user reloaded Claude's version instead
 *     base(k) == "v2"
 */
export class SaveBases {
  private readonly texts = new Map<string, string>();

  /**
   * Take note of an opened document.
   *
   * @param key The document's URI.
   * @param text Its text.
   * @param dirty Whether it has unsaved edits already; then its base is unknown.
   */
  opened(key: string, text: string, dirty: boolean): void {
    if (dirty) this.texts.delete(key);
    else this.keep(key, text);
  }

  /**
   * Take note of a change of a document. A change that leaves the document without unsaved
   * edits (a reload from disk, a revert, a save, an undo back to the saved text) makes its text
   * the base; a change that leaves unsaved edits keeps the base.
   *
   * @param key The document's URI.
   * @param text Its text after the change.
   * @param dirty Whether it has unsaved edits after the change.
   */
  changed(key: string, text: string, dirty: boolean): void {
    if (!dirty) this.keep(key, text);
  }

  /**
   * Take note of a save: the saved text becomes the base.
   *
   * @param key The document's URI.
   * @param text The saved text.
   */
  saved(key: string, text: string): void {
    this.keep(key, text);
  }

  /**
   * Forget a closed document.
   *
   * @param key The document's URI.
   */
  closed(key: string): void {
    this.texts.delete(key);
  }

  /**
   * Read a document's base.
   *
   * @param key The document's URI.
   * @returns The text the document started from, or undefined if it is not known.
   */
  base(key: string): string | undefined {
    return this.texts.get(key);
  }

  /**
   * Make a text the base; a text longer than `MAX_CHARS` is not kept (its save is not sent
   * anyway), so the base becomes unknown.
   *
   * @param key The document's URI.
   * @param text The new base.
   */
  private keep(key: string, text: string): void {
    if (text.length > MAX_CHARS) this.texts.delete(key);
    else this.texts.set(key, text);
  }
}

/** The offer of `ide.settings.set`: a related switch to turn on as well. */
export interface EnableOffer {
  /** The dotted key of the related switch, e.g. "traces.enabled". */
  key: string;
  /** The question to ask the user. */
  text: string;
}

/**
 * Read the offer from an `ide.settings.set` answer.
 *
 * @param result The answer.
 * @returns The offer, or null if the answer has none (or a malformed one).
 */
export function enableOffer(result: unknown): EnableOffer | null {
  if (typeof result !== 'object' || result === null) return null;
  const offer = (result as { offer?: unknown }).offer;
  if (typeof offer !== 'object' || offer === null) return null;
  const { key, text } = offer as { key?: unknown; text?: unknown };
  return typeof key === 'string' && key && typeof text === 'string' ? { key, text } : null;
}
