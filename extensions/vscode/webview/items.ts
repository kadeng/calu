/**
 * Elements of the transcript: one function per item kind, and the head card of a subagent tab.
 *
 *     Item (state) ──itemDeps()───► the objects its look depends on (compared by identity:
 *                                   the reducer never mutates, so an unchanged item is the
 *                                   same object and its element is reused)
 *                  ──renderItem()─► element with `data-action` attributes
 *
 * Nothing here handles events: every clickable element carries `data-action` (and `data-*`
 * arguments), which the delegated handlers of `main.ts` interpret. All texts from calu are set
 * as text nodes or rendered through `markdown()`.
 *
 * What a click on a tool row does is decided by `rowClick` / `fileClick` in
 * `src/shared/itemMenu.ts` (spec section 11): an edit row opens its diff, the file name opens
 * the file at the edited line, a read row opens the file, a command row expands in place, a
 * subagent launch opens its tab.
 */

import {
  decisionKind,
  imageSrc,
  lookupFile,
  middleTruncate,
  oneLine,
  previewLines,
  showsErrorChip,
  splitByFiles,
  splitPath,
  toolHead,
  turnCost,
  turnLabel,
  type FileIndex,
} from '../src/shared/chatModel';
import { fileClick, menuFor, rowClick, toolKind } from '../src/shared/itemMenu';
import type { ChatSettings } from '../src/shared/messages';
import {
  MAIN,
  checkerLabel,
  honestyRowChip,
  isAgentLaunch,
  literalToolInput,
  own,
  type EscalationView,
  type HonestyResultView,
  type Item,
  type QuestionView,
  type ToolView,
  type ViewState,
} from '../src/shared/viewState';
import { actionButton, actionLink, el, icon, iconButton, shown, type IconName } from './dom';
import { markdown } from './markdown';

/** What the user chose and wrote on an open escalation card (kept while the card is rebuilt). */
export interface EscalationDraft {
  note: string;
  /** Per question: the chosen option labels and the free text. */
  choices: { labels: string[]; text: string }[];
}

/** What the item renderers read. `main.ts` owns the object and replaces `view` on every event. */
export interface Ctx {
  view: ViewState;
  settings: ChatSettings;
  /** True if requests can be sent to a session (buttons that need one are disabled otherwise). */
  attached: boolean;
  /** Ids of items (and pseudo ids of head cards) whose full text is shown. */
  expanded: Set<string>;
  /** Tool calls whose details are shown. */
  openTools: Set<string>;
  /** Drafts of open escalation cards by escalation key. */
  escDrafts: Record<string, EscalationDraft>;
  /** The files that tool calls named (cached per `view.tools`). */
  fileIndex(): FileIndex;
  /**
   * The steps calu holds back now (`heldTools`) and their number per agent key (`heldByAgent`),
   * cached per state.
   */
  held(): { tools: Set<string>; byAgent: Record<string, number> };
}

/** Number of lines of thinking shown before "Show all" (finished) or as the tail (streaming). */
const THINKING_PREVIEW_LINES = 4;
/** Number of lines of a task, system message or subagent result shown before "Show all". */
const LONG_TEXT_PREVIEW_LINES = 12;
/** Number of lines of a tool result shown before "Show all". */
const RESULT_PREVIEW_LINES = 12;

/**
 * Return the draft of an escalation card, creating an empty one if needed.
 *
 * @param ctx The render context (its `escDrafts` may get a new entry).
 * @param esc The escalation.
 * @returns The draft, with one `choices` entry per question.
 */
export function draftOf(ctx: Ctx, esc: EscalationView): EscalationDraft {
  let draft = Object.hasOwn(ctx.escDrafts, esc.key) ? ctx.escDrafts[esc.key] : undefined;
  if (!draft) {
    draft = { note: '', choices: [] };
    Object.defineProperty(ctx.escDrafts, esc.key, { value: draft, enumerable: true, writable: true, configurable: true });
  }
  while (draft.choices.length < esc.questions.length) draft.choices.push({ labels: [], text: '' });
  return draft;
}

/**
 * List what the look of an item depends on. Two calls that return identical entries (`===`)
 * mean the cached element can be reused.
 *
 * @param item The item.
 * @param ctx The render context.
 * @returns The item itself, the tool or escalation it refers to, and the view-only flags that
 *   change its look (expanded, details open, attached).
 */
export function itemDeps(item: Item, ctx: Ctx): unknown[] {
  const expanded = ctx.expanded.has(String(item.id));
  switch (item.kind) {
    case 'tool': {
      const tool = own(ctx.view.tools, item.toolUseId);
      const agent = tool && isAgentLaunch(tool.name) ? ctx.view.agents.find((a) => a.agent_id === tool.toolUseId) : undefined;
      const held = ctx.held();
      return [item, tool, agent, ctx.openTools.has(item.toolUseId), expanded, held.tools.has(item.toolUseId), agent ? (held.byAgent[agent.agent_id] ?? 0) : 0];
    }
    case 'escalation':
      return [item, own(ctx.view.escalations, item.escalationKey), ctx.attached];
    case 'user':
      return [item, expanded, ctx.attached, ctx.view.turnBase];
    case 'relay':
      return [item, ctx.view.agents.find((a) => a.agent_id === item.to)];
    default:
      return [item, expanded];
  }
}

