// Slash commands of the composer: the table, the completion list, and what a submitted command does.
import { describe, expect, it } from 'vitest';
import { parseWebviewMessage } from '../../src/shared/messages';
import { CALU_COMMANDS, CLAUDE_COMMANDS, allCommands, completions, resolveSlash } from '../../src/shared/slashCommands';
import { renderMarkdownHtml } from '../../webview/markdown';

const table = allCommands();

describe('command table', () => {
  it('holds the extension commands and the built-in Claude Code commands, sorted', () => {
    expect(CALU_COMMANDS.map((c) => c.name)).toEqual([
      'model', 'effort', 'level', 'honesty', 'checkers', 'checker-context', 'sessions', 'new', 'fork', 'rename', 'settings', 'interrupt',
    ]);
    // No /help: a session without a terminal answers that it is not available.
    expect(CLAUDE_COMMANDS.map((c) => c.name)).toEqual(['compact', 'clear', 'usage', 'context', 'cost']);
    const names = table.map((c) => c.name);
    expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b)));
    expect(new Set(names).size).toBe(names.length);
  });

  it('adds reported commands once and drops malformed names', () => {
    const more = allCommands(['/review', 'review', 'compact', 'bad name', '']);
    expect(more.filter((c) => c.name === 'review')).toHaveLength(1);
    expect(more.find((c) => c.name === 'review')?.source).toBe('claude');
    expect(more).toHaveLength(table.length + 1);
  });
});

describe('completions', () => {
  it('opens only for one line that starts with a slash', () => {
    expect(completions('hello', table)).toEqual([]);
    expect(completions(' /model', table)).toEqual([]);
    expect(completions('/model\nmore', table)).toEqual([]);
    expect(completions('/', table)).toHaveLength(table.length);
  });

  it('ranks prefix matches before other matches', () => {
    expect(completions('/co', table).map((c) => c.label)).toEqual(['/compact', '/context', '/cost', '/checker-context']);
    expect(completions('/zzz', table)).toEqual([]);
  });

  it('completes a command with values to "/name " and offers the values next', () => {
    const model = completions('/mod', table)[0];
    expect(model).toMatchObject({ label: '/model', insert: '/model ', submit: false, kind: 'calu' });
    expect(completions('/model ', table).map((c) => c.label)).toEqual(['default', 'fable', 'opus', 'sonnet', 'haiku']);
    expect(completions('/model so', table)).toEqual([expect.objectContaining({ label: 'sonnet', insert: '/model sonnet', submit: true, kind: 'value' })]);
    expect(completions('/level ', table).map((c) => c.label)).toEqual(['checkers', 'edits', 'writes', 'everything']);
  });

  it('submits commands without values at once and stops after an argument', () => {
    expect(completions('/sett', table)[0]).toMatchObject({ insert: '/settings', submit: true });
    expect(completions('/compact keep the plan', table)).toEqual([]);
    expect(completions('/compact ', table)).toEqual([]);
  });
});

describe('resolveSlash', () => {
  it('sends Claude Code commands, unknown commands and plain text as chat', () => {
    for (const text of ['hello', '/compact', '/compact keep the plan', '/my-project-command arg', '/ not a command', '/context']) {
      expect(resolveSlash(text, 's1'), text).toEqual({ kind: 'chat' });
    }
  });

  it('maps extension commands to messages the host accepts', () => {
    const cases: [string, object][] = [
      ['/model opus', { type: 'setModel', model: 'opus' }],
      ['/model claude-opus-4-5[1m]', { type: 'setModel', model: 'claude-opus-4-5[1m]' }],
      ['/effort high', { type: 'setEffort', effort: 'high' }],
      ['/level writes', { type: 'setLevel', level: 'writes' }],
      ['/honesty enforce', { type: 'setHonestyMode', mode: 'enforce' }],
      ['/checkers', { type: 'editChecker' }],
      ['/checker-context', { type: 'showContext' }],
      ['/new', { type: 'newSession' }],
      ['/fork', { type: 'forkConversation' }],
      ['/rename', { type: 'renameSession', sessionId: 's1' }],
      ['/settings', { type: 'openSettings' }],
      ['  /interrupt  ', { type: 'interrupt' }],
    ];
    for (const [text, message] of cases) {
      expect(resolveSlash(text, 's1'), text).toEqual({ kind: 'message', message });
      expect(parseWebviewMessage(message), text).toEqual(message);
    }
  });

  it('opens a part of the view when a choice is missing', () => {
    expect(resolveSlash('/sessions', null)).toEqual({ kind: 'ui', action: 'sessions' });
    for (const name of ['model', 'effort', 'level', 'honesty']) expect(resolveSlash(`/${name}`, 's1')).toEqual({ kind: 'ui', action: name });
  });

  it('explains a wrong value and a missing session', () => {
    expect(resolveSlash('/effort extreme', 's1')).toEqual({ kind: 'error', text: '/effort takes one of: default, low, medium, high, xhigh, max.' });
    expect(resolveSlash('/level custom', 's1').kind).toBe('error');
    expect(resolveSlash('/model has space', 's1').kind).toBe('error');
    for (const text of ['/fork', '/rename', '/interrupt', '/model opus', '/level edits', '/checker-context']) {
      expect(resolveSlash(text, null), text).toMatchObject({ kind: 'error', text: expect.stringContaining('needs an attached session') });
    }
    expect(resolveSlash('/settings', null).kind).toBe('message');
  });
});

describe('Markdown rendering', () => {
  it('escapes raw HTML and renders images as links', () => {
    const html = renderMarkdownHtml('<img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n\n![alt text](https://example.org/p.png)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<script');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('<a href="https://example.org/p.png">[image: alt text]</a>');
  });

  it('refuses script links and linkifies plain addresses', () => {
    expect(renderMarkdownHtml('[x](javascript:alert(1))')).not.toContain('href="javascript:');
    expect(renderMarkdownHtml('[x](file:///etc/passwd)')).not.toContain('href="file:');
    expect(renderMarkdownHtml('see https://example.org now')).toContain('<a href="https://example.org">');
  });
});
