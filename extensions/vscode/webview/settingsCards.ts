/**
 * The five cards on top of the settings dialog: Setup, Models, Checkers, Honesty and
 * Temporary rules. Each
 * `render…Card` function builds a fresh `<section class="card">` from the last `data` message;
 * the dialog replaces the old card with it (and gives the focus back by `data-testid`).
 * Runs in the webview. The cards never keep data of their own: what must survive a rebuild
 * (open/closed, the preset waiting for "Apply", error texts) is in `CardState`.
 *
 *     Setup     findings ─► rows with fix buttons ─► fix {action} / runStatus
 *     Models    preset cards ─► confirm bar ─► setAllModels;  per role ─► setRoleModel;
 *               Claude Code's model, effort, Bedrock ─► host.commit (an ordinary `set`)
 *     Checkers  one row per checker ─► editChecker {name}
 *     Honesty   off / track / enforce ─► host.commit("honesty.mode", …)
 *     Temporary rules  list ─► ruleRemove {id};  form ─► ruleAdd {text, duration, scope}
 */

import type { CheckerRow, IdeModelsResult, IdeTemporaryRule, ModelPreset } from '../src/bridgeProtocol';
import type { SettingsData, SettingsWebviewMessage } from '../src/shared/settingsMessages';
import type { SettingsRowView } from '../src/shared/settingsModel';
import {
  groupPresets,
  modeSentences,
  presetForModel,
  presetModel,
  roleModelText,
  rolesState,
  selectedPresetId,
  setupRows,
  setupSummary,
  type SetupRow,
} from '../src/shared/settingsView';
import { add, button, el, icon, modelPicker, rich, wireRadios, type IconName } from './settingsDom';
import { presetChoices } from './settingsRows';

/** Ids of the cards (also their navigation ids: the section element has the id `sec-<id>`). */
export const CARD_IDS = ['setup', 'models', 'checkers', 'honesty', 'rules'] as const;
/** Id of a card. */
export type CardId = (typeof CARD_IDS)[number];

/** Aliases of Claude Code's own models that `claude.model` accepts besides full model ids. */
const CLAUDE_ALIASES = ['fable', 'opus', 'sonnet', 'haiku'] as const;
/** Effort levels offered when the `claude.effort` setting does not list its choices. */
const FALLBACK_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const;

/** State of the cards that survives a rebuild. */
export interface CardState {
  /** Open state of collapsible parts chosen by the user: "setup", "roles", "rootInstall". Absent: automatic. */
  open: Record<string, boolean>;
  /** Error text per card ("" or absent: none). */
  errors: Partial<Record<CardId, string>>;
  /** Id of the preset selected and waiting for "Apply"; "" for none. */
  draftPreset: string;
  /** Text of the custom model field. */
  draftCustom: string;
  /** Effort chosen next to the presets; "" keeps Claude Code's effort as it is. */
  draftEffort: string;
}

/** What the cards need from the dialog. */
export interface CardHost {
  /** Send a message to the extension host. */
  post(message: SettingsWebviewMessage): void;
  /** Store a setting (optimistic `set`). */
  commit(key: string, value: unknown): void;
  /** The view of a setting row, or `undefined` if calu did not list the key. */
  view(key: string): SettingsRowView | undefined;
  /** Scroll to a setting row and focus its control (clears the search). */
  reveal(key: string): void;
  /** The shown scope (`null` = global), for the model messages. */
  project(): string | null;
  /** Called when `CardState` changed and must be stored (the card updated itself already). */
  changed(): void;
}

/**
 * Create the frame of a card.
 *
 * @param id The card.
 * @param title The heading.
 * @param lead One sentence under the heading ("" for none).
 * @returns The `<section>` with heading and lead; the caller appends the content.
 */
function card(id: CardId, title: string, lead: string): HTMLElement {
  const section = el('section', 'card', '', { id: `sec-${id}`, testid: `${id}-card`, 'aria-labelledby': `sec-${id}-title` });
  add(section, el('h2', 'card-title', title, { id: `sec-${id}-title` }), lead ? el('p', 'card-lead', lead) : null);
  return section;
}

/**
 * Create the error line of a card.
 *
 * @param id The card.
 * @param state The card state (its `errors[id]` is shown).
 * @returns A polite live region; empty and without height when there is no error.
 */
