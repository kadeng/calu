// Integration tests in a real VSCode (run by `vscode-test`, see .vscode-test.mjs): the main
// wiring of the extension against a real `calu ide` and a real calu session.
//
// scripts/prepare-test-workspace.mjs creates the workspace, a `calu` wrapper that sees nothing
// of the tester's own calu, and test-config.json; the suite starts
// test/integration/scripted_session.py (a scripted session without a database) and attaches
// with its address and token. The tests run in order and share that session:
//
//   activation      the extension is active, its commands exist, `calu ide` initializes
//   chat view       the webview resolves and reports the state it rendered
//   attach          the transcript and the header follow the attached session
//   turn 1          "hello": text, `ls` (PASS), an escalated `git push` -> Allow -> text
//   held steps      a `step.held` event marks the step in the reducer the webview shares
//   status bar      "calu: N waiting" while the card is open, "calu: idle" after the turn
//   bridge restart  `calu ide` killed: the chat says so, Restart attaches again, no prompt
//   server stopped  the session's process ended: "The calu server stopped" (Start server,
//                   Retry), the transcript kept under "Session ended"; a new attach ends it

import * as assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { CaluApi } from '../../src/extension';
import { decisionsWaiting, heldTools, setupCards, statusBarEntry, transcriptEnded } from '../../src/shared/chatModel';
import { applyEvent, type ViewState } from '../../src/shared/viewState';

const EXTENSION_ID = 'kadeng.calu';

/** What prepare-test-workspace.mjs writes to <root>/test-config.json. */
interface TestConfig {
  python: string[];
  script: string;
  cwd: string;
  env: Record<string, string>;
  root: string;
}

/**
 * Poll until `check` returns a truthy value.
 *
 * @param what Description for the timeout error.
 * @param check Returns the awaited value or a falsy value.
 * @param timeoutMs Maximum wait.
 * @returns The first truthy value of `check`.
 * @throws Error after `timeoutMs`.
 */
