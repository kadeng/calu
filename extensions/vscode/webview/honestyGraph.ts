/**
 * The honesty graph of the chat webview: the honesty state of the attached session as a
 * layered graph, drawn as SVG in place of the transcript (browser context).
 *
 *     main.ts render() ──update({visible, attached, sessionId, honesty})──► HonestyGraphView
 *         hidden         nothing is asked, nothing is drawn, the elements are removed
 *         shown          asks `honestyGraphGet` when it opens, when `honesty` changes and when
 *                        the session changes (`GraphRefresh`: one request at a time)
 *     host "honestyGraph" ──receive()──► readGraph ──► settle (ghosts) ──► layoutGraph ──► SVG
 *
 *     root .honesty-graph
 *       .hg-bar      notes (review running, one node per file, …) | − + Fit
 *       .hg-notice   the text of a state without a graph (off, clean, error with "Try again")
 *       .hg-canvas   the scrolling area with the <svg>: drag pans, Ctrl + wheel zooms
 *       .hg-legend   the five states and what they mean
 *       .hg-tip      the tooltip of the node under the pointer or with the focus
 *
 * A click on a node opens its source; a right click, the context-menu key or Shift+F10 opens
 * its menu (`nodeMenu`). The menu is a popup of `popup.ts`: `main.ts` hands a click on one of
 * its entries to `menuAction`. All text from calu is set as text, never as HTML. Colours are
 * in `media/chat.css`; the script sets sizes and places as SVG attributes.
 */

import type { HonestyInfo } from '../src/protocol.generated';
import { layoutGraph, type GraphLayout, type PlacedNode } from '../src/shared/honestyGraphLayout';
import {
  GRAPH_STATES,
  GraphRefresh,
  STATE_TEXTS,
  graphNotes,
  graphNotice,
  hiddenCallersText,
  nodeLabel,
  nodeMenu,
  readGraph,
  settle,
  sourceHref,
  withGhosts,
  type Ghost,
  type GraphData,
  type GraphNode,
} from '../src/shared/honestyGraphModel';
import type { HostMessage, WebviewMessage } from '../src/shared/messages';
import { actionButton, el, shown } from './dom';
import { closePopup, openPopup, openPopupId, type PopupPlace } from './popup';

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Smallest and largest zoom factor. */
const ZOOM_MIN = 0.25;
const ZOOM_MAX = 3;
/** Factor of one click on "+" (and its inverse for "−"). */
const ZOOM_STEP = 1.25;
/** Pixels the pointer moves before a press on the canvas becomes a drag. */
const DRAG_START = 4;
/** The layout is computed again when the view's width changed by this many pixels. */
const WIDTH_STEP = 24;
/** Name of the node menu for `popup.ts`. */
const MENU_ID = 'graphNode';

/** What the view needs from the chat's state at every render. */
export interface GraphInput {
  /** True while the graph takes the place of the transcript. */
  visible: boolean;
  /** Whether a session is attached. */
  attached: boolean;
  /** Id of the attached session, or `null`. */
  sessionId: string | null;
  /** The honesty state of the session; a new object after every `honesty.update`. */
  honesty: HonestyInfo | null;
  /**
   * How many tool calls of the session have a result. A finished call can change the graph
   * without changing `honesty`: a new caller of a dishonest unit, or one more changed
   * function in a file that was unreviewed already.
   */
  steps: number;
}

/** The elements of the view while it is shown. */
interface Elements {
  notes: HTMLElement;
  notice: HTMLElement;
  canvas: HTMLElement;
  svg: SVGSVGElement;
  legend: HTMLElement;
  tip: HTMLElement;
  buttons: HTMLButtonElement[];
  observer: ResizeObserver;
}

/**
 * Create an SVG element with attributes.
 *
 * @param tag Tag name.
 * @param attrs Attributes, set as given.
 * @returns The element.
 */
function svgEl<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string> = {}): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/**
 * Create an SVG `<text>` with a text from calu (set as text, passed through `shown`).
 *
 * @param cls Class attribute.
 * @param x Left end of the text.
 * @param y Vertical centre of the text.
 * @param text The text.
 * @returns The element.
 */
