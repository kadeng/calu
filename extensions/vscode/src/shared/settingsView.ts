/**
 * Pure view logic of the settings dialog that is not about single setting rows (those are in
 * `settingsModel.ts`): the model presets, the Setup card, the Honesty card, search highlighting
 * and the bookkeeping of optimistic saves. No `vscode`, no DOM; used by `webview/settings*.ts`.
 *
 *     ide.models.presets ──groupPresets──────► preset cards by provider
 *                        ──selectedPresetId──► which card is marked (all roles equal its model)
 *     ide.status + problems ──setupRows──────► rows of the Setup card with fix buttons
 *     honesty.mode description ──modeSentences──► one sentence per mode
 *     search text ──highlightRanges(text)────► [start, end) ranges to mark in a label
 *
 *     user edit ──PendingEdits.begin──► optimistic value shown ──┬─ saved ──► settle(value)
 *                                                               └─ error ──► fail ──► roll back
 */

import type { IdeStatusResult, ModelPreset, ModelRole, SettingRow } from '../bridgeProtocol';
import type { ServerHelp } from './serverCommand';
import { setupItems, type FixAction } from './settingsModel';

// ------------------------------------------------------------------ model presets

/** Presets of one provider, for one heading of the preset cards. */
export interface PresetGroup {
  /** The provider, e.g. "Anthropic"; "Other" for presets without a group. */
  group: string;
  presets: ModelPreset[];
}

/**
 * Return the model string a preset stores.
 *
 * @param preset The preset.
 * @returns Its model; "" for the preset that stores no model (`model` is "" or null).
 */
export function presetModel(preset: Pick<ModelPreset, 'model'>): string {
  return typeof preset.model === 'string' ? preset.model : '';
}

/**
 * Group the presets by provider.
 *
 * @param presets The presets in calu's order (untrusted shape: entries without a string `id`
 *   are dropped).
 * @returns The groups in the order their first preset appears; inside a group the presets keep
 *   their order.
 */
export function groupPresets(presets: readonly ModelPreset[] | null | undefined): PresetGroup[] {
  const groups = new Map<string, PresetGroup>();
  for (const preset of presets ?? []) {
    if (!preset || typeof preset.id !== 'string' || !preset.id) continue;
    const name = typeof preset.group === 'string' && preset.group ? preset.group : 'Other';
    let group = groups.get(name);
    if (!group) {
      group = { group: name, presets: [] };
      groups.set(name, group);
    }
    group.presets.push(preset);
  }
  return [...groups.values()];
}

/** What the roles' models have in common. */
export type RolesState =
  /** There are no roles (e.g. no database login). */
  | { kind: 'none' }
  /** Every role stores the same model; "" means none is stored (the default model). */
  | { kind: 'uniform'; model: string }
  /** At least two roles store different models. */
  | { kind: 'mixed' };

/**
 * Tell whether all roles use the same model.
 *
 * @param roles The roles of `ide.models.presets`.
 * @returns See `RolesState`. Models are compared after trimming.
 */
export function rolesState(roles: readonly ModelRole[] | null | undefined): RolesState {
  if (!roles || roles.length === 0) return { kind: 'none' };
  const first = (roles[0].model ?? '').trim();
  return roles.every((role) => (role.model ?? '').trim() === first) ? { kind: 'uniform', model: first } : { kind: 'mixed' };
}

/**
 * Find the preset that stores a given model.
 *
 * @param presets The presets.
 * @param model A model string; "" finds the preset that stores no model.
 * @returns The first preset whose model equals `model`, or `undefined`.
 */
export function presetForModel(presets: readonly ModelPreset[] | null | undefined, model: string): ModelPreset | undefined {
  return (presets ?? []).find((preset) => preset && presetModel(preset) === model.trim());
}

/**
 * Decide which preset card is marked as the current state.
 *
 * @param presets The presets.
 * @param roles The roles with their stored models.
 * @returns The id of the preset whose model every role stores (the default preset when no role
 *   stores a model); `undefined` if the roles differ, there are no roles, or their common
 *   model is not a preset (a custom model).
 *
 * @example selectedPresetId(presets, [{key: "a", label: "A", model: ""}]) === "default"
 */
export function selectedPresetId(
  presets: readonly ModelPreset[] | null | undefined,
  roles: readonly ModelRole[] | null | undefined,
): string | undefined {
  const state = rolesState(roles);
  return state.kind === 'uniform' ? presetForModel(presets, state.model)?.id : undefined;
}

