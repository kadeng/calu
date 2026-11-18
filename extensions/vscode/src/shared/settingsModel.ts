/**
 * Form model of the settings dialog: turns the rows of `ide.settings.get` into sections with
 * one control per setting, and converts between a control's text and the JSON value that
 * `ide.settings.set` takes. Pure (no `vscode`, no DOM); used by the settings webview.
 *
 *     ide.settings.get rows ──buildSections(rows, search)──► sections ──► rendered form
 *     user edits a control  ──parseControlValue(control, input)──► JSON value ──► `set` message
 *     ide.status findings   ──setupItems(findings)──► Setup card rows with fix buttons
 */

import type { Finding, SettingRow } from '../bridgeProtocol';

/**
 * The control a setting is edited with.
 *
 * "toggle": a checkbox (boolean). "select": one of `choices`. "number": an integer or a
 * decimal number. "text": one line. "model": one line holding a model string (the dialog may
 * offer the presets). "markdown": a multi-line text. "list": a multi-line text, one entry per
 * line. "json": a multi-line text holding any JSON value.
 */
export type ControlKind = 'toggle' | 'select' | 'number' | 'text' | 'model' | 'markdown' | 'list' | 'json';

/** One setting as the form shows it. */
export interface SettingsRowView {
  /** Dotted key, e.g. "claude.model"; what `set` and `unset` take. */
  key: string;
  /** Human label from the key's last segments, e.g. "Stream partial". */
  label: string;
  description: string;
  control: ControlKind;
  /** For "select": the allowed values. For a setting that may be empty, "" is the first. */
  choices: string[];
  /** True for "number" settings that take whole numbers only. */
  integer: boolean;
  /** True if the setting may be empty (`null`): an empty input then stores no value. */
  nullable: boolean;
  /** The value in effect in the shown scope (a JSON value). */
  value: unknown;
  /** The default as text. */
  defaultText: string;
  /** True if a value is stored in the shown scope: show the marker and "reset to default". */
  modified: boolean;
  /** Where the value comes from when it is not modified: "global" (in a project) or "default". */
  inherits: 'global' | 'default';
  /** The type as text, for a tooltip. */
  type: string;
}

/** A group of settings: all keys with the same first segment. */
export interface SettingsSection {
  /** The first key segment, e.g. "claude"; "general" for keys without a dot. */
  id: string;
  /** Human label, e.g. "Claude". */
  label: string;
  /** Description of the group (from its "section" row); "" if none. */
  description: string;
  /** The settings of the group in the order calu lists them (after filtering). */
  rows: SettingsRowView[];
  /** Number of `rows` that are modified. */
  modifiedCount: number;
}

/** Id of the section for keys without a dot. */
export const GENERAL_SECTION = 'general';

/** Words that keep their spelling in labels. */
const LABEL_WORDS: Record<string, string> = {
  id: 'ID',
  ids: 'IDs',
  url: 'URL',
  api: 'API',
  aws: 'AWS',
  sdk: 'SDK',
  mcp: 'MCP',
  acp: 'ACP',
  cli: 'CLI',
  usd: 'USD',
  json: 'JSON',
  tui: 'TUI',
  s: 'seconds',
  ms: 'milliseconds',
};

/**
 * Turn one key segment into words.
 *
 * @param segment A segment such as "stream_partial" or "timeout_s".
 * @returns The words separated by spaces, lower case, with known abbreviations spelled as in
 *   `LABEL_WORDS`; e.g. "stream partial", "timeout seconds".
 */
function words(segment: string): string {
  return segment
    .split(/[_-]+/)
    .filter(Boolean)
    .map((w) => LABEL_WORDS[w.toLowerCase()] ?? w.toLowerCase())
    .join(' ');
}

/**
 * Capitalise the first letter of a text.
 *
 * @param text Any string.
 * @returns The text with its first character in upper case.
 */
