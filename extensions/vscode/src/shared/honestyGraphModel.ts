/**
 * The honesty graph as the chat view shows it, without the drawing (no DOM, no `vscode`).
 *
 *     honesty.graph result ──readGraph()──► GraphData ──settle()──► ghosts (settled nodes)
 *                                               │
 *                         graphNotice()  what to show instead of a graph (off, clean, error)
 *                         nodeMenu()     the entries of a node's context menu and their messages
 *                         GraphRefresh   when to ask the session for the graph again
 *
 * `webview/honestyGraph.ts` draws what these functions return.
 */

import type { WebviewMessage } from './messages';

/** The states a node of `honesty.graph` can have, strongest first. */
export const GRAPH_STATES = ['dishonest', 'recheck', 'changed', 'caller', 'honest', 'related'] as const;
/** A state of a node of `honesty.graph`. */
export type GraphState = (typeof GRAPH_STATES)[number];
/** A state as the view shows it: "settled" is a node that just left the graph. */
export type ShownState = GraphState | 'settled';

/** The word, the glyph and the meaning of each state. The glyph tells the state without colour. */
export const STATE_TEXTS: Record<ShownState, { word: string; glyph: string; meaning: string }> = {
  dishonest: { word: 'dishonest', glyph: '✕', meaning: 'A checker, a review or a person marked it: it does not do what its name and documentation say.' },
  recheck: { word: 'to judge again', glyph: '?', meaning: 'A unit it uses changed its signature or documentation.' },
  changed: { word: 'changed', glyph: 'Δ', meaning: 'It changed and nobody verified it yet.' },
  caller: { word: 'caller', glyph: '↑', meaning: 'It uses a dishonest unit and is itself unchanged. This blocks nothing.' },
  honest: { word: 'honest', glyph: '✓', meaning: 'It is shown because a unit to judge again uses it.' },
  related: { word: 'related', glyph: '·', meaning: 'It is unchanged. It uses a changed unit, or a changed or dishonest unit uses it. This blocks nothing.' },
  settled: { word: 'settled', glyph: '✓', meaning: 'It was just settled and leaves the graph.' },
};

/** A node of the graph, every field set. */
export interface GraphNode {
  /** `path:dotted.name`, or the path alone for a whole file. */
  id: string;
  /** The file, relative to the workspace. */
  path: string;
  /** The line of the definition, 1-based. */
  line: number;
  /** The short name. */
  name: string;
  kind: 'unit' | 'file';
  state: ShownState;
  /** The text for the hover. */
  detail: string;
  /** True while a running review reads the unit's file. */
  reviewing: boolean;
  /** The open marks on the unit. */
  markIds: number[];
  /** The unit's entries of code to judge again. */
  recheckIds: number[];
  /** How many units the node stands for. */
  count: number;
  /** How many unchanged callers of the unit the graph leaves out. */
  hiddenCallers: number;
}

/** An edge of the graph: `source` uses `target`. */
export interface GraphEdge {
  source: string;
  target: string;
  /** False for a link that nothing confirmed. */
  certain: boolean;
}

/** A result of `honesty.graph`, every field set. */
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** True if the graph has one node per file. */
  byFile: boolean;
  /** True if every file counts as unreviewed (no trusted commit). */
  everything: boolean;
  /** True if callers or used units are missing (the search ran out of time, or the graph is full). */
  partial: boolean;
  /** `honesty.mode` of the session. */
  mode: string;
  reviewRunning: boolean;
}

/**
 * Keep the integers of a list.
 *
 * @param raw Any value.
 * @returns The integers of `raw` if it is an array, else an empty list.
 */
function integers(raw: unknown): number[] {
  return Array.isArray(raw) ? raw.filter((v): v is number => Number.isInteger(v)) : [];
}

/**
 * Read a result of `honesty.graph` defensively.
 *
 * @param raw The result as the session sent it (data from calu).
 * @returns The graph with defaults for missing members. A node without an id or with an
 *   unknown state is left out, and so is an edge without two ends.
 */
