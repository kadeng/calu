/**
 * The inline markup of calu's descriptions (pure: no DOM), for the settings dialog.
 *
 * calu writes its setting descriptions and setup advice as plain sentences with two marks:
 *
 *     `claude.model`      a setting key, a command or a value   -> code
 *     **never**           emphasis                              -> strong
 *
 * `inlineParts` splits a text into parts; the dialog builds one DOM node per part
 * (`setRich` in `webview/settingsDom.ts`) and never passes the text to an HTML parser, so a
 * description cannot inject markup.
 */

/** One part of a text: plain, code (was between backticks) or strong (was between `**`). */
export interface InlinePart {
  kind: 'text' | 'code' | 'strong';
  /** The part's text without its marks. Never empty. */
  text: string;
}

/**
 * Split a text at its inline marks.
 *
 *     "Set `a.b` to **true** now"  ->  text "Set ", code "a.b", text " to ", strong "true", text " now"
 *
 * Rules: a code span runs from a backtick to the next backtick on the same line and may contain
 * anything, also asterisks. A strong span runs from `**` to the next `**` on the same line; its
 * content must not start or end with a space and is not split further. A mark without its
 * partner, and an empty span, stay as they are written. Nothing nests.
 *
 * @param text Any text (untrusted).
 * @returns The parts in order; their texts joined give `text` without the marks of the
 *   recognised spans. Empty for "".
 *
 * @example inlineParts("a `b` c") // [{kind: "text", text: "a "}, {kind: "code", text: "b"}, {kind: "text", text: " c"}]
 */
export function inlineParts(text: string): InlinePart[] {
  const parts: InlinePart[] = [];
  const push = (kind: InlinePart['kind'], value: string): void => {
    if (!value) return;
    const last = parts[parts.length - 1];
    if (kind === 'text' && last?.kind === 'text') last.text += value;
    else parts.push({ kind, text: value });
  };
  const pattern = /`([^`\n]+)`|\*\*([^\s*](?:[^*\n]*[^\s*])?)\*\*/g;
  let at = 0;
  for (let m = pattern.exec(text); m !== null; m = pattern.exec(text)) {
    push('text', text.slice(at, m.index));
    if (m[1] !== undefined) push('code', m[1]);
    else push('strong', m[2]);
    at = m.index + m[0].length;
  }
  push('text', text.slice(at));
  return parts;
}

/**
 * Return a text without its inline marks (what a search or a tooltip should see).
 *
 * @param text Any text.
 * @returns The texts of `inlineParts(text)` joined.
 */
export function plainInline(text: string): string {
  return inlineParts(text)
    .map((part) => part.text)
    .join('');
}
