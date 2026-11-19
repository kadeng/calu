/**
 * State of the chat view (the types; the pure reducer that applies events to it is in
 * `Reducer.kt`).
 *
 * The plugin keeps one authoritative copy per project (it receives every event, also while the
 * tool window is hidden) and renders the chat from it:
 *
 *     calu ide ──session.event──► applyEvent(state, event) ──► view
 *     plugin (ui.* / bridge.status events of its own) ──same path──►
 *     ide.attach / ide.start result ──► `ui.attached` event (snapshot + recorded events)
 *
 * No IntelliJ Platform dependency. Every type is an immutable, `@Serializable` data class; the
 * whole `ViewState` can be written with `CaluJson` and read back. The JSON has the shape of the
 * TypeScript original (`extensions/vscode/src/shared/viewState.ts`): camelCase members, the
 * item's `kind` as the class discriminator, and the protocol's snake_case names for the types
 * taken from the protocol (`AgentInfo`, `HonestyInfo`, `HonestyMarkInfo`, `RateLimit`). An
 * optional member of the original is a nullable property with the default `null` here (written
 * as JSON `null`).
 *
 * Maps keyed by ids from calu (`items`, `tools`, `escalations`, `subagents`) are read only
 * through `own()` and written only through `withEntry()` / `withoutEntries()` (`ViewHelpers.kt`),
 * which copy the map and keep the insertion order. Ids such as "__proto__" are ordinary keys.
 */
@file:OptIn(ExperimentalSerializationApi::class)

package io.github.kadeng.calu.state

import io.github.kadeng.calu.json.EMPTY_OBJECT
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator
import kotlinx.serialization.json.JsonObject

/** The main agent's key in `ViewState.items` (a message list key is "main" or a subagent's `agent_id`). */
const val MAIN: String = "main"

/** Maximum number of items kept per agent; older items (and what only they reference) are dropped. */
const val MAX_ITEMS_PER_AGENT: Int = 1000

/** Maximum stored length of a tool result and of each string inside a tool input. */
const val MAX_STORED_TEXT: Int = 4000

/** Maximum stored length of a subagent's result (it is the point of the subagent's tab). */
const val MAX_AGENT_RESULT_TEXT: Int = 40000

/** Most file links kept per tool call. */
const val MAX_TOOL_FILES: Int = 50

/**
 * Largest base64 text of one attached image that a session accepts (`MAX_IMAGE_BASE64` of
 * `messages.ts`): 5 MiB of image data (`MAX_IMAGE_BYTES` in `calu.protocol.messages`); base64
 * needs 4 characters per 3 bytes. Value: ceil(5 * 1024 * 1024 / 3) * 4 = 6 990 508.
 */
const val MAX_IMAGE_BASE64: Int = (5 * 1024 * 1024 + 2) / 3 * 4

/**
 * Largest base64 text of one image kept in the state; larger images are shown as omitted. It
 * is the largest image a session accepts (`MAX_IMAGE_BASE64`), so every image the user could
 * attach is also shown.
 */
const val MAX_IMAGE_DATA: Int = MAX_IMAGE_BASE64

/** Most side questions kept (the oldest are dropped). */
const val MAX_SIDE_EXCHANGES: Int = 20

/** Text of the divider above the recorded turns. */
const val HISTORY_START_TEXT: String = "Earlier in this conversation"

/** Text of the divider below the recorded turns. */
const val HISTORY_END_TEXT: String = "From calu's record: messages, steps and answers, without thinking and diffs"

/**
 * Values of `BridgeView.status`: the state of the bridge process (`calu ide`) as seen by the
 * plugin.
 */
object BridgeStatus {
    /** The bridge was not started or was stopped. */
    const val STOPPED: String = "stopped"

    /** The bridge process is starting. */
    const val STARTING: String = "starting"

    /** The bridge runs. */
    const val RUNNING: String = "running"

    /** The bridge process ended. */
    const val EXITED: String = "exited"

    /** The bridge could not be started or failed. */
    const val ERROR: String = "error"

    /** calu is not installed on the workspace machine (the view offers *Install*). */
    const val MISSING: String = "missing"
}

/** Values of `LinkView.status`: the connection between the bridge and a session. */
object LinkStatus {
    /** No session is attached. */
    const val DETACHED: String = "detached"

    /** A session is attached. */
    const val ATTACHED: String = "attached"

    /** The bridge tries to reach the attached session again. */
    const val RECONNECTING: String = "reconnecting"

    /** The attached session can no longer be reached. */
    const val LOST: String = "lost"
}

/** Values of `EscalationView.status`. */
object EscalationStatus {
    /** The step waits for a decision. */
    const val OPEN: String = "open"

    /** The step was allowed. */
    const val ALLOW: String = "allow"

    /** The step was denied. */
    const val DENY: String = "deny"

    /** The escalation ended without a decision (session stopped, left, or no longer listed). */
    const val CANCELLED: String = "cancelled"

    /** Closed by the user's answer to the agent's questions. */
    const val ANSWERED: String = "answered"
}

/** Values of `Item.kind` (also the class discriminator of an item in JSON). */
object ItemKind {
    /** `UserItem`. */
    const val USER: String = "user"

    /** `AssistantItem`. */
    const val ASSISTANT: String = "assistant"

    /** `ThinkingItem`. */
    const val THINKING: String = "thinking"

