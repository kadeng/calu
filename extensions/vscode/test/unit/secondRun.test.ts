// This file tests what the extension gained after its first run in VSCode. That is the waiting
// session request, the hints as one line while a session is attached, the context meter's
// tooltip, the names of forked sessions, and the summary of the settings dialog's saves.
import { describe, expect, it } from 'vitest';
import { STARTING_CARD, checkImage, setupCards, startingCard, usageMeter } from '../../src/shared/chatModel';
import { buildHints } from '../../src/shared/hints';
import { MAX_IMAGE_BASE64, parseWebviewMessage } from '../../src/shared/messages';
import { MAX_SESSION_NAME, derivedSessionName } from '../../src/shared/sessionPick';
import { saveSummary } from '../../src/shared/settingsView';
import { MAIN, MAX_IMAGE_DATA, applyEvent, applyEvents, initialViewState, type SetupHint, type ViewEvent, type ViewState } from '../../src/shared/viewState';

/** Build a state from events. */
function stateOf(...events: ViewEvent[]): ViewState {
  return applyEvents(initialViewState(), events);
}

const running: ViewEvent = { method: 'bridge.status', params: { status: 'running' } };
const attached: ViewEvent = { method: 'ui.attached', params: { session_id: 's1', snapshot: { state: 'idle', workspace: '/w' }, events: [] } };
const claudeMissing = buildHints({
  caluMissing: false,
  status: { findings: [{ name: 'claude', ok: false, level: 'missing', message: 'not on PATH', advice: '' }], database: { reachable: true, place: '', problem: '' }, server: { sessions_running: 0 } } as never,
  attached: true,
  noServer: false,
});
const noSession: SetupHint = { id: 'no-session', level: 'info', message: 'No session is attached.', action: 'newSession', actionLabel: 'New session' };

describe('a waiting session request', () => {
  it('shows progress with Cancel at once', () => {
    const card = startingCard({ title: 'Starting a session', slow: false });
    expect(card).toMatchObject({ id: STARTING_CARD, busy: true, level: 'info', secondary: [{ label: 'Cancel', message: { type: 'cancelStart' } }] });
    expect(card.message).toBe('Starting a session… Asking the calu server for a session.');
    expect(card.primary).toBeUndefined();
  });

  it('says after a while that no server may be running, and offers to start one', () => {
    const card = startingCard({ title: 'Forking the conversation', slow: true });
    expect(card.level).toBe('warning');
    expect(card.message).toContain('maybe no calu server is running');
    expect(card.primary).toEqual({ label: 'Start calu server', message: { type: 'startServer' } });
    expect(card.secondary.map((a) => a.label)).toEqual(['Cancel']);
  });

  it('comes first and replaces the "no session" card', () => {
    const state = stateOf(running, { method: 'ui.hints', params: { hints: [noSession] } }, { method: 'ui.starting', params: { title: 'Starting a session', slow: false } });
    expect(setupCards(state).map((c) => c.id)).toEqual([STARTING_CARD]);
    // A fork from an attached session: the card stands above the transcript.
    const forking = stateOf(running, attached, { method: 'ui.starting', params: { title: 'Forking the conversation', slow: false } });
    expect(setupCards(forking).map((c) => c.id)).toEqual([STARTING_CARD]);
  });
});

describe('hints while a session is attached', () => {
  it('are one dismissible line with a short text and a short action', () => {
    expect(claudeMissing).toHaveLength(1);
    const hints: ViewEvent = { method: 'ui.hints', params: { hints: claudeMissing } };
    // Before the first session: the whole card.
    const before = setupCards(stateOf(running, hints))[0];
    expect(before).toMatchObject({ id: 'claude-missing', compact: false, primary: { label: 'How to install Claude Code' } });
    expect(before.short).toBeUndefined();
    // Attached: one line; the whole message stays (the view shows it as the tooltip).
    const line = setupCards(stateOf(running, attached, hints))[0];
    expect(line).toMatchObject({ id: 'claude-missing', compact: true, short: 'Claude Code not found', primary: { label: 'Guide', message: { type: 'openLink' } } });
    expect(line.message).toBe(before.message);
    expect(setupCards(stateOf(running, attached, hints), ['claude-missing'])).toEqual([]);
    // A dismissal does not hide the card of the empty state.
    expect(setupCards(stateOf(running, hints), ['claude-missing'])).toHaveLength(1);
  });
});

