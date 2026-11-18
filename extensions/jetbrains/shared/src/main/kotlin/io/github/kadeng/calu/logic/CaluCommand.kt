/**
 * Resolution of the command line that runs calu (`calu ide`, `calu serve`).
 *
 * Pure logic (no IntelliJ Platform import); file system checks are passed in as a function, so
 * the lookup order can be unit-tested:
 *
 *     1. the setting "calu command" (non-empty list)
 *     2. `calu` in PATH (on WSL, PATH entries under /mnt/ are skipped: they are Windows paths)
 *     3. ~/.local/bin/calu (default target of `uv tool install`)
 *     4. otherwise: not found (the result's message says what to do)
 *
 * `findExecutable` does steps 2 and 3 for any program (`uv`, `claude`, `tmux`, `screen`).
 *
 * Ported from `extensions/vscode/src/caluCommand.ts`.
 */
package io.github.kadeng.calu.logic

/** Matches a kernel version text of WSL (see `looksLikeWsl`). */
private val WSL_KERNEL = Regex("microsoft|wsl", RegexOption.IGNORE_CASE)

/**
 * Facts about the machine on which calu runs that the lookups need.
 *
 * @property pathEnv Value of the PATH environment variable ("" if unset).
 * @property pathDelimiter Separator of PATH entries: ":" on Linux/macOS, ";" on Windows.
 * @property home Home directory of the user, or "" if unknown.
 * @property isWsl True if the machine is a WSL distribution.
 * @property isWindows True on Windows (adds the `.exe`/`.cmd` names to the PATH lookup).
 * @property isExecutable Returns true if its argument is the path of an existing executable file.
 *   Must not throw.
 */
data class HostFacts(
    val pathEnv: String,
    val pathDelimiter: String,
    val home: String,
    val isWsl: Boolean,
    val isWindows: Boolean,
    val isExecutable: (String) -> Boolean,
)

/**
 * A program found by `findExecutable`.
 *
 * @property path The path of the program's file.
 * @property source Where it was found: "path" (a PATH entry) or "local-bin" (a directory below
 *   the home directory).
 */
data class FoundExecutable(val path: String, val source: String)

/** Result of `resolveCaluCommand`. */
sealed interface ResolveResult {
    /**
     * calu was found.
     *
     * @property command The argv prefix to which `ide` or `serve ...` is appended (non-empty).
     * @property source Where the command comes from: "setting", "path" or "local-bin".
     */
    data class Found(val command: List<String>, val source: String) : ResolveResult

    /**
     * calu was not found.
     *
     * @property message Text for the user that says so and what to do.
     */
    data class NotFound(val message: String) : ResolveResult
}

/**
 * Join a directory and a file name with the separator style of the directory.
 *
 * @param dir A directory path (POSIX or Windows style).
 * @param name A file name without separators.
 * @return The joined path, e.g. "/usr/bin" + "calu" -> "/usr/bin/calu".
 */
fun joinPath(dir: String, name: String): String {
    val sep = if (dir.contains('\\') && !dir.contains('/')) "\\" else "/"
    return if (dir.endsWith(sep)) dir + name else dir + sep + name
}

/**
 * Find a program: in PATH (on WSL without the Windows entries under /mnt/), then in the given
 * directories below the home directory.
 *
 * @param name The program's file name, e.g. "uv".
 * @param facts Facts about the machine.
 * @param homeDirs Directories below the home directory to try after PATH, each as path
 *   segments; default: `[".local", "bin"]`.
 * @return The path of the first match and where it was found, or `null`.
 */
fun findExecutable(
    name: String,
    facts: HostFacts,
    homeDirs: List<List<String>> = listOf(listOf(".local", "bin")),
): FoundExecutable? {
    val names = if (facts.isWindows) listOf("$name.exe", "$name.cmd", name) else listOf(name)
    // An empty delimiter cannot split anything: the whole PATH value is then one entry.
    val dirs = if (facts.pathDelimiter.isEmpty()) listOf(facts.pathEnv) else facts.pathEnv.split(facts.pathDelimiter)
    for (dir in dirs) {
        if (dir.isEmpty()) continue
        if (facts.isWsl && (dir == "/mnt" || dir.startsWith("/mnt/"))) continue
        for (candidateName in names) {
            val candidate = joinPath(dir, candidateName)
            if (facts.isExecutable(candidate)) return FoundExecutable(candidate, "path")
        }
    }
    if (facts.home.isEmpty()) return null
    for (segments in homeDirs) {
        val candidate = joinPath(segments.fold(facts.home, ::joinPath), name)
        if (facts.isExecutable(candidate)) return FoundExecutable(candidate, "local-bin")
    }
    return null
}

/**
 * Find the calu command line (see the file comment for the lookup order).
 *
 * @param configured Value of the setting "calu command"; an empty list means "not set". Empty
 *   strings in it are dropped.
 * @param facts Facts about the machine.
 * @return `Found` with the argv prefix to which `ide` or `serve ...` is appended, or `NotFound`
 *   with a message saying that calu was not found. Never throws (if `facts.isExecutable` does
 *   not).
 */
fun resolveCaluCommand(configured: List<String>, facts: HostFacts): ResolveResult {
    val set = configured.filter { it != "" }
    if (set.isNotEmpty()) return ResolveResult.Found(set, "setting")
    val found = findExecutable("calu", facts)
    if (found != null) return ResolveResult.Found(listOf(found.path), found.source)
    return ResolveResult.NotFound(
        "calu was not found on this machine. Install it with `uv tool install` (into " +
            "~/.local/bin), or set the setting \"calu command\" (Settings | Tools | calu), " +
            "e.g. [\"uv\", \"run\", \"--project\", \"/path/to/calu\", \"calu\"].",
    )
}

/**
 * Build the full argv for starting the bridge.
 *
 * @param command The argv prefix from `resolveCaluCommand` (non-empty).
 * @return `[...command, "ide"]`.
 * @throws IllegalArgumentException if `command` is empty.
 */
fun buildIdeArgv(command: List<String>): List<String> {
    require(command.isNotEmpty()) { "the calu command is empty" }
    return command + "ide"
}

/**
 * Decide from the contents of /proc/version (or the kernel release) whether the machine is WSL.
 *
 * @param text The kernel version text; "" if unavailable.
 * @return True if the text mentions Microsoft/WSL (case-insensitive).
 */
fun looksLikeWsl(text: String): Boolean = WSL_KERNEL.containsMatchIn(text)
