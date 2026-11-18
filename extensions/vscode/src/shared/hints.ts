/**
 * Setup hints of the chat view (no `vscode` import): what is missing for the chat to work and
 * which button fixes it. The host computes them and sends them with `ui.hints`.
 *
 *     calu not found            -> "calu-missing"   button: Install calu
 *     claude not found          -> "claude-missing" button: How to install (opens the docs)
 *     password not given yet    -> "locked"         button: Unlock (asks the one password)
 *     database not reachable    -> "database"       button: Open settings (Setup card)
 *     no session attached and
 *       none running / no server -> "no-server"     button: Start calu server
 *       otherwise                -> "no-session"    button: New session
 */

import type { IdeStatusResult } from '../bridgeProtocol';
import { CLAUDE_DOCS_URL } from './bootstrap';
import type { SetupHint } from './viewState';

/** Inputs of `buildHints`. */
export interface HintInput {
  /** True if no calu command was found on the machine. */
  caluMissing: boolean;
  /** The last `ide.status`, or null if it was not asked yet or failed. */
  status: IdeStatusResult | null;
  /** True if a session is attached. */
  attached: boolean;
  /** True if `ide.start` last failed because no server answered (error -32013). */
  noServer: boolean;
  /** True if calu's logins need the one password and it was not given yet (default false). */
  locked?: boolean;
}

/**
 * Compute the setup hints, most important first.
 *
 * @param input What is known about the machine and the session.
 * @returns The hints. With calu missing, only that hint. While a session is attached, the
 *   "no-server" and "no-session" hints are left out.
 */
export function buildHints(input: HintInput): SetupHint[] {
  if (input.caluMissing) {
    return [
      {
        id: 'calu-missing',
        level: 'error',
        message:
          'calu is not installed on this machine. The extension can install it with uv into ~/.local/bin ' +
          '(it installs uv first if needed).',
        action: 'install',
        actionLabel: 'Install calu',
      },
    ];
  }
  const hints: SetupHint[] = [];
  const findings = input.status?.findings ?? [];
  const claude = findings.find((f) => f.name === 'claude');
  if (claude && !claude.ok && claude.level === 'missing') {
    hints.push({
      id: 'claude-missing',
      level: 'error',
      message:
        `Claude Code (\`claude\`) was not found on this machine${claude.message ? ` (${claude.message})` : ''}. The extension does not ` +
        'install Claude Code: follow the instructions, then run `claude` once in a terminal in the project folder.',
      action: 'openLink',
      actionLabel: 'How to install Claude Code',
      href: CLAUDE_DOCS_URL,
      short: 'Claude Code not found',
      actionShort: 'Guide',
    });
  }
  if (input.locked) {
    hints.push({
      id: 'locked',
      level: 'warning',
      message: 'calu needs its password once to list and attach to sessions. It is kept in memory until this window closes.',
      action: 'unlock',
      actionLabel: 'Unlock',
    });
  }
  if (input.status && !input.status.database.reachable && !input.locked) {
    hints.push({
      id: 'database',
      level: 'error',
      message: `calu's database is not reachable: ${input.status.database.problem || 'unknown problem'}.`,
      action: 'openSettings',
      actionLabel: 'Open settings',
    });
  }
  if (!input.attached) {
    if (input.noServer) {
      hints.push({
        id: 'no-server',
        level: 'warning',
        message:
          'No calu server answered. Start `calu serve` in a terminal; it runs the sessions and keeps running ' +
          'after VSCode closes.',
        action: 'startServer',
        actionLabel: 'Start calu server',
      });
    }
    hints.push({
      id: 'no-session',
      level: 'info',
      message: 'No session is attached. Start a new session, or attach to a running one.',
      action: 'newSession',
      actionLabel: 'New session',
    });
  }
  return hints;
}

/**
 * Tell from the findings of `ide.status` whether a program is installed.
 *
 * @param status The last `ide.status`, or null.
 * @param name The finding's name, e.g. "tmux".
 * @returns True if a finding with that name exists and is ok; false otherwise (also when the
 *   status is unknown).
 */
export function findingOk(status: IdeStatusResult | null, name: string): boolean {
  return status?.findings?.some((f) => f.name === name && f.ok === true) === true;
}