    /** `ToolItem`. */
    const val TOOL: String = "tool"

    /** `CheckerItem`. */
    const val CHECKER: String = "checker"

    /** `EscalationItem`. */
    const val ESCALATION: String = "escalation"

    /** `TurnItem`. */
    const val TURN: String = "turn"

    /** `LaunchItem`. */
    const val LAUNCH: String = "launch"

    /** `NoticeItem`. */
    const val NOTICE: String = "notice"

    /** `RelayItem`. */
    const val RELAY: String = "relay"

    /** `DividerItem`. */
    const val DIVIDER: String = "divider"
}

/** Values of `UserItem.source`: who wrote the text. */
object UserSource {
    /** The human. Only this source is shown as the user's words. */
    const val USER: String = "user"

    /** Another Claude session on the machine. */
    const val CROSS_SESSION: String = "cross_session"

    /** calu or Claude Code: a checker's note, the notification of a finished background task, hook feedback. */
    const val SYSTEM: String = "system"
}

/** Values of `QueuedView.delivery`. */
object Delivery {
    /** The message waits until Claude's turn ends. */
    const val QUEUED: String = "queued"

    /** The message is read after the running tool calls finish. */
    const val INJECT: String = "inject"
}

/** Values of `DividerItem.mark`. */
object DividerMark {
    /** The recorded turns follow. */
    const val HISTORY_START: String = "history-start"

    /** The session's own events follow. */
    const val HISTORY_END: String = "history-end"
}

// ------------------------------------------------------------------ protocol types

/**
 * One subagent of the session (`AgentInfo` of the protocol).
 *
 * @property agentId The subagent's id (= the id of the Agent/Task tool call that launched it).
 * @property background True if it runs in the background; `null` if not given.
 * @property description The short task description; `null` if not given.
 * @property sdkAgentId Claude Code's own id of the subagent; `null` if not given.
 * @property status "running", "completed", "failed" or "stopped"; `null` counts as "running".
 * @property subagentType The subagent type; `null` if not given.
 */
@Serializable
data class AgentInfo(
    @SerialName("agent_id") val agentId: String,
    val background: Boolean? = null,
    val description: String? = null,
    @SerialName("sdk_agent_id") val sdkAgentId: String? = null,
    val status: String? = null,
    @SerialName("subagent_type") val subagentType: String? = null,
)

/**
 * One open mark of the session's honesty state (`HonestyMarkInfo` of the protocol). A mark names
 * code a checker or an honesty review found dishonest.
 *
 * @property id The mark's number.
 * @property path The file.
 * @property reason Why the code was marked (cut to `MAX_STORED_TEXT` by `parseHonesty`).
 * @property symbol The marked symbol; "" when the whole file is meant.
 * @property openedBy Who opened the mark; `null` if unknown.
 */
@Serializable
data class HonestyMarkInfo(
    val id: Int,
    val path: String,
    val reason: String,
    val symbol: String = "",
    @SerialName("opened_by") val openedBy: String? = null,
)

/**
 * The code honesty state of a session (`HonestyInfo` of the protocol). The defaults are the
 * values `parseHonesty` uses for missing members.
 *
 * @property mode "off", "track", "enforce" or another mode name of calu.
 * @property clean True if nothing is open.
 * @property baseline The trusted commit; `null` if there is none.
 * @property baselineSource Where the baseline comes from; `null` if unknown.
 * @property marks The open marks.
 * @property reviewRequired True if a review must run before the state can be clean.
 * @property reviewRunning True while an honesty review runs.
 * @property unreviewed Paths of (some of) the unreviewed files.
 * @property unreviewedAll True if every file counts as unreviewed.
 * @property unreviewedCount Number of unreviewed files.
 */
@Serializable
data class HonestyInfo(
    val mode: String = "off",
    val clean: Boolean = false,
    val baseline: String? = null,
    @SerialName("baseline_source") val baselineSource: String? = null,
    val marks: List<HonestyMarkInfo> = emptyList(),
    @SerialName("review_required") val reviewRequired: Boolean = false,
    @SerialName("review_running") val reviewRunning: Boolean = false,
    val unreviewed: List<String> = emptyList(),
    @SerialName("unreviewed_all") val unreviewedAll: Boolean = false,
    @SerialName("unreviewed_count") val unreviewedCount: Int = 0,
)

/**
 * One usage limit of the account (`RateLimit` of the protocol).
 *
 * @property type The limit's name, e.g. "five_hour"; "" if not given.
 * @property status Claude Code's status of the limit, e.g. "allowed"; "" if not given.
 * @property utilization Used share (0..1); `null` if unknown.
 * @property resetsAt ISO time the limit resets; `null` if unknown.
 */
@Serializable
data class RateLimit(
    val type: String = "",
    val status: String = "",
    val utilization: Double? = null,
    @SerialName("resets_at") val resetsAt: String? = null,
)

/**
 * What the user is shown about starting the server (`ServerHelp` of `serverCommand.ts`; built
 * by `io.github.kadeng.calu.logic`).
 *
 * @property command The complete command for a POSIX shell.
 * @property tool What keeps the server running: "tmux", "screen" or "plain" (nothing does).
 * @property detach How to leave the server running and get the terminal back; "" for "plain".
 * @property note One or two sentences on what the command does and what happens when the editor
 *   closes.
 */
