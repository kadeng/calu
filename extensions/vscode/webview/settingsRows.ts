/**
 * One setting of the settings dialog as a DOM row: label, key, description, the control for the
 * setting's kind, the default as a hint, "Reset to default" and the error line. Runs in the
 * webview. The row never talks to the host itself; it calls `RowHost`.
 *
 *     ┌ row ─────────────────────────────────────────────────────────────┐
 *     │▌Label   dotted.key   Modified                    [Reset to default]│   ▌ = modified bar
 *     │ description paragraphs                                            │
 *     │ [ control by kind ]                                               │
 *     │ Default: …                                                        │
 *     │ error line (height animated, announced politely)                  │
 *     └───────────────────────────────────────────────────────────────────┘
 *
 *     control changed ─► parseControlValue ─┬─ not ok ─► error line, nothing sent
 *                                           └─ ok and different ─► host.commit(key, value)
 *
 * When a value is saved: toggles, selects and pickers on change; one-line fields on blur or
 * Enter; multi-line fields on blur or Ctrl+Enter. Escape restores the stored value.
 */

import type { ModelPreset } from '../src/bridgeProtocol';
import { formatControlValue, parseControlValue, type SettingsRowView } from '../src/shared/settingsModel';
import { paragraphs, presetModel, sameJson } from '../src/shared/settingsView';
import { add, button, el, modelPicker, setHighlighted, writeText, type PickerChoice } from './settingsDom';

/** What a row needs from the dialog. */
export interface RowHost {
  /** Store a value (optimistic; the dialog updates the row and sends `set`). */
  commit(key: string, value: unknown): void;
  /** Remove the stored value (sends `unset`). */
  unset(key: string): void;
  /** The model presets, for rows of the kind "model". */
  presets(): readonly ModelPreset[];
  /** The model that is used when none is stored; "" if unknown. */
  defaultModel(): string;
  /** The project whose values are shown; `null` for the global scope. */
  project(): string | null;
  /** True while a save of the key is not answered yet. */
  isPending(key: string): boolean;
}

/** The control of a row, independent of its kind. */
interface RowControl {
  /** The element to insert. */
  el: HTMLElement;
  /** The element that takes the focus, the label and `data-testid="setting-<key>"`. */
  focus: HTMLElement;
  /** Read what the control shows: a boolean for a toggle, else text. */
  read(): string | boolean;
  /** Show a value (text as `formatControlValue` gives it; "true"/"false" for a toggle). */
  write(text: string): void;
  /** True if an entered text should survive a refused save (text fields; not toggles and selects). */
  keepsText: boolean;
}

/** A rendered row and what is needed to update it. */
export interface RowHandle {
  /** The view the row was last rendered from. */
  view: SettingsRowView;
  /** The row element (`data-key` holds the key). */
  el: HTMLElement;
  control: RowControl;
  label: HTMLElement;
  key: HTMLElement;
  desc: HTMLElement;
  note: HTMLElement;
  hint: HTMLElement;
  reset: HTMLButtonElement;
  errorWrap: HTMLElement;
  error: HTMLElement;
  /** The text last written to the control from a stored value. */
  synced: string;
  /** True while the control holds a text the host refused; a stored value does not overwrite it. */
  holdsRefused: boolean;
  /** The search text the label, key and description were last highlighted for. */
  painted: string;
}

/**
 * Build an element id from a setting's key.
 *
 * @param key The dotted key.
 * @returns "s-" plus the key with every character outside `A-Z a-z 0-9 _ -` replaced by "_".
 */
export function rowId(key: string): string {
  return `s-${key.replace(/[^A-Za-z0-9_-]/g, '_')}`;
}

/**
 * Build the choices of a model picker from the presets.
 *
 * @param presets The presets.
 * @returns One choice per preset that stores a model, grouped by provider; the label is the
 *   preset's label.
 */
export function presetChoices(presets: readonly ModelPreset[]): PickerChoice[] {
  return presets
    .filter((preset) => presetModel(preset) !== '')
    .map((preset) => ({ value: presetModel(preset), label: preset.label, group: preset.group }));
}