/**
 * Build the element of one transcript item.
 *
 * @param item The item (not a "launch" item: the head card of a subagent tab replaces it).
 * @param ctx The render context.
 * @returns The element, with `data-testid="item-<id>"` (tool rows and escalation cards carry
 *   their own test id in addition).
 */
export function renderItem(item: Item, ctx: Ctx): HTMLElement {
  const node = build(item, ctx);
  node.dataset.itemId = String(item.id);
  if (!node.dataset.testid) node.dataset.testid = `item-${item.id}`;
  return node;
}

/**
 * Dispatch on the item kind (body of `renderItem`).
 *
 * @param item The item.
 * @param ctx The render context.
 * @returns The element.
 */
function build(item: Item, ctx: Ctx): HTMLElement {
  switch (item.kind) {
    case 'user':
      return renderUser(item, ctx);
    case 'assistant': {
      const box = el('div', `item assistant${item.streaming ? ' streaming' : ''}${item.stored ? ' stored' : ''}`);
      box.append(markdown(item.text, 'md', (text) => lookupFile(ctx.fileIndex(), text)));
      return box;
    }
    case 'thinking':
      return renderThinking(item, ctx);
    case 'tool':
      return renderTool(own(ctx.view.tools, item.toolUseId), ctx);
    case 'checker':
      return renderChecker(item);
    case 'escalation':
      return renderEscalation(own(ctx.view.escalations, item.escalationKey), ctx);
    case 'turn':
      return renderTurn(item);
    case 'notice':
      return el('div', `item notice ${item.level === 'error' || item.level === 'warning' ? item.level : 'info'}`, item.text, { role: 'note' });
    case 'launch':
      return el('div', 'item hidden');
    case 'relay':
      return renderRelay(item, ctx);
    case 'divider':
      return renderDivider(item);
  }
}

// ------------------------------------------------------------------ messages

/**
 * Build a "Show all (N lines)" / "Show less" toggle.
 *
 * @param id Key in `Ctx.expanded`.
 * @param expanded Whether the full text is shown now.
 * @param lines Number of lines of the full text.
 * @returns The button.
 */
function expandToggle(id: string, expanded: boolean, lines: number): HTMLElement {
  return actionButton(expanded ? 'Show less' : `Show all (${lines} lines)`, 'expand', {
    class: 'text-button',
    'data-id': id,
    'aria-expanded': String(expanded),
  });
}

/**
 * Build a user message. The user's own words are a bubble with the actions "Rewind" and "Fork"
 * (visible on hover and focus; a click on an attached image shows it larger); a message of
 * another session or of calu / Claude Code is labelled as what it is and shown quieter, long
 * ones cut to a preview. A message the user sent to a subagent (in that subagent's tab) has no
 * actions. A recorded message (`stored`) looks quieter.
 *
 * @param item The user item.
 * @param ctx The render context.
 * @returns The element.
 */
function renderUser(item: Extract<Item, { kind: 'user' }>, ctx: Ctx): HTMLElement {
  if (item.source !== 'user') {
    const box = el('div', `item foreign ${item.source}`);
    const who = item.source === 'cross_session' ? 'From another Claude session (not from you)' : 'System message (not from you)';
    box.append(el('div', 'who', who));
    const id = String(item.id);
    const expanded = ctx.expanded.has(id);
    const preview = previewLines(item.text, LONG_TEXT_PREVIEW_LINES, 'head');
    box.append(el('div', 'text', expanded ? item.text : preview.text + (preview.truncated ? '\n…' : '')));
    if (preview.truncated) box.append(expandToggle(id, expanded, preview.lines));
    return box;
  }
  const box = el('div', `item user${item.stored ? ' stored' : ''}${item.to ? ' to-agent' : ''}`);
  const bubble = el('div', 'bubble');
  if (item.images.length > 0) {
    const row = el('div', 'thumbs');
    for (const image of item.images) {
      const src = image.omitted ? undefined : imageSrc(image.mediaType, image.data);
      if (!src) {
        row.append(el('span', 'thumb-missing', image.omitted ? '[image too large to show]' : '[image]'));
        continue;
      }
      const img = el('img', 'thumb', undefined, { alt: 'Attached image', loading: 'lazy' });
      img.src = src;
      const zoom = actionButton('', 'zoomImage', { class: 'thumb-button', title: 'Show the image larger', 'aria-label': 'Show the attached image larger' });
      zoom.append(img);
      row.append(zoom);
    }
    bubble.append(row);
  }
  if (item.text) bubble.append(el('div', 'text', item.text));
  box.append(bubble);
  // A message for a subagent is relayed in a turn of the main agent: nothing to rewind to here.
  if (item.to) {
    box.append(el('div', 'user-note', 'Sent to this subagent; the main agent relays it.'));
    return box;
  }
  const actions = el('div', 'user-actions');
  const data: Record<string, string> = { 'data-turn': String(item.turn ?? 1), ...(item.uuid ? { 'data-uuid': item.uuid } : {}) };
  const rewind = iconButton('undo', 'Rewind to before this message', 'rewind', {
    ...data,
    title: 'Rewind to before this message: a new session continues from before this turn (files are not reverted)',
  });
  const fork = iconButton('fork', 'Fork from here', 'forkHere', {
    ...data,
    title: 'Fork from here: a new session continues from before this turn; this one keeps running',
  });
  // The first message of the conversation has nothing before it to go back to.
  const first = ctx.view.turnBase !== null && item.turn <= 1;
  if (first) {
    rewind.title = fork.title = 'This is the first message of the conversation: nothing is before it. Start a new session instead.';
  }
  rewind.disabled = fork.disabled = !ctx.attached || first;
  actions.append(rewind, fork);
  box.append(actions);
  return box;
}

