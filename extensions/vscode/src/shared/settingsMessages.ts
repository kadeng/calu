/**
 * Messages between the settings webview ("calu Settings") and the extension host.
 *
 *     webview                               extension host                      calu ide
 *     load {project}  ------------------->  ide.settings.get, ide.models.presets,
 *                                           ide.status
 *                     <-------------------  data {settings, presets, status, project, serverHelp}
 *     set {key, value, project}  -------->  ide.settings.set    (password prompt on -32010)
 *                     <-------------------  saved {key, value}  or  error {key, message}
 *     unset {key, project}  ------------->  ide.settings.unset  -> saved {key} + data
 *     setAllModels {model, effort}  ----->  ide.models.set_all  -> data
 *     setRoleModel {role, model}  ------->  ide.models.set      -> data
 *     editChecker {name}  --------------->  opens calu-checker:/<name>.md
 *     runStatus  ------------------------>  ide.status          -> status {status}
 *     fix {action}  --------------------->  install, update, terminal, clipboard, browser
 *     ruleAdd {text, duration, scope}  -->  ide.rules.add       -> data
 *     ruleRemove {id}  ------------------>  ide.rules.remove    -> data
 *
 * The host validates every incoming message with `parseSettingsMessage` and drops invalid
 * ones; the webview is treated as untrusted input.
 */

import type { IdeModelsResult, IdeRulesResult, IdeSettingsResult, IdeStatusResult } from '../bridgeProtocol';
import type { ServerHelp } from './serverCommand';
import { FIX_ACTIONS, type FixAction } from './settingsModel';

/** Longest key, role, model or name the host accepts, in characters. */
const MAX_NAME = 300;
/** Longest JSON text of a setting value the host accepts, in characters. */
export const MAX_VALUE_JSON = 2_000_000;

/** Messages from the settings webview to the extension host. */
export type SettingsWebviewMessage =
  /** Load everything; `project`: the scope to show (`null` = global). Sent once after start. */
  | { type: 'load'; project: string | null }
  /** Store `value` (a JSON value) for `key` in the scope `project` (`null` = global). */
  | { type: 'set'; key: string; value: unknown; project: string | null }
  /** Remove the stored value of `key` in the scope ("reset to default"). */
  | { type: 'unset'; key: string; project: string | null }
  /**
   * Use one model for every role. `model`: a model string or "default". `effort`: optional
   * effort of Claude Code's own model.
   */
  | { type: 'setAllModels'; model: string; effort?: string; project: string | null }
  /** Set the model of one role (`role` is a `roles[].key` of the presets). */
  | { type: 'setRoleModel'; role: string; model: string; project: string | null }
  /** Open a checker's instructions in an editor. */
  | { type: 'editChecker'; name: string }
  /** Run the environment check again. */
  | { type: 'runStatus' }
  /** Run a fix of the Setup card. */
  | { type: 'fix'; action: FixAction }
  /**
   * Add a temporary rule (`ide.rules.add`): `scope` "session" binds it to the attached
   * session's conversation, "project" to its project, "global" to every project. `duration`
   * is written as `/rule` takes it; calu refuses one outside 1 minute to 7 days.
   */
  | { type: 'ruleAdd'; text: string; duration: string; scope: 'session' | 'project' | 'global' }
  /** End a temporary rule before it expires (`ide.rules.remove`). */
  | { type: 'ruleRemove'; id: number };

/** Everything the dialog shows (the `data` message). */
export interface SettingsData {
  /** The scope the data belongs to (`null` = global). */
  project: string | null;
  /** `ide.settings.get`; `null` if it failed (see `problems`). */
  settings: IdeSettingsResult | null;
  /** `ide.models.presets`; `null` if it failed. */
  presets: IdeModelsResult | null;
  /** `ide.status`; `null` if it failed. */
  status: IdeStatusResult | null;
  /** How to start the server, for the Setup card. */
  serverHelp: ServerHelp | null;
  /** The root installation `calu serve --sudo` needs, as a command to copy (never run). */
  rootInstallCommand: string;
  /** `ide.rules.list`: the temporary rules that count now; `null` if it failed. */
  rules?: IdeRulesResult | null;
  /** Messages for requests that failed, e.g. "Settings: the database is not reachable". */
  problems: string[];
}