function cardError(id: CardId, state: CardState): HTMLElement {
  const wrap = el('div', `row-error-wrap${state.errors[id] ? ' shown' : ''}`);
  wrap.appendChild(el('div', 'row-error', state.errors[id] ?? '', { 'aria-live': 'polite', testid: `${id}-error` }));
  return wrap;
}

/**
 * Create a collapsible part (`<details>`) whose open state the user's choice overrides.
 *
 * @param key Key in `CardState.open`.
 * @param auto Open state while the user has not chosen.
 * @param state The card state.
 * @param host Told when the user toggles.
 * @param summary Content of the `<summary>`.
 * @returns The `<details>`; the caller appends the content.
 */
function collapsible(key: string, auto: boolean, state: CardState, host: CardHost, summary: (Node | string)[]): HTMLDetailsElement {
  const details = el('details', 'fold');
  details.open = state.open[key] ?? auto;
  const head = add(el('summary', 'fold-summary', '', { testid: `${key}-toggle` }), icon('chevron', 'fold-chevron'), ...summary);
  // `click` also fires for Enter and Space on the summary; the open state changes after it.
  head.addEventListener('click', () => {
    window.setTimeout(() => {
      state.open[key] = details.open;
      host.changed();
    }, 0);
  });
  details.appendChild(head);
  return details;
}

/**
 * Create a box with a command to copy.
 *
 * @param command The command (shown as text).
 * @param testid `data-testid` of the code element.
 * @param actions Buttons shown under the command.
 * @returns The box.
 */
function commandBox(command: string, testid: string, actions: HTMLElement[]): HTMLElement {
  const box = el('div', 'command');
  add(box, add(el('pre', 'command-text', '', { tabindex: '0', testid }), el('code', '', command)), add(el('div', 'command-actions'), ...actions));
  return box;
}

/**
 * Create a button that runs a fix and briefly shows a confirmation text.
 *
 * @param label The button text.
 * @param done Text shown for two seconds after a click, e.g. "Copied"; "" keeps the label.
 * @param cls Class names.
 * @param onClick The action.
 * @param testid `data-testid`.
 * @param iconName Optional icon.
 * @returns The button.
 */
function actionButton(label: string, done: string, cls: string, onClick: () => void, testid: string, iconName?: IconName): HTMLButtonElement {
  const node = button(
    label,
    cls,
    () => {
      onClick();
      const text = node.querySelector('.btn-text');
      if (!done || !text) return;
      text.textContent = done;
      window.setTimeout(() => (text.textContent = label), 2000);
    },
    { testid },
    iconName,
  );
  return node;
}

// ------------------------------------------------------------------ Setup

/** Icon and accessible text per level of a setup row. */
const LEVELS: Record<SetupRow['level'], { icon: IconName; text: string }> = {
  ok: { icon: 'check', text: 'In place' },
  missing: { icon: 'warning', text: 'Missing' },
  note: { icon: 'info', text: 'Note' },
};

/**
 * Build the Setup card: what calu needs on this machine and what is missing.
 *
 * @param data The last `data` message.
 * @param state The card state (`open.setup`, `open.rootInstall`, `errors.setup`).
 * @param host The dialog.
 * @returns The card. It is collapsed to its summary line while nothing is missing, unless the
 *   user opened it.
 */
