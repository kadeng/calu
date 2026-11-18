/**
 * Script of the chat webview (browser context, bundled by esbuild to `dist/webview.js`).
 *
 *     load: ui = vscode.getState() (tab, drafts, side mode); build the skeleton; post "ready"
 *     host "reset" {state}  -> view = state
 *     host "event" {event}  -> view = applyEvent(view, event)
 *     any change            -> scheduleRender() -> render() once per animation frame:
 *
 *         header   session button | settings          setSection(signature): rebuilt only on change
 *                  Checkers | Context | Honesty | level
 *         tabs     Main + one per subagent            (hidden while there is no subagent)
 *         setup    cards of the empty / error state   (setupCards)
 *         list     transcript of the visible tab      per-item cache keyed by object identity
 *         dock     escalations of other tabs | side questions | queue | activity | composer
 *
 *     clicks, keys, changes -> one delegated handler each, driven by `data-action`
 *
 * View-only state (visible tab, drafts per session, side mode) is saved with `vscode.setState`;
 * the view state itself comes from the host after every "ready". All text from calu is inserted
 * as text nodes or through `markdown()` (markdown-it with raw HTML disabled).
 */

import { ACTIVITY_VERBS, FRAME_MS, activityLine, sessionPhase } from '../src/shared/activity';
import {
  buildFileIndex,
  checkImage,
  newestItemIds,
  oneLine,
  otherOpenEscalations,
  parseImageDataUrl,
  rateLimitLine,
  sessionActionMessage,
  sessionLabel,
  sessionMenu,
  setupCards,
  tabList,
  usageMeter,
  linkMessage,
  type FileIndex,
  type SessionMenuAction,
  type SetupCard,
} from '../src/shared/chatModel';
import { fileClick, menuFor, rowClick, type ItemActionId } from '../src/shared/itemMenu';
import { LEVELS, levelLabel, levelOf, type LevelId } from '../src/shared/levels';
import {
  CLAUDE_EFFORTS,
  CLAUDE_MODELS,
  HONESTY_MODES,
  MAX_TEXT,
  type ChatSettings,
  type ClaudeEffort,
  type HonestyMode,
  type HostMessage,
  type OutgoingImage,
  type WebviewMessage,
} from '../src/shared/messages';
import { allCommands, completions, resolveSlash, type Completion, type SlashUiAction } from '../src/shared/slashCommands';
import {
  MAIN,
  applyEvent,
  formatAnswer,
  honestyChip,
  initialViewState,
  isAttached,
  messageTarget,
  openEscalationCount,
  own,
  type ViewState,
} from '../src/shared/viewState';
import { actionButton, copyText, el, icon, iconButton, logo, reveal, sameChildren, setSection } from './dom';
import { draftOf, itemDeps, renderItem, renderSubagentHead, subagentHeadDeps, type Ctx } from './items';
import { markdown } from './markdown';
import { closePopup, closePopupIfOutside, openPopup, openPopupId, refreshPopup } from './popup';