describe('the context meter', () => {
  it('says in its tooltip that the number is from the last turn while Claude works', () => {
    const usage = { rateLimits: [], costUsd: null, numTurns: 1, context: { tokens: 84_000, maxTokens: 200_000, percent: 42, model: 'opus' } };
    expect(usageMeter(usage).title).toBe('Context: 84,000 of 200,000 tokens (42%). Click for the usage panel (limits, /usage, cost).');
    expect(usageMeter(usage, true).title).toContain('As of the last turn or usage report');
    expect(usageMeter(usage, true).label).toBe(usageMeter(usage).label);
    expect(usageMeter({ ...usage, context: null }, true).title).toBe('Context usage is not known yet. Click for the usage panel.');
  });
});

describe('derivedSessionName', () => {
  it('names a fork and a rewind after the session they are cut from', () => {
    expect(derivedSessionName('fork', 'parser work')).toBe('fork of parser work');
    expect(derivedSessionName('rewind', ' demo ')).toBe('rewind of demo');
    expect(derivedSessionName('fork', '  ')).toBe('');
  });

  it('stays within the limit of a session name', () => {
    const name = derivedSessionName('rewind', 'x'.repeat(200));
    expect(name).toHaveLength(MAX_SESSION_NAME);
    expect(name.endsWith('…')).toBe(true);
  });
});

describe('saveSummary', () => {
  it('summarises the settings one by one', () => {
    expect(saveSummary(0, 0, 0)).toEqual({ text: '', kind: 'idle' });
    expect(saveSummary(2, 1, 1)).toEqual({ text: 'Saving…', kind: 'saving' });
    expect(saveSummary(0, 1, 0)).toEqual({ text: 'Saved', kind: 'saved' });
    expect(saveSummary(0, 3, 0).text).toBe('Saved');
    expect(saveSummary(0, 0, 1)).toEqual({ text: '1 setting not saved', kind: 'error' });
    expect(saveSummary(0, 0, 2).text).toBe('2 settings not saved');
    // One field was refused while another was saved: both are said.
    expect(saveSummary(0, 1, 1)).toEqual({ text: '1 saved · 1 not saved', kind: 'error' });
  });
});

describe('image sizes', () => {
  const MB = 1024 * 1024;

  it('accepts what a session accepts (5 MB) and refuses more before anything is sent', () => {
    expect(checkImage('image/png', 5 * MB, 0, true)).toBeUndefined();
    // checkImage counts base64 characters: 4 for each started group of 3 bytes.
    expect(checkImage('image/png', 5 * MB + 3, 0, true)).toMatch(/too large/);
    expect(checkImage('image/jpeg', 6 * MB, 0, true)).toBe('The image is too large: 6.0 MB; a message takes images of at most 5 MB. Nothing was attached.');
    // The host's validation has the same limit.
    const send = (length: number): unknown => parseWebviewMessage({ type: 'send', text: 'x', delivery: 'queued', to: 'main', images: [{ data: 'A'.repeat(length), mediaType: 'image/png' }] });
    expect(send(MAX_IMAGE_BASE64)).toBeDefined();
    expect(send(MAX_IMAGE_BASE64 + 4)).toBeUndefined();
  });

  it('shows every image that could be sent', () => {
    expect(MAX_IMAGE_DATA).toBe(MAX_IMAGE_BASE64);
    const data = 'A'.repeat(MAX_IMAGE_BASE64);
    const s = applyEvent(initialViewState(), { method: 'user.input', params: { text: 'see', images: [{ data, media_type: 'image/png' }] } });
    expect(s.items[MAIN][0]).toMatchObject({ kind: 'user', images: [{ omitted: false }] });
  });
});
