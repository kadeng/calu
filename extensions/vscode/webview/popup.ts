/**
 * The one floating panel of the chat webview: the session dropdown, the context menu of a tool
 * row, the honesty menu and the usage popover all use it. At most one is open.
 *
 *     openPopup(id, content, place)   append to <body>, position inside the window, focus the first item
 *     keys inside:  ArrowDown/ArrowUp/Home/End move between the items, Escape closes and gives
 *                   the focus back, Tab closes and lets the focus move on
 *     pointer down outside            closes
 *     refreshPopup(id, content)       replace the content of the open popup (state changed),
 *                                     keeping the focused item's position
 *
 * Items are the enabled buttons and links inside the content. The content's `role` ("menu",
 * "dialog") and labels are set by the caller.
 */

import { el } from './dom';

/** Where a popup opens. */
export type PopupPlace =
  /** Below (or, if there is no room, above) an element, left edges aligned. */
  | { anchor: Element }
  /** At a point in client pixels (context menus). */
  | { x: number; y: number };

let open: { id: string; box: HTMLElement; returnFocus: HTMLElement | null; place: PopupPlace } | undefined;

/**
 * Return the id of the open popup.
 *
 * @returns The id passed to `openPopup`, or `undefined` if none is open.
 */
export function openPopupId(): string | undefined {
  return open?.id;
}

/**
 * List the focusable items of a popup.
 *
 * @param box The popup element.
 * @returns Its enabled buttons, links and form controls, in document order.
 */
function items(box: HTMLElement): HTMLElement[] {
  return [...box.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], select, input')];
}

/**
 * Put the popup inside the window next to its anchor or point. Side effect: sets `left` and
 * `top` through the CSSOM.
 *
 * @param box The popup element (already in the document).
 * @param place Where it belongs.
 * @returns Nothing.
 */
function position(box: HTMLElement, place: PopupPlace): void {
  const margin = 4;
  const width = box.offsetWidth;
  const height = box.offsetHeight;
  let x: number;
  let y: number;
  if ('anchor' in place) {
    const rect = place.anchor.getBoundingClientRect();
    x = rect.left;
    const below = window.innerHeight - rect.bottom;
    y = below >= height + margin || below >= rect.top ? rect.bottom + 2 : rect.top - height - 2;
  } else {
    x = place.x;
    y = place.y;
  }
  box.style.left = `${Math.max(margin, Math.min(x, window.innerWidth - width - margin))}px`;
  box.style.top = `${Math.max(margin, Math.min(y, window.innerHeight - height - margin))}px`;
}

/**
 * Open a popup. An open popup is closed first (without giving the focus back).
 *
 * @param id Name of the popup, e.g. "sessions" (see `openPopupId`).
 * @param content The panel's content: an element with a `role` and an accessible name.
 * @param place Where it opens.
 * @param focusFirst False to leave the focus where it is (default: focus the first item).
 * @returns Nothing. Side effect: appends the popup to `<body>`.
 */
export function openPopup(id: string, content: HTMLElement, place: PopupPlace, focusFirst = true): void {
  closePopup(false);
  const box = el('div', 'popup', undefined, { 'data-popup': id });
  box.append(content);
  box.addEventListener('keydown', onKeydown);
  const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.append(box);
  open = { id, box, returnFocus, place };
  position(box, place);
  if ('anchor' in place) place.anchor.setAttribute('aria-expanded', 'true');
  if (focusFirst) {
    const all = items(box);
    (all.find((i) => i.getAttribute('aria-current') === 'true' || i.getAttribute('aria-checked') === 'true') ?? all[0])?.focus();
  }
}

/**
 * Replace the content of the open popup if it has the given id; the item at the focused
 * position gets the focus again.
 *
 * @param id Name of the popup.
 * @param content The new content.
 * @param place The new place, if the anchor element was rebuilt; default: unchanged.
 * @returns True if the popup was open and was refreshed.
 */
export function refreshPopup(id: string, content: HTMLElement, place?: PopupPlace): boolean {
  if (!open || open.id !== id) return false;
  if (place) {
    open.place = place;
    if ('anchor' in place) place.anchor.setAttribute('aria-expanded', 'true');
  }
  const index = items(open.box).indexOf(document.activeElement as HTMLElement);
  open.box.replaceChildren(content);
  position(open.box, open.place);
  if (index >= 0) {
    const all = items(open.box);
    all[Math.min(index, all.length - 1)]?.focus();
  }
  return true;
}

/**
 * Close the open popup, if any.
 *
 * @param restoreFocus True to give the focus back to the element that had it before the popup
 *   opened (keyboard use); false when the focus moves elsewhere anyway.
 * @returns Nothing.
 */
export function closePopup(restoreFocus = true): void {
  if (!open) return;
  const { box, returnFocus, place } = open;
  open = undefined;
  box.remove();
  if ('anchor' in place) place.anchor.setAttribute('aria-expanded', 'false');
  if (restoreFocus && returnFocus?.isConnected) returnFocus.focus();
}

/**
 * Close the popup if a pointer event happened outside it (document listener).
 *
 * @param target The event's target.
 * @returns Nothing.
 */
export function closePopupIfOutside(target: EventTarget | null): void {
  if (!open || !(target instanceof Node)) return;
  if (open.box.contains(target)) return;
  // A click on the anchor is handled by the anchor's own action (it toggles the popup).
  if ('anchor' in open.place && open.place.anchor.contains(target)) return;
  closePopup(false);
}

/**
 * Keyboard handling inside the popup (see the module comment).
 *
 * @param event The keydown event.
 * @returns Nothing.
 */
function onKeydown(event: KeyboardEvent): void {
  if (!open) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closePopup();
    return;
  }
  if (event.key === 'Tab') {
    closePopup();
    return;
  }
  const target = event.target as HTMLElement;
  // Text fields and selects inside a popup keep their own arrow keys.
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement) return;
  const all = items(open.box);
  if (all.length === 0) return;
  const index = all.indexOf(document.activeElement as HTMLElement);
  const next =
    event.key === 'ArrowDown' ? (index + 1) % all.length
    : event.key === 'ArrowUp' ? (index - 1 + all.length) % all.length
    : event.key === 'Home' ? 0
    : event.key === 'End' ? all.length - 1
    : -1;
  if (next < 0) return;
  event.preventDefault();
  all[next].focus();
}
