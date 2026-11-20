/**
 * The usage panel that a click on the context chip opens (`usage.panel`, PLAN D248): what it
 * says, as sections of lines. The PyCharm plugin has the same rules in
 * `extensions/jetbrains/shared/.../logic/UsagePanel.kt`; the webview only draws the sections.
 *
 *     Subscription limits   5-hour and weekly always (each with when Claude Code reported it,
 *                           or "not reported yet by Claude Code"), then any other reported limit
 *     Your plan now         the plan's limits Claude Code read at the click (`get_usage`), or why not
 *     Claude Code's /usage  its text, read without a turn
 *     This session          cost, model calls, context window
 *
 * No `vscode` import: unit-tested with vitest.
 */

/** One usage limit as the panel uses it (the protocol's `RateLimit`, read defensively). */
export interface PanelLimit {
  type: string;
  status: string;
  /** Used share, 0..1 (or 0..100 from `get_usage`); `null` if unknown. */
  utilization: number | null;
  resetsAt: string | null;
  /** When Claude Code reported the figure; `null` if unknown. */
  reportedAt: string | null;
}

/**
 * The panel's request and answer, kept in the view state (`ViewState.usagePanel`).
 *
 * `phase` "asking" while the request runs, "ready" with the answer, "failed" with `error`.
 */
export interface UsagePanelView {
  phase: 'asking' | 'ready' | 'failed';
  error: string | null;
  limits: PanelLimit[];
  accountLimits: PanelLimit[];
  accountLimitsProblem: string;
  subscription: string;
  claudeUsage: string;
  claudeUsageProblem: string;
  costUsd: number | null;
  numTurns: number;
  context: { tokens: number; maxTokens: number; percent: number; model: string } | null;
}

/** One line of the panel; `tone` "muted" for missing or secondary text, "problem" for a limit that is not "allowed". */
export interface UsagePanelLine {
  text: string;
  tone: 'plain' | 'muted' | 'problem';
}

/** One section: heading, a short note ("" for none), lines and an optional monospaced block. */
export interface UsagePanelSection {
  title: string;
  note: string;
  lines: UsagePanelLine[];
  preformatted: string | null;
}

/** The two limits every subscription has; the panel always names them. */
export const MAIN_LIMIT_TYPES = ['five_hour', 'seven_day'] as const;

/**
 * Name a limit type for people.
 *
 * @param type The protocol's type, e.g. "five_hour".
 * @returns "5-hour", "weekly", "weekly Opus", "weekly Sonnet", "extra usage", otherwise the
 *   type with spaces for underscores ("limit" for "").
 */
export function limitName(type: string): string {
  const names: Record<string, string> = {
    five_hour: '5-hour',
    seven_day: 'weekly',
    seven_day_opus: 'weekly Opus',
    seven_day_sonnet: 'weekly Sonnet',
    overage: 'extra usage',
  };
  if (Object.prototype.hasOwnProperty.call(names, type)) return names[type];
  return type ? type.replace(/_/g, ' ') : 'limit';
}

/**
 * Format an age.
 *
 * @param seconds Seconds (negative values count as 0).
 * @returns "just now" below a minute, else e.g. "5 min ago", "3 h ago", "2 d ago".
 */
