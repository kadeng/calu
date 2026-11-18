// Finding calu and other programs on the workspace machine.
import { describe, expect, it } from 'vitest';
import { buildIdeArgv, findExecutable, joinPath, looksLikeWsl, resolveCaluCommand, type ResolveInput } from '../../src/caluCommand';

/**
 * Build resolver input for a Linux machine where exactly the given paths are executable.
 *
 * @returns The input.
 */
function input(executables: string[], overrides: Partial<ResolveInput> = {}): ResolveInput {
  return {
    configured: [],
    pathEnv: '/usr/local/bin:/mnt/c/Users/me/bin:/usr/bin',
    pathDelimiter: ':',
    home: '/home/me',
    isWsl: false,
    isWindows: false,
    isExecutable: (p) => executables.includes(p),
    ...overrides,
  };
}

describe('resolveCaluCommand', () => {
  it('prefers the setting', () => {
    const r = resolveCaluCommand(input(['/usr/bin/calu'], { configured: ['uv', 'run', 'calu'] }));
    expect(r).toEqual({ ok: true, command: ['uv', 'run', 'calu'], source: 'setting' });
  });

  it('ignores an empty setting and searches PATH in order', () => {
    expect(resolveCaluCommand(input(['/usr/bin/calu'], { configured: [''] }))).toEqual({ ok: true, command: ['/usr/bin/calu'], source: 'path' });
    expect(resolveCaluCommand(input(['/mnt/c/Users/me/bin/calu', '/usr/bin/calu']))).toEqual({
      ok: true,
      command: ['/mnt/c/Users/me/bin/calu'],
      source: 'path',
    });
  });

  it('skips /mnt/ PATH entries on WSL', () => {
    const r = resolveCaluCommand(input(['/mnt/c/Users/me/bin/calu', '/usr/bin/calu'], { isWsl: true }));
    expect(r).toEqual({ ok: true, command: ['/usr/bin/calu'], source: 'path' });
  });

  it('falls back to ~/.local/bin/calu', () => {
    const r = resolveCaluCommand(input(['/home/me/.local/bin/calu'], { isWsl: true }));
    expect(r).toEqual({ ok: true, command: ['/home/me/.local/bin/calu'], source: 'local-bin' });
  });

  it('says what to do when nothing is found', () => {
    const r = resolveCaluCommand(input([], { isWsl: true }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.message).toContain('Install calu');
  });

  it('finds calu.exe on Windows', () => {
    const r = resolveCaluCommand(input(['C:\\tools\\calu.exe'], { pathEnv: 'C:\\tools;C:\\other', pathDelimiter: ';', isWindows: true, home: '' }));
    expect(r).toEqual({ ok: true, command: ['C:\\tools\\calu.exe'], source: 'path' });
  });
});

describe('findExecutable', () => {
  it('looks in PATH, then in the given home directories', () => {
    expect(findExecutable('tmux', input(['/usr/bin/tmux']))).toEqual({ path: '/usr/bin/tmux', source: 'path' });
    expect(findExecutable('uv', input(['/home/me/.cargo/bin/uv']), [['.local', 'bin'], ['.cargo', 'bin']])).toEqual({
      path: '/home/me/.cargo/bin/uv',
      source: 'local-bin',
    });
    expect(findExecutable('tmux', input(['/home/me/.local/bin/tmux']), [])).toBeUndefined();
    expect(findExecutable('screen', input([]))).toBeUndefined();
  });
});

describe('buildIdeArgv', () => {
  it('appends ide', () => {
    expect(buildIdeArgv(['uv', 'run', 'calu'])).toEqual(['uv', 'run', 'calu', 'ide']);
  });
  it('rejects an empty command', () => {
    expect(() => buildIdeArgv([])).toThrow();
  });
});

describe('small helpers', () => {
  it('detects WSL kernels', () => {
    expect(looksLikeWsl('6.6.87.2-microsoft-standard-WSL2')).toBe(true);
    expect(looksLikeWsl('6.8.0-45-generic')).toBe(false);
  });
  it('joins paths in the style of the directory', () => {
    expect(joinPath('/usr/bin/', 'calu')).toBe('/usr/bin/calu');
    expect(joinPath('C:\\tools', 'calu.exe')).toBe('C:\\tools\\calu.exe');
  });
});
