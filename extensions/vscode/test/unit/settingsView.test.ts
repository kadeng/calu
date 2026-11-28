// The settings dialog's view logic: presets, setup rows, honesty sentences, search
// highlighting and the bookkeeping of optimistic saves.
import { describe, expect, it } from 'vitest';
import type { IdeStatusResult, ModelPreset, ModelRole, SettingRow } from '../../src/bridgeProtocol';
import {
  PendingEdits,
  cardMatches,
  effectiveSearch,
  groupPresets,
  highlightRanges,
  modeSentences,
  paragraphs,
  presetForModel,
  presetModel,
  roleModelText,
  rolesState,
  sameJson,
  searchWords,
  selectedPresetId,
  setupRows,
  setupSummary,
  withRowState,
} from '../../src/shared/settingsView';

const presets: ModelPreset[] = [
  { id: 'default', label: "calu's default", group: 'Default', model: '', note: 'Stores no model.' },
  { id: 'anthropic-haiku', label: 'Anthropic · Haiku', group: 'Anthropic', model: 'anthropic:haiku', note: 'key' },
  { id: 'bedrock-haiku', label: 'Bedrock · Haiku', group: 'AWS Bedrock', model: 'bedrock:haiku', note: 'aws' },
  { id: 'anthropic-sonnet', label: 'Anthropic · Sonnet', group: 'Anthropic', model: 'anthropic:sonnet', note: 'key' },
];

/**
 * Build roles that store the given models.
 *
 * @returns One role per model.
 */
function roles(...models: string[]): ModelRole[] {
  return models.map((model, index) => ({ key: `role${index}.model`, label: `Role ${index}`, model }));
}

describe('model presets', () => {
  it('groups presets by provider in the order of first appearance', () => {
    expect(groupPresets(presets).map((g) => [g.group, g.presets.map((p) => p.id)])).toEqual([
      ['Default', ['default']],
      ['Anthropic', ['anthropic-haiku', 'anthropic-sonnet']],
      ['AWS Bedrock', ['bedrock-haiku']],
    ]);
    expect(groupPresets(null)).toEqual([]);
    expect(groupPresets([{ id: 'x', label: 'X', group: '', model: null, note: '' }, {} as ModelPreset])[0]).toMatchObject({ group: 'Other' });
  });

  it('reads a preset model; null and "" mean no model', () => {
    expect(presetModel({ model: null })).toBe('');
    expect(presetModel({ model: 'a:b' })).toBe('a:b');
  });

  it('tells whether all roles use the same model', () => {
    expect(rolesState([])).toEqual({ kind: 'none' });
    expect(rolesState(null)).toEqual({ kind: 'none' });
    expect(rolesState(roles('', ''))).toEqual({ kind: 'uniform', model: '' });
    expect(rolesState(roles('a:b', ' a:b '))).toEqual({ kind: 'uniform', model: 'a:b' });
    expect(rolesState(roles('a:b', ''))).toEqual({ kind: 'mixed' });
  });

  it('marks the preset every role stores; default when none is stored', () => {
    expect(selectedPresetId(presets, roles('', '', ''))).toBe('default');
    expect(selectedPresetId(presets, roles('bedrock:haiku', 'bedrock:haiku'))).toBe('bedrock-haiku');
    expect(selectedPresetId(presets, roles('bedrock:haiku', 'anthropic:haiku'))).toBeUndefined();
    expect(selectedPresetId(presets, roles('custom:model'))).toBeUndefined();
    expect(selectedPresetId(presets, [])).toBeUndefined();
  });

  it('finds a preset by model and describes a role model', () => {
    expect(presetForModel(presets, 'anthropic:sonnet')?.id).toBe('anthropic-sonnet');
    expect(presetForModel(presets, 'nope')).toBeUndefined();
    expect(roleModelText('', presets, 'anthropic:haiku')).toBe('default → anthropic:haiku');
    expect(roleModelText('', presets, '')).toBe('default');
    expect(roleModelText('bedrock:haiku', presets, 'x')).toBe('Bedrock · Haiku');
    expect(roleModelText('custom:model', presets, 'x')).toBe('custom:model');
  });
});