/** Messages from the extension host to the settings webview. */
export type SettingsHostMessage =
  /** Replace everything shown. */
  | ({ type: 'data' } & SettingsData)
  /**
   * A `set` or `unset` was stored. For `set`, `value` is the value after calu's validation;
   * for `unset`, `value` is absent and `unset` is true (a `data` message follows).
   */
  | { type: 'saved'; key: string; value?: unknown; unset?: boolean }
  /**
   * A request failed. `key`: the setting (show the message at its row), or one of "models",
   * "status", "load", "fix", "rules" for the other requests.
   */
  | { type: 'error'; key: string; message: string }
  /** The environment check ran again. */
  | { type: 'status'; status: IdeStatusResult }
  /** A long request started (`busy: true`) or ended; `what` names it for a progress text. */
  | { type: 'busy'; busy: boolean; what: string };

/**
 * Validate a message received from the settings webview.
 *
 * @param raw The value passed to `onDidReceiveMessage` (any JSON value).
 * @returns The typed message with only the known fields, or `undefined` if the type is
 *   unknown, a field has the wrong type or is empty where a name is required, or a value's
 *   JSON text is longer than `MAX_VALUE_JSON`. Never throws.
 */
export function parseSettingsMessage(raw: unknown): SettingsWebviewMessage | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const m = raw as Record<string, unknown>;
  const name = (v: unknown): v is string => typeof v === 'string' && v.trim() !== '' && v.length <= MAX_NAME;
  /**
   * Read the `project` member.
   *
   * @returns `{ok: true, project}` for a missing, `null` or valid name; `{ok: false}` otherwise.
   */
  const project = (): { ok: true; project: string | null } | { ok: false } => {
    if (m.project === undefined || m.project === null || m.project === '') return { ok: true, project: null };
    return name(m.project) ? { ok: true, project: m.project } : { ok: false };
  };
  const scope = project();
  switch (m.type) {
    case 'load':
      return scope.ok ? { type: 'load', project: scope.project } : undefined;
    case 'set': {
      if (!scope.ok || !name(m.key) || !('value' in m) || m.value === undefined) return undefined;
      let size: number;
      try {
        size = JSON.stringify(m.value).length;
      } catch {
        return undefined;
      }
      if (size > MAX_VALUE_JSON) return undefined;
      return { type: 'set', key: m.key, value: m.value, project: scope.project };
    }
    case 'unset':
      return scope.ok && name(m.key) ? { type: 'unset', key: m.key, project: scope.project } : undefined;
    case 'setAllModels': {
      if (!scope.ok || !name(m.model)) return undefined;
      if (m.effort !== undefined && m.effort !== null && !name(m.effort)) return undefined;
      return {
        type: 'setAllModels',
        model: m.model.trim(),
        ...(name(m.effort) ? { effort: m.effort } : {}),
        project: scope.project,
      };
    }
    case 'setRoleModel':
      // An empty model stores none: the role then uses the default model.
      if (!scope.ok || !name(m.role) || typeof m.model !== 'string' || m.model.length > MAX_NAME) return undefined;
      return { type: 'setRoleModel', role: m.role, model: m.model.trim(), project: scope.project };
    case 'editChecker':
      return name(m.name) ? { type: 'editChecker', name: m.name } : undefined;
    case 'runStatus':
      return { type: 'runStatus' };
    case 'fix':
      return FIX_ACTIONS.includes(m.action as FixAction) ? { type: 'fix', action: m.action as FixAction } : undefined;
    case 'ruleAdd': {
      const scopes = ['session', 'project', 'global'] as const;
      const chosen = scopes.find((s) => s === m.scope);
      if (typeof m.text !== 'string' || !m.text.trim() || m.text.length > 1000 || !chosen) return undefined;
      if (typeof m.duration !== 'string' || !/^\d{1,6}[mhdMHD]$/.test(m.duration.trim())) return undefined;
      return { type: 'ruleAdd', text: m.text, duration: m.duration.trim(), scope: chosen };
    }
    case 'ruleRemove':
      return Number.isSafeInteger(m.id) && (m.id as number) > 0 ? { type: 'ruleRemove', id: m.id as number } : undefined;
    default:
      return undefined;
  }
}

/**
 * Return the `key` of the `error` message that answers a failed request.
 *
 * @param message The request.
 * @returns The setting's key for `set` and `unset`; "models" for the model requests; "status"
 *   for `runStatus`; "rules" for `ruleAdd` and `ruleRemove`; "fix" for `fix` and
 *   `editChecker`; "load" for `load`.
 */
export function errorKey(message: SettingsWebviewMessage): string {
  switch (message.type) {
    case 'set':
    case 'unset':
      return message.key;
    case 'ruleAdd':
    case 'ruleRemove':
      return 'rules';
    case 'setAllModels':
    case 'setRoleModel':
      return 'models';
    case 'runStatus':
      return 'status';
    case 'load':
      return 'load';
    default:
      return 'fix';
  }
}