function svgText(cls: string, x: number, y: number, text: string): SVGTextElement {
  const node = svgEl('text', { class: cls, x: String(x), y: String(y), 'dominant-baseline': 'central' });
  node.textContent = shown(text);
  return node;
}

/** The honesty graph view. Create one per page, for the element that holds it. */
export class HonestyGraphView {
  private els: Elements | undefined;
  private input: GraphInput = { visible: false, attached: false, sessionId: null, honesty: null, steps: 0 };
  /** The last graph of the session shown, or `null` before the first answer. */
  private graph: GraphData | null = null;
  /** The error of the last request, if it failed. */
  private error: string | undefined;
  private ghosts: Ghost[] = [];
  private ghostTimer: ReturnType<typeof setTimeout> | undefined;
  /** The targets whose collapsed callers the user expanded. */
  private readonly expanded = new Set<string>();
  /** The session the graph and the expanded groups belong to. */
  private session: string | null = null;
  /** The honesty state the graph was last asked for; `undefined` if it was not asked for. */
  private asked: HonestyInfo | null | undefined;
  /** `GraphInput.steps` when the graph was last asked for. */
  private steps = -1;
  private zoom = 1;
  /** The width the layout was computed for. */
  private width = 0;
  private layout: GraphLayout | undefined;
  /** The nodes drawn, ghosts included, by id. */
  private nodes = new Map<string, GraphNode>();
  /** The placed node of each drawn `<g>`. */
  private placed = new WeakMap<Element, PlacedNode>();
  /** Counts the changes of graph, ghosts and expanded groups; part of `drawn`. */
  private version = 0;
  /** Signature of what is drawn, so that a render without a change costs one comparison. */
  private drawn = '';
  /** True after a drag, until the click that ends it was dropped. */
  private dragged = false;
  /** True while a redraw gives the focus back to a node. */
  private refocusing = false;
  private readonly refresh: GraphRefresh;
  private readonly reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  /**
   * @param root The element that takes the transcript's place; the view fills and empties it.
   * @param post Sends a message to the host.
   */
  constructor(
    private readonly root: HTMLElement,
    private readonly post: (message: WebviewMessage) => void,
  ) {
    this.refresh = new GraphRefresh(() => this.post({ type: 'honestyGraphGet' }));
  }

  // ---------------------------------------------------------------- state

  /**
   * Take over the chat's state (called at every render).
   *
   * Side effects: while `visible` is false the view removes its elements, stops its timers and
   * asks nothing. While it is true, a session is attached and code honesty is on, the view
   * asks the host for the graph (`honestyGraphGet`) when it opens, when the session changes,
   * when `honesty` is a new object and when `steps` changed; never while the page is hidden.
   *
   * @param input The state.
   * @returns Nothing.
   */
  update(input: GraphInput): void {
    this.input = input;
    if (!input.visible) {
      this.close();
      return;
    }
    if (!this.els) this.els = this.build();
    if (input.sessionId !== this.session) {
      this.session = input.sessionId;
      this.forget();
      this.expanded.clear();
    }
    const mode = input.honesty?.mode ?? 'off';
    if (!input.attached || mode === 'off') {
      this.refresh.stop();
      this.forget();
    } else if (!document.hidden && (this.asked !== input.honesty || this.steps !== input.steps)) {
      const first = this.asked === undefined;
      this.asked = input.honesty;
      this.steps = input.steps;
      this.refresh.want(first);
    }
    this.draw();
  }