describe('search', () => {
  it('splits the search into words without filter words', () => {
    expect(searchWords('  Stream @modified  PART ')).toEqual(['stream', 'part']);
    expect(searchWords('')).toEqual([]);
  });

  it('finds and merges highlight ranges', () => {
    expect(highlightRanges('Stream partial', 'art str')).toEqual([[0, 3], [8, 11]]);
    expect(highlightRanges('aaaa', 'aa')).toEqual([[0, 4]]);
    expect(highlightRanges('model models', 'model')).toEqual([[0, 5], [6, 11]]);
    expect(highlightRanges('abcdef', 'abc cde')).toEqual([[0, 5]]);
    expect(highlightRanges('Label', '@modified')).toEqual([]);
    expect(highlightRanges('Label', '')).toEqual([]);
  });

  it('adds @modified for the chip once', () => {
    expect(effectiveSearch(' model ', false)).toBe('model');
    expect(effectiveSearch('model', true)).toBe('model @modified');
    expect(effectiveSearch('@Modified x', true)).toBe('@Modified x');
    expect(effectiveSearch('', true)).toBe('@modified');
  });

  it('keeps a card for an empty search or matching words, never for a filter word', () => {
    expect(cardMatches('models presets bedrock', '')).toBe(true);
    expect(cardMatches('models presets bedrock', 'Bedrock model')).toBe(true);
    expect(cardMatches('models presets bedrock', 'sandbox')).toBe(false);
    expect(cardMatches('models presets bedrock', 'model @modified')).toBe(false);
  });

  it('splits descriptions into paragraphs', () => {
    expect(paragraphs('one\ntwo\n\n  three  \n\n\n')).toEqual(['one two', 'three']);
    expect(paragraphs('')).toEqual([]);
  });
});

describe('setup rows', () => {
  const status: IdeStatusResult = {
    findings: [
      { name: 'claude', ok: false, level: 'missing', message: 'not found', advice: 'install it' },
      { name: 'uv', ok: true, level: 'ok', message: 'uv 0.9', advice: '' },
      { name: 'tmux', ok: false, level: 'note', message: 'not installed', advice: '' },
    ],
    database: { reachable: true, place: 'localhost/calu', problem: '' },
    server: { sessions_running: 0 },
  };
  const serverHelp = { command: 'calu serve', tool: 'plain' as const, detach: '', note: '' };

  it('lists findings, database and server; claude never gets an install fix', () => {
    const rows = setupRows({ status, serverHelp, problems: [] });
    expect(rows.map((r) => [r.name, r.level])).toEqual([
      ['claude', 'missing'], ['uv', 'ok'], ['tmux', 'note'], ['database', 'ok'], ['server', 'note'],
    ]);
    expect(rows[0]).toMatchObject({ title: 'Claude Code', fixes: [{ action: 'openClaudeDocs', label: 'How to install Claude Code' }] });
    expect(rows[3].message).toBe('Reachable (localhost/calu).');
    expect(rows[4].fixes.map((f) => f.action)).toEqual(['startServer']);
    expect(rows.flatMap((r) => r.fixes).map((f) => f.action)).not.toContain('installCalu');
  });

  it('reports an unreachable database with calu\'s problem text and a running server as ok', () => {
    const rows = setupRows({
      status: { ...status, database: { reachable: false, place: 'db', problem: 'password needed' }, server: { sessions_running: 2 } },
      serverHelp,
      problems: [],
    });
    expect(rows.find((r) => r.name === 'database')).toMatchObject({ level: 'missing', message: 'password needed' });
    expect(rows.find((r) => r.name === 'server')).toMatchObject({ level: 'ok', message: '2 sessions running on this machine.', fixes: [] });
  });

  it('offers install and restart when there is no status', () => {
    const rows = setupRows({ status: null, serverHelp: null, problems: ['calu: command not found', 'Settings: x'] });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ name: 'calu', level: 'missing', message: 'command not found' });
    expect(rows[0].fixes.map((f) => f.action)).toEqual(['installCalu', 'restartBridge']);
  });

  it('summarises the setup rows: a missing item, or only notes and ok rows', () => {
    const rows = setupRows({ status, serverHelp, problems: [] });
    expect(setupSummary(rows)).toEqual({ ok: false, text: '1 thing needs attention' });
    const fine = rows.filter((r) => r.level !== 'missing');
    expect(setupSummary(fine)).toEqual({ ok: true, text: 'Everything is in place (2 notes)' });
    expect(setupSummary(fine.filter((r) => r.level === 'ok'))).toEqual({ ok: true, text: 'Everything is in place' });
  });
});

