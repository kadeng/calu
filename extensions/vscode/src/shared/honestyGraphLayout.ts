/**
 * Layout of the honesty graph: where each node and each edge is drawn (no DOM, no `vscode`).
 *
 * An edge says that its source uses its target. Callers stand above the units they use:
 *
 *     layer 2     [ main ]
 *                  /    \
 *     layer 1   [ run ]  \
 *                  \      \
 *     layer 0     [ parse ]      a unit that uses nothing in the graph
 *
 * The steps of `layoutGraph`:
 *
 * 1. Clean the input: the first node of an id counts, an edge needs two known ends, an edge
 *    from a node to itself is dropped, a repeated edge counts once.
 * 2. Collapse: more than `GROUP_LIMIT` callers of one unit become one node "N callers"
 *    (`collapseCallers`).
 * 3. Layers: a node's layer is the longest path down to a node that uses nothing. An edge that
 *    closes a cycle is ignored for this.
 * 4. Order: the lowest layer is sorted by id. Every other layer is sorted by the average
 *    position of the nodes it uses, ties by id. The same graph gives the same picture.
 * 5. Places: a layer that is wider than `maxWidth` wraps into rows. A node stands above the
 *    nodes it uses where there is room.
 * 6. Edges: one cubic curve per edge, from the lower edge of the source to the upper edge of
 *    the target.
 */

/** A node as the layout needs it. */
export interface LayoutNodeInput {
  /** The unique id. */
  id: string;
  /** The short name to show. */
  name: string;
  /** The node's state; only "caller" and "related" matter here (such callers can collapse). */
  state: string;
  /** "file" for a whole file; such a node shows `count`. */
  kind?: string;
  /** How many units the node stands for. */
  count?: number;
}

/** An edge as the layout needs it: `source` uses `target`. */
export interface LayoutEdgeInput {
  source: string;
  target: string;
  /** False for a link that nothing confirmed; default true. */
  certain?: boolean;
}

/** What `layoutGraph` takes besides the graph. */
export interface LayoutOptions {
  /** The targets whose callers are shown one by one although they could collapse. */
  expanded?: ReadonlySet<string>;
  /** The width the drawing should fit into, in pixels; a wider layer wraps into rows. */
  maxWidth?: number;
}

/** A point of the drawing, in pixels from its upper left corner. */
export interface Point {
  x: number;
  y: number;
}

/** A node with its place. */
export interface PlacedNode {
  /** The node's id; for a group an id that no node of the input has. */
  id: string;
  /** Left edge. */
  x: number;
  /** Upper edge. */
  y: number;
  width: number;
  height: number;
  /** The name, cut in the middle if it is long; for a group "N callers". */
  label: string;
  /** The count of a file node that stands for several units, else "". */
  badge: string;
  /** 0 for a node that uses nothing; a caller has a higher layer than what it uses. */
  layer: number;
  /** Set for a group of collapsed callers: the unit they use and their ids, sorted. */
  group?: { target: string; members: string[] };
}

/** An edge with its curve. */
export interface PlacedEdge {
  source: string;
  target: string;
  certain: boolean;
  /** A cubic Bézier curve: start, two control points, end. The end touches the target. */
  points: [Point, Point, Point, Point];
}

/** The result of `layoutGraph`. */
export interface GraphLayout {
  /** The nodes from top to bottom, and from left to right in a row. */
  nodes: PlacedNode[];
  /** The edges, sorted by source and target. */
  edges: PlacedEdge[];
  /** Width of the whole drawing; 0 for an empty graph. */
  width: number;
  /** Height of the whole drawing; 0 for an empty graph. */
  height: number;
  /** Per target with more than `GROUP_LIMIT` collapsible callers: their ids, sorted. */
  collapsible: Map<string, string[]>;
}