/** The API object that VSCode injects into webviews. */
interface VsCodeApi {
  postMessage(message: WebviewMessage): void;
  getState(): unknown;
  setState(state: unknown): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

/** View-only state that survives reloads of the webview (`vscode.setState`). */
interface UiState {
  /** Visible tab: "main" or an agent id. */
  tab: string;
  /** Unsent composer text per session id ("_" while no session is attached). */
  drafts: Record<string, string>;
  /** Highest `ViewState.focus.seq` already handled (tab switch done). */
  focusSeq: number;
  /** True while the composer asks side questions. */
  side: boolean;
}

/** Most drafts kept in `UiState.drafts`. */
const MAX_DRAFTS = 20;

const vscode = acquireVsCodeApi();

/**
 * Read the saved view-only state defensively (an older version may have saved another shape).
 *
 * @returns The saved state with defaults for missing or mistyped members.
 */
function loadUi(): UiState {
  const saved = (vscode.getState() ?? {}) as { ui?: Partial<UiState> };
  const ui = saved.ui ?? {};
  const drafts: Record<string, string> = Object.create(null) as Record<string, string>;
  if (ui.drafts && typeof ui.drafts === 'object') {
    for (const [k, v] of Object.entries(ui.drafts)) if (typeof v === 'string') drafts[k] = v;
  }
  return {
    tab: typeof ui.tab === 'string' ? ui.tab : MAIN,
    drafts,
    focusSeq: typeof ui.focusSeq === 'number' ? ui.focusSeq : 0,
    side: ui.side === true,
  };
}

const ui = loadUi();
let view: ViewState = initialViewState();
let settings: ChatSettings = { showThinking: true, escalationSound: true };

let fileIndexFor: ViewState['tools'] | undefined;
let fileIndexCache: FileIndex = new Map();

/** The context of the item renderers (`items.ts`); `view`, `settings` and `attached` are kept current. */
const ctx: Ctx = {
  view,
  settings,
  attached: false,
  expanded: new Set(),
  openTools: new Set(),
  escDrafts: Object.create(null) as Ctx['escDrafts'],
  fileIndex: () => {
    if (fileIndexFor !== view.tools) {
      fileIndexFor = view.tools;
      fileIndexCache = buildFileIndex(view.tools);
    }
    return fileIndexCache;
  },
};

/** Rendered item elements by item id, with the dependencies they were rendered from. */
const itemCache = new Map<number, { deps: unknown[]; el: HTMLElement }>();
let headCache: { deps: unknown[]; el: HTMLElement | undefined } | undefined;
/** The placeholder of an empty transcript, kept so that it is not rebuilt on every render. */
let emptyCache: { text: string; el: HTMLElement } | undefined;
/** Per tab the id of the newest item the user has seen. */
let seen: Record<string, number> = Object.create(null) as Record<string, number>;
/** Per tab the scroll position when the user left it ("bottom" = follow new output). */
const scrollPos = new Map<string, number | 'bottom'>();
/** True while the list follows new output (the user has not scrolled up). */
let stick = true;
let renderedTab = ui.tab;
let renderQueued = false;
/** Images attached to the message being written (not saved across reloads). */
let attachments: OutgoingImage[] = [];
/** Keys of the open escalations already announced to screen readers. */
let announced = new Set<string>();
/** True after a focus request of the state: the next render moves the focus to the new card. */
let focusNewCard = false;
/** The draft key the composer text currently belongs to. */
let draftKey = '_';

// ------------------------------------------------------------------ skeleton

let sessionSlot!: HTMLElement;
let quickSlot!: HTMLElement;
let bannerSlot!: HTMLElement;
let tabsSlot!: HTMLElement;
let setupSlot!: HTMLElement;
let transcriptWrap!: HTMLElement;
let list!: HTMLElement;
let jumpButton!: HTMLButtonElement;
let stripSlot!: HTMLElement;
let sideSlot!: HTMLElement;
let queueSlot!: HTMLElement;
let activityEl!: HTMLElement;
let composer!: HTMLElement;
let completionBox!: HTMLElement;
let thumbsSlot!: HTMLElement;
let input!: HTMLTextAreaElement;
let footerSlot!: HTMLElement;
let sendButton!: HTMLButtonElement;
let interruptButton!: HTMLButtonElement;
let attachButton!: HTMLButtonElement;
let sideButton!: HTMLButtonElement;
let fileInput!: HTMLInputElement;
let hintEl!: HTMLElement;
let announcer!: HTMLElement;

/**
 * Build the static page skeleton (once).
 *
 * @returns Nothing. Side effect: fills `#app` and sets the slot variables.
 */
function buildSkeleton(): void {
  const app = document.getElementById('app')!;
  const header = el('header', 'header');
  const top = el('div', 'header-row');
  sessionSlot = el('div', 'session-slot');
  top.append(sessionSlot, iconButton('gear', 'Open settings', 'post', { class: 'quiet', 'data-type': 'openSettings', 'data-testid': 'header-settings' }));
  quickSlot = el('div', 'header-row quick');
  bannerSlot = el('div', 'banner-slot');
  header.append(top, quickSlot, bannerSlot);

  tabsSlot = el('div', 'tabs-slot');
  setupSlot = el('div', 'setup-slot');
  const wrap = el('div', 'transcript');
  list = el('main', 'list', undefined, { id: 'transcript', role: 'tabpanel', tabindex: '-1', 'data-testid': 'chat-list' });
  jumpButton = actionButton('', 'jump', { class: 'jump', title: 'Jump to latest', 'aria-label': 'Jump to latest' });
  jumpButton.append(icon('arrowDown'), el('span', '', 'Latest'));
  jumpButton.hidden = true;
  wrap.append(list, jumpButton);
  transcriptWrap = wrap;

  const dock = el('footer', 'dock');
  stripSlot = el('div', 'strip-slot');
  sideSlot = el('div', 'side-slot');
  queueSlot = el('div', 'queue-slot', undefined, { 'data-testid': 'queue' });
  activityEl = el('div', 'activity idle', undefined, { 'data-testid': 'activity', 'aria-hidden': 'true' });
  activityEl.append(el('span', 'activity-icon'), el('span', 'activity-label', 'Idle'), el('span', 'activity-elapsed'));

  composer = el('div', 'composer');
  completionBox = el('div', 'completions', undefined, { id: 'completions', role: 'listbox', 'aria-label': 'Commands' });
  completionBox.hidden = true;
  thumbsSlot = el('div', 'thumbs-slot');
  input = el('textarea', 'input', undefined, {
    rows: '1',
    'data-testid': 'input-text',
    'aria-label': 'Message',
    'aria-autocomplete': 'list',
    'aria-controls': 'completions',
    maxlength: String(MAX_TEXT),
  });
  const footer = el('div', 'composer-footer');
  footerSlot = el('div', 'footer-slot');
  fileInput = el('input', 'hidden', undefined, { type: 'file', accept: 'image/png,image/jpeg,image/gif,image/webp', multiple: '', tabindex: '-1', 'aria-hidden': 'true' });
  attachButton = iconButton('attach', 'Attach an image', 'attach', { class: 'quiet', title: 'Attach an image (PNG, JPEG, GIF, WebP); you can also paste or drop one' });
  sideButton = iconButton('side', 'Side question', 'toggleSide', {
    class: 'quiet',
    'data-testid': 'side-toggle',
    'aria-pressed': 'false',
    title: 'Side question: ask about the running work without becoming part of the conversation',
  });
  interruptButton = iconButton('stop', 'Interrupt', 'post', { class: 'secondary round', 'data-type': 'interrupt', 'data-testid': 'input-interrupt', title: "Interrupt Claude's running turn" });
  sendButton = iconButton('send', 'Send', 'send', { class: 'primary round', 'data-testid': 'input-send' });
  const buttons = el('div', 'composer-buttons');
  buttons.append(attachButton, sideButton, interruptButton, sendButton);
  footer.append(footerSlot, buttons);
  composer.append(completionBox, thumbsSlot, input, footer, fileInput);
  hintEl = el('div', 'hint', undefined, { 'data-testid': 'input-hint' });
  dock.append(stripSlot, sideSlot, queueSlot, activityEl, composer, hintEl);

  announcer = el('div', 'sr-only', undefined, { 'aria-live': 'polite', role: 'status' });
  app.append(header, tabsSlot, setupSlot, wrap, dock, announcer);
}

/**
 * Tell screen readers something (a polite live region).
 *
 * @param text One sentence.
 * @returns Nothing.
 */
function announce(text: string): void {
  announcer.textContent = text;
}

// ------------------------------------------------------------------ header

/**
 * Build the session dropdown's button: state dot, the attached session's name, a badge with the
 * open escalations of other sessions, and a chevron.
 *
 * @returns The button.
 */
function buildSessionButton(): HTMLElement[] {
  const label = sessionLabel(view);
  const b = actionButton('', 'sessionMenu', {
    class: 'session-button',
    'data-testid': 'session-dropdown',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    title: `${label.title}\nClick to switch, start, rename or leave sessions.`,
  });
  b.append(el('span', `dot ${label.dot}`), el('span', 'session-name', label.label));
  const elsewhere = view.sessions.list.filter((s) => s.id !== view.sessions.attachedId).reduce((n, s) => n + s.openEscalations, 0);
  if (elsewhere > 0) b.append(el('span', 'badge', String(elsewhere), { title: `${elsewhere} step(s) wait for a decision in other sessions` }));
  b.append(icon('chevronDown'));
  return [b];
}

/**
 * Build the content of the session dropdown (see `sessionMenu`).
 *
 * @returns A `role="menu"` element.
 */
function buildSessionMenu(): HTMLElement {
  const menu = el('div', 'menu session-menu', undefined, { role: 'menu', 'aria-label': 'Sessions' });
  let separated = false;
  for (const entry of sessionMenu(view)) {
    if (entry.kind === 'heading') menu.append(el('div', 'menu-heading', entry.label, { role: 'presentation' }));
    else if (entry.kind === 'note') menu.append(el('div', 'menu-note', entry.label, { role: 'presentation' }));
    else if (entry.kind === 'session') {
      const b = actionButton('', 'switchSession', {
        class: 'menu-item session-entry',
        role: 'menuitemradio',
        'aria-checked': String(entry.attached),
        'data-id': entry.id,
        'data-testid': `session-entry-${entry.id}`,
        title: `${entry.directoryFull}\n${entry.state}${entry.stale ? ' (its process seems to be gone)' : ''}`,
      });
      const first = el('span', 'entry-line');
      first.append(el('span', `dot ${entry.stale ? 'off' : entry.phase}`), el('span', 'entry-title', entry.title));
      if (entry.openEscalations > 0) first.append(el('span', 'badge', String(entry.openEscalations), { title: `${entry.openEscalations} step(s) wait for a decision` }));
      if (entry.stale) first.append(el('span', 'chip', 'stale'));
      if (entry.attached) first.append(icon('check'));
      b.append(first, el('span', 'entry-directory', entry.directory));
      menu.append(b);
    } else {
      if (!separated) {
        menu.append(el('div', 'menu-separator', undefined, { role: 'separator' }));
        separated = true;
      }
      const b = actionButton(entry.label, 'sessionAction', { class: 'menu-item', role: 'menuitem', 'data-id': entry.id });
      b.disabled = !entry.enabled;
      menu.append(b);
    }
  }
  return menu;
}

/**
 * Open the session dropdown below its button (or close it if it is open) and ask the host for a
 * fresh list.
 *
 * @returns Nothing.
 */
function toggleSessionMenu(): void {
  if (openPopupId() === 'sessions') {
    closePopup();
    return;
  }
  const anchor = sessionSlot.querySelector('button');
  if (!anchor) return;
  openPopup('sessions', buildSessionMenu(), { anchor });
  vscode.postMessage({ type: 'refreshSessions' });
}

/**
 * Build a `<select>` of the header or the composer footer.
 *
 * @param name Value of `data-select` ("model", "effort" or "level").
 * @param testid Value of `data-testid`.
 * @param label Accessible name and start of the tooltip.
 * @param options `[value, text, title?]` per option; an option with the value "" is disabled.
 * @param current The selected value.
 * @param disabled True to disable the control.
 * @returns The element.
 */
function buildSelect(name: string, testid: string, label: string, options: readonly (readonly [string, string])[], current: string, disabled: boolean): HTMLSelectElement {
  const select = el('select', 'pill', undefined, { 'data-select': name, 'data-testid': testid, 'aria-label': label, title: label });
  for (const [value, text] of options) {
    const option = el('option', '', text, { value });
    if (value === '') option.disabled = true;
    select.append(option);
  }
  select.value = current;
  select.disabled = disabled;
  return select;
}

/**
 * Build the quick row of calu's own features: Checkers, Context, the honesty chip and the
 * supervision level.
 *
 * @returns The elements of the row.
 */
function buildQuickRow(): HTMLElement[] {
  const attached = isAttached(view);
  const checkers = actionButton('', 'post', { class: 'quiet-button', 'data-type': 'editChecker', title: "Edit a checker's instructions" });
  checkers.append(icon('checklist'), el('span', '', 'Checkers'));
  const context = actionButton('', 'post', { class: 'quiet-button', 'data-type': 'showContext', title: 'Show the conversation as the checkers see it' });
  context.append(icon('book'), el('span', '', 'Context'));
  context.disabled = !attached;
  const chip = honestyChip(view.honesty);
  const honesty = actionButton('', 'honestyMenu', {
    class: `quiet-button honesty ${chip.kind}`,
    'data-testid': 'header-honesty',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    title: `${chip.label || 'Code honesty is off'}. Click for the mode, the open marks and a review.`,
  });
  honesty.append(icon('shield'), el('span', '', chip.kind === 'off' ? 'Honesty: off' : chip.label));
  const level = levelOf(view.autoEscalate);
  const options: [string, string][] = LEVELS.map((l) => [l.id, l.name]);
  if (level === 'custom') options.unshift(['', levelLabel(view.autoEscalate)]);
  const select = buildSelect('level', 'header-level', 'Supervision level: which steps wait for you', options, level === 'custom' ? '' : level, !attached);
  return [checkers, context, honesty, select];
}

/**
 * Build the content of the honesty menu: the mode, the open marks with "Clear", and the
 * actions "Review now" and "Trust HEAD".
 *
 * @returns A `role="menu"` element.
 */
function buildHonestyMenu(): HTMLElement {
  const h = view.honesty;
  const mode = h?.mode && (HONESTY_MODES as readonly string[]).includes(h.mode) ? h.mode : 'off';
  const attached = isAttached(view);
  const menu = el('div', 'menu honesty-menu', undefined, { role: 'menu', 'aria-label': 'Code honesty' });
  menu.append(el('div', 'menu-heading', 'Code honesty mode', { role: 'presentation' }));
  const texts: Record<HonestyMode, string> = {
    off: 'Off',
    track: 'Track: record marks and unreviewed files',
    enforce: 'Enforce: project code and commits need a clean state',
  };
  for (const m of HONESTY_MODES) {
    menu.append(actionButton(texts[m], 'honestyMode', { class: 'menu-item', role: 'menuitemradio', 'aria-checked': String(m === mode), 'data-id': m }));
  }
  menu.append(el('div', 'menu-note', 'The mode is stored in calu\'s settings; a running session keeps the mode it started with.', { role: 'presentation' }));
  if (h && mode !== 'off') {
    const chip = honestyChip(h);
    menu.append(el('div', 'menu-separator', undefined, { role: 'separator' }), el('div', 'menu-heading', chip.label, { role: 'presentation' }));
    for (const mark of h.marks ?? []) {
      const row = el('div', 'mark');
      const text = el('div', 'mark-text');
      text.append(el('div', 'mark-path', `${mark.path}${mark.symbol ? ` · ${mark.symbol}` : ''}`, { title: mark.path }), el('div', 'mark-reason', oneLine(mark.reason, 240)));
      const clear = actionButton('Clear', 'honestyClear', { class: 'text-button', role: 'menuitem', 'data-id': String(mark.id), title: 'Close this mark by hand' });
      clear.disabled = !attached;
      row.append(text, clear);
      menu.append(row);
    }
    const unreviewed = h.unreviewed_all ? 'All files are unreviewed.' : (h.unreviewed_count ?? 0) > 0 ? `${h.unreviewed_count} unreviewed file(s): ${(h.unreviewed ?? []).slice(0, 5).join(', ')}${(h.unreviewed_count ?? 0) > 5 ? ', …' : ''}` : '';
    if (unreviewed) menu.append(el('div', 'menu-note', unreviewed, { role: 'presentation' }));
    const review = actionButton(h.review_running ? 'Review running…' : 'Review now', 'post', { class: 'menu-item', role: 'menuitem', 'data-type': 'honestyReview' });
    review.disabled = !attached || h.review_running === true;
    const trust = actionButton('Trust HEAD…', 'post', { class: 'menu-item', role: 'menuitem', 'data-type': 'honestyTrust', title: 'Record the current git HEAD as honest (asks first)' });
    trust.disabled = !attached;
    menu.append(el('div', 'menu-separator', undefined, { role: 'separator' }), review, trust);
  }
  return menu;
}

/**
 * Open the honesty menu below its chip, or close it if it is open.
 *
 * @returns Nothing.
 */
function toggleHonestyMenu(): void {
  if (openPopupId() === 'honesty') {
    closePopup();
    return;
  }
  const anchor = quickSlot.querySelector('[data-testid="header-honesty"]');
  if (anchor) openPopup('honesty', buildHonestyMenu(), { anchor });
}

/**
 * Build the banner below the header: the state of a connection that is not healthy.
 *
 * @returns One line, or nothing while the session is attached and reachable.
 */
function buildBanner(): HTMLElement[] {
  if (view.bridge.status !== 'running' || view.link.status !== 'reconnecting') return [];
  return [el('div', 'banner warning', `Reconnecting to the session…${view.link.message ? ` ${view.link.message}` : ''}`, { role: 'status' })];
}

/**
 * Update the header slots and an open session or honesty menu.
 *
 * @returns Nothing.
 */
function renderHeader(): void {
  // A rebuilt button is a new element: an open menu moves to it and shows the new entries.
  const sessionsSig = JSON.stringify([sessionLabel(view), view.sessions, view.session.claudeSessionId]);
  if (setSection(sessionSlot, sessionsSig, buildSessionButton)) {
    const anchor = sessionSlot.querySelector('button');
    if (anchor) refreshPopup('sessions', buildSessionMenu(), { anchor });
  }
  const quickSig = JSON.stringify([view.honesty, view.autoEscalate, isAttached(view)]);
  if (setSection(quickSlot, quickSig, buildQuickRow)) {
    const anchor = quickSlot.querySelector('[data-testid="header-honesty"]');
    if (anchor) refreshPopup('honesty', buildHonestyMenu(), { anchor });
  }
  setSection(bannerSlot, JSON.stringify([view.bridge.status, view.link]), buildBanner);
}

// ------------------------------------------------------------------ tabs

/**
 * Build the tab bar (`role="tablist"`): Main and one tab per subagent with a status mark, a
 * background mark, the number of open escalations and a dot for unseen output.
 *
 * @returns The bar, or nothing while the session has no subagent.
 */
function buildTabs(): HTMLElement[] {
  const tabs = tabList(view, ui.tab, seen);
  if (tabs.length < 2) return [];
  const nav = el('div', 'tabs', undefined, { role: 'tablist', 'aria-label': 'Agents' });
  for (const t of tabs) {
    const active = t.key === ui.tab;
    const title = t.key === MAIN ? 'The main agent' : `${t.label} (${t.status}${t.background ? ', background' : ''})`;
    const b = actionButton('', 'tab', {
      class: `tab${active ? ' active' : ''}`,
      role: 'tab',
      'aria-selected': String(active),
      'aria-controls': 'transcript',
      tabindex: active ? '0' : '-1',
      'data-tab': t.key,
      'data-testid': t.key === MAIN ? 'tab-main' : `tab-${t.key}`,
      title: `${title}${t.openEscalations ? `\n${t.openEscalations} step(s) wait for your decision` : ''}`,
    });
    b.append(el('span', 'tab-label', t.label));
    if (t.status === 'running') b.append(el('span', 'status-mark running', undefined, { 'aria-hidden': 'true' }));
    if (t.status) b.append(el('span', `chip agent-status ${t.status}`, t.status));
    if (t.background) b.append(el('span', 'chip', 'bg', { title: 'Runs in the background' }));
    if (t.openEscalations > 0) b.append(el('span', 'badge', String(t.openEscalations)));
    else if (t.attention) b.append(el('span', 'attention', undefined, { title: 'New output', 'aria-label': 'new output' }));
    nav.append(b);
  }
  return [nav];
}

/**
 * Update the tab bar; a tab that had the keyboard focus keeps it.
 *
 * @returns Nothing.
 */
function renderTabs(): void {
  if (ui.tab !== MAIN && !view.agents.some((a) => a.agent_id === ui.tab)) ui.tab = MAIN;
  const focused = document.activeElement instanceof HTMLElement && tabsSlot.contains(document.activeElement);
  const sig = JSON.stringify(tabList(view, ui.tab, seen));
  if (setSection(tabsSlot, sig, buildTabs)) {
    const active = tabsSlot.querySelector<HTMLElement>('[aria-selected="true"]');
    const strip = tabsSlot.querySelector<HTMLElement>('.tabs');
    if (strip) reveal(strip, active);
    if (focused) active?.focus({ preventScroll: true });
  }
  const selected = tabsSlot.querySelector('[aria-selected="true"]');
  if (selected) list.setAttribute('aria-label', `Transcript: ${selected.querySelector('.tab-label')?.textContent ?? ''}`);
  else list.setAttribute('aria-label', 'Transcript');
}

/**
 * Show another tab: remember the scroll position of the current one, then render.
 *
 * @param tab `MAIN` or an agent id.
 * @returns Nothing.
 */
function switchTab(tab: string): void {
  if (tab === ui.tab) return;
  ui.tab = tab;
  render();
}

// ------------------------------------------------------------------ empty and error states

/**
 * Build one card of the empty / error state.
 *
 * @param card The card (see `setupCards`).
 * @returns The element, with `data-testid="hint-<id>"`.
 */
function buildSetupCard(card: SetupCard): HTMLElement {
  const box = el('section', `setup-card ${card.level}`, undefined, { 'data-testid': `hint-${card.id}` });
  box.append(el('p', 'setup-message', card.message));
  const help = view.serverHelp;
  if (card.serverHelp && help) {
    const code = el('div', 'code-block', undefined, { 'data-testid': 'server-help' });
    const copy = actionButton('', 'post', { class: 'copy-button', 'data-type': 'copyServerCommand', title: 'Copy command', 'aria-label': 'Copy command' });
    copy.append(icon('copy'));
    code.append(el('pre', '', help.command), copy);
    box.append(code, el('p', 'setup-note', `${help.note}${help.detach ? ` Detach with ${help.detach}.` : ''}`));
  }
  if (card.sessionList && view.sessions.list.length > 0) {
    const pick = el('div', 'setup-sessions', undefined, { role: 'group', 'aria-label': 'Running sessions' });
    pick.append(el('div', 'label', 'Or attach to a running session'));
    for (const entry of sessionMenu(view)) {
      if (entry.kind !== 'session') continue;
      const b = actionButton('', 'switchSession', { class: 'menu-item session-entry', 'data-id': entry.id, title: entry.directoryFull });
      const first = el('span', 'entry-line');
      first.append(el('span', `dot ${entry.stale ? 'off' : entry.phase}`), el('span', 'entry-title', entry.title));
      if (entry.openEscalations > 0) first.append(el('span', 'badge', String(entry.openEscalations)));
      b.append(first, el('span', 'entry-directory', entry.directory));
      pick.append(b);
    }
    box.append(pick);
  }
  const row = el('div', 'setup-actions');
  if (card.primary) {
    row.append(actionButton(card.primary.label, 'postJson', { class: 'primary', 'data-message': JSON.stringify(card.primary.message), 'data-testid': `hint-action-${card.id}` }));
  }
  for (const action of card.secondary) {
    row.append(actionButton(action.label, 'postJson', { class: 'secondary', 'data-message': JSON.stringify(action.message) }));
  }
  if (row.childElementCount > 0) box.append(row);
  return box;
}

/**
 * Update the setup area above the transcript. With an empty transcript it fills the view and
 * shows calu's mark; otherwise it is a compact block above the transcript.
 *
 * @returns True if the transcript is empty and the setup area replaces it.
 */
function renderSetup(): boolean {
  const cards = setupCards(view);
  const empty = (own(view.items, MAIN) ?? []).length === 0 && view.agents.length === 0;
  const full = empty && cards.length > 0;
  setupSlot.className = `setup-slot${full ? ' full' : ''}`;
  setSection(setupSlot, JSON.stringify([cards, view.serverHelp, cards.some((c) => c.sessionList) ? view.sessions.list : 0, full]), () => [
    ...(full ? [logo()] : []),
    ...cards.map(buildSetupCard),
  ]);
  return full;
}

// ------------------------------------------------------------------ transcript

/**
 * Compare two dependency lists entry by entry.
 *
 * @param a A list of `itemDeps`.
 * @param b Another one.
 * @returns True if they have the same length and identical (`===`) entries.
 */
function sameDeps(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/**
 * Tell whether the list is scrolled to (nearly) its end.
 *
 * @returns True within 40 pixels of the end.
 */
function nearBottom(): boolean {
  return list.scrollHeight - list.scrollTop - list.clientHeight < 40;
}

/**
 * Update the transcript of the visible tab, reusing the elements of unchanged items.
 *
 *     for each item: deps = itemDeps(item)
 *        cached with identical deps      -> keep the element
 *        streaming text of a cached item -> swap the children in place (no reordering)
 *        else                            -> build a new element (an open <details> stays open)
 *
 * The list follows new output only while the user has not scrolled up (`stick`).
 *
 * @param hidden True if the setup area replaces the transcript.
 * @returns Nothing.
 */
function renderList(hidden: boolean): void {
  if (renderedTab !== ui.tab) {
    scrollPos.set(renderedTab, stick ? 'bottom' : list.scrollTop);
    renderedTab = ui.tab;
    const saved = scrollPos.get(ui.tab) ?? 'bottom';
    stick = saved === 'bottom';
    list.replaceChildren();
    requestAnimationFrame(() => {
      if (saved !== 'bottom') list.scrollTop = saved;
    });
  }
  transcriptWrap.hidden = hidden;
  const items = (own(view.items, ui.tab) ?? []).filter((i) => i.kind !== 'launch' && (settings.showThinking || i.kind !== 'thinking'));
  const wanted: HTMLElement[] = [];
  const headDeps = subagentHeadDeps(ui.tab, ctx);
  if (!headCache || !sameDeps(headCache.deps, headDeps)) headCache = { deps: headDeps, el: renderSubagentHead(ui.tab, ctx) };
  if (headCache.el) wanted.push(headCache.el);
  for (const item of items) {
    const deps = itemDeps(item, ctx);
    let cached = itemCache.get(item.id);
    if (!cached || !sameDeps(cached.deps, deps)) {
      const fresh = renderItem(item, ctx);
      if (cached && (item.kind === 'assistant' || item.kind === 'thinking')) {
        cached.el.className = fresh.className;
        cached.el.replaceChildren(...fresh.childNodes);
        cached.deps = deps;
      } else {
        if (cached && cached.el instanceof HTMLDetailsElement && fresh instanceof HTMLDetailsElement) fresh.open = cached.el.open;
        const inner = cached?.el.querySelector('details.esc-literal');
        if (inner instanceof HTMLDetailsElement && inner.open) fresh.querySelector<HTMLDetailsElement>('details.esc-literal')?.setAttribute('open', '');
        const hadFocus = cached?.el.contains(document.activeElement) ? focusKey(document.activeElement) : undefined;
        cached = { deps, el: fresh };
        itemCache.set(item.id, cached);
        if (hadFocus) requestAnimationFrame(() => fresh.querySelector<HTMLElement>(hadFocus)?.focus());
      }
    }
    wanted.push(cached.el);
  }
  if (itemCache.size > items.length + 200) pruneItemCache();
  const hasContent = wanted.length > 0;
  if (!hasContent && isAttached(view)) {
    const text =
      ui.tab === MAIN
        ? 'Write a message to Claude. Checkers review its commands and edits; steps that need you appear here.'
        : 'This subagent has not produced output yet.';
    if (!emptyCache || emptyCache.text !== text) emptyCache = { text, el: emptyState(text) };
    wanted.push(emptyCache.el);
  }
  // Only touch the DOM order when items were added, removed or replaced.
  if (!sameChildren(list, wanted)) list.replaceChildren(...wanted);
  if (stick) list.scrollTop = list.scrollHeight;
  jumpButton.hidden = hidden || stick || !hasContent;
  if (!document.hidden && items.length > 0) seen[ui.tab] = items[items.length - 1].id;
}

/**
 * Build the quiet placeholder of an empty transcript.
 *
 * @param text One sentence.
 * @returns The element with calu's mark.
 */
function emptyState(text: string): HTMLElement {
  const box = el('div', 'empty');
  box.append(logo(), el('p', '', text));
  return box;
}

/**
 * Describe the focused element inside an item so that the same control can be focused again
 * after the item was rebuilt.
 *
 * @param node The focused element.
 * @returns A CSS selector relative to the item, or `undefined` if the element cannot be found again.
 */
function focusKey(node: Element | null): string | undefined {
  if (!(node instanceof HTMLElement)) return undefined;
  if (node.dataset.testid) return `[data-testid="${CSS.escape(node.dataset.testid)}"]`;
  if (node.dataset.action) return `[data-action="${CSS.escape(node.dataset.action)}"]`;
  return undefined;
}

/**
 * Drop cached elements of items that no longer exist in any tab.
 *
 * @returns Nothing.
 */
function pruneItemCache(): void {
  const all = new Set<number>();
  for (const key of Object.keys(view.items)) for (const item of view.items[key]) all.add(item.id);
  for (const id of [...itemCache.keys()]) if (!all.has(id)) itemCache.delete(id);
}

/**
 * After a focus request of the state (a new escalation): scroll its card into view and, unless
 * the user is typing, move the focus to the card's first button. Nothing is submitted: the
 * decision always needs a deliberate key press or click.
 *
 * @returns Nothing.
 */
function focusOpenCard(): void {
  if (!focusNewCard) return;
  focusNewCard = false;
  const cards = list.querySelectorAll<HTMLElement>('.esc.open');
  const card = cards[cards.length - 1];
  if (!card) return;
  reveal(list, card);
  const active = document.activeElement;
  const typing = (active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement) && active.value.trim() !== '';
  if (!document.hasFocus() || typing) return;
  card.querySelector<HTMLElement>('.esc-actions button:not(:disabled), .option:not(:disabled)')?.focus({ preventScroll: true });
}

/**
 * Announce escalations that opened since the last render to screen readers.
 *
 * @returns Nothing.
 */
function announceEscalations(): void {
  const open = Object.values(view.escalations).filter((e) => e.status === 'open');
  const fresh = open.filter((e) => !announced.has(e.key));
  announced = new Set(open.map((e) => e.key));
  if (fresh.length === 0) return;
  const e = fresh[fresh.length - 1];
  announce(e.questions.length > 0 ? `Claude asks: ${oneLine(e.questions[0].question, 200)}` : `Approval needed: ${e.toolName}. ${oneLine(e.reason, 200)}`);
}

// ------------------------------------------------------------------ dock: strip, side, queue

/**
 * Build the strip of open escalations in tabs that are not visible.
 *
 * @returns The strip, or nothing if there is none.
 */
function buildStrip(): HTMLElement[] {
  const entries = otherOpenEscalations(view, ui.tab);
  if (entries.length === 0) return [];
  const strip = el('div', 'strip', undefined, { role: 'group', 'aria-label': 'Steps that wait for your decision in other tabs' });
  strip.append(icon('pause'), el('span', 'strip-label', `${entries.length} waiting in other tabs:`));
  for (const e of entries) {
    strip.append(actionButton(e.label, 'jumpEsc', { class: 'chip-button', 'data-tab': e.tab, 'data-key': e.key, title: 'Show this step' }));
  }
  return [strip];
}

/**
 * Build the panel of side questions: clearly apart from the conversation.
 *
 * @returns The panel while the side mode is on, else nothing.
 */
function buildSide(): HTMLElement[] {
  if (!ui.side) return [];
  const panel = el('section', 'side', undefined, { 'aria-label': 'Side questions' });
  const head = el('div', 'side-head');
  head.append(icon('side'), el('span', '', 'Side questions — not part of the conversation; Claude does not see them'));
  head.append(iconButton('close', 'Close side questions', 'toggleSide', { class: 'quiet' }));
  panel.append(head);
  const body = el('div', 'side-body');
  if (view.side.length === 0) body.append(el('div', 'side-empty', 'Ask about the running work without interrupting it.'));
  for (const s of view.side) {
    const q = el('div', 'side-question', s.question);
    body.append(q);
    if (s.error !== undefined) body.append(el('div', 'side-answer error', s.error));
    else if (s.answer !== undefined) body.append(markdown(s.answer || '(no answer)', 'md side-answer'));
    else body.append(el('div', 'side-answer waiting', 'Waiting for the answer…'));
  }
  panel.append(body);
  return [panel];
}

/**
 * Return the display name of a subagent.
 *
 * @param agentId The subagent's id.
 * @returns Its description, or the last 6 characters of the id.
 */
function agentName(agentId: string): string {
  return view.agents.find((a) => a.agent_id === agentId)?.description || agentId.slice(-6);
}

/**
 * Build the list of chat messages that Claude has not taken in yet.
 *
 * @returns One pending entry per queued message (`data-testid="queue-<id>"`).
 */
function buildQueue(): HTMLElement[] {
  return view.queue.map((m) => {
    const when = m.delivery === 'inject' ? 'after the running tool calls' : 'for the next turn';
    const to = m.to === MAIN ? '' : ` · to ${agentName(m.to)}`;
    const entry = el('div', `queued ${m.delivery}`, undefined, { 'data-testid': `queue-${m.id}` });
    entry.append(el('span', 'queued-label', `Pending ${when}${to}`), el('span', 'queued-text', oneLine(m.text, 240)));
    return entry;
  });
}

// ------------------------------------------------------------------ activity line

/** Start (ms) of the current or last working period (busy or waiting). */
let activitySince = Date.now();
/** Length of the last finished working period; undefined before the first one in this view. */
let activityLastMs: number | undefined;
/** Selects the first verb of a working period. */
let activitySeed = 0;
let activityWasActive = false;
let activityKind = 'idle';
let activityTimer: ReturnType<typeof setInterval> | undefined;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

/**
 * Update the activity line above the composer from the session state (`shared/activity.ts`).
 * While Claude works a timer redraws it (spinner, verb, elapsed time); with "reduce motion" the
 * spinner stands still and the line is redrawn once per second. A change of its kind (working,
 * waiting for you, done) is announced to screen readers; the line itself is hidden from them
 * because it changes several times per second.
 *
 * @returns Nothing.
 */
function updateActivity(): void {
  const connected = isAttached(view);
  const state = view.session.state;
  const phase = sessionPhase(state);
  const active = connected && (phase === 'busy' || phase === 'waiting');
  const now = Date.now();
  if (active && !activityWasActive) {
    activitySince = now;
    activitySeed = Math.floor(Math.random() * ACTIVITY_VERBS.length);
  }
  if (!active && activityWasActive) activityLastMs = now - activitySince;
  if (phase === 'starting' || phase === 'stopped' || !connected) activityLastMs = undefined;
  activityWasActive = active;
  const line = activityLine(connected, state, activitySince, activityLastMs, now, activitySeed);
  const [iconEl, labelEl, elapsedEl] = Array.from(activityEl.children);
  const glyph = reducedMotion.matches && line.kind === 'busy' ? '✻' : line.icon;
  if (iconEl.textContent !== glyph) iconEl.textContent = glyph;
  if (labelEl.textContent !== line.label) labelEl.textContent = line.label;
  if (elapsedEl.textContent !== line.elapsed) elapsedEl.textContent = line.elapsed;
  activityEl.className = `activity ${line.kind}`;
  activityEl.hidden = !connected;
  if (line.kind !== activityKind) {
    activityKind = line.kind;
    if (line.kind === 'busy') announce('Claude is working.');
    else if (line.kind === 'done') announce('Claude finished the turn.');
  }
  const interval = reducedMotion.matches ? 1000 : FRAME_MS;
  if (line.active && !activityTimer) activityTimer = setInterval(updateActivity, interval);
  if (!line.active && activityTimer) {
    clearInterval(activityTimer);
    activityTimer = undefined;
  }
}

// ------------------------------------------------------------------ composer

/**
 * Build the left part of the composer footer: model, effort and the context meter.
 *
 * @returns The controls.
 */
function buildFooterControls(): HTMLElement[] {
  const attached = isAttached(view);
  const model = view.session.model ?? 'default';
  const models: [string, string][] = CLAUDE_MODELS.map((m) => [m, m === 'default' ? 'model: default' : m]);
  if (!(CLAUDE_MODELS as readonly string[]).includes(model)) models.push([model, model]);
  const effort = view.session.effort ?? 'default';
  const efforts: [string, string][] = CLAUDE_EFFORTS.map((e) => [e, e === 'default' ? 'effort: default' : e]);
  if (!(CLAUDE_EFFORTS as readonly string[]).includes(effort)) efforts.push([effort, effort]);
  const meter = usageMeter(view.usage);
  const usage = actionButton('', 'usage', {
    class: `meter ${meter.level}`,
    'data-testid': 'header-usage',
    'aria-haspopup': 'dialog',
    'aria-label': `Context usage: ${meter.label}`,
    title: meter.title,
  });
  const bar = el('span', 'meter-bar');
  const fill = el('span', 'meter-fill');
  fill.style.width = `${meter.percent ?? 0}%`;
  bar.append(fill);
  usage.append(bar, el('span', 'meter-label', meter.label));
  usage.disabled = !attached;
  return [
    buildSelect('model', 'header-model', 'Model of the running session', models, model, !attached),
    buildSelect('effort', 'header-effort', 'Thinking effort (applies to sessions started from now on)', efforts, effort, false),
    usage,
  ];
}

/**
 * Build the usage popover: context window, account limits, cost, and "Refresh".
 *
 * @returns A `role="dialog"` element.
 */
function buildUsagePopover(): HTMLElement {
  const box = el('div', 'menu usage', undefined, { role: 'dialog', 'aria-label': 'Usage' });
  const meter = usageMeter(view.usage);
  const c = view.usage.context;
  box.append(el('div', 'menu-heading', 'Context window'));
  box.append(el('div', 'usage-line', c ? `${c.tokens.toLocaleString('en-US')} of ${c.maxTokens.toLocaleString('en-US')} tokens (${meter.percent}%)${c.model ? ` · ${c.model}` : ''}` : 'Not known yet.'));
  const bar = el('div', `meter-bar wide ${meter.level}`);
  const fill = el('span', 'meter-fill');
  fill.style.width = `${meter.percent ?? 0}%`;
  bar.append(fill);
  box.append(bar);
  box.append(el('div', 'menu-heading', 'Account limits'));
  if (view.usage.rateLimits.length === 0) box.append(el('div', 'usage-line', 'None reported (an API key or a cloud provider has none).'));
  for (const limit of view.usage.rateLimits) box.append(el('div', 'usage-line', rateLimitLine(limit)));
  box.append(el('div', 'menu-heading', 'Session'));
  const cost = view.usage.costUsd !== null ? `$${view.usage.costUsd.toFixed(4)}` : 'unknown';
  box.append(el('div', 'usage-line', `Cost ${cost} · ${view.usage.numTurns} turn(s)`));
  box.append(actionButton('Refresh', 'post', { class: 'menu-item', 'data-type': 'refreshUsage' }));
  return box;
}

let noticeTimer: ReturnType<typeof setTimeout> | undefined;
let noticeText = '';

/**
 * Show a short message below the composer for a few seconds (e.g. why an image was refused).
 *
 * @param text One sentence.
 * @returns Nothing.
 */
function showNotice(text: string): void {
  noticeText = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    noticeText = '';
    renderComposer();
  }, 5000);
  announce(text);
  renderComposer();
}

