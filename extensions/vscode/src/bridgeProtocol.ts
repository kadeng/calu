/**
 * Types of the bridge protocol: what the extension asks `calu ide` and what `calu ide` tells the
 * extension (agent/records/notes/vscode-extension-spec.md, sections 2 and 6). Hand-written; the session
 * protocol that the bridge relays is in `protocol.generated.ts`.
 *
 *     extension --request--> calu ide            ide.* and session.call {method, params}
 *     extension <--notification-- calu ide       session.event {method, params}, ide.link, ide.log
 *
 * Passwords (section 8): the extension sends one password with `ide.unlock`, once per bridge
 * process; the bridge holds the logins it opened, and later requests carry no password.
 *
 *     request --> error -32010 {needs, unlocked} --> ide.unlock {password} --> same request again
 *
 * No `vscode` import and no code besides constants and three small type guards.
 */

import type {
  CheckerInfo,
  InitializeResult,
  RecordedEvent,
  RequestMethod,
  RequestParams,
  RequestResults,
  SessionSnapshot,
} from './protocol.generated';

// ------------------------------------------------------------------ error codes

/** Error codes of the bridge (JSON-RPC `error.code`). */
export const BridgeError = {
  /** A value was refused by calu's validation; the message says why. */
  INVALID_PARAMS: -32602,
  /**
   * A credential needs a password: `data.needs` names it, `data.unlocked` lists the logins the
   * bridge holds. Send `ide.unlock`, then repeat the request. Also the answer of `ide.unlock`
   * to a wrong password.
   */
  NEEDS_PASSWORD: -32010,
  /** `ide.attach`: no session matches. */
  NO_SESSION: -32011,
  /** `ide.attach`: several sessions match (`data.sessions`). */
  AMBIGUOUS_SESSION: -32012,
  /** `ide.start`: no `calu serve` answered the request. */
  NO_SERVER: -32013,
  /** `ide.start`: the server refused the request (its message). */
  SERVER_REFUSED: -32014,
  /** `session.call`: no session is attached. */
  NOT_ATTACHED: -32015,
  /** `ide.checker.save`: the stored version differs (`data.version`, `data.instructions`). */
  CHECKER_CONFLICT: -32016,
  /** No usable database credential is stored, or the database cannot be reached. */
  NO_DATABASE: -32017,
  /** `ide.start`: the editor took the request back with `ide.start.cancel`. */
  START_CANCELLED: -32018,
} as const;

/** `data` of error -32010. */
export interface NeedsPasswordData {
  /** Name of the credential whose password is needed, e.g. "admin". */
  needs: string;
  /** Names of the logins this bridge process already holds; missing means none. */
  unlocked?: string[];
}

/** `data` of error -32016. */
export interface CheckerConflictData {
  /** The version stored now. */
  version: number;
  /** The instructions stored now. */
  instructions: string;
}

// ------------------------------------------------------------------ results and params

/** Result of `ide.initialize`. */
export interface IdeInitializeResult {
  /** calu's version. */
  bridge_version: string;
  /** Version of the session protocol the bridge relays. */
  protocol_version: number;
  /** The workspace folder the bridge runs in (its working directory). */
  workspace: string;
}

/** One line of the environment check (`ide.status`). */
export interface Finding {
  /** What was checked, e.g. "claude", "uv", "git", "tmux", "screen", "bubblewrap". */
  name: string;
  /** True if nothing is missing. */
  ok: boolean;
  level: 'ok' | 'missing' | 'note';
  /** What was found, e.g. "claude 2.1.80". */
  message: string;
  /** What to do about it; "" if nothing. */
  advice: string;
}