/**
 * Describe the model of one role for the "Per role" table.
 *
 * @param model The role's stored model ("" = none).
 * @param presets The presets (for their labels).
 * @param defaultModel The model a role without a stored model uses; "" if unknown.
 * @returns The preset's label or the model string; for no stored model "default → <model>",
 *   or "default" if the default model is unknown.
 */
export function roleModelText(model: string, presets: readonly ModelPreset[] | null | undefined, defaultModel: string): string {
  const stored = (model ?? '').trim();
  if (stored === '') return defaultModel ? `default → ${defaultModel}` : 'default';
  return presetForModel(presets, stored)?.label ?? stored;
}

// ------------------------------------------------------------------ search

/** A half-open character range `[start, end)` of a text. */
export type Range = [start: number, end: number];

/**
 * Split a search text into the words that are looked for in texts.
 *
 * @param search The search text of the dialog.
 * @returns Its words in lower case, without filter words that start with "@" (`@modified`).
 */
export function searchWords(search: string): string[] {
  return search
    .toLowerCase()
    .split(/\s+/)
    .filter((word) => word !== '' && !word.startsWith('@'));
}

/**
 * Find the parts of a text that a search matches, for highlighting.
 *
 * @param text The text shown (a label, key or description).
 * @param search The search text; each word is looked for separately, ignoring case.
 * @returns The ranges of all occurrences of all words, sorted by start, with overlapping and
 *   touching ranges merged. Empty for an empty search.
 *
 * @example highlightRanges("Stream partial", "art str") // [[0, 3], [8, 11]]
 */
export function highlightRanges(text: string, search: string): Range[] {
  const hay = text.toLowerCase();
  const found: Range[] = [];
  for (const word of searchWords(search)) {
    for (let at = hay.indexOf(word); at !== -1; at = hay.indexOf(word, at + word.length)) {
      found.push([at, at + word.length]);
    }
  }
  found.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: Range[] = [];
  for (const range of found) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([range[0], range[1]]);
  }
  return merged;
}

/**
 * Build the search text that `matchesSearch` gets from the search field and the filter chip.
 *
 * @param search The text of the search field.
 * @param onlyModified True if "Show only modified" is on.
 * @returns `search`, with the word "@modified" added if the chip is on and the text lacks it.
 */
export function effectiveSearch(search: string, onlyModified: boolean): string {
  const words = search.split(/\s+/).filter(Boolean);
  if (onlyModified && !words.some((word) => word.toLowerCase() === '@modified')) words.push('@modified');
  return words.join(' ');
}

/**
 * Tell whether a card (Setup, Models, Checkers, Honesty) stays visible during a search.
 *
 * @param keywords Words that describe the card, e.g. "models presets anthropic bedrock".
 * @param search The effective search text (see `effectiveSearch`).
 * @returns True for an empty search; false if the search holds a filter word ("@modified":
 *   cards have no modified state); else true if every word occurs in `keywords`.
 */
export function cardMatches(keywords: string, search: string): boolean {
  const all = search.toLowerCase().split(/\s+/).filter(Boolean);
  if (all.length === 0) return true;
  if (all.some((word) => word.startsWith('@'))) return false;
  const hay = keywords.toLowerCase();
  return all.every((word) => hay.includes(word));
}

/**
 * Split a description into paragraphs.
 *
 * @param text The description of a setting (plain text; blank lines separate paragraphs).
 * @returns The non-empty paragraphs with single line breaks replaced by spaces.
 */
export function paragraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/)
    .map((part) => part.replace(/\s*\n\s*/g, ' ').trim())
    .filter(Boolean);
}

// ------------------------------------------------------------------ setup card

/** One row of the Setup card. */
export interface SetupRow {
  /** Stable name for `data-testid`, e.g. "claude", "database", "server". */
  name: string;
  /** Text shown as the row's title, e.g. "Claude Code". */
  title: string;
  level: 'ok' | 'missing' | 'note';
  /** What was found. */
  message: string;
  /** What to do about it; "" if nothing. */
  advice: string;
  /** Fix buttons, the most useful first; empty if the extension has no fix. */
  fixes: { action: FixAction; label: string }[];
}

/** Titles of well-known findings. */
const SETUP_TITLES: Record<string, string> = {
  calu: 'calu',
  claude: 'Claude Code',
  uv: 'uv',
  git: 'git',
  tmux: 'tmux',
  screen: 'screen',
  database: 'Database',
  server: 'calu server',
};

/** Inputs of `setupRows`: the parts of the `data` message the Setup card shows. */
export interface SetupInput {
  status: IdeStatusResult | null;
  serverHelp: ServerHelp | null;
  /** Problem lines of the host; a line that starts with "calu:" says the bridge did not start. */
  problems: readonly string[];
}