/** Height of a node. */
export const NODE_HEIGHT = 24;
/** Narrowest node. */
export const NODE_MIN_WIDTH = 56;
/** Widest node. */
export const NODE_MAX_WIDTH = 260;
/** Longest label; a longer name is cut in the middle. */
export const LABEL_CHARS = 28;
/** Width of one character of the label (a monospace font of 12 pixels). */
export const CHAR_WIDTH = 7.3;
/** Room left of the label, for the glyph of the state. */
export const LABEL_LEFT = 24;
/** Room right of the label. */
export const LABEL_RIGHT = 10;
/** Room between the label and the badge. */
export const BADGE_GAP = 8;
/** Room between two nodes of a row. */
export const NODE_GAP = 12;
/** Room between two rows of one layer. */
export const ROW_GAP = 14;
/** Room between two layers. */
export const LAYER_GAP = 44;
/** Room around the drawing. */
export const MARGIN = 12;
/** More callers of one unit than this collapse into a group. */
export const GROUP_LIMIT = 5;
/** `LayoutOptions.maxWidth` if none is given. */
export const DEFAULT_MAX_WIDTH = 640;
/** How far the curve between two nodes of one row hangs below them. */
const LOOP_DEPTH = 14;

/**
 * Cut a text in the middle so that it has at most `max` characters.
 *
 * @param text The text.
 * @param max The longest result, at least 3.
 * @returns The text itself if it is short enough; else its start, "…" and its end.
 */
export function middleEllipsis(text: string, max: number): string {
  const chars = Array.from(text);
  if (chars.length <= max) return text;
  const keep = Math.max(2, max - 1);
  const head = Math.ceil(keep / 2);
  return `${chars.slice(0, head).join('')}…${chars.slice(chars.length - (keep - head)).join('')}`;
}

/**
 * Compute the width of a node from its label and badge.
 *
 * @param label The label as shown.
 * @param badge The badge as shown, or "".
 * @returns The width in pixels, between `NODE_MIN_WIDTH` and `NODE_MAX_WIDTH`.
 */
export function nodeWidth(label: string, badge: string): number {
  const text = Array.from(label).length * CHAR_WIDTH + (badge ? BADGE_GAP + Array.from(badge).length * CHAR_WIDTH : 0);
  return Math.min(NODE_MAX_WIDTH, Math.max(NODE_MIN_WIDTH, Math.ceil(LABEL_LEFT + text + LABEL_RIGHT)));
}

/** A node after cleaning and collapsing. */
interface Unit {
  id: string;
  label: string;
  badge: string;
  width: number;
  group?: { target: string; members: string[] };
}

/** An edge after cleaning and collapsing. */
interface Link {
  source: string;
  target: string;
  certain: boolean;
}

/**
 * Drop what cannot be drawn: later nodes of a known id, edges with an unknown end, edges from
 * a node to itself, and repeated edges (the edge is certain if one of them is).
 *
 * @param nodes The nodes as given.
 * @param edges The edges as given.
 * @returns The nodes in their order and the edges sorted by source and target.
 */
function clean(nodes: readonly LayoutNodeInput[], edges: readonly LayoutEdgeInput[]): { nodes: LayoutNodeInput[]; links: Link[] } {
  const known = new Map<string, LayoutNodeInput>();
  for (const node of nodes) if (!known.has(node.id)) known.set(node.id, node);
  const links = new Map<string, Link>();
  for (const edge of edges) {
    if (edge.source === edge.target || !known.has(edge.source) || !known.has(edge.target)) continue;
    const key = JSON.stringify([edge.source, edge.target]);
    const certain = edge.certain !== false;
    const before = links.get(key);
    if (before) before.certain = before.certain || certain;
    else links.set(key, { source: edge.source, target: edge.target, certain });
  }
  return { nodes: [...known.values()], links: sortLinks([...links.values()]) };
}

/**
 * Sort edges by source, then target (by code units, so the order is the same everywhere).
 *
 * @param links The edges (modified).
 * @returns The same array.
 */
function sortLinks<T extends { source: string; target: string }>(links: T[]): T[] {
  return links.sort((a, b) => compare(a.source, b.source) || compare(a.target, b.target));
}

/**
 * Compare two strings by their code units.
 *
 * @param a A string.
 * @param b Another one.
 * @returns A negative number, 0 or a positive number, as `Array.sort` expects.
 */
