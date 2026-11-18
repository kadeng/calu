/**
 * Bidirectional control characters in untrusted text (pure: no `vscode`, no DOM).
 *
 * Unicode has invisible characters that change the order in which the following text is drawn.
 * A command, a file name or a checker's reason that contains one can be displayed in another
 * order than it is stored and run in, so that the user approves something else than they read:
 *
 *     stored:   "rm -rf <RLO>gnp.tropxe"      drawn without this module:   "rm -rf export.png"
 *     shown by this module:                   "rm -rf ⟦RLO⟧gnp.tropxe"
 *
 * `neutraliseBidi` replaces each of them by a visible name in white square brackets. Letters of
 * right-to-left scripts (Arabic, Hebrew) are not touched: the browser orders them by the Unicode
 * algorithm as usual.
 */

/** The replaced characters by code point, with the name that is shown for each. */
export const BIDI_CONTROLS: Readonly<Record<number, string>> = {
  0x061c: 'ALM',
  0x200e: 'LRM',
  0x200f: 'RLM',
  0x202a: 'LRE',
  0x202b: 'RLE',
  0x202c: 'PDF',
  0x202d: 'LRO',
  0x202e: 'RLO',
  0x2066: 'LRI',
  0x2067: 'RLI',
  0x2068: 'FSI',
  0x2069: 'PDI',
};

const BIDI_PATTERN = /[؜‎‏‪-‮⁦-⁩]/g;

/**
 * Replace every bidirectional control character of a text by a visible placeholder.
 *
 * @param text Any text (untrusted).
 * @returns The text with U+061C, U+200E, U+200F, U+202A to U+202E and U+2066 to U+2069 each
 *   replaced by "⟦NAME⟧" (`BIDI_CONTROLS`); the same string if it contains none.
 *
 * @example neutraliseBidi("a‮b") === "a⟦RLO⟧b"
 */
export function neutraliseBidi(text: string): string {
  return text.replace(BIDI_PATTERN, (c) => `⟦${BIDI_CONTROLS[c.codePointAt(0) ?? 0] ?? '?'}⟧`);
}

/**
 * Tell whether a text contains a bidirectional control character.
 *
 * @param text Any text.
 * @returns True if `neutraliseBidi` would change it.
 */
export function hasBidiControls(text: string): boolean {
  return text.search(BIDI_PATTERN) >= 0;
}
