import { describe, expect, it } from 'vitest';
import {
  describeRange,
  fileLinkAction,
  menuFor,
  resolveItemAction,
  shortHead,
  toolKind,
  workspaceRelative,
} from '../../src/shared/itemMenu';
import { parseWebviewMessage } from '../../src/shared/messages';
import { applyEvents, initialViewState, type ToolView, type ViewEvent } from '../../src/shared/viewState';

/**
 * Shorthand for a reducer event.
 *
 * @returns The event.
 */
function ev(method: string, params: Record<string, unknown> = {}): ViewEvent {
  return { method, params };
}

const HEAD = '0123456789abcdef0123456789abcdef01234567';

/** State with one edit and one command, as produced by the session's events. */
const state = applyEvents(initialViewState(), [
  ev('tool.use', { tool_use_id: 'e1', name: 'Edit', input: { file_path: '/w/proj/src/a.py' } }),
  ev('edit.diff', { tool_use_id: 'e1', path: 'src/a.py', before: 'x', after: 'y', start_line: 12, end_line: 15 }),
  ev('check.result', { tool_use_id: 'e1', tool_name: 'Edit', route: 'edit', decision: 'ALLOW', git_head: HEAD }),
  ev('tool.use', { tool_use_id: 'c1', name: 'Bash', input: { command: 'ls -la' } }),
  ev('check.result', { tool_use_id: 'c1', tool_name: 'Bash', route: 'command', decision: 'DENY', reason: 'no', git_head: null }),
  ev('tool.use', { tool_use_id: 'g1', name: 'Grep', input: { pattern: 'x' } }),
]);
const edit = state.tools.e1 as ToolView;
const command = state.tools.c1 as ToolView;

describe('reducer: git HEAD and edit range', () => {
  it('stores git_head from check.result and the line range from edit.diff', () => {
    expect(edit.gitHead).toBe(HEAD);
    expect(edit.editRange).toEqual({ start: 12, end: 15 });
    expect(command.gitHead).toBeNull();
  });

  it('accepts pure deletions and rejects invalid ranges', () => {
    const s = applyEvents(initialViewState(), [
      ev('edit.diff', { tool_use_id: 'd', path: 'a', before: '', after: '', start_line: 5, end_line: 4 }),
      ev('edit.diff', { tool_use_id: 'n', path: 'a', before: '', after: '', start_line: null, end_line: null }),
      ev('edit.diff', { tool_use_id: 'x', path: 'a', before: '', after: '', start_line: 5, end_line: 2 }),
    ]);
    expect(s.tools.d.editRange).toEqual({ start: 5, end: 4 });
    expect(s.tools.n.editRange).toBeNull();
    expect(s.tools.x.editRange).toBeNull();
  });
});

describe('menu model', () => {
  it('classifies tools', () => {
    expect(toolKind(edit)).toBe('edit');
    expect(toolKind({ name: 'Write' })).toBe('edit');
    expect(toolKind(command)).toBe('command');
    expect(toolKind(state.tools.g1)).toBe('other');
  });

  it('formats heads and ranges', () => {
    expect(shortHead(HEAD)).toBe('01234567');
    expect(shortHead(null)).toBe('unknown');
    expect(describeRange({ start: 3, end: 3 })).toBe('3');
    expect(describeRange({ start: 3, end: 5 })).toBe('3-5');
    expect(describeRange({ start: 3, end: 2 })).toBe('deleted before line 3');
    expect(describeRange(null)).toBe('unknown');
  });

  it('builds the edit menu', () => {
    expect(menuFor(edit)).toEqual({
      kind: 'edit',
      info: [
        ['File', 'src/a.py'],
        ['Tool', 'Edit'],
        ['Lines', '12-15'],
        ['Decision', 'ALLOW'],
        ['Git HEAD', '01234567'],
      ],
      actions: [
        { id: 'openFileAtEdit', label: 'Open file at edit' },
        { id: 'showUncommittedChanges', label: 'Show uncommitted changes of this file' },
        { id: 'showEdit', label: 'Show this edit' },
        { id: 'copyPath', label: 'Copy path' },
      ],
    });
  });

  it('builds the command menu and none for other tools', () => {
    expect(menuFor(command)).toEqual({
      kind: 'command',
      info: [
        ['Command', 'ls -la'],
        ['Decision', 'DENY'],
        ['Git HEAD', 'unknown'],
      ],
      actions: [{ id: 'copyCommand', label: 'Copy command' }],
    });
    expect(menuFor(state.tools.g1)).toBeUndefined();
  });
});

