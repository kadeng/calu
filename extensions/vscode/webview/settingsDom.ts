/**
 * DOM helpers of the settings webview: elements, inline SVG icons, highlighted text, a radio
 * group with arrow keys, and the model picker (a select of known models plus a custom field).
 * Runs in the webview only. All texts are inserted as text nodes; nothing here uses `innerHTML`.
 *
 *     modelPicker:   [ select: (default) | known models… | Custom… ]  [ text input, shown for Custom ]
 *                      change ─► commit(value)                          change / Enter ─► commit(text)
 */

import { highlightRanges } from '../src/shared/settingsView';

/** Attributes for `el`: plain attributes; `testid` becomes `data-testid`. */
export type Attrs = Record<string, string | undefined>;

/**
 * Create an element.
 *
 * @param tag Tag name.
 * @param cls Class attribute ("" for none).
 * @param text Text content (inserted as text, never as HTML); "" for none.
 * @param attrs Attributes; entries with the value `undefined` are skipped; `testid` sets
 *   `data-testid`.
 * @returns The new element.
 */
export function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = '', attrs: Attrs = {}): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined) continue;
    node.setAttribute(name === 'testid' ? 'data-testid' : name, value);
  }
  return node;
}

/**
 * Append children to an element.
 *
 * @param parent The element.
 * @param children Nodes or strings (text nodes); `null`, `undefined` and `false` are skipped.
 * @returns `parent`.
 */
export function add<T extends Node>(parent: T, ...children: (Node | string | null | undefined | false)[]): T {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return parent;
}

/** Stroke paths of the icons, drawn in a 16 × 16 box. */
const ICONS = {
  check: ['M3 8.5l3.2 3.2L13 4.8'],
  warning: ['M8 2.2L14.5 13.5H1.5Z', 'M8 6.5v3.2', 'M8 11.5v.3'],
  info: ['M8 14A6 6 0 108 2a6 6 0 000 12z', 'M8 7.3v3.9', 'M8 4.8v.3'],
  error: ['M8 14A6 6 0 108 2a6 6 0 000 12z', 'M5.8 5.8l4.4 4.4', 'M10.2 5.8l-4.4 4.4'],
  search: ['M7 11.2A4.2 4.2 0 107 2.8a4.2 4.2 0 000 8.4z', 'M10.2 10.2L14 14'],
  close: ['M4 4l8 8', 'M12 4l-8 8'],
  copy: ['M5.5 5.5h7v8h-7z', 'M3.5 10.5v-8h7'],
  edit: ['M3 13l.6-2.8 7.3-7.3 2.2 2.2-7.3 7.3z'],
  reset: ['M3.5 6.5h6a3 3 0 010 6H6', 'M6 4L3.5 6.5 6 9'],
  terminal: ['M2.5 3.5h11v9h-11z', 'M5 6.5l2 1.75-2 1.75', 'M8.5 10.5H11'],
  refresh: ['M13 8a5 5 0 11-1.6-3.7', 'M13 3v3h-3'],
  chevron: ['M6 4l4 4-4 4'],
  arrow: ['M4 8h8', 'M9 5l3 3-3 3'],
  filter: ['M2.5 4h11', 'M4.5 8h7', 'M6.5 12h3'],
} as const;

/** Name of an icon. */
export type IconName = keyof typeof ICONS;

/**
 * Create an inline SVG icon (decorative: hidden from assistive technology).
 *
 * @param name The icon.
 * @param cls Extra class names ("" for none).
 * @returns An `<svg class="icon …" aria-hidden="true">` that takes the text colour.
 */
export function icon(name: IconName, cls = ''): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', `icon icon-${name}${cls ? ` ${cls}` : ''}`);
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const d of ICONS[name]) {
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  }
  return svg;
}

/**
 * Create a button.
 *
 * @param label The visible text ("" for an icon-only button; pass `aria-label` in `attrs` then).
 * @param cls Class names, e.g. "btn primary".
 * @param onClick Called on click and on Enter or Space.
 * @param attrs Further attributes (see `el`).
 * @param iconName Optional icon shown before the text.
 * @returns The `<button type="button">`.
 */
export function button(label: string, cls: string, onClick: () => void, attrs: Attrs = {}, iconName?: IconName): HTMLButtonElement {
  const node = el('button', cls, '', { type: 'button', ...attrs });
  if (iconName) node.appendChild(icon(iconName));
  if (label) node.appendChild(el('span', 'btn-text', label));
  node.addEventListener('click', onClick);
  return node;
}

/**
 * Replace the content of an element with a text in which the search matches are marked.
 *
 * @param target The element; its children are removed.
 * @param text The text to show.
 * @param search The search text ("" marks nothing); see `highlightRanges`.
 * @returns Nothing.
 */