function compare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Replace many callers of one unit by one group node.
 *
 * A caller can collapse if its state is "caller" or "related", it uses exactly one node, and
 * no node uses it. If more than `GROUP_LIMIT` such callers use the same target, and the target is not in
 * `expanded`, they become one node "N callers" with one edge to the target. That edge is
 * certain if the edge of one member is.
 *
 * @param nodes The cleaned nodes.
 * @param links The cleaned edges.
 * @param expanded The targets whose callers stay as they are.
 * @returns The nodes and edges to lay out, and per target the callers that can collapse.
 */
function collapseCallers(
  nodes: readonly LayoutNodeInput[],
  links: readonly Link[],
  expanded: ReadonlySet<string>,
): { units: Unit[]; links: Link[]; collapsible: Map<string, string[]> } {
  const uses = new Map<string, Link[]>();
  const used = new Set<string>();
  for (const link of links) {
    uses.set(link.source, [...(uses.get(link.source) ?? []), link]);
    used.add(link.target);
  }
  const callers = new Map<string, string[]>();
  for (const node of nodes) {
    const out = uses.get(node.id) ?? [];
    if ((node.state !== 'caller' && node.state !== 'related') || out.length !== 1 || used.has(node.id)) continue;
    callers.set(out[0].target, [...(callers.get(out[0].target) ?? []), node.id]);
  }
  const collapsible = new Map<string, string[]>();
  const hidden = new Map<string, string>();
  const groups: Unit[] = [];
  const groupLinks: Link[] = [];
  const ids = new Set(nodes.map((n) => n.id));
  for (const target of [...callers.keys()].sort(compare)) {
    const members = (callers.get(target) ?? []).sort(compare);
    if (members.length <= GROUP_LIMIT) continue;
    collapsible.set(target, members);
    if (expanded.has(target)) continue;
    let id = `callers of ${target}`;
    while (ids.has(id)) id += "'";
    ids.add(id);
    for (const member of members) hidden.set(member, id);
    const label = `${members.length} callers`;
    groups.push({ id, label, badge: '', width: nodeWidth(label, ''), group: { target, members } });
    groupLinks.push({ source: id, target, certain: links.some((l) => hidden.get(l.source) === id && l.certain) });
  }
  const units: Unit[] = nodes
    .filter((n) => !hidden.has(n.id))
    .map((n) => {
      const label = middleEllipsis(n.name || n.id, LABEL_CHARS);
      const badge = n.kind === 'file' && (n.count ?? 1) > 1 ? String(n.count) : '';
      return { id: n.id, label, badge, width: nodeWidth(label, badge) };
    });
  return {
    units: [...units, ...groups],
    links: sortLinks([...links.filter((l) => !hidden.has(l.source)), ...groupLinks]),
    collapsible,
  };
}

/**
 * Give every node its layer: 0 if it uses nothing, else one more than the highest layer of
 * the nodes it uses. A depth-first walk in the order of `ids`; an edge to a node that the walk
 * is still inside closes a cycle and is ignored, so the walk always ends.
 *
 * @param ids The node ids, sorted.
 * @param targets Per node the ids it uses, sorted.
 * @returns The layer of every id.
 */
function assignLayers(ids: readonly string[], targets: ReadonlyMap<string, readonly string[]>): Map<string, number> {
  const layer = new Map<string, number>();
  const open = new Set<string>();
  for (const root of ids) {
    if (layer.has(root)) continue;
    const stack = [{ id: root, next: 0, depth: 0 }];
    open.add(root);
    while (stack.length > 0) {
      const top = stack[stack.length - 1];
      const out = targets.get(top.id) ?? [];
      if (top.next < out.length) {
        const child = out[top.next++];
        const known = layer.get(child);
        if (known !== undefined) top.depth = Math.max(top.depth, known + 1);
        else if (!open.has(child)) {
          open.add(child);
          stack.push({ id: child, next: 0, depth: 0 });
        }
      } else {
        layer.set(top.id, top.depth);
        open.delete(top.id);
        stack.pop();
        const parent = stack[stack.length - 1];
        if (parent) parent.depth = Math.max(parent.depth, top.depth + 1);
      }
    }
  }
  return layer;
}