/**
 * Build the rows of the Setup card.
 *
 * @param input Status, server help and problems of the `data` message.
 * @returns With a status: one row per finding (see `setupItems`), then "database" (not
 *   reachable: level "missing" with the problem as message) and "server" (no running session
 *   and no finding named "server": level "note" with the fix "startServer" if a server
 *   command is known). Without a status: one row "calu" of level "missing" with the fixes
 *   "installCalu" and "restartBridge", whose message is the host's "calu:" problem line if
 *   there is one. A missing `claude` never gets an install fix.
 */
export function setupRows(input: SetupInput): SetupRow[] {
  const { status } = input;
  if (!status) {
    const line = input.problems.find((problem) => /^calu:/i.test(problem));
    return [
      {
        name: 'calu',
        title: 'calu',
        level: 'missing',
        message: line ? line.replace(/^calu:\s*/i, '') : 'calu did not answer, so the setup could not be checked.',
        advice: 'Install calu, or start its bridge again if it is installed.',
        fixes: [
          { action: 'installCalu', label: 'Install calu' },
          { action: 'restartBridge', label: 'Start again' },
        ],
      },
    ];
  }
  const rows: SetupRow[] = setupItems(status.findings).map((item) => ({
    name: item.name,
    title: SETUP_TITLES[item.name] ?? item.name,
    level: item.level,
    message: item.message,
    advice: item.advice,
    fixes: item.fix ? [item.fix] : [],
  }));
  const names = new Set(rows.map((row) => row.name));
  const database = status.database;
  if (database && !names.has('database')) {
    rows.push({
      name: 'database',
      title: SETUP_TITLES.database,
      level: database.reachable ? 'ok' : 'missing',
      message: database.reachable
        ? `Reachable${database.place ? ` (${database.place})` : ''}.`
        : database.problem || `Not reachable${database.place ? ` (${database.place})` : ''}.`,
      advice: '',
      fixes: [],
    });
  }
  if (status.server && !names.has('server')) {
    const running = Number(status.server.sessions_running) || 0;
    rows.push({
      name: 'server',
      title: SETUP_TITLES.server,
      level: running > 0 ? 'ok' : 'note',
      message: running > 0 ? `${running} session${running === 1 ? '' : 's'} running on this machine.` : 'No session is running on this machine.',
      advice: running > 0 ? '' : 'New sessions need a running calu server; the command is below.',
      fixes:
        running > 0 || !input.serverHelp
          ? []
          : [{ action: 'startServer', label: 'Open terminal with the server command' }],
    });
  }
  return rows;
}

/** Summary of the Setup card. */
export interface SetupSummary {
  /** True if no row is "missing": the card collapses to one line. */
  ok: boolean;
  /** The one-line text, e.g. "Everything is in place". */
  text: string;
}

/**
 * Summarise the Setup card in one line.
 *
 * @param rows The rows of `setupRows`.
 * @returns `ok` is true if no row has the level "missing". The text is "Everything is in
 *   place" (with the number of notes if there are any), or how many things are missing.
 */
export function setupSummary(rows: readonly SetupRow[]): SetupSummary {
  const missing = rows.filter((row) => row.level === 'missing').length;
  const notes = rows.filter((row) => row.level === 'note').length;
  if (missing > 0) return { ok: false, text: `${missing} thing${missing === 1 ? ' needs' : 's need'} attention` };
  return { ok: true, text: notes > 0 ? `Everything is in place (${notes} note${notes === 1 ? '' : 's'})` : 'Everything is in place' };
}

// ------------------------------------------------------------------ honesty card

/**
 * Split the description of a choice setting into one text per choice.
 *
 * The description of `honesty.mode` has the form "`off`: text. `track`: text. `enforce`: text.
 * Default `off`." (with or without the backticks).
 *
 * @param description The setting's description.
 * @param choices The allowed values, e.g. `["off", "track", "enforce"]`.
 * @returns For each choice whose marker "<choice>:" occurs in the description (at the start or
 *   after white space, optionally in backticks): the text up to the next choice's marker,
 *   without backticks and without a trailing "Default <value>." sentence. Choices without a
 *   marker are absent from the result.
 *
 * @example modeSentences("`a`: first. `b`: second. Default `a`.", ["a", "b"]) // {a: "first.", b: "second."}
 */
