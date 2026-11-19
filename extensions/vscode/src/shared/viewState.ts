/**
 * State of the chat view and the pure reducer that applies events to it.
 *
 * Used in two places with the same code:
 * - the extension host keeps the authoritative copy (it receives every event, also while the
 *   webview is hidden) and sends it to the webview when the webview (re)loads;
 * - the webview applies the same events to its copy for rendering.
 *
 *     calu ide ──session.event──► host: applyEvent(state, event) ──{type:"event"}──► webview: applyEvent
 *     host (ui.* / bridge.status events of its own) ──────────────────same path──────►
 *     ide.attach / ide.start result ──► host: `ui.attached` event (snapshot + recorded events)
 *
 * No `vscode` and no DOM dependency. `applyEvent` never mutates its input state.
 *
 * Maps keyed by ids from calu (`items`, `tools`, `escalations`, `subagents`) are read only
 * through `own()` and written only through `withEntry()` / `withoutEntries()`, so that ids such
 * as "__proto__" are stored as ordinary keys and never reach `Object.prototype`.
 */

import type {
  AgentInfo,
  ChatQueue,
  CheckResult,
  EditDiff,
  EscalationRequest,
  EscalationResolved,
  HonestyInfo,
  HonestyMarkInfo,
  Log,
  RateLimit,
  SessionSnapshot,
  SessionState,
  StepHeld,
  ToolFiles,
  ToolResult,
  ToolUse,
  TurnResult,
  UserInput,
} from '../protocol.generated';
import { historyEntries, type HistoryEntry } from './history';
import type { AutoEscalateFlags } from './levels';
import { MAX_IMAGE_BASE64 } from './messages';
import type { ServerHelp } from './serverCommand';
import { lostCause, type LostCause } from './serverLoss';

/** Key of a message list: "main" for the main agent, otherwise the subagent's `agent_id`. */
export type AgentKey = string;

/** The main agent's key in `ViewState.items`. */
export const MAIN: AgentKey = 'main';

/** Maximum number of items kept per agent; older items (and what only they reference) are dropped. */
export const MAX_ITEMS_PER_AGENT = 1000;

/** Maximum stored length of a tool result and of each string inside a tool input. */
export const MAX_STORED_TEXT = 4000;

/** Maximum stored length of a subagent's result (it is the point of the subagent's tab). */
export const MAX_AGENT_RESULT_TEXT = 40000;

/** Most file links kept per tool call. */
export const MAX_TOOL_FILES = 50;

/**
 * Largest base64 text of one image kept in the state; larger images are shown as omitted. It
 * is the largest image a session accepts (`MAX_IMAGE_BASE64`), so every image the user could
 * attach is also shown.
 */
export const MAX_IMAGE_DATA = MAX_IMAGE_BASE64;

/** Most side questions kept (the oldest are dropped). */
export const MAX_SIDE_EXCHANGES = 20;

/**
 * State of the bridge process (`calu ide`) as seen by the extension.
 *
 * "missing": calu is not installed on the workspace machine (the view offers *Install*).
 */
export type BridgeStatus = 'stopped' | 'starting' | 'running' | 'exited' | 'error' | 'missing';

/** Connection between the bridge and a session: "detached" means no session is attached. */
export type LinkStatus = 'detached' | 'attached' | 'reconnecting' | 'lost';

/** State of a session as the protocol names it. */
export type SessionStateName = SessionState['state'];

/** A string-keyed map whose keys come from calu (see the module comment). */
export type IdMap<T> = Record<string, T>;

/** A tool call with its check decision and result, as far as they are known. */
export interface ToolView {
  toolUseId: string;
  agentKey: AgentKey;
  name: string;
  /** The tool input; strings longer than `MAX_STORED_TEXT` are cut (see `boundInput`). */
  input: Record<string, unknown>;
  /**
   * The routing and the checker's decision (`check.result`). `route`: "command", "edit",
   * "changeset", "subagent", "question", or a rule's route ("allow", "deny", "escalate").
   * `decision`: "PASS", "ALLOW", "DENY" or "ESCALATE". `checkerRoute` is the route to pass to
   * `editChecker {route}` ("" if a rule decided and no checker judged the step).
   */
  check?: { route: string; decision: string; reason: string; summary: string; comment: string; checkerRoute: string };
  /**
   * The honesty checker's result of the step (`check.result.honesty`, `honesty_escalation`);
   * absent if it did not judge the step. It is a status per file and no decision: the row never
   * shows it as a PASS or ALLOW chip (agent/records/PLAN.md D224).
   */
  honesty?: HonestyResultView;
  /** The result; `content` is cut to `MAX_STORED_TEXT` characters. */
  result?: { content: string; isError: boolean };
  /** Set when an `edit.diff` arrived for this tool call: the file path shown in the diff. */
  diffPath?: string;
  /** Changed lines of the edit in the new content (1-based, inclusive; end = start - 1 for a pure
   * deletion), from `edit.diff`; `null` if nothing changed or unknown. */
  editRange?: { start: number; end: number } | null;
  /** Commit id of the git HEAD when the step was checked (from `check.result`); `null` if the
   * workspace is not a git repository or the step was not checked yet. */
  gitHead?: string | null;
  /** Files the call refers to (`tool.files`): `raw` as written in the input, `path` absolute. */
  files?: { raw: string; path: string }[];
  /**
   * Set for a step taken from calu's record (`ui.history`) instead of from events: it has no
   * diff and no duration, `input` is `{}` if the record holds none, and `result` is only the
   * beginning of what the tool returned. `subagent`: a subagent made the call (the row is
   * shown in Main, because the record names no tab for it).
   */
  stored?: { subagent: boolean; hasInput: boolean };
}

/** The honesty checker's status of one file a step changes. */
export interface HonestyFileView {
  path: string;
  /** False for the verdict DISHONEST. */
  honest: boolean;
  /** For a dishonest file: what disagrees with what. */
  reason: string;
  /** The units the checker found dishonest, with their reasons. */
  dishonestUnits: { symbol: string; reason: string }[];
}

/** The honesty checker's result of one step. */
export interface HonestyResultView {
  files: HonestyFileView[];
  /** Its reason for asking a person, or "" if it did not ask. */
  escalation: string;
}

/** A chat message waiting to be taken in by Claude (shown above the input). */
export interface QueuedView {
  id: string;
  text: string;
  delivery: 'queued' | 'inject';
  /** "main" or the subagent's id. */
  to: string;
}

/** One question of the agent inside an escalation (`AskUserQuestion`). */
export interface QuestionView {
  /** The question text. */
  question: string;
  /** A short title; "" if none. */
  header: string;
  /** The offered answers; the user may also write another one. */
  options: { label: string; description: string }[];
  /** True if several options may be chosen. */
  multiSelect: boolean;
}

/**
 * An escalated tool call.
 *
 * `key` is the map key in `ViewState.escalations` and is unique within the state;
 * `escalationId` is calu's id. `status` is "open" until `escalation.resolved` arrives; a closed
 * escalation is never reopened.
 */
export interface EscalationView {
  key: string;
  escalationId: string;
  toolUseId: string | null;
  agentKey: AgentKey;
  toolName: string;
  /** The tool input exactly as calu sent it (strings cut, see `boundInput`). */
  toolInput: Record<string, unknown>;
  reason: string;
  /** Markdown rendering of the step by calu (display aid only; see `literalToolInput`). */
  rendered: string;
  /**
   * Questions of the agent; empty for every other escalation. With questions the card shows
   * the options and an answer field, and the user answers with `decide {answer}`.
   */
  questions: QuestionView[];
  /** ISO time the step began to wait; `null` if unknown. */
  openedAt: string | null;
  /** "answered": closed by the user's answer to the agent's questions. */
  status: 'open' | 'allow' | 'deny' | 'cancelled' | 'answered';
  /** True after the user decided, until `escalation.resolved` or `ui.decisionFailed`. */
  pending?: boolean;
  /** The answer text the user sent for the questions (kept after the card closed). */
  answer?: string;
  note?: string | null;
}

/** An image attached to a user message. */
export interface ImageView {
  mediaType: string;
  /** The image file, base64; "" if it was larger than `MAX_IMAGE_DATA`. */
  data: string;
  /** True if `data` was dropped because of its size. */
  omitted: boolean;
}

/** One entry of a message list. `id` is unique within a `ViewState`. */
export type Item =
  | {
      id: number;
      kind: 'user';
      text: string;
      /**
       * Who wrote the text: "user" (the human), "cross_session" (another Claude session on the
       * machine) or "system" (calu or Claude Code: a checker's note, the notification of a
       * finished background task, hook feedback). Only "user" is shown as the user's words.
       */
      source: 'user' | 'cross_session' | 'system';
      images: ImageView[];
      /**
       * Number of the turn the message belongs to: `turnBase + turnCount + 1` when the message
       * arrived, which is the conversation's own number once the recorded history was read
       * (`ViewState.turnBase`), and a count over the turns this view saw before that. For
       * `rewindBefore` / `forkBefore`; the host prefers the turn it finds for `uuid` in the
       * recorded history.
       */
      turn: number;
      /** Claude Code's id of the message (`user.input`.uuid); `null` if it gave none. */
      uuid: string | null;
      /**
       * Set in a subagent's tab for a message the user sent to that background subagent
       * (`user.input`.to): the subagent's id. The main agent relays such a message, so the
       * turn belongs to Main and the message has no rewind or fork action.
       */
      to?: string;
      /** True for a message taken from calu's record (`ui.history`), see `ToolView.stored`. */
      stored?: boolean;
    }
  | {
      id: number;
      kind: 'assistant';
      text: string;
      /** Id of the message in calu (from `assistant.delta` / `assistant.text`), if given. */
      messageId?: string;
      /** True while `assistant.delta` fragments arrive and the final `assistant.text` is due. */
      streaming?: boolean;
      /**
       * True if an attach found this block still streaming: the replay ended in the middle of
       * it, so later fragments and the final text of the same message continue this item.
       */
      cut?: boolean;
      /** True for the recorded answer of an earlier turn (`ui.history`). */
      stored?: boolean;
    }
  | {
      id: number;
      kind: 'thinking';
      /** Claude's (summarized) thinking, Markdown. */
      text: string;
      messageId?: string;
      streaming?: boolean;
      /** As for "assistant". */
      cut?: boolean;
    }
  | { id: number; kind: 'tool'; toolUseId: string }
  | {
      id: number;
      kind: 'checker';
      /** Display name of the checker, see `checkerLabel`. */
      label: string;
      /** Route for `editChecker {route}`; "" for a rule's decision. */
      checkerRoute: string;
      decision: string;
      /** The checker's remark (`comment`) or, for DENY, its `reason`. */
      text: string;
      toolUseId: string | null;
    }
  | { id: number; kind: 'escalation'; escalationKey: string }
  | {
      id: number;
      kind: 'turn';
      subtype: string;
      isError: boolean;
      /** What Claude Code reported at the end of the turn: the cost of the whole session so far. */
      costUsd: number | null;
      /**
       * The cost of this turn alone: `costUsd` minus the session's cost before the turn. `null`
       * when that earlier cost is not known (the first turn seen) or was higher (Claude Code
       * was started again and counts from zero).
       */
      costDelta?: number | null;
      /** Background tasks (subagents, shells) still running when the turn ended. */
      backgroundTasks: number;
      /** True for the end of a recorded turn (`ui.history`): outcome and cost are not recorded. */
      stored?: boolean;
    }
  | {
      id: number;
      kind: 'launch';
      /** The Agent/Task tool call that started the subagent (= the subagent's agent_id). */
      toolUseId: string;
      description: string;
      subagentType: string | null;
      background: boolean;
      /** The full launch prompt (Markdown); not cut. */
      prompt: string;
    }
  | { id: number; kind: 'notice'; level: string; text: string }
  | {
      id: number;
      kind: 'relay';
      /** The background subagent the user's message was sent to (its tab holds the text). */
      to: AgentKey;
    }
  | {
      id: number;
      kind: 'divider';
      /** "history-start": the recorded turns follow; "history-end": the session's own events follow. */
      mark: 'history-start' | 'history-end';
      text: string;
    };

