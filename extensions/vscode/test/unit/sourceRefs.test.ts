// Source references in chat text become links that open the file at a line.
import { describe, expect, it } from 'vitest';
import { findSourceRefs, sourceRefHref, splitLine } from '../../src/shared/sourceRefs';

/** Return the references of a text as "path" or "path:line" strings. */
function refs(text: string): string[] {
  return findSourceRefs(text).map(sourceRefHref);
}

describe('findSourceRefs', () => {
  it('finds relative paths with a known extension, with or without a line', () => {
    expect(refs('See src/app.py:12 and docs/guide.md.')).toEqual(['src/app.py:12', 'docs/guide.md']);
    expect(refs('the bug is in lib/parse.rs:7:3, see main.go')).toEqual(['lib/parse.rs:7', 'main.go']);
    expect(refs('src/app.py#L12 and src/app.py#40')).toEqual(['src/app.py:12', 'src/app.py:40']);
    expect(refs('(config/settings.yaml), "./run.sh", ../x/y.ts:3')).toEqual([
      'config/settings.yaml',
      './run.sh',
      '../x/y.ts:3',
    ]);
  });

  it('keeps the positions, so that the text around a reference stays as it is', () => {
    const text = 'open hello_world.py:1293 now';
    const [ref] = findSourceRefs(text);
    expect(text.slice(ref.start, ref.end)).toBe('hello_world.py:1293');
    expect(ref).toMatchObject({ path: 'hello_world.py', line: 1293 });
  });

  it('leaves URLs, absolute paths, e-mail addresses and unknown extensions alone', () => {
    expect(refs('https://example.org/src/app.py:12')).toEqual([]);
    expect(refs('/etc/nginx/nginx.conf and C:\\code\\app.py')).toEqual([]);
    expect(refs('mail jane@example.md')).toEqual([]);
    expect(refs('version 1.2.3, picture.png, archive.tar.gz, e.g. this')).toEqual([]);
    expect(refs('app.pyc and app.py_old')).toEqual([]);
  });

  it('drops a line 0', () => {
    expect(findSourceRefs('a.py:0')).toEqual([{ start: 0, end: 6, path: 'a.py' }]);
  });
});

describe('splitLine', () => {
  it('reads ":line", "#Lline" and "#line"', () => {
    expect(splitLine('src/a.py:12')).toEqual({ path: 'src/a.py', line: 12 });
    expect(splitLine('src/a.py:12:4')).toEqual({ path: 'src/a.py', line: 12 });
    expect(splitLine('src/a.py#L12')).toEqual({ path: 'src/a.py', line: 12 });
    expect(splitLine('src/a.py#L12-L20')).toEqual({ path: 'src/a.py', line: 12 });
    expect(splitLine('src/a.py#12')).toEqual({ path: 'src/a.py', line: 12 });
    expect(splitLine('src/a.py')).toEqual({ path: 'src/a.py' });
  });
});

describe('a click on a source reference', () => {
  it('builds the openLink message for a bare file name with a line, which a scheme test would take for "hello.py:"', async () => {
    const { linkMessage } = await import('../../src/shared/chatModel');
    expect(linkMessage('hello_world.py:1293')).toEqual({ type: 'openLink', href: 'hello_world.py:1293' });
    expect(linkMessage('src/app.py#L12')).toEqual({ type: 'openLink', href: 'src/app.py#L12' });
    expect(linkMessage('vscode:extension/x')).toBeUndefined();
  });
});
