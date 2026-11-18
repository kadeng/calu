/**
 * Slash commands of the composer (pure): the command table, the completion list and what a
 * submitted command does.
 *
 *     input "/mo"            ──completions()──►  [/model …]              (list above the composer)
 *     input "/model so"      ──completions()──►  [sonnet]                (values of the command)
 *     Enter on "/model opus" ──resolveSlash()──► Act("setModel", {model: "opus"})
 *     Enter on "/compact"    ──resolveSlash()──► Chat                    (sent to Claude Code as text)
 *     Enter on "/sessions"   ──resolveSlash()──► Ui("sessions")
 *
 * Commands of Claude Code are sent as chat text; Claude Code runs them. Commands of calu run a
 * chat action (`CaluRemoteApi.act`) or open a part of the view. A "/word" that is not in the
 * table is sent as chat text too (a custom command of the project).
 *
 * Port of `extensions/vscode/src/shared/slashCommands.ts`. Where the original posts a webview
 * message, the port names the chat action; a command that needs another input first (a name,
 * a choice from a list) opens a part of the view:
 *
 *     /model <m>         Act("setModel", {model})          /model         Ui("model")
 *     /effort <e>        Act("setEffort", {effort})        /effort        Ui("effort")
 *     /level <l>         Act("setLevel", {level})          /level         Ui("level")
 *     /honesty <mode>    Act("setHonesty", {mode})         /honesty       Ui("honesty")
 *     /checker-context   Act("showContext", {})            /checkers      Ui("checkers")
 *     /new               Act("start", {kind: "new"})       /sessions      Ui("sessions")
 *     /fork              Act("forkConversation", {})       /rename        Ui("rename")
 *     /interrupt         Act("interrupt", {})              /settings      Ui("settings")
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.state.LEVELS
import io.github.kadeng.calu.state.LEVEL_IDS
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Claude Code's model aliases offered by the model selector; "default" is Claude Code's own default. */
val CLAUDE_MODELS: List<String> = listOf("default", "fable", "opus", "sonnet", "haiku")

/** Effort levels of `claude.effort`; "default" is Claude Code's own default. */
val CLAUDE_EFFORTS: List<String> = listOf("default", "low", "medium", "high", "xhigh", "max")

/** The code honesty modes (`honesty.mode`). */
val HONESTY_MODES: List<String> = listOf("off", "track", "enforce")

/**
 * An argument value of a slash command.
 *
 * @property value The value as typed.
 * @property description One line for the completion list ("" for none).
 */
data class SlashValue(val value: String, val description: String)

/**
 * One slash command.
 *
 * @property name The name without the slash, e.g. "model".
 * @property description One line for the completion list.
 * @property source "claude": sent as chat text; "calu": an action of the plugin.
 * @property values Values the command takes as its argument (offered after "/name "); empty if none.
 */
data class SlashCommand(val name: String, val description: String, val source: String, val values: List<SlashValue> = emptyList())

/**
 * Commands of Claude Code that are always offered (sent as chat text). Each one was run
 * through calu against a real Claude Code (CLI 2.1.327), which runs them without the model:
 *
 *     /compact   replaces the conversation by a summary; the summary is shown
 *     /clear     Claude forgets the conversation; the transcript stays and a notice says so
 *     /usage     prints cost, durations and tokens by model (the same text as /cost)
 *     /cost      prints cost, durations and tokens by model
 *     /context   prints a table of what fills the context window
 *
 * `/help` is not in the list: a session without a terminal answers "/help isn't available in
 * this environment."
 */
val CLAUDE_COMMANDS: List<SlashCommand> = listOf(
    SlashCommand("compact", "Summarize the conversation to free context (optional: what to keep)", "claude"),
    SlashCommand("clear", "Claude forgets the conversation; the transcript here stays", "claude"),
    SlashCommand("usage", "Show cost, durations and tokens of the session", "claude"),
    SlashCommand("context", "Show what fills the context window", "claude"),
    SlashCommand("cost", "Show cost, durations and tokens of the session", "claude"),
)

