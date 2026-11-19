/**
 * The ignore rules of `.gitignore` files, for the listing of the local project copy.
 *
 * The remote manifest is `git ls-files -co --exclude-standard`, so it leaves out what the
 * project's `.gitignore` files name. The local copy made by "Clone from server" has no `.git`
 * directory (it is never synchronised), so the plugin cannot ask git; it reads the `.gitignore`
 * files itself. Without that, a local `__pycache__` or `build` directory would be uploaded.
 *
 * Supported, as git documents them: blank lines and `#` comments, `!` negation, a trailing `/`
 * (directories only), a leading or inner `/` (anchored to the directory of the file), `*`, `?`,
 * `[...]`, `**` in the three positions. Not supported: `.git/info/exclude` and the user's
 * global excludes (they are not in the copy); a file they hide on the server is uploaded once
 * and then shown as "not listed by the server".
 */
package io.github.kadeng.calu.sync

/**
 * One line of a `.gitignore` file.
 *
 * @property dir directory of the file the line is from, as a relative path ("" for the root).
 * @property negated true for a line that starts with `!`: a match includes the path again.
 * @property dirOnly true for a line that ends with `/`: it matches directories only.
 * @property anchored true if the pattern has a `/` at its start or inside: it is matched against
 *   the path relative to [dir]; otherwise against the last segment at any depth below [dir].
 * @property regex the pattern as a regular expression over the text it is matched against.
 */
private class IgnorePattern(val dir: String, val negated: Boolean, val dirOnly: Boolean, val anchored: Boolean, val regex: Regex)

/**
 * A set of `.gitignore` files. Mutable while the files are added; not thread-safe.
 */
class GitIgnore {
    private val byDir = HashMap<String, List<IgnorePattern>>()

    /**
     * Add the lines of one `.gitignore` file. A second call for the same directory replaces the first.
     *
     * @param dir the directory the file is in, relative to the project root with "/" separators;
     *   "" for the root.
     * @param text the file's text.
     */
    fun add(dir: String, text: String) {
        byDir[dir] = text.split('\n').mapNotNull { parse(dir, it.removeSuffix("\r")) }
    }

    /**
     * Tell whether a file is ignored. No side effects.
     *
     * A path is ignored if the rules ignore the path itself or one of its directories: git never
     * looks into an ignored directory, so a `!` line cannot bring back a file inside one.
     *
     * @param path relative path of a file with "/" separators.
     * @return true if the file is ignored by the files added so far.
     */
    fun isIgnored(path: String): Boolean {
        val segments = path.split('/')
        for (end in 1..segments.size) {
            if (matches(segments.subList(0, end).joinToString("/"), isDir = end < segments.size)) return true
        }
        return false
    }

    /**
     * Tell whether a directory is ignored, so that a walk can skip it. No side effects.
     *
     * @param path relative path of a directory with "/" separators.
     * @return true if the directory or one of its parents is ignored.
     */
    fun isIgnoredDir(path: String): Boolean {
        val segments = path.split('/')
        for (end in 1..segments.size) {
            if (matches(segments.subList(0, end).joinToString("/"), isDir = true)) return true
        }
        return false
    }

    /**
     * Decide one path by the last matching line: files of deeper directories win over those of
     * higher ones, and within a file a later line wins.
     *
     * @param path relative path of a file or directory.
     * @param isDir whether the path is a directory.
     * @return true if the last matching line is not negated; false if none matches.
     */
    private fun matches(path: String, isDir: Boolean): Boolean {
        val parent = path.substringBeforeLast('/', "")
        var dir: String? = parent
        while (dir != null) {
            val patterns = byDir[dir]
            if (patterns != null) {
                val relative = if (dir.isEmpty()) path else path.substring(dir.length + 1)
                val name = path.substringAfterLast('/')
                for (p in patterns.asReversed()) {
                    if (p.dirOnly && !isDir) continue
                    if (p.regex.matches(if (p.anchored) relative else name)) return !p.negated
                }
            }
            dir = if (dir.isEmpty()) null else dir.substringBeforeLast('/', "")
        }
        return false
    }

    /**
     * Read one line.
     *
     * @param dir directory of the file.
     * @param raw the line without its line end.
     * @return the pattern, or null for a blank line, a comment or a line that is only `/` or `!`.
     */
    private fun parse(dir: String, raw: String): IgnorePattern? {
        var line = raw
        // Trailing spaces are dropped unless the last one is escaped.
        while (line.endsWith(" ") && !line.endsWith("\\ ")) line = line.dropLast(1)
        if (line.isEmpty() || line.startsWith("#")) return null
        val negated = line.startsWith("!")
        if (negated) line = line.substring(1)
        if (line.startsWith("\\#") || line.startsWith("\\!")) line = line.substring(1)
        val dirOnly = line.endsWith("/")
        if (dirOnly) line = line.dropLast(1)
        val anchored = line.contains('/')
        if (line.startsWith("/")) line = line.substring(1)
        if (line.isEmpty()) return null
        return IgnorePattern(dir, negated, dirOnly, anchored, Regex(globToRegex(line)))
    }

    /**
     * Translate a glob of a `.gitignore` line into a regular expression.
     *
     * @param glob the pattern without `!`, without the leading and the trailing `/`.
     * @return the expression: `**&#47;` any directories (also none), `&#47;**` everything inside,
     *   `*` and `?` within one segment, `[...]` a character class, `\x` the character x.
     */
    private fun globToRegex(glob: String): String {
        val out = StringBuilder()
        var i = 0
        while (i < glob.length) {
            val c = glob[i]
            when {
                glob.startsWith("**/", i) && (i == 0 || glob[i - 1] == '/') -> { out.append("(?:.*/)?"); i += 3 }
                glob.startsWith("/**", i) && i + 3 == glob.length -> { out.append("/.*"); i += 3 }
                c == '*' -> {
                    while (i < glob.length && glob[i] == '*') i++
                    out.append("[^/]*")
                }
                c == '?' -> { out.append("[^/]"); i++ }
                c == '[' -> {
                    val close = glob.indexOf(']', i + 2)
                    if (close < 0) {
                        out.append("\\["); i++
                    } else {
                        var body = glob.substring(i + 1, close)
                        if (body.startsWith("!")) body = "^" + body.substring(1)
                        out.append('[').append(body.replace("\\", "\\\\")).append(']')
                        i = close + 1
                    }
                }
                c == '\\' && i + 1 < glob.length -> { out.append(Regex.escape(glob[i + 1].toString())); i += 2 }
                else -> { out.append(Regex.escape(c.toString())); i++ }
            }
        }
        return out.toString()
    }
}