/**
 * Build the line of Main that stands for a message the user sent to a background subagent: who
 * it went to, as a link to that subagent's tab (which shows the text).
 *
 * @param item The relay item.
 * @param ctx The render context.
 * @returns The element.
 */
function renderRelay(item: Extract<Item, { kind: 'relay' }>, ctx: Ctx): HTMLElement {
  const agent = ctx.view.agents.find((a) => a.agent_id === item.to);
  const name = agent?.description || agent?.subagent_type || item.to.slice(-6);
  const box = el('div', 'item relay', undefined, {
    role: 'note',
    title: 'You wrote to this background subagent. The main agent relays the message to it; the text is in the subagent\'s tab.',
  });
  box.append(icon('agent'), el('span', '', 'Sent to subagent'), actionLink(name, 'tab', { 'data-tab': item.to, title: "Open the subagent's tab, which shows the message" }));
  return box;
}

/**
 * Build the divider above or below the recorded turns of a conversation.
 *
 * @param item The divider item.
 * @returns The element (`role="separator"`).
 */
function renderDivider(item: Extract<Item, { kind: 'divider' }>): HTMLElement {
  const box = el('div', `item divider ${item.mark}`, undefined, { role: 'separator' });
  box.append(el('span', 'divider-text', item.text));
  return box;
}

/**
 * Build a thinking item: quiet, labelled, Markdown. While streaming it shows the last lines;
 * when finished, the first ones with a "Show all" toggle.
 *
 * @param item The thinking item.
 * @param ctx The render context.
 * @returns The element.
 */
function renderThinking(item: Extract<Item, { kind: 'thinking' }>, ctx: Ctx): HTMLElement {
  const box = el('div', `item thinking${item.streaming ? ' streaming' : ''}`);
  const who = el('div', 'who');
  who.append(icon('thinking'), item.streaming ? 'Thinking…' : 'Thinking');
  box.append(who);
  const id = String(item.id);
  const expanded = ctx.expanded.has(id);
  const preview = previewLines(item.text, THINKING_PREVIEW_LINES, item.streaming ? 'tail' : 'head');
  const shown = expanded || !preview.truncated ? item.text : item.streaming ? `…\n\n${preview.text}` : `${preview.text}\n\n…`;
  box.append(markdown(shown, 'md'));
  if (preview.truncated && !item.streaming) box.append(expandToggle(id, expanded, preview.lines));
  return box;
}

/**
 * Build a checker's remark (or the reason of a denial) with a link to its instructions.
 *
 * @param item The checker item.
 * @returns The element.
 */
function renderChecker(item: Extract<Item, { kind: 'checker' }>): HTMLElement {
  const kind = decisionKind(item.decision);
  const box = el('div', `item checker ${kind}`);
  const who = el('div', 'who');
  who.append(el('span', `chip decision ${kind}`, item.decision), el('span', '', item.label));
  if (item.checkerRoute) {
    who.append(actionLink('Edit instructions', 'editChecker', { 'data-route': item.checkerRoute, title: "Open this checker's instructions" }));
  }
  box.append(who, markdown(item.text, 'md'));
  return box;
}

/**
 * Build a turn separator: a thin line with the outcome, the cost and the background tasks.
 *
 * @param item The turn item.
 * @returns The element (`role="separator"`).
 */
function renderTurn(item: Extract<Item, { kind: 'turn' }>): HTMLElement {
  const parts = [item.stored ? 'end of the turn' : turnLabel(item)];
  const cost = turnCost(item);
  if (cost) parts.push(cost);
  if (item.backgroundTasks > 0) parts.push(`${item.backgroundTasks} background task${item.backgroundTasks === 1 ? '' : 's'} running`);
  const box = el('div', `item turn${item.isError ? ' error' : ''}`, undefined, { role: 'separator' });
  box.append(el('span', 'turn-text', parts.join(' · ')));
  return box;
}