/** Result of `ide.status`. */
export interface IdeStatusResult {
  findings: Finding[];
  database: {
    reachable: boolean;
    /** Where the database is, without a secret. */
    place: string;
    /** Why it is not reachable; "" if it is. */
    problem: string;
  };
  server: {
    /** Number of sessions running on this host. */
    sessions_running: number;
    /**
     * Number of `calu serve` processes of this host that are recorded and not known to be
     * gone; null (or missing, from an older calu) when the bridge cannot tell.
     */
    servers_running?: number | null;
  };
  /** True if a protected credential exists and the bridge holds no login; missing means false. */
  locked?: boolean;
  /** Names of the logins the bridge holds; missing means none. */
  unlocked?: string[];
}

/** Result of `ide.unlock`. */
export interface IdeUnlockResult {
  /** Names of all logins the bridge holds after this request. */
  unlocked: string[];
  /** True if a reading request now finds a login without a password. */
  reading: boolean;
  /** True if a request that changes settings now finds a login without a password. */
  changing: boolean;
}

/** A running session of this host (`ide.sessions`.running). */
export interface RunningSession {
  id: string;
  /** Name given by the user ("" = none, at most 80 characters). */
  name?: string;
  directory: string;
  /** Host the session runs on (other hosts are listed only with `all`). */
  hostname?: string;
  /** Session state as in `session.state`. */
  state: string;
  claude_session_id: string | null;
  open_escalations: number;
  started_at: string;
  /** True if the session's process seems to be gone. */
  stale: boolean;
  /** True if the session's directory is the workspace folder or an ancestor of it. */
  here: boolean;
}

/** A stored conversation of the workspace (`ide.sessions`.stored). */
export interface StoredConversation {
  claude_session_id: string;
  /** The last thing the user wrote in it. */
  last_input: string;
  turns: number;
  /** Seconds since its last entry. */
  age_s: number;
  /** True if a running session uses this conversation. */
  running: boolean;
  /** True if the conversation was started without calu. */
  outside: boolean;
}

/** Result of `ide.sessions`. */
export interface IdeSessionsResult {
  running: RunningSession[];
  stored: StoredConversation[];
}

/** Result of `ide.attach` and `ide.start`. */
export interface IdeAttachResult {
  attached: true;
  session_id: string;
  initialize: InitializeResult;
  snapshot: SessionSnapshot;
  /** Recorded events after `after_seq`, oldest first. */
  events: RecordedEvent[];
  /** False if older events are no longer in the buffer (`snapshot` is then the only source). */
  complete: boolean;
}

/** Params of `ide.start`. */
export interface IdeStartParams {
  kind: 'new' | 'resume' | 'fork';
  /** The conversation to resume or fork. */
  claude_session_id?: string | null;
  /** For "fork": the turn before which the conversation is cut. */
  fork_before_turn?: number | null;
  /** First message of the session. */
  prompt?: string | null;
  model?: string | null;
  effort?: string | null;
  project?: string | null;
  /** Name of the session (section 6). */
  name?: string | null;
}

/** One setting of calu (`ide.settings.get`.rows). */
export interface SettingRow {
  /** Dotted key, e.g. "claude.model". */
  key: string;
  /** Type as text for the user, e.g. "true/false", "text (or empty)". */
  type: string;
  /** The default as text for the user. */
  default: string;
  /** The default as a JSON value. */
  default_value?: unknown;
  description: string;
  /**
   * What is stored in the asked scope, else what the scope inherits (the global value in a
   * project, else the default), as a JSON value.
   */
  value: unknown;
  /** True if a value is stored in the asked scope (the row is "modified"). */
  is_set: boolean;
  /** Where the value comes from when `is_set` is false. */
  inherits?: 'global' | 'default';
  /**
   * Control kind computed by calu: "bool", "choice" (with `choices`), "int", "number", "text",
   * "model" (a model string), "markdown", "lines" (a list of strings), "json" (any other
   * value), "section" (a nested group of settings without a control of its own).
   */
  kind: string;
  /** Allowed values of a choice; empty or null otherwise. */
  choices?: string[] | null;
}

