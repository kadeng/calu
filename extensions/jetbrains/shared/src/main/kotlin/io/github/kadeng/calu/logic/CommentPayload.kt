/**
 * Pure helpers for "Add comment for Claude" on a selection of an editor or of a diff:
 * selection -> 1-based line range, path checks, and the params of the `comment.add` request.
 *
 *     editor: (relative path, selection, the whole document)                 ──┐
 *     diff:   (relative path, selection, focused side, both whole documents) ──┼─ buildCommentParams ──► comment.add
 *     the user's comment                                                     ──┘
 *
 *     file comment   {kind: "file", path, side: "after", start_line, end_line, after_text, text}
 *     diff comment   {kind: "diff", path, side, start_line, end_line, before_text, after_text, ref?, text}
 *
 * The request carries the whole documents and the line range, not the selected excerpt: calu
 * cuts the excerpt and maps the selection to the other side of a diff (difflib). This module
 * does no diffing. The chat action is `act("comment", <these params>)` (`ChatActions.kt`).
 *
 * Port of `extensions/vscode/src/commentPayload.ts` (without the parsing of VSCode's `git:`
 * URIs) and of the prompt text of `comment.ts`.
 */
package io.github.kadeng.calu.logic

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * A position in a document.
 *
 * @property line 0-based line.
 * @property character 0-based column.
 */
data class TextPosition(val line: Int, val character: Int)

/**
 * A selection in 0-based coordinates.
 *
 * @property start Where the selection begins (the smaller position).
 * @property end Where the selection ends (exclusive).
 * @property active The cursor; its line is used for an empty selection.
 * @property isEmpty True if nothing is selected.
 */
data class PlainSelection(val start: TextPosition, val end: TextPosition, val active: TextPosition, val isEmpty: Boolean) {
    companion object {
        /**
         * Build a selection from 0-based (line, column) pairs, with the cursor at its end.
         *
         * @param startLine Line of the start.
         * @param startCharacter Column of the start.
         * @param endLine Line of the end.
         * @param endCharacter Column of the end.
         * @return The selection; empty if start and end are the same position.
         */
        fun of(startLine: Int, startCharacter: Int, endLine: Int, endCharacter: Int): PlainSelection = PlainSelection(
            TextPosition(startLine, startCharacter), TextPosition(endLine, endCharacter), TextPosition(endLine, endCharacter),
            startLine == endLine && startCharacter == endCharacter,
        )
    }
}

/**
 * A 1-based, inclusive line range. Invariant: 1 <= startLine <= endLine.
 *
 * @property startLine The first line.
 * @property endLine The last line.
 */
data class LineRange(val startLine: Int, val endLine: Int)

/**
 * Convert a selection to the 1-based inclusive line range sent to calu.
 *
 * Rules: an empty selection covers the cursor line. A non-empty selection that ends at column 0
 * of a later line does not include that line (whole-line selections made with Shift+Down).
 *
 * Example: start (4, 2), end (7, 0) -> `LineRange(5, 7)`.
 *
 * @param selection The selection (0-based lines and columns).
 * @return The line range; always at least one line.
 */
fun selectionToLines(selection: PlainSelection): LineRange {
    if (selection.isEmpty) return LineRange(selection.active.line + 1, selection.active.line + 1)
    var endLine0 = selection.end.line
    if (selection.end.character == 0 && selection.end.line > selection.start.line) endLine0 -= 1
    return LineRange(selection.start.line + 1, endLine0 + 1)
}

/**
 * Turn a git ref of a diff's left side into the `ref` value stored with a comment.
 *
 * @param ref The ref: "HEAD", a commit, "" (the staged version) or "~" (the index if staged,
 *   else HEAD).
 * @return "index" for "", "index or HEAD" for "~", else `ref` unchanged.
 */
fun describeGitRef(ref: String): String = when (ref) {
    "" -> "index"
    "~" -> "index or HEAD"
    else -> ref
}

/**
 * Return the `ref` stored with a comment on the diff of one of Claude's edits.
 *
 * @param toolUseId The edit's tool call.
 * @return "edit:<tool_use_id>".
 */
fun editRef(toolUseId: String): String = "edit:$toolUseId"

private val DRIVE_PREFIX = Regex("^[a-zA-Z]:/")

/**
 * Normalise a path relative to the project directory for calu.
 *
 * @param path A path relative to the project directory (any separator style).
 * @return The path with "/" separators and without a leading "./".
 * @throws IllegalArgumentException if the path is empty, absolute, or contains a ".." segment
 *   (it would leave the project directory).
 */