/**
 * What is known about one subagent beyond `AgentInfo`: the task it was given (from the input of
 * the Agent/Task `tool.use` that launched it) and what it returned (that call's `tool.result`).
 */
export interface SubagentView {
  /** The subagent's id (= the launching tool call's id, = its key in `items`). */
  agentId: string;
  description: string;
  subagentType: string | null;
  /** True if it was launched with `run_in_background`. */
  background: boolean;
  /** The launch prompt (Markdown); not cut. */
  task: string;
  /**
   * The launching call's result, cut to `MAX_AGENT_RESULT_TEXT`. For a foreground subagent this
   * is its final answer (`final: true`). For a background subagent it is only the launch
   * acknowledgement (`final: false`); its answer reaches the main agent later as a "system"
   * user input.
   */
  result?: { content: string; isError: boolean; final: boolean };
}

/** One running session of this machine, for the session dropdown. */
export interface SessionEntry {
  id: string;
  /** Name given by the user; "" if none. */
  name: string;
  directory: string;
  /** Session state as in `session.state`. */
  state: string;
  openEscalations: number;
  /** True if the session works in this workspace folder (or an ancestor of it). */
  here: boolean;
  /** True if the session's process seems to be gone. */
  stale: boolean;
  /** When the session started (ISO time); `null` if not reported. */
  startedAt: string | null;
}

/** One side question (`side.ask`) with its answer; never part of the conversation. */
export interface SideExchangeView {
  id: number;
  question: string;
  /** Markdown answer; `undefined` while the request runs. */
  answer?: string;
  /** Why the question was not answered. */
  error?: string;
}

/**
 * Something the user should do before the chat can work, with the button that does it.
 *
 * `action` names a webview message type without fields ("install", "startServer",
 * "newSession", "attach", "openSettings", "restartBridge", "unlock"), or is "openLink" with `href`, or ""
 * for a hint without a button.
 */
export interface SetupHint {
  /**
   * Stable id: "calu-missing", "claude-missing", "locked", "database", "server-stopped",
   * "no-server", "no-session".
   */
  id: string;
  level: 'info' | 'warning' | 'error';
  /** One or two sentences that say what is missing and what to do. */
  message: string;
  action: '' | 'install' | 'startServer' | 'newSession' | 'attach' | 'openSettings' | 'restartBridge' | 'unlock' | 'openLink';
  /** Button text; "" if `action` is "". */
  actionLabel: string;
  /** For "openLink": the https address. */
  href?: string;
  /**
   * A few words for the one-line form of the hint (shown while a session is attached); the
   * whole `message` is then the tooltip. Missing: the message itself is cut.
   */
  short?: string;
  /** Button text of the one-line form; missing: `actionLabel`. */
  actionShort?: string;
}

/** Cost, context window and account limits of the attached session. */
export interface UsageView {
  /** One entry per limit Claude Code reported (the newest state of each). */
  rateLimits: RateLimit[];
  /** Cost of the last finished turn's session total, as `turn.result` reported it. */
  costUsd: number | null;
  numTurns: number;
  /** Context window of the main agent (from `usage.get`); `null` until asked. */
  context: { tokens: number; maxTokens: number; percent: number; model: string } | null;
}

/** The complete chat view state (JSON-serialisable). */
export interface ViewState {
  /** The bridge process. `version` is calu's version once it runs. */
  bridge: { status: BridgeStatus; message?: string; version?: string };
  /**
   * The connection between the bridge and the attached session. `cause`: why a "lost" link
   * was given up (`lostCause`); "unreachable" means the session ended for this view
   * (`sessionEnded` in `serverLoss.ts`).
   */
  link: { status: LinkStatus; message?: string; cause?: LostCause };
  /**
   * The attached session. All `null`/"stopped" while `link.status` is "detached"; after a
   * detach the transcript of the last session stays visible until the next attach.
   */
  session: {
    state: SessionStateName;
    /** calu's session id; `null` if none is attached. */
    sessionId: string | null;
    claudeSessionId: string | null;
    /** The directory the session works in. */
    workspace: string | null;
    project: string | null;
    /** Claude Code model (alias or id); `null` = Claude Code's default. */
    model: string | null;
    /** Effort the session started with; `null` = Claude Code's default. */
    effort: string | null;
    /** Background tasks still running at the end of the last turn. */
    backgroundTasks: number;
  };
  /** The running sessions of this machine (for the dropdown), sessions of the workspace first. */
  sessions: {
    list: SessionEntry[];
    /** Id of the attached session; `null` if none. Equal to `session.sessionId`. */
    attachedId: string | null;
    /** Why the list could not be loaded; `undefined` if it could. */
    error?: string;
    /** True after the first answer of `ide.sessions` (an empty list then means "none run"). */
    loaded: boolean;
  };
  /** How to start `calu serve` (set when no server answered, or on request); `null` otherwise. */
  serverHelp: ServerHelp | null;
  /** What is missing for the chat to work, most important first. */
  hints: SetupHint[];
  /** The session's auto-escalation switches (the supervision level, see `levels.ts`). */
  autoEscalate: AutoEscalateFlags;
  /**
   * Claude Code's permission mode as the session reports it (`permission_mode.changed`,
   * the snapshot's `permission_mode`); `null` while it is not known.
   */
  permissionMode: string | null;
  /** Code honesty state of the session; `null` before the first report. */
  honesty: HonestyInfo | null;
  /**
   * `honesty.mode` as calu's settings store it for the session's project ("off", "track",
   * "enforce"); `null` if not known. A session reads a changed mode by itself and reports it
   * with `honesty.update`; until then (or if it never does) the header shows both.
   */
  honestyStored: string | null;
  /**
   * A session request that waits for the calu server (`ide.start`); `null` otherwise. `slow`
   * is set after a few seconds without an answer: no server may be running.
   */
  starting: { title: string; slow: boolean } | null;
  usage: UsageView;
  /**
   * Request to show a tab: set when an escalation arrives, so the view can switch to the tab
   * that contains the card. `seq` increases with every request; a view switches tabs only for a
   * `seq` it has not handled yet. The replay at attach keeps the previous value.
   */
  focus: { tab: AgentKey; seq: number } | null;
  /** Chat messages sent but not taken in by Claude yet (`chat.queue`), oldest first. */
  queue: QueuedView[];
  /** All subagents of the session, in the order of the last `agents.update`. */
  agents: AgentInfo[];
  /** Task and result per subagent, keyed by `agent_id` (an entry exists once its launch was seen). */
  subagents: IdMap<SubagentView>;
  /** Message lists per agent; always contains `MAIN`. */
  items: IdMap<Item[]>;
  tools: IdMap<ToolView>;
  escalations: IdMap<EscalationView>;
  /**
   * The tool calls the session named as held (`step.held` with `held` true and no later
   * `held` false): calu approves no step while a decision about another step is open, and a
   * step that is held after its approval was recorded already has its `check`. Keyed by
   * `tool_use_id`. `heldTools` (chatModel) combines it with the rule for sessions that do not
   * send the event.
   */
  held: IdMap<true>;
  /** Side questions of this client, oldest first. */
  side: SideExchangeView[];
  /** Number of `turn.result` events applied since the transcript was last cleared. */
  turnCount: number;
  /**
   * Number of turns the conversation had before the first turn of the replayed events; `null`
   * until the recorded history was read after an attach (`ui.history`). While it is known, a
   * user message with `turn` 1 is the first message of the conversation: nothing is before
   * it, so the view offers no rewind or fork there.
   */
  turnBase: number | null;
  /** Sequence number of the last session event applied (0 before the first). */
  lastSeq: number;
  /** Next free item id. */
  nextId: number;
}

/**
 * An input of the reducer: a session event (`method` from the protocol's `EventParams`, relayed
 * by the bridge as `session.event`) or one of the extension's own events:
 *
 * - `bridge.status` `{status: BridgeStatus, message?, version?}`: the bridge process changed
 *   state;
 * - `ui.link` `{state: "attached" | "reconnecting" | "lost", message, cause?}`: the bridge's
 *   `ide.link` (`cause` only with "lost");
 * - `ui.attached` `{session_id, initialize, snapshot, events, complete, resumed?}`: the result
 *   of `ide.attach` / `ide.start`; replaces the transcript, or with `resumed` continues it
 *   (see `applyAttach`);
 * - `ui.history` `{session_id, history}`: the result of `session.history` for the attached
 *   session; the recorded turns that the replay does not cover are put above the transcript
 *   (see `applyHistory`);
 * - `ui.honestyStored` `{mode}`: `honesty.mode` as calu's settings store it (`null` = unknown);
 * - `ui.starting` `{title, slow}` or `{}`: a session request waits for the server / is over;
 * - `ui.detached` `{}`: the session was detached; the transcript stays, nothing is open;
 * - `ui.sessions` `{running: RunningSession[], error?}`: the answer of `ide.sessions`;
 * - `ui.serverHelp` `{command, tool, detach, note}` or `{}`: set or clear `serverHelp`;
 * - `ui.hints` `{hints: SetupHint[]}`: replace `hints`;
 * - `ui.model` `{model?: string | null, effort?: string | null}`: the model or effort changed;
 * - `ui.usage` `{context, rate_limits, cost_usd, num_turns}`: the result of `usage.get`;
 * - `ui.notice` `{level: "info" | "warning" | "error", text}`: a message for the user (e.g. a
 *   failed request) shown in the main list;
 * - `ui.clear` `{}`: drop messages, tool calls, escalations, subagent tabs, queued messages and
 *   side questions;
 * - `ui.decisionPending` `{escalation_key, answer?}` / `ui.decisionFailed` `{escalation_key}`:
 *   the user's decision was sent / could not be sent;
 * - `ui.sideAsk` `{id, question}` / `ui.sideAnswer` `{id, answer}` or `{id, error}`: a side
 *   question was sent / answered.
 */
export interface ViewEvent {
  method: string;
  params: Record<string, unknown>;
}

// ------------------------------------------------------------------ safe maps

/**
 * Read an own entry of a map.
 *
 * @param map A map from the state (any prototype).
 * @param key Any string, including "__proto__".
 * @returns The value stored under `key`, or `undefined` if there is no own entry.
 */
export function own<T>(map: IdMap<T>, key: string): T | undefined {
  return Object.hasOwn(map, key) ? map[key] : undefined;
}

/**
 * Return a copy of a map (with a null prototype) with one entry set.
 *
 * @param map The map (not modified).
 * @param key Any string, including "__proto__".
 * @param value The value.
 * @returns The new map.
 */
export function withEntry<T>(map: IdMap<T>, key: string, value: T): IdMap<T> {
  const copy: IdMap<T> = Object.assign(Object.create(null) as IdMap<T>, map);
  Object.defineProperty(copy, key, { value, enumerable: true, writable: true, configurable: true });
  return copy;
}