// ------------------------------------------------------------------ tool rows

/** Icon per kind of tool. */
const TOOL_ICONS: Record<string, IconName> = { edit: 'edit', read: 'file', command: 'terminal', other: 'tool' };

/**
 * Build the row of a tool call: icon, tool name, the head of its input (a file name is a
 * separate link), the checker's decision chip (a click shows the details) and a menu button;
 * when expanded, the checker's reason, the file links, the input and the result below.
 *
 * @param tool The tool view (`undefined` if the state no longer holds it).
 * @param ctx The render context.
 * @returns The element, with `data-testid="tool-<toolUseId>"` and `data-tool-id`.
 */
function renderTool(tool: ToolView | undefined, ctx: Ctx): HTMLElement {
  if (!tool) return el('div', 'item notice info', '(This tool call is no longer in the chat.)');
  const kind = toolKind(tool);
  const launch = isAgentLaunch(tool.name);
  const open = ctx.openTools.has(tool.toolUseId);
  const click = rowClick(tool);
  const box = el('div', `item tool ${launch ? 'launch' : kind}${open ? ' open' : ''}${tool.stored ? ' stored' : ''}`, undefined, {
    'data-tool-id': tool.toolUseId,
    'data-testid': `tool-${tool.toolUseId}`,
  });
  // A recorded step has nothing to open but its own details.
  const row = el('div', 'tool-row', undefined, {
    role: 'button',
    tabindex: '0',
    'data-action': tool.stored ? 'toggleTool' : 'rowClick',
    'data-id': tool.toolUseId,
    title: tool.stored ? 'From the record: show what it holds about this step' : click.title,
    ...(click.kind === 'expand' ? { 'aria-expanded': String(open) } : {}),
  });
  row.append(icon(launch ? 'agent' : TOOL_ICONS[kind], 'tool-icon'), el('span', 'tool-name', tool.name), toolHeadElement(tool));
  const decision = decisionKind(tool.check?.decision);
  if (showsErrorChip(tool)) row.append(el('span', 'chip decision deny', 'error'));
  if (tool.stored?.subagent) row.append(el('span', 'chip', 'subagent', { title: 'A subagent made this call' }));
  if (launch && !tool.stored) row.append(agentChip(tool, ctx));
  if (tool.check) {
    row.append(
      actionButton(tool.check.decision, 'toggleTool', {
        class: `chip decision ${decision}`,
        'data-id': tool.toolUseId,
        title: `${checkerLabel(tool.check.route)}: ${tool.check.decision}${tool.check.reason ? ` — ${oneLine(tool.check.reason, 200)}` : ''}. Click for the reason.`,
      }),
    );
  }
  const held = ctx.held().tools.has(tool.toolUseId);
  if (held) {
    row.append(
      el('span', 'chip decision held', 'held', {
        title: tool.check
          ? 'Approved, but calu holds this step back until the open decision about another step is made.'
          : 'calu holds this step back until the open decision about another step is made; it is checked after that.',
      }),
    );
  } else if (!tool.check && !tool.result && !tool.stored && !(launch && subagentRuns(tool, ctx))) {
    row.append(el('span', 'chip decision pending', '…', { title: 'Not checked yet' }));
  }
  const honesty = honestyRowChip(tool.honesty);
  if (honesty) row.append(el('span', 'chip honesty', honesty.label, { title: honesty.title }));
  // One control at the end of the row: the menu (which also shows the details) if the tool has
  // one, else a details toggle, else the mark of a row that expands on click.
  if (menuFor(tool) && !tool.stored) {
    row.append(iconButton('more', 'Actions for this step', 'toolMenu', { class: 'quiet', 'data-id': tool.toolUseId, 'aria-haspopup': 'menu' }));
  } else if (click.kind !== 'expand' && !tool.stored) {
    row.append(
      iconButton(open ? 'chevronDown' : 'chevronRight', open ? 'Hide details' : 'Show details', 'toggleTool', {
        class: 'quiet',
        'data-id': tool.toolUseId,
        'aria-expanded': String(open),
      }),
    );
  } else {
    row.append(icon(open ? 'chevronDown' : 'chevronRight', 'row-chevron'));
  }
  box.append(row);
  if (open) box.append(toolBody(tool, ctx));
  return box;
}

/**
 * Build the status chip of a subagent launch row.
 *
 * @param tool The Agent/Task tool view.
 * @param ctx The render context.
 * @returns A chip such as "running" or "completed"; "background" is appended for a background
 *   subagent.
 */
