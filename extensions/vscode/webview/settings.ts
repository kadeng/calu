/**
 * Script of the settings dialog "calu Settings" (runs in the webview, bundled by esbuild into
 * `dist/settings.js`). It owns the page frame, the state and the messages; the cards are built
 * by `settingsCards.ts`, the setting rows by `settingsRows.ts`, pure logic is in
 * `src/shared/settingsModel.ts` and `src/shared/settingsView.ts`.
 *
 *     ┌ header (sticky) ─ title · scope · save status ─ search · "Modified" chip ──────────┐
 *     ├ nav (sticky) ──┬ content ───────────────────────────────────────────────────────────┤
 *     │ Common         │ cards:    Setup · Models · Checkers · Honesty                       │
 *     │ All settings   │ sections: one per first key segment, one row per setting          │
 *     └────────────────┴─────────────────────────────────────────────────────────────────────┘
 *
 *     start:  state = vscode.getState(); render it; post load {project}
 *     data    ─► replace `data`, re-apply open edits, update cards and rows (rows one by one)
 *     edit    ─► optimistic: row shows the value ─► post set ─┬─ saved ─► value from calu
 *                                                            └─ error ─► roll back + message at the row
 *     search  ─► rows and sections are hidden or shown; nothing is rebuilt
 *
 * All data is inserted as text (`textContent`). View-only state (search, scope, scroll
 * position, open parts) and the last `data` are kept with `vscode.setState`.
 */

import type { SettingRow } from '../src/bridgeProtocol';
import type { SettingsData, SettingsHostMessage, SettingsWebviewMessage } from '../src/shared/settingsMessages';
import { buildSections, formatControlValue, matchesSearch, type SettingsRowView, type SettingsSection } from '../src/shared/settingsModel';
import { PendingEdits, cardMatches, effectiveSearch, saveSummary, withRowState, type RowState } from '../src/shared/settingsView';
import {
  renderCheckersCard,
  renderHonestyCard,
  renderModelsCard,
  renderRulesCard,
  renderSetupCard,
  type CardHost,
  type CardId,
  type CardState,
} from './settingsCards';
import { add, button, el, icon, keepFocus, rich } from './settingsDom';
import { createRow, failRow, paintRow, setRowError, updateRow, type RowHandle, type RowHost } from './settingsRows';

