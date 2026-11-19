// package.json contributions and the validation of chat webview messages (no VSCode needed).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { MAX_IMAGE_BASE64, MAX_TEXT, parseWebviewMessage } from '../../src/shared/messages';

const manifest = JSON.parse(readFileSync(join(__dirname, '..', '..', 'package.json'), 'utf8'));
const extensionSource = readFileSync(join(__dirname, '..', '..', 'src', 'extension.ts'), 'utf8');

describe('package.json', () => {
  it('is a workspace extension named calu under the GPL', () => {
    expect(manifest.extensionKind).toEqual(['workspace']);
    expect(manifest).toMatchObject({ name: 'calu', displayName: 'calu', publisher: 'kadeng', license: 'GPL-2.0-only' });
    // The second event opens a checker's editor that VSCode restores before the start-up ended.
    expect(manifest.activationEvents).toEqual(['onStartupFinished', 'onFileSystem:calu-checker']);
    expect(manifest.dependencies).toBeUndefined();
  });

  it('puts the chat into the secondary side bar, which needs VSCode 1.106', () => {
    expect(manifest.contributes.viewsContainers).toEqual({ secondarySidebar: [{ id: 'calu', title: 'calu', icon: 'media/icon.svg' }] });
    expect(manifest.contributes.views.calu).toEqual([{ type: 'webview', id: 'calu.chat', name: 'Chat' }]);
    expect(manifest.engines.vscode).toBe('^1.106.0');
  });

  it('binds the comment command to ctrl+alt+k / cmd+alt+k in text editors', () => {
    const binding = manifest.contributes.keybindings.find((k: { command: string }) => k.command === 'calu.addComment');
    expect(binding).toEqual({ command: 'calu.addComment', key: 'ctrl+alt+k', mac: 'cmd+alt+k', when: 'editorTextFocus' });
  });

  it('declares the calu command as machine-scoped and restricted', () => {
    // Machine scope: a repository's .vscode/settings.json (which Claude can write) must not be
    // able to change which program the extension runs or what the server terminal runs.
    const properties = manifest.contributes.configuration.properties;
    expect(properties['calu.command']).toMatchObject({ scope: 'machine', restricted: true });
    for (const name of ['calu.install.source', 'calu.server.sudo', 'calu.server.insecure', 'calu.server.extraArgs']) {
      expect(properties[name].scope, name).toBe('machine');
    }
    expect(Object.keys(properties).sort()).toEqual([
      'calu.chat.escalationSound',
      'calu.chat.showThinking',
      'calu.command',
      'calu.install.source',
      'calu.notifications',
      'calu.server.extraArgs',
      'calu.server.insecure',
      'calu.server.sudo',
    ]);
  });

  it('contributes every command it binds or shows in menus, and registers every contributed one', () => {
    const commands: string[] = manifest.contributes.commands.map((c: { command: string }) => c.command);
    const used = [
      ...manifest.contributes.keybindings.map((k: { command: string }) => k.command),
      ...Object.values(manifest.contributes.menus).flat().map((m) => (m as { command: string }).command),
    ];
    for (const c of used) expect(commands, c).toContain(c);
    for (const c of commands) expect(extensionSource, c).toContain(`command('${c}'`);
    for (const c of manifest.contributes.commands) {
      expect(c.category, c.command).toBe('calu');
      expect(c.icon, c.command).toMatch(/^\$\([a-z-]+\)$/);
    }
  });

  it('shows the five view title buttons and the overflow entries', () => {
    const title = manifest.contributes.menus['view/title'] as { command: string; group: string }[];
    const navigation = title.filter((m) => m.group.startsWith('navigation')).map((m) => m.command);
    expect(navigation).toEqual(['calu.newSession', 'calu.attach', 'calu.editCheckerInstructions', 'calu.showCheckerContext', 'calu.openSettings']);
    const overflow = title.filter((m) => m.group.startsWith('overflow')).map((m) => m.command);
    expect(overflow).toEqual(['calu.startServer', 'calu.restartBridge', 'calu.showLog', 'calu.update']);
  });
});

