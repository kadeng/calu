/**
 * Messages between the chat webview and the extension host (`postMessage` in both directions).
 *
 *     webview                         extension host                       calu ide
 *     ready ------------------------>  reset {state}, settings back
 *     send/decide/interrupt/... ---->  bridge.session(...)  ------------->  session.call
 *     attach/newSession/switch... -->  QuickPick, ide.attach / ide.start
 *                       <------------  event {event}  <--------------------  session.event, ide.link
 *     openFile/openDiff/openLink --->  opens editors
 *
 * The extension validates every incoming message with `parseWebviewMessage` and drops invalid
 * ones; the webview is treated as untrusted input. `WEBVIEW_CONTRACT.md` describes what the
 * host does for each message.
 */

import type { ItemActionId } from './itemMenu';
import { LEVEL_IDS, type LevelId } from './levels';
import type { ViewEvent, ViewState } from './viewState';

/** Raster formats an attached image may have (an SVG can carry scripts and is refused). */
export const IMAGE_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] as const;
/** Media type of an attached image. */
export type ImageMediaType = (typeof IMAGE_MEDIA_TYPES)[number];

/** Most images per chat message (limit of `chat.send`). */
export const MAX_IMAGES = 4;
/** Largest base64 text of one attached image the host accepts (about 7.5 MB of image data). */
export const MAX_IMAGE_BASE64 = 10_000_000;
/** Longest chat message, side question, note or answer the host accepts, in characters. */
export const MAX_TEXT = 200_000;

/** An image attached to a chat message. */
export interface OutgoingImage {
  /** The image file, base64 without a `data:` prefix. */
  data: string;
  mediaType: ImageMediaType;
}

/** Messages from the webview to the extension host. */
export type WebviewMessage =
  /** The webview loaded its script; the host answers with `reset` and `settings`. */
  | { type: 'ready' }
  /**
   * A chat message. `delivery` "queued": for the next turn; "inject": into the running turn.
   * `to`: "main" or the `agent_id` of a running background subagent. `images`: main agent
   * only. `text` may be empty only if there is an image.
   */
  | { type: 'send'; text: string; delivery: 'queued' | 'inject'; to: string; images: OutgoingImage[] }
  /**
   * The user's decision on an open escalation (`escalationId` is `EscalationView.key`). With
   * `answer` (questions of the agent) the decision is ignored by calu and may be "allow".
   */
  | { type: 'decide'; escalationId: string; decision: 'allow' | 'deny'; note?: string; answer?: string }
  /** "Why?" on an escalation: open its stored context as a read-only document. */
  | { type: 'why'; escalationId: string }
  /** A question beside the running work (`side.ask`); the answer arrives as `ui.sideAnswer`. */
  | { type: 'sideAsk'; question: string }
  /** Open the session QuickPick (attach, new, resume, fork). */
  | { type: 'attach' }
  /** Ask for an optional first message and name, then start a new session. */
  | { type: 'newSession' }
  /** Attach to the running session `sessionId` (an entry of `state.sessions.list`). */
  | { type: 'switchSession'; sessionId: string }
  /** Ask for a new name of the session `sessionId`, then rename it. */
  | { type: 'renameSession'; sessionId: string }
  /** Load the list of running sessions again. */
  | { type: 'refreshSessions' }
  /** Detach from the attached session (it keeps running). */
  | { type: 'detach' }
  /** Open a terminal with the server command (`state.serverHelp.command`). */
  | { type: 'startServer' }
  /** Copy the server command to the clipboard. */
  | { type: 'copyServerCommand' }
  /** Install calu (and uv if missing) after a confirmation. */
  | { type: 'install' }
  /** Stop and start the bridge process. */
  | { type: 'restartBridge' }
  /** Open the settings dialog. */
  | { type: 'openSettings' }
  /**
   * Open a checker's instructions: by `name`, or the checker(s) of `route` (a QuickPick if
   * several), or a QuickPick of all checkers if neither is given.
   */
  | { type: 'editChecker'; name?: string; route?: string }
  /** Open the read-only checker context document. */
  | { type: 'showContext' }
  /** Change the model of the running session (`model.set`); "default" = Claude Code's default. */
  | { type: 'setModel'; model: string }
  /** Change the effort for sessions started from now on (`claude.effort` in calu's settings). */
  | { type: 'setEffort'; effort: ClaudeEffort }
  /** Change the supervision level (`auto_escalate.set`). */
  | { type: 'setLevel'; level: LevelId }
  /** Ask the session for its usage (`usage.get`); the answer arrives as `ui.usage`. */
  | { type: 'refreshUsage' }
  /** Interrupt the running turn. */
  | { type: 'interrupt' }
  /** Open a file by absolute path; the host accepts only paths it knows from tool file links. */
  | { type: 'openFile'; path: string }
  /** Open the diff editor for the edit of tool call `toolUseId`. */
  | { type: 'openDiff'; toolUseId: string }
  /** Open file link `index` of a tool call (`ToolView.files`); the host looks the path up. */
  | { type: 'openToolFile'; toolUseId: string; index: number }
  /** A link in rendered Markdown: https links open in the browser, relative ones as files. */
  | { type: 'openLink'; href: string }
  /** A context-menu action of a tool item (see `itemMenu.ts`). */
  | { type: 'itemAction'; toolUseId: string; action: ItemActionId }
  /** Answer to `reportState` (tests): the webview's own copy of the state. */
  | { type: 'stateReport'; requestId: number; state: unknown };