describe('resolveItemAction', () => {
  it('turns valid actions into tasks', () => {
    expect(resolveItemAction(edit, 'openFileAtEdit', '/w/proj')).toEqual({ action: 'openFileAtEdit', path: 'src/a.py', line: 12 });
    expect(resolveItemAction(edit, 'showUncommittedChanges', '/w/proj')).toEqual({
      action: 'showUncommittedChanges',
      path: 'src/a.py',
      line: 12,
    });
    expect(resolveItemAction(edit, 'showEdit', '/w/proj')).toEqual({ action: 'showEdit', toolUseId: 'e1' });
    expect(resolveItemAction(edit, 'copyPath', '/w/proj')).toEqual({ action: 'copy', text: 'src/a.py' });
    expect(resolveItemAction(command, 'copyCommand', '/w/proj')).toEqual({ action: 'copy', text: 'ls -la' });
  });

  it('rejects unknown tools, actions not offered, and paths outside the workspace', () => {
    expect(() => resolveItemAction(undefined, 'copyPath', '/w')).toThrow(/no longer/);
    expect(() => resolveItemAction(command, 'openFileAtEdit', '/w')).toThrow(/not available/);
    expect(() => resolveItemAction(state.tools.g1, 'copyCommand', '/w')).toThrow(/not available/);
    const outside = { ...edit, diffPath: '/etc/passwd' };
    expect(() => resolveItemAction(outside, 'openFileAtEdit', '/w/proj')).toThrow(/outside/);
    const dotdot = { ...edit, diffPath: 'src/../../x' };
    expect(() => resolveItemAction(dotdot, 'copyPath', '/w/proj')).toThrow(/outside/);
  });

  it('uses line 1 when the range is unknown and converts absolute paths inside the folder', () => {
    const noRange = { ...edit, editRange: null, diffPath: '/w/proj/b.py' };
    expect(resolveItemAction(noRange, 'openFileAtEdit', '/w/proj/')).toEqual({ action: 'openFileAtEdit', path: 'b.py', line: 1 });
    expect(workspaceRelative('./c.py', '/w')).toBe('c.py');
    expect(() => workspaceRelative('/w/projX/a', '/w/proj')).toThrow(/outside/);
  });
});

describe('itemAction messages', () => {
  it('accepts known actions only', () => {
    expect(parseWebviewMessage({ type: 'itemAction', toolUseId: 'e1', action: 'copyPath' })).toEqual({
      type: 'itemAction',
      toolUseId: 'e1',
      action: 'copyPath',
    });
    expect(parseWebviewMessage({ type: 'itemAction', toolUseId: 'e1', action: 'rm' })).toBeUndefined();
    expect(parseWebviewMessage({ type: 'itemAction', toolUseId: 3, action: 'copyPath' })).toBeUndefined();
  });
});

describe('workspaceRelative on Windows paths', () => {
  it('ignores the case of the drive letter', () => {
    expect(workspaceRelative(String.raw`C:\work\repo\src\a.py`, String.raw`c:\work\repo`)).toBe('src/a.py');
    expect(() => workspaceRelative(String.raw`D:\other\a.py`, String.raw`c:\work\repo`)).toThrow();
  });
});

describe('read tool calls', () => {
  const reads = applyEvents(initialViewState(), [
    ev('tool.use', { tool_use_id: 'r1', name: 'Read', input: { file_path: '/w/proj/docs/a.md', offset: 40, limit: 20 } }),
    ev('tool.use', { tool_use_id: 'r2', name: 'Read', input: { file_path: '/home/u/.claude/skills/x/SKILL.md' } }),
  ]);
  const read = reads.tools.r1 as ToolView;

  it('are their own kind, with a file link and an "Open file" action', () => {
    expect(toolKind(read)).toBe('read');
    expect(fileLinkAction(read)).toBe('openFile');
    expect(fileLinkAction(edit)).toBe('openFileAtEdit');
    expect(fileLinkAction(command)).toBeUndefined();
    expect(fileLinkAction(state.tools.g1 as ToolView)).toBeUndefined();
    expect(menuFor(read)?.actions.map((a) => a.id)).toEqual(['openFile', 'copyPath']);
  });

  it('open the file at the first line read, inside the workspace only', () => {
    expect(resolveItemAction(read, 'openFile', '/w/proj')).toEqual({ action: 'openFileAtEdit', path: 'docs/a.md', line: 40 });
    expect(() => resolveItemAction(reads.tools.r2, 'openFile', '/w/proj')).toThrow(/outside/);
    expect(parseWebviewMessage({ type: 'itemAction', toolUseId: 'r1', action: 'openFile' })).toEqual({
      type: 'itemAction',
      toolUseId: 'r1',
      action: 'openFile',
    });
  });
});