/**
 * Update the composer: placeholder, enabled state, buttons, the footer controls, the image
 * thumbnails and the delivery hint.
 *
 * @returns Nothing.
 */
function renderComposer(): void {
  const attached = isAttached(view);
  const target = messageTarget(view, ui.tab);
  const phase = sessionPhase(view.session.state);
  const busy = attached && (phase === 'busy' || phase === 'waiting');
  const blocked = !ui.side && target.to === null;
  input.disabled = !attached || blocked;
  const placeholder = !attached
    ? 'No session is attached.'
    : ui.side
      ? 'Ask a side question…'
      : target.to === null
        ? target.reason
        : `Message ${target.label}…`;
  if (input.placeholder !== placeholder) {
    input.placeholder = placeholder;
    if (input.value === '') growInput();
  }
  composer.className = `composer${ui.side ? ' side-mode' : ''}${input.disabled ? ' disabled' : ''}`;
  const hasContent = input.value.trim() !== '' || attachments.length > 0;
  sendButton.disabled = input.disabled || !hasContent;
  sendButton.title = ui.side ? 'Ask (Enter)' : busy ? 'Send for the next turn (Enter); Ctrl+Enter sends into the running turn' : 'Send (Enter)';
  interruptButton.hidden = !busy;
  const imagesAllowed = attached && !ui.side && target.to === MAIN;
  attachButton.disabled = !imagesAllowed;
  sideButton.disabled = !attached;
  sideButton.setAttribute('aria-pressed', String(ui.side));
  sideButton.classList.toggle('on', ui.side);
  const waiting = view.side.filter((s) => s.answer === undefined && s.error === undefined).length;
  sideButton.classList.toggle('has-waiting', waiting > 0);

  setSection(footerSlot, JSON.stringify([view.session.model, view.session.effort, view.usage, attached]), buildFooterControls);
  if (openPopupId() === 'usage') refreshPopup('usage', buildUsagePopover());

  setSection(thumbsSlot, attachments.map((a) => a.data.length).join(','), () =>
    attachments.map((a, i) => {
      const box = el('span', 'thumb-box');
      const img = el('img', 'thumb', undefined, { alt: `Attached image ${i + 1}` });
      img.src = `data:${a.mediaType};base64,${a.data}`;
      box.append(img, iconButton('close', `Remove image ${i + 1}`, 'removeImage', { class: 'thumb-remove', 'data-index': String(i) }));
      return box;
    }),
  );

  const hint = noticeText
    ? noticeText
    : !attached
      ? ''
      : ui.side
        ? 'Enter asks the side question · it does not become part of the conversation'
        : blocked
          ? ''
          : busy
            ? 'Enter: for the next turn · Ctrl+Enter: into the running turn · Shift+Enter: new line'
            : 'Enter: send · Shift+Enter: new line · /: commands';
  if (hintEl.textContent !== hint) hintEl.textContent = hint;
  hintEl.className = `hint${noticeText ? ' notice' : ''}`;
}

