/**
 * The recorded conversation of a session (`session.history`) as entries for the transcript
 * (pure: no IntelliJ Platform dependency).
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
 * (the same rule as `SessionModel.load_history` of calu's terminal client). Of the boundary turn
 * only what the replay lacks is added: its inputs if none of them was replayed, and its steps
 * that were not replayed if no input was replayed.
 *
 * Nothing is invented: a step of an inherited turn has only the tool's name, the decision and
 * the checker's summary (`TurnRecord.steps`); thinking, streamed text between the steps, diffs
 * and durations are not recorded.
 *
 * Port of `extensions/vscode/src/shared/history.ts`.
 */
package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/** Most entries taken from the record; older turns are left out (see `History.omittedTurns`). */
const val MAX_HISTORY_ENTRIES: Int = 400

/** One entry of the recorded conversation, in the order it happened. */
sealed interface HistoryEntry {
    /** The entry's kind as the TypeScript original names it: "user", "step", "answer" or "turnEnd". */
    val kind: String

    /**
     * A message of the user.
     *
     * @property text The message text.
     * @property turn The conversation's number of the turn (`TurnRecord.index`).
     * @property uuid Claude Code's id of the turn's prompt; `null` if unknown or for a later
     *   input of the turn.
     */
    data class User(val text: String, val turn: Int, val uuid: String?) : HistoryEntry {
        override val kind: String get() = "user"
    }

    /**
     * A tool call.
     *
     * @property toolUseId The call's id if the record holds it (`StoredStep`), else `null`.
     * @property name The tool's name.
     * @property input The tool input if the record holds it, else `null`.
     * @property decision "PASS", "ALLOW", "DENY" or "ESCALATE"; `null` if the step was not judged.
     * @property reason The reason of the decision ("" if none).
     * @property summary What the checker said the step does ("" if nothing).
     * @property result The beginning of what the tool returned; `null` if the record holds none.
     * @property subagent True if a subagent made the call.
     */
    data class Step(
        val toolUseId: String?,
        val name: String,
        val input: JsonObject?,
        val decision: String?,
        val reason: String,
        val summary: String,
        val result: String?,
        val subagent: Boolean,
    ) : HistoryEntry {
        override val kind: String get() = "step"
    }

    /**
     * The answer of a turn.
     *
     * @property text The answer text.
     */
    data class Answer(val text: String) : HistoryEntry {
        override val kind: String get() = "answer"
    }

    /** The end of a recorded turn. */
    data object TurnEnd : HistoryEntry {
        override val kind: String get() = "turnEnd"
    }
}

/**
 * What the replayed events of an attach already show.
 *
 * @property uuids Claude Code's ids of the replayed user messages.
 * @property inputs The trimmed texts of the replayed user messages.
 * @property toolUseIds The ids of the replayed tool calls.
 */
data class Covered(
    val uuids: Set<String> = emptySet(),
    val inputs: Set<String> = emptySet(),
    val toolUseIds: Set<String> = emptySet(),
)

/**
 * The result of `historyEntries`.
 *
 * @property entries The entries to show above the replayed events, oldest first.
 * @property turns Number of recorded turns that contributed entries.
 * @property omittedTurns Number of older turns left out because of `MAX_HISTORY_ENTRIES`.
 * @property firstReplayedTurn The conversation's number of the first turn the replayed events
 *   hold: the boundary turn, or the number after the last recorded turn if the replay holds none
 *   of them (1 for an empty record). The view numbers its own turns from here.
 */
data class History(
    val entries: List<HistoryEntry>,
    val turns: Int,
    val omittedTurns: Int,
    val firstReplayedTurn: Int,
)

/**
 * Read a string member of a recorded object.
 *
 * @param o The object (untrusted).
 * @param key The member's name.
 * @return The member if it is a JSON string, else "".
 */
private fun text(o: JsonObject, key: String): String = o.str(key) ?: ""

/**
 * Convert one recorded step.
 *
 * @param step A `StoredStep` (with `tool_input`, `tool_use_id`, `result_summary`) or a
 *   `StepRecord` of a turn (name, decision and summary only).
 * @return The entry, or `null` if the step has no tool name.
 */
