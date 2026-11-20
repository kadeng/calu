// The usage panel a click on the context chip opens (`usage.panel`, PLAN D248): its sections and
// the reducer case `ui.usagePanel`. The PyCharm plugin's `UsagePanelTest` checks the same texts.
import { describe, expect, it } from 'vitest';
import { limitName, parseUsagePanel, resetText, usagePanelSections, emptyUsagePanel } from '../../src/shared/usagePanel';
import { applyEvents, initialViewState } from '../../src/shared/viewState';
import { parseWebviewMessage } from '../../src/shared/messages';

const now = Date.parse('2026-11-20T12:00:00Z');

describe('usagePanelSections', () => {
  it('names both main limits when nothing was reported, and says that no turn ended', () => {
    const sections = usagePanelSections({ ...parseUsagePanel({ claude_usage_problem: 'Claude Code did not answer' }) }, now);
    expect(sections.map((s) => s.title)).toEqual(['Subscription limits', "Claude Code's /usage", 'This session']);
    expect(sections[0].lines).toEqual([
      { text: '5-hour: not reported yet by Claude Code', tone: 'muted' },
      { text: 'weekly: not reported yet by Claude Code', tone: 'muted' },
    ]);
    expect(sections[1].lines[0].text).toBe('Claude Code did not answer');
    expect(sections[1].preformatted).toBeNull();
    expect(sections[2].lines[0].text).toBe('cost: no turn has ended yet');
  });

  it('gives each reported limit its age and reset; other limits follow; a warning is a problem', () => {
    const panel = parseUsagePanel({
      limits: [
        { type: 'seven_day_opus', status: 'allowed_warning', utilization: 0.91, resets_at: '2026-11-23T12:00:00Z', reported_at: '2026-11-20T09:00:00Z' },
        { type: 'five_hour', status: 'allowed', utilization: 0.42, resets_at: '2026-11-20T14:10:00Z', reported_at: '2026-11-20T11:57:00Z' },
      ],
      claude_usage: 'Total cost: $0.01\nUsage: 1 input',
      cost_usd: 0.0123,
      num_turns: 2,
      context: { tokens: 42000, max_tokens: 200000, percent: 21, model: 'haiku' },
      stored: true,
      account: 'ignored',
    });
    const sections = usagePanelSections(panel, now);
    expect(sections[0].lines).toEqual([
      { text: '5-hour: 42% used, resets in 2 h 10 min · reported 3 min ago', tone: 'plain' },
      { text: 'weekly: not reported yet by Claude Code', tone: 'muted' },
      { text: 'weekly Opus: 91% used, allowed warning, resets in 3 d 0 h · reported 3 h ago', tone: 'problem' },
    ]);
    expect(sections[1].preformatted).toBe('Total cost: $0.01\nUsage: 1 input');
    expect(sections[2].lines.map((l) => l.text)).toEqual([
      'cost $0.0123 (Claude Code, whole session) · last turn: 2 model calls',
      'context 42,000 of 200,000 tokens (21%) · haiku',
    ]);
  });

  it("shows the plan's limits of get_usage without a report time, or why they are missing", () => {
    const plan = usagePanelSections(parseUsagePanel({ account_limits: [{ type: 'seven_day', utilization: 30 }], subscription: 'max' }), now);
    expect(plan[1].title).toBe('Your plan now (max)');
    expect(plan[1].lines[0].text).toBe('weekly: 30% used');
    const missing = usagePanelSections(parseUsagePanel({ account_limits_problem: 'get_usage failed: timeout' }), now);
    expect(missing[1].lines[0]).toEqual({ text: 'get_usage failed: timeout', tone: 'muted' });
  });

  it('words resets and limit names', () => {
    expect(resetText(-1)).toBe('reset time passed');
    expect(resetText(30)).toBe('resets in under a minute');
    expect(resetText(300)).toBe('resets in 5 min');
    expect(limitName('overage')).toBe('extra usage');
    expect(limitName('some_new_limit')).toBe('some new limit');
    expect(limitName('__proto__')).toBe('  proto  ');
  });
});

describe('ui.usagePanel', () => {
  it('goes from asking to the answer, updates the chip, and keeps the chip on a thin answer', () => {
    const result = {
      limits: [{ type: 'five_hour', status: 'allowed', utilization: 0.5, reported_at: '2026-11-20T11:00:00Z' }],
      claude_usage: 'text',
      cost_usd: 0.5,
      num_turns: 1,
      context: { tokens: 10, max_tokens: 100, percent: 10 },
    };
    const asking = applyEvents(initialViewState(), [{ method: 'ui.usagePanel', params: { phase: 'asking' } }]);
    expect(asking.usagePanel).toEqual(emptyUsagePanel('asking'));
    const ready = applyEvents(asking, [{ method: 'ui.usagePanel', params: { result } }]);
    expect(ready.usagePanel?.phase).toBe('ready');
    expect(ready.usagePanel?.limits[0].reportedAt).toBe('2026-11-20T11:00:00Z');
    expect(ready.usage.costUsd).toBe(0.5);
    expect(ready.usage.context?.tokens).toBe(10);
    expect(ready.usage.rateLimits[0].type).toBe('five_hour');
    const thin = applyEvents(ready, [{ method: 'ui.usagePanel', params: { result: { claude_usage: 't' } } }]);
    expect(thin.usage.context?.tokens).toBe(10);
    expect(thin.usage.rateLimits[0].type).toBe('five_hour');
    const failed = applyEvents(ready, [{ method: 'ui.usagePanel', params: { error: 'the session did not answer' } }]);
    expect(failed.usagePanel).toEqual(emptyUsagePanel('failed', 'the session did not answer'));
  });

  it('is a message the host accepts from the webview', () => {
    expect(parseWebviewMessage({ type: 'usagePanel' })).toEqual({ type: 'usagePanel' });
  });
});