/**
 * Split the nodes of a layer into rows that are at most `inner` wide. A row has at least one
 * node, so a node wider than `inner` gets a row of its own.
 *
 * @param units The nodes of the layer, in their order.
 * @param inner The widest row.
 * @returns The rows, in order.
 */
function wrapRows(units: readonly Unit[], inner: number): Unit[][] {
  const rows: Unit[][] = [];
  let row: Unit[] = [];
  let width = 0;
  for (const unit of units) {
    const next = width + (row.length > 0 ? NODE_GAP : 0) + unit.width;
    if (row.length > 0 && next > inner) {
      rows.push(row);
      row = [unit];
      width = unit.width;
    } else {
      row.push(unit);
      width = next;
    }
  }
  if (row.length > 0) rows.push(row);
  return rows;
}

/**
 * Compute the width of a row.
 *
 * @param row The nodes of the row.
 * @returns The sum of their widths and of the gaps between them.
 */
function rowWidth(row: readonly Unit[]): number {
  return row.reduce((sum, unit) => sum + unit.width, 0) + NODE_GAP * Math.max(0, row.length - 1);
}

/**
 * Place the nodes of one row next to each other, each as near to the centre it wants as the
 * others allow. Neighbours that want the same room share it: they form a block that is centred
 * on the average of what its nodes want. Nothing leaves the range from `left` to `right`,
 * unless the row is wider than that range.
 *
 * @param row The nodes, in their order.
 * @param wanted The centre each node wants, by id.
 * @param left The left limit.
 * @param right The right limit.
 * @returns The left edge of each node, in the order of `row`.
 */
function placeRow(row: readonly Unit[], wanted: ReadonlyMap<string, number>, left: number, right: number): number[] {
  /** A run of nodes without room between them: `sum / count` is the start it wants. */
  interface Block {
    first: number;
    count: number;
    width: number;
    sum: number;
    start: number;
  }
  const clamp = (block: Block): number => Math.max(left, Math.min(block.sum / block.count, right - block.width));
  const blocks: Block[] = [];
  row.forEach((unit, index) => {
    const block: Block = { first: index, count: 1, width: unit.width, sum: (wanted.get(unit.id) ?? left) - unit.width / 2, start: 0 };
    block.start = clamp(block);
    blocks.push(block);
    while (blocks.length > 1) {
      const last = blocks[blocks.length - 1];
      const before = blocks[blocks.length - 2];
      if (before.start + before.width + NODE_GAP <= last.start) break;
      // The nodes of `last` want a start of `before` that puts them where they want to be.
      before.sum += last.sum - last.count * (before.width + NODE_GAP);
      before.count += last.count;
      before.width += NODE_GAP + last.width;
      before.start = clamp(before);
      blocks.pop();
    }
  });
  const xs: number[] = [];
  for (const block of blocks) {
    let x = block.start;
    for (let i = block.first; i < block.first + block.count; i++) {
      xs.push(Math.round(x));
      x += row[i].width + NODE_GAP;
    }
  }
  return xs;
}

/**
 * Compute the curve of an edge between two placed nodes.
 *
 * @param source The node that uses the other.
 * @param target The node that is used.
 * @returns Start, two control points and end. If the source is above the target the curve
 *   leaves its lower edge and ends on the target's upper edge; if it is below (an edge of a
 *   cycle), the other way round; in the same row the curve hangs below both.
 */
function curve(source: PlacedNode, target: PlacedNode): [Point, Point, Point, Point] {
  const sx = source.x + source.width / 2;
  const tx = target.x + target.width / 2;
  if (source.y === target.y) {
    const y = source.y + source.height;
    return [{ x: sx, y }, { x: sx, y: y + LOOP_DEPTH }, { x: tx, y: y + LOOP_DEPTH }, { x: tx, y }];
  }
  const down = source.y < target.y;
  const from = down ? source.y + source.height : source.y;
  const to = down ? target.y : target.y + target.height;
  const middle = (from + to) / 2;
  return [{ x: sx, y: from }, { x: sx, y: middle }, { x: tx, y: middle }, { x: tx, y: to }];
}

