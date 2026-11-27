// The honesty graph without its drawing (src/shared/honestyGraphModel.ts): reading the
// session's answer, the texts of the special states, a node's menu, settled nodes, and when the
// graph is asked for again. Pure functions; the refresh tests use vitest's fake timers.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  EVERYTHING_TEXT,
  GraphRefresh,
  REFRESH_DEBOUNCE_MS,
  REFRESH_GAP_MS,
  REFRESH_STALE_MS,
  SETTLED_MS,
  STATE_TEXTS,
  graphNotes,
  graphNotice,
  nodeLabel,
  nodeMenu,
  readGraph,
  settle,
  sourceHref,
  symbolOf,
  withGhosts,
  type GraphData,
  type GraphNode,
} from '../../src/shared/honestyGraphModel';
import { parseWebviewMessage } from '../../src/shared/messages';
import { splitLine } from '../../src/shared/sourceRefs';

/** A node of `honesty.graph` as the session sends it. */
function raw(id: string, state: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const path = id.split(':')[0];
  return { id, path, line: 7, name: id.split(':')[1] ?? path, kind: id.includes(':') ? 'unit' : 'file', state, ...extra };
}

/** A graph read from raw nodes and edges. */
function graphOf(nodes: Record<string, unknown>[], edges: Record<string, unknown>[] = [], extra: Record<string, unknown> = {}): GraphData {
  return readGraph({ nodes, edges, mode: 'track', ...extra });
}

/** The one node of a graph read from a raw node. */
function one(id: string, state: string, extra: Record<string, unknown> = {}): GraphNode {
  return graphOf([raw(id, state, extra)]).nodes[0];
}

describe('readGraph', () => {
  it('fills the defaults and keeps the ids of marks and entries', () => {
    const graph = readGraph({
      nodes: [{ id: 'a.py:f', path: 'a.py', line: 3, name: 'f', kind: 'unit', state: 'dishonest', mark_ids: [4, 'x', 5] }, { id: 'b.py', kind: 'file', state: 'changed', count: 3 }],
      edges: [{ source: 'a.py:f', target: 'b.py' }, { source: 'a.py:f', target: 'b.py', certain: false }],
      by_file: true,
      mode: 'enforce',
      review_running: true,
    });
    expect(graph.nodes[0]).toEqual({
      id: 'a.py:f', path: 'a.py', line: 3, name: 'f', kind: 'unit', state: 'dishonest', detail: '', reviewing: false, markIds: [4, 5], recheckIds: [], count: 1,
    });
    expect(graph.nodes[1]).toMatchObject({ id: 'b.py', path: '', line: 1, name: 'b.py', kind: 'file', count: 3 });
    expect(graph.edges.map((e) => e.certain)).toEqual([true, false]);
    expect(graph).toMatchObject({ byFile: true, everything: false, mode: 'enforce', reviewRunning: true });
  });

  it('leaves out what it cannot read and never throws', () => {
    expect(readGraph(null)).toEqual({ nodes: [], edges: [], byFile: false, everything: false, mode: 'off', reviewRunning: false });
    const graph = readGraph({ nodes: [null, { id: 3, state: 'changed' }, { id: 'x', state: 'purple' }, 'text'], edges: [{ source: 'a' }, 4], mode: 5 });
    expect(graph.nodes).toEqual([]);
    expect(graph.edges).toEqual([]);
    expect(graph.mode).toBe('off');
  });
});

describe('graphNotice', () => {
  const nodes = graphOf([raw('a.py:f', 'changed')]);
  const empty = graphOf([]);

  it('names the state that keeps the graph away', () => {
    expect(graphNotice(false, 'track', nodes, undefined, false).kind).toBe('detached');
    expect(graphNotice(true, 'off', nodes, undefined, false).kind).toBe('off');
    expect(graphNotice(true, undefined, null, undefined, false).kind).toBe('off');
    expect(graphNotice(true, 'track', readGraph({ mode: 'off' }), undefined, false).kind).toBe('off');
    expect(graphNotice(true, 'track', null, undefined, false)).toEqual({ kind: 'loading', text: 'Loading the honesty graph…' });
    expect(graphNotice(true, 'track', null, 'timeout', false)).toEqual({ kind: 'error', text: 'The honesty graph was not loaded: timeout' });
    expect(graphNotice(true, 'track', nodes, undefined, false)).toEqual({ kind: 'graph' });
  });

  it('says why an empty graph is empty', () => {
    expect(graphNotice(true, 'track', empty, undefined, true)).toEqual({ kind: 'clean', text: 'Nothing to show: the honesty state is clean.' });
    expect(graphNotice(true, 'track', graphOf([], [], { everything: true }), undefined, false)).toEqual({ kind: 'clean', text: EVERYTHING_TEXT });
    // Not clean (a review is required, say), but no unit to draw: it does not claim "clean".
    const open = graphNotice(true, 'track', empty, undefined, false);
    expect(open.kind).toBe('clean');
    expect(open.kind !== 'graph' && open.text).not.toContain('is clean');
  });

  it('lists the notes above a graph', () => {
    expect(graphNotes(nodes, false)).toEqual([]);
    expect(graphNotes(graphOf([raw('a.py', 'changed')], [], { by_file: true, everything: true }), true)).toEqual([
      'Honesty review running…',
      'Too many units: one node per file.',
      EVERYTHING_TEXT,
    ]);
  });
});

