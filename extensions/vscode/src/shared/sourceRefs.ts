/**
 * Source references in chat text: a relative path with a known file extension and an optional
 * line, such as `src/app.py:12`, `src/app.py#L12`, `src/app.py#12` or `README.md` (no
 * `vscode` import). The chat turns them into links that open the file at that line.
 *
 *     "see src/app.py:12 and docs/x.md"  ──findSourceRefs()──►  [{path: "src/app.py", line: 12},
 *                                                                {path: "docs/x.md"}]
 *
 * A reference must end in one of `SOURCE_EXTENSIONS`, must not be part of a URL or of a longer
 * word, and must not be absolute (`/etc/x.conf` and `C:\x.py` are left alone). Whether the
 * file exists is decided when the link is clicked: the host opens only files inside the
 * workspace folder. The JetBrains plugin uses the same pattern (`logic/Markdown.kt`).
 */

/** File extensions that make a path a source reference (code, configuration and text files). */
export const SOURCE_EXTENSIONS = [
  'py', 'pyi', 'ipynb', 'c', 'h', 'cc', 'cpp', 'cxx', 'hh', 'hpp', 'hxx', 'rs', 'go', 'java',
  'kt', 'kts', 'scala', 'rb', 'php', 'cs', 'swift', 'm', 'mm', 'js', 'jsx', 'mjs', 'cjs', 'ts',
  'tsx', 'vue', 'svelte', 'html', 'htm', 'css', 'scss', 'sass', 'less', 'json', 'jsonc', 'yaml',
  'yml', 'toml', 'ini', 'cfg', 'conf', 'xml', 'md', 'rst', 'txt', 'sh', 'bash', 'zsh', 'ps1',
  'sql', 'proto', 'graphql', 'gradle', 'tf', 'lua', 'r', 'dart', 'ex', 'exs', 'erl', 'hs', 'ml',
  'clj', 'zig', 'nim', 'jl', 'cmake', 'mk', 'lock', 'csv', 'tsv',
];

/**
 * The pattern of one reference. Group 1 is the path, group 2 the line (or undefined).
 * Before the path there is no word character, `.`, `/`, `\`, `:` or `@` (so no URL, no
 * absolute path and no e-mail address matches); after the reference there is no word
 * character, `/` or `\`.
 */
export const SOURCE_REF = new RegExp(
  String.raw`(?<![\w./\\:@-])((?:\.{1,2}/)?(?:[\w.-]+/)*[\w-][\w.-]*\.(?:` +
    SOURCE_EXTENSIONS.join('|') +
    String.raw`))(?:(?::|#L?)(\d+)(?::\d+)?)?(?![\w/\\])`,
  'g',
);

/** One reference found in a text. */
export interface SourceRef {
  /** Index of the first character of the reference (path and line) in the text. */
  start: number;
  /** Index after the last character of the reference. */
  end: number;
  /** The relative path as written, e.g. "src/app.py". */
  path: string;
  /** The 1-based line, if the reference names one. */
  line?: number;
}

/**
 * Find the source references in a text.
 *
 * @param text Any text (untrusted).
 * @returns The references in order; none overlap. A line of 0 is left out of its reference.
 */
export function findSourceRefs(text: string): SourceRef[] {
  const refs: SourceRef[] = [];
  for (const m of text.matchAll(SOURCE_REF)) {
    const start = m.index ?? 0;
    const line = m[2] !== undefined ? Number(m[2]) : undefined;
    refs.push({
      start,
      end: start + m[0].length,
      path: m[1],
      ...(line !== undefined && line >= 1 ? { line } : {}),
    });
  }
  return refs;
}

/**
 * Return the link target the host opens for a reference: the path, with ":<line>" if known.
 *
 * @param ref A reference of `findSourceRefs`.
 * @returns E.g. "src/app.py:12" or "docs/x.md".
 */
export function sourceRefHref(ref: SourceRef): string {
  return ref.line ? `${ref.path}:${ref.line}` : ref.path;
}

/**
 * Parse a link target into a path and a line: "src/a.py:12", "src/a.py#L12", "src/a.py#12".
 *
 * @param target A relative link target as written in Markdown (already URL-decoded).
 * @returns The path without the line part, and the 1-based line if one is given.
 */
export function splitLine(target: string): { path: string; line?: number } {
  const m = /^(.*?)(?::(\d+)(?::\d+)?|#L?(\d+)(?:-L?\d+)?)$/.exec(target);
  if (!m) return { path: target };
  const line = Number(m[2] ?? m[3]);
  return line >= 1 ? { path: m[1], line } : { path: m[1] };
}

/**
 * Tell whether a whole link target is one source reference, e.g. "hello.py:12". Such a target
 * looks like a URL scheme ("hello.py:") to a plain scheme test, but names a workspace file.
 *
 * @param target A link target (untrusted).
 * @returns True if `findSourceRefs` finds exactly one reference that spans the whole target.
 */
export function isSourceRef(target: string): boolean {
  const found = findSourceRefs(target);
  return found.length === 1 && found[0].start === 0 && found[0].end === target.length;
}
