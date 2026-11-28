import { describe, expect, it } from 'vitest';
import {
  buildCommentParams,
  describeGitRef,
  normaliseRelativePath,
  parseGitQuery,
  relativePathWithin,
  selectionToLines,
  type PlainSelection,
} from '../../src/commentPayload';

/**
 * Build a selection from 0-based (line, character) pairs; the cursor is at the end.
 *
 * @returns The selection.
 */
function sel(sl: number, sc: number, el: number, ec: number): PlainSelection {
  return {
    start: { line: sl, character: sc },
    end: { line: el, character: ec },
    active: { line: el, character: ec },
    isEmpty: sl === el && sc === ec,
  };
}

describe('selectionToLines', () => {
  it('uses the cursor line for an empty selection', () => {
    expect(selectionToLines(sel(4, 3, 4, 3))).toEqual({ startLine: 5, endLine: 5 });
  });
  it('converts to 1-based inclusive lines', () => {
    expect(selectionToLines(sel(0, 2, 2, 5))).toEqual({ startLine: 1, endLine: 3 });
  });
  it('excludes a last line selected only up to column 0', () => {
    expect(selectionToLines(sel(4, 0, 7, 0))).toEqual({ startLine: 5, endLine: 7 });
  });
  it('gives the line of a one-line selection that ends inside its line', () => {
    expect(selectionToLines(sel(3, 0, 3, 4))).toEqual({ startLine: 4, endLine: 4 });
  });
});

describe('git URI query', () => {
  it('parses path and ref', () => {
    expect(parseGitQuery('{"path":"/w/a.py","ref":"HEAD"}')).toEqual({ path: '/w/a.py', ref: 'HEAD' });
    expect(parseGitQuery('{"path":"/w/a.py","ref":""}')).toEqual({ path: '/w/a.py', ref: '' });
  });
  it('rejects other queries', () => {
    expect(parseGitQuery('')).toBeUndefined();
    expect(parseGitQuery('id=1&side=before')).toBeUndefined();
    expect(parseGitQuery('{"ref":"HEAD"}')).toBeUndefined();
    expect(parseGitQuery('null')).toBeUndefined();
  });
  it('describes refs', () => {
    expect(describeGitRef('HEAD')).toBe('HEAD');
    expect(describeGitRef('')).toBe('index');
    expect(describeGitRef('~')).toBe('index or HEAD');
  });
});

describe('normaliseRelativePath', () => {
  it('uses forward slashes', () => {
    expect(normaliseRelativePath('src\\a.py')).toBe('src/a.py');
    expect(normaliseRelativePath('./a.py')).toBe('a.py');
  });
  it('rejects paths outside the workspace', () => {
    expect(() => normaliseRelativePath('/etc/passwd')).toThrow(/not inside/);
    expect(() => normaliseRelativePath('C:\\x\\a.py')).toThrow(/not inside/);
    expect(() => normaliseRelativePath('')).toThrow();
  });
});

describe('relativePathWithin', () => {
  it('computes paths inside the folder', () => {
    expect(relativePathWithin('/w/proj', '/w/proj/src/a.py')).toBe('src/a.py');
    expect(relativePathWithin('/w/proj/', '/w/proj/a.py')).toBe('a.py');
  });
  it('rejects files outside the folder, sibling prefixes and ".."', () => {
    expect(() => relativePathWithin('/w/proj', '/w/other/a.py')).toThrow(/not inside/);
    expect(() => relativePathWithin('/w/proj', '/w/project2/a.py')).toThrow(/not inside/);
    expect(() => relativePathWithin('/w/proj', '/w/proj')).toThrow(/not inside/);
    expect(() => relativePathWithin('/w/proj', '/w/proj/../etc/passwd')).toThrow(/not inside/);
    expect(() => normaliseRelativePath('src/../../x')).toThrow(/not inside/);
  });
});

describe('buildCommentParams', () => {
  it('builds a file comment', () => {
    const params = buildCommentParams({
      path: 'src/a.py',
      selection: sel(1, 0, 3, 0),
      text: '  explain  ',
      file: { text: 'l1\nl2\nl3\n' },
    });
    expect(params).toEqual({
      kind: 'file',
      path: 'src/a.py',
      side: 'after',
      start_line: 2,
      end_line: 3,
      after_text: 'l1\nl2\nl3\n',
      text: 'explain',
    });
  });

  it('builds a diff comment on the before side with a ref', () => {
    const params = buildCommentParams({
      path: 'a.py',
      selection: sel(0, 0, 0, 0),
      text: 'why removed?',
      diff: { side: 'before', beforeText: 'old\n', afterText: 'new\n', ref: 'HEAD' },
    });
    expect(params).toEqual({
      kind: 'diff',
      path: 'a.py',
      side: 'before',
      start_line: 1,
      end_line: 1,
      before_text: 'old\n',
      after_text: 'new\n',
      ref: 'HEAD',
      text: 'why removed?',
    });
  });

  it('omits ref when unknown', () => {
    const params = buildCommentParams({
      path: 'a.py',
      selection: sel(0, 0, 0, 0),
      text: 'x',
      diff: { side: 'after', beforeText: '', afterText: 'n' },
    });
    expect('ref' in params).toBe(false);
  });

  it('rejects empty comments and ambiguous input', () => {
    expect(() =>
      buildCommentParams({ path: 'a', selection: sel(0, 0, 0, 0), text: '  ', file: { text: '' } }),
    ).toThrow(/empty/);
    expect(() => buildCommentParams({ path: 'a', selection: sel(0, 0, 0, 0), text: 'x' })).toThrow();
  });
});
