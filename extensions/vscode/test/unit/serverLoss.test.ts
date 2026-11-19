// After the calu server stops: "The calu server stopped" with Start server and Retry, the
// transcript greyed under "Session ended" (the owner's decision of 2026-11-19); and the
// "Waiting for you" timer counted from the session's time of the escalation.
import { describe, expect, it } from 'vitest';
import type { IdeStatusResult } from '../../src/bridgeProtocol';
import { activityLine, waitingSince } from '../../src/shared/activity';
import { decisionsWaiting, setupCards, statusBarEntry, transcriptEnded } from '../../src/shared/chatModel';
import { buildHints } from '../../src/shared/hints';
import { lostCause, serverCheck, serverStoppedAfterLoss, serverStoppedMessage } from '../../src/shared/serverLoss';
import { applyEvents, initialViewState, type ViewEvent, type ViewState } from '../../src/shared/viewState';

const running: ViewEvent = { method: 'bridge.status', params: { status: 'running' } };
const attached: ViewEvent = {
  method: 'ui.attached',
  params: { session_id: 's1', initialize: { workspace: '/w' }, snapshot: { state: 'running', claude_session_id: 'c-1' }, events: [] },
};
const said: ViewEvent = { method: 'assistant.text', params: { text: 'hello', agent_id: null } };
const escalate: ViewEvent = {
  method: 'escalation.request',
  params: { escalation_id: 'e1', tool_use_id: 't1', tool_name: 'Bash', tool_input: { command: 'deploy' }, reason: 'r', rendered: '', opened_at: '2026-11-19T10:00:00+00:00' },
};
const unreachable: ViewEvent = {
  method: 'ui.link',
  params: { state: 'lost', message: "nothing answers at the session's address: the session or its calu server stopped", cause: 'unreachable' },
};

/** Build a state from events. */
function stateOf(...events: ViewEvent[]): ViewState {
  return applyEvents(initialViewState(), events);
}

/** An `ide.status` with the given server counts and a reachable database. */
function status(sessions: number, servers: number | null | undefined, reachable = true): IdeStatusResult {
  return {
    findings: [],
    database: { reachable, place: 'local', problem: '' },
    server: { sessions_running: sessions, ...(servers === undefined ? {} : { servers_running: servers }) },
  };
}

describe('the cause of a lost link', () => {
  it('reads the cause, and for an older bridge the sentence of a refused connection', () => {
    expect(lostCause('revoked', 'x')).toBe('revoked');
    expect(lostCause(undefined, "nothing answers at the session's address: the session or its calu server stopped")).toBe('unreachable');
    expect(lostCause(undefined, 'the session did not answer in time')).toBeUndefined();
    expect(lostCause('something new', "nothing answers at the session's address")).toBeUndefined();
  });

  it('tells a stopped server from a running one by ide.status', () => {
    expect(serverCheck(status(0, 1))).toBe('running');
    expect(serverCheck(status(2, null))).toBe('running');
    expect(serverCheck(status(0, 0))).toBe('stopped');
    expect(serverCheck(status(0, null))).toBe('unknown');
    expect(serverCheck(status(0, undefined))).toBe('unknown');
    expect(serverCheck(null)).toBe('unknown');
    expect(serverStoppedAfterLoss('unreachable', 'stopped')).toBe(true);
    expect(serverStoppedAfterLoss('unreachable', 'unknown')).toBe(true);
    expect(serverStoppedAfterLoss('unreachable', 'running')).toBe(false);
    expect(serverStoppedAfterLoss('revoked', 'stopped')).toBe(false);
  });
});