/** Commands of the plugin. */
val CALU_COMMANDS: List<SlashCommand> = listOf(
    SlashCommand(
        "model", "Change the model of the running session", "calu",
        CLAUDE_MODELS.map { SlashValue(it, if (it == "default") "Claude Code's default model" else "The latest $it model") },
    ),
    SlashCommand(
        "effort", "Change the thinking effort (sessions started from now on)", "calu",
        CLAUDE_EFFORTS.map { SlashValue(it, if (it == "default") "Claude Code's default effort" else "") },
    ),
    SlashCommand("level", "Change the supervision level", "calu", LEVELS.map { SlashValue(it.id, it.name) }),
    SlashCommand(
        "honesty", "Change the code honesty mode", "calu",
        listOf(
            SlashValue("off", "No honesty tracking"),
            SlashValue("track", "Track marks and unreviewed files"),
            SlashValue("enforce", "Running project code and commits need a clean state"),
        ),
    ),
    SlashCommand("checkers", "Edit the instructions of a checker", "calu"),
    SlashCommand("checker-context", "Show the conversation as the checkers see it", "calu"),
    SlashCommand("sessions", "Switch to another running session", "calu"),
    SlashCommand("new", "Start a new session", "calu"),
    SlashCommand("fork", "Fork the conversation into a new session", "calu"),
    SlashCommand("rename", "Rename the attached session", "calu"),
    SlashCommand("settings", "Open calu's settings", "calu"),
    SlashCommand("interrupt", "Interrupt Claude's running turn", "calu"),
)

private val COMMAND_NAME = Regex("^[\\w:.-]+$")

/**
 * Return the full command table.
 *
 * @param reported Names of further Claude Code commands that a session reported (with or
 *   without the slash); names already in the table and malformed names are ignored.
 * @return The plugin's commands, Claude Code's built-in ones and the reported ones, sorted by
 *   name (letter case ignored).
 */
fun allCommands(reported: List<String> = emptyList()): List<SlashCommand> {
    val table = ArrayList(CALU_COMMANDS + CLAUDE_COMMANDS)
    val known = table.mapTo(HashSet()) { it.name }
    for (raw in reported) {
        val name = raw.removePrefix("/").trim()
        if (!COMMAND_NAME.matches(name) || !known.add(name)) continue
        table.add(SlashCommand(name, "Command of Claude Code", "claude"))
    }
    return table.sortedWith(compareBy<SlashCommand, String>(String.CASE_INSENSITIVE_ORDER) { it.name }.thenBy { it.name })
}

/**
 * One row of the completion list.
 *
 * @property label Shown text, e.g. "/model" or "sonnet".
 * @property description One line beside the label.
 * @property kind "claude", "calu" or "value" (an argument value).
 * @property insert The composer text after accepting the row with Tab.
 * @property submit True if accepting with Enter should submit `insert` at once (nothing more to type).
 */
data class Completion(val label: String, val description: String, val kind: String, val insert: String, val submit: Boolean)

private val COMPLETION_INPUT = Regex("^/(\\S*)(?:\\s+(\\S*))?$")

/**
 * Order the items that match a typed part: those that start with it, then those that contain it.
 *
 * @param items The candidates.
 * @param part The typed part (letter case ignored).
 * @param key The text of a candidate that is compared.
 * @return The matching candidates; within each group the order of `items` is kept.
 */
private fun <T> rank(items: List<T>, part: String, key: (T) -> String): List<T> {
    val p = part.lowercase()
    val (starts, rest) = items.partition { key(it).lowercase().startsWith(p) }
    return starts + rest.filter { key(it).lowercase().contains(p) }
}

/**
 * Compute the completion list for the composer text.
 *
 * @param text The whole composer text.
 * @param commands The command table (`allCommands()`).
 * @return Empty unless the text is one line that starts with "/". For "/<part>": the commands
 *   whose name starts with `part`, then those that contain it. For "/<name> <part>" of a command
 *   with values: its values, filtered the same way. A command with values inserts "/name " and
 *   is not submitted; other rows are submitted on Enter.
 */
fun completions(text: String, commands: List<SlashCommand>): List<Completion> {
    if (!text.startsWith("/") || text.contains('\n')) return emptyList()
    val m = COMPLETION_INPUT.find(text) ?: return emptyList()
    val part = m.groupValues[1]
    val arg = m.groups[2]?.value
    if (arg == null) {
        return rank(commands, part) { it.name }.map { c ->
            Completion("/${c.name}", c.description, c.source, if (c.values.isNotEmpty()) "/${c.name} " else "/${c.name}", c.values.isEmpty())
        }
    }
    val cmd = commands.firstOrNull { it.name == part } ?: return emptyList()
    return rank(cmd.values, arg) { it.value }.map { v -> Completion(v.value, v.description, "value", "/${cmd.name} ${v.value}", true) }
}

