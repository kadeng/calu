/**
 * The inline markup of calu's descriptions (pure), for the settings dialog.
 *
 * calu writes its setting descriptions and setup advice as plain sentences with two marks:
 *
 *     `claude.model`      a setting key, a command or a value   -> code
 *     **never**           emphasis                              -> strong
 *
 * `inlineParts` splits a text into parts; a dialog builds one styled run per part and never
 * passes the text to an HTML parser, so a description cannot inject markup.
 *
 * Port of `extensions/vscode/src/shared/inlineText.ts`.
 */
package io.github.kadeng.calu.logic

/** Kind of an `InlinePart`. */
enum class InlineKind {
    /** Plain text. */
    TEXT,

    /** Was between backticks. */
    CODE,

    /** Was between `**`. */
    STRONG,
}

/**
 * One part of a text.
 *
 * @property kind Plain, code or strong.
 * @property text The part's text without its marks. Never empty.
 */
data class InlinePart(val kind: InlineKind, val text: String)

private val INLINE_PATTERN = Regex("`([^`\\n]+)`|\\*\\*([^\\s*](?:[^*\\n]*[^\\s*])?)\\*\\*")

/**
 * Split a text at its inline marks.
 *
 *     "Set `a.b` to **true** now"  ->  text "Set ", code "a.b", text " to ", strong "true", text " now"
 *
 * Rules: a code span runs from a backtick to the next backtick on the same line and may contain
 * anything, also asterisks. A strong span runs from `**` to the next `**` on the same line; its
 * content must not start or end with a space and is not split further. A mark without its
 * partner, and an empty span, stay as they are written. Nothing nests.
 *
 * @param text Any text (untrusted).
 * @return The parts in order; their texts joined give `text` without the marks of the
 *   recognised spans. Empty for "". Neighbouring plain parts are joined into one.
 */
fun inlineParts(text: String): List<InlinePart> {
    val parts = ArrayList<InlinePart>()
    fun push(kind: InlineKind, value: String) {
        if (value.isEmpty()) return
        val last = parts.lastOrNull()
        if (kind == InlineKind.TEXT && last?.kind == InlineKind.TEXT) parts[parts.size - 1] = InlinePart(InlineKind.TEXT, last.text + value)
        else parts.add(InlinePart(kind, value))
    }
    var at = 0
    for (m in INLINE_PATTERN.findAll(text)) {
        push(InlineKind.TEXT, text.substring(at, m.range.first))
        val code = m.groups[1]
        if (code != null) push(InlineKind.CODE, code.value) else push(InlineKind.STRONG, m.groups[2]?.value.orEmpty())
        at = m.range.last + 1
    }
    push(InlineKind.TEXT, text.substring(at))
    return parts
}

/**
 * Return a text without its inline marks (what a search or a tooltip should see).
 *
 * @param text Any text.
 * @return The texts of `inlineParts(text)` joined.
 */
fun plainInline(text: String): String = inlineParts(text).joinToString("") { it.text }
