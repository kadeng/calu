import { describe, expect, it } from 'vitest';
import {
  ACTIVITY_VERBS,
  FRAME_MS,
  LABEL_COLUMN_CH,
  SPINNER_FRAMES,
  VERB_MS,
  WAITING_LABEL,
  activityLine,
  formatElapsed,
  sessionPhase,
  CHECKING_LABEL,
} from '../../src/shared/activity';

describe('activity line', () => {
  it('animates a spinner and a verb with the elapsed time while busy', () => {
    const a = activityLine(true, 'running', 0, undefined, 12_000, 0);
    expect(a).toMatchObject({ active: true, kind: 'busy', label: `${ACTIVITY_VERBS[3]}…`, elapsed: '12s' });
    expect(a.icon).toBe(SPINNER_FRAMES[100 % SPINNER_FRAMES.length]);
    expect(a.text).toBe(`${a.icon} ${a.label} 12s`);
    const next = activityLine(true, 'running', 0, undefined, 12_000 + FRAME_MS, 0);
    expect(next.icon).not.toBe(a.icon);
    expect(next.label).toBe(a.label);
    expect(activityLine(true, 'running', 0, undefined, VERB_MS - 1, 5).label).toBe(`${ACTIVITY_VERBS[5]}…`);
  });

  it('keeps every animated label within the fixed label column', () => {
    const labels = [...ACTIVITY_VERBS.map((v) => `${v}…`), WAITING_LABEL];
    for (const label of labels) expect(label.length).toBeLessThan(LABEL_COLUMN_CH);
  });

  it('shows waiting, done, idle and stopped states', () => {
    expect(activityLine(true, 'escalated', 0, undefined, 40_000, 0)).toMatchObject({
      icon: '⏸',
      label: WAITING_LABEL,
      elapsed: '40s',
      active: true,
      kind: 'waiting',
    });
    expect(activityLine(true, 'idle', 0, 52_000, 99_000, 0)).toMatchObject({ icon: '✓', label: 'Done', elapsed: '52s' });
    expect(activityLine(true, 'idle', 0, undefined, 0, 0).text).toBe('Idle');
    expect(activityLine(true, 'stopped', 0, 5, 0, 0).text).toBe('Stopped');
    expect(activityLine(false, 'running', 0, undefined, 0, 0)).toMatchObject({ text: 'Not connected', active: false });
  });

  it('maps session states to phases and labels a running check', () => {
    expect(sessionPhase('tool_call')).toBe('busy');
    expect(sessionPhase('escalated')).toBe('waiting');
    expect(sessionPhase('failed')).toBe('failed');
    expect(sessionPhase('nonsense')).toBe('idle');
    expect(activityLine(true, 'checking', 0, undefined, 3000, 0)).toMatchObject({ label: CHECKING_LABEL, kind: 'busy' });
    expect(activityLine(true, 'failed', 0, undefined, 0, 0).text).toBe('Failed');
  });

  it('formats durations', () => {
    expect(formatElapsed(-5)).toBe('0s');
    expect(formatElapsed(59_999)).toBe('59s');
    expect(formatElapsed(185_000)).toBe('3m 05s');
  });
});
