/**
 * What the browser pane was opened with: the login token from the address's fragment (or from
 * the tab's session storage after a reload), the key of a `calu web` login in this origin, and the
 * project folder. Pure apart from the two storages it is given.
 *
 *     /pane#token=abc&dir=/home/me/project&session=4f1c  ──readPaneConfig──►
 *         {token: "abc", key: "", dir: "/home/me/project", session: "4f1c"}
 *     ──addressWithoutToken──►  "#dir=%2Fhome%2Fme%2Fproject&session=4f1c"
 */

/** Session storage key of the token taken from the address. */
const TOKEN_KEY = 'calu-pane-token';

/** What the pane was opened with. */
export interface PaneConfig {
  /** The login token, or "" (then the browser's login is used). */
  token: string;
  /** The browser's key of a login to `calu web` in this origin, or "". */
  key: string;
  /** The project folder for the bridge (`#dir=`), or "" for the folder `calu web` runs in. */
  dir: string;
  /** The session to attach to first (`#session=`, a session id of this machine), or "" for
   * the session of the folder. The links of `calu web`'s sessions list carry it. */
  session: string;
}

/**
 * Read the pane's address and storage, and remove the token from the address.
 *
 * @param hash The address's fragment, e.g. "#token=abc&dir=/home/me/project".
 * @param session The tab's session storage (may be unavailable).
 * @param local The origin's local storage (may be unavailable).
 * @returns The token (from the fragment, else from the session storage), the key, the dir and
 *   the session.
 *   Side effect: a token from the fragment is put into the session storage.
 */
export function readPaneConfig(hash: string, session: Storage | undefined, local: Storage | undefined): PaneConfig {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  let token = params.get('token') ?? '';
  try {
    if (token) session?.setItem(TOKEN_KEY, token);
    else token = session?.getItem(TOKEN_KEY) ?? '';
  } catch {
    // Storage may be blocked; the token then lives only in this page.
  }
  let key = '';
  try {
    key = local?.getItem('calu-key') ?? '';
  } catch {
    key = '';
  }
  return { token, key, dir: params.get('dir') ?? '', session: params.get('session') ?? '' };
}

/**
 * Build the fragment that stays in the address after the token was taken out.
 *
 * @param config The pane's config.
 * @returns "#dir=...&session=...", the parts that are set, or "".
 */
export function addressWithoutToken(config: PaneConfig): string {
  const kept = new URLSearchParams();
  if (config.dir) kept.set('dir', config.dir);
  if (config.session) kept.set('session', config.session);
  const text = kept.toString();
  return text ? `#${text}` : '';
}

/**
 * Build the WebSocket subprotocols that carry the pane's login.
 *
 * @param config The pane's config.
 * @returns "calu" (the answer `calu web` accepts with) and the token or the key protocol.
 */
export function loginProtocols(config: PaneConfig): string[] {
  const protocols = ['calu'];
  if (config.token) protocols.push(`calu.token.${config.token}`);
  if (config.key) protocols.push(`calu.key.${config.key}`);
  return protocols;
}
