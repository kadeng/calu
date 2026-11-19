/**
 * Path rules of the file synchronisation: the project relative form, the mapping between the
 * local and the remote project root, and the paths that are never synchronised.
 */
package io.github.kadeng.calu.sync

import java.util.Locale

/** Why a path is not an acceptable project relative path. */
enum class PathProblem {
    /** The path is empty or names the project root itself. */
    EMPTY,

    /** The path is absolute: it starts with "/" or with a drive letter and a colon. */
    ABSOLUTE,

    /** The path has a ".." segment. */
    PARENT_SEGMENT,

    /** The path contains a NUL character, another control character or a backslash. */
    INVALID_CHARACTER,

    /** The path is inside one of the excluded directories ([PathMapper.excluded]). */
    EXCLUDED,

    /** The local file system is Windows and cannot hold a file of this name. */
    NOT_REPRESENTABLE_LOCALLY,
}

/**
 * Maps paths between the local project copy and the remote project directory and applies the
 * path rules. Instances are immutable and have no side effects; no method touches a file system.
 *
 * The canonical form used everywhere in this package is the *relative path*: relative to the
 * project root, "/" separators, no empty, "." or ".." segment, no trailing "/".
 *
 *     local  C:\work\proj\src\a.py   <->  relative  src/a.py  <->  remote  /home/u/proj/src/a.py
 *
 * @property localRoot absolute path of the local project copy, without a trailing separator
 *   (for example `C:\work\proj` or `/home/u/proj`). May be empty if only the rules are used.
 * @property remoteRoot absolute path of the remote project directory as `ide.paths` reports it,
 *   with "/" separators. May be empty if only the rules are used.
 * @property windowsLocal true if the local machine is Windows: local paths use "\" (and accept
 *   "/"), and names Windows cannot hold are rejected.
 * @property caseInsensitiveLocal true if the local file system treats `A.txt` and `a.txt` as one
 *   file (Windows, default macOS).
 * @property excluded directories that are never synchronised, as relative paths. An entry of one
 *   segment (`.git`) excludes a directory of that name at any depth; an entry of several
 *   segments (`agent/traces`) excludes that directory under the project root. Letter case is
 *   ignored in the comparison.
 */