export function ageText(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/**
 * Word the time until a reset.
 *
 * @param seconds Seconds from now until the reset.
 * @returns "resets in 2 h 10 min", "resets in 3 d 4 h", "resets in under a minute"; "reset time
 *   passed" for a time in the past.
 */
export function resetText(seconds: number): string {
  if (seconds < 0) return 'reset time passed';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `resets in ${d} d ${h} h`;
  if (h > 0) return `resets in ${h} h ${m} min`;
  if (m > 0) return `resets in ${m} min`;
  return 'resets in under a minute';
}

/**
 * Read an ISO time.
 *
 * @param text The time or `null`.
 * @returns Milliseconds since the epoch, or `null` for none or an unreadable text.
 */
function millis(text: string | null): number | null {
  if (!text) return null;
  const t = Date.parse(text);
  return Number.isNaN(t) ? null : t;
}

/**
 * Describe one limit.
 *
 * @param limit The limit.
 * @param now Milliseconds since the epoch to measure ages and resets from.
 * @param stamped True to add when Claude Code reported it ("reported 3 min ago").
 * @returns The line; tone "problem" when the status is set and is not "allowed".
 */
export function usageLimitLine(limit: PanelLimit, now: number, stamped: boolean): UsagePanelLine {
  const parts: string[] = [];
  const bad = limit.status !== '' && limit.status !== 'allowed';
  if (limit.utilization !== null) parts.push(`${Math.round(limit.utilization <= 1 ? limit.utilization * 100 : limit.utilization)}% used`);
  if (bad) parts.push(limit.status.replace(/_/g, ' '));
  const resets = millis(limit.resetsAt);
  if (resets !== null) parts.push(resetText(Math.floor((resets - now) / 1000)));
  if (parts.length === 0) parts.push(limit.status === 'allowed' ? 'allowed' : 'no figures');
  let text = `${limitName(limit.type)}: ${parts.join(', ')}`;
  if (stamped) {
    const at = millis(limit.reportedAt);
    text += at !== null ? ` · reported ${ageText((now - at) / 1000)}` : ' · time of the report not known';
  }
  return { text, tone: bad ? 'problem' : 'plain' };
}

/**
 * Build the sections of the usage panel. No side effects.
 *
 * @param panel The panel's state; its `phase` should be "ready" (for "asking" and "failed" the
 *   webview shows one line of its own).
 * @param now Milliseconds since the epoch to measure ages and resets from.
 * @returns "Subscription limits", "Your plan now" (only when the plan's limits or a reason why
 *   they are missing were given), "Claude Code's /usage", "This session".
 *
 * Example: no limit reported yet gives the lines "5-hour: not reported yet by Claude Code" and
 * "weekly: not reported yet by Claude Code".
 */
export function usagePanelSections(panel: UsagePanelView, now: number): UsagePanelSection[] {
  const out: UsagePanelSection[] = [];
  const main: readonly string[] = MAIN_LIMIT_TYPES;
  const limitLines: UsagePanelLine[] = main.map((type) => {
    const limit = panel.limits.find((l) => l.type === type);
    return limit ? usageLimitLine(limit, now, true) : { text: `${limitName(type)}: not reported yet by Claude Code`, tone: 'muted' };
  });
  for (const limit of panel.limits) if (!main.includes(limit.type)) limitLines.push(usageLimitLine(limit, now, true));
  out.push({ title: 'Subscription limits', note: 'as Claude Code reported them', lines: limitLines, preformatted: null });
  if (panel.accountLimits.length > 0) {
    out.push({
      title: `Your plan now${panel.subscription ? ` (${panel.subscription})` : ''}`,
      note: 'read from Claude Code at this click',
      lines: panel.accountLimits.map((l) => usageLimitLine(l, now, false)),
      preformatted: null,
    });
  } else if (panel.accountLimitsProblem) {
    out.push({ title: 'Your plan now', note: '', lines: [{ text: panel.accountLimitsProblem, tone: 'muted' }], preformatted: null });
  }
  out.push(
    panel.claudeUsage
      ? { title: "Claude Code's /usage", note: 'read without a turn; not recorded, not shown to the checkers', lines: [], preformatted: panel.claudeUsage }
      : { title: "Claude Code's /usage", note: '', lines: [{ text: panel.claudeUsageProblem || 'not available', tone: 'muted' }], preformatted: null },
  );
  const session: UsagePanelLine[] = [];
  if (panel.costUsd === null) session.push({ text: 'cost: no turn has ended yet', tone: 'muted' });
  else {
    const calls = panel.numTurns > 0 ? ` · last turn: ${panel.numTurns} model call${panel.numTurns === 1 ? '' : 's'}` : '';
    session.push({ text: `cost $${panel.costUsd.toFixed(4)} (Claude Code, whole session)${calls}`, tone: 'plain' });
  }
  const c = panel.context;
  if (c) {
    session.push({
      text: `context ${c.tokens.toLocaleString('en-US')} of ${c.maxTokens.toLocaleString('en-US')} tokens (${Math.round(c.percent)}%)${c.model ? ` · ${c.model}` : ''}`,
      tone: 'plain',
    });
  }
  out.push({ title: 'This session', note: '', lines: session, preformatted: null });
  return out;
}

/**
 * Read limits defensively.
 *
 * @param raw Any JSON value (a `RateLimit[]` of the protocol).
 * @returns One entry per object; missing members become "" / `null`. Empty for a non-array.
 */
export function parsePanelLimits(raw: unknown): PanelLimit[] {
  if (!Array.isArray(raw)) return [];
  const str = (v: unknown): string => (typeof v === 'string' ? v : '');
  return raw
    .filter((r): r is Record<string, unknown> => Boolean(r) && typeof r === 'object')
    .map((r) => ({
      type: str(r.type),
      status: str(r.status),
      utilization: typeof r.utilization === 'number' ? r.utilization : null,
      resetsAt: str(r.resets_at) || null,
      reportedAt: str(r.reported_at) || null,
    }));
}

/**
 * Read the result of `usage.panel` defensively.
 *
 * @param raw The result (any JSON value).
 * @returns The panel in phase "ready"; unknown members are ignored, missing ones are empty.
 */
export function parseUsagePanel(raw: unknown): UsagePanelView {
  const r = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const str = (v: unknown): string => (typeof v === 'string' ? v : '');
  const c = r.context && typeof r.context === 'object' ? (r.context as Record<string, unknown>) : null;
  const context =
    c && typeof c.tokens === 'number' && typeof c.max_tokens === 'number'
      ? {
          tokens: c.tokens,
          maxTokens: c.max_tokens,
          percent: typeof c.percent === 'number' ? c.percent : c.max_tokens > 0 ? (100 * c.tokens) / c.max_tokens : 0,
          model: str(c.model),
        }
      : null;
  return {
    phase: 'ready',
    error: null,
    limits: parsePanelLimits(r.limits),
    accountLimits: parsePanelLimits(r.account_limits),
    accountLimitsProblem: str(r.account_limits_problem),
    subscription: str(r.subscription),
    claudeUsage: str(r.claude_usage),
    claudeUsageProblem: str(r.claude_usage_problem),
    costUsd: typeof r.cost_usd === 'number' ? r.cost_usd : null,
    numTurns: typeof r.num_turns === 'number' ? r.num_turns : 0,
    context,
  };
}

/**
 * A panel in phase "asking" or "failed".
 *
 * @param phase "asking" or "failed".
 * @param error With "failed": why the session did not answer.
 * @returns The panel without figures.
 */
export function emptyUsagePanel(phase: 'asking' | 'failed', error: string | null = null): UsagePanelView {
  return {
    phase,
    error,
    limits: [],
    accountLimits: [],
    accountLimitsProblem: '',
    subscription: '',
    claudeUsage: '',
    claudeUsageProblem: '',
    costUsd: null,
    numTurns: 0,
    context: null,
  };
}
