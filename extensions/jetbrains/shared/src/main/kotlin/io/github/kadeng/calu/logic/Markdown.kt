/**
 * A small Markdown to HTML converter for the transcript's Swing text panes (HTML 3.2 as
 * `javax.swing.text.html` renders it). Pure.
 *
 * Supported: paragraphs, headings (#..######), fenced code blocks, bullet and numbered lists
 * (one level), block quotes, horizontal rules, pipe tables (a header row, a row of dashes, body
 * rows); inline: `code`, **bold**, *italic*, [text](url)
 * with http, https and mailto addresses, and bare line breaks inside a paragraph. Source
 * references (`src/app.py:12`, `docs/x.md`, `[text](src/app.py#L12)`) become `calu-file:`
 * links that open the file at that line ([SOURCE_REF], the same pattern as the VSCode
 * extension's `sourceRefs.ts`). Everything else
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
private val LOCAL_LINK = Regex("\\[([^\\]\n]+)]\\(([^\\s)\"<>:]+(?::\\d+(?::\\d+)?|#L?\\d+(?:-L?\\d+)?)?)\\)")
private val ANCHOR = Regex("<a [^>]*>.*?</a>")

/** File extensions that make a path a source reference (code, configuration and text files). */
val SOURCE_EXTENSIONS: List<String> = listOf(
    "py", "pyi", "ipynb", "c", "h", "cc", "cpp", "cxx", "hh", "hpp", "hxx", "rs", "go", "java",
    "kt", "kts", "scala", "rb", "php", "cs", "swift", "m", "mm", "js", "jsx", "mjs", "cjs", "ts",
    "tsx", "vue", "svelte", "html", "htm", "css", "scss", "sass", "less", "json", "jsonc", "yaml",
    "yml", "toml", "ini", "cfg", "conf", "xml", "md", "rst", "txt", "sh", "bash", "zsh", "ps1",
    "sql", "proto", "graphql", "gradle", "tf", "lua", "r", "dart", "ex", "exs", "erl", "hs", "ml",
    "clj", "zig", "nim", "jl", "cmake", "mk", "lock", "csv", "tsv",
)

/**
 * One source reference: a relative path with a known extension and an optional line, e.g.
 * `src/app.py:12`, `src/app.py#L12`, `docs/x.md`. Group 1 is the path, group 2 the line. No word
 * character, `.`, `/`, `\`, `:` or `@` before it (no URL, absolute path or e-mail address);
 * no word character, `/` or `\` after it.
 */
val SOURCE_REF: Regex = Regex(
    "(?<![\\w./\\\\:@-])((?:\\.{1,2}/)?(?:[\\w.-]+/)*[\\w-][\\w.-]*\\.(?:" +
        SOURCE_EXTENSIONS.joinToString("|") +
        "))(?:(?::|#L?)(\\d+)(?::\\d+)?(?:-L?\\d+)?)?(?![\\w/\\\\])",
)

/**
 * Read a text that is exactly one source reference.
 *
 * @param text A link target or the text of a code span.
 * @return The relative path and the line (`null` for none or 0), or `null` if the text is not
 *   one whole reference.
 */
fun sourceRefOf(text: String): FileRef? {
    val m = SOURCE_REF.matchEntire(text) ?: return null
    val line = m.groupValues[2].toIntOrNull()?.takeIf { it >= 1 }
    return FileRef(m.groupValues[1], line)
}

/**
 * Return the address of a `calu-file:` link (read back by [parseFileLink]).
 *
 * @param ref The file and line.
 * @return `calu-file:<URL-encoded path>`, with `#L<line>` if a line is known.
 */
fun fileAddress(ref: FileRef): String =
    FILE_LINK_SCHEME + java.net.URLEncoder.encode(ref.path, Charsets.UTF_8) + (ref.line?.let { "#L$it" } ?: "")

/**
 * Turn the source references in inline HTML into `calu-file:` links, outside existing links.
 *
 * @param html Inline HTML of [inline] (escaped text, code span placeholders, links).
 * @return The HTML with each reference outside an `<a>` element wrapped in a link.
 */
private fun linkSourceText(html: String): String {
    val out = StringBuilder()
    var at = 0
    fun plain(text: String) = SOURCE_REF.replace(text) { m ->
        val line = m.groupValues[2].toIntOrNull()?.takeIf { it >= 1 }
        "<a href=\"${fileAddress(FileRef(m.groupValues[1], line))}\">${m.value}</a>"
    }
    for (anchor in ANCHOR.findAll(html)) {
        out.append(plain(html.substring(at, anchor.range.first))).append(anchor.value)
        at = anchor.range.last + 1
    }
    return out.append(plain(html.substring(at))).toString()
}

private val TABLE_RULE = Regex("^\\s*\\|?\\s*:?-{1,}:?\\s*(\\|\\s*:?-{1,}:?\\s*)*\\|?\\s*$")

/**
 * Split one row of a pipe table into its cells.
 *
 * @param line The row, with or without the outer pipes.
 * @return The trimmed cell texts; a pipe written as `\|` stays in its cell.
 */
