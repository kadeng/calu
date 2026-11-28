// The command that starts `calu serve` so that it survives the editor.
import { execFileSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { buildServerHelp, rootInstallCommand, serveArgv, shellJoin, shellQuote, type ServerCommandInput } from '../../src/shared/serverCommand';

/**
 * Splits a command line into words with a real POSIX shell.
 *
 * Starts `sh` and lets it evaluate the given text, so `$(...)` and backticks in it would run.
 * Use it only for input that the quoting under test has already made safe.
 *
 * @returns The words the shell passes to a program.
 */
function shellWords(command: string): string[] {
  const out = execFileSync('sh', ['-c', `for a in ${command}; do printf '%s\\0' "$a"; done`], { encoding: 'utf8' });
  return out.split('\0').slice(0, -1);
}

const base: ServerCommandInput = { calu: ['/home/me/.local/bin/calu'], sudo: false, insecure: false, extraArgs: [], tmux: false, screen: false };

describe('shell quoting', () => {
  it('leaves plain words alone and single-quotes the rest', () => {
    expect(shellQuote('calu')).toBe('calu');
    expect(shellQuote('/a/b-c_d.e:f=g,h@i%j+k')).toBe('/a/b-c_d.e:f=g,h@i%j+k');
    expect(shellQuote('')).toBe("''");
    expect(shellQuote('two words')).toBe("'two words'");
    expect(shellQuote("it's")).toBe("'it'\\''s'");
    expect(shellQuote('$(rm -rf ~); `x` "y" \\ \n')).toBe("'$(rm -rf ~); `x` \"y\" \\ \n'");
    expect(shellJoin(['a', 'b c'])).toBe("a 'b c'");
    const hard = ['', "it's", '$(echo x)', '`y`', 'a\nb', '"q"', '\\', '*', '~', '-n'];
    expect(shellWords(shellJoin(hard))).toEqual(hard);
  });
});

describe('serveArgv', () => {
  it('adds the flags of the settings', () => {
    expect(serveArgv(base)).toEqual(['/home/me/.local/bin/calu', 'serve']);
    expect(serveArgv({ ...base, sudo: true, insecure: true, extraArgs: ['--user', 'claude bot', ''] })).toEqual([
      '/home/me/.local/bin/calu', 'serve', '--sudo', '--insecure', '--user', 'claude bot',
    ]);
    expect(serveArgv({ ...base, calu: [] })).toEqual(['calu', 'serve']);
  });
});

describe('buildServerHelp', () => {
  it('uses tmux when it is installed', () => {
    const help = buildServerHelp({ ...base, calu: ['calu'], sudo: true, tmux: true, screen: true });
    expect(help.command).toBe("tmux new-session -A -s calu 'calu serve --sudo'");
    expect(help.tool).toBe('tmux');
    expect(help.detach).toBe('Ctrl+B, then D');
    expect(help.note).toContain('keeps running after VSCode closes');
  });

  it('quotes twice for tmux: the serve command is one argument that a shell reads again', () => {
    const calu = ['uv', 'run', '--project', '/my projects/calu', 'calu'];
    const help = buildServerHelp({ ...base, calu, extraArgs: ["it's", '$HOME'], tmux: true });
    // A shell splits the command into tmux's arguments; the last one is the serve command.
    const outer = shellWords(help.command);
    expect(outer.slice(0, 5)).toEqual(['tmux', 'new-session', '-A', '-s', 'calu']);
    expect(outer).toHaveLength(6);
    // tmux hands that argument to a shell, which must see the original arguments.
    expect(shellWords(outer[5])).toEqual([...calu, 'serve', "it's", '$HOME']);
  });

  it('uses screen without tmux', () => {
    const help = buildServerHelp({ ...base, calu: ['calu'], insecure: true, extraArgs: ['a b'], screen: true });
    expect(help.command).toBe("screen -S calu -D -R calu serve --insecure 'a b'");
    expect(help.tool).toBe('screen');
    expect(help.detach).toBe('Ctrl+A, then D');
  });

  it('falls back to the plain command and says that it ends with the terminal', () => {
    const help = buildServerHelp({ ...base, calu: ['calu'] });
    expect(help).toMatchObject({ command: 'calu serve', tool: 'plain', detach: '' });
    expect(help.note).toContain('ends when the terminal closes');
  });
});

describe('rootInstallCommand', () => {
  it('is the documented sudo command with the quoted source', () => {
    const command = rootInstallCommand('git+https://github.com/kadeng/calu');
    expect(command).toMatch(/^sudo env UV_TOOL_DIR=\/usr\/local\/lib\/calu UV_TOOL_BIN_DIR=\/usr\/local\/bin /);
    expect(command.endsWith('uv tool install git+https://github.com/kadeng/calu')).toBe(true);
    expect(rootInstallCommand('/my checkout')).toContain("'/my checkout'");
  });
});
