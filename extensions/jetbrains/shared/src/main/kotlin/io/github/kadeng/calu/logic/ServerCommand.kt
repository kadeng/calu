/**
 * The command line that starts `calu serve` in a terminal so that it keeps running after the
 * IDE closes (spec section 6). Pure: the settings and what is installed are passed in.
 *
 *     settings (calu command, server: sudo / insecure / extra arguments)
 *        │
 *        ▼
 *     serveArgv  ──►  tmux installed?   tmux new-session -A -s calu '<serve command>'   detach: Ctrl+B D
 *                     screen installed? screen -S calu -D -R <serve command>            detach: Ctrl+A D
 *                     neither           <serve command>            (ends with the terminal)
 *
 * `calu serve` starts sessions only in its projects directory and below it (`--projects`,
 * default the directory it starts in; PLAN D243). The plugin names one with `--projects`
 * (`projectsDirFor`): the folder that holds the project, so that one server also serves the
 * neighbouring projects, or the project's own folder where that parent is too wide or refused.
 * A `--projects` among the extra arguments wins.
 *
 * The plugin never runs this command as a child process: it may ask for a sudo password or a
 * credential password, which the user types into the terminal.
 *
 * Ported from `extensions/vscode/src/shared/serverCommand.ts`. The result type `ServerHelp` is
 * `io.github.kadeng.calu.state.ServerHelp`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ServerHelp

/** Name of the tmux and screen session that holds the server. */
const val SERVER_SESSION_NAME: String = "calu"

/** Matches an argument that a POSIX shell reads unchanged without quotes (see `shellQuote`). */
private val SHELL_SAFE = Regex("[A-Za-z0-9_@%+=:,./-]+")

/**
 * Inputs of `buildServerHelp`.
 *
 * @property calu The argv prefix that runs calu, e.g. `["/home/me/.local/bin/calu"]`; empty means
 *   `calu`.
 * @property sudo The setting "sudo" of the server: add `--sudo`.
 * @property insecure The setting "insecure" of the server: add `--insecure`.
 * @property extraArgs The setting "extra arguments" of the server: appended as they are (each one
 *   argument).
 * @property tmux True if `tmux` is installed on the workspace machine.
 * @property screen True if `screen` is installed on the workspace machine.
 * @property projects The projects directory to name with `--projects` (`projectsDirFor`), an
 *   absolute path on the workspace machine; `null` names none (the server then serves the
 *   directory its terminal starts in).
 */
data class ServerCommandInput(
    val calu: List<String>,
    val sudo: Boolean,
    val insecure: Boolean,
    val extraArgs: List<String>,
    val tmux: Boolean,
    val screen: Boolean,
    val projects: String? = null,
)

/**
 * First components of absolute paths that `calu serve` refuses to serve, or that are too wide
 * to serve as a whole (`calu.projects_dir`): system directories of Linux and macOS, `/var`,
 * and root's home.
 */
private val NOT_A_PROJECTS_PARENT = setOf(
    "etc", "proc", "sys", "dev", "boot", "bin", "sbin", "lib", "lib32", "lib64", "libx32", "run", "snap",
    "usr", "var", "root", "System", "Library", "Applications", "private", "cores",
)

/**
 * Choose the projects directory for the server command of a project.
 *
 *     /home/ada/src/demo    -> /home/ada/src     (the folder that holds the project)
 *     /home/ada/demo        -> /home/ada
 *     /home/ada             -> /home/ada         (the parent /home is too wide)
 *     /srv/demo             -> /srv/demo         (the parent has one component)
 *     /home/ada/.work/demo  -> /home/ada/.work/demo (calu refuses a hidden parent; it refuses
 *                                                  this one as well and says why)
 *     /var/www/site         -> /var/www/site
 *     "" or a relative path -> null
 *
 * @param workspace The project directory on the workspace machine (the bridge's `workspace`).
 * @return The parent of `workspace` if it has at least two components, none of them hidden,
 *   and its first is not a system directory (`NOT_A_PROJECTS_PARENT`); else `workspace`
 *   itself without a trailing slash; `null` for an empty or relative path.
 */
fun projectsDirFor(workspace: String?): String? {
    val parts = workspace?.takeIf { it.startsWith("/") }?.split('/')?.filter { it.isNotEmpty() && it != "." } ?: return null
    if (parts.isEmpty()) return null
    val own = "/" + parts.joinToString("/")
    val parent = parts.dropLast(1)
    val usable = parent.size >= 2 && parent.none { it.startsWith(".") } && parent[0] !in NOT_A_PROJECTS_PARENT
    return if (usable) "/" + parent.joinToString("/") else own
}

/**
 * Quote one argument for a POSIX shell.
 *
 * Example: `shellQuote("it's") == "'it'\\''s'"`.
 *
 * @param arg Any string, including the empty string and strings with quotes or newlines.
 * @return `arg` unchanged if it consists of characters that need no quoting, else the argument
 *   in single quotes with each embedded single quote written as `'\''`.
 */