private fun tableCells(line: String): List<String> {
    var t = line.trim()
    if (t.startsWith("|")) t = t.substring(1)
    if (t.endsWith("|") && !t.endsWith("\\|")) t = t.substring(0, t.length - 1)
    return t.split(Regex("(?<!\\\\)\\|")).map { it.trim().replace("\\|", "|") }
}

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
    s = LOCAL_LINK.replace(s) { m ->
        val ref = sourceRefOf(m.groupValues[2])
        if (ref == null) m.value else "<a href=\"${fileAddress(ref)}\">${m.groupValues[1]}</a>"
    }
    s = linkSourceText(s)
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
            out.append("<div class=\"pre\">").append(code.joinToString("<br>") { codeLineHtml(it) }).append("</div>")
            continue
        }
        val heading = HEADING_LINE.find(line)
        val bullet = BULLET.find(line)
        val numbered = NUMBERED.find(line)
        if ('|' in line && i + 1 < lines.size && '-' in lines[i + 1] && '|' in lines[i + 1] && TABLE_RULE.matches(lines[i + 1])) {
            flushParagraph(); closeList()
            out.append("<table border=\"0\" cellspacing=\"0\" cellpadding=\"3\"><tr>")
            for (c in tableCells(line)) out.append("<th align=\"left\">").append(inline(escapeHtml(c))).append("</th>")
            out.append("</tr>")
            i += 2
            while (i < lines.size && '|' in lines[i] && lines[i].isNotBlank()) {
                out.append("<tr>")
                for (c in tableCells(lines[i])) out.append("<td>").append(inline(escapeHtml(c))).append("</td>")
                out.append("</tr>")
                i++
            }
            out.append("</table>")
            continue
        }
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

/**
 * Write one line of a code block so that it keeps its spaces and can still wrap.
 *
 * Swing's HTML view does not wrap `<pre>`, so a line wider than the chat was cut. A code block
 * is a `<div class="pre">` of lines joined by `<br>`; in each line the indentation and every
 * second space of a run are non-breaking spaces, so the columns stay and the view can wrap at
 * the single spaces that remain (a wrapped line continues at the left edge).
 *
 * @param line One line of code (without its line end).
 * @return The escaped line; a tab is written as four spaces, which are non-breaking where
 *   the rule above makes them so (all of them in the indentation); "&nbsp;" for an empty line
 *   (a `<br>` alone would collapse).
 */
fun codeLineHtml(line: String): String {
    if (line.isEmpty()) return "&nbsp;"
    val text = line.replace("\t", "    ")
    val indent = text.length - text.trimStart(' ').length
    val out = StringBuilder("&nbsp;".repeat(indent))
    var spaces = 0
    for (c in text.substring(indent)) {
        if (c == ' ') {
            spaces++
            out.append(if (spaces % 2 == 0) "&nbsp;" else " ")
        } else {
            spaces = 0
            out.append(escapeHtml(c.toString()))
        }
    }
    return out.toString()
}

/** Scheme of the links [linkFiles] writes; the rest of the address is the URL-encoded path, then `#L<line>` if a line is known. */
const val FILE_LINK_SCHEME: String = "calu-file:"

private val FILE_CODE_SPAN = Regex("<code>([^<]{1,400})</code>")

/**
 * Turn the code spans of Claude's text that name a file into links.
 *
 * A span whose whole text is a file a tool call touched (optionally with `:line` or
 * `:line:column`) links to that file (`lookupFile`); else a span whose whole text is one source
 * reference ([sourceRefOf]) links to that relative path, which the IDE resolves against the
 * session's directory. A span that is already inside a link and everything else stay as they
 * are. Opening a file shows it in the editor and runs nothing.
 *
 * @param html HTML of `markdownToHtml`.
 * @param index The files the session's tool calls named (`buildFileIndex`).
 * @return The HTML with `<a href="calu-file:<encoded path>#L<line>"><code>…</code></a>` around
 *   each such span.
 */
fun linkFiles(html: String, index: FileIndex): String = FILE_CODE_SPAN.replace(html) { m ->
    val before = html.substring(0, m.range.first)
    if (before.lastIndexOf("<a ") > before.lastIndexOf("</a>")) return@replace m.value // already a link
    val shown = m.groupValues[1]
    val text = shown.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&#39;", "'").replace("&amp;", "&")
    val ref = lookupFile(index, text) ?: sourceRefOf(text.trim()) ?: return@replace m.value
    "<a href=\"${fileAddress(ref)}\"><code>$shown</code></a>"
}

/**
 * Read a link of [linkFiles].
 *
 * @param href The link's address.
 * @return The file and line it names, or `null` if the address is no such link.
 */
fun parseFileLink(href: String): FileRef? {
    if (!href.startsWith(FILE_LINK_SCHEME)) return null
    val rest = href.removePrefix(FILE_LINK_SCHEME)
    val line = rest.substringAfterLast("#L", "").toIntOrNull()
    val path = try {
        java.net.URLDecoder.decode(if (line != null) rest.substringBeforeLast("#L") else rest, Charsets.UTF_8)
    } catch (_: IllegalArgumentException) {
        return null
    }
    return if (path.isEmpty()) null else FileRef(path, line)
}