/**
 * Return a copy of a map (with a null prototype) without the given keys.
 *
 * @param map The map (not modified).
 * @param keys Keys to drop; missing keys are ignored.
 * @returns The new map; the same object if no key was present.
 */
export function withoutEntries<T>(map: IdMap<T>, keys: Iterable<string>): IdMap<T> {
  const drop = new Set([...keys].filter((k) => Object.hasOwn(map, k)));
  if (drop.size === 0) return map;
  const copy: IdMap<T> = Object.create(null) as IdMap<T>;
  for (const k of Object.keys(map)) {
    if (!drop.has(k)) withEntryInPlace(copy, k, map[k]);
  }
  return copy;
}

/**
 * Define an entry on a freshly created map (helper of the copy functions).
 *
 * @param map A map owned by the caller (modified).
 * @param key Any string.
 * @param value The value.
 * @returns Nothing.
 */
function withEntryInPlace<T>(map: IdMap<T>, key: string, value: T): void {
  Object.defineProperty(map, key, { value, enumerable: true, writable: true, configurable: true });
}

// ------------------------------------------------------------------ constructors and helpers

/**
 * Return the session part of the state while no session is attached.
 *
 * @returns State "stopped" and every other field `null` or 0.
 */
function noSession(): ViewState['session'] {
  return {
    state: 'stopped',
    sessionId: null,
    claudeSessionId: null,
    workspace: null,
    project: null,
    model: null,
    effort: null,
    backgroundTasks: 0,
  };
}

/**
 * Create the empty state shown before the bridge has started.
 *
 * @returns A fresh state: bridge "stopped", link "detached", no session, an empty main list.
 */
export function initialViewState(): ViewState {
  return {
    bridge: { status: 'stopped' },
    link: { status: 'detached' },
    session: noSession(),
    sessions: { list: [], attachedId: null, loaded: false },
    serverHelp: null,
    hints: [],
    autoEscalate: { commands: false, edits: false, subagents: false },
    permissionMode: null,
    honesty: null,
    honestyStored: null,
    starting: null,
    usage: { rateLimits: [], costUsd: null, numTurns: 0, context: null },
    focus: null,
    agents: [],
    subagents: Object.create(null) as IdMap<SubagentView>,
    items: withEntry<Item[]>({}, MAIN, []),
    tools: Object.create(null) as IdMap<ToolView>,
    escalations: Object.create(null) as IdMap<EscalationView>,
    held: Object.create(null) as IdMap<true>,
    queue: [],
    side: [],
    turnCount: 0,
    turnBase: null,
    lastSeq: 0,
    nextId: 1,
  };
}

/**
 * Return the message-list key for an `agent_id` field of an event.
 *
 * @param agentId The event's `agent_id` (`null`/`undefined`/non-string for the main agent).
 * @returns `MAIN` or `agentId`.
 */
export function agentKeyOf(agentId: unknown): AgentKey {
  return typeof agentId === 'string' && agentId ? agentId : MAIN;
}

/**
 * Cut a text to `limit` characters.
 *
 * @param text Any string.
 * @param limit Maximum length.
 * @returns `text`, or its first `limit` characters followed by "\n… (N more characters)".
 */
