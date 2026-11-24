/**
 * Dialogs of the browser pane: what VSCode's input boxes, modal messages, notifications and
 * QuickPick are in the extension. Plain DOM, styled by `pane.css` with the theme variables of
 * `theme.css`. Every text is put in as a text node; only `showText` with `html` takes markup,
 * and its callers pass the output of the chat's Markdown renderer (raw HTML disabled).
 *
 *     confirm(question, detail, button)  modal; resolves true for the button
 *     input(options)                      modal with one field; resolves the text or undefined
 *     choose(title, rows)                 modal list with a filter; resolves the chosen row
 *     notify(level, text, ...buttons)     a toast in the corner; resolves the clicked button
 *     showText(title, body)               modal with read-only text (log, context, file, diff)
 *
 * At most one modal is open; a second one waits until the first is closed. Escape cancels.
 */

import type { InputOptions } from '../src/host/sessions';

/** One row of `choose`. */
export interface ChoiceRow<T> {
  /** Main text; a row without `value` is a heading. */
  label: string;
  description?: string;
  detail?: string;
  value?: T;
  /** Label of a second button on the row (e.g. "Fork"); choosing it sets `secondary`. */
  secondary?: string;
}

let queue: Promise<unknown> = Promise.resolve();

/**
 * Run one modal after the previous one closed.
 *
 * @param build Builds the dialog content and returns the promise of its answer; `close` removes
 *   the dialog.
 * @returns The answer.
 */
function modal<T>(build: (box: HTMLElement, close: () => void) => Promise<T>): Promise<T> {
  const next = queue.then(() => {
    const backdrop = node('div', 'pane-backdrop');
    const box = node('div', 'pane-dialog');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    backdrop.append(box);
    document.body.append(backdrop);
    const close = (): void => backdrop.remove();
    return build(box, close).finally(close);
  });
  queue = next.catch(() => undefined);
  return next;
}

/**
 * Create an element with a class and an optional text.
 *
 * @param tag Tag name.
 * @param cls Class name ("" for none).
 * @param text Text content.
 * @returns The element.
 */
function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/**
 * Create a button.
 *
 * @param label Its text.
 * @param primary True for the main action.
 * @returns The button.
 */
function button(label: string, primary = false): HTMLButtonElement {
  const b = node('button', primary ? 'pane-button primary' : 'pane-button', label);
  b.type = 'button';
  return b;
}

/**
 * Ask a yes/no question.
 *
 * @param question The question (title of the dialog).
 * @param detail What answering yes does.
 * @param yes Label of the yes button.
 * @returns True if the user chose `yes`; false for Cancel or Escape.
 */
export function confirm(question: string, detail: string, yes: string): Promise<boolean> {
  return modal((box) => {
    box.append(node('h2', 'pane-title', question), node('p', 'pane-detail', detail));
    const ok = button(yes, true);
    const cancel = button('Cancel');
    box.append(row(ok, cancel));
    return new Promise<boolean>((resolve) => {
      ok.onclick = () => resolve(true);
      cancel.onclick = () => resolve(false);
      onEscape(box, () => resolve(false));
      ok.focus();
    });
  });
}

/**
 * Ask for a text.
 *
 * @param options Title, question, start value, masking and the check while typing.
 * @returns The text, or `undefined` for Cancel or Escape.
 */
export function input(options: InputOptions): Promise<string | undefined> {
  return modal((box) => {
    box.append(node('h2', 'pane-title', options.title), node('p', 'pane-detail', options.prompt));
    const field = node('input', 'pane-input');
    field.type = options.password ? 'password' : 'text';
    field.value = options.value ?? '';
    field.autocomplete = 'off';
    field.spellcheck = false;
    const problem = node('p', 'pane-problem');
    const ok = button('OK', true);
    const cancel = button('Cancel');
    box.append(field, problem, row(ok, cancel));
    const check = (): string | undefined => options.validate?.(field.value);
    const update = (): void => {
      const said = check();
      problem.textContent = said ?? '';
      ok.disabled = said !== undefined;
    };
    field.oninput = update;
    update();
    return new Promise<string | undefined>((resolve) => {
      const accept = (): void => {
        if (check() === undefined) resolve(field.value);
      };
      ok.onclick = accept;
      cancel.onclick = () => resolve(undefined);
      field.onkeydown = (event) => {
        if (event.key === 'Enter') accept();
      };
      onEscape(box, () => resolve(undefined));
      field.focus();
      field.select();
    });
  });
}

/**
 * Let the user choose a row, with a filter field above the list.
 *
 * @param title Title of the dialog.
 * @param rows The rows; rows without `value` are headings.
 * @returns The chosen value and whether the row's second button was used; `undefined` for
 *   Cancel or Escape.
 */