describe('tool.files', () => {
  it('stores valid file links on the tool and replaces them on a later event', () => {
    const s = applyEvents(initialViewState(), [
      ev('tool.use', { tool_use_id: 'b1', name: 'Bash', input: { command: 'wc -l a.md' } }),
      ev('tool.files', { tool_use_id: 'b1', files: [{ raw: 'a.md', path: '/w/a.md' }, { raw: '', path: '/x' }, 7] }),
    ]);
    expect(s.tools.b1.files).toEqual([{ raw: 'a.md', path: '/w/a.md' }]);
    const t = applyEvents(s, [ev('tool.files', { tool_use_id: 'b1', files: [] })]);
    expect(t.tools.b1.files).toEqual([]);
  });

  it('accepts openToolFile messages with a non-negative integer index only', () => {
    expect(parseWebviewMessage({ type: 'openToolFile', toolUseId: 'b1', index: 0 })).toEqual({
      type: 'openToolFile',
      toolUseId: 'b1',
      index: 0,
    });
    expect(parseWebviewMessage({ type: 'openToolFile', toolUseId: 'b1', index: -1 })).toBeUndefined();
    expect(parseWebviewMessage({ type: 'openToolFile', toolUseId: 'b1', index: '0' })).toBeUndefined();
  });
});

import { fileClick, firstChangedLine, rowClick } from '../../src/shared/itemMenu';
import type { ToolView as RowTool } from '../../src/shared/viewState';

describe('rowClick', () => {
  const tool = (over: Partial<RowTool>): RowTool => ({ toolUseId: 't1', agentKey: 'main', name: 'Bash', input: {}, ...over });

  it('opens the diff of an edit that has one, else the file', () => {
    const edit = tool({ name: 'Edit', input: { file_path: 'a.py' }, diffPath: 'a.py', editRange: { start: 42, end: 44 } });
    expect(rowClick(edit)).toEqual({ kind: 'openDiff', title: 'Open diff' });
    expect(fileClick(edit)).toEqual({ action: 'openFileAtEdit', line: 42, title: 'Open file at line 42' });
    expect(rowClick(tool({ name: 'Write', input: { file_path: 'a.py' } }))).toEqual({
      kind: 'openFile', action: 'openFileAtEdit', line: 1, title: 'Open file at line 1 (no diff was recorded)',
    });
  });

  it('opens a read file at the first line read', () => {
    expect(rowClick(tool({ name: 'Read', input: { file_path: 'a.py', offset: 30 } }))).toEqual({
      kind: 'openFile', action: 'openFile', line: 30, title: 'Open file at line 30',
    });
  });

  it('opens the tab of a subagent launch and expands everything else', () => {
    expect(rowClick(tool({ name: 'Agent', toolUseId: 'A9' }))).toEqual({ kind: 'openTab', tab: 'A9', title: "Open the subagent's tab" });
    expect(rowClick(tool({ name: 'Bash', input: { command: 'ls' } }))).toEqual({ kind: 'expand', title: 'Show the command and its output' });
    expect(rowClick(tool({ name: 'Grep', input: { pattern: 'x' } }))).toEqual({ kind: 'expand', title: 'Show details' });
    expect(rowClick(tool({ name: 'Edit', input: {} })).kind).toBe('expand');
    expect(fileClick(tool({ name: 'Bash' }))).toBeUndefined();
  });

  it('finds the first changed line', () => {
    expect(firstChangedLine('a\nb\nc', 'a\nB\nc')).toBe(2);
    expect(firstChangedLine('a\nb', 'a\nb\nc')).toBe(3);
    expect(firstChangedLine('a', 'a')).toBe(1);
    expect(firstChangedLine('', 'x')).toBe(1);
  });
});
