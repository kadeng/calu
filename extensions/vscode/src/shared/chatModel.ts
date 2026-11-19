/**
 * Pure helpers of the chat webview (no DOM, no `vscode`): everything `webview/main.ts` decides
 * before it builds elements.
 *
 *     ViewState ──► sessionMenu()      entries of the session dropdown
 *               ──► sessionLabel()     text of the dropdown's button
 *               ──► tabList()          Main + one tab per subagent, with attention marks
 *               ──► setupCards()       what the empty / error state shows, one primary action each
 *               ──► usageMeter()       "42% · 84k" for the composer footer
 *               ──► otherOpenEscalations()  the strip above the input
 *               ──► heldTools() / heldByAgent()  steps calu holds back while a decision is open
 *               ──► foldTabs()     which tabs stay in the bar when there are many subagents
 *     texts     ──► middleTruncate(), shortDirectory(), splitPath(), previewLines(), toolHead(),
 *                   splitByFiles()
 *     links     ──► linkMessage()      which message a click on a Markdown link posts
 *               ──► buildFileIndex() / lookupFile()   paths in Claude's text that tool calls named
 *     images    ──► checkImage()       may this file be attached?
 */

import { sessionPhase, type SessionPhase } from './activity';
import { formatAge } from './sessionPick';
import { IMAGE_MEDIA_TYPES, MAX_IMAGE_BASE64, MAX_IMAGES, type ImageMediaType, type WebviewMessage } from './messages';
import { MAIN, isAgentLaunch, isAttached, own, sessionTitle, type IdMap, type SetupHint, type ToolView, type ViewState } from './viewState';

// ------------------------------------------------------------------ texts

/**
 * Shorten a text in the middle, so that its start and its end stay readable (file paths).
 *
 * @param text Any text.
 * @param max Maximum length of the result (values below 5 count as 5).
 * @returns `text` if it fits, else its start, "…" and its end; the end gets the larger half.
 *
 * @example middleTruncate("/home/me/project/src/module/file.ts", 20) === "/home/me/…le/file.ts"
 */
