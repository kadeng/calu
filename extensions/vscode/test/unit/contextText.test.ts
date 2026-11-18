// Texts of the read-only context documents, and the guards for the bridge's error data.
import { describe, expect, it } from 'vitest';
import { BridgeError, checkerConflict, neededCredential } from '../../src/bridgeProtocol';
import { CONTEXT_EVENTS, demoteHeadings, fenced, formatCheckerContext, formatEscalationContext } from '../../src/shared/contextText';

describe('checker context', () => {
  it('demotes headings outside code fences', () => {
    expect(demoteHeadings('# A\ntext\n```\n# not a heading\n```\n## B')).toBe('## A\ntext\n```\n# not a heading\n```\n### B');
    expect(demoteHeadings('~~~~\n# x\n~~~\n# still code\n~~~~\n# H')).toBe('~~~~\n# x\n~~~\n# still code\n~~~~\n## H');
  });

  it('builds the document, also from an empty context', () => {
    const text = formatCheckerContext({ chat_summary: '# Chat summary\nEarlier work.', recent: '## Turn 4\nUser: go' }, new Date(0));
    expect(text).toContain('# Folded summary\n\n## Chat summary\nEarlier work.');
    expect(text).toContain('# Recent turns\n\n### Turn 4\nUser: go');
    const empty = formatCheckerContext({}, new Date(0));
    expect(empty).toContain('_Nothing folded yet._');
    expect(empty).toContain('_No turns yet._');
  });

  it('reloads after the events that change it', () => {
    expect(CONTEXT_EVENTS.has('check.result')).toBe(true);
    expect(CONTEXT_EVENTS.has('assistant.delta')).toBe(false);
  });
});

describe('escalation context', () => {
  it('uses a fence the text cannot close', () => {
    expect(fenced('plain')).toBe('```\nplain\n```');
    expect(fenced('a ```` b', 'md')).toBe('`````md\na ```` b\n`````');
  });

  it('shows the step, the checker runs and the context of that moment', () => {
    const text = formatEscalationContext({
      escalation_id: 'e1',
      state: 'denied',
      resolved_by: 'kai',
      tool_name: 'Bash',
      note: 'not now',
      reason: 'Deletes files outside the project.',
      rendered: '# Command\n`rm -rf /tmp/x`',
      tool_input: { command: 'rm -rf /tmp/x' },
      checker_runs: [
        { checker_name: 'command', checker_version: 3, decision: 'ESCALATE', model: 'anthropic:claude-haiku-5-5', reason: 'Outside.', input_tokens: 900, output_tokens: 40, duration_ms: 1200 },
      ],
      files: { 'a.py': 'print("```")' },
      chat_summary: '# Chat summary\nSetup.',
      recent: '## Turn 2\ngo',
    });
    expect(text).toContain('# Escalation e1');
    expect(text).toContain('- **State:** denied by kai');
    expect(text).toContain('- **Note:** not now');
    expect(text).toContain('## Command\n`rm -rf /tmp/x`');
    expect(text).toContain('"command": "rm -rf /tmp/x"');
    expect(text).toContain('## command (version 3): ESCALATE\n\nanthropic:claude-haiku-5-5 · 900 in, 40 out · 1200 ms\n\nOutside.');
    expect(text).toContain('## a.py\n\n````\nprint("```")\n````');
    expect(text).toContain('# Folded summary at that moment\n\n## Chat summary');
    expect(text).toContain('### Turn 2');
  });

  it('survives a result without members', () => {
    const text = formatEscalationContext({});
    expect(text).toContain('- **State:** unknown');
    expect(text).toContain('_No checker ran');
    expect(text).not.toContain('# Files before the step');
  });
});

describe('error data of the bridge', () => {
  it('reads the credential of error -32010 and the stored checker of -32016', () => {
    expect(BridgeError.NEEDS_PASSWORD).toBe(-32010);
    expect(neededCredential({ needs: 'admin' })).toBe('admin');
    expect(neededCredential({ needs: 5 })).toBeUndefined();
    expect(neededCredential(null)).toBeUndefined();
    expect(BridgeError.CHECKER_CONFLICT).toBe(-32016);
    expect(checkerConflict({ version: 4, instructions: 'text' })).toEqual({ version: 4, instructions: 'text' });
    expect(checkerConflict({ version: '4', instructions: 'text' })).toBeUndefined();
    expect(checkerConflict('x')).toBeUndefined();
  });
});
