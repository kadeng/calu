/**
 * A unified diff of two texts, for the browser pane's view of an edit (VSCode shows its own
 * diff editor instead). Pure; no DOM.
 *
 *     unifiedDiff("a\nb\nc", "a\nB\nc", "x.py")  ==  "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c"
 */

/** Largest product of the two line counts that is diffed line by line (time and memory). */
export const MAX_CELLS = 4_000_000;

/** Lines of context around each change. */
const CONTEXT = 3;

/**
 * Build a unified diff of two texts.
 *
 * @param before The old text.
 * @param after The new text.
 * @param path The file's path, for the header lines.
 * @returns The diff with `---`/`+++` header and `@@` hunks; "" if the texts are equal. If the
 *   texts have more than `MAX_CELLS` line pairs, the whole old text is shown as removed and the
 *   whole new text as added (one hunk).
 */
export function unifiedDiff(before: string, after: string, path: string): string {
  if (before === after) return '';
  const a = before.split('\n');
  const b = after.split('\n');
  const ops = a.length * b.length > MAX_CELLS ? replaceAll(a, b) : lcsOps(a, b);
  return [`--- a/${path}`, `+++ b/${path}`, ...hunks(ops)].join('\n');
}

/** One line of the edit script: kept (" "), removed ("-") or added ("+"). */
interface Op {
  kind: ' ' | '-' | '+';
  text: string;
}

/**
 * The edit script that removes every old line and adds every new one.
 *
 * @param a Old lines.
 * @param b New lines.
 * @returns The script.
 */
function replaceAll(a: string[], b: string[]): Op[] {
  return [...a.map((text) => ({ kind: '-' as const, text })), ...b.map((text) => ({ kind: '+' as const, text }))];
}

/**
 * The shortest edit script by the longest common subsequence of lines.
 *
 * @param a Old lines.
 * @param b New lines.
 * @returns The script, removals before additions at each change.
 */
function lcsOps(a: string[], b: string[]): Op[] {
  const n = a.length;
  const m = b.length;
  // lengths[i][j] = LCS length of a[i:] and b[j:], one flat array.
  const lengths = new Uint32Array((n + 1) * (m + 1));
  const at = (i: number, j: number): number => i * (m + 1) + j;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lengths[at(i, j)] = a[i] === b[j] ? lengths[at(i + 1, j + 1)] + 1 : Math.max(lengths[at(i + 1, j)], lengths[at(i, j + 1)]);
    }
  }
  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) {
      ops.push({ kind: ' ', text: a[i] });
      i++;
      j++;
    } else if (j < m && (i === n || lengths[at(i, j + 1)] > lengths[at(i + 1, j)])) {
      ops.push({ kind: '+', text: b[j] });
      j++;
    } else {
      ops.push({ kind: '-', text: a[i] });
      i++;
    }
  }
  return ops;
}

/**
 * Group an edit script into hunks with `CONTEXT` lines around each change.
 *
 * @param ops The script.
 * @returns The hunk lines, each hunk starting with its `@@ -l,s +l,s @@` header.
 */
function hunks(ops: Op[]): string[] {
  const out: string[] = [];
  const changed = ops.map((o, index) => (o.kind === ' ' ? -1 : index)).filter((index) => index >= 0);
  let k = 0;
  while (k < changed.length) {
    const start = Math.max(0, changed[k] - CONTEXT);
    let end = Math.min(ops.length, changed[k] + CONTEXT + 1);
    while (k + 1 < changed.length && changed[k + 1] - CONTEXT <= end) {
      k++;
      end = Math.min(ops.length, changed[k] + CONTEXT + 1);
    }
    k++;
    let oldLine = 1;
    let newLine = 1;
    for (const o of ops.slice(0, start)) {
      if (o.kind !== '+') oldLine++;
      if (o.kind !== '-') newLine++;
    }
    const body = ops.slice(start, end);
    const oldCount = body.filter((o) => o.kind !== '+').length;
    const newCount = body.filter((o) => o.kind !== '-').length;
    out.push(`@@ -${oldLine},${oldCount} +${newLine},${newCount} @@`, ...body.map((o) => o.kind + o.text));
  }
  return out;
}