export function modeSentences(description: string, choices: readonly string[]): Record<string, string> {
  const marks: { choice: string; start: number; end: number }[] = [];
  for (const choice of choices) {
    if (!choice) continue;
    const escaped = choice.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = new RegExp(`(^|\\s)\`?${escaped}\`?:\\s`).exec(description);
    if (match) marks.push({ choice, start: match.index + match[1].length, end: match.index + match[0].length });
  }
  marks.sort((a, b) => a.start - b.start);
  const result: Record<string, string> = {};
  marks.forEach((mark, index) => {
    const stop = index + 1 < marks.length ? marks[index + 1].start : description.length;
    result[mark.choice] = description
      .slice(mark.end, stop)
      .replace(/`/g, '')
      .replace(/\s+/g, ' ')
      .replace(/\s*Default \S+\.?\s*$/, '')
      .trim();
  });
  return result;
}

// ------------------------------------------------------------------ optimistic saves

/** What a row showed before an edit: the state to return to when the edit is refused. */
export interface RowState {
  value: unknown;
  /** `is_set` of the row. */
  isSet: boolean;
}

/**
 * Bookkeeping of edits that were sent to the host and not answered yet.
 *
 * The dialog shows an edit at once (optimistic). Per key this class remembers the state the
 * host last confirmed and how many requests are open:
 *
 *     begin(key, shownBefore, optimistic)   open += 1; confirmed = shownBefore (first edit only)
 *     settle(key, stateFromHost)            open -= 1; confirmed = stateFromHost
 *     fail(key)                             open -= 1; returns confirmed when open reaches 0
 *
 * Invariant: `isPending(key)` is true exactly while more `begin` than `settle` + `fail` calls
 * were made for the key.
 */
export class PendingEdits {
  private readonly entries = new Map<string, { open: number; confirmed: RowState; optimistic: RowState }>();

  /**
   * Note that an edit was sent.
   *
   * @param key The setting.
   * @param before What the row showed before the edit.
   * @param optimistic What the row shows now.
   * @returns Nothing.
   */
  begin(key: string, before: RowState, optimistic: RowState): void {
    const entry = this.entries.get(key);
    if (entry) {
      entry.open += 1;
      entry.optimistic = optimistic;
    } else {
      this.entries.set(key, { open: 1, confirmed: before, optimistic });
    }
  }

  /**
   * Note that the host stored an edit.
   *
   * @param key The setting.
   * @param stored The state the host reported.
   * @returns The state the row must show: `stored` if no other edit of the key is open, else
   *   the optimistic state of the latest edit. `stored` also for a key without an open edit.
   */
  settle(key: string, stored: RowState): RowState {
    const entry = this.entries.get(key);
    if (!entry) return stored;
    entry.open -= 1;
    entry.confirmed = stored;
    if (entry.open > 0) return entry.optimistic;
    this.entries.delete(key);
    return stored;
  }

  /**
   * Note that the host refused an edit.
   *
   * @param key The setting.
   * @returns The state to roll back to (the last confirmed one) if no other edit of the key is
   *   open; `undefined` if another edit is still open or the key has no open edit.
   */
  fail(key: string): RowState | undefined {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    entry.open -= 1;
    if (entry.open > 0) return undefined;
    this.entries.delete(key);
    return entry.confirmed;
  }

  /**
   * Return the optimistic state of a key, for re-applying it to freshly loaded data.
   *
   * @param key The setting.
   * @returns The state of the latest open edit, or `undefined` if none is open.
   */
  optimistic(key: string): RowState | undefined {
    return this.entries.get(key)?.optimistic;
  }

  /**
   * Tell whether an edit of a key is open.
   *
   * @param key The setting.
   * @returns True while a `begin` of the key has no `settle` or `fail` yet.
   */
  isPending(key: string): boolean {
    return this.entries.has(key);
  }

  /** Number of keys with an open edit. */
  get size(): number {
    return this.entries.size;
  }

  /**
   * Forget all open edits (after a scope switch: their answers belong to the old scope).
   *
   * @returns Nothing.
   */
  clear(): void {
    this.entries.clear();
  }
}

/**
 * Return the rows with one row's state replaced.
 *
 * @param rows The rows of `ide.settings.get`.
 * @param key The setting to change.
 * @param state The new value and `is_set`.
 * @returns A new array; the row with `key` is a copy with `value` and `is_set` from `state`,
 *   all other rows are the same objects. The same rows if no row has the key.
 */
export function withRowState(rows: readonly SettingRow[], key: string, state: RowState): SettingRow[] {
  return rows.map((row) => (row.key === key ? { ...row, value: state.value, is_set: state.isSet } : row));
}

/**
 * Compare two JSON values.
 *
 * @param a A JSON value.
 * @param b A JSON value.
 * @returns True if both serialise to the same JSON text (object members in the same order).
 */
export function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}