fun normaliseRelativePath(path: String): String {
    var p = path.replace('\\', '/')
    while (p.startsWith("./")) p = p.substring(2)
    require(p.isNotEmpty() && !p.startsWith("/") && !DRIVE_PREFIX.containsMatchIn(p) && ".." !in p.split("/")) {
        "The file is not inside the workspace folder: $path"
    }
    return p
}

/**
 * Compute the path of a file relative to a folder, both given as absolute file system paths.
 *
 * The comparison is textual after converting "\" to "/" and removing trailing "/" from the
 * folder; it is case-sensitive (Linux semantics).
 *
 * Example: ("/w/proj", "/w/proj/src/a.py") -> "src/a.py".
 *
 * @param folder Absolute path of the project directory of the session.
 * @param file Absolute path of the commented file.
 * @return The relative path with "/" separators (see `normaliseRelativePath`).
 * @throws IllegalArgumentException if `file` is not strictly inside `folder`, or the relative
 *   path contains "..".
 */
fun relativePathWithin(folder: String, file: String): String {
    val root = folder.replace('\\', '/').trimEnd('/')
    val target = file.replace('\\', '/')
    require(target.startsWith("$root/")) { "The file is not inside the workspace folder $folder: $file" }
    return normaliseRelativePath(target.substring(root.length + 1))
}

/**
 * The two sides of a diff editor in which a selection was made.
 *
 * @property side The side that has the selection: "before" (original, left) or "after"
 *   (modified, right).
 * @property beforeText The whole text of the original side.
 * @property afterText The whole text of the modified side.
 * @property ref What the original side is: "HEAD", "index", `editRef(toolUseId)`, ...; `null`
 *   if unknown.
 */
data class CommentDiff(val side: String, val beforeText: String, val afterText: String, val ref: String? = null)

/**
 * Everything the comment action collected from the editor.
 *
 * @property path Path of the commented file relative to the project directory, "/" separators
 *   (the modified side for diffs); see `relativePathWithin`.
 * @property selection The selection in the focused editor (0-based).
 * @property text The user's comment; must contain non-whitespace characters.
 * @property fileText For a normal editor: the whole text of the document; `null` for a diff.
 * @property diff For a diff editor: its two sides; `null` for a normal editor.
 */
data class CommentInput(
    val path: String,
    val selection: PlainSelection,
    val text: String,
    val fileText: String? = null,
    val diff: CommentDiff? = null,
)

/**
 * Build the params of a `comment.add` request.
 *
 * The line range refers to the focused document (for diffs: the document of `diff.side`).
 *
 * @param input The collected editor data.
 * @return The params: kind "file" (side "after", no `before_text`) or kind "diff"; `text` is
 *   the comment without white space at its ends; `ref` is left out if unknown.
 * @throws IllegalArgumentException if the comment is empty, if not exactly one of
 *   `input.fileText` and `input.diff` is set, or if `diff.side` is neither "before" nor "after".
 */
fun buildCommentParams(input: CommentInput): JsonObject {
    val text = input.text.trim()
    require(text.isNotEmpty()) { "The comment is empty." }
    require((input.fileText != null) != (input.diff != null)) { "buildCommentParams needs exactly one of `fileText` and `diff`." }
    val lines = selectionToLines(input.selection)
    val diff = input.diff
    require(diff == null || diff.side == "before" || diff.side == "after") { "The side of a diff is \"before\" or \"after\"." }
    return buildJsonObject {
        put("kind", JsonPrimitive(if (diff != null) "diff" else "file"))
        put("path", JsonPrimitive(input.path))
        put("side", JsonPrimitive(diff?.side ?: "after"))
        put("start_line", JsonPrimitive(lines.startLine))
        put("end_line", JsonPrimitive(lines.endLine))
        if (diff != null) put("before_text", JsonPrimitive(diff.beforeText))
        put("after_text", JsonPrimitive(diff?.afterText ?: input.fileText.orEmpty()))
        diff?.ref?.let { put("ref", JsonPrimitive(it)) }
        put("text", JsonPrimitive(text))
    }
}

/**
 * Describe the commented lines for the prompt that asks the comment's text.
 *
 * @param path The relative path.
 * @param selection The selection (0-based).
 * @param side The diff side ("before" or "after"), or `null` for a normal editor.
 * @return E.g. "src/a.py, lines 3-5 (before the change)" or "a.py, line 7".
 */
fun describeCommentTarget(path: String, selection: PlainSelection, side: String? = null): String {
    val range = selectionToLines(selection)
    val lines = if (range.startLine == range.endLine) "line ${range.startLine}" else "lines ${range.startLine}-${range.endLine}"
    val where = when (side) {
        "before" -> " (before the change)"
        "after" -> " (after the change)"
        else -> ""
    }
    return "$path, $lines$where"
}