@Serializable
data class ServerHelp(
    val command: String,
    val tool: String,
    val detach: String,
    val note: String,
)

// ------------------------------------------------------------------ tool calls and escalations

/**
 * The routing and the checker's decision of a tool call (`check.result`).
 *
 * @property route "command", "edit", "changeset", "subagent", "question", a rule's route
 *   ("allow", "deny", "escalate"), or "stored" for a step taken from calu's record.
 * @property decision "PASS", "ALLOW", "DENY" or "ESCALATE".
 * @property reason The reason of the decision ("" if none).
 * @property summary What the checker said the step does ("" if nothing).
 * @property comment The checker's remark made while approving ("" if none).
 * @property checkerRoute The route to pass to `editChecker {route}` ("" if a rule decided and no
 *   checker judged the step).
 */
@Serializable
data class ToolCheck(
    val route: String,
    val decision: String,
    val reason: String,
    val summary: String,
    val comment: String,
    val checkerRoute: String,
)

/**
 * A unit (function, class, method) the honesty checker found dishonest.
 *
 * @property symbol The unit's name.
 * @property reason What disagrees with what.
 */
@Serializable
data class HonestyUnitView(val symbol: String, val reason: String)

/**
 * The honesty checker's status of one file a step changes.
 *
 * @property path The file as the checker names it.
 * @property honest False for the verdict DISHONEST.
 * @property reason For a dishonest file: what disagrees with what ("" if none).
 * @property dishonestUnits The units the checker found dishonest, with their reasons.
 */
@Serializable
data class HonestyFileView(val path: String, val honest: Boolean, val reason: String = "", val dishonestUnits: List<HonestyUnitView> = emptyList())

/**
 * The honesty checker's result of one step (`check.result.honesty`, `honesty_escalation`). It is
 * a status per file and no decision: a row never shows it as a PASS or ALLOW chip.
 *
 * @property files The status of every file the checker judged.
 * @property escalation Its reason for asking a person; "" if it did not ask.
 */
@Serializable
data class HonestyResultView(val files: List<HonestyFileView> = emptyList(), val escalation: String = "")

/**
 * The result of a tool call.
 *
 * @property content What the tool returned, cut to `MAX_STORED_TEXT` characters.
 * @property isError True if the call failed.
 */
@Serializable
data class ToolResultView(val content: String, val isError: Boolean)

/**
 * Changed lines of an edit in the new content.
 *
 * @property start First changed line (1-based).
 * @property end Last changed line (inclusive); `start - 1` for a pure deletion.
 */
@Serializable
data class EditRange(val start: Int, val end: Int)

/**
 * A file a tool call refers to (`tool.files`).
 *
 * @property raw The path as written in the tool input.
 * @property path The absolute path.
 */
@Serializable
data class ToolFile(val raw: String, val path: String)

/**
 * Marker of a step taken from calu's record (`ui.history`) instead of from events.
 *
 * @property subagent True if a subagent made the call (the row is shown in Main, because the
 *   record names no tab for it).
 * @property hasInput True if the record holds the tool input.
 */
@Serializable
data class StoredStepInfo(val subagent: Boolean, val hasInput: Boolean)

/**
 * A tool call with its check decision and result, as far as they are known.
 *
 * @property toolUseId The call's id (its key in `ViewState.tools`).
 * @property agentKey The message list that shows the call.
 * @property name The tool's name.
 * @property input The tool input; strings longer than `MAX_STORED_TEXT` are cut (see `boundInput`).
 * @property check The routing and the checker's decision; `null` before `check.result`.
 * @property result The result; `null` before `tool.result`.
 * @property honesty The honesty checker's result of the step; `null` if it did not judge it.
 * @property diffPath Set when an `edit.diff` arrived for this tool call: the file path shown in
 *   the diff.
 * @property editRange Changed lines of the edit, from `edit.diff`; `null` if nothing changed or
 *   unknown.
 * @property gitHead Commit id of the git HEAD when the step was checked (from `check.result`);
 *   `null` if the workspace is not a git repository or the step was not checked yet.
 * @property files Files the call refers to (`tool.files`); `null` before that event.
 * @property stored Set for a step taken from calu's record: it has no diff and no duration,
 *   `input` is `{}` if the record holds none, and `result` is only the beginning of what the
 *   tool returned.
 */
@Serializable
data class ToolView(
    val toolUseId: String,
    val agentKey: String,
    val name: String,
    val input: JsonObject = EMPTY_OBJECT,
    val check: ToolCheck? = null,
    val honesty: HonestyResultView? = null,
    val result: ToolResultView? = null,
    val diffPath: String? = null,
    val editRange: EditRange? = null,
    val gitHead: String? = null,
    val files: List<ToolFile>? = null,
    val stored: StoredStepInfo? = null,
)

/**
 * A chat message waiting to be taken in by Claude (shown above the input).
 *
 * @property id calu's id of the queued message.
 * @property text The message text.
 * @property delivery `Delivery.QUEUED` or `Delivery.INJECT`.
 * @property to "main" or the subagent's id.
 */
@Serializable
data class QueuedView(val id: String, val text: String, val delivery: String, val to: String)

/**
 * One offered answer of a question.
 *
 * @property label The answer's text.
 * @property description An explanation; "" if none.
 */
