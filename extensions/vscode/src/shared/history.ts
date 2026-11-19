/**
 * The recorded conversation of a session (`session.history`) as entries for the transcript
 * (pure: no `vscode`, no DOM).
 *
 * The events an attach replays only reach back to the start of the session's process, and not
 * even that far once the replay buffer moved on. A resumed, forked or rewound conversation has
 * earlier turns that only calu's record holds:
 *
 *     session.history {turns, steps}
 *        turns[]: index, prompt_uuid, user_inputs[], steps[] (tool, decision, summary), answer
 *        steps[]: the tool calls of the newest session of the conversation, with input and result
 *
 *     historyEntries(history, covered) -> entries of the turns the replay does not cover
 *
 *     turn 1   user · step · step · answer · end      \
 *     turn 2   user · step · answer · end              > added whole
 *     turn 3   user (replayed)  -> boundary: nothing of this turn or a later one is added
 *
 * The first recorded turn that has an input or a step among the replayed ones is the boundary
 * (the same rule as `SessionModel.load_history` of calu's terminal client). An input is found
 * by Claude Code's id of the prompt if the turn has one and the replay has ids at all, else by
 * its text: by text alone, the same words in two turns ("continue") made the earlier turn the
 * boundary and dropped every turn between them. Of the boundary turn
 * only what the replay lacks is added: its inputs if none of them was replayed, and its steps
 * that were not replayed if no input was replayed.
 *
 * Nothing is invented: a step of an inherited turn has only the tool's name, the decision and
 * the checker's summary (`TurnRecord.steps`); thinking, streamed text between the steps, diffs
 * and durations are not recorded.
 */

/** Most entries taken from the record; older turns are left out (see `History.omittedTurns`). */
export const MAX_HISTORY_ENTRIES = 400;

/** One entry of the recorded conversation, in the order it happened. */
export type HistoryEntry =
  | {
      kind: 'user';
      text: string;
      /** The conversation's number of the turn (`TurnRecord.index`). */
      turn: number;
      /** Claude Code's id of the turn's prompt; `null` if unknown or for a later input of the turn. */
      uuid: string | null;
    }
  | {
      kind: 'step';
      /** The call's id if the record holds it (`StoredStep`), else `null`. */
      toolUseId: string | null;
      name: string;
      /** The tool input if the record holds it, else `null`. */
      input: Record<string, unknown> | null;
      /** "PASS", "ALLOW", "DENY" or "ESCALATE"; `null` if the step was not judged. */
      decision: string | null;
      reason: string;
      /** What the checker said the step does ("" if nothing). */
      summary: string;
      /** The beginning of what the tool returned; `null` if the record holds none. */
      result: string | null;
      /** True if a subagent made the call. */
      subagent: boolean;
    }
  | { kind: 'answer'; text: string }
  | { kind: 'turnEnd' };

/** What the replayed events of an attach already show. */
export interface Covered {
  /** Claude Code's ids of the replayed user messages. */
  uuids: ReadonlySet<string>;
  /** The trimmed texts of the replayed user messages. */
  inputs: ReadonlySet<string>;
  /** The ids of the replayed tool calls. */
  toolUseIds: ReadonlySet<string>;
}

/** The result of `historyEntries`. */
export interface History {
  /** The entries to show above the replayed events, oldest first. */
  entries: HistoryEntry[];
  /** Number of recorded turns that contributed entries. */
  turns: number;
  /** Number of older turns left out because of `MAX_HISTORY_ENTRIES`. */
  omittedTurns: number;
  /**
   * The conversation's number of the first turn the replayed events hold: the boundary turn,
   * or the number after the last recorded turn if the replay holds none of them (1 for an
   * empty record). The view numbers its own turns from here.
   */
  firstReplayedTurn: number;
}

/** A recorded step as this module reads it (`StoredStep` or `StepRecord`, untrusted). */
type RawStep = Record<string, unknown>;

/**
 * Read a JSON value as an object.
 *
 * @param v Any value.
 * @returns `v` if it is a non-array object, else `undefined`.
 */
