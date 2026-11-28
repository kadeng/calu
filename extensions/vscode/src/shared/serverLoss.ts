/**
 * How the chat reads a lost connection to the session. Pure; no `vscode` import.
 *
 *     ide.link lost {cause}
 *       cause "unreachable" ──► ide.status ──► serverCheck
 *          "running"  (a server or a session of this host runs)  -> the session ended; the
 *                                                                   server still runs: the
 *                                                                   session list is shown
 *          "stopped" / "unknown"                                  -> "The calu server stopped"
 *                                                                   [Start server] [Retry]
 *       any other cause (revoked token, refused, closed)         -> "connection lost" with
 *                                                                   "Attach again" (as before)
 *
 *     while "The calu server stopped" is shown: Retry, or every check of the status timer
 *       serverCheck "running" -> attach as at the start (the same session, else the workspace's)
 *
 * In every unreachable case the transcript of the session stays visible, greyed, under
 * "Session ended" (`sessionEnded`), until another session is attached.
 */

import type { IdeStatusResult, LostCause } from '../bridgeProtocol';

export type { LostCause };


/** The sentence an older bridge sends for a refused connection (it sends no `cause`). */
const UNREACHABLE_TEXT = "nothing answers at the session's address";

/**
 * Read the cause of a lost link from the `ide.link` params.
 *
 * @param cause The `cause` member (untrusted).
 * @param message The `message` member (untrusted); used only when `cause` is missing, for a
 *   bridge of an older calu, whose sentence for a refused connection starts with
 *   "nothing answers at the session's address".
 * @returns The cause, or `undefined` if neither member names one.
 */
export function lostCause(cause: unknown, message: unknown): LostCause | undefined {
  if (cause === 'unreachable' || cause === 'revoked' || cause === 'refused' || cause === 'closed') return cause;
  if (cause === undefined && typeof message === 'string' && message.startsWith(UNREACHABLE_TEXT)) return 'unreachable';
  return undefined;
}

/**
 * The server status that `ide.status` reports for this host.
 *
 * "running": a `calu serve` process of this host is recorded and not known to be gone, or a
 * session of this host says it runs. "stopped": the database was read and holds neither.
 * "unknown": no status, or the status could not tell (no database, a login that may not read
 * the servers, or an older calu without `servers_running`) and no session runs.
 */
export type ServerCheck = 'running' | 'stopped' | 'unknown';

/**
 * Tell from `ide.status` whether a calu server runs on the workspace machine.
 *
 * @param status The last `ide.status` result, or null if it failed or was not asked.
 * @returns See `ServerCheck`.
 */
export function serverCheck(status: IdeStatusResult | null): ServerCheck {
  if (!status) return 'unknown';
  const servers = status.server?.servers_running;
  const sessions = status.server?.sessions_running ?? 0;
  if ((typeof servers === 'number' && servers > 0) || sessions > 0) return 'running';
  return typeof servers === 'number' && status.database?.reachable ? 'stopped' : 'unknown';
}

/**
 * Decide whether a lost link means that the calu server stopped.
 *
 * @param cause The cause of the loss (`lostCause`).
 * @param check What `ide.status` said right after the loss.
 * @returns True for an unreachable session while no server is known to run ("stopped" or
 *   "unknown": a session that nothing answers for is most likely gone with its server).
 */
export function serverStoppedAfterLoss(cause: LostCause | undefined, check: ServerCheck): boolean {
  return cause === 'unreachable' && check !== 'running';
}

/** The parts of the view state `sessionEnded` reads. */
export interface EndedInput {
  /** True if a session is attached (`isAttached`). */
  attached: boolean;
  /** The view's link state. */
  link: { status: string; cause?: LostCause };
}

/**
 * Tell whether the transcript shows a session that ended: the link to it was lost because
 * nothing answers at its address, and no session was attached since. The view greys the
 * transcript and writes "Session ended" above it; open cards can no longer be decided.
 *
 * @param state The attach state and the link of the view.
 * @returns True in that case.
 */
export function sessionEnded(state: EndedInput): boolean {
  return !state.attached && state.link.status === 'lost' && state.link.cause === 'unreachable';
}

/**
 * The text of the "server stopped" card.
 *
 * @param retriedAt Local time ("14:02:11") of the last Retry that found no server, or null
 *   before the first.
 * @returns One or two sentences.
 */
export function serverStoppedMessage(retriedAt: string | null): string {
  const base = 'The calu server stopped; the session ended with it. Start server opens a terminal that runs it.';
  return retriedAt ? `${base} Checked at ${retriedAt}: no server runs yet.` : base;
}
