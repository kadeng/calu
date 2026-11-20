/**
 * The questions the synchronisation of the SSH mode asks the user, as data: text, buttons,
 * which button is the default (Enter) and which one has the keyboard focus. The backend shows
 * them with a dialog of its own (`ChoiceDialog`); keeping them here lets unit tests check the
 * rule that matters:
 *
 *     a button that can lose a file or a change   never the default, never focused
 *     a question where every answer loses data    no default at all; the focus is on Cancel
 *
 *     conflict without a text merge (`sideChoice`)   Keep Mine | Take Theirs | Cancel      default: none
 *     plan of a first synchronisation (`planQuestion`) Apply | Not Now                       default: Apply unless it deletes
 *     run or debug while files are held (`heldRunQuestion`) Upload now and run | Run anyway | Cancel  default: Upload now and run
 */
package io.github.kadeng.calu.sync

/**
 * A question with buttons.
 *
 * Invariants: `options` is not empty; `defaultIndex` (if set), `focusIndex` and `cancelIndex`
 * are indexes of `options`.
 *
 * @property title The dialog's title.
 * @property message The text above the buttons (may contain line breaks).
 * @property options The button labels, left to right.
 * @property defaultIndex The button that Enter presses wherever the focus is; `null` for none.
 * @property focusIndex The button that has the keyboard focus when the dialog opens.
 * @property cancelIndex The button that Escape and closing the window stand for.
 */
data class Question(
    val title: String,
    val message: String,
    val options: List<String>,
    val defaultIndex: Int?,
    val focusIndex: Int,
    val cancelIndex: Int,
) {
    init {
        require(options.isNotEmpty()) { "a question needs buttons" }
        require(defaultIndex == null || defaultIndex in options.indices) { "default button out of range: $defaultIndex" }
        require(focusIndex in options.indices) { "focused button out of range: $focusIndex" }
        require(cancelIndex in options.indices) { "cancel button out of range: $cancelIndex" }
    }
}

/** Button of `sideChoice`: the local content (or deletion) goes to the server. */
const val KEEP_MINE: Int = 0

/** Button of `sideChoice`: the server's content (or deletion) comes to this machine. */
const val TAKE_THEIRS: Int = 1

/** Button of `sideChoice`: nothing changes; the conflict stays open. */
const val SIDE_CANCEL: Int = 2

/**
 * The question for a conflict that the three-way merge dialog cannot show: a change against a
 * deletion (either direction), a binary file, a file above the merge limit.
 *
 * Each of "Keep Mine" and "Take Theirs" replaces or deletes the other side's version, so the
 * question has no default button and the focus is on "Cancel": Enter decides nothing.
 *
 * Example: `sideChoice("a.py", "changed on the server, deleted here", mineExists = false,
 * theirsExists = true)` -> "Keep Mine: the server gets the deletion." / "Take Theirs: this
 * machine gets the server's file."
 *
 * @param path The conflicted file, relative to the project.
 * @param what What happened, in words ("changed on the server, deleted here").
 * @param mineExists True if the file exists on this machine.
 * @param theirsExists True if the file exists on the server.
 * @return The question; its buttons are `KEEP_MINE`, `TAKE_THEIRS`, `SIDE_CANCEL`.
 */
fun sideChoice(path: String, what: String, mineExists: Boolean, theirsExists: Boolean): Question = Question(
    title = "calu Sync Conflict",
    message = "$path: $what.\n\n" +
        "Keep Mine: the server gets ${if (mineExists) "this machine's file" else "the deletion"}.\n" +
        "Take Theirs: this machine gets ${if (theirsExists) "the server's file" else "the deletion"}.",
    options = listOf("Keep Mine", "Take Theirs", "Cancel"),
    defaultIndex = null,
    focusIndex = SIDE_CANCEL,
    cancelIndex = SIDE_CANCEL,
)

/** Button of `planQuestion`: carry the plan out. */
const val PLAN_APPLY: Int = 0

/** Button of `planQuestion`: leave both sides as they are. */
const val PLAN_NOT_NOW: Int = 1