export function renderSetupCard(data: SettingsData, state: CardState, host: CardHost): HTMLElement {
  const rows = setupRows(data);
  const summary = setupSummary(rows);
  const section = el('section', `card card-fold${summary.ok ? ' setup-ok' : ' setup-missing'}`, '', {
    id: 'sec-setup',
    testid: 'setup-card',
    'aria-labelledby': 'sec-setup-title',
  });
  const details = collapsible('setup', !summary.ok, state, host, [
    el('h2', 'card-title', 'Setup', { id: 'sec-setup-title' }),
    add(el('span', `setup-summary level-${summary.ok ? 'ok' : 'missing'}`, '', { testid: 'setup-summary' }), icon(summary.ok ? 'check' : 'warning'), summary.text),
  ]);
  const list = el('ul', 'setup-list');
  for (const row of rows) {
    const level = LEVELS[row.level];
    const item = el('li', `setup-item level-${row.level}`, '', { testid: `setup-item-${row.name}` });
    const text = el('div', 'setup-text');
    add(text, add(el('div', 'setup-line'), el('strong', '', row.title), el('span', 'sr-only', ` (${level.text}) `), rich('span', 'setup-message', row.message)));
    if (row.advice) text.appendChild(rich('div', 'setup-advice', row.advice));
    const fixes = el('div', 'setup-fixes');
    row.fixes.forEach((fix, index) => {
      const testid = index === 0 ? `setup-fix-${row.name}` : `setup-fix-${row.name}-${fix.action}`;
      const done = fix.action.startsWith('copy') ? 'Copied' : '';
      fixes.appendChild(actionButton(fix.label, done, index === 0 ? 'btn primary' : 'btn', () => host.post({ type: 'fix', action: fix.action }), testid));
    });
    add(list, add(item, icon(level.icon, 'setup-icon'), text, row.fixes.length > 0 ? fixes : null));
  }
  details.appendChild(list);

  if (data.serverHelp) {
    const help = data.serverHelp;
    const block = el('div', 'setup-block', '', { testid: 'server-help' });
    add(
      block,
      el('h3', 'sub-title', 'Start the calu server so that it keeps running'),
      el('p', 'sub-lead', help.note),
      commandBox(help.command, 'server-command', [
        actionButton('Copy', 'Copied', 'btn', () => host.post({ type: 'fix', action: 'copyServerCommand' }), 'server-copy', 'copy'),
        actionButton('Open terminal with this command', '', 'btn', () => host.post({ type: 'fix', action: 'startServer' }), 'server-start', 'terminal'),
      ]),
      help.detach ? add(el('p', 'sub-lead'), 'Detach with ', el('kbd', '', help.detach), ' to leave the server running and get the terminal back.') : null,
    );
    details.appendChild(block);
  }
  if (data.rootInstallCommand) {
    const root = collapsible('rootInstall', false, state, host, [el('span', 'fold-title', 'Root installation (only for `calu serve --sudo`)')]);
    root.classList.add('setup-block');
    add(
      root,
      el('p', 'sub-lead', 'Run this command yourself in a terminal; it asks for your sudo password. The extension never runs it.'),
      commandBox(data.rootInstallCommand, 'root-install-command', [
        actionButton('Copy', 'Copied', 'btn', () => host.post({ type: 'fix', action: 'copyRootInstall' }), 'setup-fix-root-install', 'copy'),
      ]),
    );
    details.appendChild(root);
  }
  add(
    details,
    add(
      el('div', 'card-actions'),
      button('Check again', 'btn', () => host.post({ type: 'runStatus' }), { testid: 'setup-check' }, 'refresh'),
      rows.some((row) => row.name === 'calu' && row.level === 'ok')
        ? button('Update calu', 'btn quiet', () => host.post({ type: 'fix', action: 'updateCalu' }), { testid: 'setup-update' })
        : null,
    ),
    cardError('setup', state),
  );
  section.appendChild(details);
  return section;
}

// ------------------------------------------------------------------ Models

/**
 * Read the parts of `ide.models.presets` defensively (without a database login calu sends
 * `claude: null`, no roles and a `problem` text).
 *
 * @param presets The result, or `null`.
 * @returns The parts with safe defaults.
 */
function modelParts(presets: IdeModelsResult | null): {
  presets: ModelPreset[];
  roles: IdeModelsResult['roles'];
  claude: IdeModelsResult['claude'] | null;
  defaultModel: string;
  problem: string;
} {
  const raw = (presets ?? {}) as Partial<IdeModelsResult> & { problem?: unknown };
  return {
    presets: Array.isArray(raw.presets) ? raw.presets : [],
    roles: Array.isArray(raw.roles) ? raw.roles : [],
    claude: raw.claude ?? null,
    defaultModel: typeof raw.default_model === 'string' ? raw.default_model : '',
    problem: typeof raw.problem === 'string' ? raw.problem : '',
  };
}

/**
 * Build the block "Use one model for everything": the preset cards, the custom field, the
 * effort select and the confirm bar.
 *
 * @param parts The model data.
 * @param state The card state (`draftPreset`, `draftCustom`, `draftEffort`).
 * @param host The dialog; "Apply" posts `setAllModels`.
 * @param efforts The effort levels to offer.
 * @returns The block. Selecting a card or typing a custom model opens the confirm bar, and
 *   nothing is sent until "Apply" is clicked or Enter is pressed in the custom field. Enter
 *   there posts `setAllModels` at once.
 */
