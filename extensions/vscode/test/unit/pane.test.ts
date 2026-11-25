// The browser pane of `calu web`: its login subprotocols, the diff of an edit, and that the
// committed bundle matches the sources (PLAN.md D261).
import { existsSync, readFileSync } from 'node:fs';
import * as esbuild from 'esbuild';
import { describe, expect, it } from 'vitest';
import { base64url, keptFragment, loginProtocols, readPaneConfig } from '../../pane/config';
import { unifiedDiff } from '../../pane/diff';
import { PANE_OPTIONS, PANE_OUT } from '../../scripts/pane-options.mjs';

/** A storage in memory. */
function memory(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (k: string) => items.get(k) ?? null,
    key: (i: number) => [...items.keys()][i] ?? null,
    removeItem: (k: string) => void items.delete(k),
    setItem: (k: string, v: string) => void items.set(k, v),
  };
}

describe('the pane login', () => {
  it('reads dir and session from the fragment and drops anything else from the address', () => {
    const config = readPaneConfig('#token=abc&dir=%2Fhome%2Fme%2Fp', memory());
    expect(config).toEqual({ key: '', dir: '/home/me/p', session: '' });
    expect(keptFragment(config)).toBe('#dir=%2Fhome%2Fme%2Fp');
    expect(keptFragment({ key: '', dir: '', session: '' })).toBe('');
  });

  it('keeps the session a sessions-list link names', () => {
    const config = readPaneConfig('#dir=%2Fw&session=4f1c-77', memory());
    expect(config.session).toBe('4f1c-77');
    expect(keptFragment(config)).toBe('#dir=%2Fw&session=4f1c-77');
  });

  it('offers the key of a calu web login and a typed password as WebSocket subprotocols', () => {
    const local = memory();
    local.setItem('calu-key', 'k1');
    const config = readPaneConfig('', local);
    expect(loginProtocols(config)).toEqual(['calu', 'calu.key.k1']);
    expect(loginProtocols(config, 'pässwort/+')).toEqual(['calu', 'calu.key.k1', `calu.password.${base64url('pässwort/+')}`]);
    expect(loginProtocols({ key: '', dir: '', session: '' }, '')).toEqual(['calu', 'calu.password.']);
  });

  it('encodes the password in base64url without padding (subprotocol characters only)', () => {
    expect(base64url('p&ss')).toBe('cCZzcw');
    expect(base64url('ä?>')).toBe(Buffer.from('ä?>', 'utf-8').toString('base64url'));
    expect(base64url('ä?>')).toMatch(/^[A-Za-z0-9_-]*$/);
  });

  it('works without storage (a frame whose storage the browser blocks)', () => {
    const blocked = { getItem: () => { throw new Error('blocked'); } } as unknown as Storage;
    expect(readPaneConfig('#dir=%2Fw', blocked)).toEqual({ key: '', dir: '/w', session: '' });
  });
});

describe('unifiedDiff', () => {
  it('shows the changed lines with three lines of context', () => {
    expect(unifiedDiff('a\nb\nc', 'a\nB\nc', 'x.py')).toBe('--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c');
    expect(unifiedDiff('same', 'same', 'x.py')).toBe('');
  });

  it('splits distant changes into hunks with the right line numbers', () => {
    const before = Array.from({ length: 20 }, (_, i) => `l${i + 1}`).join('\n');
    const after = before.replace('l2\n', 'L2\n').replace('l18\n', 'L18\n');
    const diff = unifiedDiff(before, after, 'f');
    expect(diff).toContain('@@ -1,5 +1,5 @@');
    expect(diff).toContain('@@ -15,6 +15,6 @@');
    expect(diff).toContain('-l18\n+L18');
  });
});

describe('the committed pane bundle', () => {
  // Skipped where the extension is used without the Python package beside it (the Docker test
  // image copies the extension alone).
  it.skipIf(!existsSync(PANE_OUT))('is what the sources build to (run `npm run build:pane` after a change)', async () => {
    const built = await esbuild.build({ ...PANE_OPTIONS, write: false, outfile: 'pane.js' });
    const fresh = built.outputFiles[0].text;
    expect(fresh === readFileSync(`${PANE_OUT}/pane.js`, 'utf8'), 'src/calu/web/static/pane/pane.js is out of date').toBe(true);
    for (const [source, name] of [['media/chat.css', 'chat.css'], ['pane/theme.css', 'theme.css'], ['pane/pane.css', 'pane.css']]) {
      expect(readFileSync(source, 'utf8') === readFileSync(`${PANE_OUT}/${name}`, 'utf8'), `${name} is out of date`).toBe(true);
    }
  }, 30_000);
});
