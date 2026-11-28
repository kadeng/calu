// Commands that install uv and calu, and the report about claude.
import { describe, expect, it } from 'vitest';
import {
  CLAUDE_DOCS_URL,
  DEFAULT_INSTALL_SOURCE,
  caluInstallArgv,
  caluUpgradeArgv,
  claudeCheck,
  findUv,
  installSource,
  uvInstallCommand,
} from '../../src/shared/bootstrap';

const facts = (executables: string[], isWsl = false) => ({
  pathEnv: '/mnt/c/tools:/usr/bin',
  pathDelimiter: ':',
  home: '/home/me',
  isWsl,
  isWindows: false,
  isExecutable: (p: string) => executables.includes(p),
});

describe('uv', () => {
  it('is found in PATH, ~/.local/bin and ~/.cargo/bin, without Windows entries on WSL', () => {
    expect(findUv(facts(['/usr/bin/uv']))).toBe('/usr/bin/uv');
    expect(findUv(facts(['/mnt/c/tools/uv', '/home/me/.local/bin/uv'], true))).toBe('/home/me/.local/bin/uv');
    expect(findUv(facts(['/home/me/.cargo/bin/uv']))).toBe('/home/me/.cargo/bin/uv');
    expect(findUv(facts([]))).toBeUndefined();
  });

  it('is installed with the astral script, by curl or else wget, without editing shell files', () => {
    const command = uvInstallCommand();
    expect(command).toContain('curl -LsSf https://astral.sh/uv/install.sh');
    expect(command).toContain('wget -qO- https://astral.sh/uv/install.sh');
    expect(command).toContain('UV_NO_MODIFY_PATH=1 sh');
  });
});

describe('calu', () => {
  it('is installed as a uv tool with Python 3.14 from the configured source', () => {
    expect(caluInstallArgv('/u/uv', DEFAULT_INSTALL_SOURCE)).toEqual([
      '/u/uv', 'tool', 'install', '--force', '--python', '3.14', 'git+https://github.com/kadeng/calu',
    ]);
    expect(caluUpgradeArgv('/u/uv')).toEqual(['/u/uv', 'tool', 'upgrade', 'calu']);
  });

  it('trims the install source, falls back to the default when blank, and refuses an option', () => {
    expect(installSource(undefined)).toBe(DEFAULT_INSTALL_SOURCE);
    expect(installSource('  ')).toBe(DEFAULT_INSTALL_SOURCE);
    expect(installSource(' /home/me/calu ')).toBe('/home/me/calu');
    expect(() => installSource('--index-url=http://x')).toThrow(/must not start/);
  });
});

describe('claude', () => {
  it('is reported with its version, or with the address of its instructions', () => {
    expect(claudeCheck('/usr/bin/claude', '2.1.80 (Claude Code)\n')).toEqual({ found: true, path: '/usr/bin/claude', version: '2.1.80 (Claude Code)' });
    expect(claudeCheck('/usr/bin/claude', '')).toMatchObject({ found: true, version: 'unknown version' });
    const missing = claudeCheck(undefined, '');
    expect(missing.found).toBe(false);
    if (!missing.found) expect(missing.message).toContain(CLAUDE_DOCS_URL);
  });
});