/**
 * Fit the height of the composer's text field to its content (up to 40 % of the view's height).
 *
 * @returns Nothing. Side effect: sets the field's height through the CSSOM.
 */
function growInput(): void {
  input.style.height = 'auto';
  const max = Math.max(80, Math.round(window.innerHeight * 0.4));
  // An empty field is measured with its placeholder, which may be a sentence that wraps (why
  // this tab takes no message); `scrollHeight` alone ignores the placeholder.
  const empty = input.value === '';
  if (empty) input.value = input.placeholder;
  const height = input.scrollHeight;
  if (empty) input.value = '';
  input.style.height = `${Math.min(height + 2, max)}px`;
}

/**
 * Replace the composer text (programmatically) and update everything that depends on it.
 *
 * @param text The new text.
 * @returns Nothing.
 */
function setInput(text: string): void {
  input.value = text;
  storeDraft();
  growInput();
  updateCompletions();
  renderComposer();
}

/**
 * Return the key under which the composer text is kept: the attached session's id.
 *
 * @returns The session id, or "_" while no session is attached.
 */
function currentDraftKey(): string {
  return view.sessions.attachedId ?? view.session.sessionId ?? '_';
}

/**
 * Remember the composer text for the current session and schedule a save.
 *
 * @returns Nothing. Side effect: updates `ui.drafts` (at most `MAX_DRAFTS` non-empty entries).
 */