@Serializable
data class QuestionOption(val label: String, val description: String)

/**
 * One question of the agent inside an escalation (`AskUserQuestion`).
 *
 * @property question The question text.
 * @property header A short title; "" if none.
 * @property options The offered answers; the user may also write another one.
 * @property multiSelect True if several options may be chosen.
 */
@Serializable
data class QuestionView(
    val question: String,
    val header: String,
    val options: List<QuestionOption>,
    val multiSelect: Boolean,
)

/**
 * An escalated tool call.
 *
 * `status` is "open" until `escalation.resolved` arrives; a closed escalation is never reopened.
 *
 * @property key The map key in `ViewState.escalations`; unique within the state.
 * @property escalationId calu's id of the escalation.
 * @property toolUseId The id of the escalated tool call; `null` if calu gave none.
 * @property agentKey The message list that shows the card.
 * @property toolName The tool's name.
 * @property toolInput The tool input exactly as calu sent it (strings cut, see `boundInput`).
 * @property reason Why the step was escalated.
 * @property rendered Markdown rendering of the step by calu (display aid only; see
 *   `literalToolInput`).
 * @property questions Questions of the agent; empty for every other escalation. With questions
 *   the card shows the options and an answer field, and the user answers with `decide {answer}`.
 * @property openedAt ISO time the step began to wait; `null` if unknown.
 * @property status One of `EscalationStatus`.
 * @property pending True after the user decided, until `escalation.resolved` or
 *   `ui.decisionFailed`; `null` before the first decision.
 * @property answer The answer text the user sent for the questions (kept after the card closed).
 * @property note The note of the decision; `null` if none.
 */
@Serializable
data class EscalationView(
    val key: String,
    val escalationId: String,
    val toolUseId: String?,
    val agentKey: String,
    val toolName: String,
    val toolInput: JsonObject,
    val reason: String,
    val rendered: String,
    val questions: List<QuestionView>,
    val openedAt: String?,
    val status: String,
    val pending: Boolean? = null,
    val answer: String? = null,
    val note: String? = null,
)

/**
 * An image attached to a user message.
 *
 * @property mediaType The image's media type, e.g. "image/png".
 * @property data The image file, base64; "" if it was larger than `MAX_IMAGE_DATA`.
 * @property omitted True if `data` was dropped because of its size.
 */
@Serializable
data class ImageView(val mediaType: String, val data: String, val omitted: Boolean)

// ------------------------------------------------------------------ items

/** One entry of a message list. */
@Serializable
@JsonClassDiscriminator("kind")
sealed interface Item {
    /** The item's id; unique within a `ViewState`. */
    val id: Int

    /** One of `ItemKind`; the same text is the class discriminator "kind" in JSON. */
    val kind: String
}

/** An item that can be streamed with `assistant.delta`: Claude's text or thinking. */
@Serializable
sealed interface StreamItem : Item {
    /** The text so far (Markdown). */
    val text: String

    /** Id of the message in calu (from `assistant.delta` / `assistant.text`); `null` if not given. */
    val messageId: String?

    /** True while `assistant.delta` fragments arrive and the final text is due. */
    val streaming: Boolean?

    /**
     * True if an attach found this block still streaming: the replay ended in the middle of it,
     * so later fragments and the final text of the same message continue this item.
     */
    val cut: Boolean?

    /**
     * Return a copy with another text and streaming flag and without the `cut` mark.
     *
     * @param text The new text.
     * @param streaming The new `streaming` flag.
     * @return The copy (same id, kind and message id; `cut` is `null`).
     */
    fun continued(text: String, streaming: Boolean): StreamItem

    /**
     * Return a copy that no longer streams.
     *
     * @param cut True to mark the copy `cut` (see `cut`); false keeps the present mark.
     * @return The copy with `streaming` false.
     */
    fun finished(cut: Boolean): StreamItem
}

/**
 * A message that entered the conversation as user input.
 *
 * @property text The message text.
 * @property source One of `UserSource`.
 * @property images The attached images.
 * @property turn Number of the turn the message belongs to: `turnBase + turnCount + 1` when the
 *   message arrived, which is the conversation's own number once the recorded history was read
 *   (`ViewState.turnBase`), and a count over the turns this view saw before that. For
 *   `rewindBefore` / `forkBefore`; the host prefers the turn it finds for `uuid` in the recorded
 *   history.
 * @property uuid Claude Code's id of the message (`user.input`.uuid); `null` if it gave none.
 * @property to Set in a subagent's tab for a message the user sent to that background subagent
 *   (`user.input`.to): the subagent's id. The main agent relays such a message, so the turn
 *   belongs to Main and the message has no rewind or fork action.
 * @property stored True for a message taken from calu's record (`ui.history`), see
 *   `ToolView.stored`.
 */
@Serializable
@SerialName(ItemKind.USER)
data class UserItem(
    override val id: Int,
    val text: String,
    val source: String,
    val images: List<ImageView> = emptyList(),
    val turn: Int,
    val uuid: String? = null,
    val to: String? = null,
    val stored: Boolean? = null,
) : Item {
    override val kind: String get() = ItemKind.USER
}

/**
 * A block of Claude's text.
 *
 * @property text The text (Markdown).
 * @property stored True for the recorded answer of an earlier turn (`ui.history`).
 */
