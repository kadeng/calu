// The inline marks of calu's descriptions: backticks and double asterisks become parts.
import { describe, expect, it } from 'vitest';
import { inlineParts, plainInline } from '../../src/shared/inlineText';

describe('inlineParts', () => {
  it('splits code and strong spans from the text around them', () => {
    expect(inlineParts('Set `a.b` to **true** now')).toEqual([
      { kind: 'text', text: 'Set ' },
      { kind: 'code', text: 'a.b' },
      { kind: 'text', text: ' to ' },
      { kind: 'strong', text: 'true' },
      { kind: 'text', text: ' now' },
    ]);
    expect(inlineParts('`calu config set claude.sandbox.enabled false`')).toEqual([{ kind: 'code', text: 'calu config set claude.sandbox.enabled false' }]);
    expect(inlineParts('**two words**')).toEqual([{ kind: 'strong', text: 'two words' }]);
  });

  it('leaves marks without a partner, empty spans and spaced asterisks as written', () => {
    for (const plain of ['a ` b', 'a ** b', '``', '****', 'a ** b ** c', '2 * 3 * 4', 'line `one\ntwo` end', '']) {
      expect(plainInline(plain)).toBe(plain);
      expect(inlineParts(plain).every((p) => p.kind === 'text')).toBe(true);
    }
    expect(inlineParts('')).toEqual([]);
  });

  it('does not look for marks inside a code span, and does not nest', () => {
    expect(inlineParts('`a **b** c`')).toEqual([{ kind: 'code', text: 'a **b** c' }]);
    expect(inlineParts('**a `b` c**')).toEqual([{ kind: 'strong', text: 'a `b` c' }]);
  });

  it('keeps markup characters as text: nothing is HTML', () => {
    const parts = inlineParts('<img src=x onerror=alert(1)> `<b>` **<i>**');
    expect(parts).toEqual([
      { kind: 'text', text: '<img src=x onerror=alert(1)> ' },
      { kind: 'code', text: '<b>' },
      { kind: 'text', text: ' ' },
      { kind: 'strong', text: '<i>' },
    ]);
    expect(plainInline('use `x` **now**')).toBe('use x now');
  });
});
