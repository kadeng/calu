// Supervision levels: the auto-escalation switches as one of five named levels.
import { describe, expect, it } from 'vitest';
import { LEVELS, LEVEL_IDS, flagsForLevel, levelLabel, levelOf } from '../../src/shared/levels';

describe('levels', () => {
  it('are a ladder: no checkers first, then each level includes the one before', () => {
    expect(LEVELS.map((l) => l.id)).toEqual([...LEVEL_IDS]);
    const count = (l: (typeof LEVELS)[number]) => [l.flags.commands, l.flags.edits, l.flags.subagents].filter(Boolean).length;
    expect(LEVELS.map(count)).toEqual([0, 0, 1, 2, 3]);
    expect(LEVELS.map((l) => l.flags.unchecked)).toEqual([true, false, false, false, false]);
  });

  it('name the level without checkers, and tell it from "Checkers decide"', () => {
    expect(flagsForLevel('unchecked')).toEqual({ commands: false, edits: false, subagents: false, unchecked: true });
    expect(levelLabel({ unchecked: true })).toBe('Unchecked / Claude Code only');
    expect(levelOf({ unchecked: true, edits: true })).toBe('custom');
    expect(levelLabel({ unchecked: true, edits: true })).toBe('Custom: no checkers, confirm edits');
  });

  it('map to switches and back', () => {
    for (const id of LEVEL_IDS) expect(levelOf(flagsForLevel(id))).toBe(id);
    expect(flagsForLevel('writes')).toEqual({ commands: true, edits: true, subagents: false, unchecked: false });
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
