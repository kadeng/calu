/**
 * Slash commands of the composer (pure): the command table, the completion list and what a
 * submitted command does.
 *
 *     input "/mo"            ──completions()──►  [/model …]              (list above the composer)
 *     input "/model so"      ──completions()──►  [sonnet]                (values of the command)
 *     Enter on "/model opus" ──resolveSlash()──► {kind: "message", message: {type: "setModel", model: "opus"}}
 *     Enter on "/compact"    ──resolveSlash()──► {kind: "chat"}          (sent to Claude Code as text)
 *     Enter on "/sessions"   ──resolveSlash()──► {kind: "ui", action: "sessions"}
 *
 * Commands of Claude Code are sent as chat text; Claude Code runs them. Commands of the
 * extension post the matching webview message or open a part of the view. A "/word" that is
 * not in the table is sent as chat text too (a custom command of the project).
 */

import { LEVELS, LEVEL_IDS, type LevelId } from './levels';
import { CLAUDE_EFFORTS, CLAUDE_MODELS, HONESTY_MODES, type ClaudeEffort, type HonestyMode, type WebviewMessage } from './messages';

/** One slash command. */
export interface SlashCommand {
  /** The name without the slash, e.g. "model". */
  name: string;
  /** One line for the completion list. */
  description: string;
  /** "claude": sent as chat text; "calu": an action of the extension. */
  source: 'claude' | 'calu';
  /** Values the command takes as its argument (offered after "/name "); empty if none. */
  values: readonly { value: string; description: string }[];
}

/**
 * Build a command entry.
 *
 * @param name The name without the slash.
 * @param source "claude" or "calu".
 * @param description One line for the list.
 * @param values Argument values with their descriptions.
 * @returns The entry.
 */
function command(name: string, source: SlashCommand['source'], description: string, values: SlashCommand['values'] = []): SlashCommand {
  return { name, description, source, values };
}

/** Commands of Claude Code that are always offered (sent as chat text). */
export const CLAUDE_COMMANDS: readonly SlashCommand[] = [
  command('compact', 'claude', "Summarize the conversation to free context (optional: what to keep)"),
  command('clear', 'claude', 'Start over with an empty conversation'),
  command('usage', 'claude', 'Show the usage of the account limits'),
  command('context', 'claude', 'Show what fills the context window'),
  command('cost', 'claude', 'Show the cost of the session'),
  command('help', 'claude', "Show Claude Code's help"),
];

/** Commands of the extension. */
export const CALU_COMMANDS: readonly SlashCommand[] = [
  command('model', 'calu', 'Change the model of the running session', CLAUDE_MODELS.map((m) => ({ value: m, description: m === 'default' ? "Claude Code's default model" : `The latest ${m} model` }))),
  command('effort', 'calu', 'Change the thinking effort (sessions started from now on)', CLAUDE_EFFORTS.map((e) => ({ value: e, description: e === 'default' ? "Claude Code's default effort" : '' }))),
  command('level', 'calu', 'Change the supervision level', LEVELS.map((l) => ({ value: l.id, description: l.name }))),
  command('honesty', 'calu', 'Change the code honesty mode', [
    { value: 'off', description: 'No honesty tracking' },
    { value: 'track', description: 'Track marks and unreviewed files' },
    { value: 'enforce', description: 'Running project code and commits need a clean state' },
  ]),
  command('checkers', 'calu', 'Edit the instructions of a checker'),
  command('checker-context', 'calu', 'Show the conversation as the checkers see it'),
  command('sessions', 'calu', 'Switch to another running session'),
  command('new', 'calu', 'Start a new session'),
  command('fork', 'calu', 'Fork the conversation into a new session'),
  command('rename', 'calu', 'Rename the attached session'),
  command('settings', 'calu', "Open calu's settings"),
  command('interrupt', 'calu', "Interrupt Claude's running turn"),
];

/**
 * Return the full command table.
 *
 * @param reported Names of further Claude Code commands that a session reported (without the
 *   slash); names already in the table are ignored.
 * @returns The extension's commands, Claude Code's built-in ones and the reported ones, sorted
 *   by name.
 */