describe('parseWebviewMessage', () => {
  it('accepts the messages without fields', () => {
    for (const type of [
      'ready', 'attach', 'newSession', 'refreshSessions', 'detach', 'startServer', 'retryServer', 'copyServerCommand', 'install',
      'restartBridge', 'openSettings', 'showContext', 'refreshUsage', 'interrupt', 'showLog', 'unlock',
      'forkConversation', 'honestyReview', 'honestyTrust',
    ]) {
      expect(parseWebviewMessage({ type, extra: 'dropped' })).toEqual({ type });
    }
  });

  it('accepts chat messages with and without images', () => {
    expect(parseWebviewMessage({ type: 'send', text: 'hi', delivery: 'inject', to: 'main' })).toEqual({
      type: 'send', text: 'hi', delivery: 'inject', to: 'main', images: [],
    });
    const image = { data: 'aGVsbG8=', mediaType: 'image/png', name: 'dropped.png' };
    expect(parseWebviewMessage({ type: 'send', text: '', delivery: 'queued', to: 'main', images: [image] })).toEqual({
      type: 'send', text: '', delivery: 'queued', to: 'main', images: [{ data: 'aGVsbG8=', mediaType: 'image/png' }],
    });
  });

  it('accepts decisions, answers and the other messages with fields', () => {
    expect(parseWebviewMessage({ type: 'decide', escalationId: 'e', decision: 'deny', note: ' why ' })).toEqual({
      type: 'decide', escalationId: 'e', decision: 'deny', note: 'why',
    });
    expect(parseWebviewMessage({ type: 'decide', escalationId: 'e', decision: 'allow', note: '', answer: ' SQLite ' })).toEqual({
      type: 'decide', escalationId: 'e', decision: 'allow', answer: 'SQLite',
    });
    expect(parseWebviewMessage({ type: 'why', escalationId: 'e' })).toEqual({ type: 'why', escalationId: 'e' });
    expect(parseWebviewMessage({ type: 'sideAsk', question: ' what now? ' })).toEqual({ type: 'sideAsk', question: 'what now?' });
    expect(parseWebviewMessage({ type: 'switchSession', sessionId: 's1' })).toEqual({ type: 'switchSession', sessionId: 's1' });
    expect(parseWebviewMessage({ type: 'renameSession', sessionId: 's1', name: 'ignored' })).toEqual({ type: 'renameSession', sessionId: 's1' });
    expect(parseWebviewMessage({ type: 'editChecker' })).toEqual({ type: 'editChecker' });
    expect(parseWebviewMessage({ type: 'editChecker', name: 'command' })).toEqual({ type: 'editChecker', name: 'command' });
    expect(parseWebviewMessage({ type: 'editChecker', route: 'edit' })).toEqual({ type: 'editChecker', route: 'edit' });
    expect(parseWebviewMessage({ type: 'setModel', model: 'sonnet' })).toEqual({ type: 'setModel', model: 'sonnet' });
    expect(parseWebviewMessage({ type: 'setEffort', effort: 'xhigh' })).toEqual({ type: 'setEffort', effort: 'xhigh' });
    expect(parseWebviewMessage({ type: 'setLevel', level: 'writes' })).toEqual({ type: 'setLevel', level: 'writes' });
    expect(parseWebviewMessage({ type: 'openFile', path: '/w/a.py' })).toEqual({ type: 'openFile', path: '/w/a.py' });
    expect(parseWebviewMessage({ type: 'openFile', path: '/w/a.py', line: 12 })).toEqual({ type: 'openFile', path: '/w/a.py', line: 12 });
    expect(parseWebviewMessage({ type: 'openFile', path: '/w/a.py', line: 0 })).toEqual({ type: 'openFile', path: '/w/a.py' });
    expect(parseWebviewMessage({ type: 'openToolFile', toolUseId: 't', index: 1, line: 3 })).toEqual({ type: 'openToolFile', toolUseId: 't', index: 1, line: 3 });
    expect(parseWebviewMessage({ type: 'rewindBefore', turn: 3, uuid: 'u-1' })).toEqual({ type: 'rewindBefore', turn: 3, uuid: 'u-1' });
    expect(parseWebviewMessage({ type: 'forkBefore', turn: 2 })).toEqual({ type: 'forkBefore', turn: 2 });
    expect(parseWebviewMessage({ type: 'setHonestyMode', mode: 'enforce' })).toEqual({ type: 'setHonestyMode', mode: 'enforce' });
    expect(parseWebviewMessage({ type: 'honestyClear', markId: 4 })).toEqual({ type: 'honestyClear', markId: 4 });
    expect(parseWebviewMessage({ type: 'openDiff', toolUseId: 't' })).toEqual({ type: 'openDiff', toolUseId: 't' });
    expect(parseWebviewMessage({ type: 'openToolFile', toolUseId: 't', index: 0 })).toEqual({ type: 'openToolFile', toolUseId: 't', index: 0 });
    expect(parseWebviewMessage({ type: 'openLink', href: 'https://example.org' })).toEqual({ type: 'openLink', href: 'https://example.org' });
    expect(parseWebviewMessage({ type: 'itemAction', toolUseId: 't', action: 'copyPath' })).toEqual({ type: 'itemAction', toolUseId: 't', action: 'copyPath' });
    expect(parseWebviewMessage({ type: 'stateReport', requestId: 1, state: { a: 1 } })).toEqual({ type: 'stateReport', requestId: 1, state: { a: 1 } });
  });

  it('drops invalid messages', () => {
    const image = { data: 'aGk=', mediaType: 'image/png' };
    for (const bad of [
      null,
      'ready',
      { type: 'eval', code: 'x' },
      { type: 'send', text: '  ', delivery: 'queued', to: 'main' },
      { type: 'send', text: 'x', delivery: 'now', to: 'main' },
      { type: 'send', text: 'x', delivery: 'queued', to: '' },
      { type: 'send', text: 'x'.repeat(MAX_TEXT + 1), delivery: 'queued', to: 'main' },
      { type: 'send', text: 'x', delivery: 'queued', to: 'main', images: 'png' },
      { type: 'send', text: 'x', delivery: 'queued', to: 'main', images: [image, image, image, image, image] },
      { type: 'send', text: 'x', delivery: 'queued', to: 'main', images: [{ data: 'aGk=', mediaType: 'image/svg+xml' }] },
      { type: 'send', text: 'x', delivery: 'queued', to: 'main', images: [{ data: 'not base64!', mediaType: 'image/png' }] },
      { type: 'send', text: 'x', delivery: 'queued', to: 'main', images: [{ data: 'A'.repeat(MAX_IMAGE_BASE64 + 4), mediaType: 'image/png' }] },
      { type: 'send', text: 'x', delivery: 'queued', to: 'A1', images: [image] },
      { type: 'decide', escalationId: 'e', decision: 'maybe' },
      { type: 'decide', escalationKey: 'e', decision: 'allow' },
      { type: 'why' },
      { type: 'sideAsk', question: '   ' },
      { type: 'switchSession', sessionId: 5 },
      { type: 'editChecker', name: 7 },
      { type: 'setModel', model: 'm'.repeat(101) },
      { type: 'setEffort', effort: 'extreme' },
      { type: 'setLevel', level: 'custom' },
      { type: 'openFile', path: 3 },
      { type: 'openToolFile', toolUseId: 't', index: -1 },
      { type: 'openToolFile', toolUseId: 't', index: 1.5 },
      { type: 'itemAction', toolUseId: 't', action: 'deleteFile' },
      { type: 'stateReport', requestId: 'x' },
      { type: 'rewindBefore', turn: 0 },
      { type: 'forkBefore', turn: '2' },
      { type: 'setHonestyMode', mode: 'strict' },
      { type: 'honestyClear', markId: '4' },
    ]) {
      expect(parseWebviewMessage(bad), JSON.stringify(bad)?.slice(0, 80)).toBeUndefined();
    }
  });
});