export function truncate(text: string, limit = MAX_STORED_TEXT): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}\n… (${text.length - limit} more characters)`;
}

/**
 * Bound the size of a tool input for storage: every string value (also nested) is cut with
 * `truncate`. Non-object inputs become `{}`.
 *
 * @param input The tool input from calu.
 * @returns A new object of the same shape with bounded strings.
 */
export function boundInput(input: unknown): Record<string, unknown> {
  /**
   * Recursively copy a JSON value with bounded strings.
   *
   * @param v Any JSON value.
   * @param depth Nesting depth (values deeper than 20 levels become "…").
   * @returns The bounded copy.
   */
  const bound = (v: unknown, depth: number): unknown => {
    if (typeof v === 'string') return truncate(v);
    if (depth > 20) return '…';
    if (Array.isArray(v)) return v.map((x) => bound(x, depth + 1));
    if (v && typeof v === 'object') {
      const out: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
      for (const [k, x] of Object.entries(v)) withEntryInPlace(out, k, bound(x, depth + 1));
      return out;
    }
    return v;
  };
  return input && typeof input === 'object' && !Array.isArray(input)
    ? (bound(input, 0) as Record<string, unknown>)
    : {};
}

/**
 * Describe a tool input literally, for display next to an escalation (the user must see what
 * they approve, independent of calu's Markdown rendering).
 *
 * @param input The (bounded) tool input.
 * @returns `[label, text]` pairs: "Command" (Bash `command`), "File" (`file_path`/`path`),
 *   always followed by "Input (JSON)" with the whole input pretty-printed.
 */
export function literalToolInput(input: Record<string, unknown>): [string, string][] {
  const parts: [string, string][] = [];
  if (typeof input.command === 'string') parts.push(['Command', input.command]);
  for (const key of ['file_path', 'path', 'notebook_path']) {
    if (typeof input[key] === 'string') parts.push(['File', input[key] as string]);
  }
  parts.push(['Input (JSON)', JSON.stringify(input, null, 2)]);
  return parts;
}

/**
 * Read the questions of an escalation (`escalation.request`.questions) defensively.
 *
 * @param raw The event's `questions` member (any JSON value).
 * @returns One `QuestionView` per entry that is an object with a string `question`; options
 *   without a string `label` are dropped; texts are cut with `truncate`. Empty for a non-array.
 */
export function parseQuestions(raw: unknown): QuestionView[] {
  if (!Array.isArray(raw)) return [];
  const out: QuestionView[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const q = entry as Record<string, unknown>;
    if (typeof q.question !== 'string') continue;
    const options = (Array.isArray(q.options) ? q.options : [])
      .filter((o): o is Record<string, unknown> => Boolean(o) && typeof o === 'object' && typeof o.label === 'string')
      .map((o) => ({
        label: truncate(o.label as string),
        description: typeof o.description === 'string' ? truncate(o.description) : '',
      }));
    out.push({
      question: truncate(q.question),
      header: typeof q.header === 'string' ? truncate(q.header) : '',
      options,
      multiSelect: q.multiSelect === true,
    });
  }
  return out;
}

/** How calu begins the text Claude receives for a denied step (`calu.escalations`). */
const DENIED_PREFIX = 'The user denied this step.';
/** What calu puts before the user's own note in that text. */
const NOTE_PREFIX = 'Note from the user:';

/**
 * Take the user's own words out of the note of an `escalation.resolved` event of a denial.
 *
 * For a denied step calu sends the whole text Claude receives ("The user denied this step.
 * Note from the user: <note>"); a card shows what the user wrote (the rule of the terminal
 * client, `calu.tui.model.own_words`).
 *
 * @param note The event's note.
 * @returns "" for a denial without a note, the note of a denial; any other text unchanged.
 *
 * @example deniedNote('The user denied this step. Note from the user: not yet') === 'not yet'
 */
export function deniedNote(note: string): string {
  const text = note.trim();
  if (!text.startsWith(DENIED_PREFIX)) return text;
  const rest = text.slice(DENIED_PREFIX.length).trim();
  return rest.startsWith(NOTE_PREFIX) ? rest.slice(NOTE_PREFIX.length).trim() : rest;
}

/**
 * Extract the user's answer from the note of an `escalation.resolved` event of a question.
 *
 * calu sends the text Claude receives: "The user answered your question: <answer>\nThe
 * AskUserQuestion call itself was not run; ..." (`ANSWER_TEMPLATE` in `calu.escalations`).
 *
 * @param note The event's note.
 * @returns The answer without calu's frame; the whole note if it has another form.
 */
export function answerFromNote(note: string): string {
  const match = /^The user answered your question: ([\s\S]*?)\nThe AskUserQuestion call itself was not run;/.exec(note);
  return match ? match[1] : note;
}

/**
 * Build the answer text for the questions of an escalation from the user's choices.
 *
 * @param questions The questions of the escalation, in order.
 * @param choices Per question (same index) the chosen option labels and/or a free text; a
 *   missing entry counts as no answer.
 * @returns For one question the answer alone; for several, one line "<header or question>:
 *   <answer>" per question. Chosen labels are joined with ", "; a free text is appended after
 *   them. "" if nothing was chosen or written.
 *
 * @example formatAnswer([{question: "Which?", ...}], [{labels: ["A", "B"], text: ""}]) === "A, B"
 */
export function formatAnswer(
  questions: readonly QuestionView[],
  choices: readonly ({ labels: readonly string[]; text: string } | undefined)[],
): string {
  const answers = questions.map((_, i) => {
    const c = choices[i];
    return c ? [...c.labels, c.text.trim()].filter((part) => part !== '').join(', ') : '';
  });
  if (answers.every((a) => a === '')) return '';
  if (questions.length === 1) return answers[0];
  return questions.map((q, i) => `${q.header || q.question}: ${answers[i] || '(no answer)'}`).join('\n');
}

/**
 * Read the honesty checker's result from `check.result` params defensively.
 *
 * @param p The params of a `check.result` event.
 * @returns `null` if the event carries no honesty result (no `honesty` entries and no
 *   `honesty_escalation`). Otherwise one `HonestyFileView` per well-formed entry (an entry
 *   without a string `path` is dropped; texts are cut by `truncate`) and the escalation reason.
 */
export function parseHonestyResult(p: Record<string, unknown>): HonestyResultView | null {
  const text = (v: unknown): string => (typeof v === 'string' ? truncate(v) : '');
  const files: HonestyFileView[] = (Array.isArray(p.honesty) ? p.honesty : [])
    .filter((f): f is Record<string, unknown> => Boolean(f) && typeof f === 'object' && typeof f.path === 'string')
    .map((f) => ({
      path: f.path as string,
      honest: f.verdict !== 'DISHONEST',
      reason: text(f.reason),
      dishonestUnits: (Array.isArray(f.units) ? f.units : [])
        .filter((u): u is Record<string, unknown> => Boolean(u) && typeof u === 'object' && u.honest === false)
        .map((u) => ({ symbol: text(u.symbol), reason: text(u.reason) })),
    }));
  const escalation = text(p.honesty_escalation);
  if (files.length === 0 && !escalation) return null;
  return { files, escalation };
}

/**
 * Name the chip a tool row shows for the honesty checker's result, if any.
 *
 * Only findings that need a person get a chip; a step whose files are all honest gets none
 * (its details list them). The chip never reads PASS or ALLOW.
 *
 * @param honesty The step's honesty result, or `undefined`.
 * @returns `null` for no chip; else the label ("dishonest" or "honesty: ask") and a title
 *   naming the dishonest files and the escalation reason.
 */
export function honestyRowChip(honesty: HonestyResultView | undefined): { label: string; title: string } | null {
  if (!honesty) return null;
  const dishonest = honesty.files.filter((f) => !f.honest);
  if (dishonest.length === 0 && !honesty.escalation) return null;
  const parts = dishonest.map((f) => `${f.path} is dishonest${f.reason ? `: ${f.reason}` : ''}`);
  if (honesty.escalation) parts.push(`The honesty checker asks a person to look: ${honesty.escalation}`);
  return { label: dishonest.length ? 'dishonest' : 'honesty: ask', title: `Honesty checker. ${parts.join('. ')}` };
}

/**
 * Read a code honesty state (`honesty.update` params or the snapshot's `honesty`) defensively.
 *
 * @param raw Any JSON value.
 * @returns A `HonestyInfo` with every field set (defaults for missing or mistyped ones), or
 *   `null` if `raw` is not an object.
 */
export function parseHonesty(raw: unknown): HonestyInfo | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const p = raw as Record<string, unknown>;
  const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
  const marks: HonestyMarkInfo[] = (Array.isArray(p.marks) ? p.marks : [])
    .filter((m): m is Record<string, unknown> => Boolean(m) && typeof m === 'object' && typeof m.id === 'number')
    .map((m) => ({
      id: m.id as number,
      path: str(m.path) ?? '',
      reason: truncate(str(m.reason) ?? ''),
      symbol: str(m.symbol) ?? '',
      opened_by: str(m.opened_by),
    }));
  return {
    mode: str(p.mode) ?? 'off',
    clean: p.clean === true,
    baseline: str(p.baseline),
    baseline_source: str(p.baseline_source),
    marks,
    review_required: p.review_required === true,
    review_running: p.review_running === true,
    unreviewed: (Array.isArray(p.unreviewed) ? p.unreviewed : []).filter((u): u is string => typeof u === 'string'),
    unreviewed_all: p.unreviewed_all === true,
    unreviewed_count: typeof p.unreviewed_count === 'number' ? p.unreviewed_count : 0,
  };
}

/**
 * Summarise a code honesty state for a header chip.
 *
 * @param honesty The state, or `null`.
 * @returns `kind` "off" (mode off or unknown), "clean", "review" (a review runs) or "open"
 *   (marks, unreviewed files or a required review), with a short `label` such as "Honesty: 2
 *   marks, 3 unreviewed".
 */
export function honestyChip(honesty: HonestyInfo | null): { kind: 'off' | 'clean' | 'review' | 'open'; label: string } {
  if (!honesty || !honesty.mode || honesty.mode === 'off') return { kind: 'off', label: '' };
  if (honesty.review_running) return { kind: 'review', label: 'Honesty: review running' };
  if (honesty.clean) return { kind: 'clean', label: 'Honesty: clean' };
  const marks = honesty.marks?.length ?? 0;
  const unreviewed = honesty.unreviewed_all ? 'all files' : `${honesty.unreviewed_count ?? 0}`;
  const parts = [
    marks ? `${marks} mark${marks === 1 ? '' : 's'}` : '',
    honesty.unreviewed_all || (honesty.unreviewed_count ?? 0) > 0 ? `${unreviewed} unreviewed` : '',
    !honesty.baseline ? 'no baseline' : '',
    honesty.review_required ? 'review required' : '',
  ].filter(Boolean);
  return { kind: 'open', label: `Honesty: ${parts.join(', ') || 'not clean'}` };
}

/**
 * Say that calu's settings store another honesty mode than the session uses.
 *
 * A session reads a changed `honesty.mode` by itself and reports it (`honesty.update`); the
 * two differ in the moment between, and for a session that does not read the change (it uses
 * another database, or its settings could not be reloaded).
 *
 * @param honesty The session's honesty state, or `null` (counts as mode "off").
 * @param stored `ViewState.honestyStored`.
 * @returns "" if the stored mode is unknown or equal to the session's; else e.g.
 *   "stored: track".
 */
export function honestyStoredNote(honesty: HonestyInfo | null, stored: string | null): string {
  if (!stored || stored === (honesty?.mode || 'off')) return '';
  return `stored: ${stored}`;
}

/**
 * Read a list of usage limits defensively.
 *
 * @param raw The `rate_limits` member of `usage.update`, the snapshot or `usage.get`.
 * @returns The entries that are objects, with `type`, `status`, `utilization` and `resets_at`
 *   set (defaults "" / `null`). Empty for a non-array.
 */
export function parseRateLimits(raw: unknown): RateLimit[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((r): r is Record<string, unknown> => Boolean(r) && typeof r === 'object')
    .map((r) => ({
      type: typeof r.type === 'string' ? r.type : '',
      status: typeof r.status === 'string' ? r.status : '',
      utilization: typeof r.utilization === 'number' ? r.utilization : null,
      resets_at: typeof r.resets_at === 'string' ? r.resets_at : null,
    }));
}

/**
 * Read the running sessions of `ide.sessions` for the dropdown.
 *
 * @param raw The `running` member (any JSON value).
 * @returns One `SessionEntry` per object with a string `id`, sessions of the workspace
 *   (`here`) first, otherwise in the given order. Empty for a non-array.
 */
export function parseSessions(raw: unknown): SessionEntry[] {
  if (!Array.isArray(raw)) return [];
  const list = raw
    .filter((s): s is Record<string, unknown> => Boolean(s) && typeof s === 'object' && typeof s.id === 'string')
    .map((s) => ({
      id: s.id as string,
      name: typeof s.name === 'string' ? s.name : '',
      directory: typeof s.directory === 'string' ? s.directory : '',
      state: typeof s.state === 'string' ? s.state : '',
      openEscalations: typeof s.open_escalations === 'number' ? s.open_escalations : 0,
      here: s.here === true,
      stale: s.stale === true,
      startedAt: typeof s.started_at === 'string' && s.started_at ? s.started_at : null,
    }));
  return [...list.filter((s) => s.here), ...list.filter((s) => !s.here)];
}

/**
 * Return the text that names a session in a list.
 *
 * @param session The session.
 * @returns Its name if it has one, else the last segment of its directory, else the first 8
 *   characters of its id.
 */
export function sessionTitle(session: Pick<SessionEntry, 'id' | 'name' | 'directory'>): string {
  if (session.name.trim()) return session.name.trim();
  const last = session.directory.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
  return last || session.id.slice(0, 8);
}

/**
 * Make sure a subagent key has an entry in `state.agents`, so its tab exists even if an event of
 * the subagent (e.g. an escalation) arrives before the `agents.update` that lists it.
 *
 * @param state The current state (not modified).
 * @param key A list key; `MAIN` and known agents leave the state unchanged.
 * @returns The state, with a placeholder agent `{agent_id: key, status: "running",
 *   description: "", background: false}` appended if the key was unknown. A later
 *   `agents.update` replaces the placeholder with the real entry.
 */
function ensureAgent(state: ViewState, key: AgentKey): ViewState {
  if (key === MAIN || state.agents.some((a) => a.agent_id === key)) return state;
  const placeholder: AgentInfo = { agent_id: key, description: '', background: false, status: 'running' };
  return { ...state, agents: [...state.agents, placeholder] };
}

/** `Omit` applied to each member of a union separately. */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/**
 * Append an item to an agent's list (copy-on-write). Above `MAX_ITEMS_PER_AGENT` the oldest
 * items are dropped together with the tools and escalations that only they referenced; a
 * leading "launch" item (subagent instructions) is always kept as the first item. An unknown
 * subagent key gets a placeholder agent (see `ensureAgent`), so its tab exists.
 *
 * @param state The current state (not modified).
 * @param key The list to append to; created if missing.
 * @param item The item without id; the id is taken from `state.nextId`.
 * @returns A new state containing the item, with `nextId` incremented.
 */
function appendItem(state: ViewState, key: AgentKey, item: DistributiveOmit<Item, 'id'>): ViewState {
  const list = [...(own(state.items, key) ?? []), { ...item, id: state.nextId } as Item];
  let next: ViewState = { ...state, nextId: state.nextId + 1 };
  next = ensureAgent(next, key);
  if (list.length > MAX_ITEMS_PER_AGENT) {
    const head = list[0].kind === 'launch' ? [list[0]] : [];
    const body = list.slice(head.length);
    const keep = MAX_ITEMS_PER_AGENT - head.length;
    const dropped = body.slice(0, body.length - keep);
    next = dropReferenced(next, dropped);
    next = { ...next, items: withEntry(next.items, key, [...head, ...body.slice(-keep)]) };
  } else {
    next = { ...next, items: withEntry(next.items, key, list) };
  }
  return next;
}

/**
 * Remove the tools (with their held marks) and escalations referenced by dropped items.
 *
 * @param state The current state (not modified).
 * @param dropped Items that are removed from their list.
 * @returns The state without those map entries.
 */
function dropReferenced(state: ViewState, dropped: readonly Item[]): ViewState {
  const tools: string[] = [];
  const escalations: string[] = [];
  for (const i of dropped) {
    if (i.kind === 'tool') tools.push(i.toolUseId);
    if (i.kind === 'escalation') escalations.push(i.escalationKey);
  }
  return {
    ...state,
    tools: withoutEntries(state.tools, tools),
    escalations: withoutEntries(state.escalations, escalations),
    held: withoutEntries(state.held, tools),
  };
}

/** Kinds of items that can be streamed with `assistant.delta`. */
type StreamKind = 'assistant' | 'thinking';

/** A streamable item (Claude's text or thinking). */
type StreamItem = Extract<Item, { kind: StreamKind }>;

/**
 * Find the latest text or thinking item with a given message id in an agent's list. Text and
 * thinking items of the same message are separate items.
 *
 * @param state The current state.
 * @param key The agent's list.
 * @param messageId calu's message id.
 * @param kind "assistant" (text) or "thinking".
 * @returns The item and its index, or `undefined` if the list has no such item.
 */
function findLatestStream(
  state: ViewState,
  key: AgentKey,
  messageId: string,
  kind: StreamKind,
): { item: StreamItem; index: number } | undefined {
  const list = own(state.items, key) ?? [];
  for (let i = list.length - 1; i >= 0; i--) {
    const item = list[i];
    if (item.kind === kind && item.messageId === messageId) return { item, index: i };
  }
  return undefined;
}

/**
 * Append a streamed fragment: extend the latest streaming item of the same message and kind
 * (also one that an attach cut off, see `StreamItem.cut`), or start a new streaming item (also
 * after a finalized block of the same message).
 *
 * @param state The current state (not modified).
 * @param key The agent's list.
 * @param kind "assistant" or "thinking".
 * @param messageId calu's message id.
 * @param text The fragment.
 * @returns The new state.
 */
function appendFragment(state: ViewState, key: AgentKey, kind: StreamKind, messageId: string, text: string): ViewState {
  const found = findLatestStream(state, key, messageId, kind);
  if (!found || !(found.item.streaming || found.item.cut)) {
    return appendItem(state, key, { kind, text, messageId, streaming: true });
  }
  const { cut: _cut, ...item } = found.item;
  return replaceItem(state, key, found.index, { ...item, text: item.text + text, streaming: true });
}

/**
 * Add a complete block: it finalizes the latest streaming item of the same message and kind,
 * otherwise it is appended.
 *
 * @param state The current state (not modified).
 * @param key The agent's list.
 * @param kind "assistant" or "thinking".
 * @param messageId calu's message id, if any.
 * @param text The complete text.
 * @returns The new state.
 */
function completeBlock(state: ViewState, key: AgentKey, kind: StreamKind, messageId: string | undefined, text: string): ViewState {
  const found = messageId ? findLatestStream(state, key, messageId, kind) : undefined;
  if (found && (found.item.streaming || found.item.cut)) {
    const { cut: _cut, ...item } = found.item;
    return replaceItem(state, key, found.index, { ...item, text, streaming: false });
  }
  return appendItem(state, key, { kind, text, ...(messageId ? { messageId } : {}) });
}

/**
 * Return a copy of `state` in which `items[key][index]` is replaced by `item`.
 *
 * @param state The current state (not modified).
 * @param key The agent's list; precondition: `0 <= index < items[key].length`.
 * @param index Position of the item.
 * @param item The replacement (keeps its id).
 * @returns The new state.
 */
function replaceItem(state: ViewState, key: AgentKey, index: number, item: Item): ViewState {
  const list = [...(own(state.items, key) ?? [])];
  list[index] = item;
  return { ...state, items: withEntry(state.items, key, list) };
}

/**
 * Return a copy of `state` with `tools[id]` replaced by `update(old)`.
 *
 * @param state The current state (not modified).
 * @param id The tool_use_id (any string).
 * @param update Receives the existing ToolView or `undefined`; returns the new ToolView.
 * @returns The new state.
 */
function updateTool(
  state: ViewState,
  id: string,
  update: (old: ToolView | undefined) => ToolView,
): ViewState {
  return { ...state, tools: withEntry(state.tools, id, update(own(state.tools, id))) };
}

/**
 * Ensure a placeholder ToolView (and its list item) exists for a tool call whose `tool.use`
 * event was not seen yet (e.g. `edit.diff` came first, or the client attached mid-turn).
 *
 * @param state The current state (not modified).
 * @param id The tool_use_id.
 * @param agentKey The list where a new item is appended.
 * @param name The tool name to use for a new placeholder.
 * @returns The state with `tools[id]` present.
 */
function ensureTool(state: ViewState, id: string, agentKey: AgentKey, name: string): ViewState {
  if (own(state.tools, id)) return state;
  const withTool = updateTool(state, id, () => ({ toolUseId: id, agentKey, name: String(name), input: {} }));
  return appendItem(withTool, agentKey, { kind: 'tool', toolUseId: id });
}

/**
 * Tell whether a tool starts a subagent.
 *
 * @param name The tool name from `tool.use`.
 * @returns True for "Agent" and "Task" (the older name of the same tool).
 */
export function isAgentLaunch(name: unknown): boolean {
  return name === 'Agent' || name === 'Task';
}

/**
 * Record the launch of a subagent: its task in `subagents`, and a "launch" item (the
 * instructions) at the start of the subagent's list.
 *
 * The subagent's list key is the `tool_use_id` of the Agent/Task call. If the list already
 * starts with a launch item, it is replaced (keeping its id); otherwise the item is inserted
 * before any events of the subagent that arrived earlier. The prompt is stored in full. A
 * result already stored for the subagent is kept.
 *
 * @param state The current state (not modified).
 * @param toolUseId The id of the Agent/Task tool call.
 * @param input The tool input (`description`, `prompt`, `subagent_type`, `run_in_background`).
 * @returns The new state.
 */
function recordLaunch(state: ViewState, toolUseId: string, input: unknown): ViewState {
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>;
  const list = own(state.items, toolUseId) ?? [];
  const existing = list[0]?.kind === 'launch' ? list[0] : undefined;
  const description = typeof i.description === 'string' ? i.description : '';
  const subagentType = typeof i.subagent_type === 'string' ? i.subagent_type : null;
  const background = i.run_in_background === true;
  const prompt = typeof i.prompt === 'string' ? i.prompt : '';
  const launch: Item = {
    id: existing?.id ?? state.nextId,
    kind: 'launch',
    toolUseId,
    description,
    subagentType,
    background,
    prompt,
  };
  const rest = existing ? list.slice(1) : list;
  const withAgent = ensureAgent(state, toolUseId);
  const previous = own(state.subagents, toolUseId);
  const subagent: SubagentView = {
    agentId: toolUseId,
    description,
    subagentType,
    background,
    task: prompt,
    ...(previous?.result ? { result: { ...previous.result, final: !background } } : {}),
  };
  return {
    ...withAgent,
    subagents: withEntry(state.subagents, toolUseId, subagent),
    items: withEntry(state.items, toolUseId, [launch, ...rest]),
    nextId: existing ? state.nextId : state.nextId + 1,
  };
}

/**
 * Record what the launching call of a subagent returned.
 *
 * @param state The current state (not modified).
 * @param toolUseId The id of the Agent/Task tool call.
 * @param content The result text (cut to `MAX_AGENT_RESULT_TEXT`).
 * @param isError Whether the call failed.
 * @returns The new state; `subagents[toolUseId]` exists afterwards (with an empty task if the
 *   launch was not seen).
 */
function recordAgentResult(state: ViewState, toolUseId: string, content: string, isError: boolean): ViewState {
  const previous: SubagentView = own(state.subagents, toolUseId) ?? {
    agentId: toolUseId,
    description: '',
    subagentType: null,
    background: state.agents.find((a) => a.agent_id === toolUseId)?.background === true,
    task: '',
  };
  const result = { content: truncate(content, MAX_AGENT_RESULT_TEXT), isError, final: !previous.background };
  return { ...state, subagents: withEntry(state.subagents, toolUseId, { ...previous, result }) };
}

/**
 * Clear the `pending` flag of every open escalation (used when the bridge or the connection to
 * the session is lost). The cards stay open: the steps still wait in the session, and a
 * decision that was on its way is not known to have arrived. The view shows them as "connection
 * lost" while `isAttached` is false; the next attach says what is open then.
 *
 * @param state The current state (not modified).
 * @returns The new state; the same object if no open escalation was pending.
 */
function unpendEscalations(state: ViewState): ViewState {
  let escalations = state.escalations;
  for (const e of Object.values(state.escalations)) {
    if (e.status === 'open' && e.pending) escalations = withEntry(escalations, e.key, { ...e, pending: false });
  }
  return escalations === state.escalations ? state : { ...state, escalations };
}

/**
 * Close all open escalations with status "cancelled" (used when the session ends or is left).
 *
 * @param state The current state (not modified).
 * @returns The new state; the same object if no escalation was open.
 */
function cancelOpenEscalations(state: ViewState): ViewState {
  let escalations = state.escalations;
  for (const e of Object.values(state.escalations)) {
    if (e.status === 'open') {
      escalations = withEntry(escalations, e.key, { ...e, status: 'cancelled', pending: false });
    }
  }
  return escalations === state.escalations ? state : { ...state, escalations };
}

/**
 * Mark every streaming text or thinking item as finished (used at turn end, session stop,
 * bridge exit, attach).
 *
 * @param state The current state (not modified).
 * @param cut True at an attach: the items are marked `cut`, so that the fragments and the final
 *   text that arrive after the replay continue them instead of starting a second item.
 * @returns The new state; the same object if nothing was streaming.
 */
function finishStreaming(state: ViewState, cut = false): ViewState {
  let items = state.items;
  for (const key of Object.keys(state.items)) {
    const list = state.items[key];
    const streaming = (i: Item) => (i.kind === 'assistant' || i.kind === 'thinking') && i.streaming;
    if (!list.some(streaming)) continue;
    items = withEntry(
      items,
      key,
      list.map((i) => (streaming(i) ? ({ ...i, streaming: false, ...(cut ? { cut: true } : {}) } as Item) : i)),
    );
  }
  return items === state.items ? state : { ...state, items };
}

/**
 * Find the open escalation with one of calu's escalation ids.
 *
 * @param state The current state.
 * @param escalationId calu's id.
 * @returns The latest open escalation with that id, or `undefined`.
 */
function findOpenEscalation(state: ViewState, escalationId: string): EscalationView | undefined {
  let found: EscalationView | undefined;
  for (const e of Object.values(state.escalations)) {
    if (e.escalationId === escalationId && e.status === 'open') found = e;
  }
  return found;
}

/**
 * Set or clear the `pending` flag of an open escalation.
 *
 * @param state The current state (not modified).
 * @param key The escalation's state key.
 * @param pending The new flag.
 * @param answer With `pending` true: the answer text the user sent, if it was an answer.
 * @returns The new state; unchanged if the escalation is unknown or not open.
 */
function setPending(state: ViewState, key: string, pending: boolean, answer?: string): ViewState {
  const esc = own(state.escalations, key);
  if (!esc || esc.status !== 'open') return state;
  const next: EscalationView = { ...esc, pending };
  if (pending && answer !== undefined) next.answer = answer;
  if (!pending) delete next.answer;
  return { ...state, escalations: withEntry(state.escalations, key, next) };
}

/**
 * Drop the transcript: messages, tool calls, escalations, subagents, queue and side questions.
 *
 * @param state The current state (not modified).
 * @returns The state with empty lists; bridge, link, session, sessions, hints and settings-like
 *   parts are kept.
 */
function clearTranscript(state: ViewState): ViewState {
  return {
    ...state,
    items: withEntry<Item[]>({}, MAIN, []),
    tools: Object.create(null) as IdMap<ToolView>,
    escalations: Object.create(null) as IdMap<EscalationView>,
    held: Object.create(null) as IdMap<true>,
    subagents: Object.create(null) as IdMap<SubagentView>,
    agents: [],
    queue: [],
    side: [],
    focus: null,
    turnCount: 0,
  };
}

/**
 * Read the attached images of a user input.
 *
 * @param raw The event's `images` member (any JSON value).
 * @returns One `ImageView` per entry with string `data` and `media_type`; data longer than
 *   `MAX_IMAGE_DATA` is dropped and the image marked `omitted`.
 */
function parseImages(raw: unknown): ImageView[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (i): i is { data: string; media_type: string } =>
        Boolean(i) && typeof i === 'object' && typeof i.data === 'string' && typeof i.media_type === 'string',
    )
    .map((i) =>
      i.data.length > MAX_IMAGE_DATA
        ? { mediaType: i.media_type, data: '', omitted: true }
        : { mediaType: i.media_type, data: i.data, omitted: false },
    );
}

/**
 * Add an escalation card (reducer case `escalation.request`).
 *
 * Always a new card; an id that is already in the state gets a fresh state key, so a closed
 * card is never reopened or overwritten.
 *
 * @param state The current state (not modified).
 * @param e The event params (untrusted).
 * @param focus True to request a switch to the card's tab.
 * @returns The new state; unchanged without a string `escalation_id`.
 */
function addEscalation(state: ViewState, e: EscalationRequest, focus: boolean): ViewState {
  if (typeof e.escalation_id !== 'string') return state;
  const key = agentKeyOf(e.agent_id);
  const escKey = own(state.escalations, e.escalation_id) ? `${e.escalation_id}#${state.nextId}` : e.escalation_id;
  const view: EscalationView = {
    key: escKey,
    escalationId: e.escalation_id,
    toolUseId: typeof e.tool_use_id === 'string' ? e.tool_use_id : null,
    agentKey: key,
    toolName: String(e.tool_name),
    toolInput: boundInput(e.tool_input),
    reason: typeof e.reason === 'string' ? e.reason : '',
    rendered: typeof e.rendered === 'string' ? truncate(e.rendered, 4 * MAX_STORED_TEXT) : '',
    questions: parseQuestions(e.questions),
    openedAt: typeof e.opened_at === 'string' ? e.opened_at : null,
    status: 'open',
  };
  const next = { ...state, escalations: withEntry(state.escalations, escKey, view) };
  const withCard = appendItem(next, key, { kind: 'escalation', escalationKey: escKey });
  return focus ? { ...withCard, focus: { tab: key, seq: (state.focus?.seq ?? 0) + 1 } } : withCard;
}