export function choose<T>(title: string, rows: ChoiceRow<T>[]): Promise<{ value: T; secondary: boolean } | undefined> {
  return modal((box) => {
    box.classList.add('wide');
    const filter = node('input', 'pane-input');
    filter.placeholder = 'Type to filter';
    const list = node('div', 'pane-list');
    const cancel = button('Cancel');
    box.append(node('h2', 'pane-title', title), filter, list, row(cancel));
    return new Promise<{ value: T; secondary: boolean } | undefined>((resolve) => {
      const render = (): void => {
        const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
        list.replaceChildren();
        for (const r of rows) {
          if (r.value === undefined) {
            list.append(node('div', 'pane-heading', r.label));
            continue;
          }
          const text = `${r.label} ${r.description ?? ''} ${r.detail ?? ''}`.toLowerCase();
          if (!words.every((w) => text.includes(w))) continue;
          const item = node('div', 'pane-row');
          const main = node('button', 'pane-row-main');
          main.type = 'button';
          main.append(node('span', 'pane-row-label', r.label));
          if (r.description) main.append(node('span', 'pane-row-description', r.description));
          if (r.detail) main.append(node('span', 'pane-row-detail', r.detail));
          const value = r.value;
          main.onclick = () => resolve({ value, secondary: false });
          item.append(main);
          if (r.secondary) {
            const second = button(r.secondary);
            second.onclick = () => resolve({ value, secondary: true });
            item.append(second);
          }
          list.append(item);
        }
      };
      filter.oninput = render;
      filter.onkeydown = (event) => {
        if (event.key === 'Enter') list.querySelector<HTMLButtonElement>('.pane-row-main')?.click();
      };
      cancel.onclick = () => resolve(undefined);
      onEscape(box, () => resolve(undefined));
      render();
      filter.focus();
    });
  });
}

/**
 * Show read-only text.
 *
 * @param title Title of the dialog.
 * @param body `text`: shown as preformatted text with line numbers, `line` (1-based) marked
 *   and scrolled to; `diff`: a unified diff, without line numbers, with added, removed and
 *   hunk-header lines coloured; `html`: trusted markup from the chat's Markdown renderer.
 * @param actions Further buttons with what they do (the dialog stays open).
 * @returns Resolves when the dialog was closed.
 */
export function showText(
  title: string,
  body: { text: string; line?: number } | { diff: string } | { html: string },
  actions: { label: string; run: () => void }[] = [],
): Promise<void> {
  return modal((box) => {
    box.classList.add('wide');
    box.append(node('h2', 'pane-title', title));
    let target: HTMLElement | undefined;
    if ('html' in body) {
      const content = node('div', 'pane-text markdown-body');
      content.innerHTML = body.html;
      box.append(content);
    } else if ('diff' in body) {
      const pre = node('pre', 'pane-text diff');
      for (const line of body.diff.replace(/\n$/, '').split('\n')) {
        const kind = /^(\+\+\+|---) /.test(line) ? 'file' : line.startsWith('@@') ? 'hunk' : line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : '';
        pre.append(node('span', `pane-line ${kind}`.trim(), line || ' '));
      }
      box.append(pre);
    } else {
      // One row per line, with its number in a column that is not selected with the text.
      const pre = node('pre', 'pane-text numbered');
      // A final newline ends the last line; it starts no empty one.
      body.text.replace(/\n$/, '').split('\n').forEach((line, index) => {
        const row = node('span', index + 1 === body.line ? 'pane-line marked' : 'pane-line');
        row.append(node('span', 'pane-number', String(index + 1)), node('span', 'pane-code', line || ' '));
        if (index + 1 === body.line) target = row;
        pre.append(row);
      });
      box.append(pre);
    }
    const close = button('Close', true);
    const buttons = actions.map((a) => {
      const b = button(a.label);
      b.onclick = a.run;
      return b;
    });
    box.append(row(close, ...buttons));
    return new Promise<void>((resolve) => {
      close.onclick = () => resolve();
      onEscape(box, () => resolve());
      close.focus();
      target?.scrollIntoView({ block: 'center' });
    });
  });
}

/**
 * Show a toast in the corner, optionally with buttons.
 *
 * @param level "info", "warning" or "error".
 * @param text The message.
 * @param buttons Button labels.
 * @returns The clicked button; `undefined` when the toast was closed or timed out (a toast
 *   without buttons closes after 8 s, one with buttons after 60 s).
 */
export function notify(level: 'info' | 'warning' | 'error', text: string, ...buttons: string[]): Promise<string | undefined> {
  let stack = document.querySelector<HTMLElement>('.pane-toasts');
  if (!stack) {
    stack = node('div', 'pane-toasts');
    stack.setAttribute('role', 'status');
    document.body.append(stack);
  }
  const toast = node('div', `pane-toast ${level}`);
  toast.append(node('span', 'pane-toast-text', text.replace(/^calu: /, '')));
  const close = node('button', 'pane-toast-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  stack.append(toast);
  return new Promise<string | undefined>((resolve) => {
    const done = (answer: string | undefined): void => {
      toast.remove();
      resolve(answer);
    };
    for (const label of buttons) {
      const b = button(label);
      b.onclick = () => done(label);
      toast.append(b);
    }
    toast.append(close);
    close.onclick = () => done(undefined);
    setTimeout(() => done(undefined), buttons.length > 0 ? 60_000 : 8_000);
  });
}

/**
 * Put buttons into a row.
 *
 * @param buttons The buttons.
 * @returns The row.
 */
function row(...buttons: HTMLElement[]): HTMLElement {
  const r = node('div', 'pane-buttons');
  r.append(...buttons);
  return r;
}

/**
 * Call `cancel` when Escape is pressed inside a dialog.
 *
 * @param box The dialog.
 * @param cancel What Escape does.
 * @returns Nothing.
 */
function onEscape(box: HTMLElement, cancel: () => void): void {
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      cancel();
    }
  });
}