/**
 * Create the control of a row.
 *
 * @param view The row's view.
 * @param host The dialog (presets and default model for "model" rows).
 * @param onCommit Called when the user finished an edit (see the module comment).
 * @param onEdit Called on every change of the content before a commit (clears the error line).
 * @param onCancel Called on Escape (restores the stored value).
 * @returns The control. Its focus element has the id `rowId(key)` and
 *   `data-testid="setting-<key>"`.
 */
function createControl(view: SettingsRowView, host: RowHost, onCommit: () => void, onEdit: () => void, onCancel: () => void): RowControl {
  const id = rowId(view.key);
  const describedBy = `${id}-desc ${id}-hint ${id}-err`;
  const common = { id, testid: `setting-${view.key}`, 'aria-describedby': describedBy };
  switch (view.control) {
    case 'toggle': {
      const input = el('input', 'switch', '', { ...common, type: 'checkbox', role: 'switch' });
      input.addEventListener('change', onCommit);
      return {
        el: input,
        focus: input,
        read: () => input.checked,
        write: (text) => {
          input.checked = text === 'true';
        },
        keepsText: false,
      };
    }
    case 'select': {
      const select = el('select', 'select', '', common);
      for (const choice of view.choices) select.appendChild(new Option(choice === '' ? '(not set)' : choice, choice));
      select.addEventListener('change', onCommit);
      return {
        el: select,
        focus: select,
        read: () => select.value,
        write: (text) => {
          if (![...select.options].some((option) => option.value === text)) select.appendChild(new Option(text, text));
          select.value = text;
        },
        keepsText: false,
      };
    }
    case 'model': {
      const fallback = host.defaultModel();
      const picker = modelPicker({
        id,
        testid: `setting-${view.key}`,
        value: '',
        choices: presetChoices(host.presets()),
        emptyLabel: fallback ? `Default → ${fallback}` : 'Default',
        customLabel: `${view.label}: custom model`,
        describedBy,
        onCommit,
      });
      picker.input.addEventListener('input', onEdit);
      picker.input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') onCancel();
      });
      return { el: picker.el, focus: picker.select, read: picker.read, write: picker.write, keepsText: true };
    }
    case 'markdown':
    case 'list':
    case 'json': {
      const area = el('textarea', `textarea${view.control === 'markdown' ? '' : ' mono'}`, '', {
        ...common,
        rows: view.control === 'list' ? '3' : '5',
        spellcheck: view.control === 'markdown' ? 'true' : 'false',
      });
      area.addEventListener('change', onCommit);
      area.addEventListener('input', onEdit);
      area.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          onCommit();
        } else if (event.key === 'Escape') {
          onCancel();
        }
      });
      return { el: area, focus: area, read: () => area.value, write: (text) => writeText(area, text), keepsText: true };
    }
    default: {
      const number = view.control === 'number';
      const input = el('input', `input${number ? ' input-number' : ''}`, '', {
        ...common,
        type: 'text',
        inputmode: number ? (view.integer ? 'numeric' : 'decimal') : undefined,
        spellcheck: 'false',
        autocomplete: 'off',
      });
      input.addEventListener('change', onCommit);
      input.addEventListener('input', onEdit);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          onCommit();
        } else if (event.key === 'Escape') {
          onCancel();
        }
      });
      return { el: input, focus: input, read: () => input.value, write: (text) => writeText(input, text), keepsText: true };
    }
  }
}

/**
 * Describe where a row's value comes from.
 *
 * @param view The row's view.
 * @param project The shown project, or `null` for the global scope.
 * @returns "Modified" (global), "Set for this project", "Inherited from Global", or "".
 */
function noteText(view: SettingsRowView, project: string | null): string {
  if (view.modified) return project ? 'Set for this project' : 'Modified';
  return project && view.inherits === 'global' ? 'Inherited from Global' : '';
}

/**
 * Build the hint line of a row.
 *
 * @param view The row's view.
 * @returns "Default: <text>" (or "Default: (empty)"), followed by the save key for multi-line
 *   fields.
 */