/** The part of the VSCode webview API used here. */
interface VsCodeApi {
  postMessage(message: SettingsWebviewMessage): void;
  getState(): unknown;
  setState(state: unknown): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

/** View-only state that is persisted. */
interface UiState {
  /** Text of the search field. */
  search: string;
  /** "Show only modified" chip. */
  onlyModified: boolean;
  /** The scope to load (`null` = global). */
  project: string | null;
  /** Vertical scroll position of the page in pixels. */
  scrollY: number;
  cards: CardState;
}

/** What `vscode.setState` stores. */
interface Persisted {
  data: SettingsData | null;
  ui: UiState;
}

/** A card of the dialog with the words a search can find it by. */
interface CardSpec {
  id: CardId;
  label: string;
  keywords: string;
  render(): HTMLElement | null;
}

/** A rendered settings section. */
interface SectionHandle {
  el: HTMLElement;
  count: HTMLElement;
  keys: string[];
}

/** Settings whose value a card shows too: the card is rebuilt when one of them changes. */
const CARD_KEYS: Record<string, CardId> = {
  'honesty.mode': 'honesty',
  'claude.model': 'models',
  'claude.effort': 'models',
  'claude.use_bedrock': 'models',
};

const vscode = acquireVsCodeApi();
const restored = vscode.getState() as Partial<Persisted> | undefined;

let data: SettingsData | null = restored?.data ?? null;
const ui: UiState = {
  search: restored?.ui?.search ?? '',
  onlyModified: restored?.ui?.onlyModified ?? false,
  project: restored?.ui?.project ?? null,
  scrollY: restored?.ui?.scrollY ?? 0,
  cards: { open: { ...(restored?.ui?.cards?.open ?? {}) }, errors: {}, draftPreset: '', draftCustom: '', draftEffort: '' },
};

const pending = new PendingEdits();
/** Text of the value last sent per key, to show it again when calu refuses it. */
const attempted = new Map<string, string>();
/** Row views by key, from the last `data`. */
let views = new Map<string, SettingsRowView>();
/** Rendered rows by key. */
const rows = new Map<string, RowHandle>();
/** Rendered sections by id. */
const sections = new Map<string, SectionHandle>();
/** Order and keys of the rendered sections; a change means the list is rebuilt. */
let structure = '';
/** Text of the host's running long request; "" for none. */
let busyText = '';
/** True from a `load` until its `data`. */
let loading = true;
/** Error of the last `load`; "" for none. */
let loadError = '';
/** Keys saved in the last moment (forgotten after `SAVED_SHOWN_MS`), for the status text. */
const savedKeys = new Set<string>();
/** Keys whose last save calu refused and that were not saved since; their rows show why. */
const failedKeys = new Set<string>();
/** Milliseconds the status text names the settings that were just saved. */
const SAVED_SHOWN_MS = 2500;
let savedTimer = 0;
let persistTimer = 0;
let reloadTimer = 0;
let scrollQueued = false;

// ------------------------------------------------------------------ page frame (built once)

const app = document.getElementById('app') as HTMLElement;
const header = el('header', 'top');
const scopeWrap = el('div', 'scope');
const scopeSelect = el('select', 'select', '', { id: 'scope-select', testid: 'scope-select' });
const saveStatus = el('span', 'save-status', '', { role: 'status', 'aria-live': 'polite', testid: 'save-status' });
const searchInput = el('input', 'search-input', '', {
  type: 'search',
  id: 'settings-search',
  testid: 'settings-search',
  placeholder: 'Search settings',
  'aria-label': 'Search settings',
  spellcheck: 'false',
  autocomplete: 'off',
});
const searchClear = button('', 'icon-btn search-clear', () => setSearch('', true), { 'aria-label': 'Clear search', testid: 'settings-search-clear' }, 'close');
const modifiedChip = button('Modified', 'chip', () => setOnlyModified(!ui.onlyModified), { testid: 'settings-only-modified', 'aria-pressed': 'false', title: 'Show only modified settings' }, 'filter');
const navSelect = el('select', 'select nav-select', '', { 'aria-label': 'Go to section', testid: 'settings-nav-select' });
const countText = el('span', 'count', '', { 'aria-live': 'polite', testid: 'settings-count' });
const progress = el('div', 'progress', '', { role: 'status', 'aria-live': 'polite', testid: 'settings-busy' });
const banner = el('div', 'banner', '', { testid: 'settings-problems' });
const nav = el('nav', 'nav', '', { 'aria-label': 'Sections' });
const content = el('main', 'content');
const cardsEl = el('div', 'cards');
const sectionsEl = el('div', 'sections');
const emptyEl = el('div', 'empty', '', { testid: 'settings-empty' });

/**
 * Build the page frame inside `#app` and wire the header controls.
 *
 * @returns Nothing.
 */
function buildFrame(): void {
  add(scopeWrap, el('label', 'scope-label', 'Scope', { for: 'scope-select' }), scopeSelect);
  add(
    header,
    add(el('div', 'top-row'), el('h1', 'title', 'calu Settings'), scopeWrap, saveStatus),
    add(
      el('div', 'search-row'),
      add(el('div', 'search'), icon('search', 'search-icon'), searchInput, searchClear),
      modifiedChip,
      navSelect,
      countText,
    ),
    progress,
  );
  add(
    emptyEl,
    el('p', 'empty-text', 'No settings match.'),
    button('Clear search and filter', 'btn', () => setSearch('', true, true), { testid: 'settings-empty-clear' }),
  );
  add(app, header, add(el('div', 'layout'), nav, add(content, banner, cardsEl, sectionsEl, emptyEl)));

  searchInput.value = ui.search;
  searchInput.addEventListener('input', () => setSearch(searchInput.value, false));
  searchInput.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && searchInput.value) {
      event.preventDefault();
      setSearch('', true);
    }
  });
  scopeSelect.addEventListener('change', () => switchScope(scopeSelect.value === '' ? null : scopeSelect.value));
  navSelect.addEventListener('change', () => goTo(navSelect.value));
  window.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  });
  window.addEventListener('scroll', onScroll, { passive: true });
  new ResizeObserver(() => {
    document.documentElement.style.setProperty('--top-height', `${header.offsetHeight}px`);
  }).observe(header);
}

