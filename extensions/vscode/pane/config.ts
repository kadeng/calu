/**
 * What the browser pane was opened with: the key of a `calu web` login in this origin and the
 * parts of the address's fragment. Pure apart from the storage it is given.
 *
 *     /pane#dir=/home/me/project&session=4f1c  ──readPaneConfig──►
 *         {key: "", dir: "/home/me/project", session: "4f1c"}
 *     ──keptFragment──►  "#dir=%2Fhome%2Fme%2Fproject&session=4f1c"
 *
 * A pane in a frame of another site gets neither the cookie nor the key of a login; it asks for
 * the password of `calu web` and sends it in its WebSocket's subprotocol (`loginProtocols`).
 * The password stays in the page's memory and is never stored (PLAN.md D279).
 */

/** What the pane was opened with. */
export interface PaneConfig {
  /** The browser's key of a login to `calu web` in this origin, or "". */
  key: string;
  /** The project folder for the bridge (`#dir=`), or "" for the folder `calu web` runs in. */
  dir: string;
  /** The session to attach to first (`#session=`, a session id of this machine), or "" for
   * the session of the folder. The links of `calu web`'s sessions list carry it. */
  session: string;
}

/**
 * Read the pane's address and the origin's storage.
 *
 * @param hash The address's fragment, e.g. "#dir=/home/me/project&session=4f1c".
 * @param local The origin's local storage (may be unavailable).
 * @returns The key of this browser's login (or ""), the dir and the session.
 */
export function readPaneConfig(hash: string, local: Storage | undefined): PaneConfig {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  let key = '';
  try {
    key = local?.getItem('calu-key') ?? '';
  } catch {
    key = '';
  }
  return { key, dir: params.get('dir') ?? '', session: params.get('session') ?? '' };
}

/**
 * Build the fragment the address keeps: the known parts only (an old `#token=` goes).
 *
 * @param config The pane's config.
 * @returns "#dir=...&session=...", the parts that are set, or "".
 */
export function keptFragment(config: PaneConfig): string {
  const kept = new URLSearchParams();
  if (config.dir) kept.set('dir', config.dir);
  if (config.session) kept.set('session', config.session);
  const text = kept.toString();
  return text ? `#${text}` : '';
}

/**
 * Encode text as base64url without padding (the characters a WebSocket subprotocol allows).
 *
 * @param text Any text.
 * @returns The UTF-8 bytes of `text` in base64url, e.g. "p&ss" -> "cCZzcw".
 */
export function base64url(text: string): string {
  let binary = '';
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Build the WebSocket subprotocols that carry the pane's login.
 *
 * @param config The pane's config.
 * @param password The password a person typed into the pane, or undefined.
 * @returns "calu" (the answer `calu web` accepts with), then `calu.key.<key>` if the browser
 *   has a login, then `calu.password.<base64url>` if a password was typed. `calu web` tries
 *   the key first.
 */
export function loginProtocols(config: PaneConfig, password?: string): string[] {
  const protocols = ['calu'];
  if (config.key) protocols.push(`calu.key.${config.key}`);
  if (password !== undefined) protocols.push(`calu.password.${base64url(password)}`);
  return protocols;
}