describe('modeSentences', () => {
  it('splits a description at the choice markers, with or without backticks', () => {
    const text = '`off`: nothing of this runs. `track`: the state is kept; a `git commit` is certified. `enforce`: additionally, a command needs a clean state. Default `off`.';
    expect(modeSentences(text, ['off', 'track', 'enforce'])).toEqual({
      off: 'nothing of this runs.',
      track: 'the state is kept; a git commit is certified.',
      enforce: 'additionally, a command needs a clean state.',
    });
    expect(modeSentences('off: a. track: b.', ['off', 'track', 'enforce'])).toEqual({ off: 'a.', track: 'b.' });
    expect(modeSentences('No markers here.', ['off'])).toEqual({});
  });
});

describe('PendingEdits', () => {
  const before = { value: 'old', isSet: false };

  it('settles one edit with the value the host reports', () => {
    const pending = new PendingEdits();
    pending.begin('k', before, { value: 'new', isSet: true });
    expect(pending.isPending('k')).toBe(true);
    expect(pending.size).toBe(1);
    expect(pending.optimistic('k')).toEqual({ value: 'new', isSet: true });
    expect(pending.settle('k', { value: 'NEW', isSet: true })).toEqual({ value: 'NEW', isSet: true });
    expect(pending.isPending('k')).toBe(false);
    expect(pending.size).toBe(0);
  });

  it('rolls a refused edit back to what was shown before', () => {
    const pending = new PendingEdits();
    pending.begin('k', before, { value: 'bad', isSet: true });
    expect(pending.fail('k')).toEqual(before);
    expect(pending.isPending('k')).toBe(false);
    expect(pending.fail('k')).toBeUndefined();
  });

  it('keeps the latest optimistic value while a second edit is open', () => {
    const pending = new PendingEdits();
    pending.begin('k', before, { value: 'a', isSet: true });
    pending.begin('k', { value: 'a', isSet: true }, { value: 'b', isSet: true });
    expect(pending.settle('k', { value: 'a', isSet: true })).toEqual({ value: 'b', isSet: true });
    expect(pending.isPending('k')).toBe(true);
    // The second edit is refused: back to the value the host confirmed for the first.
    expect(pending.fail('k')).toEqual({ value: 'a', isSet: true });
  });

  it('does not roll back while another edit of the key is open, and settles unknown keys as given', () => {
    const pending = new PendingEdits();
    pending.begin('k', before, { value: 'a', isSet: true });
    pending.begin('k', { value: 'a', isSet: true }, { value: 'b', isSet: true });
    expect(pending.fail('k')).toBeUndefined();
    expect(pending.settle('k', { value: 'b', isSet: true })).toEqual({ value: 'b', isSet: true });
    expect(pending.settle('other', { value: 1, isSet: true })).toEqual({ value: 1, isSet: true });
    pending.begin('x', before, before);
    pending.clear();
    expect(pending.size).toBe(0);
  });
});

describe('row helpers', () => {
  it('replaces one row state and keeps the other rows', () => {
    const rows: SettingRow[] = [
      { key: 'a', type: '', default: '', description: '', value: 1, is_set: false, kind: 'int' },
      { key: 'b', type: '', default: '', description: '', value: 2, is_set: false, kind: 'int' },
    ];
    const next = withRowState(rows, 'b', { value: 5, isSet: true });
    expect(next[0]).toBe(rows[0]);
    expect(next[1]).toMatchObject({ key: 'b', value: 5, is_set: true });
    expect(rows[1].value).toBe(2);
  });

  it('compares JSON values', () => {
    expect(sameJson({ a: [1] }, { a: [1] })).toBe(true);
    expect(sameJson(null, undefined)).toBe(true);
    expect(sameJson('1', 1)).toBe(false);
  });
});