@Serializable
@SerialName(ItemKind.ASSISTANT)
data class AssistantItem(
    override val id: Int,
    override val text: String,
    override val messageId: String? = null,
    override val streaming: Boolean? = null,
    override val cut: Boolean? = null,
    val stored: Boolean? = null,
) : StreamItem {
    override val kind: String get() = ItemKind.ASSISTANT

    override fun continued(text: String, streaming: Boolean): AssistantItem =
        copy(text = text, streaming = streaming, cut = null)

    override fun finished(cut: Boolean): AssistantItem =
        copy(streaming = false, cut = if (cut) true else this.cut)
}

/**
 * A block of Claude's (summarized) thinking.
 *
 * @property text The thinking (Markdown).
 */
@Serializable
@SerialName(ItemKind.THINKING)
data class ThinkingItem(
    override val id: Int,
    override val text: String,
    override val messageId: String? = null,
    override val streaming: Boolean? = null,
    override val cut: Boolean? = null,
) : StreamItem {
    override val kind: String get() = ItemKind.THINKING

    override fun continued(text: String, streaming: Boolean): ThinkingItem =
        copy(text = text, streaming = streaming, cut = null)

    override fun finished(cut: Boolean): ThinkingItem =
        copy(streaming = false, cut = if (cut) true else this.cut)
}

/**
 * A tool call; the details are in `ViewState.tools`.
 *
 * @property toolUseId The key of the call's `ToolView` in `ViewState.tools`.
 */
@Serializable
@SerialName(ItemKind.TOOL)
data class ToolItem(override val id: Int, val toolUseId: String) : Item {
    override val kind: String get() = ItemKind.TOOL
}

/**
 * A message of a checker (a remark, or the reason of a DENY).
 *
 * @property label Display name of the checker, see `checkerLabel`.
 * @property checkerRoute Route for `editChecker {route}`; "" for a rule's decision.
 * @property decision The checker's decision ("PASS", "ALLOW", "DENY" or "ESCALATE").
 * @property text The checker's remark (`comment`) or, for DENY, its `reason`.
 * @property toolUseId The judged tool call; `null` if the event named none.
 */
@Serializable
@SerialName(ItemKind.CHECKER)
data class CheckerItem(
    override val id: Int,
    val label: String,
    val checkerRoute: String,
    val decision: String,
    val text: String,
    val toolUseId: String?,
) : Item {
    override val kind: String get() = ItemKind.CHECKER
}

/**
 * An escalation card; the details are in `ViewState.escalations`.
 *
 * @property escalationKey The key of the `EscalationView` in `ViewState.escalations`.
 */
@Serializable
@SerialName(ItemKind.ESCALATION)
data class EscalationItem(override val id: Int, val escalationKey: String) : Item {
    override val kind: String get() = ItemKind.ESCALATION
}

/**
 * The end of a turn.
 *
 * @property subtype The result's subtype, e.g. "success".
 * @property isError True if the turn ended with an error.
 * @property costUsd What Claude Code reported at the end of the turn: the cost of the whole
 *   session so far; `null` if not reported.
 * @property backgroundTasks Background tasks (subagents, shells) still running when the turn ended.
 * @property stored True for the end of a recorded turn (`ui.history`): outcome and cost are not
 *   recorded.
 * @property costDelta The cost of this turn alone: `costUsd` minus the session's cost before the
 *   turn. `null` when that earlier cost is not known (the first turn seen) or was higher (Claude
 *   Code was started again and counts from zero).
 */
@Serializable
@SerialName(ItemKind.TURN)
data class TurnItem(
    override val id: Int,
    val subtype: String,
    val isError: Boolean,
    val costUsd: Double?,
    val backgroundTasks: Int,
    val stored: Boolean? = null,
    val costDelta: Double? = null,
) : Item {
    override val kind: String get() = ItemKind.TURN
}

/**
 * The instructions a subagent was launched with (first item of the subagent's list).
 *
 * @property toolUseId The Agent/Task tool call that started the subagent (= the subagent's
 *   agent_id).
 * @property description The short task description ("" if none).
 * @property subagentType The subagent type; `null` if none.
 * @property background True if it was launched with `run_in_background`.
 * @property prompt The full launch prompt (Markdown); not cut.
 */
@Serializable
@SerialName(ItemKind.LAUNCH)
data class LaunchItem(
    override val id: Int,
    val toolUseId: String,
    val description: String,
    val subagentType: String?,
    val background: Boolean,
    val prompt: String,
) : Item {
    override val kind: String get() = ItemKind.LAUNCH
}

/**
 * A message of calu or of the plugin for the user.
 *
 * @property level "info", "warning" or "error".
 * @property text The message.
 * @property key Names what the message is about (e.g. "sync-conflict:src/a.py"), so that a later
 *   `ui.noticeResolved` can mark it; `null` for a message that is never resolved.
 * @property resolved Whether what the message reported was settled since (a sync conflict that
 *   was merged). The line stays in the transcript and is drawn without the warning colour.
 */
@Serializable
@SerialName(ItemKind.NOTICE)
data class NoticeItem(
    override val id: Int,
    val level: String,
    val text: String,
    val key: String? = null,
    val resolved: Boolean = false,
) : Item {
    override val kind: String get() = ItemKind.NOTICE
}

/**
 * The note in Main that the user sent a message to a background subagent.
 *
 * @property to The background subagent the user's message was sent to (its tab holds the text).
 */
