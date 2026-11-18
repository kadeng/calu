// Supervision levels: the auto-escalation switches as one of four named levels.
import { describe, expect, it } from 'vitest';
import { LEVELS, LEVEL_IDS, flagsForLevel, levelLabel, levelOf } from '../../src/shared/levels';

describe('levels', () => {
  it('are a ladder: each level includes the one before', () => {
    expect(LEVELS.map((l) => l.id)).toEqual([...LEVEL_IDS]);
    const count = (l: (typeof LEVELS)[number]) => Object.values(l.flags).filter(Boolean).length;
    expect(LEVELS.map(count)).toEqual([0, 1, 2, 3]);
  });

  it('map to switches and back', () => {
    for (const id of LEVEL_IDS) expect(levelOf(flagsForLevel(id))).toBe(id);
    expect(flagsForLevel('writes')).toEqual({ commands: true, edits: true, subagents: false });
    expect(() => flagsForLevel('custom')).toThrow();
    expect(levelOf({ commands: true })).toBe('custom');
    expect(levelOf(null)).toBe('checkers');
  });

  it('have a label, also outside the ladder', () => {
    expect(levelLabel({ edits: true })).toBe('Confirm edits');
    expect(levelLabel({ commands: true, subagents: true })).toBe('Custom: confirm commands, subagent results');
    expect(levelLabel(undefined)).toBe('Checkers decide');
  });
});