function storeDraft(): void {
  if (input.value) ui.drafts[draftKey] = input.value;
  else delete ui.drafts[draftKey];
  const keys = Object.keys(ui.drafts);
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_DRAFTS))) delete ui.drafts[key];
  persist();
}

/**
 * After the state changed: if another session is attached now, keep the text written for the
 * previous one and show the draft of the new one.
 *
 * @returns Nothing.
 */
function syncDraft(): void {
  const key = currentDraftKey();
  if (key === draftKey) return;
  storeDraft();
  draftKey = key;
  attachments = [];
  input.value = Object.hasOwn(ui.drafts, key) ? ui.drafts[key] : '';
  growInput();
  updateCompletions();
}

/**
 * Send what the composer holds.
 *
 *     side mode             -> sideAsk {question}
 *     text starts with "/"  -> resolveSlash(): a message to the host, a part of the view, an
 *                              error below the composer, or chat text for Claude Code
 *     otherwise             -> send {text, delivery, to: the tab's agent, images}
 *
 * @param delivery "queued" (Enter: for the next turn) or "inject" (Ctrl/Cmd+Enter: into the
 *   running turn).
 * @returns Nothing. Empty input is ignored; the composer is cleared after a send.
 */
function sendInput(delivery: 'queued' | 'inject'): void {
  const text = input.value.trim();
  if (!isAttached(view) && !text.startsWith('/')) return;
  if (ui.side) {
    if (!text) return;
    vscode.postMessage({ type: 'sideAsk', question: text });
    setInput('');
    return;
  }
  if (text.startsWith('/') && attachments.length === 0) {
    const result = resolveSlash(text, view.sessions.attachedId);
    if (result.kind === 'error') {
      showNotice(result.text);
      return;
    }
    if (result.kind === 'message') {
      vscode.postMessage(result.message);
      setInput('');
      return;
    }
    if (result.kind === 'ui') {
      setInput('');
      openUi(result.action);
      return;
    }
  }
  const target = messageTarget(view, ui.tab);
  if (target.to === null) {
    showNotice(target.reason);
    return;
  }
  if (!text && attachments.length === 0) return;
  if (!isAttached(view)) {
    showNotice('No session is attached.');
    return;
  }
  vscode.postMessage({ type: 'send', text, delivery, to: target.to, images: target.to === MAIN ? attachments : [] });
  attachments = [];
  stick = true;
  setInput('');
}