export function readGraph(raw: unknown): GraphData {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const nodes: GraphNode[] = [];
  for (const entry of Array.isArray(r.nodes) ? r.nodes : []) {
    if (!entry || typeof entry !== 'object') continue;
    const n = entry as Record<string, unknown>;
    const state = GRAPH_STATES.find((s) => s === n.state);
    if (typeof n.id !== 'string' || !n.id || !state) continue;
    nodes.push({
      id: n.id,
      path: typeof n.path === 'string' ? n.path : '',
      line: typeof n.line === 'number' && Number.isInteger(n.line) && n.line >= 1 ? n.line : 1,
      name: typeof n.name === 'string' && n.name ? n.name : n.id,
      kind: n.kind === 'file' ? 'file' : 'unit',
      state,
      detail: typeof n.detail === 'string' ? n.detail : '',
      reviewing: n.reviewing === true,
      markIds: integers(n.mark_ids),
      recheckIds: integers(n.recheck_ids),
      count: typeof n.count === 'number' && Number.isInteger(n.count) && n.count >= 1 ? n.count : 1,
      hiddenCallers: typeof n.hidden_callers === 'number' && Number.isInteger(n.hidden_callers) && n.hidden_callers > 0 ? n.hidden_callers : 0,
    });
  }
  const edges: GraphEdge[] = [];
  for (const entry of Array.isArray(r.edges) ? r.edges : []) {
    if (!entry || typeof entry !== 'object') continue;
    const e = entry as Record<string, unknown>;
    if (typeof e.source !== 'string' || typeof e.target !== 'string') continue;
    edges.push({ source: e.source, target: e.target, certain: e.certain !== false });
  }
  return {
    nodes,
    edges,
    byFile: r.by_file === true,
    everything: r.everything === true,
    partial: r.partial === true,
    mode: typeof r.mode === 'string' ? r.mode : 'off',
    reviewRunning: r.review_running === true,
  };
}

// ---------------------------------------------------------------- what to show

/** What the view shows: the graph, or one text instead of it. */
export type GraphNotice =
  | { kind: 'graph' }
  /** "error" comes with a "Try again" button. */
  | { kind: 'detached' | 'off' | 'loading' | 'clean' | 'error'; text: string };

/** The line shown above a graph that leaves the unreviewed files out. */
export const EVERYTHING_TEXT = 'There is no trusted commit, so every file counts as unreviewed. Only marks and code to judge again are shown.';

/** The line shown above a graph that lacks callers or used units. */
export const PARTIAL_TEXT = 'Not every caller or used unit is shown: the search ran out of time, or the graph is full.';

/**
 * Decide what the view shows.
 *
 * @param attached Whether a session is attached.
 * @param mode `honesty.mode` as the session reports it; `undefined` or "off" if it is off.
 * @param graph The last graph of the session, or `null` before the first answer.
 * @param error The error of the last request, if it failed.
 * @param clean Whether the session reports a clean honesty state.
 * @returns "graph" if there are nodes to draw; else the text for the state.
 */
export function graphNotice(attached: boolean, mode: string | undefined, graph: GraphData | null, error: string | undefined, clean: boolean): GraphNotice {
  if (!attached) return { kind: 'detached', text: 'No session is attached. The honesty graph shows the honesty state of an attached session.' };
  if (!mode || mode === 'off' || (graph && graph.mode === 'off')) {
    return { kind: 'off', text: 'Code honesty is off. Choose "track" or "enforce" in the Honesty menu to see the graph.' };
  }
  if (error !== undefined) return { kind: 'error', text: `The honesty graph was not loaded: ${error}` };
  if (!graph) return { kind: 'loading', text: 'Loading the honesty graph…' };
  if (graph.nodes.length > 0) return { kind: 'graph' };
  if (graph.everything) return { kind: 'clean', text: EVERYTHING_TEXT };
  if (clean) return { kind: 'clean', text: 'Nothing to show: the honesty state is clean.' };
  return { kind: 'clean', text: 'Nothing to show: no unit is marked, changed or to judge again. The Honesty chip says what is still open.' };
}

/**
 * List the lines shown above a drawn graph.
 *
 * @param graph The graph.
 * @param reviewRunning Whether an honesty review runs.
 * @returns Zero to four lines: a running review, one node per file, no trusted commit,
 *   callers or used units missing.
 */
export function graphNotes(graph: GraphData, reviewRunning: boolean): string[] {
  return [
    ...(reviewRunning ? ['Honesty review running…'] : []),
    ...(graph.byFile ? ['Too many units: one node per file.'] : []),
    ...(graph.everything ? [EVERYTHING_TEXT] : []),
    ...(graph.partial ? [PARTIAL_TEXT] : []),
  ];
}

// ---------------------------------------------------------------- one node

/**
 * Name the unit of a node inside its file.
 *
 * @param node The node.
 * @returns The dotted name: the part of the id after the path and ":". "" for a file node.
 */
export function symbolOf(node: Pick<GraphNode, 'id' | 'path' | 'kind'>): string {
  if (node.kind === 'file') return '';
  if (node.path && node.id.startsWith(`${node.path}:`)) return node.id.slice(node.path.length + 1);
  const colon = node.id.indexOf(':');
  return colon >= 0 ? node.id.slice(colon + 1) : '';
}

/**
 * Build the link target that opens a node's source (an `openLink` message).
 *
 * The host opens a relative link as a file of the workspace and reads "#L<line>" as the line.
 * Each part of the path is percent-encoded, so "?", "#", ":" and "%" in a file name stay part
 * of the name.
 *
 * @param path The file, relative to the workspace, with "/" separators.
 * @param line The 1-based line.
 * @returns E.g. "src/app.py#L12".
 */
