/**
 * The activity line of the chat view (pure, no DOM): what Claude is doing right now.
 *
 * The line has three columns at fixed offsets (CSS grid in `media/chat.css`), so that changing
 * spinner glyphs, verbs and digits never move the other parts:
 *
 *     | icon | label (LABEL_COLUMN_CH wide) | elapsed |
 *     | ✻    | Combobulating…               | 12s     |   busy (spinner and verb change)
 *     | ⏸    | Waiting for you…             | 40s     |   waiting (an approval is open)
 *     | ✓    | Done                         | 52s     |   idle after a turn
 *     |      | Idle / Starting… / Stopped / Not connected |
 */

/** Spinner frames, played forward and back (like Claude Code's terminal UI). */
export const SPINNER_FRAMES: readonly string[] = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢'];

/** Verbs shown while Claude works; one is picked per few seconds. */
export const ACTIVITY_VERBS: readonly string[] = [
  'Combobulating', 'Pondering', 'Cogitating', 'Ruminating', 'Percolating', 'Noodling',
  'Mulling', 'Deliberating', 'Brewing', 'Tinkering', 'Wrangling', 'Untangling', 'Musing',
  'Synthesizing', 'Concocting', 'Crunching', 'Simmering', 'Puzzling', 'Contemplating',
  'Reticulating', 'Spelunking', 'Marinating', 'Finagling', 'Discombobulating',
];

/** Label shown while a checker judges a step. */
export const CHECKING_LABEL = 'Checking…';

/** Coarse phase of a session, derived from its protocol state. */
export type SessionPhase = 'starting' | 'idle' | 'busy' | 'waiting' | 'stopped' | 'failed';

/**
 * Map a protocol session state to its phase.
 *
 * @param state The `state` of `session.state` or of the snapshot.
 * @returns "busy" for "running", "checking" and "tool_call"; "waiting" for "escalated"; the
 *   state itself for "starting", "idle", "stopped" and "failed"; "idle" for an unknown value.
 */
export function sessionPhase(state: string): SessionPhase {
  if (state === 'running' || state === 'checking' || state === 'tool_call') return 'busy';
  if (state === 'escalated') return 'waiting';
  if (state === 'starting' || state === 'stopped' || state === 'failed') return state;
  return 'idle';
}

/** Label shown while an approval is open. */
export const WAITING_LABEL = 'Waiting for you…';

/**
 * Width of the label column in characters of the (monospace) editor font: the longest busy or
 * waiting label plus one character of space before the timer. `media/chat.css` uses the same
 * number (`grid-template-columns`).
 */
export const LABEL_COLUMN_CH = 18;

/** Milliseconds between two spinner frames. */
export const FRAME_MS = 120;
/** Milliseconds a verb stays before the next one. */
export const VERB_MS = 4000;

/** What the activity line shows. */
export interface Activity {
  /** First column: spinner frame, "⏸", "✓" or "". */
  icon: string;
  /** Second column, e.g. "Combobulating…". */
  label: string;
  /** Third column: elapsed time ("12s") or "". */
  elapsed: string;
  /** All parts as one text, e.g. "✻ Combobulating… 12s" (for screen readers and tests). */
  text: string;
  /** True while Claude works or waits (the line animates and the timer keeps running). */
  active: boolean;
  /** CSS modifier: "busy", "waiting", "done" or "idle". */
  kind: 'busy' | 'waiting' | 'done' | 'idle';
}

/**
 * Format a duration for the activity line.
 *
 * @param ms Milliseconds (negative values count as 0).
 * @returns "12s" below a minute, else "3m 05s".
 */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

/**
 * Build an `Activity` from its columns.
 *
 * @param icon First column.
 * @param label Second column.
 * @param elapsed Third column.
 * @param active Whether the line animates.
 * @param kind CSS modifier.
 * @returns The activity, with `text` joining the non-empty columns by spaces.
 */
function activity(icon: string, label: string, elapsed: string, active: boolean, kind: Activity['kind']): Activity {
  const text = [icon, label, elapsed].filter((part) => part !== '').join(' ');
  return { icon, label, elapsed, text, active, kind };
}

/**
 * Compute the activity line.
 *
 * @param connected True if a session is attached.
 * @param state The session state from `session.state` ("starting", "idle", "running",
 *   "checking", "tool_call", "escalated", "stopped", "failed").
 * @param since Time (ms) the current busy/waiting period began, or the last one began (idle).
 * @param lastDurationMs Length of the last finished busy period, or undefined before the first.
 * @param now Current time (ms).
 * @param seed A number picked at the start of the period; selects the first verb.
 * @returns The columns, whether the line animates, and its kind.
 */
export function activityLine(
  connected: boolean,
  state: string,
  since: number,
  lastDurationMs: number | undefined,
  now: number,
  seed: number,
): Activity {
  if (!connected) return activity('', 'Not connected', '', false, 'idle');
  const elapsed = now - since;
  const phase = sessionPhase(state);
  if (phase === 'busy') {
    if (state === 'checking') {
      const frame = SPINNER_FRAMES[Math.floor(elapsed / FRAME_MS) % SPINNER_FRAMES.length];
      return activity(frame, CHECKING_LABEL, formatElapsed(elapsed), true, 'busy');
    }
    const frame = SPINNER_FRAMES[Math.floor(elapsed / FRAME_MS) % SPINNER_FRAMES.length];
    const verb = ACTIVITY_VERBS[(seed + Math.floor(elapsed / VERB_MS)) % ACTIVITY_VERBS.length];
    return activity(frame, `${verb}…`, formatElapsed(elapsed), true, 'busy');
  }
  if (phase === 'waiting') return activity('⏸', WAITING_LABEL, formatElapsed(elapsed), true, 'waiting');
  if (phase === 'starting') return activity('', 'Starting…', '', false, 'idle');
  if (phase === 'failed') return activity('', 'Failed', '', false, 'idle');
  if (phase === 'stopped') return activity('', 'Stopped', '', false, 'idle');
  if (lastDurationMs !== undefined) return activity('✓', 'Done', formatElapsed(lastDurationMs), false, 'done');
  return activity('', 'Idle', '', false, 'idle');
}