// ------------------------------------------------------------------ the reducer

/**
 * Apply one event to the view state.
 *
 * Unknown methods and events with missing required fields leave the state unchanged. The input
 * state is not modified. For any JSON value as `event.params` the function returns a state;
 * it is designed not to throw (malformed values are converted or ignored), and unit tests cover
 * malformed input, but it relies on the invariants of `state` that it creates itself.
 *
 * @param state The current state.
 * @param event A session event or an extension event (see `ViewEvent`).
 * @returns The new state (may be the same object if nothing changed).
 */
export function applyEvent(state: ViewState, event: ViewEvent): ViewState {
  const p: Record<string, unknown> =
    event.params && typeof event.params === 'object' ? event.params : {};
  const next = reduce(state, event.method, p);
  // Session events carry their sequence number; the extension's own events have none.
  return typeof p.seq === 'number' && p.seq > next.lastSeq && !event.method.startsWith('ui.')
    ? { ...next, lastSeq: p.seq }
    : next;
}

/**
 * Body of `applyEvent` without the sequence number bookkeeping.
 *
 * @param state The current state (not modified).
 * @param method The event's method.
 * @param p The event's params (an object; members untrusted).
 * @returns The new state.
 */
function reduce(state: ViewState, method: string, p: Record<string, unknown>): ViewState {
  const str = (v: unknown): v is string => typeof v === 'string';
  switch (method) {
    case 'bridge.status': {
      const status = p.status as BridgeStatus;
      let next: ViewState = {
        ...state,
        bridge: {
          status,
          ...(str(p.message) ? { message: p.message } : {}),
          ...(str(p.version) ? { version: p.version } : status === 'running' && state.bridge.version ? { version: state.bridge.version } : {}),
        },
      };
      if (status !== 'running') {
        // Without the bridge nothing is attached; the transcript stays readable, and the open
        // cards stay as they were (the steps still wait in the session).
        next = finishStreaming(unpendEscalations(next));
        next = {
          ...next,
          starting: null,
          link: { status: 'detached' },
          session: { ...next.session, state: 'stopped' },
          sessions: { ...next.sessions, attachedId: null },
          queue: [],
        };
      }
      return next;
    }
    case 'ui.link': {
      const status = p.state === 'attached' || p.state === 'reconnecting' || p.state === 'lost' ? p.state : undefined;
      if (!status) return state;
      const cause = status === 'lost' ? lostCause(p.cause, p.message) : undefined;
      const next: ViewState = {
        ...state,
        link: { status, ...(str(p.message) && p.message ? { message: p.message } : {}), ...(cause ? { cause } : {}) },
      };
      if (status !== 'lost') return next;
      // The session can no longer be reached: nothing is streaming, and nothing can be decided
      // until it is attached again (the cards stay open, see `unpendEscalations`).
      return {
        ...finishStreaming(unpendEscalations(next)),
        sessions: { ...next.sessions, attachedId: null },
        queue: [],
      };
    }
    case 'ui.attached':
      return applyAttach(state, p);
    case 'ui.history':
      return applyHistory(state, p);
    case 'ui.honestyStored':
      return { ...state, honestyStored: str(p.mode) && p.mode ? p.mode : null };
    case 'ui.starting':
      return { ...state, starting: str(p.title) ? { title: p.title, slow: p.slow === true } : null };
    case 'ui.detached': {
      const next = finishStreaming(cancelOpenEscalations(state));
      return {
        ...next,
        link: { status: 'detached' },
        session: { ...noSession(), workspace: next.session.workspace },
        sessions: { ...next.sessions, attachedId: null },
        queue: [],
      };
    }
    case 'ui.sessions':
      return {
        ...state,
        sessions: {
          list: str(p.error) ? state.sessions.list : parseSessions(p.running),
          attachedId: state.sessions.attachedId,
          loaded: true,
          ...(str(p.error) ? { error: p.error } : {}),
        },
      };
    case 'ui.serverHelp': {
      const tool = p.tool === 'tmux' || p.tool === 'screen' || p.tool === 'plain' ? p.tool : undefined;
      if (!str(p.command) || !tool) return { ...state, serverHelp: null };
      return {
        ...state,
        serverHelp: { command: p.command, tool, detach: str(p.detach) ? p.detach : '', note: str(p.note) ? p.note : '' },
      };
    }
    case 'ui.hints': {
      const hints = (Array.isArray(p.hints) ? p.hints : []).filter(
        (h): h is SetupHint => Boolean(h) && typeof h === 'object' && str((h as SetupHint).id) && str((h as SetupHint).message),
      );
      return { ...state, hints };
    }
    case 'ui.model':
      return {
        ...state,
        session: {
          ...state.session,
          ...('model' in p ? { model: str(p.model) && p.model ? p.model : null } : {}),
          ...('effort' in p ? { effort: str(p.effort) && p.effort ? p.effort : null } : {}),
        },
      };
    case 'ui.usage': {
      const c = p.context && typeof p.context === 'object' ? (p.context as Record<string, unknown>) : undefined;
      const context =
        c && typeof c.tokens === 'number' && typeof c.max_tokens === 'number'
          ? {
              tokens: c.tokens,
              maxTokens: c.max_tokens,
              percent: typeof c.percent === 'number' ? c.percent : c.max_tokens > 0 ? (100 * c.tokens) / c.max_tokens : 0,
              model: str(c.model) ? c.model : '',
            }
          : null;
      return {
        ...state,
        usage: {
          rateLimits: Array.isArray(p.rate_limits) ? parseRateLimits(p.rate_limits) : state.usage.rateLimits,
          costUsd: typeof p.cost_usd === 'number' ? p.cost_usd : state.usage.costUsd,
          numTurns: typeof p.num_turns === 'number' ? p.num_turns : state.usage.numTurns,
          context,
        },
      };
    }
    case 'ui.notice':
      return appendItem(state, MAIN, {
        kind: 'notice',
        level: str(p.level) ? p.level : 'info',
        text: String(p.text ?? ''),
      });
    case 'ui.clear':
      return clearTranscript(state);
    case 'ui.decisionPending':
      return str(p.escalation_key) ? setPending(state, p.escalation_key, true, str(p.answer) ? p.answer : undefined) : state;
    case 'ui.decisionFailed':
      return str(p.escalation_key) ? setPending(state, p.escalation_key, false) : state;
    case 'ui.sideAsk': {
      if (typeof p.id !== 'number' || !str(p.question)) return state;
      const side = [...state.side, { id: p.id, question: p.question }].slice(-MAX_SIDE_EXCHANGES);
      return { ...state, side };
    }
    case 'ui.sideAnswer': {
      if (typeof p.id !== 'number') return state;
      const id = p.id;
      return {
        ...state,
        side: state.side.map((s) =>
          s.id !== id ? s : str(p.error) ? { ...s, error: p.error } : { ...s, answer: str(p.answer) ? p.answer : '' },
        ),
      };
    }
    case 'session.state': {
      const e = p as unknown as SessionState;
      if (!str(e.state)) return state;
      const next: ViewState = {
        ...state,
        session: {
          ...state.session,
          state: e.state,
          claudeSessionId: str(e.session_id) ? e.session_id : state.session.claudeSessionId,
        },
      };
      return e.state === 'stopped' || e.state === 'failed' ? finishStreaming(cancelOpenEscalations(next)) : next;
    }
    case 'assistant.delta': {
      // A fragment of the in-progress text (or, with `thinking: true`, thinking) block of message
      // `message_id`. A message can have several blocks; a delta after a finalized block starts a
      // new item. Text and thinking are separate items.
      const messageId = p.message_id;
      if (!str(p.text) || !str(messageId)) return state;
      const kind: StreamKind = p.thinking === true ? 'thinking' : 'assistant';
      return appendFragment(state, agentKeyOf(p.agent_id), kind, messageId, p.text);
    }
    case 'assistant.text':
    case 'assistant.thinking': {
      // A complete text or thinking block; it finalizes the latest streaming item of the same
      // message_id and kind, otherwise it is appended.
      if (!str(p.text)) return state;
      const kind: StreamKind = method === 'assistant.thinking' ? 'thinking' : 'assistant';
      const messageId = str(p.message_id) ? p.message_id : undefined;
      return completeBlock(state, agentKeyOf(p.agent_id), kind, messageId, p.text);
    }
    case 'user.input': {
      const e = p as unknown as UserInput;
      if (!str(e.text)) return state;
      const source: 'user' | 'cross_session' | 'system' = e.source === 'cross_session' || e.source === 'system' ? e.source : 'user';
      const message = {
        kind: 'user' as const,
        text: e.text,
        source,
        images: parseImages(e.images),
        turn: (state.turnBase ?? 0) + (state.turnCount ?? 0) + 1,
        uuid: str(e.uuid) && e.uuid ? e.uuid : null,
      };
      // A message for a background subagent: the user's words belong to that subagent's tab;
      // Main only notes that something was sent (the main agent relays it).
      const to = source === 'user' ? agentKeyOf(e.to) : MAIN;
      if (to === MAIN) return appendItem(state, MAIN, message);
      return appendItem(appendItem(state, to, { ...message, to }), MAIN, { kind: 'relay', to });
    }
    case 'tool.use': {
      const e = p as unknown as ToolUse;
      if (!str(e.tool_use_id)) return state;
      const key = agentKeyOf(e.agent_id);
      const existed = Boolean(own(state.tools, e.tool_use_id));
      const next = updateTool(state, e.tool_use_id, (old) => ({
        ...old,
        toolUseId: e.tool_use_id,
        agentKey: old?.agentKey ?? key,
        name: String(e.name),
        input: boundInput(e.input),
      }));
      const withLaunch = isAgentLaunch(e.name) ? recordLaunch(next, e.tool_use_id, e.input) : next;
      return existed ? withLaunch : appendItem(withLaunch, key, { kind: 'tool', toolUseId: e.tool_use_id });
    }
    case 'check.result': {
      // Updates the tool call's decision and, for a checker remark or a DENY reason, appends a
      // "checker" message after the tool item in the list of the agent that made the call.
      const e = p as unknown as CheckResult;
      const key = agentKeyOf(e.agent_id);
      const checkerRoute = checkerRouteOf(String(e.route));
      let next = state;
      if (str(e.tool_use_id)) {
        next = ensureTool(next, e.tool_use_id, key, e.tool_name);
        next = updateTool(next, e.tool_use_id, (old) => ({
          ...old!,
          check: {
            route: String(e.route),
            decision: String(e.decision),
            reason: str(e.reason) ? e.reason : '',
            summary: str(e.step_summary) ? e.step_summary : '',
            comment: str(e.comment) ? e.comment : '',
            checkerRoute,
          },
          honesty: parseHonestyResult(p) ?? old?.honesty,
          gitHead: str(e.git_head) ? e.git_head : (old?.gitHead ?? null),
        }));
      }
      const message = checkerMessage(e);
      if (!message) return next;
      return appendItem(next, key, {
        kind: 'checker',
        label: checkerLabel(String(e.route)),
        checkerRoute,
        decision: String(e.decision),
        text: message,
        toolUseId: str(e.tool_use_id) ? e.tool_use_id : null,
      });
    }
    case 'tool.result': {
      const e = p as unknown as ToolResult;
      if (!str(e.tool_use_id)) return state;
      const content = String(e.content ?? '');
      let next = ensureTool(state, e.tool_use_id, agentKeyOf(e.agent_id), '?');
      next = updateTool(next, e.tool_use_id, (old) => ({
        ...old!,
        result: { content: truncate(content), isError: Boolean(e.is_error) },
      }));
      const launched = isAgentLaunch(own(next.tools, e.tool_use_id)?.name) || Boolean(own(next.subagents, e.tool_use_id));
      return launched ? recordAgentResult(next, e.tool_use_id, content, Boolean(e.is_error)) : next;
    }
    case 'step.held': {
      // The session holds a step back while a decision about another one is open, and says
      // so when the hold begins and when it ends (agent/records/PLAN.md D237 (6)).
      const e = p as unknown as StepHeld;
      if (!str(e.tool_use_id)) return state;
      if (e.held === true) return own(state.held, e.tool_use_id) ? state : { ...state, held: withEntry(state.held, e.tool_use_id, true as const) };
      return own(state.held, e.tool_use_id) ? { ...state, held: withoutEntries(state.held, [e.tool_use_id]) } : state;
    }
    case 'chat.queue': {
      const e = p as unknown as ChatQueue;
      if (!Array.isArray(e.messages)) return state;
      const queue = e.messages
        .filter((m) => str(m?.id) && str(m?.text))
        .map((m) => ({
          id: m.id,
          text: m.text,
          delivery: m.delivery === 'inject' ? ('inject' as const) : ('queued' as const),
          to: str(m.to) && m.to ? m.to : 'main',
        }));
      return { ...state, queue };
    }
    case 'tool.files': {
      const e = p as unknown as ToolFiles;
      if (!str(e.tool_use_id) || !Array.isArray(e.files)) return state;
      const files = e.files
        .filter((f) => str(f?.raw) && f.raw !== '' && str(f?.path) && f.path !== '')
        .slice(0, MAX_TOOL_FILES)
        .map((f) => ({ raw: f.raw, path: f.path }));
      const next = ensureTool(state, e.tool_use_id, agentKeyOf(e.agent_id), '?');
      return updateTool(next, e.tool_use_id, (old) => ({ ...old!, files }));
    }
    case 'edit.diff': {
      const e = p as unknown as EditDiff;
      if (!str(e.tool_use_id)) return state;
      const next = ensureTool(state, e.tool_use_id, agentKeyOf(e.agent_id), 'Edit');
      const start = typeof e.start_line === 'number' && e.start_line >= 1 ? e.start_line : null;
      const end = typeof e.end_line === 'number' ? e.end_line : null;
      const editRange = start !== null && end !== null && end >= start - 1 ? { start, end } : null;
      return updateTool(next, e.tool_use_id, (old) => ({ ...old!, diffPath: String(e.path), editRange }));
    }
    case 'escalation.request':
      return addEscalation(state, p as unknown as EscalationRequest, true);
    case 'escalation.resolved': {
      const e = p as unknown as EscalationResolved;
      if (!str(e.escalation_id)) return state;
      const old = findOpenEscalation(state, e.escalation_id);
      if (!old) return state;
      const decided = (['allow', 'deny', 'cancelled'] as const).includes(e.decision) ? e.decision : 'cancelled';
      // calu reports a question closed by an answer as "deny" (the AskUserQuestion call itself
      // does not run) with the text Claude receives as the note. The card shows it as answered:
      // also after a replay and when another client answered, where no `ui.decisionPending`
      // of this view recorded the answer.
      const question = old.questions.length > 0;
      const answered = decided !== 'cancelled' && (question || old.answer !== undefined);
      const raw = str(e.note) ? e.note : null;
      const answer = old.answer ?? (answered && raw !== null ? answerFromNote(raw) : undefined);
      const own = raw !== null && !answered ? deniedNote(raw) : raw;
      const note = own ? own : null;
      return {
        ...state,
        escalations: withEntry(state.escalations, old.key, {
          ...old,
          status: answered ? 'answered' : decided,
          pending: false,
          ...(answer !== undefined ? { answer } : {}),
          note: answered && question ? null : note,
        }),
      };
    }
    case 'turn.result': {
      const e = p as unknown as TurnResult;
      // `cost_usd` is Claude Code's running total of the session, not the turn's own cost.
      const costUsd = typeof e.cost_usd === 'number' ? e.cost_usd : null;
      const before = state.usage.costUsd;
      const costDelta = costUsd !== null && before !== null && costUsd >= before ? costUsd - before : null;
      const backgroundTasks = typeof e.background_tasks === 'number' ? e.background_tasks : 0;
      const next = appendItem(finishStreaming(state), MAIN, {
        kind: 'turn',
        subtype: str(e.subtype) ? e.subtype : 'success',
        isError: Boolean(e.is_error),
        costUsd,
        costDelta,
        backgroundTasks,
      });
      return {
        ...next,
        turnCount: (next.turnCount ?? 0) + 1,
        session: {
          ...next.session,
          backgroundTasks,
          claudeSessionId: str(e.session_id) ? e.session_id : next.session.claudeSessionId,
        },
        usage: {
          ...next.usage,
          costUsd: costUsd ?? next.usage.costUsd,
          numTurns: typeof e.num_turns === 'number' ? e.num_turns : next.usage.numTurns,
        },
      };
    }
    case 'agents.update': {
      if (!Array.isArray(p.agents)) return state;
      const agents = (p.agents as unknown[]).filter(
        (a): a is AgentInfo => Boolean(a) && typeof a === 'object' && str((a as AgentInfo).agent_id),
      );
      let items = state.items;
      for (const a of agents) if (!own(items, a.agent_id)) items = withEntry(items, a.agent_id, []);
      // Keep agents that the update does not list but whose tab already has content (e.g. a
      // placeholder created by an early event), so their tab does not disappear.
      const listed = new Set(agents.map((a) => a.agent_id));
      const kept = state.agents.filter((a) => !listed.has(a.agent_id) && (own(items, a.agent_id) ?? []).length > 0);
      return { ...state, agents: [...agents, ...kept], items };
    }
    case 'auto_escalate.changed': {
      const s = p.settings && typeof p.settings === 'object' ? (p.settings as Record<string, unknown>) : {};
      return {
        ...state,
        autoEscalate: { commands: s.commands === true, edits: s.edits === true, subagents: s.subagents === true },
      };
    }
    case 'permission_mode.changed':
      return typeof p.mode === 'string' && p.mode ? { ...state, permissionMode: p.mode } : state;
    case 'honesty.update':
      return { ...state, honesty: parseHonesty(p) };
    case 'usage.update':
      return Array.isArray(p.rate_limits)
        ? { ...state, usage: { ...state.usage, rateLimits: parseRateLimits(p.rate_limits) } }
        : state;
    case 'log': {
      const e = p as unknown as Log;
      const level = e.level ?? 'info';
      if (level !== 'warning' && level !== 'error') return state;
      return appendItem(state, MAIN, { kind: 'notice', level, text: String(e.message ?? '') });
    }
    default:
      return state;
  }
}

