// The settings dialog: message validation and the form model.
import { describe, expect, it } from 'vitest';
import type { SettingRow } from '../../src/bridgeProtocol';
import { MAX_VALUE_JSON, errorKey, parseSettingsMessage } from '../../src/shared/settingsMessages';
import {
  buildSections,
  controlKind,
  formatControlValue,
  labelOf,
  matchesSearch,
  parseControlValue,
  rowView,
  sectionLabel,
  sectionOf,
  setupItems,
} from '../../src/shared/settingsModel';

/**
 * Build a settings row with defaults.
 *
 * @returns The row.
 */
function row(key: string, kind: string, extra: Partial<SettingRow> = {}): SettingRow {
  return { key, type: 'text', default: '', description: '', value: null, is_set: false, kind, choices: [], ...extra };
}

const rows: SettingRow[] = [
  row('claude', 'section', { description: 'How Claude Code is run.' }),
  row('claude.model', 'text', { type: 'text (or empty)', default: '"opus"', value: 'opus', description: 'Claude Code model alias.' }),
  row('claude.effort', 'choice', { type: 'one of `low`, `high` (or empty)', choices: ['low', 'high'], is_set: true, value: 'high' }),
  row('claude.stream_partial', 'bool', { type: 'true/false', value: true, default_value: true }),
  row('checkers', 'section', { description: 'The checker agents.' }),
  row('checkers.command', 'section', { description: 'nested' }),
  row('checkers.command.request_limit', 'int', { type: 'whole number', value: 8, default_value: 8 }),
  row('checkers.model', 'model', { value: '', inherits: 'global' }),
  row('honesty.rules', 'markdown', { value: '# Rules' }),
  row('sandbox.extra_paths', 'lines', { value: ['/a', '/b'] }),
  row('sandbox.env', 'json', { value: { A: '1' } }),
  row('budget_usd', 'number', { type: 'number (or empty)', value: null, default_value: null }),
];