// ------------------------------------------------------------------ state helpers

/**
 * Send a message to the extension host.
 *
 * @param message The message.
 * @returns Nothing.
 */
function post(message: SettingsWebviewMessage): void {
  vscode.postMessage(message);
}

/**
 * Store the view state and the last data soon (at most once per 200 ms).
 *
 * @returns Nothing. The JSON schema of the settings is not stored: the dialog does not use it
 *   and it is large.
 */
function persist(): void {
  if (persistTimer) return;
  persistTimer = window.setTimeout(() => {
    persistTimer = 0;
    const slim = data && data.settings ? { ...data, settings: { ...data.settings, schema: {} } } : data;
    const state: Persisted = { data: slim, ui: { ...ui, cards: { ...ui.cards, errors: {} } } };
    vscode.setState(state);
  }, 200);
}

/**
 * Return the rows of the last `data`.
 *
 * @returns The rows; empty if the settings were not loaded.
 */
function settingRows(): readonly SettingRow[] {
  return Array.isArray(data?.settings?.rows) ? data.settings.rows : [];
}

/**
 * Replace the state of one setting in `data` and update its view.
 *
 * @param key The setting.
 * @param state The value and `is_set` to show.
 * @returns Nothing. Does nothing if the settings are not loaded or lack the key.
 */
function applyRowState(key: string, state: RowState): void {
  if (!data?.settings || !views.has(key)) return;
  data.settings = { ...data.settings, rows: withRowState(data.settings.rows, key, state) };
  views = viewsOf(buildSections(data.settings.rows));
  const handle = rows.get(key);
  const view = views.get(key);
  if (handle && view) rows.set(key, updateRow(handle, view, rowHost));
  syncClaudeFromRows();
  updateSectionCounts();
  if (ui.onlyModified || ui.search.includes('@')) applyFilter();
}

/**
 * Copy Claude Code's own settings from the rows into the presets data, so that the Models card
 * shows an optimistic edit of `claude.model`, `claude.effort` or `claude.use_bedrock`.
 *
 * @returns Nothing. Does nothing while the presets have no `claude` part.
 */
function syncClaudeFromRows(): void {
  const claude = data?.presets?.claude;
  if (!data?.presets || !claude) return;
  const model = views.get('claude.model');
  const effort = views.get('claude.effort');
  const bedrock = views.get('claude.use_bedrock');
  data.presets = {
    ...data.presets,
    claude: {
      model: model ? (typeof model.value === 'string' && model.value ? model.value : null) : claude.model,
      effort: effort ? (typeof effort.value === 'string' && effort.value ? effort.value : null) : claude.effort,
      use_bedrock: bedrock ? bedrock.value === true : claude.use_bedrock,
    },
  };
}

/**
 * Index the row views of sections by key.
 *
 * @param list The sections.
 * @returns A map from key to view.
 */
function viewsOf(list: readonly SettingsSection[]): Map<string, SettingsRowView> {
  const map = new Map<string, SettingsRowView>();
  for (const section of list) for (const view of section.rows) map.set(view.key, view);
  return map;
}

// ------------------------------------------------------------------ saving

/**
 * Store a value of a setting: show it at once, remember what to roll back to, and send `set`.
 *
 * @param key The setting.
 * @param value The JSON value.
 * @returns Nothing. Without a row for the key, the value is only sent. Does nothing before
 *   the first `data`.
 */
function commit(key: string, value: unknown): void {
  const view = views.get(key);
  if (!data) return;
  const card = CARD_KEYS[key];
  if (card) ui.cards.errors[card] = '';
  if (!view) {
    // The settings rows are not loaded but a card offers the setting: send it without a row.
    post({ type: 'set', key, value, project: data.project });
    return;
  }
  pending.begin(key, { value: view.value, isSet: view.modified }, { value, isSet: true });
  attempted.set(key, formatControlValue(view.control, value));
  applyRowState(key, { value, isSet: true });
  const handle = rows.get(key);
  if (handle) setRowError(handle, '');
  post({ type: 'set', key, value, project: data.project });
  if (card) renderCards();
  renderStatus();
}

/**
 * Remove the stored value of a setting ("Reset to default"): show the row as unmodified at
 * once and send `unset`. The host answers `saved` and then fresh `data` with the value in effect.
 *
 * @param key The setting.
 * @returns Nothing.
 */