/** A checker definition as the settings list it. */
export interface CheckerRow {
  name: string;
  route: string;
  model: string;
  enabled: boolean;
  version: number;
  request_limit?: number | null;
  output_tokens_limit?: number | null;
  max_tokens?: number | null;
}

/** Result of `ide.settings.get`. */
export interface IdeSettingsResult {
  /** `Config.model_json_schema()`. */
  schema: Record<string, unknown>;
  rows: SettingRow[];
  checkers: CheckerRow[];
  /** Names of the projects that can have their own values. */
  projects: string[];
}

/** One model preset (`ide.models.presets`.presets). */
export interface ModelPreset {
  id: string;
  label: string;
  group: string;
  /** The model string; "" or null for calu's default. */
  model: string | null;
  /** What the preset needs (which key or sign-in). */
  note: string;
}

/** A role that has a model (`ide.models.presets`.roles). */
export interface ModelRole {
  /** Key for `ide.models.set`. */
  key: string;
  label: string;
  /** The stored model; "" means the default model. */
  model: string;
}

/** Result of `ide.models.presets`. */
export interface IdeModelsResult {
  presets: ModelPreset[];
  roles: ModelRole[];
  /** Claude Code's own model settings. */
  claude: { model: string | null; effort: string | null; use_bedrock: boolean };
  /** The model a role without a stored model uses. */
  default_model?: string;
}

/** Result of `ide.checker.get`. */
export interface IdeCheckerResult {
  name: string;
  route: string;
  model: string;
  version: number;
  enabled: boolean;
  instructions: string;
}

/**
 * A parameter that requests using stored data accept. Other clients send it; the extension
 * does not: it sends `ide.unlock` once instead (see `BridgeError.NEEDS_PASSWORD`).
 */
interface WithPassword {
  /** Password of the credential named by error -32010. */
  password?: string;
}

/** Bridge request method -> type of its `params`. */
export interface BridgeRequestParams {
  'ide.initialize': { client: string; client_version: string };
  'ide.status': Record<string, never>;
  /** The one password; a wrong one is answered with error -32010. */
  'ide.unlock': { password: string };
  'ide.sessions': { all?: boolean } & WithPassword;
  'ide.attach': { session: string | null; after_seq?: number; address?: string; token?: string };
  'ide.detach': Record<string, never>;
  'ide.start': IdeStartParams;
  'ide.start.cancel': Record<string, never>;
  'ide.session.rename': { session_id: string; name: string } & WithPassword;
  'session.call': { method: string; params: object };
  'ide.settings.get': { project?: string | null } & WithPassword;
  'ide.settings.set': { key: string; value: unknown; project?: string | null } & WithPassword;
  'ide.settings.unset': { key: string; project?: string | null } & WithPassword;
  'ide.models.presets': { project?: string | null } & WithPassword;
  'ide.models.set_all': { model: string; effort?: string; project?: string | null } & WithPassword;
  'ide.models.set': { role: string; model: string; project?: string | null } & WithPassword;
  'ide.checker.get': { name: string } & WithPassword;
  'ide.checker.save': { name: string; instructions: string; base_version: number } & WithPassword;
  'ide.shutdown': Record<string, never>;
}

/** Bridge request method -> type of its `result`. */
export interface BridgeRequestResults {
  'ide.initialize': IdeInitializeResult;
  'ide.status': IdeStatusResult;
  'ide.unlock': IdeUnlockResult;
  'ide.sessions': IdeSessionsResult;
  'ide.attach': IdeAttachResult;
  'ide.detach': Record<string, never>;
  'ide.start': IdeAttachResult;
  /** How many waiting `ide.start` requests were taken back (each fails with -32018). */
  'ide.start.cancel': { cancelled: number };
  'ide.session.rename': { name: string };
  'session.call': unknown;
  'ide.settings.get': IdeSettingsResult;
  /** `offer`: a related switch to offer as well (D253); set only on the user's yes. */
  'ide.settings.set': { value: unknown; offer?: { key: string; text: string } };
  'ide.settings.unset': Record<string, never>;
  'ide.models.presets': IdeModelsResult;
  'ide.models.set_all': { changed: string[] };
  'ide.models.set': Record<string, never>;
  'ide.checker.get': IdeCheckerResult;
  'ide.checker.save': { version: number };
  'ide.shutdown': Record<string, never>;
}