/**
 * The question before a plan is carried out (the first synchronisation of a copy, or a plan
 * that deletes many files).
 *
 * A plan that deletes files on either side has no default button and the focus on "Not Now":
 * Enter must not delete. A plan without deletions (a clone into an empty folder, downloads and
 * uploads of files the other side lacks) keeps "Apply" (or "Clone from Server") as the default.
 *
 * @param summary What the plan would do.
 * @return The question; its buttons are `PLAN_APPLY` and `PLAN_NOT_NOW`.
 */
fun planQuestion(summary: PlanSummary): Question {
    val deletes = summary.deleteLocal.isNotEmpty() || summary.deleteRemote.isNotEmpty()
    return Question(
        title = "calu Sync",
        message = summary.text(),
        options = listOf(if (summary.isClone) "Clone from Server" else "Apply", "Not Now"),
        defaultIndex = if (deletes) null else PLAN_APPLY,
        focusIndex = if (deletes) PLAN_NOT_NOW else PLAN_APPLY,
        cancelIndex = PLAN_NOT_NOW,
    )
}

/** What the user chose when a run or debug configuration started while files were held. */
enum class HeldRunChoice {
    /** Upload the held files now (although the agent edits them), then start. */
    UPLOAD_AND_RUN,

    /** Start at once; the server runs the version it has. */
    RUN_ANYWAY,

    /** Do not start. */
    CANCEL,
}

/**
 * The question shown when a run or debug configuration starts while local saves are held
 * until Claude's turn ends (the server still has the old content of those files).
 *
 * "Upload now and run" is the default: it loses nothing (an upload that would overwrite a
 * change of the agent is refused by the synchronisation's base check and becomes a conflict).
 *
 * @param held The held files, relative to the project; must not be empty.
 * @return The question; map its answer with `heldRunChoice`.
 * @throws IllegalArgumentException if `held` is empty.
 */
fun heldRunQuestion(held: List<String>): Question {
    require(held.isNotEmpty()) { "no file is held" }
    val n = held.size
    val head = if (n == 1) "1 file is held" else "$n files are held"
    val list = held.take(8).joinToString("\n") { "  $it" } + if (n > 8) "\n  ..." else ""
    return Question(
        title = "calu: Files Held",
        message = "$head until Claude's turn ends; the server still runs the old version.\n\n$list",
        options = listOf("Upload now and run", "Run anyway", "Cancel"),
        defaultIndex = 0,
        focusIndex = 0,
        cancelIndex = 2,
    )
}

/**
 * Map the answer to `heldRunQuestion` to a choice.
 *
 * @param index The pressed button's index, or any other number for a closed dialog.
 * @return `UPLOAD_AND_RUN` for 0, `RUN_ANYWAY` for 1, otherwise `CANCEL`.
 */
fun heldRunChoice(index: Int): HeldRunChoice = when (index) {
    0 -> HeldRunChoice.UPLOAD_AND_RUN
    1 -> HeldRunChoice.RUN_ANYWAY
    else -> HeldRunChoice.CANCEL
}

/**
 * The question shown when "Upload now and run" could not upload every held file (a conflict
 * with the agent's change, or the connection dropped).
 *
 * @param left The files that are still not on the server, relative to the project; not empty.
 * @return The question: "Run anyway" (0, not the default) or "Cancel" (1, focused).
 * @throws IllegalArgumentException if `left` is empty.
 */
fun notUploadedQuestion(left: List<String>): Question {
    require(left.isNotEmpty()) { "every file was uploaded" }
    val head = if (left.size == 1) "1 file was not uploaded" else "${left.size} files were not uploaded"
    return Question(
        title = "calu: Files Not Uploaded",
        message = "$head (a conflict with Claude's change, or no connection); the server still runs the old version of " +
            "${if (left.size == 1) "it" else "them"}.\n\n" + left.take(8).joinToString("\n") { "  $it" } + if (left.size > 8) "\n  ..." else "",
        options = listOf("Run anyway", "Cancel"),
        defaultIndex = null,
        focusIndex = 1,
        cancelIndex = 1,
    )
}
