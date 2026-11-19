/**
 * Supervision levels: the three auto-escalation switches of a session as one of four named
 * levels (the ladder of `calu.acp.modes`, also used by `calu attach`). Pure.
 *
 *     level        commands  edits  subagents
 *     checkers     off       off    off        only a checker's escalation waits for a human
 *     edits        off       on     off
 *     writes       on        on     off
 *     everything   on        on     on
 *     custom       any other combination (shown, cannot be chosen)
 */

/** The auto-escalation switches of a session, all set. */
export interface AutoEscalateFlags {
  /** Every command waits for a human, even one the command checker approved. */
  commands: boolean;
  /** Every file edit waits for a human, even one the edit checker approved. */
  edits: boolean;
  /** Every subagent's final result waits for a human. */
  subagents: boolean;
}

/** Ids of the levels a user can choose. */
export const LEVEL_IDS = ['checkers', 'edits', 'writes', 'everything'] as const;
/** A level a user can choose. */
export type LevelId = (typeof LEVEL_IDS)[number];

/** One level with its texts and switches. */
export interface Level {
  id: LevelId;
  /** Short name for a selector. */
  name: string;
  /** One sentence for a tooltip. */
  description: string;
  flags: AutoEscalateFlags;
}

/** The levels from the least to the most strict. */
export const LEVELS: readonly Level[] = [
  {
    id: 'checkers',
    name: 'Checkers decide',
    description: "Only a checker's escalation waits for you; what the checkers approve runs.",
    flags: { commands: false, edits: false, subagents: false },
  },
  {
    id: 'edits',
    name: 'Confirm edits',
    description: 'Every file edit waits for you, even one the edit checker approved.',
    flags: { commands: false, edits: true, subagents: false },
  },
  {
    id: 'writes',
    name: 'Confirm edits and commands',
    description: 'Every file edit and every command waits for you, whatever the checkers decided.',
    flags: { commands: true, edits: true, subagents: false },
  },
  {
    id: 'everything',
    name: 'Confirm everything',
    description: 'Every edit, every command and every subagent result waits for you.',
    flags: { commands: true, edits: true, subagents: true },
  },
];

/**
 * Name the level of a combination of switches.
 *
 * @param flags The switches (missing fields count as off).
 * @returns The id of the level with exactly these switches, or "custom" if none has them.
 */
export function levelOf(flags: Partial<AutoEscalateFlags> | null | undefined): LevelId | 'custom' {
  const f = { commands: flags?.commands === true, edits: flags?.edits === true, subagents: flags?.subagents === true };
  const level = LEVELS.find(
    (l) => l.flags.commands === f.commands && l.flags.edits === f.edits && l.flags.subagents === f.subagents,
  );
  return level ? level.id : 'custom';
}

/**
 * Return the switches of a level, as the params of `auto_escalate.set`.
 *
 * @param id A level id.
 * @returns A new object with the three switches.
 * @throws Error if `id` is not one of `LEVEL_IDS`.
 */
export function flagsForLevel(id: string): AutoEscalateFlags {
  const level = LEVELS.find((l) => l.id === id);
  if (!level) throw new Error(`Unknown supervision level: ${id}`);
  return { ...level.flags };
}

/**
 * Describe the switches for the chat header.
 *
 * @param flags The switches (missing fields count as off).
 * @returns The level's name, or for a combination outside the ladder e.g.
 *   "Custom: confirm commands, subagent results".
 */
export function levelLabel(flags: Partial<AutoEscalateFlags> | null | undefined): string {
  const id = levelOf(flags);
  if (id !== 'custom') return LEVELS.find((l) => l.id === id)!.name;
  const parts = [
    flags?.edits ? 'edits' : '',
    flags?.commands ? 'commands' : '',
    flags?.subagents ? 'subagent results' : '',
  ].filter(Boolean);
  return `Custom: confirm ${parts.join(', ')}`;
}

/**
 * Claude Code's permission modes a client can switch to while a session runs
 * (`permission_mode.set`, agent/records/PLAN.md D247), in the order of Claude Code's own
 * Shift+Tab. The core refuses every other mode with the reason.
 */
export const PERMISSION_MODES = ['default', 'acceptEdits', 'auto'] as const;
/** A permission mode a user can choose. */
export type PermissionModeId = (typeof PERMISSION_MODES)[number];

/** Short name and tooltip of each mode, for the selector. */
export const PERMISSION_MODE_TEXTS: Readonly<Record<PermissionModeId, { name: string; description: string }>> = {
  default: { name: 'Default', description: 'The checkers decide every tool call alone.' },
  acceptEdits: {
    name: 'Accept edits',
    description: 'Claude Code accepts file edits on its own; under calu the checkers still decide each.',
  },
  auto: { name: 'Auto', description: "A step runs only if the checkers and auto mode's classifier both approve it." },
};