@Serializable
@SerialName(ItemKind.RELAY)
data class RelayItem(override val id: Int, val to: String) : Item {
    override val kind: String get() = ItemKind.RELAY
}

/**
 * A line between the recorded turns and the session's own events.
 *
 * @property mark One of `DividerMark`.
 * @property text The text shown on the line.
 */
@Serializable
@SerialName(ItemKind.DIVIDER)
data class DividerItem(override val id: Int, val mark: String, val text: String) : Item {
    override val kind: String get() = ItemKind.DIVIDER
}

// ------------------------------------------------------------------ the rest of the state

/**
 * What the launching call of a subagent returned.
 *
 * @property content The result text, cut to `MAX_AGENT_RESULT_TEXT`.
 * @property isError True if the call failed.
 * @property final True for a foreground subagent (the text is its final answer); false for a
 *   background subagent (the text is only the launch acknowledgement; its answer reaches the main
 *   agent later as a "system" user input).
 */
@Serializable
data class SubagentResult(val content: String, val isError: Boolean, val final: Boolean)

/**
 * What is known about one subagent beyond `AgentInfo`: the task it was given (from the input of
 * the Agent/Task `tool.use` that launched it) and what it returned (that call's `tool.result`).
 *
 * @property agentId The subagent's id (= the launching tool call's id, = its key in `items`).
 * @property description The short task description ("" if none).
 * @property subagentType The subagent type; `null` if none.
 * @property background True if it was launched with `run_in_background`.
 * @property task The launch prompt (Markdown); not cut.
 * @property result The launching call's result; `null` before it arrived.
 */
@Serializable
data class SubagentView(
    val agentId: String,
    val description: String,
    val subagentType: String?,
    val background: Boolean,
    val task: String,
    val result: SubagentResult? = null,
)

/**
 * One running session of this machine, for the session dropdown.
 *
 * @property id calu's session id.
 * @property name Name given by the user; "" if none.
 * @property directory The directory the session works in.
 * @property state Session state as in `session.state`.
 * @property openEscalations Number of steps that wait for a decision.
 * @property here True if the session works in this workspace folder (or an ancestor of it).
 * @property stale True if the session's process seems to be gone.
 */
@Serializable
data class SessionEntry(
    val id: String,
    val name: String,
    val directory: String,
    val state: String,
    val openEscalations: Int,
    val here: Boolean,
    val stale: Boolean,
)

/**
 * One side question (`side.ask`) with its answer; never part of the conversation.
 *
 * @property id The plugin's number of the question.
 * @property question The question text.
 * @property answer Markdown answer; `null` while the request runs.
 * @property error Why the question was not answered; `null` if it was or while the request runs.
 */
@Serializable
data class SideExchangeView(
    val id: Int,
    val question: String,
    val answer: String? = null,
    val error: String? = null,
)

/**
 * Something the user should do before the chat can work, with the button that does it.
 *
 * @property id Stable id: "calu-missing", "claude-missing", "locked", "database", "no-server",
 *   "no-session".
 * @property level "info", "warning" or "error".
 * @property message One or two sentences that say what is missing and what to do.
 * @property action Names a view action without fields ("install", "startServer", "newSession",
 *   "attach", "openSettings", "restartBridge", "unlock"), or is "openLink" with `href`, or "" for
 *   a hint without a button.
 * @property actionLabel Button text; "" if `action` is "".
 * @property href For "openLink": the https address.
 * @property short A few words for the one-line form of the hint (shown while a session is
 *   attached); the whole `message` is then the tooltip. `null`: the message itself is cut.
 * @property actionShort Button text of the one-line form; `null`: `actionLabel`.
 */
@Serializable
data class SetupHint(
    val id: String,
    val level: String = "info",
    val message: String,
    val action: String = "",
    val actionLabel: String = "",
    val href: String? = null,
    val short: String? = null,
    val actionShort: String? = null,
)

/**
 * The file synchronisation of the SSH mode, as the chat and the status bar show it.
 *
 * @property phase "connecting", "refused", "waiting", "syncing", "idle", "offline" or "failed"
 *   (the lower-case names of `SyncPhase`).
 * @property text The status bar's line, e.g. "calu sync: in sync".
 * @property message Detail of the phase (why refused or failed, what waits); `null` if none.
 * @property conflicts Relative paths that need the user's decision.
 * @property held Relative paths whose local change waits for the agent's turn to end.
 * @property notes Paths that are not synchronised, each as "path: reason".
 * @property queued Number of queued local changes.
 */
@Serializable
data class SyncView(
    val phase: String,
    val text: String,
    val message: String? = null,
    val conflicts: List<String> = emptyList(),
    val held: List<String> = emptyList(),
    val notes: List<String> = emptyList(),
    val queued: Int = 0,
)

/**
 * The offer to set up running and debugging on the server of the SSH mode: PyCharm's SSH
 * interpreter with the path mapping local copy <-> server folder.
 *
 * @property host The SSH host as the plugin's settings name it.
 * @property directory The project directory on the server.
 * @property phase "offer" (nothing done yet), "working" (the path is guessed or the interpreter
 *   is being created) or "failed" (`message` says why; the offer stands again).
 * @property message What went wrong, for "failed"; `null` otherwise.
 */