describe('settings model', () => {
  it('labels keys and sections', () => {
    expect(sectionOf('claude.model')).toBe('claude');
    expect(sectionOf('budget_usd')).toBe('general');
    expect(sectionLabel('auto_escalate')).toBe('Auto escalate');
    expect(sectionLabel('general')).toBe('General');
    expect(labelOf('claude.stream_partial')).toBe('Stream partial');
    expect(labelOf('checkers.command.request_limit')).toBe('Command › request limit');
    expect(labelOf('budget_usd')).toBe('Budget USD');
    expect(labelOf('escalation.timeout_s')).toBe('Timeout seconds');
  });

  it('chooses a control per kind', () => {
    const kinds = ['bool', 'choice', 'int', 'number', 'text', 'model', 'markdown', 'lines', 'json', 'future-kind'];
    expect(kinds.map((kind) => controlKind({ kind, choices: ['a'] }))).toEqual([
      'toggle', 'select', 'number', 'number', 'text', 'model', 'markdown', 'list', 'json', 'json',
    ]);
    expect(controlKind({ kind: 'choice', choices: [] })).toBe('text');
  });

  it('groups rows by the first key segment, in calu\'s order, without the section rows', () => {
    const sections = buildSections(rows);
    expect(sections.map((s) => [s.id, s.rows.map((r) => r.key)])).toEqual([
      ['claude', ['claude.model', 'claude.effort', 'claude.stream_partial']],
      ['checkers', ['checkers.command.request_limit', 'checkers.model']],
      ['honesty', ['honesty.rules']],
      ['sandbox', ['sandbox.extra_paths', 'sandbox.env']],
      ['general', ['budget_usd']],
    ]);
    expect(sections[0]).toMatchObject({ label: 'Claude', description: 'How Claude Code is run.', modifiedCount: 1 });
    expect(sections[1].description).toBe('The checker agents.');
  });

  it('marks modified rows and where the others get their value', () => {
    const effort = rowView(rows[2]);
    expect(effort).toMatchObject({ modified: true, control: 'select', choices: ['', 'low', 'high'], nullable: true, inherits: 'default' });
    expect(rowView(rows[7])).toMatchObject({ modified: false, inherits: 'global', control: 'model' });
    expect(rowView(rows[1]).defaultText).toBe('"opus"');
    expect(rowView(rows[6])).toMatchObject({ integer: true, nullable: false });
    expect(rowView(rows[11])).toMatchObject({ integer: false, nullable: true });
  });

  it('filters by words in key, label and description, and by @modified', () => {
    expect(buildSections(rows, 'ALIAS').map((s) => s.rows.map((r) => r.key))).toEqual([['claude.model']]);
    expect(buildSections(rows, 'request limit')[0].rows[0].key).toBe('checkers.command.request_limit');
    expect(buildSections(rows, '@modified').flatMap((s) => s.rows.map((r) => r.key))).toEqual(['claude.effort']);
    expect(buildSections(rows, 'claude @modified').flatMap((s) => s.rows)).toHaveLength(1);
    expect(buildSections(rows, 'zzz')).toEqual([]);
    expect(matchesSearch(rowView(rows[1]), '   ')).toBe(true);
  });

  it('formats values for their control', () => {
    expect(formatControlValue('toggle', true)).toBe('true');
    expect(formatControlValue('toggle', null)).toBe('false');
    expect(formatControlValue('list', ['/a', '/b'])).toBe('/a\n/b');
    expect(formatControlValue('json', { A: '1' })).toBe('{\n  "A": "1"\n}');
    expect(formatControlValue('json', null)).toBe('null');
    expect(formatControlValue('text', null)).toBe('');
    expect(formatControlValue('number', 8)).toBe('8');
  });

  it('parses what the user entered', () => {
    const view = (index: number) => rowView(rows[index]);
    expect(parseControlValue(view(3), true)).toEqual({ ok: true, value: true });
    expect(parseControlValue(view(6), ' 12 ')).toEqual({ ok: true, value: 12 });
    expect(parseControlValue(view(6), '1.5')).toEqual({ ok: false, message: 'Enter a whole number.' });
    expect(parseControlValue(view(6), '')).toEqual({ ok: false, message: 'Enter a number.' });
    expect(parseControlValue(view(6), 'many')).toMatchObject({ ok: false });
    expect(parseControlValue(view(11), '2.5')).toEqual({ ok: true, value: 2.5 });
    expect(parseControlValue(view(11), '')).toEqual({ ok: true, value: null });
    expect(parseControlValue(view(2), 'high')).toEqual({ ok: true, value: 'high' });
    expect(parseControlValue(view(2), '')).toEqual({ ok: true, value: null });
    expect(parseControlValue(view(2), 'extreme')).toEqual({ ok: false, message: 'Choose one of: low, high.' });
    expect(parseControlValue(view(9), ' /a \n\n/b\n')).toEqual({ ok: true, value: ['/a', '/b'] });
    expect(parseControlValue(view(9), '')).toEqual({ ok: true, value: [] });
    expect(parseControlValue(view(10), '{"B": 2}')).toEqual({ ok: true, value: { B: 2 } });
    expect(parseControlValue(view(10), '{B: 2}')).toMatchObject({ ok: false });
    expect(parseControlValue(view(8), '# New\n')).toEqual({ ok: true, value: '# New\n' });
    expect(parseControlValue(view(1), ' sonnet ')).toEqual({ ok: true, value: 'sonnet' });
    expect(parseControlValue(view(1), '')).toEqual({ ok: true, value: null });
  });

  it('turns findings into setup items; claude gets a link, never an install', () => {
    const items = setupItems([
      { name: 'claude', ok: false, level: 'missing', message: 'not found', advice: 'install it' },
      { name: 'git', ok: true, level: 'ok', message: 'git 2.43', advice: '' },
      { name: 'tmux', ok: false, level: 'note', message: 'not installed', advice: '' },
      { bogus: true } as never,
    ]);
    expect(items).toEqual([
      { name: 'claude', level: 'missing', message: 'not found', advice: 'install it', fix: { action: 'openClaudeDocs', label: 'How to install Claude Code' } },
      { name: 'git', level: 'ok', message: 'git 2.43', advice: '' },
      { name: 'tmux', level: 'note', message: 'not installed', advice: '' },
    ]);
    expect(setupItems(null)).toEqual([]);
  });
});