/**
 * Return the route whose checkers judged a step, for *Edit checker instructions*. Checker
 * names are free (several checkers can serve one route), so the host looks the checkers up by
 * route.
 *
 * @param route The `route` of a `check.result`.
 * @returns The route itself for the routes a checker judges ("command", "edit", "changeset",
 *   "subagent"); "" for the rules' routes ("allow", "deny", "escalate") and for "question".
 */
export function checkerRouteOf(route: string): string {
  return route === 'command' || route === 'edit' || route === 'changeset' || route === 'subagent' ? route : '';
}

/**
 * Return the display name of the checker (or rule) behind a `check.result` route.
 *
 * @param route The event's `route`.
 * @returns "Command checker", "Edit checker", "Changeset checker", "Subagent result checker",
 *   "Question" (a question of the agent), "Recorded decision" for "stored" (a step taken from
 *   calu's record, which does not say who decided), or "Rules" for "allow", "deny" and "escalate".
 */
export function checkerLabel(route: string): string {
  if (route === 'command') return 'Command checker';
  if (route === 'edit') return 'Edit checker';
  if (route === 'changeset') return 'Changeset checker';
  if (route === 'subagent') return 'Subagent result checker';
  if (route === 'question') return 'Question';
  if (route === 'stored') return 'Recorded decision';
  return 'Rules';
}