/**
 * Open the part of the view a slash command without an argument asks for.
 *
 * @param action The part.
 * @returns Nothing.
 */
function openUi(action: SlashUiAction): void {
  if (action === 'sessions') toggleSessionMenu();
  else if (action === 'honesty') toggleHonestyMenu();
  else {
    const select = document.querySelector<HTMLSelectElement>(`select[data-select="${action}"]`);
    select?.focus();
    try {
      select?.showPicker();
    } catch {
      // Not allowed without a user gesture, or not supported: the focused select is enough.
    }
  }
}

// ------------------------------------------------------------------ slash completion

let completionRows: Completion[] = [];
let completionIndex = 0;
/** The composer text for which the list was dismissed with Escape. */
let completionDismissedFor: string | undefined;
const commandTable = allCommands();

/**
 * Recompute and redraw the completion list for the composer text.
 *
 * @returns Nothing.
 */
function updateCompletions(): void {
  const text = input.value;
  if (completionDismissedFor !== undefined && completionDismissedFor !== text) completionDismissedFor = undefined;
  completionRows = ui.side || completionDismissedFor === text ? [] : completions(text, commandTable);
  // Nothing to complete once the text is exactly the only row's text.
  if (completionRows.length === 1 && completionRows[0].insert === text && completionRows[0].kind === 'value') completionRows = [];
  completionIndex = Math.min(completionIndex, Math.max(0, completionRows.length - 1));
  drawCompletions();
}

/**
 * Draw the completion list (`role="listbox"`); the composer's `aria-activedescendant` names
 * the highlighted row.
 *
 * @returns Nothing.
 */
function drawCompletions(): void {
  completionBox.hidden = completionRows.length === 0;
  input.setAttribute('aria-expanded', String(completionRows.length > 0));
  if (completionRows.length === 0) {
    completionBox.replaceChildren();
    input.removeAttribute('aria-activedescendant');
    return;
  }
  completionBox.replaceChildren(
    ...completionRows.map((row, i) => {
      const option = el('div', `completion${i === completionIndex ? ' active' : ''}`, undefined, {
        role: 'option',
        id: `completion-${i}`,
        'aria-selected': String(i === completionIndex),
        'data-action': 'completion',
        'data-index': String(i),
      });
      option.append(el('span', 'completion-label', row.label), el('span', 'completion-description', row.description));
      if (row.kind !== 'value') option.append(el('span', 'chip', row.kind === 'calu' ? 'calu' : 'Claude Code'));
      return option;
    }),
  );
  input.setAttribute('aria-activedescendant', `completion-${completionIndex}`);
  reveal(completionBox, completionBox.children[completionIndex]);
}

/**
 * Accept a row of the completion list.
 *
 * @param index The row.
 * @param submit True for Enter and a click: a row that needs nothing more is sent at once.
 *   False for Tab: the text is only completed.
 * @returns Nothing.
 */
function acceptCompletion(index: number, submit: boolean): void {
  const row = completionRows[index];
  if (!row) return;
  setInput(row.insert);
  input.focus();
  if (submit && row.submit) sendInput('queued');
}

/**
 * Keyboard shortcuts of the composer: with the completion list open, the arrow keys move in it,
 * Enter and Tab accept, Escape closes it. Otherwise Enter sends for the next turn,
 * Ctrl/Cmd+Enter into the running turn, Shift+Enter inserts a line break. Ignored during IME
 * composition.
 *
 * @param event The keydown event.
 * @returns Nothing.
 */
function onInputKeydown(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) return;
  if (completionRows.length > 0) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const n = completionRows.length;
      completionIndex = (completionIndex + (event.key === 'ArrowDown' ? 1 : n - 1)) % n;
      drawCompletions();
      return;
    }
    if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey)) {
      event.preventDefault();
      acceptCompletion(completionIndex, event.key === 'Enter');
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      completionDismissedFor = input.value;
      updateCompletions();
      return;
    }
  }
  if (event.key !== 'Enter' || event.shiftKey) return;
  event.preventDefault();
  sendInput(event.ctrlKey || event.metaKey ? 'inject' : 'queued');
}

// ------------------------------------------------------------------ images

/**
 * Attach image files to the message being written, after `checkImage` accepted each.
 *
 * @param files Files from a paste, a drop or the file dialog; other files are refused with a
 *   message below the composer.
 * @returns Nothing. The thumbnails appear when the files are read.
 */
function addImages(files: readonly File[]): void {
  const toMain = !ui.side && messageTarget(view, ui.tab).to === MAIN;
  let pending = attachments.length;
  for (const file of files) {
    const problem = checkImage(file.type, file.size, pending, toMain);
    if (problem) {
      showNotice(problem);
      continue;
    }
    pending++;
    const reader = new FileReader();
    reader.onload = () => {
      const image = typeof reader.result === 'string' ? parseImageDataUrl(reader.result) : undefined;
      if (!image) {
        showNotice('The image could not be read.');
        return;
      }
      if (checkImage(image.mediaType, 0, attachments.length, true)) return;
      attachments = [...attachments, image];
      renderComposer();
    };
    reader.onerror = () => showNotice('The image could not be read.');
    reader.readAsDataURL(file);
  }
}

/**
 * Pick the image files of a paste or drop.
 *
 * @param data The event's `clipboardData` or `dataTransfer`.
 * @returns Its files whose type starts with "image/".
 */
function imageFiles(data: DataTransfer | null): File[] {
  return data ? [...data.files].filter((f) => f.type.startsWith('image/')) : [];
}

// ------------------------------------------------------------------ rendering

/**
 * Schedule a render for the next animation frame (several events -> one render).
 *
 * @returns Nothing.
 */
function scheduleRender(): void {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(render);
}

/**
 * Re-render all dynamic parts and schedule saving the view-only state.
 *
 * @returns Nothing.
 */
function render(): void {
  renderQueued = false;
  ctx.view = view;
  ctx.settings = settings;
  ctx.attached = isAttached(view);
  renderTabs();
  renderHeader();
  const setupOnly = renderSetup();
  renderList(setupOnly);
  setSection(stripSlot, JSON.stringify(otherOpenEscalations(view, ui.tab)), buildStrip);
  setSection(sideSlot, JSON.stringify([ui.side, view.side]), buildSide);
  setSection(queueSlot, JSON.stringify(view.queue), buildQueue);
  renderComposer();
  updateActivity();
  // Once more after the dock was updated: its height changes the list's height.
  if (stick) list.scrollTop = list.scrollHeight;
  announceEscalations();
  focusOpenCard();
  persist();
}

/** Milliseconds between a change and the `setState` call that saves it. */
const PERSIST_DELAY_MS = 500;
let persistTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Schedule saving the view-only state with `vscode.setState` (survives hiding the view and
 * reloads). Calls within `PERSIST_DELAY_MS` are merged into one save.
 *
 * @returns Nothing.
 */
function persist(): void {
  if (persistTimer !== undefined) return;
  persistTimer = setTimeout(persistNow, PERSIST_DELAY_MS);
}

/**
 * Save the view-only state immediately (cancels a scheduled save).
 *
 * @returns Nothing.
 */
function persistNow(): void {
  if (persistTimer !== undefined) clearTimeout(persistTimer);
  persistTimer = undefined;
  vscode.setState({ ui });
}

// ------------------------------------------------------------------ escalation chime

let audio: AudioContext | undefined;
/** Outcome of each chime request ("played" / "blocked" / "error"), for tests and diagnostics. */
const bellLog: string[] = [];

/**
 * Create the audio context (once) and resume it; called on the first click or key press, so
 * that later chimes are allowed by the browser's autoplay rules.
 *
 * @returns Nothing.
 */
function unlockAudio(): void {
  try {
    audio ??= new AudioContext();
    if (audio.state === 'suspended') void audio.resume();
  } catch {
    // no audio in this environment
  }
}

/**
 * Play the escalation chime: two short sine tones (880 Hz, then 1320 Hz), quiet, about 0.4 s.
 * Generated with Web Audio, so no sound file and no extra content-security rule are needed.
 *
 * @returns Nothing. Side effect: appends the outcome to `bellLog`.
 */
function playBell(): void {
  try {
    audio ??= new AudioContext();
    const ac = audio;
    if (ac.state !== 'running') {
      // Not unlocked by a click or key press yet: skip this chime instead of playing it late.
      bellLog.push('blocked');
      void ac.resume().catch(() => undefined);
      return;
    }
    const start = ac.currentTime + 0.01;
    for (const [i, freq] of [880, 1320].entries()) {
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      const t = start + i * 0.17;
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      osc.connect(gain).connect(ac.destination);
      osc.start(t);
      osc.stop(t + 0.24);
    }
    bellLog.push('played');
  } catch {
    bellLog.push('error');
  }
}

// ------------------------------------------------------------------ context menu of tool rows

