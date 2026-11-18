/**
 * Model of the session QuickPick (no `vscode` import): which entries it shows, in which order,
 * and what choosing one does.
 *
 *     ide.sessions ──buildSessionPicks──►  New session…
 *                                          ── Running in this workspace ──   -> ide.attach
 *                                          ── Other running sessions ──      -> ide.attach
 *                                          ── Stored conversations ──        -> ide.start resume | fork
 */

import type { IdeSessionsResult, RunningSession, StoredConversation } from '../bridgeProtocol';
import { sessionTitle } from './viewState';

/** What choosing an entry does. */
export type SessionChoice =
  | { kind: 'new' }
  | { kind: 'attach'; sessionId: string }
  /** Resume the stored conversation, or (via the entry's button) fork it. */
  | { kind: 'stored'; claudeSessionId: string };

/** One row of the QuickPick: an entry or a separator line. */
export type SessionPick =
  | { separator: true; label: string }
  | {
      separator: false;
      label: string;
      /** Shown after the label, dimmed. */
      description: string;
      /** Second line. */
      detail: string;
      choice: SessionChoice;
      /** True for the session that is attached now. */
      current: boolean;
    };

/**
 * Format an age for a list.
 *
 * @param seconds Seconds (negative values count as 0).
 * @returns "just now" below a minute, else e.g. "5 min ago", "3 h ago", "2 d ago".
 */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/**
 * Shorten a text to one line.
 *
 * @param text Any text.
 * @param limit Maximum length.
 * @returns The text with white space runs as single spaces, cut to `limit` characters with "…".
 */
export function oneLine(text: string, limit = 100): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}

/**
 * Build the row of a running session.
 *
 * @param s The session.
 * @param attachedId Id of the attached session, or null.
 * @returns The row: label = name or directory name; description = state, open escalations,
 *   "stale" and "attached" marks; detail = directory and short id.
 */
function runningPick(s: RunningSession, attachedId: string | null): SessionPick {
  const marks = [
    s.state,
    s.open_escalations > 0 ? `${s.open_escalations} waiting for a decision` : '',
    s.stale ? 'stale (its process seems gone)' : '',
    s.id === attachedId ? 'attached' : '',
  ].filter(Boolean);
  return {
    separator: false,
    label: sessionTitle({ id: s.id, name: s.name ?? '', directory: s.directory }),
    description: marks.join(' · '),
    detail: `${s.directory} · ${s.id.slice(0, 8)}`,
    choice: { kind: 'attach', sessionId: s.id },
    current: s.id === attachedId,
  };
}

/**
 * Build the row of a stored conversation.
 *
 * @param c The conversation.
 * @returns The row: label = its last user input (one line); description = turns and age;
 *   detail = the Claude session id and whether it was started without calu.
 */
function storedPick(c: StoredConversation): SessionPick {
  return {
    separator: false,
    label: oneLine(c.last_input) || '(no user input)',
    description: `${c.turns} turn${c.turns === 1 ? '' : 's'} · ${formatAge(c.age_s)}`,
    detail: `${c.claude_session_id}${c.outside ? ' · started without calu' : ''}`,
    choice: { kind: 'stored', claudeSessionId: c.claude_session_id },
    current: false,
  };
}

/**
 * Build the rows of the session QuickPick.
 *
 * @param sessions The result of `ide.sessions` (members may be missing).
 * @param attachedId Id of the attached session, or null.
 * @returns "New session…" first; then the running sessions of the workspace, the other running
 *   sessions, and the stored conversations that no running session uses, each group under a
 *   separator and omitted when empty.
 */
export function buildSessionPicks(sessions: Partial<IdeSessionsResult>, attachedId: string | null): SessionPick[] {
  const running = Array.isArray(sessions.running) ? sessions.running.filter((s) => s && typeof s.id === 'string') : [];
  const stored = Array.isArray(sessions.stored)
    ? sessions.stored.filter((c) => c && typeof c.claude_session_id === 'string' && !c.running)
    : [];
  const picks: SessionPick[] = [
    {
      separator: false,
      label: '$(add) New session…',
      description: '',
      detail: 'Start a session in this workspace folder, with an optional first message',
      choice: { kind: 'new' },
      current: false,
    },
  ];
  const here = running.filter((s) => s.here);
  const elsewhere = running.filter((s) => !s.here);
  if (here.length > 0) picks.push({ separator: true, label: 'Running in this workspace' }, ...here.map((s) => runningPick(s, attachedId)));
  if (elsewhere.length > 0) picks.push({ separator: true, label: 'Other running sessions' }, ...elsewhere.map((s) => runningPick(s, attachedId)));
  if (stored.length > 0) picks.push({ separator: true, label: 'Stored conversations (resume or fork)' }, ...stored.map(storedPick));
  return picks;
}

/** Most characters of a session's name (calu's limit). */
export const MAX_SESSION_NAME = 80;

/**
 * Name a session that is cut from another one.
 *
 * @param how "fork" (a copy that continues beside the original) or "rewind" (the conversation
 *   from before one of its turns).
 * @param from The display name of the session it is cut from (its name, or its directory's
 *   last segment).
 * @returns "fork of <from>" or "rewind of <from>", cut to `MAX_SESSION_NAME` characters with
 *   "…"; "" if `from` is empty.
 *
 * @example derivedSessionName("rewind", "parser work") === "rewind of parser work"
 */
export function derivedSessionName(how: 'fork' | 'rewind', from: string): string {
  const base = from.trim();
  if (!base) return '';
  const name = `${how} of ${base}`;
  return name.length <= MAX_SESSION_NAME ? name : `${name.slice(0, MAX_SESSION_NAME - 1)}…`;
}

/**
 * Check a session name entered by the user.
 *
 * @param name The text of the input box.
 * @returns `undefined` if the name is acceptable (at most 80 characters after trimming; empty
 *   removes the name), else the message to show.
 */
export function validateSessionName(name: string): string | undefined {
  return name.trim().length > 80 ? 'A session name has at most 80 characters.' : undefined;
}