function agentChip(tool: ToolView, ctx: Ctx): HTMLElement {
  const agent = ctx.view.agents.find((a) => a.agent_id === tool.toolUseId);
  const status = agent?.status ?? (tool.result ? 'completed' : 'running');
  const held = status === 'running' ? (ctx.held().byAgent[tool.toolUseId] ?? 0) : 0;
  const shownStatus = held > 0 ? 'held' : status;
  const title = held > 0 ? `running; ${held} of its steps held until the open decision is made` : status;
  return el('span', `chip agent-status ${shownStatus}`, agent?.background ? `${shownStatus} · bg` : shownStatus, {
    title: agent?.background ? `${title}, runs in the background` : title,
  });
}

/**
 * Tell whether the subagent an Agent/Task call launched is still running.
 *
 * @param tool The Agent/Task tool view.
 * @param ctx The render context.
 * @returns True if `agents.update` lists it as running, or (not listed) the call has no result.
 */
function subagentRuns(tool: ToolView, ctx: Ctx): boolean {
  const agent = ctx.view.agents.find((a) => a.agent_id === tool.toolUseId);
  return (agent?.status ?? (tool.result ? 'completed' : 'running')) === 'running';
}

/**
 * Build the head of a tool row: the start of its input. For edits and reads it is the file
 * name as a link of its own (cut in the middle, full path in the tooltip); for other tools the
 * text with the files it names as links.
 *
 * @param tool The tool view.
 * @returns The element with the class `tool-head`.
 */
function toolHeadElement(tool: ToolView): HTMLElement {
  const head = toolHead(tool);
  const file = fileClick(tool);
  // A recorded step without its input shows what the checker said the step does.
  if (tool.stored && !tool.stored.hasInput) {
    const summary = tool.check?.summary ?? '';
    return el('span', 'tool-head summary', summary, { title: summary });
  }
  if (head.isPath) {
    // The directory shrinks first; the file name is cut only when nothing else is left.
    const path = splitPath(head.text);
    const parts = [el('span', 'path-dir', path.dir), el('span', 'path-base', path.base)];
    if (!file || tool.stored) {
      const span = el('span', 'tool-head path', undefined, { title: head.text });
      span.append(...parts);
      return span;
    }
    const link = actionLink('', 'fileClick', { class: 'tool-head path file-link', 'data-id': tool.toolUseId, title: `${file.title}\n${head.text}` });
    link.append(...parts);
    if (tool.editRange && toolKind(tool) === 'edit') link.append(el('span', 'line-range', `:${tool.editRange.start}`));
    return link;
  }
  const span = el('span', 'tool-head', undefined, { title: head.text });
  for (const part of splitByFiles(head.text, tool.files)) {
    if (part.file === undefined) span.append(shown(part.text));
    else span.append(toolFileLink(tool, part.file, part.text));
  }
  return span;
}

/**
 * Build a link that opens file `index` of a tool call (`ToolView.files`).
 *
 * @param tool The tool view.
 * @param index Index into `tool.files`.
 * @param text The link text.
 * @returns The link element.
 */
function toolFileLink(tool: ToolView, index: number, text: string): HTMLElement {
  return actionLink(text, 'openToolFile', {
    class: 'file-link',
    'data-id': tool.toolUseId,
    'data-index': String(index),
    title: `Open file\n${tool.files?.[index]?.path ?? text}`,
  });
}

/**
 * Build the honesty checker's part of a tool's details: one line per file with its status, the
 * dishonest units under it, and the escalation reason. It has no decision chip.
 *
 * @param honesty The step's honesty result.
 * @returns The element.
 */
function honestyBlock(honesty: HonestyResultView): HTMLElement {
  const box = el('div', 'check honesty-result');
  box.append(el('div', 'check-line', 'Honesty checker'));
  for (const file of honesty.files) {
    const row = el('div', `honesty-file${file.honest ? '' : ' dishonest'}`);
    row.append(el('span', 'honesty-path', file.path), el('span', 'honesty-verdict', file.honest ? 'honest' : 'dishonest'));
    if (!file.honest && file.reason) row.append(el('span', 'check-reason', file.reason));
    box.append(row);
    for (const unit of file.dishonestUnits) box.append(el('div', 'honesty-unit', `${unit.symbol}: ${unit.reason}`));
  }
  if (honesty.escalation) box.append(el('div', 'check-reason honesty-ask', `Asks a person to look: ${honesty.escalation}`));
  return box;
}

/**
 * Build the details of a tool row.
 *
 * @param tool The tool view.
 * @param ctx The render context.
 * @returns The element with the checker's decision, the files, the input and the result.
 */