/**
 * Return the text a `check.result` contributes to the chat as a checker message.
 *
 * @param e The event params.
 * @returns The non-empty `comment` (a remark made while approving), else the non-empty `reason`
 *   of a DENY decision, else `undefined` (no chat message).
 */
export function checkerMessage(e: Partial<CheckResult>): string | undefined {
  const comment = typeof e.comment === 'string' ? e.comment.trim() : '';
  if (comment) return comment;
  const reason = typeof e.reason === 'string' ? e.reason.trim() : '';
  if (e.decision === 'DENY' && reason) return reason;
  return undefined;
}

/**
 * Apply a sequence of events.
 *
 * @param state The start state (not modified).
 * @param events Events in arrival order.
 * @returns The state after all events.
 */
export function applyEvents(state: ViewState, events: readonly ViewEvent[]): ViewState {
  return events.reduce(applyEvent, state);
}

/** Methods of recorded events that describe the live session and are taken from the snapshot. */
const NOT_REPLAYED = new Set(['session.state', 'log', 'chat.queue', 'auto_escalate.changed', 'permission_mode.changed', 'honesty.update', 'usage.update']);

/**
 * Rebuild the state from the result of `ide.attach` / `ide.start` (reducer case `ui.attached`).
 *
 *     1. drop the transcript of the session shown before -- unless `resumed` is true: the
 *        host attached again to the session this state shows, asked only for the events after
 *        `lastSeq` and got them without a gap (after a restart of the bridge); the transcript
 *        then stays and the events continue it
 *     2. replay `events` (the recorded events of the session, oldest first)  -> transcript
 *     3. apply `snapshot` (authoritative for what is true now):
 *          session state, model, effort, agents, queue, auto-escalation, permission mode,
 *          honesty, limits;
 *          escalations: one that the replay left open but the snapshot does not list is
 *          closed as "cancelled"; one that the snapshot lists but the replay did not produce
 *          is added
 *
 * Postconditions: `link.status` is "attached"; `session.sessionId` and `sessions.attachedId`
 * are `session_id`; exactly the snapshot's escalations are open; nothing is streaming;
 * `serverHelp` is cleared; `focus` is the value before the call, or a request for the tab of
 * the first open escalation if there is one.
 *
 * @param state The current state (not modified).
 * @param p The params of `ui.attached`: `session_id`, `initialize`, `snapshot`, `events`,
 *   `resumed` (untrusted; missing members are treated as empty). `resumed` is honoured only if
 *   `session_id` is the session the state shows.
 * @returns The rebuilt state; unchanged without a string `session_id`.
 */
