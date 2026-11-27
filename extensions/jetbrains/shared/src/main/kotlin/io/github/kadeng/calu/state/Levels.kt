/**
 * Supervision levels: the auto-escalation switches of a session as one of five named levels
 * (the ladder of `calu.acp.modes`, also used by `calu attach`). Pure.
 *
 *     level        unchecked  commands  edits  subagents
 *     unchecked    on         off       off    off        no checker runs; calu's rules still ask
 *     checkers     off        off       off    off        only a checker's escalation waits for a human
 *     edits        off        off       on     off
 *     writes       off        on        on     off
 *     everything   off        on        on     on
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
 * @property unchecked No checker runs: Claude Code decides by itself; calu's rules still apply.
 */
@Serializable
data class AutoEscalateFlags(
    val commands: Boolean = false,
    val edits: Boolean = false,
    val subagents: Boolean = false,
    val unchecked: Boolean = false,
)

/** Ids of the levels a user can choose, from the least to the most strict. */
val LEVEL_IDS: List<String> = listOf("unchecked", "checkers", "edits", "writes", "everything")

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
        id = "unchecked",
        name = "Unchecked / Claude Code only",
        description = "No checker runs: Claude Code decides by itself. calu's rules still ask you.",
        flags = AutoEscalateFlags(unchecked = true),
    ),
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
 * @return The four switches of the level.
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
 *   "Custom: confirm commands, subagent results", with "no checkers, " in front if no checker
 *   runs.
 */
fun levelLabel(flags: AutoEscalateFlags?): String {
    val id = levelOf(flags)
    LEVELS.firstOrNull { it.id == id }?.let { return it.name }
    val parts = listOf(
        if (flags?.edits == true) "edits" else "",
        if (flags?.commands == true) "commands" else "",
        if (flags?.subagents == true) "subagent results" else "",
    ).filter { it.isNotEmpty() }
    val unchecked = if (flags?.unchecked == true) "no checkers, " else ""
    return "Custom: ${unchecked}confirm ${parts.joinToString(", ")}"
}

/**
 * Claude Code's permission modes a client can switch to while a session runs
 * (`permission_mode.set`, agent/records/PLAN.md D247), in the order of Claude Code's own
 * Shift+Tab. The core refuses every other mode with the reason.
 */
val PERMISSION_MODES: List<String> = listOf("default", "acceptEdits", "auto")

/**
 * One permission mode with its texts.
 *
 * @property id The mode as `permission_mode.set` takes it.
 * @property name Short name for a selector.
 * @property description One sentence for a tooltip.
 */
data class PermissionModeText(val id: String, val name: String, val description: String)

/** The modes of `PERMISSION_MODES` with their texts, in the same order. */
val PERMISSION_MODE_TEXTS: List<PermissionModeText> = listOf(
    PermissionModeText("default", "Default", "The checkers decide every tool call alone."),
    PermissionModeText("acceptEdits", "Accept edits", "Claude Code accepts file edits on its own; under calu the checkers still decide each."),
    PermissionModeText("auto", "Auto", "A step runs only if the checkers and auto mode's classifier both approve it."),
)

/**
 * Describe Claude Code's permission mode for the chat footer.
 *
 * @param mode The mode the session reports; `null` while it is not known.
 * @return "mode: Auto" for a mode of `PERMISSION_MODES`, "mode: plan" for another reported
 *   mode, "mode: not known yet" without one.
 */
fun permissionModeLabel(mode: String?): String =
    "mode: " + when (mode) {
        null -> "not known yet"
        else -> PERMISSION_MODE_TEXTS.firstOrNull { it.id == mode }?.name ?: mode
    }
