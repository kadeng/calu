import { describe, expect, it } from 'vitest';
import { HumanDiffGate, MAX_CHARS, OFF_PAUSE_MS, enableOffer, humanDiffParams, normaliseLineEnds } from '../../src/shared/humanDiffs';

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