describe('a node', () => {
  it('names its unit inside the file', () => {
    expect(symbolOf(one('src/a.py:Parser.parse', 'changed'))).toBe('Parser.parse');
    expect(symbolOf(one('src/a.py', 'changed'))).toBe('');
    // A ":" in the path belongs to the path.
    expect(symbolOf({ id: 'odd:dir/a.py:f', path: 'odd:dir/a.py', kind: 'unit' })).toBe('f');
    expect(symbolOf({ id: 'a.py:f', path: '', kind: 'unit' })).toBe('f');
  });

  it('builds a link that the hosts read back as the same file and line', () => {
    expect(sourceHref('src/app.py', 12)).toBe('src/app.py#L12');
    for (const path of ['src/app.py', 'Makefile', 'docs/a b#c?.md', 'odd:1/x%20.py']) {
      const href = sourceHref(path, 40);
      // What both hosts do with a relative link (`openLink`).
      expect(/^[a-z][a-z0-9+.-]*:/i.test(href), href).toBe(false);
      expect(splitLine(decodeURIComponent(href.split('?')[0]))).toEqual({ path, line: 40 });
    }
    expect(sourceHref('a.py', 0)).toBe('a.py#L1');
  });

  it('describes itself with its state in words', () => {
    expect(nodeLabel(one('src/a.py:f', 'recheck', { reviewing: true }))).toBe('f, to judge again, a review reads it now. src/a.py:f');
    expect(nodeLabel(one('src/a.py', 'changed', { count: 4 }))).toBe('src/a.py, changed, a file with 4 units. src/a.py');
    for (const texts of Object.values(STATE_TEXTS)) expect(texts.glyph).toHaveLength(1);
  });
});

describe('nodeMenu', () => {
  const labels = (node: GraphNode) => nodeMenu(node).map((i) => i.label);

  it('clears every mark of a dishonest node and offers no second mark', () => {
    const menu = nodeMenu(one('a.py:f', 'dishonest', { mark_ids: [4, 9] }));
    expect(menu.map((i) => i.label)).toEqual(['Mark as honest', 'Open the source']);
    expect(menu[0].messages).toEqual([{ type: 'honestyClear', markId: 4 }, { type: 'honestyClear', markId: 9 }]);
    expect(menu[1].messages).toEqual([{ type: 'openLink', href: 'a.py#L7' }]);
  });

  it('drops the entries of a node to judge again', () => {
    const menu = nodeMenu(one('a.py:f', 'recheck', { recheck_ids: [2, 3] }));
    expect(menu.map((i) => i.label)).toEqual(['Mark as honest', 'Mark as dishonest…', 'Open the source']);
    expect(menu[0].messages).toEqual([{ type: 'honestyDismiss', recheckIds: [2, 3] }]);
    expect(menu[1].messages).toEqual([{ type: 'honestyMark', path: 'a.py', symbol: 'f' }]);
  });

  it('says that a changed node is settled by trusting the whole file', () => {
    const menu = nodeMenu(one('src/a.py:Parser.parse', 'changed'));
    expect(menu[0].label).toBe('Mark as honest: trust the whole file…');
    expect(menu[0].messages).toEqual([{ type: 'honestyTrust', path: 'src/a.py' }]);
    expect(menu[1].messages).toEqual([{ type: 'honestyMark', path: 'src/a.py', symbol: 'Parser.parse' }]);
  });

  it('has no "honest" entry for a caller, an honest node and a settled one, and marks a file with an empty name', () => {
    expect(labels(one('a.py:f', 'caller'))).toEqual(['Mark as dishonest…', 'Open the source']);
    expect(labels(one('a.py:f', 'honest'))).toEqual(['Mark as dishonest…', 'Open the source']);
    expect(labels({ ...one('a.py:f', 'changed'), state: 'settled' })).toEqual(['Open the source']);
    expect(nodeMenu(one('run.sh', 'caller'))[0].messages).toEqual([{ type: 'honestyMark', path: 'run.sh', symbol: '' }]);
    // Without ids there is nothing to clear or to drop.
    expect(labels(one('a.py:f', 'dishonest'))).toEqual(['Open the source']);
    expect(labels(one('a.py:f', 'recheck'))).toEqual(['Mark as dishonest…', 'Open the source']);
  });

  it('sends only messages that the host accepts', () => {
    for (const state of ['dishonest', 'recheck', 'changed', 'caller', 'honest']) {
      for (const item of nodeMenu(one('src/a.py:f', state, { mark_ids: [1], recheck_ids: [2] }))) {
        for (const message of item.messages) expect(parseWebviewMessage(message), item.label).toEqual(message);
      }
    }
  });
});