export function applyAttach(state: ViewState, p: Record<string, unknown>): ViewState {
  if (typeof p.session_id !== 'string') return state;
  const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
  const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
  const snapshot = obj(p.snapshot) as Partial<SessionSnapshot> & Record<string, unknown>;
  const init = obj(p.initialize);

  const resumed = p.resumed === true && state.session.sessionId === p.session_id;
  // Cost, model calls and context belong to the session shown before; the limits are the
  // account's and stay. The replayed turns and the next `ui.usage` bring this session's numbers.
  const fresh = { ...state.usage, costUsd: null, numTurns: 0, context: null };
  let next: ViewState = resumed ? unpendEscalations(state) : { ...clearTranscript(state), lastSeq: 0, turnBase: null, usage: fresh };
  for (const raw of Array.isArray(p.events) ? p.events : []) {
    const e = obj(raw);
    if (typeof e.method !== 'string' || NOT_REPLAYED.has(e.method) || e.method.startsWith('ui.') || e.method === 'bridge.status') continue;
    next = applyEvent(next, { method: e.method, params: obj(e.params) });
  }

  // Escalations: the snapshot says which are open now.
  const open = (Array.isArray(snapshot.escalations) ? snapshot.escalations : []).filter(
    (e): e is EscalationRequest => Boolean(e) && typeof e === 'object' && typeof e.escalation_id === 'string',
  );
  const openIds = new Set(open.map((e) => e.escalation_id));
  let escalations = next.escalations;
  for (const esc of Object.values(next.escalations)) {
    if (esc.status === 'open' && !openIds.has(esc.escalationId)) {
      escalations = withEntry(escalations, esc.key, { ...esc, status: 'cancelled', pending: false });
    }
  }
  next = { ...next, escalations };
  for (const e of open) {
    if (!findOpenEscalation(next, e.escalation_id)) next = addEscalation(next, e, false);
  }
  const firstOpen = Object.values(next.escalations).find((e) => e.status === 'open');

  if (Array.isArray(snapshot.agents)) next = applyEvent(next, { method: 'agents.update', params: { agents: snapshot.agents } });
  next = applyEvent(next, { method: 'chat.queue', params: { messages: Array.isArray(snapshot.queue) ? snapshot.queue : [] } });
  next = applyEvent(next, { method: 'auto_escalate.changed', params: { settings: obj(snapshot.auto_escalate) } });
  next = { ...next, permissionMode: str(snapshot.permission_mode) };

  const snapshotSeq = typeof snapshot.last_seq === 'number' ? snapshot.last_seq : 0;
  const stateName = typeof snapshot.state === 'string' ? (snapshot.state as SessionStateName) : 'idle';
  next = finishStreaming(next, true);
  return {
    ...next,
    link: { status: 'attached' },
    session: {
      state: stateName,
      sessionId: p.session_id,
      claudeSessionId: str(snapshot.claude_session_id) ?? next.session.claudeSessionId,
      workspace: str(snapshot.workspace) ?? str(init.workspace),
      project: str(snapshot.project) ?? str(init.project),
      model: str(snapshot.model),
      effort: str(snapshot.effort),
      backgroundTasks: 0,
    },
    sessions: { ...next.sessions, attachedId: p.session_id },
    serverHelp: null,
    starting: null,
    honesty: parseHonesty(snapshot.honesty),
    honestyStored: resumed ? state.honestyStored : null,
    usage: { ...next.usage, rateLimits: parseRateLimits(snapshot.rate_limits), context: resumed ? next.usage.context : null },
    focus: firstOpen ? { tab: firstOpen.agentKey, seq: (state.focus?.seq ?? 0) + 1 } : state.focus,
    lastSeq: Math.max(next.lastSeq, snapshotSeq),
  };
}

/** Text of the divider above the recorded turns. */
export const HISTORY_START_TEXT = 'Earlier in this conversation';

/** Text of the divider below the recorded turns. */
export const HISTORY_END_TEXT = 'From calu\'s record: messages, steps and answers, without thinking and diffs';

/**
 * Put the recorded turns of the conversation above the transcript (reducer case `ui.history`).
 *
 * The events of an attach only reach back to the start of the session's process (or less, once
 * the replay buffer moved on). A resumed, forked or rewound conversation has earlier turns; the
 * host asks `session.history` after the attach and sends the result here.
 *
 *     Main before:  [events of this session ...]
 *     Main after:   divider "Earlier in this conversation"
 *                   user · tool · tool · assistant · turn      (per recorded turn, `stored`)
 *                   divider "From calu's record ..."
 *                   [events of this session ...]
 *
 * Which turns are added is decided by `historyEntries` (those the replayed events do not
 * cover). A recorded user message keeps the conversation's turn number and the prompt's uuid,
 * so "Rewind" and "Fork from here" work on it. A recorded step is a tool row with the decision
 * and the checker's summary; its input and the start of its result are shown if the record
 * holds them. Steps of subagents are rows of Main (the record names no tab).
 *
 * @param state The current state (not modified).
 * @param p The params: `session_id` (the session the history was asked from) and `history`
 *   (the result of `session.history`, untrusted).
 * Side effect on the numbering: `turnBase` becomes the number of turns before the first
 * replayed one, and the user messages of the replayed events are renumbered to the
 * conversation's turn numbers.
 *
 * @returns The new state; unchanged if another session is shown now or if the history was
 *   already applied (`turnBase` is set). If the record holds nothing the transcript lacks,
 *   only the numbering changes.
 */
export function applyHistory(state: ViewState, p: Record<string, unknown>): ViewState {
  if (typeof p.session_id !== 'string' || p.session_id !== state.session.sessionId) return state;
  if (state.turnBase !== null) return state;
  const main = own(state.items, MAIN) ?? [];
  const users = main.filter((i): i is Extract<Item, { kind: 'user' }> => i.kind === 'user');
  const relayed = Object.keys(state.items).flatMap((key) =>
    key === MAIN ? [] : state.items[key].filter((i): i is Extract<Item, { kind: 'user' }> => i.kind === 'user'),
  );
  const history = historyEntries(p.history, {
    uuids: new Set([...users, ...relayed].map((u) => u.uuid).filter((u): u is string => u !== null)),
    inputs: new Set(users.filter((u) => u.source === 'user').map((u) => u.text.trim())),
    toolUseIds: new Set(Object.keys(state.tools)),
  });
  // Number the replayed turns as the conversation does.
  const base = Math.max(0, history.firstReplayedTurn - 1);
  let numbered: ViewState = { ...state, turnBase: base };
  if (base > 0) {
    let items = state.items;
    for (const key of Object.keys(state.items)) {
      if (!state.items[key].some((i) => i.kind === 'user')) continue;
      items = withEntry(items, key, state.items[key].map((i) => (i.kind === 'user' ? { ...i, turn: i.turn + base } : i)));
    }
    numbered = { ...numbered, items };
  }
  if (history.entries.length === 0) return numbered;
  state = numbered;
  const live = own(state.items, MAIN) ?? [];

  let nextId = state.nextId;
  let tools = state.tools;
  const omitted = history.omittedTurns > 0 ? ` (${history.omittedTurns} older turn${history.omittedTurns === 1 ? ' is' : 's are'} not shown)` : '';
  const items: Item[] = [{ id: nextId++, kind: 'divider', mark: 'history-start', text: `${HISTORY_START_TEXT}${omitted}` }];
  for (const entry of history.entries) {
    const id = nextId++;
    items.push(historyItem(entry, id));
    if (entry.kind !== 'step') continue;
    const toolUseId = historyToolId(entry, id);
    tools = withEntry(tools, toolUseId, {
      toolUseId,
      agentKey: MAIN,
      name: entry.name,
      input: boundInput(entry.input),
      ...(entry.decision
        ? { check: { route: 'stored', decision: entry.decision, reason: truncate(entry.reason), summary: truncate(entry.summary), comment: '', checkerRoute: '' } }
        : {}),
      ...(entry.result !== null ? { result: { content: truncate(entry.result), isError: false } } : {}),
      stored: { subagent: entry.subagent, hasInput: entry.input !== null },
    });
  }
  items.push({ id: nextId++, kind: 'divider', mark: 'history-end', text: HISTORY_END_TEXT });
  return { ...state, nextId, tools, items: withEntry(state.items, MAIN, [...items, ...live]) };
}

/**
 * Return the key in `ViewState.tools` of a recorded step.
 *
 * @param entry The step.
 * @param id The id of its item.
 * @returns The call's own id if the record holds it, else "history:<item id>" (unique in the
 *   state, and never the id of a real call: those do not contain a colon after "history").
 */
function historyToolId(entry: Extract<HistoryEntry, { kind: 'step' }>, id: number): string {
  return entry.toolUseId ?? `history:${id}`;
}

/**
 * Build the transcript item of one recorded entry.
 *
 * @param entry The entry.
 * @param id The item's id.
 * @returns The item, marked `stored` (a tool item refers to the `ToolView` of `historyToolId`).
 */
function historyItem(entry: HistoryEntry, id: number): Item {
  switch (entry.kind) {
    case 'user':
      return { id, kind: 'user', text: truncate(entry.text, MAX_AGENT_RESULT_TEXT), source: 'user', images: [], turn: entry.turn, uuid: entry.uuid, stored: true };
    case 'step':
      return { id, kind: 'tool', toolUseId: historyToolId(entry, id) };
    case 'answer':
      return { id, kind: 'assistant', text: truncate(entry.text, MAX_AGENT_RESULT_TEXT), stored: true };
    case 'turnEnd':
      return { id, kind: 'turn', subtype: 'success', isError: false, costUsd: null, backgroundTasks: 0, stored: true };
  }
}

/**
 * Return who receives a chat message written while `tab` is shown: the agent of that tab.
 * Only the main agent and running background subagents can receive messages (calu rejects
 * others); an unknown tab key sends to the main agent.
 *
 * @param state The current state.
 * @param tab The visible tab: `MAIN`, an agent_id, or another tab key.
 * @returns `{to, label}` for a possible recipient (`label` names it for the input's
 *   placeholder), or `{to: null, label, reason}` if the tab's subagent cannot receive messages.
 */
export function messageTarget(
  state: ViewState,
  tab: string,
): { to: string; label: string } | { to: null; label: string; reason: string } {
  const agent = state.agents.find((a) => a.agent_id === tab);
  if (!agent) return { to: MAIN, label: 'Claude (main agent)' };
  const label = `subagent "${agent.description || agent.agent_id}"`;
  if ((agent.status ?? 'running') !== 'running') {
    return { to: null, label, reason: `The ${label} has finished; switch to Main to write to Claude.` };
  }
  if (!agent.background) {
    return {
      to: null,
      label,
      reason: `The ${label} runs in the foreground and cannot receive messages; switch to Main to write to Claude.`,
    };
  }
  return { to: agent.agent_id, label };
}

/**
 * Count the open escalations (for the view's badge).
 *
 * @param state The current state.
 * @returns The number of escalations with status "open".
 */
export function openEscalationCount(state: ViewState): number {
  return Object.values(state.escalations).filter((e) => e.status === 'open').length;
}

/**
 * Tell whether the chat can send requests to a session.
 *
 * @param state The current state.
 * @returns True if the bridge runs and a session is attached (also while reconnecting).
 */
export function isAttached(state: ViewState): boolean {
  return state.bridge.status === 'running' && (state.link.status === 'attached' || state.link.status === 'reconnecting');
}

/**
 * Return the side questions that were answered, as the `history` of the next `side.ask`.
 *
 * @param state The current state.
 * @returns `{question, answer}` pairs, oldest first, at most `MAX_SIDE_EXCHANGES`.
 */
export function sideHistory(state: ViewState): { question: string; answer: string }[] {
  return state.side
    .filter((s): s is SideExchangeView & { answer: string } => typeof s.answer === 'string' && s.answer !== '')
    .map((s) => ({ question: s.question, answer: s.answer }))
    .slice(-MAX_SIDE_EXCHANGES);
}

/**
 * Tell whether a file path came from calu as a file link of a tool call, so the host may open
 * it when the webview asks (`openFile`).
 *
 * @param state The host's state.
 * @param path An absolute path from the webview (untrusted).
 * @returns True if some tool call of the state lists exactly this path in its `files`.
 */
export function isKnownFile(state: ViewState, path: string): boolean {
  return Object.values(state.tools).some((tool) => tool.files?.some((f) => f.path === path) === true);
}