  /**
   * Take over the host's answer to `honestyGraphGet`.
   *
   * Side effects: draws the new graph; nodes that were settled since the graph before are
   * shown once more for `SETTLED_MS` (not with reduced motion). A failed request shows its
   * error. A graph of another session than the attached one is dropped: the change of the
   * session already asked for the right one. An answer that arrives while the view is closed
   * is dropped.
   *
   * @param message The `honestyGraph` message.
   * @returns Nothing.
   */
  receive(message: Extract<HostMessage, { type: 'honestyGraph' }>): void {
    this.refresh.answered();
    if (!this.els || this.asked === undefined) return;
    if (message.graph === null || message.error !== undefined) {
      this.graph = null;
      this.ghosts = [];
      this.error = message.error ?? 'the session gave no answer';
    } else {
      if (message.sessionId !== this.session) return;
      const next = readGraph(message.graph);
      this.ghosts = this.reducedMotion.matches ? [] : settle(this.graph, next, this.ghosts, Date.now());
      this.graph = next;
      this.error = undefined;
    }
    this.version += 1;
    this.draw();
    this.watchGhosts();
  }

  /**
   * Do what a click on an entry of the node menu asks (`main.ts` calls it for
   * `data-action="graphMenu"`).
   *
   * Side effects: "expand" and "collapse" change which callers are shown one by one; any other
   * entry sends its messages to the host (`nodeMenu`).
   *
   * @param op The entry: `data-op` of the button.
   * @param id The node: `data-id` of the button.
   * @returns Nothing; an unknown node or entry does nothing.
   */
  menuAction(op: string, id: string): void {
    if (op === 'expand' || op === 'collapse') {
      if (op === 'expand') this.expanded.add(id);
      else this.expanded.delete(id);
      this.version += 1;
      this.draw();
      return;
    }
    const node = this.nodes.get(id);
    const item = node && nodeMenu(node).find((i) => i.id === op);
    for (const message of item?.messages ?? []) this.post(message);
  }

  /**
   * Drop the graph, its error and its ghosts, so that the next visible render asks again.
   *
   * @returns Nothing.
   */
  private forget(): void {
    if (this.graph !== null || this.error !== undefined || this.ghosts.length > 0) this.version += 1;
    this.graph = null;
    this.error = undefined;
    this.ghosts = [];
    this.asked = undefined;
    if (this.ghostTimer !== undefined) clearTimeout(this.ghostTimer);
    this.ghostTimer = undefined;
  }

  /**
   * Remove the view's elements and stop everything that costs while it is hidden: the pending
   * request, the timer of the ghosts, the size observer, an open node menu.
   *
   * @returns Nothing.
   */
  private close(): void {
    if (!this.els) return;
    this.els.observer.disconnect();
    this.els = undefined;
    this.refresh.stop();
    this.forget();
    this.layout = undefined;
    this.nodes = new Map();
    this.drawn = '';
    if (openPopupId() === MENU_ID) closePopup(false);
    this.root.replaceChildren();
  }

  /**
   * Arrange that ghosts are removed when their time is over.
   *
   * @returns Nothing. Side effect: one timer for the ghost that leaves first.
   */
  private watchGhosts(): void {
    if (this.ghostTimer !== undefined) clearTimeout(this.ghostTimer);
    this.ghostTimer = undefined;
    if (this.ghosts.length === 0) return;
    const first = Math.min(...this.ghosts.map((g) => g.until));
    this.ghostTimer = setTimeout(
      () => {
        this.ghostTimer = undefined;
        this.ghosts = this.ghosts.filter((g) => g.until > Date.now());
        this.version += 1;
        this.draw();
        this.watchGhosts();
      },
      Math.max(0, first - Date.now()) + 20,
    );
  }

  // ---------------------------------------------------------------- elements