export function middleTruncate(text: string, max: number): string {
  const limit = Math.max(5, Math.floor(max));
  if (text.length <= limit) return text;
  const keep = limit - 1;
  const head = Math.floor(keep / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}

/**
 * Shorten a directory for a list: the home directory becomes "~", the rest is cut in the middle.
 *
 * @param directory An absolute path ("" allowed).
 * @param max Maximum length.
 * @returns The short form, e.g. "~/projects/…/calu".
 */
export function shortDirectory(directory: string, max = 40): string {
  const home = directory.replace(/^\/(?:home|Users)\/[^/]+(?=\/|$)/, '~');
  return middleTruncate(home, max);
}

/**
 * Split a path into its directory and its file name, so that a narrow row can cut the directory
 * and still show the name.
 *
 * @param path A path with "/" or "\\" separators.
 * @returns `dir` including the trailing separator ("" if the path has none) and `base`.
 *
 * @example splitPath("src/app/main.py") -> {dir: "src/app/", base: "main.py"}
 */
export function splitPath(path: string): { dir: string; base: string } {
  const at = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return { dir: path.slice(0, at + 1), base: path.slice(at + 1) };
}

/**
 * Put a text on one line.
 *
 * @param text Any text.
 * @param limit Maximum length.
 * @returns The text with runs of whitespace replaced by one space, cut at `limit` with "…".
 */
export function oneLine(text: string, limit = 160): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit)}…`;
}

/** A shortened text and whether lines were left out. */
export interface Preview {
  text: string;
  truncated: boolean;
  /** Number of lines of the whole text. */
  lines: number;
}

/**
 * Keep the first or the last `count` lines of a text.
 *
 * @param text Any text ("\n" line breaks; a trailing newline does not count as a line).
 * @param count Maximum number of lines to keep (at least 1).
 * @param from "head" keeps the first lines (finished texts), "tail" the last lines (while
 *   streaming, so the newest output stays visible).
 * @returns The kept lines, `truncated: true` if lines were left out, and the line count.
 *
 * @example previewLines("a\nb\nc", 2, "tail") -> {text: "b\nc", truncated: true, lines: 3}
 */
export function previewLines(text: string, count: number, from: 'head' | 'tail'): Preview {
  const lines = text.replace(/\n$/, '').split('\n');
  const n = Math.max(1, Math.floor(count));
  if (lines.length <= n) return { text, truncated: false, lines: lines.length };
  const kept = from === 'head' ? lines.slice(0, n) : lines.slice(-n);
  return { text: kept.join('\n'), truncated: true, lines: lines.length };
}

/**
 * Describe a tool input in one line: the command of Bash, the file of an edit or read, the
 * pattern of a search, the first question of `AskUserQuestion`, else the input as JSON.
 *
 * @param tool The tool view.
 * @returns `text` for display and `isPath` true if the text is a file path (it is then cut in
 *   the middle by the caller instead of at the end).
 */
export function toolHead(tool: Pick<ToolView, 'input' | 'diffPath'>): { text: string; isPath: boolean } {
  const input = tool.input ?? {};
  if (typeof input.command === 'string') return { text: oneLine(input.command, 300), isPath: false };
  for (const key of ['file_path', 'notebook_path', 'path']) {
    if (typeof input[key] === 'string') return { text: input[key] as string, isPath: true };
  }
  if (tool.diffPath) return { text: tool.diffPath, isPath: true };
  for (const key of ['description', 'pattern', 'query', 'url', 'skill', 'prompt']) {
    if (typeof input[key] === 'string') return { text: oneLine(input[key] as string, 300), isPath: false };
  }
  const first = Array.isArray(input.questions) ? (input.questions[0] as { question?: unknown } | null | undefined) : undefined;
  if (typeof first?.question === 'string') return { text: oneLine(first.question, 300), isPath: false };
  const json = JSON.stringify(input);
  return { text: json === '{}' ? '' : oneLine(json, 300), isPath: false };
}

/** A piece of a label: plain text, or a file link (`index` into `ToolView.files`). */
export type LabelPart = { text: string; file?: undefined } | { text: string; file: number };

/**
 * Split a label into text and file links: every occurrence of a file's `raw` text (e.g. a path
 * argument of a command) becomes a link part.
 *
 * @param text The label.
 * @param files The tool call's files (`ToolView.files`).
 * @returns The parts in order; their texts joined give `text`. At the same position the longer
 *   `raw` wins.
 */
export function splitByFiles(text: string, files: readonly { raw: string; path: string }[] | undefined): LabelPart[] {
  const sorted = (files ?? []).map((f, index) => ({ raw: f.raw, index })).filter((f) => f.raw !== '').sort((a, b) => b.raw.length - a.raw.length);
  const parts: LabelPart[] = [];
  let pos = 0;
  while (pos < text.length) {
    let best: { at: number; raw: string; index: number } | undefined;
    for (const f of sorted) {
      const at = text.indexOf(f.raw, pos);
      if (at >= 0 && (!best || at < best.at)) best = { at, raw: f.raw, index: f.index };
    }
    if (!best) break;
    if (best.at > pos) parts.push({ text: text.slice(pos, best.at) });
    parts.push({ text: best.raw, file: best.index });
    pos = best.at + best.raw.length;
  }
  if (pos < text.length) parts.push({ text: text.slice(pos) });
  return parts;
}

/**
 * Name the look of a checker decision chip.
 *
 * @param decision The `decision` of a `check.result` ("PASS", "ALLOW", "DENY", "ESCALATE"), or
 *   `undefined` while the step is not checked.
 * @returns "pass" (a rule let it through), "allow", "deny", "escalate" or "pending".
 */
export function decisionKind(decision: string | undefined): 'pass' | 'allow' | 'deny' | 'escalate' | 'pending' {
  const d = (decision ?? '').toUpperCase();
  if (d === 'PASS') return 'pass';
  if (d === 'ALLOW') return 'allow';
  if (d === 'DENY') return 'deny';
  if (d === 'ESCALATE') return 'escalate';
  return 'pending';
}

/**
 * Tell whether a tool row shows the "error" chip next to the checker's decision.
 *
 *     result is no error                         -> no
 *     AskUserQuestion                            -> no (its "error" result carries the user's answer)
 *     the checker denied the call (DENY)         -> no (the DENY chip says why the call did not run)
 *     every other failed call                    -> yes
 *
 * @param tool The tool view.
 * @returns True if the chip is shown.
 */
export function showsErrorChip(tool: Pick<ToolView, 'name' | 'check' | 'result'>): boolean {
  if (!tool.result?.isError) return false;
  if (tool.name === 'AskUserQuestion') return false;
  return decisionKind(tool.check?.decision) !== 'deny';
}

/**
 * Word the outcome of a turn for its separator line.
 *
 *     success                         -> "turn done"
 *     interrupted                     -> "turn interrupted"
 *     any other subtype, or an error  -> "turn ended: <subtype, underscores as spaces>"
 *
 * @param turn The turn item's `subtype` (of `turn.result`) and `isError`.
 * @returns The text.
 */
export function turnLabel(turn: { subtype: string; isError: boolean }): string {
  if (turn.subtype === 'interrupted') return 'turn interrupted';
  if (!turn.isError && (turn.subtype === 'success' || turn.subtype === '')) return 'turn done';
  return `turn ended: ${(turn.subtype || 'error').replace(/_/g, ' ')}`;
}

/**
 * Word the cost on a turn's separator line. Claude Code reports the cost of the whole session
 * at the end of every turn; the turn's own cost is the difference to the turn before.
 *
 *     total 0.0050, turn 0.0003  -> "$0.0003 · session $0.0050"
 *     total 0.0027, turn unknown -> "session $0.0027"
 *     total unknown              -> ""
 *
 * @param turn `costUsd`: the session's cost when the turn ended, or `null`; `costDelta`: the
 *   cost of this turn alone, or `null`/`undefined` if not known.
 * @returns The text, "" without a cost. Amounts have four decimals.
 */
export function turnCost(turn: { costUsd: number | null; costDelta?: number | null }): string {
  if (turn.costUsd === null) return '';
  const session = `session $${turn.costUsd.toFixed(4)}`;
  return typeof turn.costDelta === 'number' ? `$${turn.costDelta.toFixed(4)} · ${session}` : session;
}

/**
 * Word the session line of the usage popover.
 *
 *     (0.005, 2) -> "Cost $0.0050 (Claude Code, whole session) · last turn: 2 model calls"
 *     (null, 0)  -> "Cost unknown"
 *
 * The checkers' models are billed to their own keys and are not in this number.
 *
 * @param costUsd The session's cost as Claude Code reported it last, or `null`.
 * @param numTurns `num_turns` of the last `turn.result`: the model calls of that turn; 0 if no
 *   turn ended yet.
 * @returns The line.
 */
export function usageCostLine(costUsd: number | null, numTurns: number): string {
  const cost = costUsd !== null ? `Cost $${costUsd.toFixed(4)} (Claude Code, whole session)` : 'Cost unknown';
  return numTurns > 0 ? `${cost} · last turn: ${numTurns} model call${numTurns === 1 ? '' : 's'}` : cost;
}

// ------------------------------------------------------------------ session dropdown

/** One row of the session dropdown. */
export type SessionMenuEntry =
  | { kind: 'heading'; label: string }
  | {
      kind: 'session';
      id: string;
      title: string;
      /** Second line: the short directory, the start and, for rows of the same title, the id; `directoryFull` is the tooltip. */
      directory: string;
      directoryFull: string;
      /** Phase for the state dot. */
      phase: SessionPhase;
      state: string;
      openEscalations: number;
      stale: boolean;
      attached: boolean;
    }
  | { kind: 'note'; label: string }
  | { kind: 'action'; id: SessionMenuAction; label: string; enabled: boolean };

/** Actions at the bottom of the session dropdown. */
export type SessionMenuAction = 'new' | 'resume' | 'fork' | 'rename' | 'refresh' | 'detach';

/**
 * Build the rows of the session dropdown.
 *
 *     This workspace        (heading, if a session of the workspace runs)
 *       ● name              one row per running session, the attached one marked; the
 *         directory · started 5 min ago [· first 8 characters of the id, if another row has the same name]
 *     Other sessions        (heading, if sessions of other directories run)
 *       ...
 *     (note: "No session is running." / the list's error)
 *     New session… · Resume a stored conversation… · Fork conversation · Rename session… ·
 *     Refresh · Detach
 *
 * @param state The view state.
 * @param now The current time in milliseconds since the epoch (for "started … ago").
 * @returns The rows. "Fork conversation" needs an attached session with a conversation;
 *   "Rename session…" and "Detach" need an attached session.
 */
export function sessionMenu(state: ViewState, now: number = Date.now()): SessionMenuEntry[] {
  const entries: SessionMenuEntry[] = [];
  const attachedId = state.sessions.attachedId;
  // Unnamed sessions of one directory share their title; the start and the id tell them apart.
  const titles = new Map<string, number>();
  for (const s of state.sessions.list) titles.set(sessionTitle(s), (titles.get(sessionTitle(s)) ?? 0) + 1);
  const row = (s: ViewState['sessions']['list'][number]): SessionMenuEntry => {
    const started = s.startedAt ? Date.parse(s.startedAt) : NaN;
    const parts = [shortDirectory(s.directory, 36)];
    if (!Number.isNaN(started)) parts.push(`started ${formatAge((now - started) / 1000)}`);
    if ((titles.get(sessionTitle(s)) ?? 0) > 1) parts.push(s.id.slice(0, 8));
    return {
      kind: 'session',
      id: s.id,
      title: sessionTitle(s),
      directory: parts.join(' · '),
      directoryFull: s.directory,
      phase: sessionPhase(s.state),
      state: s.state,
      openEscalations: s.openEscalations,
      stale: s.stale,
      attached: s.id === attachedId,
    };
  };
  const here = state.sessions.list.filter((s) => s.here);
  const other = state.sessions.list.filter((s) => !s.here);
  if (here.length > 0) entries.push({ kind: 'heading', label: 'This workspace' }, ...here.map(row));
  if (other.length > 0) entries.push({ kind: 'heading', label: here.length > 0 ? 'Other sessions' : 'Running sessions' }, ...other.map(row));
  if (state.sessions.error) entries.push({ kind: 'note', label: `The sessions could not be listed: ${state.sessions.error}` });
  else if (state.sessions.list.length === 0) {
    entries.push({ kind: 'note', label: state.sessions.loaded ? 'No session is running.' : 'Loading the sessions…' });
  }
  const attached = attachedId !== null;
  entries.push(
    { kind: 'action', id: 'new', label: 'New session…', enabled: true },
    { kind: 'action', id: 'resume', label: 'Resume a stored conversation…', enabled: true },
    { kind: 'action', id: 'fork', label: 'Fork conversation', enabled: attached && state.session.claudeSessionId !== null },
    { kind: 'action', id: 'rename', label: 'Rename session…', enabled: attached },
    { kind: 'action', id: 'refresh', label: 'Refresh', enabled: true },
    { kind: 'action', id: 'detach', label: 'Detach', enabled: attached },
  );
  return entries;
}

/**
 * Return the webview message of a session menu action.
 *
 * @param action The action.
 * @param attachedId Id of the attached session, or `null`.
 * @returns The message, or `undefined` if the action needs an attached session and none is.
 */
export function sessionActionMessage(action: SessionMenuAction, attachedId: string | null): WebviewMessage | undefined {
  switch (action) {
    case 'new':
      return { type: 'newSession' };
    case 'resume':
      return { type: 'attach' };
    case 'fork':
      return attachedId ? { type: 'forkConversation' } : undefined;
    case 'rename':
      return attachedId ? { type: 'renameSession', sessionId: attachedId } : undefined;
    case 'refresh':
      return { type: 'refreshSessions' };
    case 'detach':
      return attachedId ? { type: 'detach' } : undefined;
  }
}

/**
 * Return the text and state of the session dropdown's button.
 *
 * @param state The view state.
 * @returns `label`: the attached session's name, else the last segment of its directory, else
 *   "No session"; `title`: the full directory and state for the tooltip; `dot`: the CSS
 *   modifier of the state dot ("off" while the bridge does not run, "detached", "lost",
 *   "reconnecting", or the session's phase).
 */
export function sessionLabel(state: ViewState): { label: string; title: string; dot: string } {
  if (state.bridge.status !== 'running') {
    return { label: 'calu', title: `The calu bridge is ${state.bridge.status}.`, dot: state.bridge.status === 'starting' ? 'starting' : 'off' };
  }
  const id = state.sessions.attachedId;
  if (!id || state.link.status === 'detached') return { label: 'No session', title: 'No session is attached. Choose or start one.', dot: 'detached' };
  const entry = state.sessions.list.find((s) => s.id === id);
  const label = sessionTitle(entry ?? { id, name: '', directory: state.session.workspace ?? '' });
  const where = entry?.directory || state.session.workspace || '';
  if (state.link.status !== 'attached') {
    return { label, title: `${where}\n${state.link.status}${state.link.message ? `: ${state.link.message}` : ''}`, dot: state.link.status };
  }
  return { label, title: `${where}\nSession ${id.slice(0, 8)}, ${state.session.state}`, dot: sessionPhase(state.session.state) };
}

// ------------------------------------------------------------------ tabs

/** One tab of the tab bar. */
export interface TabView {
  /** `MAIN` or the subagent's id. */
  key: string;
  label: string;
  /** "" for Main, else the subagent's status. */
  status: '' | 'running' | 'completed' | 'failed' | 'stopped';
  background: boolean;
  /** Number of open escalations in the tab. */
  openEscalations: number;
  /** Number of the tab's tool calls that calu holds back (`heldTools`). */
  held: number;
  /** True if a hidden tab has an open escalation or items the user has not seen. */
  attention: boolean;
}

/**
 * Build the tab bar: Main and one tab per subagent.
 *
 * @param state The view state.
 * @param active Key of the visible tab.
 * @param seen Per tab the id of the newest item the user has seen (missing = nothing).
 * @returns The tabs in display order. `attention` is never set for the active tab.
 */
export function tabList(state: ViewState, active: string, seen: Readonly<Record<string, number>>): TabView[] {
  const open: Record<string, number> = {};
  for (const e of Object.values(state.escalations)) {
    if (e.status === 'open') open[e.agentKey] = (Object.hasOwn(open, e.agentKey) ? open[e.agentKey] : 0) + 1;
  }
  const held = heldByAgent(state);
  const tab = (key: string, label: string, status: TabView['status'], background: boolean): TabView => {
    const list = own(state.items, key) ?? [];
    const newest = list.length > 0 ? list[list.length - 1].id : 0;
    const escalations = Object.hasOwn(open, key) ? open[key] : 0;
    const unseen = newest > (Object.hasOwn(seen, key) ? seen[key] : 0);
    return {
      key,
      label,
      status,
      background,
      openEscalations: escalations,
      held: Object.hasOwn(held, key) ? held[key] : 0,
      attention: key !== active && (escalations > 0 || unseen),
    };
  };
  return [
    tab(MAIN, 'Main', '', false),
    ...state.agents.map((a) => tab(a.agent_id, a.description || a.subagent_type || a.agent_id.slice(-6), a.status ?? 'running', a.background === true)),
  ];
}

/** Subagent tabs beyond which finished subagents fold into one menu (`foldTabs`). */
export const TAB_FOLD_LIMIT = 6;

/**
 * Decide which tabs the bar shows. With more than `limit` subagents the finished ones
 * (completed, failed, stopped) fold into one "N finished" menu, unless a tab is the visible
 * one, waits for a decision or has held steps; the rest keeps its order.
 *
 *     Main · Lint ● · Tests ⚠1 · Build held · … · [12 finished ▾]
 *
 * @param tabs The result of `tabList` (Main first).
 * @param active Key of the visible tab.
 * @param limit Number of subagent tabs that never fold (default `TAB_FOLD_LIMIT`).
 * @returns `shown`: the tabs of the bar, in order; `folded`: the finished tabs of the menu, in
 *   order (empty while nothing folds).
 *
 * @example foldTabs(tabsWith8Finished, MAIN).folded.length === 8
 */
export function foldTabs(tabs: readonly TabView[], active: string, limit = TAB_FOLD_LIMIT): { shown: TabView[]; folded: TabView[] } {
  const subagents = tabs.filter((t) => t.key !== MAIN).length;
  if (subagents <= limit) return { shown: [...tabs], folded: [] };
  const folds = (t: TabView): boolean =>
    t.key !== MAIN && t.key !== active && t.status !== 'running' && t.status !== '' && t.openEscalations === 0 && t.held === 0;
  return { shown: tabs.filter((t) => !folds(t)), folded: tabs.filter(folds) };
}

/**
 * Return the tool calls that calu holds back now: no step is approved while a decision about
 * another step is open (`calu.escalations.EscalationDesk.hold`). The same rule as the
 * full-screen terminal client (`calu.tui.model.SessionModel.held`):
 *
 *     a call the session named as held (step.held) and that has no result       -> held
 *     else, while an escalation that is no question is open:
 *       a call without check and result, not from the record, not itself waiting,
 *       no question of the agent, no launch whose subagent runs                  -> held
 *
 * The second rule covers sessions that do not send `step.held`; a question of the agent holds
 * nothing.
 *
 * @param state The view state.
 * @returns The `toolUseId`s of the held calls.
 */
export function heldTools(state: ViewState): Set<string> {
  const out = new Set<string>();
  const waiting = new Set<string>();
  let holding = false;
  for (const e of Object.values(state.escalations)) {
    if (e.status !== 'open') continue;
    if (e.toolUseId) waiting.add(e.toolUseId);
    if (e.questions.length === 0) holding = true;
  }
  const running = new Set(state.agents.filter((a) => (a.status ?? 'running') === 'running').map((a) => a.agent_id));
  for (const tool of Object.values(state.tools)) {
    if (tool.result || tool.stored || waiting.has(tool.toolUseId)) continue;
    if (own(state.held, tool.toolUseId)) {
      out.add(tool.toolUseId);
      continue;
    }
    if (!holding || tool.check) continue;
    if (tool.name === 'AskUserQuestion') continue;
    if (isAgentLaunch(tool.name) && running.has(tool.toolUseId)) continue;
    out.add(tool.toolUseId);
  }
  return out;
}

/**
 * Count the held tool calls per tab.
 *
 * @param state The view state.
 * @returns Per agent key (`MAIN` or a subagent's id) the number of its held calls; agents
 *   without a held call are left out.
 */
export function heldByAgent(state: ViewState): Record<string, number> {
  const out: Record<string, number> = Object.create(null) as Record<string, number>;
  for (const id of heldTools(state)) {
    const key = own(state.tools, id)?.agentKey ?? MAIN;
    out[key] = (Object.hasOwn(out, key) ? out[key] : 0) + 1;
  }
  return out;
}

/**
 * Return the id of the newest item per tab (what "seen" is set to when everything counts as read).
 *
 * @param items `ViewState.items`.
 * @returns Per tab key the id of its last item (tabs without items are left out).
 */
export function newestItemIds(items: IdMap<{ id: number }[]>): Record<string, number> {
  const out: Record<string, number> = Object.create(null) as Record<string, number>;
  for (const key of Object.keys(items)) {
    const list = items[key];
    if (list.length > 0) out[key] = list[list.length - 1].id;
  }
  return out;
}

/** An open escalation of a tab that is not visible, for the strip above the input. */
export interface StripEntry {
  key: string;
  tab: string;
  /** e.g. "Main: Bash" or "tests: Edit". */
  label: string;
}

/**
 * List the open escalations of the tabs that are not visible.
 *
 * @param state The view state.
 * @param active Key of the visible tab.
 * @returns One entry per open escalation outside `active`, oldest first.
 */
export function otherOpenEscalations(state: ViewState, active: string): StripEntry[] {
  const name = (tab: string): string =>
    tab === MAIN ? 'Main' : state.agents.find((a) => a.agent_id === tab)?.description || tab.slice(-6);
  return Object.values(state.escalations)
    .filter((e) => e.status === 'open' && e.agentKey !== active)
    .map((e) => ({ key: e.key, tab: e.agentKey, label: `${name(e.agentKey)}: ${e.questions.length > 0 ? 'question' : e.toolName}` }));
}

// ------------------------------------------------------------------ empty and error states

/** A button of a setup card: the message it posts. */
export interface CardAction {
  label: string;
  message: WebviewMessage;
}

/** One card of the empty / error state: one sentence, one primary action. */
export interface SetupCard {
  /** For `data-testid="hint-<id>"`. */
  id: string;
  level: 'info' | 'warning' | 'error';
  message: string;
  /** The one primary button; `undefined` if nothing can be done (e.g. while starting). */
  primary?: CardAction;
  secondary: CardAction[];
  /** True for the card that shows the server command box. */
  serverHelp: boolean;
  /** True for the card that lists the running sessions to pick from. */
  sessionList: boolean;
  /** True while the card stands for a request that runs (it shows a spinner). */
  busy: boolean;
  /**
   * True for a card that is shown as one line with a "Dismiss" button: a hint that matters
   * before the first session but not for the one that is attached and working.
   */
  compact: boolean;
  /** For a compact card: the few words shown in the line (`message` is the tooltip). */
  short?: string;
}

/** Id of the card of a session request that waits for the server (`ViewState.starting`). */
export const STARTING_CARD = 'starting-session';

/**
 * Build the card of a session request that waits for the server.
 *
 *     at once        "<title>… Asking the calu server for a session."          [Cancel]
 *     after ~3 s     "… No answer yet: maybe no calu server is running. ..."   [Start calu server] [Cancel]
 *
 * @param starting `ViewState.starting` (not `null`).
 * @returns The card; "Cancel" posts `cancelStart`, "Start calu server" posts `startServer`
 *   (the request keeps waiting, so a server started now still answers it).
 */
export function startingCard(starting: NonNullable<ViewState['starting']>): SetupCard {
  const cancel: CardAction = { label: 'Cancel', message: { type: 'cancelStart' } };
  const message = `${starting.title}… Asking the calu server for a session.`;
  const base = { id: STARTING_CARD, serverHelp: false, sessionList: false, busy: true, compact: false };
  if (!starting.slow) return { ...base, level: 'info', message, secondary: [cancel] };
  return {
    ...base,
    level: 'warning',
    message: `${message} No answer yet: maybe no calu server is running. You can start one now; this request keeps waiting for it.`,
    primary: { label: 'Start calu server', message: { type: 'startServer' } },
    secondary: [cancel],
  };
}

/**
 * Turn a hint's action into the message its button posts.
 *
 * @param hint The hint.
 * @returns The action, or `undefined` for a hint without a button (or "openLink" without `href`).
 */
export function hintAction(hint: SetupHint): CardAction | undefined {
  if (!hint.action) return undefined;
  if (hint.action === 'openLink') return hint.href ? { label: hint.actionLabel, message: { type: 'openLink', href: hint.href } } : undefined;
  return { label: hint.actionLabel, message: { type: hint.action } };
}

/**
 * Decide what the chat shows while it cannot work: one card per problem, most important first.
 *
 *     bridge "missing"                 -> Install calu
 *     bridge "error" / "exited"        -> Restart bridge (secondary: Show log)
 *     bridge "stopped"                 -> Start (restartBridge)
 *     bridge "starting"                -> a line without a button
 *     bridge "running", not attached   -> the host's hints; "no-server" shows the server command
 *                                         (Open terminal / Copy command), "no-session" lists the
 *                                         running sessions (secondary: Resume a stored conversation…)
 *     attached                         -> the hints that still apply (e.g. "claude-missing"),
 *                                         as one dismissible line each (`compact`)
 *     a session request waits          -> `startingCard` first; the "no-session" card is left out
 *
 * @param state The view state.
 * @param dismissed Ids of compact cards the user dismissed; they are left out.
 * @returns The cards; empty if nothing is missing.
 */
export function setupCards(state: ViewState, dismissed: readonly string[] = []): SetupCard[] {
  const card = (id: string, level: SetupCard['level'], message: string, primary?: CardAction, secondary: CardAction[] = []): SetupCard => ({
    id,
    level,
    message,
    ...(primary ? { primary } : {}),
    secondary,
    serverHelp: false,
    sessionList: false,
    busy: false,
    compact: false,
  });
  const bridge = state.bridge;
  const fromHint = (hint: SetupHint): SetupCard => {
    const c = card(hint.id, hint.level, hint.message, hintAction(hint));
    if (hint.id === 'no-server') {
      c.serverHelp = state.serverHelp !== null;
      c.primary = { label: 'Open terminal with this command', message: { type: 'startServer' } };
      c.secondary = [{ label: 'Copy command', message: { type: 'copyServerCommand' } }];
    }
    if (hint.id === 'no-session') {
      c.sessionList = true;
      c.secondary = [{ label: 'Resume a stored conversation…', message: { type: 'attach' } }];
    }
    return c;
  };
  if (bridge.status === 'missing') {
    const hint = state.hints.find((h) => h.id === 'calu-missing');
    return [
      hint
        ? fromHint(hint)
        : card('calu-missing', 'error', bridge.message || 'calu is not installed on this machine.', { label: 'Install calu', message: { type: 'install' } }),
    ];
  }
  if (bridge.status === 'error' || bridge.status === 'exited') {
    return [
      card('bridge', 'error', bridge.message || 'The calu bridge stopped unexpectedly.', { label: 'Restart bridge', message: { type: 'restartBridge' } }, [
        { label: 'Show log', message: { type: 'showLog' } },
      ]),
    ];
  }
  if (bridge.status === 'stopped') {
    return [card('bridge', 'info', bridge.message || 'The calu bridge is not running.', { label: 'Start', message: { type: 'restartBridge' } })];
  }
  if (bridge.status === 'starting') return [card('starting', 'info', 'Starting calu…')];
  const attached = isAttached(state);
  let cards = state.hints.map(fromHint);
  if (attached) {
    cards = state.hints
      .filter((h) => !dismissed.includes(h.id))
      .map((h) => {
        const c = fromHint(h);
        const primary = c.primary && h.actionShort ? { ...c.primary, label: h.actionShort } : c.primary;
        return { ...c, ...(primary ? { primary } : {}), compact: true, ...(h.short ? { short: h.short } : {}) };
      });
  }
  if (!attached && state.link.status === 'lost') {
    cards.unshift(card('link-lost', 'warning', `The connection to the session was lost${state.link.message ? `: ${state.link.message}` : '.'}`));
  }
  if (state.starting) cards = [startingCard(state.starting), ...cards.filter((c) => c.id !== 'no-session')];
  return cards;
}

// ------------------------------------------------------------------ usage

/**
 * Format a token count.
 *
 * @param tokens A non-negative number.
 * @returns "950", "84k", "1.2M" (one decimal below 10 of a unit).
 */
export function formatTokens(tokens: number): string {
  const n = Math.max(0, Math.round(tokens));
  if (n < 1000) return String(n);
  const [value, unit] = n < 1_000_000 ? [n / 1000, 'k'] : [n / 1_000_000, 'M'];
  return `${value < 10 ? value.toFixed(1).replace(/\.0$/, '') : Math.round(value)}${unit}`;
}

/** What the context meter of the composer footer shows. */
export interface UsageMeter {
  /** 0..100, or `null` while the context is unknown. */
  percent: number | null;
  /** e.g. "42% · 84k"; "ctx –" while unknown. */
  label: string;
  /** Tooltip, e.g. "Context: 84,000 of 200,000 tokens (42%). Click for limits and cost." */
  title: string;
  /** "ok" below 70 %, "warn" below 90 %, else "high"; "unknown" without data. */
  level: 'unknown' | 'ok' | 'warn' | 'high';
}

/**
 * Summarise the context usage for the composer footer.
 *
 * @param usage `ViewState.usage`.
 * @param working True while Claude works on a turn: the number was read at the last turn end
 *   or at Claude Code's last usage report, and the tooltip says so.
 * @returns The meter (see `UsageMeter`).
 */
export function usageMeter(usage: ViewState['usage'], working = false): UsageMeter {
  const c = usage.context;
  if (!c) return { percent: null, label: 'ctx –', title: 'Context usage is not known yet. Click to ask.', level: 'unknown' };
  const percent = Math.max(0, Math.min(100, Math.round(c.percent)));
  const asOf = working ? ' As of the last turn or usage report; it is read again when the turn ends.' : '';
  return {
    percent,
    label: `${percent}% · ${formatTokens(c.tokens)}`,
    title: `Context: ${c.tokens.toLocaleString('en-US')} of ${c.maxTokens.toLocaleString('en-US')} tokens (${percent}%).${asOf} Click for limits and cost.`,
    level: percent < 70 ? 'ok' : percent < 90 ? 'warn' : 'high',
  };
}

/**
 * Describe one account limit for the usage popover.
 *
 * @param limit A rate limit of `ViewState.usage.rateLimits`.
 * @returns e.g. "five_hour: 63% used, allowed, resets 14:30"; parts that are unknown are left out.
 */
export function rateLimitLine(limit: { type?: string; status?: string; utilization?: number | null; resets_at?: string | null }): string {
  const parts: string[] = [];
  if (typeof limit.utilization === 'number') {
    const u = limit.utilization <= 1 ? limit.utilization * 100 : limit.utilization;
    parts.push(`${Math.round(u)}% used`);
  }
  if (limit.status) parts.push(limit.status.replace(/_/g, ' '));
  if (limit.resets_at) {
    const at = new Date(limit.resets_at);
    if (!Number.isNaN(at.getTime())) parts.push(`resets ${at.toISOString().slice(0, 16).replace('T', ' ')} UTC`);
  }
  return `${(limit.type || 'limit').replace(/_/g, ' ')}: ${parts.join(', ') || 'no data'}`;
}

// ------------------------------------------------------------------ links and files

/**
 * Decide which message a click on a link of rendered Markdown posts. Links never navigate.
 *
 * @param href The link's `href` attribute (untrusted).
 * @returns `openLink` for http(s) and relative links (the host opens a browser or a workspace
 *   file); `undefined` for an empty link, a fragment ("#…") and every other scheme.
 */
export function linkMessage(href: string | null | undefined): WebviewMessage | undefined {
  const target = (href ?? '').trim();
  if (!target || target.startsWith('#')) return undefined;
  if (/^https?:\/\//i.test(target)) return { type: 'openLink', href: target };
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('//')) return undefined;
  return { type: 'openLink', href: target };
}

/** Lookup of the files that tool calls named: by absolute path, by `raw` text and by base name. */
export type FileIndex = Map<string, string | null>;

/**
 * Index the files that the tool calls of the state named (`tool.files`), so that a path in
 * Claude's text can be turned into a link.
 *
 * @param tools `ViewState.tools`.
 * @returns A map from the absolute path, the `raw` text and the base name to the absolute
 *   path; a base name or raw text that stands for several files maps to `null` (ambiguous).
 */
export function buildFileIndex(tools: IdMap<ToolView>): FileIndex {
  const index: FileIndex = new Map();
  const add = (key: string, path: string): void => {
    if (!key) return;
    const old = index.get(key);
    index.set(key, old === undefined || old === path ? path : null);
  };
  for (const tool of Object.values(tools)) {
    for (const f of tool.files ?? []) {
      add(f.path, f.path);
      add(f.raw, f.path);
      add(f.path.split(/[\\/]/).pop() ?? '', f.path);
    }
  }
  return index;
}

/**
 * Look up a text that may name a file, with an optional ":line" suffix.
 *
 * @param index The index of `buildFileIndex`.
 * @param text e.g. "src/app.py", "app.py:42" or "/work/src/app.py:42:7".
 * @returns The absolute path and the 1-based line (if given), or `undefined` if the text names
 *   no known file or an ambiguous one.
 */
export function lookupFile(index: FileIndex, text: string): { path: string; line?: number } | undefined {
  const m = /^(.+?)(?::(\d+))?(?::\d+)?$/.exec(text.trim());
  if (!m) return undefined;
  const path = index.get(m[1]) ?? index.get(m[1].replace(/^\.\//, ''));
  if (!path) return undefined;
  const line = m[2] ? Number(m[2]) : undefined;
  return line !== undefined && line >= 1 ? { path, line } : { path };
}

// ------------------------------------------------------------------ images

/**
 * Check whether an image may be attached to the message being written.
 *
 * @param mediaType The file's MIME type.
 * @param bytes The file's size in bytes.
 * @param attached Number of images already attached.
 * @param toMain True if the message goes to the main agent.
 * @returns `undefined` if the image may be attached, else the reason as a sentence.
 */
export function checkImage(mediaType: string, bytes: number, attached: number, toMain: boolean): string | undefined {
  if (!toMain) return 'Images can only be sent to the main agent.';
  if (!isImageType(mediaType)) return 'Only PNG, JPEG, GIF and WebP images can be attached.';
  if (attached >= MAX_IMAGES) return `At most ${MAX_IMAGES} images fit into one message.`;
  // base64 needs 4 characters per 3 bytes.
  if (Math.ceil(bytes / 3) * 4 > MAX_IMAGE_BASE64) {
    return `The image is too large: ${(bytes / (1024 * 1024)).toFixed(1)} MB; a message takes images of at most ${Math.floor((MAX_IMAGE_BASE64 / 4) * 3 / (1024 * 1024))} MB. Nothing was attached.`;
  }
  return undefined;
}

/**
 * Tell whether a MIME type is one of the raster formats that may be attached or shown.
 *
 * @param mediaType Any string.
 * @returns True for PNG, JPEG, GIF and WebP.
 */
export function isImageType(mediaType: string): mediaType is ImageMediaType {
  return (IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType);
}

/**
 * Split a `data:` URL of an image into its parts.
 *
 * @param dataUrl e.g. "data:image/png;base64,iVBOR…" (as `FileReader.readAsDataURL` returns it).
 * @returns The media type and the base64 text, or `undefined` if the URL is not a base64 data
 *   URL of an allowed image type.
 */
export function parseImageDataUrl(dataUrl: string): { mediaType: ImageMediaType; data: string } | undefined {
  const m = /^data:([a-z0-9.+/-]+);base64,([A-Za-z0-9+/]+={0,2})$/i.exec(dataUrl);
  if (!m || !isImageType(m[1].toLowerCase())) return undefined;
  return { mediaType: m[1].toLowerCase() as ImageMediaType, data: m[2] };
}

/**
 * Build the `src` of an image of the transcript.
 *
 * @param mediaType The image's MIME type (from calu, untrusted).
 * @param data The image file, base64.
 * @returns A `data:` URL, or `undefined` if the type is not an allowed raster format or the
 *   data is not base64 (the caller then shows a placeholder).
 */
export function imageSrc(mediaType: string, data: string): string | undefined {
  if (!isImageType(mediaType) || !data || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) return undefined;
  return `data:${mediaType};base64,${data}`;
}
