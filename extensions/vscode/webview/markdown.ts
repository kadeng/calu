/**
 * Safe Markdown rendering of the chat webview.
 *
 *     text ──renderMarkdownHtml()──► HTML string        (pure; markdown-it, `html: false`)
 *          ──markdown()────────────► <div class="md">    + copy buttons on code blocks
 *                                                        + file links for known paths in `code`
 *                                                        + links for source references
 *                                                          (`src/app.py:12`, `sourceRefs.ts`)
 *
 * markdown-it runs with `html: false`, so raw HTML in the source is escaped; its link validator
 * rejects `javascript:`, `vbscript:`, `file:` and non-image `data:` URLs. Images are rendered as
 * plain links: loading remote images would let text from Claude or a checker send data to any
 * server (the webview's Content Security Policy blocks them as well). Links never navigate: the
 * click handler of `main.ts` turns a click into an `openLink` / `openFile` message.
 */

import MarkdownIt from 'markdown-it';
import { findSourceRefs, sourceRefHref } from '../src/shared/sourceRefs';
import { actionButton, el, icon, shown } from './dom';

const md = new MarkdownIt({ html: false, linkify: true, breaks: false });
// Only addresses with a scheme (https://...) become links: without one, linkify took file
// names such as "README.md", "lib.rs" or "hello.py" for web addresses (.md, .rs and .py are
// country domains). Such names become source references instead (`linkSourceRefs`).
md.linkify.set({ fuzzyLink: false });

/**
 * Render a Markdown image token as a link (markdown-it renderer rule).
 *
 * @param tokens The token stream; `tokens[idx]` is the image token.
 * @param idx Index of the image token.
 * @returns HTML of a link whose text is the image's alt text (escaped).
 */
md.renderer.rules.image = (tokens, idx) => {
  const token = tokens[idx];
  const src = String(token.attrGet('src') ?? '');
  const alt = String(token.content || src);
  return `<a href="${md.utils.escapeHtml(src)}">[image: ${md.utils.escapeHtml(alt)}]</a>`;
};

/**
 * Render Markdown to HTML.
 *
 * @param text Markdown source (untrusted).
 * @returns HTML in which every "<" of the source is escaped; images are links.
 */
export function renderMarkdownHtml(text: string): string {
  return md.render(text);
}

/** Finds the file a text names: the absolute path and an optional 1-based line. */
export type FileResolver = (text: string) => { path: string; line?: number } | undefined;

/**
 * Render Markdown into a new div element.
 *
 * Side effects on the new element only: every code block gets a "Copy" button
 * (`data-action="copyCode"`); every link gets its target as tooltip; with `resolveFile`, an
 * inline `code` span that names a known file becomes a link with `data-action="openFile"`,
 * `data-path` and `data-line`.
 *
 * @param text Markdown source (untrusted).
 * @param cls Class attribute of the div.
 * @param resolveFile Lookup of known files (see `lookupFile`); omit to leave code spans alone.
 * @returns The div with the rendered (escaped) HTML.
 */
export function markdown(text: string, cls: string, resolveFile?: FileResolver): HTMLElement {
  const div = el('div', cls);
  // Safe: markdown-it escapes raw HTML (`html: false`) and validates link targets.
  // Bidirectional control characters are made visible before rendering (`shown`), so they
  // cannot reorder a rendered command, path or link text.
  div.innerHTML = renderMarkdownHtml(shown(text));
  for (const pre of [...div.querySelectorAll('pre')]) {
    const wrap = el('div', 'code-block');
    pre.replaceWith(wrap);
    const copy = actionButton('', 'copyCode', { class: 'copy-button', title: 'Copy', 'aria-label': 'Copy code' });
    copy.append(icon('copy'));
    wrap.append(pre, copy);
  }
  for (const a of [...div.querySelectorAll('a')]) {
    a.title = a.getAttribute('href') ?? '';
  }
  if (resolveFile) linkFiles(div, resolveFile);
  linkSourceRefs(div);
  return div;
}

/**
 * Turn source references (`src/app.py:12`, `docs/x.md`; see `sourceRefs.ts`) into links, in
 * place: in plain text, and an inline `code` span whose whole text is one reference. Text
 * inside a link or a code block is left alone. A link's `href` is the path with ":<line>";
 * the click handler of `main.ts` sends it as `openLink`, and the host opens it only inside
 * the workspace folder.
 *
 * @param root The rendered Markdown.
 * @returns Nothing.
 */
export function linkSourceRefs(root: HTMLElement): void {
  for (const code of [...root.querySelectorAll('code')]) {
    if (code.closest('pre, a')) continue;
    const text = code.textContent ?? '';
    const [ref] = findSourceRefs(text);
    if (!ref || ref.start !== 0 || ref.end !== text.length) continue;
    const link = sourceLink(sourceRefHref(ref));
    code.replaceWith(link);
    link.append(code);
  }
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!(node.parentElement?.closest('a, pre, code'))) texts.push(node as Text);
  }
  for (const node of texts) {
    const text = node.data;
    const refs = findSourceRefs(text);
    if (!refs.length) continue;
    const parts: Node[] = [];
    let at = 0;
    for (const ref of refs) {
      if (ref.start > at) parts.push(root.ownerDocument.createTextNode(text.slice(at, ref.start)));
      const link = sourceLink(sourceRefHref(ref));
      link.textContent = text.slice(ref.start, ref.end);
      parts.push(link);
      at = ref.end;
    }
    if (at < text.length) parts.push(root.ownerDocument.createTextNode(text.slice(at)));
    node.replaceWith(...parts);
  }
}

/**
 * Create the link element of one source reference.
 *
 * @param href The path with an optional ":<line>".
 * @returns An `<a>` with the classes `link file-link` and the target as tooltip.
 */
function sourceLink(href: string): HTMLAnchorElement {
  const link = el('a', 'link file-link', undefined, { href, title: `Open ${href}` });
  return link as HTMLAnchorElement;
}

/**
 * Turn inline code spans that name a known file into file links (in place).
 *
 * @param root The rendered Markdown.
 * @param resolveFile Lookup of known files.
 * @returns Nothing.
 */
function linkFiles(root: HTMLElement, resolveFile: FileResolver): void {
  for (const code of [...root.querySelectorAll('code')]) {
    if (code.closest('pre, a')) continue;
    const text = code.textContent ?? '';
    if (text.length > 300 || /\s/.test(text)) continue;
    const file = resolveFile(text);
    if (!file) continue;
    const link = el('a', 'link file-link', undefined, {
      href: '#',
      'data-action': 'openFile',
      'data-path': file.path,
      title: file.line ? `Open file at line ${file.line}` : 'Open file',
    });
    if (file.line) link.dataset.line = String(file.line);
    code.replaceWith(link);
    link.append(code);
  }
}
