// The layout of the honesty graph (src/shared/honestyGraphLayout.ts): layers, a stable order,
// cycles, collapsed callers, sizes and edges. Pure functions; nothing is read or written.
import { describe, expect, it } from 'vitest';
import {
  GROUP_LIMIT,
  LABEL_CHARS,
  MARGIN,
  NODE_GAP,
  NODE_HEIGHT,
  NODE_MAX_WIDTH,
  NODE_MIN_WIDTH,
  layoutGraph,
  middleEllipsis,
  nodeWidth,
  type GraphLayout,
  type LayoutEdgeInput,
  type LayoutNodeInput,
  type PlacedNode,
} from '../../src/shared/honestyGraphLayout';

/** A node with the given id; the name is the id. */
function node(id: string, state = 'changed', extra: Partial<LayoutNodeInput> = {}): LayoutNodeInput {
  return { id, name: id, state, ...extra };
}

/** An edge `source` uses `target`. */
function edge(source: string, target: string, certain = true): LayoutEdgeInput {
  return { source, target, certain };
}

/** The placed node with the given id. */
function at(layout: GraphLayout, id: string): PlacedNode {
  const found = layout.nodes.find((n) => n.id === id);
  if (!found) throw new Error(`no node ${id}`);
  return found;
}

/** Seven callers c0..c6 of the unit "t". */
function manyCallers(count = GROUP_LIMIT + 2): { nodes: LayoutNodeInput[]; edges: LayoutEdgeInput[] } {
  const callers = Array.from({ length: count }, (_, i) => `c${i}`);
  return {
    nodes: [node('t', 'dishonest'), ...callers.map((c) => node(c, 'caller'))],
    edges: callers.map((c, i) => edge(c, 't', i === 0)),
  };
}

describe('layers', () => {
  it('puts a caller above what it uses, by the longest path', () => {
    const layout = layoutGraph([node('a'), node('b'), node('c')], [edge('a', 'b'), edge('b', 'c'), edge('a', 'c')]);
    expect([at(layout, 'a').layer, at(layout, 'b').layer, at(layout, 'c').layer]).toEqual([2, 1, 0]);
    expect(at(layout, 'a').y).toBeLessThan(at(layout, 'b').y);
    expect(at(layout, 'b').y).toBeLessThan(at(layout, 'c').y);
  });

  it('puts every node that uses nothing into the lowest layer', () => {
    const layout = layoutGraph([node('x'), node('y'), node('z')], []);
    expect(layout.nodes.map((n) => n.layer)).toEqual([0, 0, 0]);
    expect(new Set(layout.nodes.map((n) => n.y)).size).toBe(1);
  });

  it('lays out a cycle without hanging: the edge that closes it is ignored for the layers', () => {
    const layout = layoutGraph([node('a'), node('b'), node('c')], [edge('a', 'b'), edge('b', 'c'), edge('c', 'a'), edge('a', 'a')]);
    expect(layout.nodes.map((n) => n.id).sort()).toEqual(['a', 'b', 'c']);
    expect([at(layout, 'a').layer, at(layout, 'b').layer, at(layout, 'c').layer]).toEqual([2, 1, 0]);
    // The edge from a node to itself is dropped; the edge of the cycle is drawn upwards.
    expect(layout.edges.map((e) => `${e.source}>${e.target}`)).toEqual(['a>b', 'b>c', 'c>a']);
    const back = layout.edges[2].points;
    expect(back[0].y).toBeGreaterThan(back[3].y);
  });

  it('returns an empty drawing for an empty graph', () => {
    expect(layoutGraph([], [])).toMatchObject({ nodes: [], edges: [], width: 0, height: 0 });
  });
});