private fun stepEntry(step: JsonObject): HistoryEntry.Step? {
    val name = text(step, "tool_name")
    if (name.isEmpty()) return null
    val decision = text(step, "decision").uppercase()
    val input = step.obj("tool_input")
    return HistoryEntry.Step(
        toolUseId = text(step, "tool_use_id").ifEmpty { null },
        name = name,
        input = input?.takeIf { it.isNotEmpty() },
        decision = decision.ifEmpty { null },
        reason = text(step, "reason"),
        summary = text(step, "summary"),
        result = step.str("result_summary")?.takeIf { it.isNotEmpty() },
        subagent = text(step, "agent_id").isNotEmpty(),
    )
}

/**
 * Convert the result of `session.history` into the transcript entries that the replayed events
 * do not cover (see the module comment for the boundary rule).
 *
 * Example: for `{"turns": [{"index": 1, "user_inputs": ["hi"], "answer": "Hello",
 * "ended_at": "…"}]}` and an empty `covered` the result has the entries user "hi" (turn 1),
 * answer "Hello", turnEnd; `turns` 1; `omittedTurns` 0; `firstReplayedTurn` 2.
 *
 * @param raw The result of `session.history` (untrusted; anything that is not an object gives
 *   no entries).
 * @param covered What the replayed events already show.
 * @return The entries, oldest first, at most `MAX_HISTORY_ENTRIES` (whole turns are dropped
 *   from the old end; the newest turn is kept even if it alone is longer).
 */
fun historyEntries(raw: JsonElement?, covered: Covered): History {
    val history = raw.asObj()
    val turns = (history?.arr("turns") ?: emptyList())
        .mapNotNull { it.asObj() }
        .filter { it.int("index") != null }
    val known = turns.mapNotNull { it.int("index") }.toSet()
    val stored = LinkedHashMap<Int, MutableList<JsonObject>>()
    for (s in history?.arr("steps") ?: emptyList()) {
        val step = s.asObj() ?: continue
        val turn = step.int("turn") ?: continue
        if (turn !in known) continue
        stored.getOrPut(turn) { mutableListOf() }.add(step)
    }

    val perTurn = mutableListOf<List<HistoryEntry>>()
    var firstReplayedTurn = known.fold(0) { last, index -> maxOf(last, index) } + 1
    for (turn in turns) {
        val index = turn.int("index") ?: continue
        val inputs = turn.strings("user_inputs")
        val uuid = text(turn, "prompt_uuid")
        // The steps with input and result if this session recorded them, else the turn's own list.
        val steps: List<JsonObject> = stored[index] ?: turn.arr("steps").mapNotNull { it.asObj() }
        val inputReplayed = (uuid != "" && uuid in covered.uuids) || inputs.any { it.trim() in covered.inputs }
        val stepReplayed = steps.any { text(it, "tool_use_id") in covered.toolUseIds }
        val entries = mutableListOf<HistoryEntry>()
        if (!inputReplayed) {
            inputs.forEachIndexed { n, input ->
                if (input.trim().isNotEmpty()) {
                    entries += HistoryEntry.User(text = input, turn = index, uuid = if (n == 0 && uuid != "") uuid else null)
                }
            }
            for (s in steps) {
                if (text(s, "tool_use_id") in covered.toolUseIds) continue
                stepEntry(s)?.let { entries += it }
            }
        }
        if (inputReplayed || stepReplayed) {
            if (entries.isNotEmpty()) perTurn += entries
            firstReplayedTurn = index
            break // the replay holds the conversation from here on
        }
        val answer = text(turn, "answer")
        if (answer.trim().isNotEmpty()) entries += HistoryEntry.Answer(answer)
        if (text(turn, "ended_at").isNotEmpty()) entries += HistoryEntry.TurnEnd
        if (entries.isNotEmpty()) perTurn += entries
    }

    var first = perTurn.size
    var count = 0
    while (first > 0 && (first == perTurn.size || count + perTurn[first - 1].size <= MAX_HISTORY_ENTRIES)) {
        first--
        count += perTurn[first].size
    }
    return History(entries = perTurn.drop(first).flatten(), turns = perTurn.size - first, omittedTurns = first, firstReplayedTurn = firstReplayedTurn)
}