/**
 * Open the context menu of a tool row: information rows and actions (see `menuFor`).
 *
 * @param toolUseId The tool call.
 * @param place The button that opened it, or the pointer position.
 * @returns Nothing. Does nothing for tools without a menu.
 */
function openToolMenu(toolUseId: string, place: { anchor: Element } | { x: number; y: number }): void {
  const tool = own(view.tools, toolUseId);
  const menu = tool && menuFor(tool);
  if (!tool || !menu) return;
  const box = el('div', 'menu tool-menu', undefined, { role: 'menu', 'aria-label': `Actions for ${tool.name}` });
  const info = el('dl', 'menu-info');
  for (const [label, value] of menu.info) info.append(el('dt', '', label), el('dd', '', value));
  box.append(info);
  box.append(
    actionButton(ctx.openTools.has(toolUseId) ? 'Hide details' : 'Show details (decision, input, result)', 'toggleTool', {
      class: 'menu-item',
      role: 'menuitem',
      'data-id': toolUseId,
    }),
  );
  for (const action of menu.actions) {
    box.append(actionButton(action.label, 'menuAction', { class: 'menu-item', role: 'menuitem', 'data-id': toolUseId, 'data-menu-action': action.id }));
  }
  if (tool.check?.checkerRoute) {
    box.append(actionButton("Open this checker's instructions", 'editChecker', { class: 'menu-item', role: 'menuitem', 'data-route': tool.check.checkerRoute }));
  }
  openPopup('tool', box, place);
}

// ------------------------------------------------------------------ event handlers

/**
 * Toggle an entry of a set.
 *
 * @param set The set (modified).
 * @param key The entry.
 * @returns Nothing.
 */
function toggle(set: Set<string>, key: string): void {
  if (set.has(key)) set.delete(key);
  else set.add(key);
}

/**
 * Do what a click on a tool row means (`rowClick`): open the diff, the file or the subagent's
 * tab, or expand the row in place.
 *
 * @param toolUseId The tool call.
 * @returns Nothing.
 */
function onRowClick(toolUseId: string): void {
  const tool = own(view.tools, toolUseId);
  if (!tool) return;
  const click = rowClick(tool);
  if (click.kind === 'openDiff') vscode.postMessage({ type: 'openDiff', toolUseId });
  else if (click.kind === 'openFile') vscode.postMessage({ type: 'itemAction', toolUseId, action: click.action });
  else if (click.kind === 'openTab' && view.agents.some((a) => a.agent_id === click.tab)) switchTab(click.tab);
  else {
    toggle(ctx.openTools, toolUseId);
    render();
  }
}

/**
 * Send the decision or the answer of an escalation card and disable the card's buttons until
 * the host's state arrives.
 *
 * @param key The escalation's key.
 * @param decision "allow" or "deny"; with `answer` true the answer text is built from the
 *   chosen options and sent instead.
 * @param answer True for "Send answer" on a question card.
 * @param card The card element.
 * @returns Nothing. Without a chosen option or text an answer is not sent.
 */
function decide(key: string, decision: 'allow' | 'deny', answer: boolean, card: Element | null): void {
  const esc = own(view.escalations, key);
  if (!esc || esc.status !== 'open') return;
  const draft = draftOf(ctx, esc);
  const note = draft.note.trim();
  if (answer) {
    const text = formatAnswer(esc.questions, draft.choices);
    if (!text) {
      showNotice('Choose an option or write an answer first.');
      return;
    }
    vscode.postMessage({ type: 'decide', escalationId: key, decision: 'allow', answer: text });
  } else {
    vscode.postMessage({ type: 'decide', escalationId: key, decision, ...(note ? { note } : {}) });
  }
  card?.querySelectorAll('button').forEach((b) => (b.disabled = true));
}

/**
 * Choose or unchoose an option of a question (single choice: at most one stays chosen).
 *
 * @param button The option button (`data-key`, `data-q`, `data-label`, `data-multi`).
 * @returns Nothing. Side effects: updates the card's draft and the `aria-checked` states.
 */
function chooseOption(button: HTMLElement): void {
  const esc = own(view.escalations, button.dataset.key ?? '');
  const q = Number(button.dataset.q);
  const label = button.dataset.label ?? '';
  if (!esc || !esc.questions[q]) return;
  const choice = draftOf(ctx, esc).choices[q];
  const had = choice.labels.includes(label);
  if (button.dataset.multi === 'true') choice.labels = had ? choice.labels.filter((l) => l !== label) : [...choice.labels, label];
  else choice.labels = had ? [] : [label];
  for (const sibling of button.parentElement?.querySelectorAll<HTMLElement>('.option') ?? []) {
    sibling.setAttribute('aria-checked', String(choice.labels.includes(sibling.dataset.label ?? '')));
  }
}

/**
 * Handle clicks for the whole page (delegated `data-action` handler). Links without an action
 * (rendered Markdown) never navigate; they are sent to the host.
 *
 * @param event The click event.
 * @returns Nothing.
 */
function onClick(event: MouseEvent): void {
  const target = event.target as HTMLElement;
  const actionEl = target.closest<HTMLElement>('[data-action]');
  if (!actionEl) {
    const anchor = target.closest('a');
    if (anchor) {
      event.preventDefault();
      const message = linkMessage(anchor.getAttribute('href'));
      if (message) vscode.postMessage(message);
    }
    return;
  }
  event.preventDefault();
  if (actionEl instanceof HTMLButtonElement && actionEl.disabled) return;
  const data = actionEl.dataset;
  const inPopup = actionEl.closest('.popup') !== null;
  switch (data.action) {
    case 'post':
      vscode.postMessage({ type: data.type } as WebviewMessage);
      if (inPopup && data.type !== 'refreshUsage') closePopup();
      return;
    case 'postJson':
      vscode.postMessage(JSON.parse(data.message ?? '{}') as WebviewMessage);
      return;
    case 'sessionMenu':
      toggleSessionMenu();
      return;
    case 'switchSession':
      closePopup();
      if (data.id && data.id !== view.sessions.attachedId) vscode.postMessage({ type: 'switchSession', sessionId: data.id });
      return;
    case 'sessionAction': {
      closePopup();
      const message = sessionActionMessage(data.id as SessionMenuAction, view.sessions.attachedId);
      if (message) vscode.postMessage(message);
      return;
    }
    case 'honestyMenu':
      toggleHonestyMenu();
      return;
    case 'honestyMode':
      closePopup();
      vscode.postMessage({ type: 'setHonestyMode', mode: data.id as HonestyMode });
      return;
    case 'honestyClear':
      vscode.postMessage({ type: 'honestyClear', markId: Number(data.id) });
      return;
    case 'usage':
      if (openPopupId() === 'usage') closePopup();
      else {
        openPopup('usage', buildUsagePopover(), { anchor: actionEl });
        vscode.postMessage({ type: 'refreshUsage' });
      }
      return;
    case 'tab':
      switchTab(data.tab ?? MAIN);
      return;
    case 'jump':
      stick = true;
      list.scrollTop = list.scrollHeight;
      jumpButton.hidden = true;
      return;
    case 'jumpEsc':
      focusNewCard = true;
      if (data.tab) switchTab(data.tab);
      requestAnimationFrame(() => reveal(list, list.querySelector(`[data-esc="${CSS.escape(data.key ?? '')}"]`), 'center'));
      render();
      return;
    case 'expand':
      if (data.id) toggle(ctx.expanded, data.id);
      render();
      return;
    case 'rowClick':
      if (data.id) onRowClick(data.id);
      return;
    case 'toggleTool':
      if (inPopup) closePopup();
      if (data.id) toggle(ctx.openTools, data.id);
      render();
      return;
    case 'toolMenu':
      if (data.id) openToolMenu(data.id, { anchor: actionEl });
      return;
    case 'fileClick': {
      const tool = own(view.tools, data.id ?? '');
      const file = tool && fileClick(tool);
      if (tool && file) vscode.postMessage({ type: 'itemAction', toolUseId: tool.toolUseId, action: file.action });
      return;
    }
    case 'menuAction':
      closePopup();
      if (data.id && data.menuAction) vscode.postMessage({ type: 'itemAction', toolUseId: data.id, action: data.menuAction as ItemActionId });
      return;
    case 'openDiff':
      if (inPopup) closePopup();
      if (data.id) vscode.postMessage({ type: 'openDiff', toolUseId: data.id });
      return;
    case 'openToolFile':
      if (data.id && data.index) vscode.postMessage({ type: 'openToolFile', toolUseId: data.id, index: Number(data.index) });
      return;
    case 'openFile':
      if (data.path) vscode.postMessage({ type: 'openFile', path: data.path, ...(data.line ? { line: Number(data.line) } : {}) });
      return;
    case 'editChecker':
      if (inPopup) closePopup();
      vscode.postMessage(data.route ? { type: 'editChecker', route: data.route } : { type: 'editChecker' });
      return;
    case 'copyCode': {
      const pre = actionEl.parentElement?.querySelector('pre');
      void copyText(pre?.textContent ?? '').then((ok) => {
        actionEl.classList.toggle('copied', ok);
        actionEl.title = ok ? 'Copied' : 'Copying failed';
        setTimeout(() => {
          actionEl.classList.remove('copied');
          actionEl.title = 'Copy';
        }, 1500);
      });
      return;
    }
    case 'decide':
      if (data.key && (data.decision === 'allow' || data.decision === 'deny')) decide(data.key, data.decision, false, actionEl.closest('.esc'));
      return;
    case 'answer':
      if (data.key) decide(data.key, 'allow', true, actionEl.closest('.esc'));
      return;
    case 'why':
      if (data.key) vscode.postMessage({ type: 'why', escalationId: data.key });
      return;
    case 'escOption':
      chooseOption(actionEl);
      return;
    case 'rewind':
    case 'forkHere':
      vscode.postMessage({
        type: data.action === 'rewind' ? 'rewindBefore' : 'forkBefore',
        turn: Math.max(1, Number(data.turn) || 1),
        ...(data.uuid ? { uuid: data.uuid } : {}),
      });
      return;
    case 'send':
      sendInput('queued');
      input.focus();
      return;
    case 'attach':
      fileInput.click();
      return;
    case 'removeImage':
      attachments = attachments.filter((_, i) => i !== Number(data.index));
      renderComposer();
      input.focus();
      return;
    case 'toggleSide':
      ui.side = !ui.side;
      updateCompletions();
      render();
      input.focus();
      return;
    case 'completion':
      acceptCompletion(Number(data.index), true);
      return;
  }
}