function renderUseOne(parts: ReturnType<typeof modelParts>, state: CardState, host: CardHost, efforts: readonly string[]): HTMLElement {
  const block = el('div', 'use-one');
  const count = parts.roles.length;
  const disabled = count === 0;
  const currentId = selectedPresetId(parts.presets, parts.roles);
  const titleId = 'use-one-title';
  block.appendChild(el('h3', 'sub-title', 'Use one model for everything', { id: titleId }));
  block.appendChild(
    el(
      'p',
      'sub-lead',
      disabled
        ? parts.problem || 'The roles could not be read, so the model cannot be changed here.'
        : `Sets the model of all ${count} roles (the checkers, the narrator, the summarizer and the honesty reviewer) in one step.`,
    ),
  );

  const group = el('div', 'presets', '', { role: 'radiogroup', 'aria-labelledby': titleId });
  const radios: HTMLElement[] = [];
  const ids: string[] = [];
  for (const presetGroup of groupPresets(parts.presets)) {
    const column = el('div', 'preset-group');
    const grid = el('div', 'preset-grid');
    for (const preset of presetGroup.presets) {
      const model = presetModel(preset);
      const radio = el('div', `preset${preset.id === currentId ? ' current' : ''}`, '', {
        role: 'radio',
        testid: `preset-${preset.id}`,
        'aria-disabled': disabled ? 'true' : undefined,
        'aria-describedby': `preset-${preset.id}-note`,
      });
      add(
        radio,
        el('span', 'preset-group-name', presetGroup.group),
        add(el('div', 'preset-head'), el('span', 'preset-label', preset.label), preset.id === currentId ? el('span', 'badge', 'Current') : null),
        el('code', 'preset-model', model || 'no model stored'),
        rich('div', 'preset-note', preset.note ?? '', { id: `preset-${preset.id}-note` }),
      );
      radios.push(radio);
      ids.push(preset.id);
      grid.appendChild(radio);
    }
    add(group, add(column, grid));
  }
  block.appendChild(group);

  const custom = el('input', 'input', '', {
    type: 'text',
    id: 'use-one-custom',
    testid: 'preset-custom',
    placeholder: 'provider:model, e.g. anthropic:claude-haiku-5-5',
    spellcheck: 'false',
    autocomplete: 'off',
  });
  custom.value = state.draftCustom;
  custom.disabled = disabled;
  const effort = el('select', 'select', '', { id: 'use-one-effort', testid: 'models-effort' });
  effort.appendChild(new Option('Keep as it is', ''));
  for (const level of efforts) effort.appendChild(new Option(level, level));
  effort.value = state.draftEffort;
  effort.disabled = disabled;
  add(
    block,
    add(
      el('div', 'use-one-extra'),
      add(el('div', 'field'), el('label', 'field-label', 'Or a custom model', { for: 'use-one-custom' }), custom),
      add(el('div', 'field field-narrow'), el('label', 'field-label', 'Claude Code effort', { for: 'use-one-effort' }), effort),
    ),
  );

  const confirm = el('div', 'confirm', '', { testid: 'models-confirm', role: 'group', 'aria-label': 'Confirm the model for all roles' });
  const confirmText = el('span', 'confirm-text', '', { 'aria-live': 'polite' });
  const apply = button('Apply', 'btn primary', () => applyDraft(), { testid: 'models-apply' });
  const cancel = button('Cancel', 'btn', () => clearDraft(), { testid: 'models-cancel' });
  add(block, add(confirm, confirmText, add(el('div', 'confirm-actions'), apply, cancel)));

  /**
   * Return what "Apply" would send.
   *
   * @returns The model string to store (the typed text for a custom model; else the preset's
   *   model, or `default` for a preset without one) and the label naming it; `undefined` if
   *   neither a preset nor a custom model is chosen.
   */
  const draft = (): { model: string; label: string } | undefined => {
    const customText = state.draftCustom.trim();
    if (customText) return { model: customText, label: presetForModel(parts.presets, customText)?.label ?? customText };
    const preset = parts.presets.find((candidate) => candidate.id === state.draftPreset);
    return preset ? { model: presetModel(preset) || 'default', label: preset.label } : undefined;
  };
  const mark = wireRadios(
    radios,
    ids.indexOf(state.draftPreset || currentId || ''),
    (index) => {
      state.draftPreset = ids[index];
      state.draftCustom = '';
      custom.value = '';
      refresh();
      host.changed();
    },
    true,
  );

  /**
   * Show the confirm bar for the current draft, or hide it.
   *
   * @returns Nothing.
   */
  function refresh(): void {
    const chosen = draft();
    const effortOnly = !chosen && state.draftEffort !== '';
    mark(state.draftCustom.trim() ? -1 : ids.indexOf(state.draftPreset || currentId || ''));
    confirm.classList.toggle('shown', chosen !== undefined || effortOnly);
    apply.disabled = cancel.disabled = chosen === undefined;
    confirmText.textContent = chosen
      ? `Set all ${count} roles to ${chosen.label}${state.draftEffort ? ` and Claude Code's effort to ${state.draftEffort}` : ''}?`
      : effortOnly
        ? 'Choose a model to apply the effort with.'
        : '';
  }

  /**
   * Send the draft as `setAllModels` and close the confirm bar.
   *
   * @returns Nothing.
   */
  function applyDraft(): void {
    const chosen = draft();
    if (!chosen || disabled) return;
    state.errors.models = '';
    host.post({
      type: 'setAllModels',
      model: chosen.model,
      ...(state.draftEffort ? { effort: state.draftEffort } : {}),
      project: host.project(),
    });
    clearDraft();
  }

  /**
   * Drop the draft and close the confirm bar.
   *
   * @returns Nothing.
   */
  function clearDraft(): void {
    state.draftPreset = '';
    state.draftCustom = '';
    state.draftEffort = '';
    custom.value = '';
    effort.value = '';
    refresh();
    host.changed();
  }

  custom.addEventListener('input', () => {
    state.draftCustom = custom.value;
    if (custom.value.trim()) state.draftPreset = '';
    refresh();
    host.changed();
  });
  custom.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') applyDraft();
    else if (event.key === 'Escape') clearDraft();
  });
  effort.addEventListener('change', () => {
    state.draftEffort = effort.value;
    refresh();
    host.changed();
  });
  refresh();
  return block;
}

