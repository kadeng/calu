/**
 * Resolution of the command line that runs calu (`calu ide`, `calu serve`).
 *
 * Pure logic (no `vscode` import); file system checks are passed in as a function, so the
 * lookup order can be unit-tested:
 *
 *     1. setting `calu.command` (non-empty array)
 *     2. `calu` in PATH (on WSL, PATH entries under /mnt/ are skipped: they are Windows paths)
 *     3. ~/.local/bin/calu (default target of `uv tool install`)
 *     4. otherwise: not found (the extension offers to install calu)
 *
 * `findExecutable` does steps 2 and 3 for any program (`uv`, `claude`, `tmux`, `screen`).
 */

/** Facts about the extension host's machine that the lookups need. */
export interface HostFacts {
  /** Value of the PATH environment variable ("" if unset). */
  pathEnv: string;
  /** Separator of PATH entries: ":" on Linux/macOS, ";" on Windows. */
  pathDelimiter: string;
  /** Home directory of the user, or "" if unknown. */
  home: string;
  /** True if the extension host runs inside WSL. */
  isWsl: boolean;
  /** True on Windows (adds the `.exe`/`.cmd` names to the PATH lookup). */
  isWindows: boolean;
  /** Returns true if `path` is an existing executable file. Must not throw. */
  isExecutable: (path: string) => boolean;
}

/** Inputs of `resolveCaluCommand`. */
export interface ResolveInput extends HostFacts {
  /**
   * Value of the `calu.command` setting as the user wrote it (untrusted): a list of strings;
   * a single string is taken as the path of one program (the shape the PyCharm plugin's
   * "calu command" has). Empty or undefined means "not set".
   */
  configured?: unknown;
}

/** Result of `resolveCaluCommand`. */
export type ResolveResult =
  | { ok: true; command: string[]; source: 'setting' | 'path' | 'local-bin' }
  | { ok: false; message: string };

/**
 * Join a directory and a file name with the separator style of the directory.
 *
 * @param dir A directory path (POSIX or Windows style).
 * @param name A file name without separators.
 * @returns The joined path, e.g. "/usr/bin" + "calu" -> "/usr/bin/calu".
 */
export function joinPath(dir: string, name: string): string {
  const sep = dir.includes('\\') && !dir.includes('/') ? '\\' : '/';
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

/**
 * Find a program: in PATH (on WSL without the Windows entries under /mnt/), then in the given
 * directories below the home directory.
 *
 * @param name The program's file name, e.g. "uv".
 * @param facts Facts about the machine.
 * @param homeDirs Directories below the home directory to try after PATH, each as path
 *   segments; default: `[".local", "bin"]`.
 * @returns The path of the first match and where it was found, or `undefined`.
 */
export function findExecutable(
  name: string,
  facts: HostFacts,
  homeDirs: readonly (readonly string[])[] = [['.local', 'bin']],
): { path: string; source: 'path' | 'local-bin' } | undefined {
  const names = facts.isWindows ? [`${name}.exe`, `${name}.cmd`, name] : [name];
  for (const dir of facts.pathEnv.split(facts.pathDelimiter)) {
    if (!dir) continue;
    if (facts.isWsl && (dir === '/mnt' || dir.startsWith('/mnt/'))) continue;
    for (const candidateName of names) {
      const candidate = joinPath(dir, candidateName);
      if (facts.isExecutable(candidate)) return { path: candidate, source: 'path' };
    }
  }
  if (!facts.home) return undefined;
  for (const segments of homeDirs) {
    const candidate = joinPath(segments.reduce(joinPath, facts.home), name);
    if (facts.isExecutable(candidate)) return { path: candidate, source: 'local-bin' };
  }
  return undefined;
}

/**
 * Find the calu command line (see the module comment for the lookup order).
 *
 * @param input Settings and environment facts (see `ResolveInput`).
 * @returns `{ok: true, command, source}` with the argv prefix to which `ide` or `serve ...` is
 *   appended, or `{ok: false, message}` saying that calu was not found. Never throws.
 */
export function resolveCaluCommand(input: ResolveInput): ResolveResult {
  const raw = input.configured;
  let configured: string[];
  if (raw === undefined || raw === null) configured = [];
  else if (typeof raw === 'string') configured = raw.trim() ? [raw.trim()] : [];
  else if (Array.isArray(raw) && raw.every((part) => typeof part === 'string')) configured = raw.filter((part) => part !== '');
  else {
    return {
      ok: false,
      message: 'The setting `calu.command` must be a list of strings, e.g. ["/home/me/.local/bin/calu"] or ["uv", "run", "calu"].',
    };
  }
  if (configured.length > 0) {
    return { ok: true, command: [...configured], source: 'setting' };
  }
  const found = findExecutable('calu', input);
  if (found) return { ok: true, command: [found.path], source: found.source };
  return {
    ok: false,
    message:
      'calu was not found on this machine. Run "calu: Install calu" (installs it with uv into ' +
      '~/.local/bin), or set `calu.command`, e.g. ["uv", "run", "--project", "/path/to/calu", "calu"].',
  };
}

/**
 * Build the full argv for starting the bridge.
 *
 * @param command The argv prefix from `resolveCaluCommand` (non-empty).
 * @returns `[...command, "ide"]`.
 * @throws Error if `command` is empty.
 */
export function buildIdeArgv(command: readonly string[]): string[] {
  if (command.length === 0) throw new Error('the calu command is empty');
  return [...command, 'ide'];
}

/**
 * Decide from the contents of /proc/version (or os.release()) whether we run inside WSL.
 *
 * @param text The kernel version text; "" if unavailable.
 * @returns True if the text mentions Microsoft/WSL (case-insensitive).
 */
export function looksLikeWsl(text: string): boolean {
  return /microsoft|wsl/i.test(text);
}