function hintText(view: SettingsRowView): string {
  const shown = view.defaultText.trim() === '' || view.defaultText === '""' ? '(empty)' : view.defaultText.replace(/\s+/g, ' ');
  const multi = view.control === 'markdown' || view.control === 'list' || view.control === 'json';
  const how = view.control === 'list' ? ' · one entry per line' : view.control === 'json' ? ' · a JSON value' : '';
  return `Default: ${shown.length > 160 ? `${shown.slice(0, 160)}…` : shown}${how}${multi ? ' · Ctrl+Enter saves' : ''}`;
}

/**
 * Show a message in a row's error line, or remove it.
 *
 * @param handle The row.
 * @param message The message, shown verbatim; "" removes the error.
 * @returns Nothing. Side effects: sets `aria-invalid` on the control and opens or closes the
 *   error line (its height is animated in CSS, so the rows below do not jump).
 */
export function setRowError(handle: RowHandle, message: string): void {
  handle.error.textContent = message;
  handle.errorWrap.classList.toggle('shown', message !== '');
  handle.el.classList.toggle('invalid', message !== '');
  if (message) handle.control.focus.setAttribute('aria-invalid', 'true');
  else handle.control.focus.removeAttribute('aria-invalid');
}

/**
 * Highlight the search matches in a row's label, key and description.
 *
 * @param handle The row.
 * @param search The search text ("" removes the highlighting).
 * @returns Nothing. Does nothing if the row is already painted for this search.
 */
export function paintRow(handle: RowHandle, search: string): void {
  if (handle.painted === search) return;
  handle.painted = search;
  setHighlighted(handle.label, handle.view.label, search);
  setHighlighted(handle.key, handle.view.key, search);
  handle.desc.textContent = '';
  for (const paragraph of paragraphs(handle.view.description)) {
    const p = el('p');
    setHighlighted(p, paragraph, search);
    handle.desc.appendChild(p);
  }
}

/**
 * Check and save what a row's control shows.
 *
 * @param handle The row.
 * @param host The dialog.
 * @returns Nothing. Side effects: an input that `parseControlValue` refuses is reported in the
 *   error line and not sent; a value equal to the stored one only clears the error; any other
 *   value goes to `host.commit`.
 */
function commitRow(handle: RowHandle, host: RowHost): void {
  const parsed = parseControlValue(handle.view, handle.control.read());
  if (!parsed.ok) {
    setRowError(handle, parsed.message);
    return;
  }
  if (sameJson(parsed.value, handle.view.value)) {
    handle.holdsRefused = false;
    setRowError(handle, '');
    return;
  }
  handle.holdsRefused = false;
  setRowError(handle, '');
  host.commit(handle.view.key, parsed.value);
}

/**
 * Put the stored value back into a row's control and remove the error (Escape).
 *
 * @param handle The row.
 * @returns Nothing.
 */
function restoreRow(handle: RowHandle): void {
  handle.holdsRefused = false;
  handle.control.write(handle.synced);
  setRowError(handle, '');
}

/**
 * Create the DOM row of a setting.
 *
 * @param view The row's view.
 * @param host The dialog.
 * @returns The handle; `handle.el` is not inserted yet.
 */
export function createRow(view: SettingsRowView, host: RowHost): RowHandle {
  const id = rowId(view.key);
  const row = el('div', `row row-${view.control}`, '', { 'data-key': view.key, testid: `setting-row-${view.key}` });
  const label = el('label', 'row-label', '', { for: id, id: `${id}-label` });
  const key = el('code', 'row-key', '', { title: view.type || undefined });
  const note = el('span', 'row-note');
  const desc = el('div', 'row-desc', '', { id: `${id}-desc` });
  const hint = el('div', 'row-hint', '', { id: `${id}-hint` });
  const errorWrap = el('div', 'row-error-wrap');
  const error = el('div', 'row-error', '', { id: `${id}-err`, testid: `setting-error-${view.key}`, 'aria-live': 'polite' });
  errorWrap.appendChild(error);
  // The callbacks run after `handle` is assigned (they are event handlers).
  const reset = button('Reset to default', 'btn quiet row-reset', () => host.unset(handle.view.key), { testid: `setting-reset-${view.key}` }, 'reset');
  const control = createControl(
    view,
    host,
    () => commitRow(handle, host),
    () => {
      if (handle.error.textContent) setRowError(handle, '');
    },
    () => restoreRow(handle),
  );
  const handle: RowHandle = { view, el: row, control, label, key, desc, note, hint, reset, errorWrap, error, synced: '', holdsRefused: false, painted: '\u0000' };
  const head = add(el('div', 'row-head'), add(el('div', 'row-title'), label, key, note), handle.reset);
  const body = el('div', 'row-body');
  if (view.control === 'toggle') add(body, handle.control.el, desc);
  else add(body, handle.control.el);
  add(row, head, view.control === 'toggle' ? null : desc, body, hint, errorWrap);
  paintRow(handle, '');
  refreshRow(handle, host);
  return handle;
}