describe('order', () => {
  it('sorts the lowest layer by id and a caller layer by what it uses, ties by id', () => {
    const nodes = [node('t1'), node('t2'), node('x'), node('y'), node('z')];
    const edges = [edge('x', 't2'), edge('y', 't1'), edge('z', 't1')];
    const layout = layoutGraph(nodes, edges);
    const row = (layer: number) => layout.nodes.filter((n) => n.layer === layer).map((n) => n.id);
    expect(row(0)).toEqual(['t1', 't2']);
    expect(row(1)).toEqual(['y', 'z', 'x']);
  });

  it('gives the same picture for the same graph in any input order', () => {
    const nodes = [node('a'), node('b'), node('c'), node('d'), node('e')];
    const edges = [edge('a', 'c'), edge('b', 'c'), edge('b', 'd'), edge('e', 'a')];
    const first = layoutGraph(nodes, edges);
    const second = layoutGraph([...nodes].reverse(), [...edges].reverse());
    expect(second).toEqual(first);
  });

  it('keeps the other nodes of the lowest row in their order when one is added', () => {
    const before = layoutGraph([node('a'), node('c')], []);
    const after = layoutGraph([node('a'), node('b'), node('c')], []);
    expect(before.nodes.map((n) => n.id)).toEqual(['a', 'c']);
    expect(after.nodes.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });

  it('lists the nodes from top to bottom and from left to right', () => {
    const layout = layoutGraph([node('a'), node('b'), node('c'), node('d')], [edge('a', 'c'), edge('b', 'd')]);
    const keys = layout.nodes.map((n) => [n.y, n.x]);
    expect(keys).toEqual([...keys].sort((p, q) => p[0] - q[0] || p[1] - q[1]));
  });
});

describe('collapsed callers', () => {
  it('replaces more than five callers of one unit by one group node', () => {
    const { nodes, edges } = manyCallers();
    const layout = layoutGraph(nodes, edges);
    expect(layout.nodes).toHaveLength(2);
    const group = layout.nodes.find((n) => n.group)!;
    expect(group.label).toBe('7 callers');
    expect(group.group).toEqual({ target: 't', members: ['c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6'] });
    expect(group.y).toBeLessThan(at(layout, 't').y);
    // One edge of the members is certain, so the group's edge is.
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0]).toMatchObject({ source: group.id, target: 't', certain: true });
    expect(layout.collapsible.get('t')).toHaveLength(7);
  });

  it('shows the callers one by one for an expanded target, and still says that they can collapse', () => {
    const { nodes, edges } = manyCallers();
    const layout = layoutGraph(nodes, edges, { expanded: new Set(['t']) });
    expect(layout.nodes).toHaveLength(8);
    expect(layout.nodes.some((n) => n.group)).toBe(false);
    expect(layout.collapsible.get('t')).toHaveLength(7);
  });

  it('leaves five callers alone', () => {
    const { nodes, edges } = manyCallers(GROUP_LIMIT);
    const layout = layoutGraph(nodes, edges);
    expect(layout.nodes).toHaveLength(GROUP_LIMIT + 1);
    expect(layout.collapsible.size).toBe(0);
  });

  it('keeps out of the group: a caller of two units, a caller that is used, and a node of another state', () => {
    const { nodes, edges } = manyCallers(9);
    const layout = layoutGraph(
      [...nodes.map((n) => (n.id === 'c8' ? node('c8', 'recheck') : n)), node('other', 'dishonest'), node('top', 'caller')],
      [...edges, edge('c6', 'other'), edge('top', 'c7')],
    );
    const group = layout.nodes.find((n) => n.group)!;
    expect(group.group!.members).toEqual(['c0', 'c1', 'c2', 'c3', 'c4', 'c5']);
    expect(layout.nodes.map((n) => n.id)).toEqual(expect.arrayContaining(['c6', 'c7', 'c8', 'top', 'other', 't']));
  });

  it('gives the group an id that no node has', () => {
    const { nodes, edges } = manyCallers();
    const layout = layoutGraph([...nodes, node('callers of t')], edges);
    const ids = layout.nodes.map((n) => n.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('sizes', () => {
  it('cuts a long name in the middle and leaves a short one alone', () => {
    expect(middleEllipsis('parse', 10)).toBe('parse');
    expect(middleEllipsis('abcdefghij', 7)).toBe('abc…hij');
    expect(Array.from(middleEllipsis('x'.repeat(100), LABEL_CHARS))).toHaveLength(LABEL_CHARS);
  });

  it('makes a node as wide as its name needs, within the limits', () => {
    expect(nodeWidth('f', '')).toBe(NODE_MIN_WIDTH);
    expect(nodeWidth('Parser.parse_all', '')).toBeGreaterThan(nodeWidth('Parser.parse', ''));
    expect(nodeWidth('x'.repeat(500), '')).toBe(NODE_MAX_WIDTH);
    const layout = layoutGraph([node('a', 'changed', { name: 'a_very_long_function_name_that_does_not_fit_into_a_node' })], []);
    expect(layout.nodes[0].label).toContain('…');
    expect(layout.nodes[0].width).toBeLessThanOrEqual(NODE_MAX_WIDTH);
    expect(layout.nodes[0].height).toBe(NODE_HEIGHT);
  });

  it('shows the count of a file node that stands for several units', () => {
    const layout = layoutGraph([node('a.py', 'changed', { kind: 'file', count: 12 }), node('b.py', 'changed', { kind: 'file', count: 1 }), node('c.py:f', 'changed', { count: 3 })], []);
    expect(layout.nodes.map((n) => n.badge)).toEqual(['12', '', '']);
    expect(at(layout, 'a.py').width).toBeGreaterThan(at(layout, 'b.py').width);
  });

  it('keeps every node inside the drawing and no two nodes of a row on top of each other', () => {
    const nodes = Array.from({ length: 30 }, (_, i) => node(`unit_${String(i).padStart(2, '0')}`));
    const edges = [edge('unit_00', 'unit_05'), edge('unit_01', 'unit_05'), edge('unit_02', 'unit_29'), edge('unit_03', 'unit_00')];
    const layout = layoutGraph(nodes, edges, { maxWidth: 360 });
    expect(layout.width).toBeLessThanOrEqual(360);
    for (const n of layout.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(MARGIN);
      expect(n.x + n.width).toBeLessThanOrEqual(layout.width - MARGIN);
      expect(n.y).toBeGreaterThanOrEqual(MARGIN);
      expect(n.y + n.height).toBeLessThanOrEqual(layout.height - MARGIN);
    }
    const rows = new Map<number, PlacedNode[]>();
    for (const n of layout.nodes) rows.set(n.y, [...(rows.get(n.y) ?? []), n]);
    // The lowest layer has 26 nodes: it wraps into several rows.
    expect(rows.size).toBeGreaterThan(3);
    for (const row of rows.values()) {
      for (let i = 1; i < row.length; i++) expect(row[i].x).toBeGreaterThanOrEqual(row[i - 1].x + row[i - 1].width + NODE_GAP - 1);
    }
  });

  it('puts one caller above the middle of the units it uses', () => {
    const layout = layoutGraph([node('a'), node('b'), node('top')], [edge('top', 'a'), edge('top', 'b')]);
    const middle = (n: PlacedNode) => n.x + n.width / 2;
    expect(Math.abs(middle(at(layout, 'top')) - (middle(at(layout, 'a')) + middle(at(layout, 'b'))) / 2)).toBeLessThanOrEqual(1);
  });
});

describe('edges', () => {
  it('draws a curve from the lower edge of the caller to the upper edge of the used unit', () => {
    const layout = layoutGraph([node('caller'), node('unit')], [edge('caller', 'unit', false)]);
    const [from, c1, c2, to] = layout.edges[0].points;
    const source = at(layout, 'caller');
    const target = at(layout, 'unit');
    expect(from).toEqual({ x: source.x + source.width / 2, y: source.y + source.height });
    expect(to).toEqual({ x: target.x + target.width / 2, y: target.y });
    expect(c1.x).toBe(from.x);
    expect(c2.x).toBe(to.x);
    expect(layout.edges[0].certain).toBe(false);
  });

  it('drops an edge with an unknown end and counts a repeated edge once', () => {
    const layout = layoutGraph([node('a'), node('b')], [edge('a', 'b', false), edge('a', 'b', true), edge('a', 'gone'), edge('gone', 'b')]);
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0]).toMatchObject({ source: 'a', target: 'b', certain: true });
  });
});
