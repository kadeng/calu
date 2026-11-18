/**
 * Texts of the read-only context documents (no `vscode` import):
 *
 *     context.get         -> formatCheckerContext    -> calu-context:/Checker context.md
 *     escalation.context  -> formatEscalationContext -> calu-context:/escalation-<id>.md
 *
 * Both show what the checkers saw: the folded chat summary and the recent turns. The second
 * adds the step, the reason and the checker runs of one escalation, as stored with it.
 */

import type { CheckerContextResult, EscalationContextResult } from '../protocol.generated';

/** Session events after which the checker context may have changed. */
export const CONTEXT_EVENTS: ReadonlySet<string> = new Set(['check.result', 'turn.result', 'user.input', 'session.state']);

/**
 * Add one `#` to every Markdown heading outside fenced code blocks, so that an embedded text's
 * "# Chat summary" sits below the document's own top-level sections.
 *
 * @param text - Markdown text.
 * @returns The text with demoted headings; indented lines and fenced code are unchanged.
 */
export function demoteHeadings(text: string): string {
  let fence: string | undefined;
  return text
    .split('\n')
    .map((line) => {
      const marker = /^(`{3,}|~{3,})/.exec(line)?.[1];
      if (marker && (!fence || marker.startsWith(fence))) {
        fence = fence ? undefined : marker;
        return line;
      }
      return !fence && /^#{1,5} /.test(line) ? `#${line}` : line;
    })
    .join('\n');
}

/**
 * Wrap a text in a code fence that the text cannot close.
 *
 * @param text Any text.
 * @param language Language tag after the opening fence; "" for none.
 * @returns The fenced block: the fence is one backtick longer than the longest run of
 *   backticks in `text`, at least three.
 */
export function fenced(text: string, language = ''): string {
  const longest = Math.max(2, ...[...text.matchAll(/`+/g)].map((m) => m[0].length));
  const fence = '`'.repeat(longest + 1);
  return `${fence}${language}\n${text}\n${fence}`;
}

/**
 * Build the Markdown of the "Checker context" document.
 *
 * @param context - The result of `context.get`: `chat_summary` (the folded summary) and
 *   `recent` (the turns not folded yet, with "## Turn N" headings).
 * @param loadedAt - Time of the request, shown in the document's header line.
 * @returns The document text: a header, "# Folded summary" and "# Recent turns", embedded
 *   headings one level lower.
 */
export function formatCheckerContext(context: Partial<CheckerContextResult>, loadedAt: Date): string {
  const summaryText = typeof context.chat_summary === 'string' ? context.chat_summary.trim() : '';
  const recentText = typeof context.recent === 'string' ? context.recent.trim() : '';
  return [
    '# Checker context',
    '',
    `The conversation as the checkers see it, loaded ${loadedAt.toLocaleTimeString()}. Read-only; it`,
    'reloads after each checked step and turn while it is open. Not shown: the checker instructions',
    'and the step being checked.',
    '',
    '# Folded summary',
    '',
    summaryText ? demoteHeadings(summaryText) : '_Nothing folded yet._',
    '',
    '# Recent turns',
    '',
    recentText ? demoteHeadings(recentText) : '_No turns yet._',
    '',
  ].join('\n');
}

/**
 * Build the Markdown of an escalation's context document ("Why?").
 *
 * @param context The result of `escalation.context` (members may be missing).
 * @returns The document text: state, tool, reason, the rendered step, the checker runs (name,
 *   version, model, decision, reason, tokens, duration), the files the step would change, then
 *   the chat summary and recent turns of the moment the step was checked.
 */
export function formatEscalationContext(context: Partial<EscalationContextResult>): string {
  const lines: string[] = [`# Escalation ${context.escalation_id ?? ''}`.trimEnd(), ''];
  const resolved = context.resolved_by ? ` by ${context.resolved_by}` : '';
  lines.push(`- **State:** ${context.state ?? 'unknown'}${resolved}`);
  if (context.tool_name) lines.push(`- **Tool:** ${context.tool_name}`);
  if (context.agent_id) lines.push(`- **Subagent:** ${context.agent_id}`);
  if (context.note) lines.push(`- **Note:** ${context.note}`);
  lines.push('', '# Reason', '', context.reason?.trim() || '_No reason was stored._', '');
  lines.push('# Step', '');
  if (context.rendered?.trim()) lines.push(demoteHeadings(context.rendered.trim()), '');
  lines.push(fenced(JSON.stringify(context.tool_input ?? {}, null, 2), 'json'), '');
  lines.push('# Checker runs', '');
  const runs = Array.isArray(context.checker_runs) ? context.checker_runs : [];
  if (runs.length === 0) lines.push('_No checker ran (a rule or a setting escalated the step)._', '');
  for (const run of runs) {
    const tokens = [
      run.input_tokens != null ? `${run.input_tokens} in` : '',
      run.cache_read_tokens != null ? `${run.cache_read_tokens} cached` : '',
      run.output_tokens != null ? `${run.output_tokens} out` : '',
    ].filter(Boolean);
    const facts = [run.model, tokens.join(', '), run.duration_ms != null ? `${run.duration_ms} ms` : ''].filter(Boolean);
    lines.push(`## ${run.checker_name} (version ${run.checker_version}): ${run.decision}`, '', facts.join(' · '), '');
    if (run.reason?.trim()) lines.push(run.reason.trim(), '');
  }
  const files = context.files && typeof context.files === 'object' ? Object.entries(context.files) : [];
  if (files.length > 0) {
    lines.push('# Files before the step', '');
    for (const [path, text] of files) lines.push(`## ${path}`, '', fenced(typeof text === 'string' ? text : ''), '');
  }
  lines.push('# Folded summary at that moment', '');
  lines.push(context.chat_summary?.trim() ? demoteHeadings(context.chat_summary.trim()) : '_Nothing was folded._', '');
  lines.push('# Recent turns at that moment', '');
  lines.push(context.recent?.trim() ? demoteHeadings(context.recent.trim()) : '_No turns._', '');
  return lines.join('\n');
}