function unset(key: string): void {
  const view = views.get(key);
  if (!data || !view) return;
  const row = settingRows().find((candidate) => candidate.key === key);
  const fallback = data.project === null && row && row.default_value !== undefined ? row.default_value : view.value;
  pending.begin(key, { value: view.value, isSet: view.modified }, { value: fallback, isSet: false });
  attempted.delete(key);
  const handle = rows.get(key);
  if (handle) {
    handle.holdsRefused = false;
    setRowError(handle, '');
  }
  applyRowState(key, { value: fallback, isSet: false });
  post({ type: 'unset', key, project: data.project });
  if (CARD_KEYS[key]) renderCards();
  renderStatus();
}

/**
 * Ask the host for fresh data soon, without a loading state (after a save that changes what
 * the Models card shows, e.g. the default model in Bedrock mode).
 *
 * @returns Nothing.
 */
function reloadSoon(): void {
  window.clearTimeout(reloadTimer);
  reloadTimer = window.setTimeout(() => post({ type: 'load', project: ui.project }), 300);
}

/**
 * Load another scope.
 *
 * @param project The project, or `null` for the global scope.
 * @returns Nothing. Open edits are forgotten: their answers belong to the old scope.
 */
function switchScope(project: string | null): void {
  ui.project = project;
  pending.clear();
  attempted.clear();
  savedKeys.clear();
  failedKeys.clear();
  loading = true;
  loadError = '';
  post({ type: 'load', project });
  renderStatus();
  persist();
}

// ------------------------------------------------------------------ host messages

/**
 * Handle a message from the extension host (see the module comment).
 *
 * @param message The message.
 * @returns Nothing.
 */
function receive(message: SettingsHostMessage): void {
  switch (message.type) {
    case 'data': {
      const { type: _type, ...rest } = message;
      data = rest;
      ui.project = rest.project;
      loading = false;
      loadError = '';
      if (data.settings) {
        let fresh = data.settings.rows;
        for (const row of fresh) {
          const open = pending.optimistic(row.key);
          if (open) fresh = withRowState(fresh, row.key, open);
        }
        data.settings = { ...data.settings, rows: fresh };
      }
      renderAll();
      return;
    }
    case 'saved': {
      const current = views.get(message.key);
      const stored: RowState = message.unset
        ? { value: current?.value, isSet: false }
        : { value: message.value, isSet: true };
      const state = pending.settle(message.key, stored);
      attempted.delete(message.key);
      const handle = rows.get(message.key);
      if (handle && !pending.isPending(message.key)) {
        handle.holdsRefused = false;
        setRowError(handle, '');
      }
      applyRowState(message.key, state);
      markSaved(message.key);
      if (CARD_KEYS[message.key]) renderCards();
      if (isModelKey(message.key)) reloadSoon();
      renderStatus();
      return;
    }
    case 'error':
      receiveError(message.key, message.message);
      return;
    case 'status':
      if (data) data.status = message.status;
      ui.cards.errors.setup = '';
      renderCards();
      persist();
      return;
    case 'busy':
      busyText = message.busy ? message.what : '';
      renderStatus();
      return;
  }
}

/**
 * Tell whether a saved setting changes what the Models card shows.
 *
 * @param key The setting.
 * @returns True for Claude Code's own model settings and for the keys of the model roles.
 */
function isModelKey(key: string): boolean {
  return CARD_KEYS[key] === 'models' || (data?.presets?.roles ?? []).some((role) => role.key === key);
}

/**
 * Show a failed request.
 *
 * @param key A setting's key (roll the row back and show the message there), or "models",
 *   "rules", "status", "fix" or "load" (show it at the card or on top).
 * @param message The message, shown verbatim.
 * @returns Nothing.
 */
function receiveError(key: string, message: string): void {
  const card = CARD_KEYS[key];
  if (views.has(key)) {
    failedKeys.add(key);
    savedKeys.delete(key);
    const text = attempted.get(key);
    const back = pending.fail(key);
    if (back) applyRowState(key, back);
    const handle = rows.get(key);
    if (handle) failRow(handle, message, back ? text : undefined, rowHost);
  }
  if (card) {
    ui.cards.errors[card] = message;
    renderCards();
  } else if (key === 'models' || key === 'rules') {
    ui.cards.errors[key] = message;
    renderCards();
  } else if (key === 'status' || key === 'fix') {
    ui.cards.errors.setup = message;
    ui.cards.open.setup = true;
    renderCards();
  } else if (!views.has(key)) {
    loading = false;
    loadError = message;
    renderBanner();
  }
  renderStatus();
}

