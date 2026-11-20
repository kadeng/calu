/**
 * Recording the user's own saves as diffs (agent/records/PLAN.md D253): the parts without
 * `vscode`, shared with the tests.
 *
 *     onWillSaveTextDocument ── gate.wanted()? ── read the file on disk ──► before (kept per uri)
 *     onDidSaveTextDocument  ── before + document text ──► humanDiffParams ──► session.call
 *                                                          human_diffs.add {path, before, after}
 *     answer {enabled: false} ──► gate.heard(false): nothing is sent for OFF_PAUSE_MS
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