class PathMapper(
    val localRoot: String = "",
    val remoteRoot: String = "",
    val windowsLocal: Boolean = false,
    val caseInsensitiveLocal: Boolean = windowsLocal,
    val excluded: List<String> = DEFAULT_EXCLUDED,
) {
    private val excludedLower: List<List<String>> = excluded.map { it.lowercase(Locale.ROOT).split('/') }

    /**
     * Checks a relative path in canonical form against the rules. No side effects.
     *
     * @param relative the path to check.
     * @return null if the path may be synchronised, else the first problem found, in the order
     *   EMPTY, INVALID_CHARACTER, ABSOLUTE, PARENT_SEGMENT, EXCLUDED, NOT_REPRESENTABLE_LOCALLY.
     *   An empty or "." segment and a trailing "/" count as INVALID_CHARACTER, because the
     *   canonical form has none ([normalize] removes them).
     */
    fun problem(relative: String): PathProblem? {
        if (relative.isEmpty()) return PathProblem.EMPTY
        if (relative.any { it < ' ' || it == '\\' || it == '\u007f' }) return PathProblem.INVALID_CHARACTER
        if (relative.startsWith("/") || DRIVE.matchesAt(relative, 0)) return PathProblem.ABSOLUTE
        val segments = relative.split('/')
        if (segments.any { it == ".." }) return PathProblem.PARENT_SEGMENT
        if (segments.any { it.isEmpty() || it == "." }) return PathProblem.INVALID_CHARACTER
        if (isExcluded(relative)) return PathProblem.EXCLUDED
        if (windowsLocal && segments.any { !isWindowsName(it) }) return PathProblem.NOT_REPRESENTABLE_LOCALLY
        return null
    }

    /**
     * Tells whether a relative path is inside an excluded directory, or is one. No side effects.
     *
     * @param relative a relative path with "/" separators.
     * @return true for `.git`, `.git/config`, `sub/.git/HEAD`, `agent/traces/x.md` with the
     *   default list; false for `.gitignore` and `agent/notes.md`.
     */
    fun isExcluded(relative: String): Boolean {
        val segments = relative.lowercase(Locale.ROOT).split('/')
        for (entry in excludedLower) {
            if (entry.size == 1) {
                if (entry[0] in segments) return true
            } else if (segments.size >= entry.size && segments.subList(0, entry.size) == entry) {
                return true
            }
        }
        return false
    }

    /**
     * Brings a relative path into canonical form and checks it. No side effects.
     *
     * With [windowsLocal], "\" is read as a separator. Empty and "." segments and a trailing
     * separator are removed: `.\src\\a.py` becomes `src/a.py`.
     *
     * @param path a path relative to the project root, as typed or as a file system reports it.
     * @return the canonical relative path.
     * @throws IllegalArgumentException if [problem] finds a problem in the result; the message
     *   names the [PathProblem].
     */
    fun normalize(path: String): String {
        val slashed = if (windowsLocal) path.replace('\\', '/') else path
        if (slashed.startsWith("/")) throw IllegalArgumentException("${PathProblem.ABSOLUTE}: $path")
        val relative = slashed.split('/').filter { it.isNotEmpty() && it != "." }.joinToString("/")
        problem(relative)?.let { throw IllegalArgumentException("$it: $path") }
        return relative
    }

    /**
     * Maps an absolute local path to its relative path. No side effects.
     *
     * @param absoluteLocal an absolute path on the local machine.
     * @return the canonical relative path, or null if the path is not below [localRoot], is the
     *   root itself, or breaks a rule ([problem]). The root prefix is compared without regard
     *   to letter case if [caseInsensitiveLocal].
     */
    fun toRelativeLocal(absoluteLocal: String): String? {
        val path = if (windowsLocal) absoluteLocal.replace('\\', '/') else absoluteLocal
        val root = (if (windowsLocal) localRoot.replace('\\', '/') else localRoot).trimEnd('/')
        return below(path, root, caseInsensitiveLocal)
    }

    /**
     * Maps a path of the remote side to its relative path. No side effects.
     *
     * @param remotePath an absolute remote path, or a path that is already relative to the
     *   project root (session events carry either form).
     * @return the canonical relative path, or null if an absolute path is not below
     *   [remoteRoot], is the root itself, or the path breaks a rule ([problem]).
     */
    fun toRelativeRemote(remotePath: String): String? {
        if (remotePath.startsWith("/")) return below(remotePath, remoteRoot.trimEnd('/'), false)
        val relative = remotePath.split('/').filter { it.isNotEmpty() && it != "." }.joinToString("/")
        return relative.takeIf { problem(it) == null }
    }

    /**
     * Builds the absolute local path of a relative path. No side effects.
     *
     * @param relative a canonical relative path.
     * @return [localRoot], the local separator and the path with local separators.
     * @throws IllegalArgumentException if [problem] finds a problem in [relative].
     */
    fun toLocal(relative: String): String {
        problem(relative)?.let { throw IllegalArgumentException("$it: $relative") }
        val separator = if (windowsLocal) '\\' else '/'
        return localRoot.trimEnd('/', '\\') + separator + relative.replace('/', separator)
    }

    /**
     * Builds the absolute remote path of a relative path. No side effects.
     *
     * @param relative a canonical relative path.
     * @return [remoteRoot], "/" and the path.
     * @throws IllegalArgumentException if [problem] finds a problem in [relative].
     */
    fun toRemote(relative: String): String {
        problem(relative)?.let { throw IllegalArgumentException("$it: $relative") }
        return remoteRoot.trimEnd('/') + "/" + relative
    }

    /**
     * Finds the paths that a case-insensitive file system cannot keep apart. No side effects.
     *
     * Two paths collide if the paths, or directories on the way to them, are spelled
     * differently and are equal when letter case is ignored: `README.md` and `Readme.md`
     * collide, and so do `Src/a.py` and `src/b.py` (one directory, two spellings).
     *
     * @param paths relative paths of both sides.
     * @return every path that takes part in a collision; empty if there is none. The result does
     *   not depend on [caseInsensitiveLocal]; the caller asks only when the local side ignores case.
     */
    fun caseCollisions(paths: Collection<String>): Set<String> {
        val spellings = HashMap<String, MutableSet<String>>()
        for (path in paths) {
            for (prefix in prefixes(path)) {
                spellings.getOrPut(prefix.lowercase(Locale.ROOT)) { HashSet() }.add(prefix)
            }
        }
        val colliding = spellings.filterValues { it.size > 1 }.keys
        if (colliding.isEmpty()) return emptySet()
        return paths.filterTo(LinkedHashSet()) { path ->
            prefixes(path).any { it.lowercase(Locale.ROOT) in colliding }
        }
    }

    /**
     * Lists a path's directory prefixes and the path itself.
     *
     * @param path a relative path, for example `a/b/c`.
     * @return `a`, `a/b`, `a/b/c`.
     */
    private fun prefixes(path: String): List<String> {
        val out = ArrayList<String>()
        var at = path.indexOf('/')
        while (at >= 0) {
            out.add(path.substring(0, at))
            at = path.indexOf('/', at + 1)
        }
        out.add(path)
        return out
    }

    /**
     * Strips a root from an absolute path with "/" separators and checks the rest.
     *
     * @param path the absolute path.
     * @param root the root without a trailing "/".
     * @param ignoreCase whether the root prefix is compared without regard to letter case.
     * @return the canonical relative path, or null if [path] is not strictly below [root] or the
     *   rest breaks a rule.
     */
    private fun below(path: String, root: String, ignoreCase: Boolean): String? {
        if (!path.startsWith("$root/", ignoreCase)) return null
        val relative = path.substring(root.length).split('/').filter { it.isNotEmpty() && it != "." }.joinToString("/")
        return relative.takeIf { problem(it) == null }
    }

    /**
     * Tells whether Windows can hold a file or directory of this name.
     *
     * @param segment one path segment.
     * @return false if the segment contains one of `< > : " | ? *`, ends with a dot or a space,
     *   or is a reserved device name (`CON`, `NUL`, `COM1`, ... with or without an extension).
     */
    private fun isWindowsName(segment: String): Boolean {
        if (segment.any { it in "<>:\"|?*" }) return false
        if (segment.endsWith(".") || segment.endsWith(" ")) return false
        return segment.substringBefore('.').uppercase(Locale.ROOT) !in RESERVED
    }

    companion object {
        /**
         * The directories no side ever synchronises: git's own data and the session traces
         * that calu writes itself (requirement 36).
         */
        val DEFAULT_EXCLUDED: List<String> = listOf(".git", "agent/traces")

        private val DRIVE = Regex("[A-Za-z]:")

        private val RESERVED: Set<String> =
            setOf("CON", "PRN", "AUX", "NUL") + (1..9).map { "COM$it" } + (1..9).map { "LPT$it" }
    }
}