/**
 * Record that a setting was saved: it counts as "saved" in the status text for a moment and no
 * longer as "not saved".
 *
 * @param key The setting.
 * @returns Nothing.
 */
function markSaved(key: string): void {
  failedKeys.delete(key);
  savedKeys.add(key);
  window.clearTimeout(savedTimer);
  savedTimer = window.setTimeout(() => {
    savedKeys.clear();
    renderStatus();
  }, SAVED_SHOWN_MS);
}

// ------------------------------------------------------------------ rendering

const rowHost: RowHost = {
  commit,
  unset,
  presets: () => (Array.isArray(data?.presets?.presets) ? data.presets.presets : []),
  defaultModel: () => (typeof data?.presets?.default_model === 'string' ? data.presets.default_model : ''),
  project: () => data?.project ?? null,
  isPending: (key) => pending.isPending(key),
};

const cardHost: CardHost = {
  post,
  commit,
  view: (key) => views.get(key),
  reveal,
  project: () => data?.project ?? null,
  changed: persist,
};

/**
 * List the cards in their order.
 *
 * @param shown The data to render them from.
 * @returns One entry per card; `render` returns `null` for a card that has nothing to show.
 */
function cardSpecs(shown: SettingsData): CardSpec[] {
  return [
    {
      id: 'setup',
      label: 'Setup',
      keywords: 'setup install calu uv claude database server tmux screen status keys',
      render: () => renderSetupCard(shown, ui.cards, cardHost),
    },
    {
      id: 'models',
      label: 'Models',
      keywords: 'models model presets anthropic bedrock fireworks mistral haiku sonnet roles claude effort',
      render: () => renderModelsCard(shown, ui.cards, cardHost),
    },
    {
      id: 'checkers',
      label: 'Checkers',
      keywords: 'checkers checker instructions edit enabled',
      render: () => renderCheckersCard(shown, ui.cards, cardHost),
    },
    { id: 'honesty', label: 'Honesty', keywords: 'honesty mode off track enforce', render: () => renderHonestyCard(ui.cards, cardHost) },
    {
      id: 'rules',
      label: 'Temporary rules',
      keywords: 'temporary rules rule checkers allow session project expire',
      render: () => renderRulesCard(shown, ui.cards, cardHost),
    },
  ];
}

/** The cards that are in the document, by id. */
const cardEls = new Map<CardId, HTMLElement>();

/**
 * Rebuild the cards from `data` and give the focus back to the control that had it.
 *
 * @returns Nothing. Also rebuilds the navigation, whose entries depend on the cards.
 */
function renderCards(): void {
  if (!data) return;
  const refocus = keepFocus(cardsEl);
  cardsEl.textContent = '';
  cardEls.clear();
  for (const spec of cardSpecs(data)) {
    const node = spec.render();
    if (!node) continue;
    node.dataset.keywords = spec.keywords;
    node.dataset.label = spec.label;
    cardEls.set(spec.id, node);
    cardsEl.appendChild(node);
  }
  refocus();
  renderNav();
  applyFilter();
}

/**
 * Create the DOM of one settings section with its rows.
 *
 * @param section The section.
 * @returns The handle; rows are taken from `rows` where they exist, else created.
 */
function buildSection(section: SettingsSection): SectionHandle {
  const node = el('section', 'group', '', { id: `sec-s-${section.id}`, 'aria-labelledby': `sec-s-${section.id}-title`, testid: `settings-section-${section.id}` });
  const count = el('span', 'group-count');
  add(node, add(el('h2', 'group-title', '', { id: `sec-s-${section.id}-title` }), section.label, count));
  if (section.description) node.appendChild(rich('p', 'group-desc', section.description));
  for (const view of section.rows) {
    const existing = rows.get(view.key);
    const handle = existing ? updateRow(existing, view, rowHost) : createRow(view, rowHost);
    rows.set(view.key, handle);
    node.appendChild(handle.el);
  }
  return { el: node, count, keys: section.rows.map((view) => view.key) };
}