export function sourceHref(path: string, line: number): string {
  return `${path.split('/').map(encodeURIComponent).join('/')}#L${Math.max(1, Math.floor(line) || 1)}`;
}

/**
 * Describe a node for a screen reader.
 *
 * @param node The node.
 * @returns The name, the state in words, a running review, the count of a file node, the
 *   callers left out, the id.
 */
export function nodeLabel(node: GraphNode): string {
  const parts = [node.name, STATE_TEXTS[node.state].word];
  if (node.reviewing) parts.push('a review reads it now');
  if (node.kind === 'file') parts.push(node.count > 1 ? `a file with ${node.count} units` : 'a whole file');
  const hidden = hiddenCallersText(node);
  if (hidden) parts.push(hidden);
  return `${parts.join(', ')}. ${node.id}`;
}

/**
 * Say how many callers of a node the graph leaves out.
 *
 * @param node The node.
 * @returns "" if none is left out; else e.g. "30 more unchanged callers are not shown".
 */
export function hiddenCallersText(node: Pick<GraphNode, 'hiddenCallers'>): string {
  if (node.hiddenCallers <= 0) return '';
  return node.hiddenCallers === 1 ? '1 more unchanged caller is not shown' : `${node.hiddenCallers} more unchanged callers are not shown`;
}

/** An entry of a node's context menu. */
export interface NodeMenuItem {
  /** "honest", "dishonest" or "open". */
  id: 'honest' | 'dishonest' | 'open';
  label: string;
  /** What choosing it does. */
  title: string;
  /** The messages to the host that choosing it sends, in order. */
  messages: WebviewMessage[];
}

/**
 * List the entries of a node's context menu.
 *
 *     state       "honest" entry                              sends
 *     dishonest   Mark as honest                              honestyClear, once per open mark
 *     recheck     Mark as honest                              honestyDismiss {recheckIds}
 *     changed     Mark as honest: trust the whole file…       honestyTrust {path} (the host asks first)
 *     caller, related, honest, settled    none
 *
 * "Mark as dishonest…" (`honestyMark`; the host asks for the reason) is there unless the node
 * is dishonest or settled. "Open the source" is always there.
 *
 * @param node The node.
 * @returns The entries, in the order of the menu.
 */
export function nodeMenu(node: GraphNode): NodeMenuItem[] {
  const items: NodeMenuItem[] = [];
  if (node.state === 'dishonest' && node.markIds.length > 0) {
    items.push({
      id: 'honest',
      label: 'Mark as honest',
      title: node.markIds.length > 1 ? `Clear its ${node.markIds.length} open marks by hand` : 'Clear its open mark by hand',
      messages: node.markIds.map((markId) => ({ type: 'honestyClear', markId })),
    });
  } else if (node.state === 'recheck' && node.recheckIds.length > 0) {
    items.push({
      id: 'honest',
      label: 'Mark as honest',
      title: 'It still says what it does: drop it from the code to judge again',
      messages: [{ type: 'honestyDismiss', recheckIds: node.recheckIds }],
    });
  } else if (node.state === 'changed' && node.path) {
    items.push({
      id: 'honest',
      label: 'Mark as honest: trust the whole file…',
      title: `Trust is kept per file content: the present content of ${node.path} is recorded as honest (asks first)`,
      messages: [{ type: 'honestyTrust', path: node.path }],
    });
  }
  if (node.state !== 'dishonest' && node.state !== 'settled' && node.path) {
    const symbol = symbolOf(node);
    items.push({
      id: 'dishonest',
      label: 'Mark as dishonest…',
      title: symbol ? `Record ${symbol} as dishonest (asks for the reason)` : `Record the whole file ${node.path} as dishonest (asks for the reason)`,
      messages: [{ type: 'honestyMark', path: node.path, symbol }],
    });
  }
  if (node.path) {
    items.push({
      id: 'open',
      label: 'Open the source',
      title: `Open ${node.path} at line ${node.line}`,
      messages: [{ type: 'openLink', href: sourceHref(node.path, node.line) }],
    });
  }
  return items;
}

// ---------------------------------------------------------------- settled nodes

/** Milliseconds a settled node stays visible. */
export const SETTLED_MS = 2500;

/** A node that left the graph and is shown once more, in green. */
export interface Ghost {
  /** The node with the state "settled". */
  node: GraphNode;
  /** The edges it had. */
  edges: GraphEdge[];
  /** The time (ms) at which it is removed. */
  until: number;
}