/**
 * Build the collapsible table "Per role".
 *
 * @param parts The model data.
 * @param state The card state (`open.roles`).
 * @param host The dialog; a changed picker posts `setRoleModel`.
 * @returns The part, or `null` if there are no roles. It is closed while all roles use the
 *   same model, unless the user opened it.
 */
function renderPerRole(parts: ReturnType<typeof modelParts>, state: CardState, host: CardHost): HTMLElement | null {
  if (parts.roles.length === 0) return null;
  const roles = rolesState(parts.roles);
  const summaryText =
    roles.kind === 'uniform' ? `all use ${roleModelText(roles.model, parts.presets, parts.defaultModel)}` : 'the roles use different models';
  const details = collapsible('roles', roles.kind === 'mixed', state, host, [
    el('span', 'fold-title', 'Per role'),
    el('span', 'fold-note', summaryText, { testid: 'roles-summary' }),
  ]);
  const table = el('table', 'roles');
  add(table, add(el('thead'), add(el('tr'), el('th', '', 'Role', { scope: 'col' }), el('th', '', 'Current model', { scope: 'col' }), el('th', '', 'Change', { scope: 'col' }))));
  const body = el('tbody');
  const choices = presetChoices(parts.presets);
  parts.roles.forEach((role, index) => {
    const id = `role-${index}`;
    const stored = (role.model ?? '').trim();
    const picker = modelPicker({
      id,
      testid: `role-${role.key}`,
      value: stored,
      choices,
      emptyLabel: parts.defaultModel ? `Default → ${parts.defaultModel}` : 'Default',
      customLabel: `${role.label}: custom model`,
      onCommit: (value) => {
        if (value === stored) return;
        state.errors.models = '';
        host.post({ type: 'setRoleModel', role: role.key, model: value, project: host.project() });
      },
    });
    add(
      body,
      add(
        el('tr'),
        add(el('th', 'role-name', '', { scope: 'row' }), el('label', '', role.label, { for: id }), el('code', 'row-key', role.key)),
        el('td', `role-current${stored ? '' : ' muted'}`, roleModelText(stored, parts.presets, parts.defaultModel)),
        add(el('td', 'role-change'), picker.el),
      ),
    );
  });
  add(details, add(el('div', 'table-scroll'), add(table, body)));
  return details;
}