function toolBody(tool: ToolView, ctx: Ctx): HTMLElement {
  const body = el('div', 'tool-body');
  if (tool.check) {
    const kind = decisionKind(tool.check.decision);
    const check = el('div', `check ${kind}`);
    const line = el('div', 'check-line');
    line.append(el('span', `chip decision ${kind}`, tool.check.decision), el('span', 'check-who', checkerLabel(tool.check.route)));
    if (tool.check.checkerRoute) {
      line.append(actionLink("Open this checker's instructions", 'editChecker', { 'data-route': tool.check.checkerRoute }));
    }
    check.append(line);
    if (tool.check.reason) check.append(el('div', 'check-reason', tool.check.reason));
    if (tool.check.summary) check.append(el('div', 'check-summary', tool.check.summary));
    if (tool.check.comment) check.append(el('div', 'check-comment', tool.check.comment));
    body.append(check);
  }
  if (tool.honesty) body.append(honestyBlock(tool.honesty));
  const links = el('div', 'tool-links');
  if (tool.diffPath !== undefined) links.append(actionLink('Open diff', 'openDiff', { 'data-id': tool.toolUseId, title: `Open the diff of ${tool.diffPath}` }));
  (tool.files ?? []).forEach((f, i) => links.append(toolFileLink(tool, i, middleTruncate(f.path, 60))));
  if (links.childElementCount > 0) body.append(links);
  const command = typeof tool.input.command === 'string' ? tool.input.command : undefined;
  if (tool.stored && !tool.stored.hasInput) {
    body.append(el('div', 'tool-note', 'From calu\'s record of an earlier session: it holds the tool, the decision and the summary of this step, not its input or result.'));
  } else {
    body.append(el('div', 'label', command !== undefined ? 'Command' : 'Input'));
    body.append(el('pre', 'tool-input', command ?? JSON.stringify(tool.input, null, 2)));
  }
  if (tool.result) {
    const id = `result:${tool.toolUseId}`;
    const expanded = ctx.expanded.has(id);
    const preview = previewLines(tool.result.content, RESULT_PREVIEW_LINES, 'head');
    body.append(el('div', 'label', tool.stored ? 'Result (its beginning, from the record)' : tool.result.isError ? 'Result (error)' : 'Result'));
    body.append(el('pre', `tool-result${tool.result.isError ? ' error' : ''}`, expanded ? tool.result.content : preview.text || '(empty)'));
    if (preview.truncated) body.append(expandToggle(id, expanded, preview.lines));
  }
  return body;
}

// ------------------------------------------------------------------ escalations

/** Text of a closed escalation's outcome chip. */
const OUTCOMES: Record<EscalationView['status'], string> = {
  open: 'Not decided',
  allow: 'Allowed',
  deny: 'Denied',
  cancelled: 'Cancelled',
  answered: 'Answered',
};

/**
 * Build an escalation card. Open: the tool, the reason, the rendered step, the literal input on
 * expand, the agent's questions (if any), a note field and the buttons. The row of buttons is
 * the card's last child and sticks to the lower edge of the transcript (`.esc-actions` in
 * `chat.css`), so Allow and Deny stay reachable while a card taller than the view scrolls.
 * While the session cannot be reached the card stays open, says "connection lost" and its
 * buttons are disabled. Closed: one line with the outcome that expands to the details.
 *
 * @param esc The escalation view (`undefined` if the state no longer holds it).
 * @param ctx The render context.
 * @returns The element, with `data-testid="escalation-<key>"`, `data-esc` and `data-status`.
 */