/** Messages from the extension host to the webview. */
export type HostMessage =
  /** Replace the whole state (after `ready`, after an attach). */
  | { type: 'reset'; state: ViewState }
  /** Apply one event with `applyEvent`. */
  | { type: 'event'; event: ViewEvent }
  /** Answer with `stateReport {requestId, state}`. */
  | { type: 'reportState'; requestId: number }
  /** The display settings changed (also sent after `ready`). */
  | { type: 'settings'; settings: ChatSettings }
  /** A new escalation arrived: play the chime (sent only when `escalationSound` is on). */
  | { type: 'bell' }
  /** Put text into the input field (e.g. after a failed send, so nothing typed is lost). */
  | { type: 'restoreInput'; text: string };

/** Display settings of the chat view (from the `calu.chat.*` settings). */
export interface ChatSettings {
  /** Show Claude's thinking items (`calu.chat.showThinking`). */
  showThinking: boolean;
  /** Play a chime for new escalations (`calu.chat.escalationSound`). */
  escalationSound: boolean;
}

/**
 * Claude Code model aliases offered in the header; Claude Code resolves each to its latest
 * version. "default" switches back to Claude Code's own default. Any other model id that the
 * session reports is shown as it is.
 */
export const CLAUDE_MODELS = ['default', 'fable', 'opus', 'sonnet', 'haiku'] as const;
/** Effort levels of `claude.effort`; "default" is Claude Code's own default. */
export const CLAUDE_EFFORTS = ['default', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
/** An effort level. */
export type ClaudeEffort = (typeof CLAUDE_EFFORTS)[number];

/** All context-menu actions the host accepts (see `itemMenu.ts`). */
const ITEM_ACTIONS: readonly ItemActionId[] = [
  'openFile',
  'openFileAtEdit',
  'showUncommittedChanges',
  'showEdit',
  'copyPath',
  'copyCommand',
];

/** Message types without fields. */
const BARE_TYPES = [
  'ready',
  'attach',
  'newSession',
  'refreshSessions',
  'detach',
  'startServer',
  'copyServerCommand',
  'install',
  'restartBridge',
  'openSettings',
  'showContext',
  'refreshUsage',
  'interrupt',
] as const;

/**
 * Validate the `images` member of a `send` message.
 *
 * @param raw Any JSON value.
 * @returns The images, or `undefined` if `raw` is neither missing nor an array of at most
 *   `MAX_IMAGES` objects with an allowed `mediaType` and non-empty base64 `data` of at most
 *   `MAX_IMAGE_BASE64` characters.
 */
function parseImages(raw: unknown): OutgoingImage[] | undefined {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw) || raw.length > MAX_IMAGES) return undefined;
  const out: OutgoingImage[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') return undefined;
    const { data, mediaType } = entry as Record<string, unknown>;
    if (typeof data !== 'string' || !data || data.length > MAX_IMAGE_BASE64) return undefined;
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(data)) return undefined;
    if (!IMAGE_MEDIA_TYPES.includes(mediaType as ImageMediaType)) return undefined;
    out.push({ data, mediaType: mediaType as ImageMediaType });
  }
  return out;
}