/**
 * Lay out a graph in layers, callers above the units they use (see the module comment).
 *
 * The result depends only on the arguments: the order of the given nodes and edges does not
 * matter, and the same graph gives the same places.
 *
 * @param nodes The nodes.
 * @param edges The edges; `source` uses `target`.
 * @param options The expanded groups and the width to fit into.
 * @returns The placed nodes and edges, the size of the drawing, and which callers can collapse.
 */
export function layoutGraph(nodes: readonly LayoutNodeInput[], edges: readonly LayoutEdgeInput[], options: LayoutOptions = {}): GraphLayout {
  const cleaned = clean(nodes, edges);
  const { units, links, collapsible } = collapseCallers(cleaned.nodes, cleaned.links, options.expanded ?? new Set());
  if (units.length === 0) return { nodes: [], edges: [], width: 0, height: 0, collapsible };
  const byId = new Map(units.map((u) => [u.id, u]));
  const ids = [...byId.keys()].sort(compare);
  const targets = new Map<string, string[]>();
  for (const link of links) targets.set(link.source, [...(targets.get(link.source) ?? []), link.target]);
  const layerOf = assignLayers(ids, targets);
  const top = Math.max(...layerOf.values());
  const layers: Unit[][] = Array.from({ length: top + 1 }, () => []);
  for (const id of ids) layers[layerOf.get(id) ?? 0].push(byId.get(id)!);

  const inner = Math.max(NODE_MIN_WIDTH, (options.maxWidth ?? DEFAULT_MAX_WIDTH) - 2 * MARGIN);
  const content = Math.min(inner, Math.max(...layers.map(rowWidth)));
  const centre = new Map<string, number>();
  const placed = new Map<string, PlacedNode>();
  const rowsOf: Unit[][][] = [];
  for (let layer = 0; layer <= top; layer++) {
    const wanted = new Map<string, number>();
    if (layer > 0) {
      for (const unit of layers[layer]) {
        // Only what lies below counts: an edge of a cycle points to a higher layer.
        const below = (targets.get(unit.id) ?? []).filter((t) => (layerOf.get(t) ?? 0) < layer).map((t) => centre.get(t) ?? 0);
        wanted.set(unit.id, below.reduce((sum, x) => sum + x, 0) / Math.max(1, below.length));
      }
      layers[layer].sort((a, b) => (wanted.get(a.id) ?? 0) - (wanted.get(b.id) ?? 0) || compare(a.id, b.id));
    }
    const rows = wrapRows(layers[layer], inner);
    rowsOf.push(rows);
    for (const row of rows) {
      // The lowest layer has nothing to stand above: its rows are centred.
      const middle = MARGIN + content / 2;
      const xs = layer > 0 ? placeRow(row, wanted, MARGIN, MARGIN + content) : placeRow(row, new Map(), middle - rowWidth(row) / 2, MARGIN + content);
      row.forEach((unit, index) => {
        const x = Math.max(MARGIN, xs[index]);
        centre.set(unit.id, x + unit.width / 2);
        placed.set(unit.id, {
          id: unit.id,
          x,
          y: 0,
          width: unit.width,
          height: NODE_HEIGHT,
          label: unit.label,
          badge: unit.badge,
          layer,
          ...(unit.group ? { group: unit.group } : {}),
        });
      });
    }
  }

  const ordered: PlacedNode[] = [];
  let y = MARGIN;
  for (let layer = top; layer >= 0; layer--) {
    for (const row of rowsOf[layer]) {
      for (const unit of row) {
        const node = placed.get(unit.id)!;
        node.y = y;
        ordered.push(node);
      }
      y += NODE_HEIGHT + ROW_GAP;
    }
    y += LAYER_GAP - ROW_GAP;
  }
  return {
    nodes: ordered,
    edges: links.map((link) => ({ ...link, points: curve(placed.get(link.source)!, placed.get(link.target)!) })),
    width: Math.max(...ordered.map((n) => n.x + n.width)) + MARGIN,
    height: y - LAYER_GAP + MARGIN,
    collapsible,
  };
}