function capitalise(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/**
 * Return the human label of a section.
 *
 * @param id The first key segment, e.g. "auto_escalate".
 * @returns E.g. "Auto escalate"; "General" for `GENERAL_SECTION`.
 */
export function sectionLabel(id: string): string {
  return capitalise(words(id)) || 'General';
}

/**
 * Return the human label of a setting inside its section.
 *
 * @param key The dotted key.
 * @returns The segments after the first as words, deeper levels joined with " › "; the only
 *   segment for a key without a dot.
 *
 * @example labelOf("claude.stream_partial") === "Stream partial"
 * @example labelOf("checkers.command.request_limit") === "Command › request limit"
 */
export function labelOf(key: string): string {
  const parts = key.split('.');
  const rest = parts.length > 1 ? parts.slice(1) : parts;
  return capitalise(rest.map(words).join(' › '));
}

/**
 * Return the section id of a key.
 *
 * @param key The dotted key.
 * @returns The first segment, or `GENERAL_SECTION` for a key without a dot.
 */
export function sectionOf(key: string): string {
  const dot = key.indexOf('.');
  return dot > 0 ? key.slice(0, dot) : GENERAL_SECTION;
}

/**
 * Choose the control of a row from the kind calu computed.
 *
 * @param row The row.
 * @returns The control; "json" for an unknown kind, so every setting stays editable. A
 *   "choice" without choices becomes "text".
 */
export function controlKind(row: Pick<SettingRow, 'kind' | 'choices'>): ControlKind {
  switch (row.kind) {
    case 'bool':
      return 'toggle';
    case 'choice':
      return row.choices && row.choices.length > 0 ? 'select' : 'text';
    case 'int':
    case 'number':
      return 'number';
    case 'text':
      return 'text';
    case 'model':
      return 'model';
    case 'markdown':
      return 'markdown';
    case 'lines':
      return 'list';
    default:
      return 'json';
  }
}

/**
 * Tell whether a setting may be empty, from its type text.
 *
 * @param type The row's `type`, e.g. "text (or empty)".
 * @returns True if the text says the value may be empty or none.
 */
function nullableType(type: string): boolean {
  return /\bor empty\b|\bNone\b|\bnull\b|\boptional\b/i.test(type);
}

/**
 * Build the view of one row.
 *
 * @param row A row of `ide.settings.get` whose kind is not "section".
 * @returns The row as the form shows it.
 */
export function rowView(row: SettingRow): SettingsRowView {
  const control = controlKind(row);
  const nullable = nullableType(row.type) || row.default_value === null;
  const choices = control === 'select' ? [...(row.choices ?? [])] : [];
  if (control === 'select' && nullable && !choices.includes('')) choices.unshift('');
  return {
    key: row.key,
    label: labelOf(row.key),
    description: row.description ?? '',
    control,
    choices,
    integer: row.kind === 'int',
    nullable,
    value: row.value,
    defaultText: typeof row.default === 'string' ? row.default : JSON.stringify(row.default ?? null),
    modified: row.is_set === true,
    inherits: row.inherits === 'global' ? 'global' : 'default',
    type: row.type ?? '',
  };
}

/**
 * Tell whether a row matches a search text.
 *
 * @param row The row.
 * @param search The search text; every word of it (split at white space) must occur in the
 *   key, the label or the description, ignoring case. The word "@modified" matches modified
 *   rows only.
 * @returns True if the row matches; always true for an empty search.
 */
export function matchesSearch(row: SettingsRowView, search: string): boolean {
  const needles = search.toLowerCase().split(/\s+/).filter(Boolean);
  if (needles.length === 0) return true;
  const hay = `${row.key} ${row.label} ${row.description}`.toLowerCase();
  return needles.every((n) => (n === '@modified' ? row.modified : hay.includes(n)));
}

/**
 * Group the rows of `ide.settings.get` into sections.
 *
 * @param rows The rows, in calu's order. Rows of the kind "section" are not shown as
 *   settings; the description of a first-level one describes its section.
 * @param search Optional search text (see `matchesSearch`).
 * @returns The sections in the order their first row appears, without sections that have no
 *   matching row.
 */
export function buildSections(rows: readonly SettingRow[], search = ''): SettingsSection[] {
  const sections = new Map<string, SettingsSection>();
  const descriptions = new Map<string, string>();
  for (const row of rows) {
    if (!row || typeof row.key !== 'string' || !row.key) continue;
    if (row.kind === 'section') {
      if (!row.key.includes('.')) descriptions.set(row.key, row.description ?? '');
      continue;
    }
    const view = rowView(row);
    if (!matchesSearch(view, search)) continue;
    const id = sectionOf(row.key);
    let section = sections.get(id);
    if (!section) {
      section = { id, label: sectionLabel(id), description: '', rows: [], modifiedCount: 0 };
      sections.set(id, section);
    }
    section.rows.push(view);
    if (view.modified) section.modifiedCount += 1;
  }
  for (const section of sections.values()) section.description = descriptions.get(section.id) ?? '';
  return [...sections.values()];
}

/**
 * Format a setting's value for its control.
 *
 * @param control The control.
 * @param value The JSON value.
 * @returns For "toggle": "true" or "false". For "list": one entry per line. For "json":
 *   pretty-printed JSON. Otherwise the value as text; "" for `null` and `undefined`.
 */
export function formatControlValue(control: ControlKind, value: unknown): string {
  if (control === 'toggle') return value === true ? 'true' : 'false';
  if (control === 'json') return value === undefined ? '' : JSON.stringify(value, null, 2);
  if (value === null || value === undefined) return '';
  if (control === 'list') return Array.isArray(value) ? value.map(String).join('\n') : String(value);
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** Result of `parseControlValue`. */
export type ParsedValue = { ok: true; value: unknown } | { ok: false; message: string };

/**
 * Convert what the user entered into the JSON value for `ide.settings.set`.
 *
 * This is a first check for an immediate message; calu validates the value again and its
 * message is shown the same way.
 *
 * @param row The row's view (`control`, `choices`, `integer`, `nullable`).
 * @param input The control's content: a boolean for "toggle", else its text.
 * @returns `{ok: true, value}`: a boolean ("toggle"), a number ("number"), a list of the
 *   non-empty trimmed lines ("list"), the parsed JSON ("json"), else the text; `null` for an
 *   empty input of a nullable setting. `{ok: false, message}` for a number that is not one, a
 *   decimal where a whole number is needed, a value outside `choices`, invalid JSON, or an
 *   empty input of a "number" setting that cannot be empty.
 */
export function parseControlValue(
  row: Pick<SettingsRowView, 'control' | 'choices' | 'integer' | 'nullable'>,
  input: string | boolean,
): ParsedValue {
  if (row.control === 'toggle') return { ok: true, value: input === true || input === 'true' };
  const text = typeof input === 'string' ? input : String(input);
  const trimmed = text.trim();
  if (trimmed === '' && row.nullable && row.control !== 'list' && row.control !== 'json') return { ok: true, value: null };
  switch (row.control) {
    case 'number': {
      if (trimmed === '') return { ok: false, message: 'Enter a number.' };
      const n = Number(trimmed);
      if (!Number.isFinite(n)) return { ok: false, message: `"${trimmed}" is not a number.` };
      if (row.integer && !Number.isInteger(n)) return { ok: false, message: 'Enter a whole number.' };
      return { ok: true, value: n };
    }
    case 'select':
      return row.choices.includes(trimmed)
        ? { ok: true, value: trimmed }
        : { ok: false, message: `Choose one of: ${row.choices.filter(Boolean).join(', ')}.` };
    case 'list':
      return { ok: true, value: text.split('\n').map((line) => line.trim()).filter(Boolean) };
    case 'json':
      if (trimmed === '') return row.nullable ? { ok: true, value: null } : { ok: false, message: 'Enter a JSON value.' };
      try {
        return { ok: true, value: JSON.parse(trimmed) as unknown };
      } catch (error) {
        return { ok: false, message: `Not valid JSON: ${error instanceof Error ? error.message : String(error)}` };
      }
    case 'markdown':
      return { ok: true, value: text };
    default:
      return { ok: true, value: trimmed };
  }
}

/** Fix actions the Setup card can offer (the `action` of a `fix` message). */
export const FIX_ACTIONS = [
  'installCalu',
  'updateCalu',
  'startServer',
  'copyServerCommand',
  'copyRootInstall',
  'openClaudeDocs',
  'restartBridge',
] as const;
/** A fix action. */
export type FixAction = (typeof FIX_ACTIONS)[number];

/** One row of the Setup card. */
export interface SetupItem {
  name: string;
  level: 'ok' | 'missing' | 'note';
  message: string;
  advice: string;
  /** The fix button of the row; `undefined` if the extension has no fix for it. */
  fix?: { action: FixAction; label: string };
}

/**
 * Turn the findings of `ide.status` into the rows of the Setup card.
 *
 * @param findings The findings (untrusted shape: entries without a string `name` are dropped).
 * @returns One item per finding in the given order. A missing `claude` gets the fix "How to
 *   install Claude Code" (the extension never installs it); a finding named `calu` that is not
 *   ok gets "Update calu"; other findings get no button.
 */
export function setupItems(findings: readonly Finding[] | undefined | null): SetupItem[] {
  if (!Array.isArray(findings)) return [];
  const items: SetupItem[] = [];
  for (const f of findings) {
    if (!f || typeof f.name !== 'string') continue;
    const level = f.level === 'missing' || f.level === 'note' ? f.level : 'ok';
    const item: SetupItem = {
      name: f.name,
      level,
      message: typeof f.message === 'string' ? f.message : '',
      advice: typeof f.advice === 'string' ? f.advice : '',
    };
    if (level !== 'ok' && f.name === 'claude') item.fix = { action: 'openClaudeDocs', label: 'How to install Claude Code' };
    if (level !== 'ok' && f.name === 'calu') item.fix = { action: 'updateCalu', label: 'Update calu' };
    items.push(item);
  }
  return items;
}