  /**
   * Build the view's elements inside the root and connect their handlers.
   *
   * @returns The elements.
   */
  private build(): Elements {
    const notes = el('div', 'hg-notes', undefined, { role: 'status', 'data-testid': 'honesty-graph-notes' });
    const zoomOut = this.barButton('−', 'Zoom out', () => this.zoomBy(1 / ZOOM_STEP));
    const zoomIn = this.barButton('+', 'Zoom in', () => this.zoomBy(ZOOM_STEP));
    const fit = this.barButton('Fit', 'Fit the whole graph into the view', () => this.fit());
    const tools = el('div', 'hg-tools');
    tools.append(zoomOut, zoomIn, fit);
    const bar = el('div', 'hg-bar');
    bar.append(notes, tools);
    const notice = el('div', 'hg-notice', undefined, { role: 'status', 'data-testid': 'honesty-graph-notice' });
    const canvas = el('div', 'hg-canvas', undefined, { 'data-testid': 'honesty-graph-canvas' });
    const svg = svgEl('svg', { class: 'hg-svg', role: 'group', 'aria-label': 'Honesty graph: callers above the units they use' });
    canvas.append(svg);
    const tip = el('div', 'hg-tip', undefined, { role: 'tooltip' });
    tip.hidden = true;
    const legend = this.buildLegend();

    svg.addEventListener('click', (event) => this.onClick(event));
    svg.addEventListener('keydown', (event) => this.onKeydown(event));
    svg.addEventListener('contextmenu', (event) => {
      const node = this.nodeAt(event.target);
      if (!node) return;
      event.preventDefault();
      this.openMenu(node.placed, { x: event.clientX, y: event.clientY });
    });
    svg.addEventListener('pointerover', (event) => {
      const node = this.nodeAt(event.target);
      if (node) this.showTip(node.placed, event.clientX, event.clientY + 14);
    });
    svg.addEventListener('pointerout', (event) => {
      const from = (event.target as Element).closest('.hg-node');
      if (from && !(event.relatedTarget instanceof Node && from.contains(event.relatedTarget))) this.hideTip();
    });
    svg.addEventListener('focusin', (event) => {
      const node = this.nodeAt(event.target);
      if (!node || this.refocusing) return;
      const rect = node.element.getBoundingClientRect();
      this.showTip(node.placed, rect.left, rect.bottom + 4);
    });
    svg.addEventListener('focusout', () => this.hideTip());
    canvas.addEventListener('scroll', () => this.hideTip());
    canvas.addEventListener('pointerdown', (event) => this.startDrag(event));
    canvas.addEventListener('wheel', (event) => this.onWheel(event), { passive: false });
    // A side bar that is dragged wider or narrower gets a layout for its new width.
    const observer = new ResizeObserver(() => {
      const width = this.root.clientWidth;
      if (Math.abs(width - this.width) < WIDTH_STEP) return;
      this.width = width;
      this.draw();
    });
    observer.observe(this.root);
    this.width = this.root.clientWidth;
    this.root.replaceChildren(bar, notice, canvas, legend, tip);
    return { notes, notice, canvas, svg, legend, tip, buttons: [zoomOut, zoomIn, fit], observer };
  }

  /**
   * Create a button of the bar.
   *
   * @param label The button's text.
   * @param name Accessible name and tooltip.
   * @param act What a click does.
   * @returns The button.
   */
  private barButton(label: string, name: string, act: () => void): HTMLButtonElement {
    const button = el('button', 'hg-button', label, { type: 'button', title: name, 'aria-label': name });
    button.addEventListener('click', act);
    return button;
  }

  /**
   * Build the legend: the five states with glyph, word and meaning, and what a dashed edge and
   * square corners mean. Closed, it shows the glyphs and words; opened, the meanings too.
   *
   * @returns A `<details>` element.
   */
  private buildLegend(): HTMLElement {
    const legend = el('details', 'hg-legend', undefined, { 'data-testid': 'honesty-graph-legend' });
    const summary = el('summary', '', undefined, { title: 'What the colours and signs mean' });
    summary.append(el('span', 'hg-legend-title', 'Legend'));
    const list = el('dl', 'hg-meanings');
    for (const state of GRAPH_STATES) {
      const texts = STATE_TEXTS[state];
      const key = el('span', `hg-key ${state}`, undefined, { title: texts.meaning });
      key.append(el('span', 'hg-key-glyph', texts.glyph, { 'aria-hidden': 'true' }), el('span', '', texts.word));
      summary.append(key);
      list.append(el('dt', `hg-key ${state}`, `${texts.glyph} ${texts.word}`), el('dd', '', texts.meaning));
    }
    list.append(
      el('dt', '', 'pulsing ring'),
      el('dd', '', 'A running honesty review reads the file.'),
      el('dt', '', 'square corners'),
      el('dd', '', 'A whole file; a number says how many units it stands for.'),
      el('dt', '', 'dashed line'),
      el('dd', '', 'A use that was found by the name alone and that nothing confirmed.'),
      el('dt', '', 'green, then gone'),
      el('dd', '', 'The unit was just settled.'),
    );
    legend.append(summary, list);
    return legend;
  }