function obj(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

/**
 * Read a JSON value as a string.
 *
 * @param v Any value.
 * @returns `v` if it is a string, else "".
 */
function text(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/**
 * Convert one recorded step.
 *
 * @param step A `StoredStep` (with `tool_input`, `tool_use_id`, `result_summary`) or a
 *   `StepRecord` of a turn (name, decision and summary only).
 * @returns The entry, or `undefined` if the step has no tool name.
 */
function stepEntry(step: RawStep): HistoryEntry | undefined {
  const name = text(step.tool_name);
  if (!name) return undefined;
  const decision = text(step.decision).toUpperCase();
  const input = obj(step.tool_input);
  return {
    kind: 'step',
    toolUseId: text(step.tool_use_id) || null,
    name,
    input: input && Object.keys(input).length > 0 ? input : null,
    decision: decision || null,
    reason: text(step.reason),
    summary: text(step.summary),
    result: typeof step.result_summary === 'string' && step.result_summary !== '' ? step.result_summary : null,
    subagent: text(step.agent_id) !== '',
  };
}

/**
 * Convert the result of `session.history` into the transcript entries that the replayed events
 * do not cover (see the module comment for the boundary rule).
 *
 * @param raw The result of `session.history` (untrusted; anything that is not an object gives
 *   no entries).
 * @param covered What the replayed events already show.
 * @returns The entries, oldest first, at most `MAX_HISTORY_ENTRIES` (whole turns are dropped
 *   from the old end; the newest turn is kept even if it alone is longer).
 *
 * @example
 * historyEntries({turns: [{index: 1, user_inputs: ["hi"], answer: "Hello", ended_at: "…"}]},
 *                {uuids: new Set(), inputs: new Set(), toolUseIds: new Set()})
 * // -> entries: user "hi" (turn 1), answer "Hello", turnEnd; turns: 1; omittedTurns: 0;
 * //    firstReplayedTurn: 2
 */
export function historyEntries(raw: unknown, covered: Covered): History {
  const history = obj(raw);
  const turns = (Array.isArray(history?.turns) ? history.turns : [])
    .map(obj)
    .filter((t): t is Record<string, unknown> => t !== undefined && typeof t.index === 'number');
  const known = new Set(turns.map((t) => t.index as number));
  const stored = new Map<number, RawStep[]>();
  for (const s of Array.isArray(history?.steps) ? history.steps : []) {
    const step = obj(s);
    if (!step || typeof step.turn !== 'number' || !known.has(step.turn)) continue;
    stored.set(step.turn, [...(stored.get(step.turn) ?? []), step]);
  }

  const perTurn: HistoryEntry[][] = [];
  let firstReplayedTurn = turns.reduce((last, t) => Math.max(last, t.index as number), 0) + 1;
  for (const turn of turns) {
    const index = turn.index as number;
    const inputs = (Array.isArray(turn.user_inputs) ? turn.user_inputs : []).filter((i): i is string => typeof i === 'string');
    const uuid = text(turn.prompt_uuid);
    // The steps with input and result if this session recorded them, else the turn's own list.
    const detailed = stored.get(index);
    const steps = (detailed ?? (Array.isArray(turn.steps) ? turn.steps : []).map(obj)).filter((s): s is RawStep => s !== undefined);
    // A prompt is recognised by its id where both sides have ids; the text decides only without.
    const inputReplayed =
      uuid !== '' && covered.uuids.size > 0 ? covered.uuids.has(uuid) : inputs.some((i) => covered.inputs.has(i.trim()));
    const stepReplayed = steps.some((s) => covered.toolUseIds.has(text(s.tool_use_id)));
    const entries: HistoryEntry[] = [];
    if (!inputReplayed) {
      inputs.forEach((input, n) => {
        if (input.trim()) entries.push({ kind: 'user', text: input, turn: index, uuid: n === 0 && uuid ? uuid : null });
      });
      for (const s of steps) {
        if (covered.toolUseIds.has(text(s.tool_use_id))) continue;
        const entry = stepEntry(s);
        if (entry) entries.push(entry);
      }
    }
    if (inputReplayed || stepReplayed) {
      if (entries.length > 0) perTurn.push(entries);
      firstReplayedTurn = index;
      break; // the replay holds the conversation from here on
    }
    const answer = text(turn.answer);
    if (answer.trim()) entries.push({ kind: 'answer', text: answer });
    if (typeof turn.ended_at === 'string' && turn.ended_at) entries.push({ kind: 'turnEnd' });
    if (entries.length > 0) perTurn.push(entries);
  }

  let first = perTurn.length;
  let count = 0;
  while (first > 0 && (first === perTurn.length || count + perTurn[first - 1].length <= MAX_HISTORY_ENTRIES)) {
    first--;
    count += perTurn[first].length;
  }
  return { entries: perTurn.slice(first).flat(), turns: perTurn.length - first, omittedTurns: first, firstReplayedTurn };
}