/**
 * Bring the settings sections in line with `data`: update each row in place, or rebuild the
 * list when calu lists other keys or another order than before.
 *
 * @returns Nothing.
 */
function renderSections(): void {
  const list = buildSections(settingRows());
  views = viewsOf(list);
  const next = list.map((section) => `${section.id}:${section.rows.map((view) => view.key).join(',')}`).join('|');
  if (next !== structure) {
    structure = next;
    const refocus = keepFocus(sectionsEl);
    for (const key of [...rows.keys()]) if (!views.has(key)) rows.delete(key);
    sectionsEl.textContent = '';
    sections.clear();
    for (const section of list) {
      const handle = buildSection(section);
      sections.set(section.id, handle);
      sectionsEl.appendChild(handle.el);
    }
    refocus();
  } else {
    for (const [key, view] of views) {
      const handle = rows.get(key);
      if (handle) rows.set(key, updateRow(handle, view, rowHost));
    }
  }
  updateSectionCounts();
}

/**
 * Update the "n modified" texts of the section headings and navigation entries.
 *
 * @returns Nothing.
 */
function updateSectionCounts(): void {
  for (const [id, section] of sections) {
    const modified = section.keys.filter((key) => views.get(key)?.modified).length;
    section.count.textContent = modified > 0 ? `${modified} modified` : '';
    const badge = nav.querySelector<HTMLElement>(`[data-nav="s-${CSS.escape(id)}"] .nav-badge`);
    if (badge) {
      badge.textContent = modified > 0 ? String(modified) : '';
      badge.title = modified > 0 ? `${modified} modified` : '';
    }
  }
}

/**
 * Rebuild the navigation list and the navigation select (shown on narrow widths) from the
 * cards and sections in the document.
 *
 * @returns Nothing.
 */
function renderNav(): void {
  const refocus = keepFocus(nav);
  nav.textContent = '';
  navSelect.textContent = '';
  /**
   * Add one group of entries to the list and the select.
   *
   * @param title Heading of the group.
   * @param entries Navigation id, label and `data-testid` per entry.
   * @returns Nothing.
   */
  const group = (title: string, entries: { id: string; label: string; testid: string }[]): void => {
    if (entries.length === 0) return;
    const list = el('ul', 'nav-list');
    const options = el('optgroup', '', '', { label: title });
    for (const entry of entries) {
      const item = button(entry.label, 'nav-item', () => goTo(entry.id), { testid: entry.testid, 'data-nav': entry.id });
      item.appendChild(el('span', 'nav-badge'));
      add(list, add(el('li'), item));
      options.appendChild(new Option(entry.label, entry.id));
    }
    add(nav, el('div', 'nav-heading', title), list);
    navSelect.appendChild(options);
  };
  group(
    'Common',
    [...cardEls].map(([id, node]) => ({ id: `c-${id}`, label: node.dataset.label ?? id, testid: `settings-nav-card-${id}` })),
  );
  group(
    'All settings',
    [...sections].map(([id, section]) => ({
      id: `s-${id}`,
      label: section.el.querySelector('.group-title')?.firstChild?.textContent ?? id,
      testid: `settings-nav-${id}`,
    })),
  );
  refocus();
  updateSectionCounts();
  markActive();
}

/**
 * Return the element a navigation id stands for.
 *
 * @param id "c-<cardId>" or "s-<sectionId>".
 * @returns The card or section element, or `undefined`.
 */
function navTarget(id: string): HTMLElement | undefined {
  return id.startsWith('c-') ? cardEls.get(id.slice(2) as CardId) : sections.get(id.slice(2))?.el;
}

/**
 * Scroll to a card or section and move the focus to its heading.
 *
 * @param id The navigation id.
 * @returns Nothing.
 */