export function setHighlighted(target: HTMLElement, text: string, search: string): void {
  const ranges = search ? highlightRanges(text, search) : [];
  if (ranges.length === 0) {
    target.textContent = text;
    return;
  }
  target.textContent = '';
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) target.appendChild(document.createTextNode(text.slice(at, start)));
    target.appendChild(el('mark', 'hit', text.slice(start, end)));
    at = end;
  }
  if (at < text.length) target.appendChild(document.createTextNode(text.slice(at)));
}

/**
 * Make a list of elements behave as one radio group (WAI-ARIA radio pattern with a roving
 * tab stop). The elements need `role="radio"`; this function sets `aria-checked` and `tabindex`.
 *
 * @param radios The radios in visual order.
 * @param checked Index of the checked radio, or -1 for none (the first one then takes the tab stop).
 * @param onSelect Called with the index when the user selects a radio: by click, Space or
 *   Enter, and by an arrow key if `selectOnArrow` is true.
 * @param selectOnArrow True: arrow keys move the focus and select. False: they only move the
 *   focus (for groups where a selection is saved at once).
 * @returns A function that marks another radio as checked (-1 for none) without calling `onSelect`.
 */
export function wireRadios(
  radios: readonly HTMLElement[],
  checked: number,
  onSelect: (index: number) => void,
  selectOnArrow: boolean,
): (index: number) => void {
  /**
   * Set `aria-checked` and the tab stop.
   *
   * @param index The checked radio, or -1.
   * @returns Nothing.
   */
  const mark = (index: number): void => {
    radios.forEach((radio, i) => {
      radio.setAttribute('aria-checked', i === index ? 'true' : 'false');
      radio.tabIndex = i === (index >= 0 ? index : 0) ? 0 : -1;
    });
  };
  mark(checked);
  radios.forEach((radio, i) => {
    radio.addEventListener('click', () => {
      if (radio.getAttribute('aria-disabled') === 'true') return;
      onSelect(i);
    });
    radio.addEventListener('keydown', (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      let target = -1;
      if (step !== undefined) target = (i + step + radios.length) % radios.length;
      else if (event.key === 'Home') target = 0;
      else if (event.key === 'End') target = radios.length - 1;
      else if (event.key === ' ' || event.key === 'Enter') {
        event.preventDefault();
        if (radio.getAttribute('aria-disabled') !== 'true') onSelect(i);
        return;
      } else return;
      event.preventDefault();
      radios.forEach((other, j) => (other.tabIndex = j === target ? 0 : -1));
      radios[target].focus();
      if (selectOnArrow && radios[target].getAttribute('aria-disabled') !== 'true') onSelect(target);
    });
  });
  return mark;
}

/** One known model of a `modelPicker`. */
export interface PickerChoice {
  /** The value that is committed. */
  value: string;
  /** The text of the option. */
  label: string;
  /** Heading the option is listed under ("" or absent: no heading). */
  group?: string;
}

/** Options of `modelPicker`. */
export interface PickerOptions {
  /** `id` of the select (for `<label for>`); the custom field gets `<id>-custom`. */
  id: string;
  /** `data-testid` of the select; the custom field gets `<testid>-custom`. */
  testid: string;
  /** The current value ("" = none). */
  value: string;
  choices: readonly PickerChoice[];
  /** Text of the option for the empty value, e.g. "Default → model"; `null`: no such option. */
  emptyLabel: string | null;
  /** Accessible name of the custom field. */
  customLabel: string;
  /** `aria-describedby` of both controls. */
  describedBy?: string;
  /** Called with the chosen value (trimmed; "" for the empty option) when the user commits. */
  onCommit(value: string): void;
}

/** What `modelPicker` returns. */
export interface Picker {
  /** The wrapper to insert. */
  el: HTMLElement;
  /** The select (the element that takes the focus and the label). */
  select: HTMLSelectElement;
  /** The custom text field (hidden unless "Custom…" is selected). */
  input: HTMLInputElement;
  /** Read the value the controls show (the custom text while "Custom…" is selected). */
  read(): string;
  /** Show a value without calling `onCommit`. */
  write(value: string): void;
}

/** Value of the option "Custom…" (not a possible model string). */
const CUSTOM = '\u0001custom';

/**
 * Create a control that chooses a model string: a select of the known models with an option
 * "Custom…" that shows a text field for any other string.
 *
 * @param options See `PickerOptions`.
 * @returns The picker. `onCommit` is called when a known model is selected, and when the
 *   custom field loses the focus or Enter is pressed in it; selecting "Custom…" alone commits
 *   nothing. Escape in the custom field restores the last written value.
 */