/**
 * Build the block "Claude Code itself": its model, effort and the Bedrock switch.
 *
 * @param claude Claude Code's stored settings.
 * @param host The dialog; each control stores its setting with `host.commit`.
 * @param efforts The effort levels to offer.
 * @returns The block.
 */
function renderClaudeOwn(claude: IdeModelsResult['claude'], host: CardHost, efforts: readonly string[]): HTMLElement {
  const block = el('div', 'claude-own');
  add(
    block,
    el('h3', 'sub-title', 'Claude Code itself'),
    el('p', 'sub-lead', 'The model and effort of the Claude Code session that calu supervises. They apply to sessions started after the change.'),
  );
  const model = claude.model ?? '';
  const picker = modelPicker({
    id: 'claude-model',
    testid: 'claude-model',
    value: model,
    choices: CLAUDE_ALIASES.map((alias) => ({ value: alias, label: alias })),
    emptyLabel: "Claude Code's default",
    customLabel: 'Claude Code model: custom model id',
    onCommit: (value) => {
      if (value !== model) host.commit('claude.model', value === '' ? null : value);
    },
  });
  const effort = el('select', 'select', '', { id: 'claude-effort', testid: 'claude-effort' });
  effort.appendChild(new Option("Claude Code's default", ''));
  const current = claude.effort ?? '';
  for (const level of current && !efforts.includes(current) ? [...efforts, current] : efforts) effort.appendChild(new Option(level, level));
  effort.value = current;
  effort.addEventListener('change', () => host.commit('claude.effort', effort.value === '' ? null : effort.value));
  const bedrock = el('input', 'switch', '', {
    type: 'checkbox',
    role: 'switch',
    id: 'claude-bedrock',
    testid: 'claude-bedrock',
    'aria-describedby': 'claude-bedrock-desc',
  });
  bedrock.checked = claude.use_bedrock === true;
  bedrock.addEventListener('change', () => host.commit('claude.use_bedrock', bedrock.checked));
  add(
    block,
    add(
      el('div', 'field-grid'),
      add(el('div', 'field'), el('label', 'field-label', 'Model', { for: 'claude-model' }), picker.el),
      add(el('div', 'field field-narrow'), el('label', 'field-label', 'Effort', { for: 'claude-effort' }), effort),
    ),
    add(
      el('div', 'switch-line'),
      bedrock,
      add(
        el('div'),
        el('label', 'field-label', 'Use AWS Bedrock', { for: 'claude-bedrock' }),
        el(
          'div',
          'sub-lead',
          host.view('claude.use_bedrock')?.description ||
            'Runs Claude Code through AWS Bedrock; roles without a stored model then use Haiku through Bedrock.',
          { id: 'claude-bedrock-desc' },
        ),
      ),
    ),
  );
  return block;
}

/**
 * Build the Models card.
 *
 * @param data The last `data` message.
 * @param state The card state.
 * @param host The dialog.
 * @returns The card: "Use one model for everything", "Per role" and "Claude Code itself"; a
 *   notice instead if the presets could not be loaded.
 */
export function renderModelsCard(data: SettingsData, state: CardState, host: CardHost): HTMLElement {
  const section = card('models', 'Models', 'Which model the checkers and helper agents use, and which model Claude Code itself runs.');
  if (!data.presets) {
    add(section, el('p', 'notice', 'The model presets could not be loaded.'), cardError('models', state));
    return section;
  }
  const parts = modelParts(data.presets);
  const listed = host.view('claude.effort')?.choices.filter(Boolean) ?? [];
  const efforts = listed.length > 0 ? listed : [...FALLBACK_EFFORTS];
  add(
    section,
    renderUseOne(parts, state, host, efforts),
    cardError('models', state),
    renderPerRole(parts, state, host),
    parts.claude ? renderClaudeOwn(parts.claude, host, efforts) : null,
  );
  return section;
}