async function waitFor<T>(what: string, check: () => T | undefined | null | false, timeoutMs = 30000): Promise<T> {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const value = check();
    if (value) return value;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

/**
 * Find the test configuration next to the workspace VSCode opened.
 *
 * @returns The parsed test-config.json.
 * @throws Error if the file is missing (prepare-test-workspace.mjs did not run).
 */
function readTestConfig(): TestConfig {
  const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  assert.ok(workspace, 'no workspace folder is open');
  const file = path.join(path.dirname(workspace), 'test-config.json');
  return JSON.parse(fs.readFileSync(file, 'utf8')) as TestConfig;
}

/**
 * Return Claude's finished texts in the main list.
 *
 * @param state The view state.
 * @returns The texts in order.
 */
function assistantTexts(state: ViewState): string[] {
  return (state.items.main ?? []).flatMap((i) => (i.kind === 'assistant' && !i.streaming ? [i.text] : []));
}

suite('calu extension', function () {
  let api: CaluApi;
  let config: TestConfig;
  let scripted: ChildProcess | undefined;
  let endpoint: { address: string; token: string };

  /**
   * Start a scripted session (scripted_session.py) and wait until it listens.
   *
   * @param name File name of its endpoint file below the test root.
   * @returns The process and its address and token.
   * @throws Error if it does not listen within two minutes.
   */
  async function startScripted(name: string): Promise<{ child: ChildProcess; endpoint: { address: string; token: string } }> {
    const file = path.join(config.root, name);
    fs.rmSync(file, { force: true });
    const [program, ...args] = config.python;
    const child = spawn(program, [...args, config.script, vscode.workspace.workspaceFolders![0].uri.fsPath, file], {
      cwd: config.cwd,
      env: { ...process.env, ...config.env },
      stdio: ['pipe', 'inherit', 'inherit'],
    });
    const found = await waitFor('the scripted session', () => (fs.existsSync(file) && fs.statSync(file).size > 0 ? JSON.parse(fs.readFileSync(file, 'utf8')) : undefined), 120000);
    return { child, endpoint: found };
  }

  suiteSetup(async () => {
    const ext = vscode.extensions.getExtension<CaluApi>(EXTENSION_ID);
    assert.ok(ext, 'extension not found');
    api = await ext.activate();
    config = readTestConfig();
    ({ child: scripted, endpoint } = await startScripted('endpoint.json'));
  });

  suiteTeardown(async () => {
    // Closing stdin ends the scripted session (see scripted_session.py).
    scripted?.stdin?.end();
    scripted?.kill('SIGTERM');
    await api?.bridge.stop();
  });

  test('activates and registers its commands', async () => {
    const commands = await vscode.commands.getCommands(true);
    for (const id of ['calu.openChat', 'calu.newSession', 'calu.attach', 'calu.openSettings', 'calu.restartBridge', 'calu.showCheckerContext']) {
      assert.ok(commands.includes(id), `command ${id} is missing`);
    }
  });

  test('starts calu ide and initializes', async () => {
    const result = await api.bridge.ensureStarted();
    assert.equal(api.bridge.status, 'running');
    assert.equal(typeof result.bridge_version, 'string');
    assert.equal(path.resolve(String(result.workspace)), path.resolve(vscode.workspace.workspaceFolders![0].uri.fsPath));
  });

  test('the chat view resolves and renders the state', async () => {
    await vscode.commands.executeCommand('calu.openChat');
    await waitFor('the chat view', () => api.chat.isVisible, 20000);
    // The webview gets the host's state with its "ready" message: ask until it has it.
    let status: string | undefined;
    for (let attempt = 0; attempt < 50 && status !== 'running'; attempt++) {
      const rendered = (await api.chat.requestWebviewState(10000)) as { view?: ViewState };
      status = rendered.view?.bridge.status;
      if (status !== 'running') await new Promise((r) => setTimeout(r, 200));
    }
    assert.equal(status, 'running');
  });

  test('attaches to the scripted session', async () => {
    await api.sessions.attachEndpoint(endpoint.address, endpoint.token);
    const state = await waitFor('attached', () => (api.chat.state.link.status === 'attached' && api.chat.state.session.sessionId ? api.chat.state : undefined));
    assert.equal(statusBarEntry(state).text, '$(shield) calu: idle');
  });

  test('a turn with an escalated step: the card, Allow, the rest of the turn', async () => {
    await api.handleWebviewMessage({ type: 'send', text: 'hello', delivery: 'queued', to: 'main', images: [] });
    const esc = await waitFor('the escalation card', () => Object.values(api.chat.state.escalations).find((e) => e.status === 'open'));
    assert.equal(esc.toolName, 'Bash');
    assert.match(String(esc.toolInput.command), /git push origin main/);
    // The step that passed before it has its decision; the waiting one is not shown as held.
    const passed = Object.values(api.chat.state.tools).find((t) => t.input.command === 'ls');
    assert.equal(passed?.check?.decision, 'PASS');
    assert.ok(!heldTools(api.chat.state).has(esc.toolUseId ?? ''));
    assert.deepEqual(statusBarEntry(api.chat.state), {
      text: '$(shield) calu: 1 waiting',
      tooltip: statusBarEntry(api.chat.state).tooltip,
      warning: true,
    });
    // The webview drew the card from the same state.
    const rendered = (await api.chat.requestWebviewState(10000)) as { view?: ViewState; dom?: { openEscalations?: number } };
    assert.equal(rendered.view?.escalations[esc.key]?.status, 'open');
    assert.equal(rendered.dom?.openEscalations, 1);

    await api.handleWebviewMessage({ type: 'decide', escalationId: esc.key, decision: 'allow' });
    const done = await waitFor('the end of the turn', () => {
      const s = api.chat.state;
      return s.escalations[esc.key]?.status === 'allow' && assistantTexts(s).includes('After the decision.') && s.session.state === 'idle' ? s : undefined;
    });
    const step = done.tools[esc.toolUseId!];
    assert.equal(step.check?.decision, 'ESCALATE');
    assert.equal(step.result?.content, 'pushed');
    assert.equal(statusBarEntry(done).text, '$(shield) calu: idle');
  });

  test('a held step is marked by step.held and released by its end', () => {
    // The scripted session holds nothing on its own; the event goes through the reducer the
    // host and the webview share, on a copy of the live state.
    const toolUseId = Object.keys(api.chat.state.tools)[0];
    const held = applyEvent(api.chat.state, { method: 'step.held', params: { tool_use_id: toolUseId, tool_name: 'Bash', held: true } });
    assert.ok(held.held[toolUseId]);
    const released = applyEvent(held, { method: 'step.held', params: { tool_use_id: toolUseId, tool_name: 'Bash', held: false } });
    assert.ok(!released.held[toolUseId]);
  });

  test('a killed calu ide is reported and Restart attaches to the same session again', async () => {
    const sessionId = api.chat.state.session.sessionId;
    const pid = api.bridge.pid;
    assert.ok(pid, 'the bridge has no pid');
    // The bridge leads a process group (the wrapper, uv, Python): end all of it, as a crash would.
    process.kill(-pid, 'SIGKILL');
    await waitFor('the bridge to be reported as gone', () => api.chat.state.bridge.status === 'exited' || api.chat.state.bridge.status === 'error');
    await vscode.commands.executeCommand('calu.restartBridge');
    const again = await waitFor('attached again', () => (api.chat.state.link.status === 'attached' && api.bridge.status === 'running' ? api.chat.state : undefined), 30000);
    assert.equal(again.session.sessionId, sessionId);
    // The transcript of the first turn is still there.
    assert.ok(assistantTexts(again).includes('After the decision.'));
    // And a second turn goes through the new bridge.
    await api.handleWebviewMessage({ type: 'send', text: 'again', delivery: 'queued', to: 'main', images: [] });
    await waitFor('the echo turn', () => assistantTexts(api.chat.state).includes('Echo.'));
  });

  test('a stopped server: "The calu server stopped", Retry, the transcript kept until a new attach', async function () {
    // The bridge tries again for 15 s before it gives up the link.
    this.timeout(120000);
    const exited = new Promise<void>((resolve) => scripted?.once('exit', () => resolve()));
    scripted?.stdin?.end();
    scripted?.kill('SIGTERM');
    await exited;
    const stopped = await waitFor('the server-stopped card', () => (api.chat.state.hints.some((h) => h.id === 'server-stopped') ? api.chat.state : undefined), 60000);
    assert.equal(stopped.link.status, 'lost');
    assert.equal(stopped.link.cause, 'unreachable');
    // The test has no database, so its card ("database") stands next to it.
    const cards = setupCards(stopped);
    assert.deepEqual(cards.map((c) => c.id).filter((id) => id !== 'database'), ['server-stopped']);
    const card = cards.find((c) => c.id === 'server-stopped')!;
    assert.equal(card.primary?.label, 'Start server');
    assert.deepEqual(card.secondary.map((a) => a.label), ['Retry']);
    assert.ok(transcriptEnded(stopped));
    assert.ok(assistantTexts(stopped).includes('Echo.'), 'the transcript was kept');
    assert.equal(decisionsWaiting(stopped), 0);
    assert.equal(statusBarEntry(stopped).text, '$(shield) calu: server stopped');
    // The webview draws the same state.
    const rendered = (await api.chat.requestWebviewState(10000)) as { view?: ViewState };
    assert.equal(rendered.view?.link.cause, 'unreachable');

    // Retry without a server: the card stays and says when it looked.
    await api.handleWebviewMessage({ type: 'retryServer' });
    await waitFor('the time of the Retry', () => api.chat.state.hints.find((h) => h.id === 'server-stopped')?.message.includes('Checked at'));

    // A session again: attaching ends the card and the "Session ended" mark.
    const next = await startScripted('endpoint-2.json');
    scripted = next.child;
    await api.sessions.attachEndpoint(next.endpoint.address, next.endpoint.token);
    const back = await waitFor('attached to the new session', () => (api.chat.state.link.status === 'attached' && !api.chat.state.hints.some((h) => h.id === 'server-stopped') ? api.chat.state : undefined));
    assert.ok(!transcriptEnded(back));
    assert.equal(statusBarEntry(back).text, '$(shield) calu: idle');
  });
});
