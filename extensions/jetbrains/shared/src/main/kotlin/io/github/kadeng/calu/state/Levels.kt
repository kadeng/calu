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
 *
 * Port of `extensions/vscode/src/shared/levels.ts`.
 */
package io.github.kadeng.calu.state

import kotlinx.serialization.Serializable

/**
 * The auto-escalation switches of a session, all set.
 *
 * @property commands Every command waits for a human, even one the command checker approved.
 * @property edits Every file edit waits for a human, even one the edit checker approved.
 * @property subagents Every subagent's final result waits for a human.
 */
@Serializable
data class AutoEscalateFlags(
    val commands: Boolean = false,
    val edits: Boolean = false,
    val subagents: Boolean = false,
)

/** Ids of the levels a user can choose, from the least to the most strict. */
val LEVEL_IDS: List<String> = listOf("checkers", "edits", "writes", "everything")

/** The result of `levelOf` for a combination of switches that is none of the levels. */
const val LEVEL_CUSTOM: String = "custom"

/**
 * One level with its texts and switches.
 *
 * @property id One of `LEVEL_IDS`.
 * @property name Short name for a selector.
 * @property description One sentence for a tooltip.
 * @property flags The switches of the level.
 */
@Serializable
data class Level(
    val id: String,
    val name: String,
    val description: String,
    val flags: AutoEscalateFlags,
)

/** The levels from the least to the most strict. */
val LEVELS: List<Level> = listOf(
    Level(
        id = "checkers",
        name = "Checkers decide",
        description = "Only a checker's escalation waits for you; what the checkers approve runs.",
        flags = AutoEscalateFlags(commands = false, edits = false, subagents = false),
    ),
    Level(
        id = "edits",
        name = "Confirm edits",
        description = "Every file edit waits for you, even one the edit checker approved.",
        flags = AutoEscalateFlags(commands = false, edits = true, subagents = false),
    ),
    Level(
        id = "writes",
        name = "Confirm edits and commands",
        description = "Every file edit and every command waits for you, whatever the checkers decided.",
        flags = AutoEscalateFlags(commands = true, edits = true, subagents = false),
    ),
    Level(
        id = "everything",
        name = "Confirm everything",
        description = "Every edit, every command and every subagent result waits for you.",
        flags = AutoEscalateFlags(commands = true, edits = true, subagents = true),
    ),
)

/**
 * Name the level of a combination of switches.
 *
 * @param flags The switches; `null` counts as all off.
 * @return The id of the level with exactly these switches, or `LEVEL_CUSTOM` ("custom") if none
 *   has them.
 */
fun levelOf(flags: AutoEscalateFlags?): String {
    val f = flags ?: AutoEscalateFlags()
    return LEVELS.firstOrNull { it.flags == f }?.id ?: LEVEL_CUSTOM
}

/**
 * Return the switches of a level, as the params of `auto_escalate.set`.
 *
 * @param id A level id.
 * @return The three switches of the level.
 * @throws IllegalArgumentException if `id` is not one of `LEVEL_IDS`.
 */
fun flagsForLevel(id: String): AutoEscalateFlags =
    LEVELS.firstOrNull { it.id == id }?.flags
        ?: throw IllegalArgumentException("Unknown supervision level: $id")

/**
 * Describe the switches for the chat header.
 *
 * @param flags The switches; `null` counts as all off.
 * @return The level's name, or for a combination outside the ladder e.g.
 *   "Custom: confirm commands, subagent results".
 */
fun levelLabel(flags: AutoEscalateFlags?): String {
    val id = levelOf(flags)
    LEVELS.firstOrNull { it.id == id }?.let { return it.name }
    val parts = listOf(
        if (flags?.edits == true) "edits" else "",
        if (flags?.commands == true) "commands" else "",
        if (flags?.subagents == true) "subagent results" else "",
    ).filter { it.isNotEmpty() }
    return "Custom: confirm ${parts.joinToString(", ")}"
}