/** Name of a bridge request. */
export type BridgeMethod = keyof BridgeRequestParams;

/** Bridge notification method -> type of its `params`. */
export interface BridgeNotifications {
  /** One event of the attached session, in order; `params.seq` is included. */
  'session.event': { method: string; params: Record<string, unknown> };
  /**
   * The connection between the bridge and the session changed. `cause` comes with "lost"
   * (missing from an older calu): see `LostCause`.
   */
  'ide.link': { state: LinkState; message: string; cause?: LostCause; session_id?: string; complete?: boolean };
  'ide.log': { level: 'debug' | 'info' | 'warning' | 'error'; message: string };
}

/** State of the connection between the bridge and the attached session. */
export type LinkState = 'attached' | 'reconnecting' | 'lost';

/**
 * Why the bridge gave up the connection (`ide.link lost` `cause`, set by `calu.ide.link`):
 * "unreachable" nothing answers at the session's address (its process or its server stopped,
 * or the network is down); "revoked" the token was revoked; "refused" the session refused the
 * connection; "closed" the session closed every new connection.
 */
export type LostCause = 'unreachable' | 'revoked' | 'refused' | 'closed';

// ------------------------------------------------------------------ session requests

/**
 * Result type of a session request: the generated type, else an object without known members
 * (requests that answer `{}`).
 */
export type SessionResultOf<M extends RequestMethod> = M extends keyof RequestResults
  ? RequestResults[M]
  : Record<string, unknown>;

/** Params type of a session request (re-exported for `Bridge.session`). */
export type SessionParamsOf<M extends RequestMethod> = RequestParams[M];

/** A checker as either source lists it (`ide.settings.get`.checkers or `checkers.get`). */
export type AnyChecker = Pick<CheckerRow, 'name' | 'route' | 'model'> | Pick<CheckerInfo, 'name' | 'route' | 'model'>;

// ------------------------------------------------------------------ type guards for error data

/**
 * Read the credential name from the `data` of error -32010.
 *
 * @param data The error's `data` member (any JSON value).
 * @returns The name in `data.needs`, or `undefined` if `data` has no such string.
 */
export function neededCredential(data: unknown): string | undefined {
  if (typeof data !== 'object' || data === null) return undefined;
  const needs = (data as Record<string, unknown>).needs;
  return typeof needs === 'string' && needs ? needs : undefined;
}

/**
 * Read the names of the held logins from the `data` of error -32010, from an `ide.unlock`
 * result or from an `ide.status` result.
 *
 * @param data Any JSON value; an object with a member `unlocked` is expected.
 * @returns The strings in `data.unlocked`; `[]` if the member is missing or not an array.
 *   Members that are not non-empty strings are left out.
 */
export function unlockedLogins(data: unknown): string[] {
  if (typeof data !== 'object' || data === null) return [];
  const unlocked = (data as Record<string, unknown>).unlocked;
  if (!Array.isArray(unlocked)) return [];
  return unlocked.filter((name): name is string => typeof name === 'string' && name !== '');
}

/**
 * Read the stored checker from the `data` of error -32016.
 *
 * @param data The error's `data` member (any JSON value).
 * @returns Version and instructions, or `undefined` if a member is missing or has another type.
 */
export function checkerConflict(data: unknown): CheckerConflictData | undefined {
  if (typeof data !== 'object' || data === null) return undefined;
  const { version, instructions } = data as Record<string, unknown>;
  return typeof version === 'number' && typeof instructions === 'string' ? { version, instructions } : undefined;
}
