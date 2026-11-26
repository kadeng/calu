/**
 * DOM helpers of the chat webview: elements, inline SVG icons, buttons, and sections that are
 * rebuilt only when their signature changes.
 *
 *     el("div", "cls", "text", {attr})      create an element; text is always a text node
 *     icon("send")                          inline <svg> drawn with `currentColor`
 *     actionButton(label, action, data)     <button data-action=…> for the delegated click handler
 *     setSection(slot, signature, build)    replace the slot's content only if `signature` changed
 *
 * The page has a strict Content Security Policy: no inline styles and no inline scripts. All
 * styling is in `media/chat.css`; the few dynamic sizes are set through the CSSOM
 * (`element.style.x = …`), which the policy allows.
 */

import { neutraliseBidi } from '../src/shared/bidi';

/** Attributes whose value is shown to the user as text (tooltips, names, placeholders). */
const TEXT_ATTRIBUTES = new Set(['title', 'aria-label', 'placeholder', 'alt']);

/**
 * Prepare a text from calu for display: bidirectional control characters become visible
 * placeholders (`neutraliseBidi`), so that a text cannot be drawn in another order than it is
 * stored in. Every text node and tooltip of the chat goes through this function: `el()` applies
 * it, `markdown()` applies it to its source, and code that sets `textContent` or appends a
 * string itself calls it.
 *
 * @param text Any text.
 * @returns The text to show.
 */
export function shown(text: string): string {
  return neutraliseBidi(text);
}

/**
 * Create an element with optional class, text content and attributes.
 *
 * @param tag Tag name.
 * @param cls Class attribute ("" for none).
 * @param text Text content (inserted as text, never as HTML; passed through `shown`).
 * @param attrs Further attributes; `title`, `aria-label`, `placeholder` and `alt` are passed
 *   through `shown`, every other value is set as given (ids and data for the handlers).
 * @returns The new element.
 */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls = '',
  text?: string,
  attrs: Record<string, string> = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = shown(text);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, TEXT_ATTRIBUTES.has(k) ? shown(v) : v);
  return node;
}

/** Stroke paths of the icons, drawn in a 16x16 box. */
const ICONS = {
  send: 'M8 13V3M4 7l4-4 4 4',
  stop: 'M4.5 4.5h7v7h-7z',
  attach: 'M11 4.5v6a3 3 0 0 1-6 0V4a2 2 0 0 1 4 0v6a1 1 0 0 1-2 0V5',
  gear: 'M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
  chevronDown: 'M4 6l4 4 4-4',
  chevronRight: 'M6 4l4 4-4 4',
  check: 'M3.5 8.5l3 3 6-7',
  close: 'M4 4l8 8M12 4l-8 8',
  question: 'M6 6a2 2 0 1 1 3 1.7c-.6.4-1 .8-1 1.6M8 12v.5',
  undo: 'M4 7h5.5a3 3 0 0 1 0 6H6M4 7l3-3M4 7l3 3',
  fork: 'M5 3v3a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V3M8 8v5',
  copy: 'M5.5 5.5h7v8h-7zM3.5 10.5v-8h7',
  refresh: 'M13 8a5 5 0 1 1-1.5-3.5M13 3v2.5h-2.5',
  more: 'M4 8h.01M8 8h.01M12 8h.01',
  terminal: 'M3 4.5l3 3-3 3M8 11h5',
  edit: 'M3 13l1-3.5 7-7 2.5 2.5-7 7L3 13zM10 3.5l2.5 2.5',
  file: 'M4.5 2h5l2.5 2.5V14h-7.5zM9.5 2v2.5H12',
  tool: 'M10 2.5a3 3 0 0 0-3 4L2.5 11 5 13.5 9.5 9a3 3 0 0 0 4-3l-2 2-2-2 2-2a3 3 0 0 0-1.5-1.5z',
  agent: 'M8 2.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM3 13.5a5 5 0 0 1 10 0',
  arrowDown: 'M8 3v10M4 9l4 4 4-4',
  checklist: 'M2.5 4.5l1.5 1.5 2.5-3M2.5 10.5l1.5 1.5 2.5-3M9 5h4.5M9 11h4.5',
  book: 'M3 3h4a1.5 1.5 0 0 1 1 .5 1.5 1.5 0 0 1 1-.5h4v9.5H9a1.5 1.5 0 0 0-1 .5 1.5 1.5 0 0 0-1-.5H3zM8 3.5V13',
  shield: 'M8 2l5 2v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V4z',
  lock: 'M4.5 7.5h7v6h-7zM6 7.5V5a2 2 0 0 1 4 0v2.5',
  plus: 'M8 3v10M3 8h10',
  side: 'M3 3.5h10v7H8l-3 2.5v-2.5H3z',
  thinking: 'M5.5 12.5h5M6.5 14h3M8 2a4 4 0 0 0-2.5 7.1c.5.4.8 1 .8 1.6h3.4c0-.6.3-1.2.8-1.6A4 4 0 0 0 8 2z',
  pause: 'M5.5 3.5v9M10.5 3.5v9',
  play: 'M5 3.5l7 4.5-7 4.5z',
  warning: 'M8 2.5l6 10.5H2zM8 6.5v3M8 11.5v.5',
} as const;