  // ---------------------------------------------------------------- drawing

  /**
   * Show what the state asks for: a text, or the graph. Does nothing if nothing changed since
   * the last call.
   *
   * @returns Nothing.
   */
  private draw(): void {
    const els = this.els;
    if (!els) return;
    const { attached, honesty } = this.input;
    const mode = honesty?.mode ?? 'off';
    const reviewing = honesty?.review_running === true || this.graph?.reviewRunning === true;
    const signature = JSON.stringify([attached, mode, honesty?.clean === true, reviewing, this.error ?? null, this.version, this.width]);
    if (signature === this.drawn) return;
    this.drawn = signature;
    const notice = graphNotice(attached, mode, this.graph, this.error, honesty?.clean === true, this.ghosts.length);
    const graph = notice.kind === 'graph' ? this.graph : null;
    els.notice.hidden = graph !== null;
    els.canvas.hidden = graph === null;
    els.legend.hidden = graph === null;
    for (const button of els.buttons) button.disabled = graph === null;
    els.notes.replaceChildren(...(graph ? graphNotes(graph, reviewing) : reviewing ? ['Honesty review running…'] : []).map((line) => el('div', '', line)));
    this.hideTip();
    if (!graph) {
      this.layout = undefined;
      this.nodes = new Map();
      els.svg.replaceChildren();
      const children: HTMLElement[] = [el('p', '', notice.kind === 'graph' ? '' : notice.text)];
      if (notice.kind === 'error') {
        const again = el('button', 'secondary', 'Try again', { type: 'button', 'data-testid': 'honesty-graph-retry' });
        again.addEventListener('click', () => {
          this.error = undefined;
          this.version += 1;
          this.refresh.want(true);
          this.draw();
        });
        children.push(again);
      }
      els.notice.className = `hg-notice ${notice.kind}`;
      els.notice.replaceChildren(...children);
      return;
    }
    this.drawGraph(els, graph);
  }