/**
 * Find the nodes to show once more after a new graph arrived.
 *
 * A node of the graph before that is dishonest, changed or to judge again, and that the new
 * graph does not have, was settled: it becomes a ghost for `SETTLED_MS`. Callers, related and
 * honest nodes are only there for another node and leave without a ghost. Ghosts from before stay
 * until their time, unless the new graph has their node again. There are no new ghosts when
 * one of the two graphs has one node per file and the other has not: the ids differ then.
 *
 * @param before The graph before, or `null` if there was none.
 * @param next The new graph.
 * @param ghosts The ghosts so far.
 * @param now The present time in milliseconds.
 * @returns The ghosts to show with `next`.
 */
export function settle(before: GraphData | null, next: GraphData, ghosts: readonly Ghost[], now: number): Ghost[] {
  const present = new Set(next.nodes.map((n) => n.id));
  const kept = ghosts.filter((g) => g.until > now && !present.has(g.node.id));
  if (!before || before.byFile !== next.byFile) return kept;
  const known = new Set(kept.map((g) => g.node.id));
  for (const node of before.nodes) {
    if (present.has(node.id) || known.has(node.id)) continue;
    if (node.state !== 'dishonest' && node.state !== 'changed' && node.state !== 'recheck') continue;
    kept.push({
      node: { ...node, state: 'settled', reviewing: false, markIds: [], recheckIds: [] },
      edges: before.edges.filter((e) => e.source === node.id || e.target === node.id),
      until: now + SETTLED_MS,
    });
  }
  return kept;
}

/**
 * Put the ghosts into a graph for the drawing.
 *
 * @param graph The graph.
 * @param ghosts The ghosts of `settle`.
 * @returns The graph's nodes and edges, plus each ghost and those of its edges whose other
 *   end is still shown.
 */
export function withGhosts(graph: GraphData, ghosts: readonly Ghost[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (ghosts.length === 0) return { nodes: graph.nodes, edges: graph.edges };
  const nodes = [...graph.nodes, ...ghosts.map((g) => g.node)];
  const shown = new Set(nodes.map((n) => n.id));
  const edges = [...graph.edges];
  for (const ghost of ghosts) edges.push(...ghost.edges.filter((e) => shown.has(e.source) && shown.has(e.target)));
  return { nodes, edges };
}

// ---------------------------------------------------------------- asking again

/** Least milliseconds between two requests for the graph. */
export const REFRESH_GAP_MS = 500;
/** Milliseconds a change waits for further changes before the graph is asked for. */
export const REFRESH_DEBOUNCE_MS = 150;
/** Milliseconds after which a request without an answer no longer holds back the next one. */
export const REFRESH_STALE_MS = 30_000;

/**
 * Decides when the view asks for the graph: at most one request waits for its answer, and two
 * requests are at least `REFRESH_GAP_MS` apart.
 *
 *     want() ──(debounce, gap)──► request() ──► answered() ──(a want() came meanwhile)──► again
 *     stop()   forgets what was wanted; an answer that is still due is awaited
 *
 * Side effects: timers (`setTimeout`), and the calls of `request`.
 */
export class GraphRefresh {
  private timer: ReturnType<typeof setTimeout> | undefined;
  /** True from a request until its answer. */
  private waiting = false;
  /** True if the graph is wanted and not asked for yet. */
  private wanted = false;
  /** Time (ms) of the last request. */
  private sentAt = Number.NEGATIVE_INFINITY;

  /**
   * @param request Asks the host for the graph; the caller reports the answer with `answered`.
   */
  constructor(private readonly request: () => void) {}

  /**
   * Note that the graph is wanted, and ask for it as soon as the rules allow.
   *
   * @param atOnce True to skip the debounce (the view was just opened). The gap to the
   *   request before still holds.
   * @returns Nothing.
   */
  want(atOnce = false): void {
    this.wanted = true;
    if (this.waiting && Date.now() - this.sentAt >= REFRESH_STALE_MS) this.waiting = false;
    if (this.waiting) return;
    if (this.timer !== undefined) {
      if (!atOnce) return;
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    const wait = Math.max(atOnce ? 0 : REFRESH_DEBOUNCE_MS, this.sentAt + REFRESH_GAP_MS - Date.now());
    if (wait <= 0) this.fire();
    else this.timer = setTimeout(() => this.fire(), wait);
  }

  /**
   * Note that the answer to the last request arrived (a graph or an error). If the graph was
   * wanted again meanwhile, it is asked for again.
   *
   * @returns Nothing.
   */
  answered(): void {
    this.waiting = false;
    if (this.wanted) this.want();
  }

  /**
   * Forget what was wanted and cancel a request that was not sent yet (the view was closed).
   *
   * @returns Nothing.
   */
  stop(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.wanted = false;
  }

  /**
   * Send the request if the graph is still wanted.
   *
   * @returns Nothing.
   */
  private fire(): void {
    this.timer = undefined;
    if (!this.wanted) return;
    this.wanted = false;
    this.waiting = true;
    this.sentAt = Date.now();
    this.request();
  }
}
