/**
 * Bidirectional control characters in untrusted text (pure).
 *
 * Unicode has invisible characters that change the order in which the following text is drawn.
 * A command, a file name or a checker's reason that contains one can be displayed in another
 * order than it is stored and run in, so that the user approves something else than they read:
 *
 *     stored:   "rm -rf <RLO>gnp.tropxe"      drawn without this module:   "rm -rf export.png"
 *     shown by this module:                   "rm -rf ⟦RLO⟧gnp.tropxe"
 *
 * `neutraliseBidi` replaces each of them by a visible name in white square brackets. Letters of
 * right-to-left scripts (Arabic, Hebrew) are not touched: the text component orders them by the
 * Unicode algorithm as usual.
 *
 * A chat surface passes every text of the session through `neutraliseBidi` before it draws it:
 * transcript texts, tool inputs, commands, file names, reasons, tooltips and Markdown sources
 * (the VSCode extension does this in `shown()` of `webview/dom.ts`).
 *
 * Port of `extensions/vscode/src/shared/bidi.ts`.
 */
package io.github.kadeng.calu.logic

/** The replaced characters by code point, with the name that is shown for each. */
val BIDI_CONTROLS: Map<Int, String> = linkedMapOf(
    0x061c to "ALM",
    0x200e to "LRM",
    0x200f to "RLM",
    0x202a to "LRE",
    0x202b to "RLE",
    0x202c to "PDF",
    0x202d to "LRO",
    0x202e to "RLO",
    0x2066 to "LRI",
    0x2067 to "RLI",
    0x2068 to "FSI",
    0x2069 to "PDI",
)

/**
 * Tell whether a UTF-16 unit is one of the replaced characters.
 *
 * @param c The unit.
 * @return True for U+061C, U+200E, U+200F, U+202A to U+202E and U+2066 to U+2069.
 */
private fun isBidiControl(c: Char): Boolean =
    c == '؜' || c == '‎' || c == '‏' || c in '‪'..'‮' || c in '⁦'..'⁩'

/**
 * Replace every bidirectional control character of a text by a visible placeholder.
 *
 * Example: `neutraliseBidi("a‮b") == "a⟦RLO⟧b"`.
 *
 * @param text Any text (untrusted).
 * @return The text with U+061C, U+200E, U+200F, U+202A to U+202E and U+2066 to U+2069 each
 *   replaced by "⟦NAME⟧" (`BIDI_CONTROLS`); the same string object if it contains none.
 */
fun neutraliseBidi(text: String): String {
    if (!hasBidiControls(text)) return text
    val out = StringBuilder(text.length + 16)
    for (c in text) {
        if (isBidiControl(c)) out.append('⟦').append(BIDI_CONTROLS[c.code] ?: "?").append('⟧') else out.append(c)
    }
    return out.toString()
}

/**
 * Tell whether a text contains a bidirectional control character.
 *
 * @param text Any text.
 * @return True if `neutraliseBidi` would change it.
 */
fun hasBidiControls(text: String): Boolean = text.any(::isBidiControl)