export function modelPicker(options: PickerOptions): Picker {
  const wrapper = el('div', 'picker');
  const select = el('select', 'select', '', { id: options.id, testid: options.testid, 'aria-describedby': options.describedBy });
  const input = el('input', 'input picker-custom', '', {
    type: 'text',
    id: `${options.id}-custom`,
    testid: `${options.testid}-custom`,
    'aria-label': options.customLabel,
    'aria-describedby': options.describedBy,
    placeholder: 'provider:model',
    spellcheck: 'false',
    autocomplete: 'off',
  });
  if (options.emptyLabel !== null) select.appendChild(new Option(options.emptyLabel, ''));
  const groups = new Map<string, HTMLOptGroupElement>();
  for (const choice of options.choices) {
    const option = new Option(choice.label, choice.value);
    if (!choice.group) {
      select.appendChild(option);
      continue;
    }
    let group = groups.get(choice.group);
    if (!group) {
      group = el('optgroup', '', '', { label: choice.group });
      groups.set(choice.group, group);
      select.appendChild(group);
    }
    group.appendChild(option);
  }
  select.appendChild(new Option('Custom…', CUSTOM));
  let written = options.value;

  /**
   * Tell whether the select has an option for a value.
   *
   * @param value The value.
   * @returns True if a known model or the empty option has it.
   */
  const known = (value: string): boolean =>
    value === '' ? options.emptyLabel !== null : options.choices.some((choice) => choice.value === value);

  /**
   * Show a value in both controls.
   *
   * @param value The value.
   * @returns Nothing.
   */
  const write = (value: string): void => {
    written = value;
    if (known(value)) {
      select.value = value;
      input.hidden = true;
      input.value = '';
    } else {
      select.value = CUSTOM;
      input.hidden = false;
      if (input.value !== value) input.value = value;
    }
  };

  /**
   * Read the shown value.
   *
   * @returns The custom text (trimmed) while "Custom…" is selected, else the selected value.
   */
  const read = (): string => (select.value === CUSTOM ? input.value.trim() : select.value);

  select.addEventListener('change', () => {
    if (select.value === CUSTOM) {
      input.hidden = false;
      input.focus();
      return;
    }
    input.hidden = true;
    options.onCommit(select.value);
  });
  input.addEventListener('change', () => options.onCommit(input.value.trim()));
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      options.onCommit(input.value.trim());
    } else if (event.key === 'Escape') {
      write(written);
      select.focus();
    }
  });
  write(options.value);
  add(wrapper, select, input);
  return { el: wrapper, select, input, read, write };
}

/**
 * Set the value of a text control and keep the caret where it was if the control has the focus.
 *
 * @param control An input or textarea.
 * @param text The new content.
 * @returns Nothing. Does nothing if the content already equals `text`.
 */
export function writeText(control: HTMLInputElement | HTMLTextAreaElement, text: string): void {
  if (control.value === text) return;
  const focused = document.activeElement === control;
  let start: number | null = null;
  let end: number | null = null;
  if (focused) {
    try {
      start = control.selectionStart;
      end = control.selectionEnd;
    } catch {
      // Inputs of the type "number" have no selection.
    }
  }
  control.value = text;
  if (focused && start !== null && end !== null) {
    try {
      control.setSelectionRange(Math.min(start, text.length), Math.min(end, text.length));
    } catch {
      // See above.
    }
  }
}

/**
 * Remember which control has the focus, so that it can be given back after a part of the page
 * was rebuilt.
 *
 * @param within Only a focused element inside this element is remembered.
 * @returns A function that, called after the rebuild, focuses the element with the same
 *   `data-testid` inside `within` and restores a text selection. It does nothing if no element
 *   in `within` had the focus or the element no longer exists.
 */
export function keepFocus(within: HTMLElement): () => void {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !within.contains(active) || !active.dataset.testid) return () => {};
  const testid = active.dataset.testid;
  let range: [number, number] | undefined;
  if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
    try {
      if (active.selectionStart !== null && active.selectionEnd !== null) range = [active.selectionStart, active.selectionEnd];
    } catch {
      // No selection for this input type.
    }
  }
  return () => {
    const next = [...within.querySelectorAll<HTMLElement>('[data-testid]')].find((node) => node.dataset.testid === testid);
    if (!next) return;
    next.focus({ preventScroll: true });
    if (range && (next instanceof HTMLInputElement || next instanceof HTMLTextAreaElement)) {
      try {
        next.setSelectionRange(range[0], range[1]);
      } catch {
        // No selection for this input type.
      }
    }
  };
}