@Serializable
data class InterpreterOfferView(val host: String, val directory: String, val phase: String, val message: String? = null)

/**
 * The context window of the main agent.
 *
 * @property tokens Tokens in the context.
 * @property maxTokens Size of the context window.
 * @property percent Used share in percent.
 * @property model The model the numbers are for ("" if not given).
 */
@Serializable
data class ContextView(val tokens: Int, val maxTokens: Int, val percent: Double, val model: String)

/**
 * Cost, context window and account limits of the attached session.
 *
 * @property rateLimits One entry per limit Claude Code reported (the newest state of each).
 * @property costUsd Cost of the last finished turn's session total, as `turn.result` reported it.
 * @property numTurns Number of turns as `turn.result` or `usage.get` reported it.
 * @property context Context window of the main agent (from `usage.get`); `null` until asked.
 */
@Serializable
data class UsageView(
    val rateLimits: List<RateLimit> = emptyList(),
    val costUsd: Double? = null,
    val numTurns: Int = 0,
    val context: ContextView? = null,
)

/**
 * The bridge process.
 *
 * @property status One of `BridgeStatus` ("" if a `bridge.status` event carried no status text).
 * @property message A text for the user about the status; `null` if none.
 * @property version calu's version once the bridge runs; `null` before.
 */
@Serializable
data class BridgeView(val status: String, val message: String? = null, val version: String? = null)

/**
 * The connection between the bridge and the attached session.
 *
 * @property status One of `LinkStatus`.
 * @property message A text for the user about the connection; `null` if none.
 */
@Serializable
data class LinkView(val status: String, val message: String? = null)

/**
 * The attached session. All `null`/"stopped" while `link.status` is "detached"; after a detach
 * the transcript of the last session stays visible until the next attach.
 *
 * @property state The session state as the protocol names it (`session.state`), e.g. "idle",
 *   "running", "escalated", "stopped", "failed".
 * @property sessionId calu's session id; `null` if none is attached.
 * @property claudeSessionId Claude Code's session id; `null` if unknown.
 * @property workspace The directory the session works in.
 * @property project The session's project in calu; `null` if unknown.
 * @property model Claude Code model (alias or id); `null` = Claude Code's default.
 * @property effort Effort the session started with; `null` = Claude Code's default.
 * @property backgroundTasks Background tasks still running at the end of the last turn.
 */
@Serializable
data class SessionView(
    val state: String = "stopped",
    val sessionId: String? = null,
    val claudeSessionId: String? = null,
    val workspace: String? = null,
    val project: String? = null,
    val model: String? = null,
    val effort: String? = null,
    val backgroundTasks: Int = 0,
)

/**
 * The running sessions of this machine (for the dropdown).
 *
 * @property list The sessions, sessions of the workspace first.
 * @property attachedId Id of the attached session; `null` if none. Equal to `session.sessionId`.
 * @property error Why the list could not be loaded; `null` if it could.
 * @property loaded True after the first answer of `ide.sessions` (an empty list then means
 *   "none run").
 */
@Serializable
data class SessionsView(
    val list: List<SessionEntry> = emptyList(),
    val attachedId: String? = null,
    val error: String? = null,
    val loaded: Boolean = false,
)

/**
 * A session request that waits for the calu server (`ide.start`).
 *
 * @property title What is being started, for the user.
 * @property slow Set after a few seconds without an answer: no server may be running.
 */
@Serializable
data class StartingView(val title: String, val slow: Boolean)

/**
 * Request to show a tab.
 *
 * @property tab The message list key of the tab.
 * @property seq Increases with every request; a view switches tabs only for a `seq` it has not
 *   handled yet.
 */
@Serializable
data class FocusRequest(val tab: String, val seq: Int)

/**
 * The complete chat view state (JSON-serialisable with `CaluJson`).
 *
 * @property bridge The bridge process.
 * @property link The connection between the bridge and the attached session.
 * @property session The attached session.
 * @property sessions The running sessions of this machine.
 * @property serverHelp How to start `calu serve` (set when no server answered, or on request);
 *   `null` otherwise.
 * @property hints What is missing for the chat to work, most important first.
 * @property autoEscalate The session's auto-escalation switches (the supervision level, see
 *   `Levels.kt`).
 * @property honesty Code honesty state of the session; `null` before the first report.
 * @property honestyStored `honesty.mode` as calu's settings store it for the session's project
 *   ("off", "track", "enforce"); `null` if not known. A session reads a changed mode by itself and
 *   reports it with `honesty.update`; until then (or if it never does) the header shows both.
 * @property starting A session request that waits for the calu server; `null` otherwise.
 * @property usage Cost, context window and account limits.
 * @property focus Request to show a tab: set when an escalation arrives, so the view can switch
 *   to the tab that contains the card. The replay at attach keeps the previous value.
 * @property queue Chat messages sent but not taken in by Claude yet (`chat.queue`), oldest first.
 * @property agents All subagents of the session, in the order of the last `agents.update`.
 * @property subagents Task and result per subagent, keyed by `agent_id` (an entry exists once
 *   its launch was seen).
 * @property items Message lists per agent; always contains `MAIN`.
 * @property tools The tool calls, keyed by `tool_use_id`.
 * @property escalations The escalations, keyed by `EscalationView.key`.
 * @property side Side questions of this client, oldest first.
 * @property turnCount Number of `turn.result` events applied since the transcript was last cleared.
 * @property turnBase Number of turns the conversation had before the first turn of the replayed
 *   events; `null` until the recorded history was read after an attach (`ui.history`). While it
 *   is known, a user message with `turn` 1 is the first message of the conversation: nothing is
 *   before it, so the view offers no rewind or fork there.
 * @property lastSeq Sequence number of the last session event applied (0 before the first).
 * @property sync The file synchronisation of the SSH mode; `null` in every other mode and with
 *   "Sync: none".
 * @property interpreter The offer to create PyCharm's SSH interpreter for the server of the SSH
 *   mode; `null` when there is nothing to offer (another mode, not connected, a matching
 *   interpreter exists, or no Python support in the IDE).
 * @property nextId Next free item id.
 */