/** Name of an icon of `icon()`. */
export type IconName = keyof typeof ICONS;

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Build an inline SVG icon (16x16, stroked with the current text colour, hidden from screen
 * readers).
 *
 * @param name The icon.
 * @param cls Extra class names.
 * @returns The `<svg>` element with the class `icon`.
 */
export function icon(name: IconName, cls = ''): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `icon${cls ? ` ${cls}` : ''}`);
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}

/**
 * Build calu's mark for the empty state: a ring of four segments with a check inside, drawn
 * with the current text colour.
 *
 * @returns The `<svg>` element with the class `logo`.
 */
export function logo(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 48 48');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'logo');
  const ring = document.createElementNS(SVG_NS, 'circle');
  ring.setAttribute('cx', '24');
  ring.setAttribute('cy', '24');
  ring.setAttribute('r', '18');
  ring.setAttribute('class', 'logo-ring');
  const check = document.createElementNS(SVG_NS, 'path');
  check.setAttribute('d', 'M16 24.5l6 6 11-13');
  check.setAttribute('class', 'logo-check');
  svg.append(ring, check);
  return svg;
}

/**
 * Create a button that the delegated click handler of `main.ts` handles.
 *
 * @param label Button text ("" for none).
 * @param action Value of `data-action`.
 * @param attrs Further attributes (e.g. `class`, `data-id`, `title`, `data-testid`).
 * @returns The button (`type="button"`).
 */
export function actionButton(label: string, action: string, attrs: Record<string, string> = {}): HTMLButtonElement {
  return el('button', '', label || undefined, { type: 'button', 'data-action': action, ...attrs });
}

/**
 * Create a button that shows only an icon; `label` is its accessible name and tooltip.
 *
 * @param name The icon.
 * @param label Accessible name, e.g. "Send".
 * @param action Value of `data-action`.
 * @param attrs Further attributes; `title` replaces the default tooltip (`label`).
 * @returns The button with the class `icon-button` (plus `attrs.class`).
 */
export function iconButton(name: IconName, label: string, action: string, attrs: Record<string, string> = {}): HTMLButtonElement {
  const b = actionButton('', action, { title: label, ...attrs, 'aria-label': label });
  b.classList.add('icon-button');
  b.append(icon(name));
  return b;
}

/**
 * Create a link-like element that the delegated click handler handles (no navigation).
 *
 * @param text Link text.
 * @param action Value of `data-action`.
 * @param attrs Further attributes.
 * @returns An `<a href="#">` with the class `link`.
 */
export function actionLink(text: string, action: string, attrs: Record<string, string> = {}): HTMLAnchorElement {
  const a = el('a', 'link', text, { href: '#', 'data-action': action, ...attrs });
  if (attrs.class) a.className = `link ${attrs.class}`;
  return a;
}

const sectionSignatures = new WeakMap<HTMLElement, string>();

/**
 * Replace the content of a slot if its signature changed since the last call. Unchanged
 * sections keep their elements, so an open `<select>`, the focus and the scroll position in
 * them survive the frequent renders while Claude streams text.
 *
 * @param slot The container.
 * @param signature Any string that changes whenever the content would change.
 * @param build Builds the new children; called only when needed.
 * @returns True if the content was rebuilt.
 */
export function setSection(slot: HTMLElement, signature: string, build: () => (Node | string)[]): boolean {
  if (sectionSignatures.get(slot) === signature) return false;
  sectionSignatures.set(slot, signature);
  slot.replaceChildren(...build());
  return true;
}

/**
 * Check whether a container already holds exactly the given elements, in order.
 *
 * @param container The container.
 * @param wanted The elements it should hold.
 * @returns True if no DOM change is needed.
 */
export function sameChildren(container: HTMLElement, wanted: readonly Element[]): boolean {
  const children = container.children;
  if (children.length !== wanted.length) return false;
  for (let i = 0; i < wanted.length; i++) if (children[i] !== wanted[i]) return false;
  return true;
}

/**
 * Copy text to the clipboard.
 *
 * @param text The text.
 * @returns A promise that resolves to true if the text was copied (false if the browser
 *   refused, e.g. because the view does not have the focus).
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * Scroll one container so that an element inside it is visible, and nothing else.
 *
 * `Element.scrollIntoView` also scrolls every ancestor, the frames around the webview included:
 * VSCode's own view then shifts by a few pixels. This function changes only `container`.
 *
 * @param container The scrolling ancestor of `target` (it scrolls vertically, horizontally or both).
 * @param target The element to show; nothing happens if it is not inside `container`.
 * @param block "nearest" scrolls as little as needed; "center" puts the element in the middle
 *   of the container's height.
 * @returns Nothing.
 */
export function reveal(container: HTMLElement, target: Element | null | undefined, block: 'nearest' | 'center' = 'nearest'): void {
  if (!target || !container.contains(target)) return;
  const box = container.getBoundingClientRect();
  const rect = target.getBoundingClientRect();
  if (block === 'center') {
    container.scrollTop += rect.top - box.top - (container.clientHeight - rect.height) / 2;
  } else if (rect.top < box.top || rect.height > container.clientHeight) {
    container.scrollTop += rect.top - box.top;
  } else if (rect.bottom > box.bottom) {
    container.scrollTop += rect.bottom - box.bottom;
  }
  if (rect.left < box.left) container.scrollLeft += rect.left - box.left;
  else if (rect.right > box.right) container.scrollLeft += rect.right - box.right;
}