fun shellQuote(arg: String): String {
    if (SHELL_SAFE.matches(arg)) return arg
    return "'" + arg.replace("'", "'\\''") + "'"
}

/**
 * Join arguments to one shell command.
 *
 * @param argv The arguments.
 * @return The arguments quoted with `shellQuote`, separated by spaces.
 */
fun shellJoin(argv: List<String>): String = argv.joinToString(" ") { shellQuote(it) }

/**
 * Build the argv of `calu serve` from the settings.
 *
 * @param calu The argv prefix that runs calu; empty strings are dropped, and an empty prefix
 *   stands for `["calu"]`.
 * @param sudo True to add `--sudo`.
 * @param insecure True to add `--insecure`.
 * @param extraArgs Arguments appended as they are; empty strings are dropped.
 * @param projects The projects directory; `null` for none. Left out when `extraArgs` has a
 *   `--projects` of its own.
 * @return `[...calu, "serve", ("--sudo")?, ("--insecure")?, ("--projects", projects)?, ...extraArgs]`.
 */
fun serveArgv(calu: List<String>, sudo: Boolean, insecure: Boolean, extraArgs: List<String>, projects: String? = null): List<String> {
    val prefix = calu.filter { it != "" }
    val argv = (prefix.ifEmpty { listOf("calu") } + "serve").toMutableList()
    if (sudo) argv.add("--sudo")
    if (insecure) argv.add("--insecure")
    if (projects != null && extraArgs.none { it == "--projects" || it.startsWith("--projects=") }) argv.addAll(listOf("--projects", projects))
    argv.addAll(extraArgs.filter { it != "" })
    return argv
}

/**
 * Say which directory the server command serves.
 *
 * @param argv The argv of `calu serve` (`serveArgv`).
 * @return " It serves <dir>: sessions start in it or below it." for a `--projects` in `argv`,
 *   else " It serves the directory it starts in: sessions start in it or below it.".
 */
private fun servesNote(argv: List<String>): String {
    val i = argv.indexOf("--projects")
    val dir = if (i >= 0) argv.getOrNull(i + 1) else argv.firstOrNull { it.startsWith("--projects=") }?.removePrefix("--projects=")
    return if (dir != null) " It serves $dir: sessions start in it or below it." else " It serves the directory it starts in: sessions start in it or below it."
}

/**
 * Build the server command and the texts that explain it.
 *
 * @param input The settings and what is installed.
 * @return The command for a POSIX shell with its tool ("tmux", "screen" or "plain"), detach hint
 *   and note (which names the directory served). With tmux, the serve command is one quoted argument (tmux passes it to a shell);
 *   with screen, its arguments follow screen's own.
 */
fun buildServerHelp(input: ServerCommandInput): ServerHelp {
    val argv = serveArgv(input.calu, input.sudo, input.insecure, input.extraArgs, input.projects)
    val serve = shellJoin(argv)
    val serves = servesNote(argv)
    if (input.tmux) {
        return ServerHelp(
            command = shellJoin(listOf("tmux", "new-session", "-A", "-s", SERVER_SESSION_NAME, serve)),
            tool = "tmux",
            detach = "Ctrl+B, then D",
            note = "Starts the calu server in the tmux session \"$SERVER_SESSION_NAME\", or opens that session if it " +
                "exists.$serves Detach to leave it running; it keeps running after the IDE closes.",
        )
    }
    if (input.screen) {
        return ServerHelp(
            command = shellJoin(listOf("screen", "-S", SERVER_SESSION_NAME, "-D", "-R") + argv),
            tool = "screen",
            detach = "Ctrl+A, then D",
            note = "Starts the calu server in the screen session \"$SERVER_SESSION_NAME\", or opens that session if it " +
                "exists.$serves Detach to leave it running; it keeps running after the IDE closes.",
        )
    }
    return ServerHelp(
        command = serve,
        tool = "plain",
        detach = "",
        note = "Starts the calu server in this terminal.$serves The server ends when the terminal closes; install tmux " +
            "or screen to keep it running after the IDE closes.",
    )
}

/**
 * Describe the root installation that `calu serve --sudo` needs, as a command to copy.
 *
 * The plugin only shows this command; it needs sudo and is never run by the plugin.
 *
 * @param source What to install (the install source of calu, e.g. a git address or a directory).
 * @return The shell command of DOCUMENTATION.md that installs calu for root with uv (below
 *   /usr/local/lib/calu, linked from /usr/local/bin).
 */
fun rootInstallCommand(source: String): String =
    "sudo env UV_TOOL_DIR=/usr/local/lib/calu UV_TOOL_BIN_DIR=/usr/local/bin " +
        "UV_PYTHON_INSTALL_DIR=/usr/local/lib/calu/python uv tool install ${shellQuote(source)}"