@Serializable
data class ViewState(
    val bridge: BridgeView = BridgeView(BridgeStatus.STOPPED),
    val link: LinkView = LinkView(LinkStatus.DETACHED),
    val session: SessionView = SessionView(),
    val sessions: SessionsView = SessionsView(),
    val serverHelp: ServerHelp? = null,
    val hints: List<SetupHint> = emptyList(),
    val autoEscalate: AutoEscalateFlags = AutoEscalateFlags(),
    val honesty: HonestyInfo? = null,
    val honestyStored: String? = null,
    val starting: StartingView? = null,
    val usage: UsageView = UsageView(),
    val focus: FocusRequest? = null,
    val queue: List<QueuedView> = emptyList(),
    val agents: List<AgentInfo> = emptyList(),
    val subagents: Map<String, SubagentView> = emptyMap(),
    val items: Map<String, List<Item>> = mapOf(MAIN to emptyList()),
    val tools: Map<String, ToolView> = emptyMap(),
    val escalations: Map<String, EscalationView> = emptyMap(),
    val side: List<SideExchangeView> = emptyList(),
    val sync: SyncView? = null,
    val interpreter: InterpreterOfferView? = null,
    val turnCount: Int = 0,
    val turnBase: Int? = null,
    val lastSeq: Int = 0,
    val nextId: Int = 1,
)

/**
 * An input of the reducer: a session event (`method` from the protocol's `EventParams`, relayed
 * by the bridge as `session.event`) or one of the plugin's own events:
 *
 * - `bridge.status` `{status: BridgeStatus, message?, version?}`: the bridge process changed
 *   state;
 * - `ui.link` `{state: "attached" | "reconnecting" | "lost", message}`: the bridge's `ide.link`;
 * - `ui.attached` `{session_id, initialize, snapshot, events, complete, resumed?}`: the result
 *   of `ide.attach` / `ide.start`; replaces the transcript, or with `resumed` continues it
 *   (see `applyAttach`);
 * - `ui.history` `{session_id, history}`: the result of `session.history` for the attached
 *   session; the recorded turns that the replay does not cover are put above the transcript
 *   (see `applyHistory`);
 * - `ui.honestyStored` `{mode}`: `honesty.mode` as calu's settings store it (`null` = unknown);
 * - `ui.starting` `{title, slow}` or `{}`: a session request waits for the server / is over;
 * - `ui.detached` `{}`: the session was detached; the transcript stays, nothing is open;
 * - `ui.sessions` `{running: RunningSession[], error?}`: the answer of `ide.sessions`;
 * - `ui.serverHelp` `{command, tool, detach, note}` or `{}`: set or clear `serverHelp`;
 * - `ui.hints` `{hints: SetupHint[]}`: replace `hints`;
 * - `ui.model` `{model?: string | null, effort?: string | null}`: the model or effort changed;
 * - `ui.usage` `{context, rate_limits, cost_usd, num_turns}`: the result of `usage.get`;
 * - `ui.notice` `{level: "info" | "warning" | "error", text, key?}`: a message for the user (e.g. a
 *   failed request) shown in the main list;
 * - `ui.noticeResolved` `{key}`: mark every unresolved notice of the main list with this key as
 *   resolved (it stays in the list);
 * - `ui.clear` `{}`: drop messages, tool calls, escalations, subagent tabs, queued messages and
 *   side questions;
 * - `ui.decisionPending` `{escalation_key, answer?}` / `ui.decisionFailed` `{escalation_key}`:
 *   the user's decision was sent / could not be sent;
 * - `ui.pickSession` `{}`: `ide.attach` without a session found several (-32012); changes no
 *   state, the surface opens its session list;
 * - `ui.sync` `{phase, text, message?, conflicts, held, notes, queued}` or `{}`: the file
 *   synchronisation's status changed / is off (see `SyncView`);
 * - `ui.interpreter` `{host, directory, phase, message?}` or `{}`: set or clear the offer of an
 *   SSH interpreter (see `InterpreterOfferView`);
 * - `ui.sideAsk` `{id, question}` / `ui.sideAnswer` `{id, answer}` or `{id, error}`: a side
 *   question was sent / answered.
 *
 * @property method The event's method.
 * @property params The event's params (members untrusted).
 */
@Serializable
data class ViewEvent(val method: String, val params: JsonObject = EMPTY_OBJECT)

/**
 * Create the empty state shown before the bridge has started.
 *
 * @return A fresh state: bridge "stopped", link "detached", no session, an empty main list.
 */
fun initialViewState(): ViewState = ViewState()