// ------------------------------------------------------------------ Checkers

/**
 * Build the Checkers card: one row per checker with "Edit instructions" as its main action.
 *
 * @param data The last `data` message.
 * @param state The card state (`errors.checkers`).
 * @param host The dialog; the button posts `editChecker`.
 * @returns The card. Whether a checker is enabled is shown, not changed: the bridge offers no
 *   request for it.
 */
export function renderCheckersCard(data: SettingsData, state: CardState, host: CardHost): HTMLElement {
  const section = card(
    'checkers',
    'Checkers',
    'The agents that review each command and edit. Their instructions are Markdown texts; editing them changes what the checkers allow, deny and escalate.',
  );
  const checkers: CheckerRow[] = Array.isArray(data.settings?.checkers) ? data.settings.checkers : [];
  if (checkers.length === 0) {
    add(section, el('p', 'notice', data.settings ? 'No checkers are defined.' : 'The checkers could not be loaded.'), cardError('checkers', state));
    return section;
  }
  const defaultModel = modelParts(data.presets).defaultModel;
  const list = el('ul', 'checkers');
  for (const checker of checkers) {
    if (!checker || typeof checker.name !== 'string') continue;
    const item = el('li', `checker${checker.enabled === false ? ' off' : ''}`, '', { testid: `checker-${checker.name}` });
    const model = roleModelText(checker.model ?? '', data.presets?.presets, defaultModel);
    add(
      item,
      add(
        el('div', 'checker-text'),
        add(
          el('div', 'checker-line'),
          el('strong', 'checker-name', checker.name),
          el('span', `badge${checker.enabled === false ? ' badge-off' : ' badge-on'}`, checker.enabled === false ? 'Disabled' : 'Enabled'),
          el('span', 'muted', `v${checker.version}`),
        ),
        add(el('div', 'checker-meta'), 'Checks ', el('code', '', checker.route || '—'), ' · model: ', el('span', 'checker-model', model)),
      ),
      button('Edit instructions', 'btn primary', () => host.post({ type: 'editChecker', name: checker.name }), {
        testid: `checker-edit-${checker.name}`,
        'aria-label': `Edit instructions of the checker ${checker.name}`,
      }, 'edit'),
    );
    list.appendChild(item);
  }
  add(section, list, cardError('checkers', state));
  return section;
}

// ------------------------------------------------------------------ Temporary rules

/**
 * Name the scope of a temporary rule for a person.
 *
 * @param rule The rule as `ide.rules.list` gives it.
 * @returns "every project", "project <name>", "this session" for a rule of the attached
 *   session, or "another session".
 */
function ruleWhere(rule: IdeTemporaryRule): string {
  if (rule.scope === 'global') return 'every project';
  if (rule.scope === 'project') return `project ${rule.project ?? ''}`;
  return rule.applies ? 'this session' : 'another session';
}

/**
 * Build the card of the temporary rules: every active rule, each with a Remove button and its
 * scope (a rule of another session is marked "another session", because it does not apply to
 * the attached one), and a form that adds one (`ruleAdd`, `ruleRemove`).
 *
 * @param data The dialog's data (`rules` is `ide.rules.list`, `null` if it failed).
 * @param state The card state (`errors.rules`).
 * @param host The dialog; the buttons post their message with `host.post`.
 * @returns The card.
 */
