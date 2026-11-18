/**
 * A small Markdown to HTML converter for the transcript's Swing text panes (HTML 3.2 as
 * `javax.swing.text.html` renders it). Pure.
 *
 * Supported: paragraphs, headings (#..######), fenced code blocks, bullet and numbered lists
 * (one level), block quotes, horizontal rules; inline: `code`, **bold**, *italic*, [text](url)
 * with http, https and mailto addresses, and bare line breaks inside a paragraph. Everything else
 * is shown as the text it is. All text is HTML-escaped first, so no markup of the input reaches
 * the pane; raw HTML in the input is shown literally.
 */
package io.github.kadeng.calu.logic

private val FENCE_OPEN = Regex("^\\s{0,3}(`{3,}|~{3,})(.*)$")
private val HEADING_LINE = Regex("^(#{1,6})\\s+(.*)$")
private val BULLET = Regex("^\\s{0,6}[-*+]\\s+(.*)$")
private val NUMBERED = Regex("^\\s{0,6}\\d{1,9}[.)]\\s+(.*)$")
private val RULE = Regex("^\\s{0,3}([-*_])(\\s*\\1){2,}\\s*$")
private val CODE_SPAN = Regex("`([^`\n]+)`")
private val BOLD = Regex("\\*\\*([^*\n]+)\\*\\*")
private val ITALIC = Regex("(?<![*\\w])\\*([^*\n]+)\\*(?![*\\w])")
private val LINK = Regex("\\[([^\\]\n]+)]\\(((?:https?://|mailto:)[^\\s)\"<>]+)\\)")

/**
 * Escape a text for HTML.
 *
 * @param text Any text.
 * @return The text with `&`, `<`, `>` and `"` written as entities.
 */
fun escapeHtml(text: String): String =
    text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")

/**
 * Convert the inline Markdown of one already escaped text.
 *
 * @param escaped Text after `escapeHtml`.
 * @return HTML with `<code>`, `<b>`, `<i>` and `<a>`; the content of a code span is not
 *   converted further.
 */
private fun inline(escaped: String): String {
    val codes = ArrayList<String>()
    var s = CODE_SPAN.replace(escaped) { m -> codes.add(m.groupValues[1]); "\u0000${codes.size - 1}\u0000" }
    s = LINK.replace(s) { m -> "<a href=\"${m.groupValues[2]}\">${m.groupValues[1]}</a>" }
    s = BOLD.replace(s) { m -> "<b>${m.groupValues[1]}</b>" }
    s = ITALIC.replace(s) { m -> "<i>${m.groupValues[1]}</i>" }
    return Regex("\u0000(\\d+)\u0000").replace(s) { m -> "<code>${codes[m.groupValues[1].toInt()]}</code>" }
}

/**
 * Convert Markdown to an HTML fragment (no `<html>` or `<body>` element).
 *
 * @param markdown The text; an unclosed code fence (text that is still streaming) runs to the end.
 * @return The HTML. For any input the result contains no tag or attribute that is not written
 *   by this function.
 */
fun markdownToHtml(markdown: String): String {
    val out = StringBuilder()
    val paragraph = ArrayList<String>()
    var list: String? = null

    fun flushParagraph() {
        if (paragraph.isNotEmpty()) out.append("<p>").append(paragraph.joinToString("<br>") { inline(escapeHtml(it)) }).append("</p>")
        paragraph.clear()
    }

    fun closeList() {
        list?.let { out.append("</$it>") }
        list = null
    }

    val lines = markdown.replace("\r\n", "\n").split("\n")
    var i = 0
    while (i < lines.size) {
        val line = lines[i]
        val fence = FENCE_OPEN.find(line)
        if (fence != null) {
            flushParagraph(); closeList()
            val marker = fence.groupValues[1]
            val code = ArrayList<String>()
            i++
            while (i < lines.size && !(lines[i].trim().startsWith(marker) && lines[i].trim().all { it == marker[0] })) code.add(lines[i++])
            i++
            out.append("<pre>").append(escapeHtml(code.joinToString("\n"))).append("</pre>")
            continue
        }
        val heading = HEADING_LINE.find(line)
        val bullet = BULLET.find(line)
        val numbered = NUMBERED.find(line)
        when {
            line.isBlank() -> { flushParagraph(); closeList() }
            RULE.matches(line) -> { flushParagraph(); closeList(); out.append("<hr>") }
            heading != null -> {
                flushParagraph(); closeList()
                val level = minOf(6, heading.groupValues[1].length + 2)
                out.append("<h$level>").append(inline(escapeHtml(heading.groupValues[2]))).append("</h$level>")
            }
            bullet != null || numbered != null -> {
                flushParagraph()
                val kind = if (bullet != null) "ul" else "ol"
                if (list != kind) { closeList(); out.append("<$kind>"); list = kind }
                out.append("<li>").append(inline(escapeHtml((bullet ?: numbered)!!.groupValues[1]))).append("</li>")
            }
            line.trimStart().startsWith(">") -> {
                flushParagraph(); closeList()
                out.append("<blockquote>").append(inline(escapeHtml(line.trimStart().removePrefix(">").trimStart()))).append("</blockquote>")
            }
            else -> { closeList(); paragraph.add(line) }
        }
        i++
    }
    flushParagraph(); closeList()
    return out.toString()
}