/**
 * Validate a message received from the webview.
 *
 * @param raw The value passed to `onDidReceiveMessage` (any JSON value).
 * @returns The typed message with only the known fields, or `undefined` if the type is unknown
 *   or a field has the wrong type, is empty where text is required, or exceeds a size limit.
 *   Never throws.
 */
export function parseWebviewMessage(raw: unknown): WebviewMessage | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const m = raw as Record<string, unknown>;
  const str = (v: unknown): v is string => typeof v === 'string' && v.length <= MAX_TEXT;
  const text = (v: unknown): v is string => str(v) && v.trim() !== '';
  const optional = (v: unknown): string | undefined => (text(v) ? v.trim() : undefined);
  if ((BARE_TYPES as readonly unknown[]).includes(m.type)) return { type: m.type as (typeof BARE_TYPES)[number] };
  switch (m.type) {
    case 'send': {
      const images = parseImages(m.images);
      if (!str(m.text) || !text(m.to) || !images) return undefined;
      if (m.delivery !== 'queued' && m.delivery !== 'inject') return undefined;
      if (!m.text.trim() && images.length === 0) return undefined;
      if (images.length > 0 && m.to !== 'main') return undefined;
      return { type: 'send', text: m.text, delivery: m.delivery, to: m.to, images };
    }
    case 'decide': {
      if (!text(m.escalationId)) return undefined;
      if (m.decision !== 'allow' && m.decision !== 'deny') return undefined;
      const note = optional(m.note);
      const answer = optional(m.answer);
      return {
        type: 'decide',
        escalationId: m.escalationId,
        decision: m.decision,
        ...(note !== undefined ? { note } : {}),
        ...(answer !== undefined ? { answer } : {}),
      };
    }
    case 'why':
      return text(m.escalationId) ? { type: 'why', escalationId: m.escalationId } : undefined;
    case 'sideAsk':
      return text(m.question) ? { type: 'sideAsk', question: m.question.trim() } : undefined;
    case 'switchSession':
    case 'renameSession':
      return text(m.sessionId) ? { type: m.type, sessionId: m.sessionId } : undefined;
    case 'editChecker': {
      const name = optional(m.name);
      const route = optional(m.route);
      if ((m.name !== undefined && name === undefined) || (m.route !== undefined && route === undefined)) return undefined;
      return { type: 'editChecker', ...(name !== undefined ? { name } : {}), ...(route !== undefined ? { route } : {}) };
    }
    case 'setModel':
      return text(m.model) && m.model.length <= 100 ? { type: 'setModel', model: m.model.trim() } : undefined;
    case 'setEffort':
      return CLAUDE_EFFORTS.includes(m.effort as ClaudeEffort) ? { type: 'setEffort', effort: m.effort as ClaudeEffort } : undefined;
    case 'setLevel':
      return LEVEL_IDS.includes(m.level as LevelId) ? { type: 'setLevel', level: m.level as LevelId } : undefined;
    case 'openFile':
      return text(m.path) ? { type: 'openFile', path: m.path } : undefined;
    case 'openDiff':
      return text(m.toolUseId) ? { type: 'openDiff', toolUseId: m.toolUseId } : undefined;
    case 'openToolFile':
      return text(m.toolUseId) && typeof m.index === 'number' && Number.isInteger(m.index) && m.index >= 0
        ? { type: 'openToolFile', toolUseId: m.toolUseId, index: m.index }
        : undefined;
    case 'itemAction':
      if (!text(m.toolUseId) || !str(m.action) || !ITEM_ACTIONS.includes(m.action as ItemActionId)) return undefined;
      return { type: 'itemAction', toolUseId: m.toolUseId, action: m.action as ItemActionId };
    case 'openLink':
      return text(m.href) ? { type: 'openLink', href: m.href } : undefined;
    case 'stateReport':
      return typeof m.requestId === 'number' ? { type: 'stateReport', requestId: m.requestId, state: m.state } : undefined;
    default:
      return undefined;
  }
}