/** What submitting a composer text that starts with "/" does. */
sealed interface SlashResult {
    /** Send the text to Claude as a chat message. */
    data object Chat : SlashResult

    /**
     * Run a chat action (`CaluRemoteApi.act`).
     *
     * @property action The action's name.
     * @property args Its arguments.
     */
    data class Act(val action: String, val args: JsonObject = EMPTY_OBJECT) : SlashResult

    /**
     * Open a part of the view (the command needs a choice or another input).
     *
     * @property action "sessions", "model", "effort", "level", "honesty", "checkers" (the list
     *   of checkers to edit), "rename" (ask the new name of the attached session) or "settings".
     */
    data class Ui(val action: String) : SlashResult

    /**
     * Do nothing and show a text near the input.
     *
     * @property text The text.
     */
    data class Error(val text: String) : SlashResult
}

private val SLASH_INPUT = Regex("^/([\\w:.-]+)(?:\\s+([\\s\\S]*))?$")
private val MODEL_NAME = Regex("^[\\w.:\\[\\]-]{1,100}$")

/**
 * Build the arguments of an action with one string member.
 *
 * @param key The member's name.
 * @param value Its value.
 * @return `{key: value}`.
 */
private fun arg(key: String, value: String): JsonObject = buildJsonObject { put(key, JsonPrimitive(value)) }

/**
 * Decide what a submitted composer text does.
 *
 * @param text The composer text (trimmed by the caller or not).
 * @param attachedId Id of the attached session, or `null`.
 * @return `Chat` for a text that does not start with "/", for Claude Code's commands and for
 *   unknown commands; the action or view part of a plugin command (see the table in the file
 *   comment); `Error` for an argument that is not one of the command's values, or a command
 *   that needs a session while none is attached.
 */
fun resolveSlash(text: String, attachedId: String?): SlashResult {
    val m = SLASH_INPUT.find(text.trim()) ?: return SlashResult.Chat
    val cmd = CALU_COMMANDS.firstOrNull { it.name == m.groupValues[1] } ?: return SlashResult.Chat
    val value = m.groups[2]?.value.orEmpty().trim()
    val attached = !attachedId.isNullOrEmpty()
    fun needsSession(result: SlashResult): SlashResult = if (attached) result else SlashResult.Error("/${cmd.name} needs an attached session.")
    val badValue = SlashResult.Error("/${cmd.name} takes one of: ${cmd.values.joinToString(", ") { it.value }}.")
    return when (cmd.name) {
        "model" -> when {
            value.isEmpty() -> SlashResult.Ui("model")
            MODEL_NAME.matches(value) -> needsSession(SlashResult.Act("setModel", arg("model", value)))
            else -> badValue
        }
        "effort" -> when {
            value.isEmpty() -> SlashResult.Ui("effort")
            value in CLAUDE_EFFORTS -> SlashResult.Act("setEffort", arg("effort", value))
            else -> badValue
        }
        "level" -> when {
            value.isEmpty() -> SlashResult.Ui("level")
            value in LEVEL_IDS -> needsSession(SlashResult.Act("setLevel", arg("level", value)))
            else -> badValue
        }
        "honesty" -> when {
            value.isEmpty() -> SlashResult.Ui("honesty")
            value in HONESTY_MODES -> SlashResult.Act("setHonesty", arg("mode", value))
            else -> badValue
        }
        "checkers" -> SlashResult.Ui("checkers")
        "checker-context" -> needsSession(SlashResult.Act("showContext"))
        "sessions" -> SlashResult.Ui("sessions")
        "new" -> SlashResult.Act("start", arg("kind", "new"))
        "fork" -> needsSession(SlashResult.Act("forkConversation"))
        "rename" -> needsSession(SlashResult.Ui("rename"))
        "settings" -> SlashResult.Ui("settings")
        "interrupt" -> needsSession(SlashResult.Act("interrupt"))
        else -> SlashResult.Chat
    }
}