function goTo(id: string): void {
  const target = navTarget(id);
  if (!target || target.hidden) return;
  target.scrollIntoView({ block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth' });
  const heading = target.querySelector<HTMLElement>('h2');
  if (heading) {
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
}

/**
 * Tell whether the user asked for reduced motion.
 *
 * @returns True if `prefers-reduced-motion: reduce` matches.
 */
function reducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Show a setting: clear search and filter, scroll to its row, focus its control and flash the row.
 *
 * @param key The setting.
 * @returns Nothing. Does nothing if the key has no row.
 */
function reveal(key: string): void {
  const handle = rows.get(key);
  if (!handle) return;
  setSearch('', false, true);
  handle.el.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
  handle.control.focus.focus({ preventScroll: true });
  handle.el.classList.add('flash');
  window.setTimeout(() => handle.el.classList.remove('flash'), 1600);
}

/**
 * Set the search text and filter the page.
 *
 * @param text The new search text.
 * @param focus True to move the focus to the search field.
 * @param clearChip True to switch "Show only modified" off as well.
 * @returns Nothing.
 */
function setSearch(text: string, focus: boolean, clearChip = false): void {
  ui.search = text;
  if (clearChip) ui.onlyModified = false;
  if (searchInput.value !== text) searchInput.value = text;
  if (focus) searchInput.focus();
  applyFilter();
  persist();
}

/**
 * Switch the filter "Show only modified".
 *
 * @param on The new state.
 * @returns Nothing.
 */
function setOnlyModified(on: boolean): void {
  ui.onlyModified = on;
  applyFilter();
  persist();
}

/**
 * Show the rows, sections, cards and navigation entries that match the search text and the
 * "Modified" chip, hide the others, and highlight the matches. Nothing is rebuilt.
 *
 * @returns Nothing.
 */
function applyFilter(): void {
  const search = effectiveSearch(ui.search, ui.onlyModified);
  const filtering = search !== '';
  let shownRows = 0;
  for (const [id, section] of sections) {
    let visible = 0;
    for (const key of section.keys) {
      const handle = rows.get(key);
      const view = views.get(key);
      if (!handle || !view) continue;
      const match = matchesSearch(view, search);
      handle.el.hidden = !match;
      if (match) {
        visible += 1;
        paintRow(handle, ui.search);
      }
    }
    section.el.hidden = visible === 0;
    setNavHidden(`s-${id}`, visible === 0);
    shownRows += visible;
  }
  let shownCards = 0;
  for (const [id, node] of cardEls) {
    const match = cardMatches(node.dataset.keywords ?? '', search);
    node.hidden = !match;
    setNavHidden(`c-${id}`, !match);
    if (match) shownCards += 1;
  }
  for (const heading of nav.querySelectorAll<HTMLElement>('.nav-heading')) {
    const list = heading.nextElementSibling;
    heading.hidden = list !== null && [...list.children].every((item) => (item as HTMLElement).hidden);
  }
  searchClear.hidden = ui.search === '';
  modifiedChip.setAttribute('aria-pressed', ui.onlyModified ? 'true' : 'false');
  modifiedChip.classList.toggle('on', ui.onlyModified);
  countText.textContent = filtering && data?.settings ? `${shownRows} setting${shownRows === 1 ? '' : 's'} found` : '';
  emptyEl.hidden = !(filtering && shownRows === 0 && shownCards === 0 && data !== null);
  markActive();
}

/**
 * Hide or show a navigation entry in the list and in the select.
 *
 * @param id The navigation id.
 * @param hidden True to hide it.
 * @returns Nothing.
 */
function setNavHidden(id: string, hidden: boolean): void {
  const item = [...nav.querySelectorAll<HTMLElement>('[data-nav]')].find((node) => node.dataset.nav === id);
  if (item?.parentElement) item.parentElement.hidden = hidden;
  const option = [...navSelect.options].find((candidate) => candidate.value === id);
  if (option) option.hidden = option.disabled = hidden;
}

/**
 * Mark the navigation entry of the card or section that is at the top of the visible part.
 *
 * @returns Nothing.
 */
function markActive(): void {
  const limit = header.offsetHeight + 32;
  let active = '';
  for (const item of nav.querySelectorAll<HTMLElement>('[data-nav]')) {
    const target = navTarget(item.dataset.nav ?? '');
    if (!target || target.hidden) continue;
    if (active === '' || target.getBoundingClientRect().top <= limit) active = item.dataset.nav ?? '';
  }
  for (const item of nav.querySelectorAll<HTMLElement>('[data-nav]')) {
    if (item.dataset.nav === active) item.setAttribute('aria-current', 'true');
    else item.removeAttribute('aria-current');
  }
  if (active && navSelect.value !== active) navSelect.value = active;
}

/**
 * Handle a scroll of the page: once per animation frame, mark the active navigation entry and
 * store the scroll position.
 *
 * @returns Nothing.
 */
function onScroll(): void {
  if (scrollQueued) return;
  scrollQueued = true;
  window.requestAnimationFrame(() => {
    scrollQueued = false;
    ui.scrollY = window.scrollY;
    markActive();
    persist();
  });
}

/**
 * Update the header: the scope select, the save status and the progress line.
 *
 * @returns Nothing.
 */
function renderStatus(): void {
  const projects = Array.isArray(data?.settings?.projects) ? data.settings.projects : [];
  const names = ui.project !== null && !projects.includes(ui.project) ? [...projects, ui.project] : projects;
  scopeWrap.hidden = names.length === 0;
  const wanted = ['', ...names].join('\n');
  if (scopeSelect.dataset.options !== wanted) {
    scopeSelect.dataset.options = wanted;
    scopeSelect.textContent = '';
    scopeSelect.appendChild(new Option('Global', ''));
    for (const name of names) scopeSelect.appendChild(new Option(`Project: ${name}`, name));
  }
  scopeSelect.value = ui.project ?? '';

  // One state per setting (its row shows it); this text is the summary of all of them. A
  // refused setting counts until it is saved or its row no longer shows the refusal (the
  // user put the stored value back, or pressed Escape in the field).
  for (const key of [...failedKeys]) {
    if (!rows.get(key)?.el.classList.contains('invalid')) failedKeys.delete(key);
  }
  const summary = saveSummary(pending.size, savedKeys.size, failedKeys.size);
  if (saveStatus.textContent !== summary.text) {
    saveStatus.textContent = '';
    if (summary.text) add(saveStatus, summary.kind === 'saving' ? el('span', 'spinner') : icon(summary.kind === 'error' ? 'error' : 'check'), summary.text);
  }
  saveStatus.title = failedKeys.size > 0 ? `Not saved: ${[...failedKeys].join(', ')}. Each of these settings shows the reason.` : '';
  saveStatus.className = `save-status${summary.kind === 'idle' ? '' : ` ${summary.kind}`}`;

  const working = busyText || (loading ? (data ? 'Loading' : "Loading calu's settings") : '');
  progress.classList.toggle('shown', working !== '');
  progress.textContent = working ? `${working}…` : '';
}

/**
 * Show the problems of the last load on top of the content, with "Try again".
 *
 * @returns Nothing.
 */
function renderBanner(): void {
  const problems = [...(data?.problems ?? []), ...(loadError ? [loadError] : [])];
  banner.textContent = '';
  banner.hidden = problems.length === 0;
  if (problems.length === 0) return;
  const list = el('ul', 'banner-list');
  for (const problem of problems) list.appendChild(el('li', '', problem));
  add(
    banner,
    icon('warning'),
    add(el('div', 'banner-text', '', { role: 'alert' }), el('strong', '', 'Some parts could not be loaded.'), list),
    button(
      'Try again',
      'btn',
      () => {
        loading = true;
        post({ type: 'load', project: ui.project });
        renderStatus();
      },
      { testid: 'settings-retry' },
      'refresh',
    ),
  );
}

/**
 * Render everything from `data` (after a `data` message and at start from the stored state).
 *
 * @returns Nothing.
 */
function renderAll(): void {
  renderBanner();
  if (data) {
    renderSections();
    syncClaudeFromRows();
    renderCards();
    if (!data.settings) {
      sectionsEl.textContent = '';
      sections.clear();
      rows.clear();
      structure = '';
      sectionsEl.appendChild(el('p', 'notice', 'The settings could not be loaded. See the message above; the Setup card may say what is missing.'));
      renderNav();
    }
  }
  renderStatus();
  persist();
}

// ------------------------------------------------------------------ start

buildFrame();
window.addEventListener('message', (event: MessageEvent<SettingsHostMessage>) => {
  if (event.data && typeof event.data === 'object') receive(event.data);
});
renderAll();
if (data && ui.scrollY > 0) window.requestAnimationFrame(() => window.scrollTo(0, ui.scrollY));
// A row clears its own error line (Escape, the stored value typed again): bring the summary
// up to date after such an input.
for (const type of ['change', 'keyup', 'focusout']) {
  document.addEventListener(type, () => {
    if (failedKeys.size > 0) window.setTimeout(renderStatus, 0);
  });
}
post({ type: 'load', project: ui.project });