  /**
   * Lay the graph out and replace the SVG's content. The node that had the focus gets it again.
   *
   * @param els The view's elements.
   * @param graph The graph to draw.
   * @returns Nothing.
   */
  private drawGraph(els: Elements, graph: GraphData): void {
    const { nodes, edges } = withGhosts(graph, this.ghosts);
    const layout = layoutGraph(nodes, edges, { expanded: this.expanded, maxWidth: Math.max(240, this.width) });
    const focused = this.nodeAt(document.activeElement)?.placed.id;
    this.layout = layout;
    this.nodes = new Map(nodes.map((n) => [n.id, n]));
    this.placed = new WeakMap();
    const lines = svgEl('g', { class: 'hg-edges', 'aria-hidden': 'true' });
    for (const edge of layout.edges) {
      const [a, b, c, d] = edge.points;
      lines.append(
        svgEl('path', { class: `hg-edge${edge.certain ? '' : ' uncertain'}`, d: `M${a.x} ${a.y}C${b.x} ${b.y} ${c.x} ${c.y} ${d.x} ${d.y}` }),
        // The arrow head: the curve ends vertically, pointing down or up.
        svgEl('path', { class: 'hg-arrow', d: d.y >= c.y ? `M${d.x - 4} ${d.y - 6}L${d.x} ${d.y}L${d.x + 4} ${d.y - 6}z` : `M${d.x - 4} ${d.y + 6}L${d.x} ${d.y}L${d.x + 4} ${d.y + 6}z` }),
      );
    }
    const boxes = svgEl('g', { class: 'hg-nodes' });
    let again: SVGGElement | undefined;
    for (const placed of layout.nodes) {
      const element = this.nodeElement(placed);
      this.placed.set(element, placed);
      if (placed.id === focused) again = element;
      boxes.append(element);
    }
    els.svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`);
    els.svg.replaceChildren(lines, boxes);
    this.applyZoom();
    // The focus goes back to the node that had it, without its tooltip appearing again.
    this.refocusing = true;
    again?.focus({ preventScroll: true });
    this.refocusing = false;
  }

  /**
   * Build the SVG group of one node: the pulsing ring of a review, the pill (square corners
   * for a file or a group) on a background in the page's colour, the glyph of the state, the
   * label and the count.
   *
   * @param placed The node with its place.
   * @returns A focusable `<g role="button">` with an `aria-label` that names the state.
   */
  private nodeElement(placed: PlacedNode): SVGGElement {
    const node = this.nodes.get(placed.id);
    const state = node?.state ?? 'caller';
    const target = placed.group ? this.nodes.get(placed.group.target) : undefined;
    const label = node
      ? nodeLabel(node)
      : `${placed.label} of ${target?.name ?? placed.group?.target ?? ''}, collapsed. Enter shows them one by one.`;
    const classes = ['hg-node', state, ...(node?.kind === 'file' ? ['file'] : []), ...(placed.group ? ['group'] : []), ...(node?.reviewing ? ['reviewing'] : [])];
    const g = svgEl('g', {
      class: classes.join(' '),
      transform: `translate(${placed.x} ${placed.y})`,
      tabindex: '0',
      role: 'button',
      'aria-label': shown(label),
      'data-state': state,
    });
    const corner = node?.kind === 'file' || placed.group ? 3 : placed.height / 2;
    if (node?.reviewing) {
      g.append(svgEl('rect', { class: 'hg-ring', x: '-4', y: '-4', width: String(placed.width + 8), height: String(placed.height + 8), rx: String(corner + 4) }));
    }
    const box = { width: String(placed.width), height: String(placed.height), rx: String(corner) };
    // The shape is tinted, not filled: a rectangle in the page's colour keeps edges from showing through.
    g.append(svgEl('rect', { class: 'hg-back', ...box }), svgEl('rect', { class: 'hg-shape', ...box }));
    const glyph = svgText('hg-glyph', 12, placed.height / 2, placed.group ? '+' : STATE_TEXTS[state].glyph);
    glyph.setAttribute('text-anchor', 'middle');
    g.append(glyph, svgText('hg-label', 24, placed.height / 2, placed.label));
    if (placed.badge) {
      const badge = svgText('hg-count', placed.width - 10, placed.height / 2, placed.badge);
      badge.setAttribute('text-anchor', 'end');
      g.append(badge);
    }
    return g;
  }

  /**
   * Find the drawn node an event target belongs to.
   *
   * @param target An event target or the focused element.
   * @returns The node's `<g>` and its place, or `undefined` if the target is not inside a node.
   */
  private nodeAt(target: EventTarget | null): { element: SVGGElement; placed: PlacedNode } | undefined {
    const element = target instanceof Element ? target.closest<SVGGElement>('.hg-node') : null;
    const placed = element ? this.placed.get(element) : undefined;
    return element && placed ? { element, placed } : undefined;
  }

  // ---------------------------------------------------------------- pan and zoom

  /**
   * Set the SVG's size from the layout and the zoom factor.
   *
   * @returns Nothing.
   */
  private applyZoom(): void {
    if (!this.els || !this.layout) return;
    this.els.svg.setAttribute('width', String(Math.round(this.layout.width * this.zoom)));
    this.els.svg.setAttribute('height', String(Math.round(this.layout.height * this.zoom)));
  }

  /**
   * Zoom by a factor around a point of the window: what is under the point stays there.
   *
   * @param factor Above 1 to zoom in, below 1 to zoom out; the zoom stays in its limits.
   * @param clientX The point's x; default: the middle of the canvas.
   * @param clientY The point's y; default: the middle of the canvas.
   * @returns Nothing.
   */
  private zoomBy(factor: number, clientX?: number, clientY?: number): void {
    const canvas = this.els?.canvas;
    if (!canvas || !this.layout) return;
    const before = this.zoom;
    const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, before * factor));
    if (next === before) return;
    const rect = canvas.getBoundingClientRect();
    const offsetX = (clientX ?? rect.left + rect.width / 2) - rect.left;
    const offsetY = (clientY ?? rect.top + rect.height / 2) - rect.top;
    const atX = canvas.scrollLeft + offsetX;
    const atY = canvas.scrollTop + offsetY;
    this.zoom = next;
    this.hideTip();
    this.applyZoom();
    canvas.scrollLeft = (atX * next) / before - offsetX;
    canvas.scrollTop = (atY * next) / before - offsetY;
  }

  /**
   * Choose the zoom at which the whole graph is visible (never above 1) and scroll to its
   * upper left corner.
   *
   * @returns Nothing.
   */
  private fit(): void {
    const canvas = this.els?.canvas;
    if (!canvas || !this.layout || this.layout.width === 0) return;
    const wanted = Math.min(1, canvas.clientWidth / this.layout.width, canvas.clientHeight / this.layout.height);
    this.zoom = Math.max(ZOOM_MIN, wanted);
    this.applyZoom();
    canvas.scrollLeft = 0;
    canvas.scrollTop = 0;
  }

  /**
   * Zoom around the pointer for Ctrl (or Cmd) + wheel; a plain wheel scrolls the canvas as usual.
   *
   * @param event The wheel event.
   * @returns Nothing.
   */
  private onWheel(event: WheelEvent): void {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const pixels = event.deltaMode === WheelEvent.DOM_DELTA_PIXEL ? event.deltaY : event.deltaY * 16;
    this.zoomBy(Math.exp(-pixels * 0.0015), event.clientX, event.clientY);
  }

  /**
   * Start a drag that pans the canvas (primary button). The click that ends a drag is dropped,
   * so that panning from a node does not open its source.
   *
   * @param event The pointerdown event on the canvas.
   * @returns Nothing. Side effect: window listeners until the button is released.
   */
  private startDrag(event: PointerEvent): void {
    const canvas = this.els?.canvas;
    if (!canvas || event.button !== 0) return;
    const start = { x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
    this.dragged = false;
    const move = (e: PointerEvent): void => {
      const dx = e.clientX - start.x;
      const dy = e.clientY - start.y;
      if (!this.dragged && Math.abs(dx) + Math.abs(dy) < DRAG_START) return;
      this.dragged = true;
      canvas.classList.add('panning');
      this.hideTip();
      canvas.scrollLeft = start.left - dx;
      canvas.scrollTop = start.top - dy;
    };
    const end = (): void => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      canvas.classList.remove('panning');
      // The click of this release comes next; a release outside a node brings none.
      if (this.dragged) setTimeout(() => (this.dragged = false), 0);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  }

  // ---------------------------------------------------------------- nodes: click, keys, menu, tooltip

  /**
   * Open a node's source, or expand a group of callers.
   *
   * @param placed The node.
   * @returns Nothing. Side effect: an `openLink` message for a node with a file.
   */
  private activate(placed: PlacedNode): void {
    if (placed.group) {
      this.menuAction('expand', placed.group.target);
      return;
    }
    const node = this.nodes.get(placed.id);
    if (node?.path) this.post({ type: 'openLink', href: sourceHref(node.path, node.line) });
  }

  /**
   * Handle a click in the graph: on a node it opens the source (or expands a group).
   *
   * @param event The click event.
   * @returns Nothing.
   */
  private onClick(event: MouseEvent): void {
    if (this.dragged) {
      this.dragged = false;
      return;
    }
    const node = this.nodeAt(event.target);
    if (node) this.activate(node.placed);
  }

  /**
   * Handle a key on a focused node: Enter or Space acts like a click; the context-menu key or
   * Shift+F10 opens the menu.
   *
   * @param event The keydown event.
   * @returns Nothing.
   */
  private onKeydown(event: KeyboardEvent): void {
    const node = this.nodeAt(event.target);
    if (!node) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      this.activate(node.placed);
    } else if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault();
      const rect = node.element.getBoundingClientRect();
      this.openMenu(node.placed, { x: rect.left + 12, y: rect.bottom });
    }
  }

  /**
   * Open the context menu of a node: its name and state, then the entries of `nodeMenu`; for a
   * group "Show the N callers"; for a unit whose callers were expanded "Collapse its N callers".
   *
   * @param placed The node.
   * @param place Where the menu opens.
   * @returns Nothing.
   */
  private openMenu(placed: PlacedNode, place: PopupPlace): void {
    this.hideTip();
    const node = this.nodes.get(placed.id);
    const box = el('div', 'menu graph-menu', undefined, { role: 'menu', 'aria-label': `Actions for ${node?.name ?? placed.label}` });
    const entry = (label: string, op: string, id: string, title: string): HTMLButtonElement =>
      actionButton(label, 'graphMenu', { class: 'menu-item', role: 'menuitem', 'data-op': op, 'data-id': id, title });
    if (placed.group) {
      box.append(
        el('div', 'menu-heading', placed.label, { role: 'presentation' }),
        entry(`Show the ${placed.group.members.length} callers`, 'expand', placed.group.target, 'Show each caller as a node of its own'),
      );
    } else if (node) {
      const texts = STATE_TEXTS[node.state];
      box.append(
        el('div', 'menu-heading', node.name, { role: 'presentation' }),
        el('div', 'menu-note', `${texts.word}: ${texts.meaning}`, { role: 'presentation' }),
      );
      for (const item of nodeMenu(node)) box.append(entry(item.label, item.id, node.id, item.title));
      const callers = this.layout?.collapsible.get(node.id);
      if (callers && this.expanded.has(node.id)) {
        box.append(entry(`Collapse its ${callers.length} callers`, 'collapse', node.id, 'Show the callers as one node again'));
      }
    }
    openPopup(MENU_ID, box, place);
  }

  /**
   * Show the tooltip of a node: its full id, its state in words, its detail text and how
   * many of its callers the graph leaves out.
   *
   * @param placed The node.
   * @param x Left edge of the tooltip in the window, before it is moved into the window.
   * @param y Upper edge of the tooltip in the window.
   * @returns Nothing.
   */
  private showTip(placed: PlacedNode, x: number, y: number): void {
    const tip = this.els?.tip;
    if (!tip || openPopupId() === MENU_ID) return;
    const node = this.nodes.get(placed.id);
    if (node) {
      const texts = STATE_TEXTS[node.state];
      const lines = [el('div', 'hg-tip-id', node.id), el('div', 'hg-tip-state', `${texts.word}: ${texts.meaning}`)];
      if (node.reviewing) lines.push(el('div', '', 'A running honesty review reads this file.'));
      if (node.detail) lines.push(el('div', 'hg-tip-detail', node.detail));
      const hidden = hiddenCallersText(node);
      if (hidden) lines.push(el('div', '', `${hidden}.`));
      tip.replaceChildren(...lines);
    } else if (placed.group) {
      const target = this.nodes.get(placed.group.target);
      tip.replaceChildren(
        el('div', 'hg-tip-state', `${placed.label} of ${target?.name ?? placed.group.target}. A click shows them one by one.`),
        el('div', 'hg-tip-detail', placed.group.members.slice(0, 8).join('\n') + (placed.group.members.length > 8 ? '\n…' : '')),
      );
    } else return;
    tip.hidden = false;
    const margin = 4;
    const top = y + tip.offsetHeight + margin > window.innerHeight ? y - tip.offsetHeight - 36 : y;
    tip.style.left = `${Math.max(margin, Math.min(x, window.innerWidth - tip.offsetWidth - margin))}px`;
    tip.style.top = `${Math.max(margin, top)}px`;
  }

  /**
   * Hide the tooltip.
   *
   * @returns Nothing.
   */
  private hideTip(): void {
    if (this.els) this.els.tip.hidden = true;
  }
}