export function allCommands(reported: readonly string[] = []): SlashCommand[] {
  const table = [...CALU_COMMANDS, ...CLAUDE_COMMANDS];
  const known = new Set(table.map((c) => c.name));
  for (const raw of reported) {
    const name = raw.replace(/^\//, '').trim();
    if (!/^[\w:.-]+$/.test(name) || known.has(name)) continue;
    known.add(name);
    table.push(command(name, 'claude', 'Command of Claude Code'));
  }
  return table.sort((a, b) => a.name.localeCompare(b.name));
}

/** One row of the completion list. */
export interface Completion {
  /** Shown text, e.g. "/model" or "sonnet". */
  label: string;
  description: string;
  /** "claude", "calu" or "value" (an argument value). */
  kind: 'claude' | 'calu' | 'value';
  /** The composer text after accepting the row with Tab. */
  insert: string;
  /** True if accepting with Enter should submit `insert` at once (nothing more to type). */
  submit: boolean;
}

/**
 * Compute the completion list for the composer text.
 *
 * @param text The whole composer text.
 * @param commands The command table (`allCommands()`).
 * @returns Empty unless the text is one line that starts with "/". For "/<part>": the commands
 *   whose name starts with `part`, then those that contain it. For "/<name> <part>" of a command
 *   with values: its values, filtered the same way. A command with values inserts "/name " and
 *   is not submitted; other rows are submitted on Enter.
 */
export function completions(text: string, commands: readonly SlashCommand[]): Completion[] {
  if (!text.startsWith('/') || text.includes('\n')) return [];
  const m = /^\/(\S*)(?:\s+(\S*))?$/.exec(text);
  if (!m) return [];
  const rank = <T>(items: readonly T[], key: (item: T) => string, part: string): T[] => {
    const p = part.toLowerCase();
    return [...items.filter((i) => key(i).toLowerCase().startsWith(p)), ...items.filter((i) => !key(i).toLowerCase().startsWith(p) && key(i).toLowerCase().includes(p))];
  };
  if (m[2] === undefined) {
    return rank(commands, (c) => c.name, m[1]).map((c) => ({
      label: `/${c.name}`,
      description: c.description,
      kind: c.source,
      insert: c.values.length > 0 ? `/${c.name} ` : `/${c.name}`,
      submit: c.values.length === 0,
    }));
  }
  const cmd = commands.find((c) => c.name === m[1]);
  if (!cmd || cmd.values.length === 0) return [];
  return rank(cmd.values, (v) => v.value, m[2]).map((v) => ({
    label: v.value,
    description: v.description,
    kind: 'value' as const,
    insert: `/${cmd.name} ${v.value}`,
    submit: true,
  }));
}

/** Parts of the view a command opens. */
export type SlashUiAction = 'sessions' | 'model' | 'effort' | 'level' | 'honesty';

/** What submitting a composer text that starts with "/" does. */
export type SlashResult =
  /** Send the text to Claude as a chat message. */
  | { kind: 'chat' }
  /** Post this message to the host. */
  | { kind: 'message'; message: WebviewMessage }
  /** Open a part of the view (the command needs a choice). */
  | { kind: 'ui'; action: SlashUiAction }
  /** Do nothing and show `text` near the input. */
  | { kind: 'error'; text: string };

/**
 * Decide what a submitted composer text does.
 *
 * @param text The composer text (trimmed by the caller or not).
 * @param attachedId Id of the attached session, or `null`.
 * @returns "chat" for a text that does not start with "/", for Claude Code's commands and for
 *   unknown commands; the message or view action of an extension command; "error" for an
 *   argument that is not one of the command's values, or a command that needs a session while
 *   none is attached.
 */
export function resolveSlash(text: string, attachedId: string | null): SlashResult {
  const m = /^\/([\w:.-]+)(?:\s+([\s\S]*))?$/.exec(text.trim());
  if (!m) return { kind: 'chat' };
  const cmd = CALU_COMMANDS.find((c) => c.name === m[1]);
  if (!cmd) return { kind: 'chat' };
  const arg = (m[2] ?? '').trim();
  const needsSession = (message: WebviewMessage): SlashResult =>
    attachedId ? { kind: 'message', message } : { kind: 'error', text: `/${cmd.name} needs an attached session.` };
  const value = (): string | undefined => (cmd.values.some((v) => v.value === arg) ? arg : undefined);
  const badValue = (): SlashResult => ({
    kind: 'error',
    text: `/${cmd.name} takes one of: ${cmd.values.map((v) => v.value).join(', ')}.`,
  });
  switch (cmd.name) {
    case 'model':
      if (!arg) return { kind: 'ui', action: 'model' };
      return /^[\w.:\[\]-]{1,100}$/.test(arg) ? needsSession({ type: 'setModel', model: arg }) : badValue();
    case 'effort':
      if (!arg) return { kind: 'ui', action: 'effort' };
      return value() ? { kind: 'message', message: { type: 'setEffort', effort: arg as ClaudeEffort } } : badValue();
    case 'level':
      if (!arg) return { kind: 'ui', action: 'level' };
      return (LEVEL_IDS as readonly string[]).includes(arg) ? needsSession({ type: 'setLevel', level: arg as LevelId }) : badValue();
    case 'honesty':
      if (!arg) return { kind: 'ui', action: 'honesty' };
      return (HONESTY_MODES as readonly string[]).includes(arg) ? { kind: 'message', message: { type: 'setHonestyMode', mode: arg as HonestyMode } } : badValue();
    case 'checkers':
      return { kind: 'message', message: { type: 'editChecker' } };
    case 'checker-context':
      return needsSession({ type: 'showContext' });
    case 'sessions':
      return { kind: 'ui', action: 'sessions' };
    case 'new':
      return { kind: 'message', message: { type: 'newSession' } };
    case 'fork':
      return needsSession({ type: 'forkConversation' });
    case 'rename':
      return attachedId ? { kind: 'message', message: { type: 'renameSession', sessionId: attachedId } } : needsSession({ type: 'detach' });
    case 'settings':
      return { kind: 'message', message: { type: 'openSettings' } };
    case 'interrupt':
      return needsSession({ type: 'interrupt' });
    default:
      return { kind: 'chat' };
  }
}