/**
 * Bring the changeable parts of a row in line with `handle.view`: the modified bar, the note,
 * the reset button, the hint, the saving state and the control's value.
 *
 * @param handle The row.
 * @param host The dialog.
 * @returns Nothing. The control's content is left alone while the user edits it (it has the
 *   focus and differs from the last stored value) and while it holds a refused text.
 */
function refreshRow(handle: RowHandle, host: RowHost): void {
  const { view } = handle;
  const project = host.project();
  handle.el.classList.toggle('modified', view.modified);
  handle.el.classList.toggle('saving', host.isPending(view.key));
  handle.note.textContent = noteText(view, project);
  handle.reset.hidden = !view.modified;
  const resetText = project ? 'Remove project value' : 'Reset to default';
  const resetLabel = handle.reset.querySelector('.btn-text');
  if (resetLabel) resetLabel.textContent = resetText;
  handle.reset.setAttribute('aria-label', `${resetText}: ${view.label}`);
  handle.hint.textContent = hintText(view);
  handle.hint.title = view.defaultText.length > 160 ? view.defaultText : '';
  const text = formatControlValue(view.control, view.value);
  const current = handle.control.read();
  const shown = typeof current === 'boolean' ? String(current) : current;
  const editing = handle.el.contains(document.activeElement) && handle.control.keepsText && shown !== handle.synced;
  if (!editing && !handle.holdsRefused && shown !== text) handle.control.write(text);
  handle.synced = text;
}

/**
 * Update a row after its setting changed (a `data`, `saved` or `error` message, or an
 * optimistic edit).
 *
 * @param handle The row.
 * @param view The new view of the same key.
 * @param host The dialog.
 * @returns The handle to keep: the same one, updated in place, or a new one whose element
 *   replaced the old element in the document (when the control kind or the choices changed).
 */
export function updateRow(handle: RowHandle, view: SettingsRowView, host: RowHost): RowHandle {
  const rebuilt = view.control !== handle.view.control || !sameJson(view.choices, handle.view.choices) || view.integer !== handle.view.integer;
  if (rebuilt) {
    const next = createRow(view, host);
    next.el.hidden = handle.el.hidden;
    handle.el.replaceWith(next.el);
    return next;
  }
  const textChanged = view.label !== handle.view.label || view.description !== handle.view.description;
  handle.view = view;
  if (textChanged) {
    const search = handle.painted;
    handle.painted = '\u0000';
    paintRow(handle, search);
  }
  refreshRow(handle, host);
  return handle;
}

/**
 * React to a refused save of a row.
 *
 * @param handle The row (its view already holds the rolled-back state).
 * @param message The host's message, shown verbatim.
 * @param attempted The text of the value that was refused, as `formatControlValue` gives it;
 *   `undefined` if unknown.
 * @param host The dialog.
 * @returns Nothing. A text field shows the refused text again (so that it can be corrected;
 *   Escape restores the stored value); a toggle or select goes back to the stored value.
 */
export function failRow(handle: RowHandle, message: string, attempted: string | undefined, host: RowHost): void {
  if (handle.control.keepsText && attempted !== undefined) {
    handle.holdsRefused = true;
    refreshRow(handle, host);
    handle.control.write(attempted);
  } else {
    handle.holdsRefused = false;
    refreshRow(handle, host);
  }
  setRowError(handle, message);
}