function renderEscalation(esc: EscalationView | undefined, ctx: Ctx): HTMLElement {
  if (!esc) return el('div', 'item notice info', '(This escalation is no longer in the chat.)');
  const attrs = { 'data-testid': `escalation-${esc.key}`, 'data-esc': esc.key, 'data-status': esc.status };
  if (esc.status !== 'open') return renderClosedEscalation(esc, attrs);
  const asks = esc.questions.length > 0;
  const card = el('section', `item esc open${esc.pending ? ' pending' : ''}${ctx.attached ? '' : ' offline'}`, undefined, {
    ...attrs,
    role: 'group',
    'aria-label': asks ? 'Claude asks' : `Approval needed: ${esc.toolName}`,
  });
  const title = el('div', 'esc-title');
  title.append(icon(asks ? 'question' : 'pause'), el('span', '', asks ? 'Claude asks' : 'Approval needed'));
  if (!asks) title.append(el('span', 'chip', esc.toolName));
  if (!ctx.attached) {
    title.append(
      el('span', 'chip offline', 'connection lost', {
        title: 'The session cannot be reached, so nothing can be decided here now. The step still waits in the session; attach again to decide.',
      }),
    );
  }
  card.append(title);
  // calu's reason for a question is "Claude asks: <the question> (<the options>)": the card
  // shows the question and the options itself, so the reason would say it twice.
  if (esc.reason && !(asks && esc.reason.startsWith('Claude asks:'))) card.append(el('div', 'esc-reason', esc.reason));
  const draft = draftOf(ctx, esc);
  if (asks) esc.questions.forEach((q, i) => card.append(questionElement(esc, q, i, draft.choices[i], Boolean(esc.pending))));
  else if (esc.rendered) card.append(markdown(esc.rendered, 'md esc-step'));
  card.append(literalDetails(esc));
  const note = el('input', 'esc-note', undefined, {
    type: 'text',
    placeholder: asks ? 'Note (optional)' : 'Note for Claude (optional, e.g. why you deny)',
    'aria-label': 'Note for Claude',
    'data-esc-note': esc.key,
  });
  note.value = draft.note;
  note.disabled = Boolean(esc.pending);
  if (!asks) card.append(note);
  const row = el('div', 'esc-actions');
  const disabled = Boolean(esc.pending) || !ctx.attached;
  if (asks) {
    const answer = actionButton('Send answer', 'answer', { class: 'primary', 'data-key': esc.key, 'data-testid': `escalation-answer-${esc.key}` });
    const decline = actionButton('Decline', 'decide', { class: 'secondary', 'data-key': esc.key, 'data-decision': 'deny', 'data-testid': `escalation-deny-${esc.key}` });
    answer.disabled = decline.disabled = disabled;
    row.append(answer, decline);
  } else {
    const allow = actionButton('Allow', 'decide', { class: 'primary', 'data-key': esc.key, 'data-decision': 'allow', 'data-testid': `escalation-allow-${esc.key}` });
    const deny = actionButton('Deny', 'decide', { class: 'secondary', 'data-key': esc.key, 'data-decision': 'deny', 'data-testid': `escalation-deny-${esc.key}` });
    allow.disabled = deny.disabled = disabled;
    row.append(allow, deny);
  }
  const why = actionButton('Why?', 'why', {
    class: 'text-button',
    'data-key': esc.key,
    'data-testid': `escalation-why-${esc.key}`,
    title: 'Open the context the checker had for this step',
  });
  why.disabled = !ctx.attached;
  row.append(why);
  if (esc.pending) row.append(el('span', 'esc-pending', 'Sending…', { role: 'status' }));
  else if (!ctx.attached) row.append(el('span', 'esc-pending', 'Not connected', { role: 'status' }));
  card.append(row);
  return card;
}

/**
 * Build one question of the agent: its text, the options as toggle buttons (one may be chosen,
 * or several with `multiSelect`) and a field for another answer.
 *
 * @param esc The escalation.
 * @param question The question.
 * @param index Its index in `esc.questions`.
 * @param choice The draft for this question.
 * @param disabled True while the answer is being sent.
 * @returns A `fieldset`.
 */
function questionElement(
  esc: EscalationView,
  question: QuestionView,
  index: number,
  choice: { labels: string[]; text: string },
  disabled: boolean,
): HTMLElement {
  const field = el('fieldset', 'question');
  if (question.header) field.append(el('legend', 'question-header', question.header));
  field.append(el('div', 'question-text', question.question));
  if (question.options.length > 0) {
    const options = el('div', 'options', undefined, { role: question.multiSelect ? 'group' : 'radiogroup', 'aria-label': question.question });
    for (const option of question.options) {
      const chosen = choice.labels.includes(option.label);
      const b = actionButton('', 'escOption', {
        class: 'option',
        role: question.multiSelect ? 'checkbox' : 'radio',
        'aria-checked': String(chosen),
        'data-key': esc.key,
        'data-q': String(index),
        'data-label': option.label,
        'data-multi': String(question.multiSelect),
      });
      b.append(el('span', 'option-label', option.label));
      if (option.description) b.append(el('span', 'option-description', option.description));
      b.disabled = disabled;
      options.append(b);
    }
    field.append(options);
    if (question.multiSelect) field.append(el('div', 'hint', 'Several may be chosen.'));
  }
  const other = el('input', 'question-other', undefined, {
    type: 'text',
    placeholder: question.options.length > 0 ? 'Another answer (optional)' : 'Your answer',
    'aria-label': `Answer to: ${question.question}`,
    'data-esc-answer': esc.key,
    'data-q': String(index),
  });
  other.value = choice.text;
  other.disabled = disabled;
  field.append(other);
  return field;
}

/**
 * Build the expandable literal input of an escalation: plain text, so that Markdown in calu's
 * rendering cannot hide or fake what is approved.
 *
 * @param esc The escalation.
 * @returns A `details` element titled "Exact input".
 */
function literalDetails(esc: EscalationView): HTMLElement {
  const details = el('details', 'esc-literal');
  details.append(el('summary', '', 'Exact input'));
  for (const [label, text] of literalToolInput(esc.toolInput ?? {})) {
    details.append(el('div', 'label', label), el('pre', '', text));
  }
  return details;
}

/**
 * Build a closed escalation: one line with the outcome, the tool and the start of its input;
 * expanding it shows the reason, the step and the literal input.
 *
 * @param esc The escalation (status not "open").
 * @param attrs The data attributes of the card.
 * @returns A `details` element (the caller keeps it expanded across re-renders).
 */