describe('the chat after the server stopped', () => {
  const hints = buildHints({ caluMissing: false, status: status(0, 0), attached: false, noServer: false, serverStopped: true });

  it('shows "The calu server stopped" with Start server and Retry, and no "connection lost" card', () => {
    expect(hints.map((h) => h.id)).toEqual(['server-stopped']);
    const s = stateOf(running, attached, said, unreachable, { method: 'ui.hints', params: { hints } });
    const cards = setupCards(s);
    expect(cards.map((c) => c.id)).toEqual(['server-stopped']);
    expect(cards[0].message).toMatch(/^The calu server stopped;/);
    expect(cards[0].serverHelp).toBe(false);
    expect(cards[0].primary).toEqual({ label: 'Start server', message: { type: 'startServer' } });
    expect(cards[0].secondary).toEqual([{ label: 'Retry', message: { type: 'retryServer' } }]);
    expect(statusBarEntry(s).text).toBe('$(shield) calu: server stopped');
  });

  it('keeps the transcript, marked as ended, and the open card does not count as waiting', () => {
    const s = stateOf(running, attached, said, escalate, unreachable, { method: 'ui.hints', params: { hints } });
    expect(transcriptEnded(s)).toBe(true);
    expect(s.items.main.length).toBeGreaterThan(0);
    expect(s.escalations.e1.status).toBe('open');
    expect(statusBarEntry(s)).toMatchObject({ text: '$(shield) calu: server stopped', warning: false });
    expect(decisionsWaiting(s)).toBe(0);
    expect(decisionsWaiting(stateOf(running, attached, escalate))).toBe(1);
  });

  it('says when a Retry found no server', () => {
    expect(serverStoppedMessage('14:02:11')).toMatch(/Checked at 14:02:11: no server runs yet\.$/);
    const retried = buildHints({ caluMissing: false, status: null, attached: false, noServer: false, serverStopped: true, serverRetriedAt: '14:02:11' });
    expect(retried[0].message).toContain('14:02:11');
  });

  it('with the server back but the session gone: the session list, the transcript still marked ended', () => {
    const back = buildHints({ caluMissing: false, status: status(0, 1), attached: false, noServer: false });
    const s = stateOf(running, attached, said, unreachable, { method: 'ui.hints', params: { hints: back } });
    expect(setupCards(s).map((c) => c.id)).toEqual(['no-session']);
    expect(transcriptEnded(s)).toBe(true);
    expect(statusBarEntry(s).text).toBe('$(shield) calu: session ended');
  });

  it('a new attach ends the mark; another kind of loss keeps "connection lost"', () => {
    const s = stateOf(running, attached, said, unreachable, attached);
    expect(transcriptEnded(s)).toBe(false);
    const revoked = stateOf(running, attached, { method: 'ui.link', params: { state: 'lost', message: 'the token was revoked', cause: 'revoked' } });
    expect(transcriptEnded(revoked)).toBe(false);
    expect(setupCards(revoked)[0]).toMatchObject({ id: 'link-lost' });
    expect(statusBarEntry(revoked).text).toBe('$(shield) calu: connection lost');
  });

  it('a detach (the bridge restarted) is not an ended session', () => {
    const s = stateOf(running, attached, unreachable, { method: 'ui.detached', params: {} });
    expect(transcriptEnded(s)).toBe(false);
  });
});

describe('the waiting timer', () => {
  const opened = Date.parse('2026-11-19T10:00:00Z');

  it('counts from the oldest open escalation the session reported', () => {
    expect(waitingSince(['2026-11-19T10:02:00Z', '2026-11-19T10:00:00Z', null], 1, opened + 300_000)).toBe(opened);
    expect(activityLine(true, 'escalated', waitingSince(['2026-11-19T10:00:00Z'], 0, opened + 180_000), undefined, opened + 180_000, 0).text).toBe('‖ Waiting for you… 3m 00s');
  });

  it('falls back to the view\'s own start without a time, and never counts a time ahead of now', () => {
    expect(waitingSince([null, 'not a time'], 42, opened)).toBe(42);
    expect(waitingSince([], 42, opened)).toBe(42);
    expect(waitingSince(['2026-11-19T10:05:00Z'], 42, opened)).toBe(opened);
  });

  it('the time survives in the state: from the event and from the snapshot of a later attach', () => {
    expect(stateOf(attached, escalate).escalations.e1.openedAt).toBe('2026-11-19T10:00:00+00:00');
    const later = stateOf({
      method: 'ui.attached',
      params: {
        session_id: 's1',
        initialize: { workspace: '/w' },
        snapshot: { state: 'escalated', claude_session_id: 'c-1', escalations: [escalate.params] },
        events: [],
      },
    });
    expect(Object.values(later.escalations).map((e) => e.openedAt)).toEqual(['2026-11-19T10:00:00+00:00']);
  });
});