export function renderRulesCard(data: SettingsData, state: CardState, host: CardHost): HTMLElement {
  const section = card(
    'rules',
    'Temporary rules',
    'Sentences the checkers read next to their instructions until they expire (1 minute to 7 days). Claude is not told which rules count.',
  );
  const rules = data.rules?.rules ?? [];
  const list = el('ul', 'checkers', '', { testid: 'rules-list' });
  for (const rule of rules) {
    const until = new Date(rule.expires_at).toLocaleString();
    const where = ruleWhere(rule);
    add(
      list,
      add(
        el('li', 'checker', '', { testid: `rule-${rule.id}` }),
        add(
          el('div', 'checker-text'),
          add(el('div', 'checker-line'), el('strong', 'checker-name', `#${rule.id}`), el('span', 'muted', `${where} · until ${until}`)),
          el('div', 'checker-meta', rule.text),
        ),
        button('Remove', 'btn', () => host.post({ type: 'ruleRemove', id: rule.id }), { testid: `rule-remove-${rule.id}`, 'aria-label': `Remove rule ${rule.id}` }),
      ),
    );
  }
  if (rules.length === 0) list.appendChild(el('li', 'muted', data.rules ? 'No temporary rule is active.' : 'The rules could not be loaded.'));
  const text = el('textarea', 'textarea', '', { rows: '3', maxlength: '1000', 'aria-label': 'Rule', placeholder: 'e.g. Claude may run the database tests.', testid: 'rule-text' }) as HTMLTextAreaElement;
  const duration = el('input', 'input rule-duration', '', { type: 'text', value: '1d', size: '5', 'aria-label': 'For how long', testid: 'rule-duration' }) as HTMLInputElement;
  const scope = el('select', 'select', '', { 'aria-label': 'Applies to', testid: 'rule-scope' }) as HTMLSelectElement;
  for (const [value, label] of [['session', 'this session'], ['project', "this session's project"], ['global', 'every project']] as const) {
    scope.appendChild(el('option', '', label, { value }));
  }
  const addButton = button('Add rule', 'btn primary', () => {
    const scoped = scope.value === 'project' || scope.value === 'global' ? scope.value : 'session';
    if (!text.value.trim()) {
      state.errors.rules = 'A rule needs a text.';
      host.changed();
      return;
    }
    state.errors.rules = '';
    host.post({ type: 'ruleAdd', text: text.value, duration: duration.value.trim() || '1d', scope: scoped });
  }, { testid: 'rule-add' });
  add(
    section,
    list,
    text,
    add(el('div', 'card-actions'), el('span', 'muted', 'For'), duration, el('span', 'muted', 'applies to'), scope, addButton),
    cardError('rules', state),
  );
  return section;
}

// ------------------------------------------------------------------ Honesty

/** Settings the Honesty card links to. */
const HONESTY_LINKS = ['honesty.sufficient_trust', 'honesty.max_open_marks'] as const;

/**
 * Build the Honesty card: the mode as a segmented control with one sentence per mode.
 *
 * @param state The card state (`errors.honesty`).
 * @param host The dialog; selecting a mode stores `honesty.mode` with `host.commit`.
 * @returns The card, or `null` if calu lists no setting `honesty.mode` with choices.
 */
export function renderHonestyCard(state: CardState, host: CardHost): HTMLElement | null {
  const view = host.view('honesty.mode');
  const modes = view?.choices.filter(Boolean) ?? [];
  if (!view || modes.length === 0) return null;
  const section = card('honesty', 'Honesty', 'Code honesty: calu tracks which code was verified to do what its names and documentation say.');
  const current = typeof view.value === 'string' ? view.value : '';
  const sentences = modeSentences(view.description, modes);
  const segments = el('div', 'segments', '', { role: 'radiogroup', 'aria-label': 'Honesty mode' });
  const radios = modes.map((mode) =>
    el('div', 'segment', mode, { role: 'radio', testid: `honesty-mode-${mode}`, 'aria-describedby': sentences[mode] ? `honesty-mode-${mode}-text` : undefined }),
  );
  wireRadios(
    radios,
    modes.indexOf(current),
    (index) => {
      if (modes[index] === current) return;
      state.errors.honesty = '';
      host.commit('honesty.mode', modes[index]);
    },
    false,
  );
  add(segments, ...radios);
  const list = el('dl', 'modes');
  for (const mode of modes) {
    if (!sentences[mode]) continue;
    add(
      list,
      add(
        el('div', `mode${mode === current ? ' current' : ''}`),
        el('dt', '', mode),
        el('dd', '', sentences[mode], { id: `honesty-mode-${mode}-text` }),
      ),
    );
  }
  const links = el('div', 'card-actions');
  for (const key of HONESTY_LINKS) {
    const target = host.view(key);
    if (target) links.appendChild(button(target.label, 'btn link', () => host.reveal(key), { testid: `honesty-link-${key}` }, 'arrow'));
  }
  add(
    section,
    segments,
    list.childElementCount > 0 ? list : el('p', 'sub-lead', view.description),
    cardError('honesty', state),
    links.childElementCount > 0 ? add(el('div', 'related'), el('span', 'muted', 'Related settings:'), links) : null,
  );
  return section;
}