function renderClosedEscalation(esc: EscalationView, attrs: Record<string, string>): HTMLElement {
  const card = el('details', `item esc closed ${esc.status}`, undefined, attrs);
  const summary = el('summary', 'esc-line');
  const first = esc.questions.length > 0 ? esc.questions[0].question : (literalToolInput(esc.toolInput ?? {})[0]?.[1] ?? '');
  summary.append(
    el('span', `chip decision ${esc.status === 'allow' || esc.status === 'answered' ? 'allow' : esc.status === 'deny' ? 'deny' : 'pending'}`, OUTCOMES[esc.status]),
    el('span', 'tool-name', esc.questions.length > 0 ? 'Question' : esc.toolName),
    el('span', 'esc-line-text', oneLine(first, 160)),
  );
  // What the user said is the part of a decided card one looks for: on the line itself.
  const said = esc.answer ?? esc.note ?? '';
  if (said) summary.append(el('span', 'esc-line-note', `"${oneLine(said, 80)}"`, { title: esc.answer ? 'Your answer' : 'Your note' }));
  card.append(summary);
  if (esc.answer) card.append(el('div', 'esc-outcome', `Your answer: ${esc.answer}`));
  if (esc.note) card.append(el('div', 'esc-outcome', `Your note: ${esc.note}`));
  if (esc.reason) card.append(el('div', 'esc-reason', esc.reason));
  if (esc.rendered) card.append(markdown(esc.rendered, 'md esc-step'));
  card.append(literalDetails(esc));
  return card;
}

// ------------------------------------------------------------------ subagent tab

/**
 * Build the head card of a subagent tab: the task the subagent was given and, once its
 * launching call returned, what it returned. Long texts are cut to a preview with a toggle.
 *
 * @param tab The subagent's id.
 * @param ctx The render context.
 * @returns The card, or `undefined` for the main tab and for a subagent whose launch was not
 *   seen and that has no result.
 */
export function renderSubagentHead(tab: string, ctx: Ctx): HTMLElement | undefined {
  if (tab === MAIN) return undefined;
  const sub = own(ctx.view.subagents, tab);
  const agent = ctx.view.agents.find((a) => a.agent_id === tab);
  if (!sub && !agent) return undefined;
  const card = el('section', 'item agent-head', undefined, { 'data-testid': `agent-head-${tab}`, 'aria-label': 'Task of this subagent' });
  const title = el('div', 'agent-title');
  const running = agent?.status ?? 'running';
  const held = running === 'running' ? (ctx.held().byAgent[tab] ?? 0) : 0;
  const status = held > 0 ? 'held' : running;
  title.append(
    icon('agent'),
    el('span', 'agent-name', sub?.description || agent?.description || 'Subagent'),
    el('span', 'chip', sub?.subagentType ?? agent?.subagent_type ?? 'subagent'),
    el('span', `chip agent-status ${status}`, status, held > 0 ? { title: `running; ${held} of its steps held until the open decision is made` } : {}),
  );
  if (sub?.background ?? agent?.background) title.append(el('span', 'chip', 'background'));
  card.append(title);
  card.append(el('div', 'label', 'Task'));
  card.append(...longMarkdown(sub?.task || '(The task was not recorded.)', `task:${tab}`, ctx));
  if (sub?.result) {
    const label = sub.result.final ? (sub.result.isError ? 'Failed with' : 'Returned') : 'Launch answer (the result reaches the main agent later)';
    card.append(el('div', `label${sub.result.isError ? ' error' : ''}`, label));
    card.append(...longMarkdown(sub.result.content || '(empty)', `agent-result:${tab}`, ctx));
  }
  return card;
}

/**
 * Return what the head card of a subagent tab depends on (see `itemDeps`).
 *
 * @param tab The subagent's id.
 * @param ctx The render context.
 * @returns The subagent's entries of the state and the expand flags of the card.
 */
export function subagentHeadDeps(tab: string, ctx: Ctx): unknown[] {
  return [
    tab,
    own(ctx.view.subagents, tab),
    ctx.view.agents.find((a) => a.agent_id === tab),
    ctx.expanded.has(`task:${tab}`),
    ctx.expanded.has(`agent-result:${tab}`),
    ctx.held().byAgent[tab] ?? 0,
  ];
}

/**
 * Render a possibly long Markdown text with a "Show all" toggle.
 *
 * @param text The Markdown source.
 * @param id Key in `Ctx.expanded`.
 * @param ctx The render context.
 * @returns The rendered text and, if it was cut, the toggle.
 */
function longMarkdown(text: string, id: string, ctx: Ctx): HTMLElement[] {
  const expanded = ctx.expanded.has(id);
  const preview = previewLines(text, LONG_TEXT_PREVIEW_LINES, 'head');
  const body = markdown(expanded || !preview.truncated ? text : `${preview.text}\n\n…`, 'md');
  return preview.truncated ? [body, expandToggle(id, expanded, preview.lines)] : [body];
}
