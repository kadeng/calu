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
 */
data class ServerCommandInput(
    val calu: List<String>,
    val sudo: Boolean,
    val insecure: Boolean,
    val extraArgs: List<String>,
    val tmux: Boolean,
    val screen: Boolean,
)

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
 * @return `[...calu, "serve", ("--sudo")?, ("--insecure")?, ...extraArgs]`.
 */
fun serveArgv(calu: List<String>, sudo: Boolean, insecure: Boolean, extraArgs: List<String>): List<String> {
    val prefix = calu.filter { it != "" }
    val argv = (prefix.ifEmpty { listOf("calu") } + "serve").toMutableList()
    if (sudo) argv.add("--sudo")
    if (insecure) argv.add("--insecure")
    argv.addAll(extraArgs.filter { it != "" })
    return argv
}

/**
 * Build the server command and the texts that explain it.
 *
 * @param input The settings and what is installed.
 * @return The command for a POSIX shell with its tool ("tmux", "screen" or "plain"), detach hint
 *   and note. With tmux, the serve command is one quoted argument (tmux passes it to a shell);
 *   with screen, its arguments follow screen's own.
 */
fun buildServerHelp(input: ServerCommandInput): ServerHelp {
    val argv = serveArgv(input.calu, input.sudo, input.insecure, input.extraArgs)
    val serve = shellJoin(argv)
    if (input.tmux) {
        return ServerHelp(
            command = shellJoin(listOf("tmux", "new-session", "-A", "-s", SERVER_SESSION_NAME, serve)),
            tool = "tmux",
            detach = "Ctrl+B, then D",
            note = "Starts the calu server in the tmux session \"$SERVER_SESSION_NAME\", or opens that session if it " +
                "exists. Detach to leave it running; it keeps running after the IDE closes.",
        )
    }
    if (input.screen) {
        return ServerHelp(
            command = shellJoin(listOf("screen", "-S", SERVER_SESSION_NAME, "-D", "-R") + argv),
            tool = "screen",
            detach = "Ctrl+A, then D",
            note = "Starts the calu server in the screen session \"$SERVER_SESSION_NAME\", or opens that session if it " +
                "exists. Detach to leave it running; it keeps running after the IDE closes.",
        )
    }
    return ServerHelp(
        command = serve,
        tool = "plain",
        detach = "",
        note = "Starts the calu server in this terminal. The server ends when the terminal closes; install tmux " +
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