/**
 * Keyboard handling for the whole page: Enter or Space on a focused row acts like a click;
 * Shift+F10 or the ContextMenu key on a tool row opens its menu; the arrow keys move between
 * tabs.
 *
 * @param event The keydown event.
 * @returns Nothing.
 */
function onDocumentKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement;
  if ((event.key === 'Enter' || event.key === ' ') && target.matches('[role="button"][data-action], [role="option"][data-action]')) {
    event.preventDefault();
    target.click();
    return;
  }
  if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
    const row = target.closest<HTMLElement>('[data-tool-id]');
    if (!row?.dataset.toolId) return;
    event.preventDefault();
    const rect = row.getBoundingClientRect();
    openToolMenu(row.dataset.toolId, { x: rect.left + 16, y: rect.top + 22 });
    return;
  }
  if ((event.key === 'ArrowRight' || event.key === 'ArrowLeft' || event.key === 'Home' || event.key === 'End') && target.matches('[role="tab"]')) {
    const tabs = [...tabsSlot.querySelectorAll<HTMLElement>('[role="tab"]')];
    const index = tabs.indexOf(target);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length;
    event.preventDefault();
    switchTab(tabs[next].dataset.tab ?? MAIN);
    tabsSlot.querySelector<HTMLElement>('[aria-selected="true"]')?.focus();
  }
}

/**
 * Right-click on a tool row opens its context menu instead of the browser's.
 *
 * @param event The contextmenu event.
 * @returns Nothing.
 */
function onContextMenu(event: MouseEvent): void {
  const row = (event.target as HTMLElement).closest<HTMLElement>('[data-tool-id]');
  if (!row?.dataset.toolId) return;
  const tool = own(view.tools, row.dataset.toolId);
  if (!tool || !menuFor(tool)) return;
  event.preventDefault();
  openToolMenu(row.dataset.toolId, { x: event.clientX, y: event.clientY });
}

/**
 * Handle a changed selector: model, effort or supervision level.
 *
 * @param event A `change` event anywhere in the document.
 * @returns Nothing.
 */
function onChange(event: Event): void {
  const target = event.target;
  if (target === fileInput) {
    addImages([...(fileInput.files ?? [])]);
    fileInput.value = '';
    return;
  }
  if (!(target instanceof HTMLSelectElement) || !target.dataset.select) return;
  const value = target.value;
  if (target.dataset.select === 'model' && value) vscode.postMessage({ type: 'setModel', model: value });
  else if (target.dataset.select === 'effort' && (CLAUDE_EFFORTS as readonly string[]).includes(value)) {
    vscode.postMessage({ type: 'setEffort', effort: value as ClaudeEffort });
  } else if (target.dataset.select === 'level' && value) vscode.postMessage({ type: 'setLevel', level: value as LevelId });
}

/**
 * Handle typing: the composer (draft, height, completion list) and the fields of escalation
 * cards (their drafts).
 *
 * @param event An `input` event anywhere in the document.
 * @returns Nothing.
 */
function onInput(event: Event): void {
  const target = event.target;
  if (target === input) {
    storeDraft();
    growInput();
    completionIndex = 0;
    updateCompletions();
    renderComposer();
    return;
  }
  if (!(target instanceof HTMLInputElement)) return;
  const key = target.dataset.escNote ?? target.dataset.escAnswer;
  const esc = key !== undefined ? own(view.escalations, key) : undefined;
  if (!esc) return;
  const draft = draftOf(ctx, esc);
  if (target.dataset.escNote !== undefined) draft.note = target.value;
  else if (draft.choices[Number(target.dataset.q)]) draft.choices[Number(target.dataset.q)].text = target.value;
}

/**
 * Align the view-only state with a state received in a "reset": tabs count as read, drafts of
 * cards that no longer exist are dropped, and a focus request with a lower number than the
 * handled one (the host restarted) is handled again.
 *
 * @returns Nothing.
 */
function syncUiWithState(): void {
  const hostSeq = view.focus?.seq ?? 0;
  if (hostSeq < ui.focusSeq) ui.focusSeq = hostSeq;
  seen = newestItemIds(view.items);
  for (const key of Object.keys(ctx.escDrafts)) {
    if (own(view.escalations, key)?.status !== 'open') delete ctx.escDrafts[key];
  }
  for (const id of [...ctx.openTools]) if (!own(view.tools, id)) ctx.openTools.delete(id);
  announced = new Set(Object.values(view.escalations).filter((e) => e.status === 'open').map((e) => e.key));
}

/**
 * Switch to the tab named by a new focus request of the state (an escalation arrived there).
 * Each request (`view.focus.seq`) is handled once, so the user can switch tabs afterwards.
 *
 * @returns Nothing. Side effect: may change `ui.tab`, `ui.focusSeq` and `focusNewCard`.
 */
function applyFocus(): void {
  const focus = view.focus;
  if (!focus || focus.seq <= ui.focusSeq) return;
  ui.focusSeq = focus.seq;
  ui.tab = focus.tab;
  focusNewCard = true;
}

/**
 * Handle a message from the extension host.
 *
 * @param message The message (the host is trusted; the state inside is data from calu).
 * @returns Nothing.
 */
function onHostMessage(message: HostMessage): void {
  switch (message.type) {
    case 'reset': {
      const wasAttached = isAttached(view);
      view = message.state;
      itemCache.clear();
      headCache = undefined;
      scrollPos.clear();
      stick = true;
      syncUiWithState();
      applyFocus();
      syncDraft();
      scheduleRender();
      if (!wasAttached && isAttached(view) && document.hasFocus() && !focusNewCard) requestAnimationFrame(() => input.focus());
      return;
    }
    case 'event':
      view = applyEvent(view, message.event);
      applyFocus();
      syncDraft();
      scheduleRender();
      return;
    case 'settings':
      settings = { showThinking: message.settings.showThinking !== false, escalationSound: message.settings.escalationSound !== false };
      scheduleRender();
      return;
    case 'bell':
      if (settings.escalationSound) playBell();
      return;
    case 'restoreInput':
      setInput(input.value.trim() ? `${message.text}\n${input.value}` : message.text);
      return;
    case 'reportState': {
      if (renderQueued) render();
      vscode.postMessage({
        type: 'stateReport',
        requestId: message.requestId,
        state: {
          view,
          ui,
          bell: bellLog,
          dom: {
            tab: ui.tab,
            itemCount: list.querySelectorAll(':scope > .item').length,
            openEscalations: openEscalationCount(view),
            input: input.value,
            text: list.innerText.slice(0, 20000),
          },
        },
      });
      return;
    }
  }
}

buildSkeleton();
draftKey = currentDraftKey();
input.value = Object.hasOwn(ui.drafts, draftKey) ? ui.drafts[draftKey] : '';
document.addEventListener('click', onClick);
document.addEventListener('auxclick', (event) => {
  // A middle click on a link must not navigate either.
  if ((event.target as HTMLElement).closest('a')) event.preventDefault();
});
document.addEventListener('contextmenu', onContextMenu);
document.addEventListener('pointerdown', (event) => closePopupIfOutside(event.target), { capture: true });
// Browsers start audio only after a user gesture; unlock it at the first one in the chat.
document.addEventListener('pointerdown', unlockAudio, { capture: true });
document.addEventListener('keydown', unlockAudio, { capture: true });
document.addEventListener('keydown', onDocumentKeydown);
document.addEventListener('change', onChange);
document.addEventListener('input', onInput);
input.addEventListener('keydown', onInputKeydown);
input.addEventListener('paste', (event) => {
  const files = imageFiles(event.clipboardData);
  if (files.length === 0) return;
  event.preventDefault();
  addImages(files);
});
composer.addEventListener('dragover', (event) => {
  event.preventDefault();
  composer.classList.add('dragging');
});
composer.addEventListener('dragleave', () => composer.classList.remove('dragging'));
composer.addEventListener('drop', (event) => {
  event.preventDefault();
  composer.classList.remove('dragging');
  addImages(imageFiles(event.dataTransfer));
});
list.addEventListener('scroll', () => {
  stick = nearBottom();
  jumpButton.hidden = stick || list.childElementCount === 0;
});
// The tab strip scrolls sideways and shows no scrollbar: the mouse wheel moves it, so that the
// tabs of many subagents can be reached with the mouse.
tabsSlot.addEventListener(
  'wheel',
  (event) => {
    const tabs = tabsSlot.querySelector<HTMLElement>('.tabs');
    if (!tabs || tabs.scrollWidth <= tabs.clientWidth || event.deltaY === 0 || event.shiftKey) return;
    tabs.scrollLeft += event.deltaY;
    event.preventDefault();
  },
  { passive: false },
);
// The dock grows and shrinks (queue, strip, composer): keep following the newest output.
new ResizeObserver(() => {
  if (stick) list.scrollTop = list.scrollHeight;
}).observe(list);
window.addEventListener('resize', () => {
  growInput();
  closePopup(false);
});
window.addEventListener('blur', () => closePopup(false));
window.addEventListener('message', (event: MessageEvent) => onHostMessage(event.data as HostMessage));
// Save pending state before the webview is hidden or unloaded.
window.addEventListener('pagehide', persistNow);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) scheduleRender();
});
render();
growInput();
vscode.postMessage({ type: 'ready' });
