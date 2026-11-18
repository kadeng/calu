// The session QuickPick's rows and the setup hints.
import { describe, expect, it } from 'vitest';
import type { IdeSessionsResult, IdeStatusResult } from '../../src/bridgeProtocol';
import { buildHints, findingOk } from '../../src/shared/hints';
import { buildSessionPicks, formatAge, oneLine, validateSessionName } from '../../src/shared/sessionPick';

const sessions: IdeSessionsResult = {
  running: [
    { id: 'aaaaaaaa-1', name: '', directory: '/other/proj', state: 'idle', claude_session_id: 'c0', open_escalations: 0, started_at: '', stale: true, here: false },
    { id: 'bbbbbbbb-2', name: 'refactor', directory: '/w', state: 'escalated', claude_session_id: 'c1', open_escalations: 2, started_at: '', stale: false, here: true },
  ],
  stored: [
    { claude_session_id: 'c1', last_input: 'in use', turns: 3, age_s: 10, running: true, outside: false },
    { claude_session_id: 'c2', last_input: 'Fix the\n  flaky test', turns: 1, age_s: 7200, running: false, outside: true },
  ],
};

describe('buildSessionPicks', () => {
  it('lists new, workspace sessions, other sessions, then stored conversations', () => {
    const picks = buildSessionPicks(sessions, 'bbbbbbbb-2');
    expect(picks.map((p) => (p.separator ? `--${p.label}` : p.label))).toEqual([
      '$(add) New session…',
      '--Running in this workspace',
      'refactor',
      '--Other running sessions',
      'proj',
      '--Stored conversations (resume or fork)',
      'Fix the flaky test',
    ]);
    const here = picks[2];
    if (here.separator) throw new Error('expected an entry');
    expect(here).toMatchObject({
      description: 'escalated · 2 waiting for a decision · attached',
      detail: '/w · bbbbbbbb',
      choice: { kind: 'attach', sessionId: 'bbbbbbbb-2' },
      current: true,
    });
    const other = picks[4];
    if (other.separator) throw new Error('expected an entry');
    expect(other.description).toContain('stale');
    const stored = picks[6];
    if (stored.separator) throw new Error('expected an entry');
    expect(stored).toMatchObject({
      description: '1 turn · 2 h ago',
      detail: 'c2 · started without calu',
      choice: { kind: 'stored', claudeSessionId: 'c2' },
    });
  });

  it('offers only a new session when nothing is listed', () => {
    expect(buildSessionPicks({}, null)).toHaveLength(1);
    expect(buildSessionPicks({ running: 'x', stored: null } as unknown as IdeSessionsResult, null)).toHaveLength(1);
  });
});

describe('texts', () => {
  it('formats ages and one-line texts', () => {
    expect(formatAge(-3)).toBe('just now');
    expect(formatAge(59)).toBe('just now');
    expect(formatAge(60)).toBe('1 min ago');
    expect(formatAge(86400 * 2 + 5)).toBe('2 d ago');
    expect(oneLine(' a\n\tb ')).toBe('a b');
    expect(oneLine('x'.repeat(200), 10)).toBe('xxxxxxxxx…');
  });

  it('limits session names to 80 characters', () => {
    expect(validateSessionName('')).toBeUndefined();
    expect(validateSessionName(` ${'n'.repeat(80)} `)).toBeUndefined();
    expect(validateSessionName('n'.repeat(81))).toMatch(/80/);
  });
});

describe('buildHints', () => {
  const status = (overrides: Partial<IdeStatusResult> = {}): IdeStatusResult => ({
    findings: [
      { name: 'claude', ok: true, level: 'ok', message: 'claude 2.1.80', advice: '' },
      { name: 'tmux', ok: true, level: 'ok', message: 'tmux 3.4', advice: '' },
      { name: 'screen', ok: false, level: 'note', message: 'not installed', advice: '' },
    ],
    database: { reachable: true, place: 'local', problem: '' },
    server: { sessions_running: 0 },
    ...overrides,
  });

  it('shows only the install hint while calu is missing', () => {
    const hints = buildHints({ caluMissing: true, status: null, attached: false, noServer: true });
    expect(hints.map((h) => [h.id, h.action])).toEqual([['calu-missing', 'install']]);
  });

  it('names Claude Code in the hint about a missing claude, with the finding as the detail', () => {
    const missing = status({ findings: [{ name: 'claude', ok: false, level: 'missing', message: 'not on PATH', advice: '' }] });
    const [hint] = buildHints({ caluMissing: false, status: missing, attached: true, noServer: false });
    expect(hint).toMatchObject({ id: 'claude-missing', action: 'openLink' });
    expect(hint.message.startsWith('Claude Code (`claude`) was not found on this machine (not on PATH). The extension does not install')).toBe(true);
  });

  it('offers a new session, and the server command when no server answered', () => {
    expect(buildHints({ caluMissing: false, status: status(), attached: false, noServer: false }).map((h) => h.id)).toEqual(['no-session']);
    expect(buildHints({ caluMissing: false, status: status(), attached: false, noServer: true }).map((h) => [h.id, h.action])).toEqual([
      ['no-server', 'startServer'],
      ['no-session', 'newSession'],
    ]);
    expect(buildHints({ caluMissing: false, status: status(), attached: true, noServer: true })).toEqual([]);
  });

  it('reports a missing claude with a link, never with an install action', () => {
    const missing = status({ findings: [{ name: 'claude', ok: false, level: 'missing', message: 'claude was not found.', advice: '' }] });
    const hint = buildHints({ caluMissing: false, status: missing, attached: true, noServer: false })[0];
    expect(hint).toMatchObject({ id: 'claude-missing', action: 'openLink', href: 'https://docs.anthropic.com/en/docs/claude-code' });
    expect(hint.message).toContain('does not install Claude Code');
  });

  it('reports an unreachable database', () => {
    const down = status({ database: { reachable: false, place: 'local', problem: 'connection refused' } });
    expect(buildHints({ caluMissing: false, status: down, attached: true, noServer: false })[0]).toMatchObject({
      id: 'database',
      action: 'openSettings',
      message: "calu's database is not reachable: connection refused.",
    });
  });

  it('reads tmux and screen from the findings', () => {
    expect(findingOk(status(), 'tmux')).toBe(true);
    expect(findingOk(status(), 'screen')).toBe(false);
    expect(findingOk(status(), 'absent')).toBe(false);
    expect(findingOk(null, 'tmux')).toBe(false);
  });
});
