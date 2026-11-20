import { describe, expect, it } from 'vitest';
import { HumanDiffGate, MAX_CHARS, SaveBases, OFF_PAUSE_MS, enableOffer, humanDiffParams, normaliseLineEnds } from '../../src/shared/humanDiffs';

describe('humanDiffParams', () => {
  it('sends the path relative to the folder and the texts with "\\n" line ends', () => {
    expect(humanDiffParams('/w', '/w/doc/a.md', 'a\r\n', 'b\n')).toEqual({ path: 'doc/a.md', before: 'a\n', after: 'b\n' });
  });
  it('sends nothing for a file outside the folder, an unknown before, equal texts or long texts', () => {
    expect(humanDiffParams('/w', '/elsewhere/a.md', 'a', 'b')).toBeNull();
    expect(humanDiffParams('/w', '/w/a.md', undefined, 'b')).toBeNull();
    expect(humanDiffParams('/w', '/w/a.md', 'a\r\n', 'a\n')).toBeNull();
    expect(humanDiffParams('/w', '/w/a.md', 'x'.repeat(MAX_CHARS + 1), 'b')).toBeNull();
  });
  it('normalises lone carriage returns too', () => {
    expect(normaliseLineEnds('a\rb\r\nc')).toBe('a\nb\nc');
  });
});

describe('HumanDiffGate', () => {
  it('sends only while attached, and pauses after an "off" answer', () => {
    const gate = new HumanDiffGate();
    expect(gate.wanted(false, 0)).toBe(false);
    expect(gate.wanted(true, 0)).toBe(true);
    gate.heard(false, 1000);
    expect(gate.wanted(true, 1000 + OFF_PAUSE_MS - 1)).toBe(false);
    expect(gate.wanted(true, 1000 + OFF_PAUSE_MS)).toBe(true);
    gate.heard(false, 0);
    gate.reset();
    expect(gate.wanted(true, 1)).toBe(true);
    gate.heard(false, 0);
    gate.heard(true, 5);
    expect(gate.wanted(true, 6)).toBe(true);
  });
});

describe('enableOffer', () => {
  it('reads the offer of an ide.settings.set answer', () => {
    expect(enableOffer({ value: true, offer: { key: 'traces.enabled', text: 'Also?' } })).toEqual({ key: 'traces.enabled', text: 'Also?' });
    expect(enableOffer({ value: true })).toBeNull();
    expect(enableOffer({ value: true, offer: { key: 3 } })).toBeNull();
    expect(enableOffer(null)).toBeNull();
  });
});

describe('SaveBases', () => {
  const key = 'file:///w/doc/a.md';
  /** The params of a save of `after`, diffed against the base the document started from. */
  const save = (bases: SaveBases, after: string) => {
    const params = humanDiffParams('/w', '/w/doc/a.md', bases.base(key), after);
    bases.saved(key, after);
    return params;
  };

  it('diffs a normal save against the opened text, and the next save against the saved one', () => {
    const bases = new SaveBases();
    bases.opened(key, 'v1\n', false);
    bases.changed(key, 'v1 mine\n', true);
    expect(save(bases, 'v1 mine\n')).toEqual({ path: 'doc/a.md', before: 'v1\n', after: 'v1 mine\n' });
    bases.changed(key, 'v1 mine\n', false); // the dirty flag clears after the save
    expect(save(bases, 'v1 mine again\n')).toEqual({ path: 'doc/a.md', before: 'v1 mine\n', after: 'v1 mine again\n' });
  });

  it('diffs an overwrite of a version Claude wrote meanwhile against the buffer\'s start', () => {
    const bases = new SaveBases();
    bases.opened(key, 'a\nb\n', false);
    bases.changed(key, 'a mine\nb\n', true);
    // Claude writes "a\nb claude\n" on disk; VSCode keeps the dirty buffer; the user overwrites.
    expect(save(bases, 'a mine\nb\n')).toEqual({ path: 'doc/a.md', before: 'a\nb\n', after: 'a mine\nb\n' });
  });

  it('diffs against Claude\'s version after the user reloaded or reverted to it', () => {
    const bases = new SaveBases();
    bases.opened(key, 'a\nb\n', false);
    bases.changed(key, 'a mine\nb\n', true);
    bases.changed(key, 'a\nb claude\n', false); // revert, or a reload of a clean buffer
    bases.changed(key, 'a\nb claude mine\n', true);
    expect(save(bases, 'a\nb claude mine\n')).toEqual({ path: 'doc/a.md', before: 'a\nb claude\n', after: 'a\nb claude mine\n' });
  });

  it('has no base for a document opened dirty, closed, or too long', () => {
    const bases = new SaveBases();
    bases.opened(key, 'restored', true);
    expect(bases.base(key)).toBeUndefined();
    bases.saved(key, 'x');
    expect(bases.base(key)).toBe('x');
    bases.closed(key);
    expect(bases.base(key)).toBeUndefined();
    bases.opened(key, 'x'.repeat(MAX_CHARS + 1), false);
    expect(bases.base(key)).toBeUndefined();
  });
});
