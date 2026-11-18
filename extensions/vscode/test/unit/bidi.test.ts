// Bidirectional control characters in untrusted text are made visible.
import { describe, expect, it } from 'vitest';
import { BIDI_CONTROLS, hasBidiControls, neutraliseBidi } from '../../src/shared/bidi';

describe('neutraliseBidi', () => {
  it('shows an override as a placeholder, so that the stored order is what is read', () => {
    expect(neutraliseBidi('rm -rf ‮gnp.tropxe')).toBe('rm -rf ⟦RLO⟧gnp.tropxe');
    expect(neutraliseBidi('a⁦b⁩c‏d؜e')).toBe('a⟦LRI⟧b⟦PDI⟧c⟦RLM⟧d⟦ALM⟧e');
  });

  it('replaces every character of the list and nothing else', () => {
    for (const [code, name] of Object.entries(BIDI_CONTROLS)) {
      const c = String.fromCodePoint(Number(code));
      expect(neutraliseBidi(`x${c}y`)).toBe(`x⟦${name}⟧y`);
      expect(hasBidiControls(c)).toBe(true);
    }
    expect(Object.keys(BIDI_CONTROLS)).toHaveLength(12);
  });

  it('leaves ordinary text alone, also right-to-left scripts and other invisible characters', () => {
    const plain = 'English, العربية, עברית, 日本語, zero​width, é, 👩‍💻';
    expect(neutraliseBidi(plain)).toBe(plain);
    expect(hasBidiControls(plain)).toBe(false);
    expect(neutraliseBidi('')).toBe('');
  });

  it('is stable when applied twice', () => {
    const once = neutraliseBidi('‭abc‬');
    expect(neutraliseBidi(once)).toBe(once);
  });
});