describe('settled nodes', () => {
  const before = graphOf(
    [raw('a.py:f', 'dishonest', { mark_ids: [1], reviewing: true }), raw('a.py:g', 'caller'), raw('b.py:h', 'changed'), raw('c.py:k', 'honest'), raw('d.py:r', 'recheck')],
    [{ source: 'a.py:g', target: 'a.py:f' }, { source: 'd.py:r', target: 'c.py:k' }],
  );

  it('shows a node that left the graph once more, without its marks, until its time is over', () => {
    const next = graphOf([raw('b.py:h', 'changed')]);
    const ghosts = settle(before, next, [], 1000);
    // The caller and the honest node leave without a ghost.
    expect(ghosts.map((g) => g.node.id)).toEqual(['a.py:f', 'd.py:r']);
    expect(ghosts[0].node).toMatchObject({ state: 'settled', markIds: [], reviewing: false });
    expect(ghosts[0].until).toBe(1000 + SETTLED_MS);
    const shown = withGhosts(next, ghosts);
    expect(shown.nodes.map((n) => n.id)).toEqual(['b.py:h', 'a.py:f', 'd.py:r']);
    // The other ends of the ghosts' edges are gone, so the edges are.
    expect(shown.edges).toEqual([]);
    expect(settle(next, next, ghosts, 1000 + SETTLED_MS - 1)).toHaveLength(2);
    expect(settle(next, next, ghosts, 1000 + SETTLED_MS)).toEqual([]);
  });

  it('keeps the edge of a ghost whose other end is still shown', () => {
    const next = graphOf([raw('c.py:k', 'changed')]);
    const shown = withGhosts(next, settle(before, next, [], 0));
    expect(shown.edges).toEqual([{ source: 'd.py:r', target: 'c.py:k', certain: true }]);
  });

  it('drops a ghost whose node is back, and makes none for the first graph or a change to one node per file', () => {
    const gone = graphOf([]);
    const ghosts = settle(before, gone, [], 0);
    expect(settle(gone, before, ghosts, 10)).toEqual([]);
    expect(settle(null, gone, [], 0)).toEqual([]);
    expect(settle(before, graphOf([raw('a.py', 'changed')], [], { by_file: true }), [], 0)).toEqual([]);
    expect(withGhosts(before, []).nodes).toBe(before.nodes);
  });
});

describe('GraphRefresh', () => {
  let sent: number[];
  let refresh: GraphRefresh;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(100_000);
    sent = [];
    refresh = new GraphRefresh(() => sent.push(Date.now()));
  });
  afterEach(() => vi.useRealTimers());

  it('asks at once when the view opens, and after a short wait for a change', () => {
    refresh.want(true);
    expect(sent).toEqual([100_000]);
    refresh.answered();
    refresh.want();
    refresh.want();
    vi.advanceTimersByTime(REFRESH_GAP_MS - 1);
    expect(sent).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sent).toEqual([100_000, 100_000 + REFRESH_GAP_MS]);
  });

  it('merges the changes of a burst into one request', () => {
    vi.advanceTimersByTime(10_000);
    for (let i = 0; i < 5; i++) refresh.want();
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(sent).toHaveLength(1);
    vi.advanceTimersByTime(10_000);
    expect(sent).toHaveLength(1);
  });

  it('keeps one request in flight and asks again after its answer if something changed meanwhile', () => {
    refresh.want(true);
    refresh.want();
    refresh.want(true);
    vi.advanceTimersByTime(5000);
    expect(sent).toHaveLength(1);
    refresh.answered();
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(sent).toHaveLength(2);
    refresh.answered();
    vi.advanceTimersByTime(5000);
    expect(sent).toHaveLength(2);
  });

  it('sends at most two requests a second under steady changes', () => {
    refresh.want(true);
    for (let t = 0; t < 3000; t += 50) {
      vi.advanceTimersByTime(50);
      refresh.answered();
      refresh.want();
    }
    for (let i = 1; i < sent.length; i++) expect(sent[i] - sent[i - 1]).toBeGreaterThanOrEqual(REFRESH_GAP_MS);
    expect(sent.length).toBeGreaterThan(4);
    expect(sent.length).toBeLessThanOrEqual(7);
  });

  it('asks nothing after stop, and nothing when the answer of the last request arrives', () => {
    refresh.want(true);
    refresh.want();
    refresh.stop();
    refresh.answered();
    refresh.want();
    refresh.stop();
    vi.advanceTimersByTime(60_000);
    expect(sent).toHaveLength(1);
  });

  it('does not wait for ever for an answer that never comes', () => {
    refresh.want(true);
    vi.advanceTimersByTime(REFRESH_STALE_MS - 1);
    refresh.want();
    vi.advanceTimersByTime(1000);
    expect(sent).toHaveLength(1);
    vi.advanceTimersByTime(REFRESH_STALE_MS);
    refresh.want();
    vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS);
    expect(sent).toHaveLength(2);
  });
});