describe('parseSettingsMessage', () => {
  it('accepts valid messages and keeps only the known fields', () => {
    expect(parseSettingsMessage({ type: 'load' })).toEqual({ type: 'load', project: null });
    expect(parseSettingsMessage({ type: 'load', project: 'demo', x: 1 })).toEqual({ type: 'load', project: 'demo' });
    expect(parseSettingsMessage({ type: 'set', key: 'claude.model', value: null })).toEqual({ type: 'set', key: 'claude.model', value: null, project: null });
    expect(parseSettingsMessage({ type: 'set', key: 'a.b', value: { x: [1] }, project: 'demo' })).toEqual({
      type: 'set', key: 'a.b', value: { x: [1] }, project: 'demo',
    });
    expect(parseSettingsMessage({ type: 'unset', key: 'a.b', project: '' })).toEqual({ type: 'unset', key: 'a.b', project: null });
    expect(parseSettingsMessage({ type: 'setAllModels', model: ' default ', effort: 'high' })).toEqual({
      type: 'setAllModels', model: 'default', effort: 'high', project: null,
    });
    expect(parseSettingsMessage({ type: 'setAllModels', model: 'anthropic:claude-haiku-5-5' })).toEqual({
      type: 'setAllModels', model: 'anthropic:claude-haiku-5-5', project: null,
    });
    expect(parseSettingsMessage({ type: 'setRoleModel', role: 'checkers.model', model: '' })).toEqual({
      type: 'setRoleModel', role: 'checkers.model', model: '', project: null,
    });
    expect(parseSettingsMessage({ type: 'editChecker', name: 'command' })).toEqual({ type: 'editChecker', name: 'command' });
    expect(parseSettingsMessage({ type: 'runStatus' })).toEqual({ type: 'runStatus' });
    expect(parseSettingsMessage({ type: 'fix', action: 'copyRootInstall' })).toEqual({ type: 'fix', action: 'copyRootInstall' });
  });

  it('drops invalid messages', () => {
    for (const bad of [
      undefined,
      [],
      { type: 'save' },
      { type: 'load', project: 5 },
      { type: 'set', key: '', value: 1 },
      { type: 'set', key: 'a.b' },
      { type: 'set', key: 'a.b', value: 'x'.repeat(MAX_VALUE_JSON) },
      { type: 'set', key: 'a.b', value: 1, project: { name: 'demo' } },
      { type: 'unset' },
      { type: 'setAllModels', model: '' },
      { type: 'setAllModels', model: 'm', effort: 5 },
      { type: 'setRoleModel', role: 'r' },
      { type: 'editChecker', name: ' ' },
      { type: 'fix', action: 'runAsRoot' },
    ]) {
      expect(parseSettingsMessage(bad), JSON.stringify(bad)?.slice(0, 80)).toBeUndefined();
    }
  });

  it('names where an error is shown', () => {
    expect(errorKey({ type: 'set', key: 'a.b', value: 1, project: null })).toBe('a.b');
    expect(errorKey({ type: 'setRoleModel', role: 'r', model: 'm', project: null })).toBe('models');
    expect(errorKey({ type: 'runStatus' })).toBe('status');
    expect(errorKey({ type: 'load', project: null })).toBe('load');
    expect(errorKey({ type: 'editChecker', name: 'c' })).toBe('fix');
  });
});
