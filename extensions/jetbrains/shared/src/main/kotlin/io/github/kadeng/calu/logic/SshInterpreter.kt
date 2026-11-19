/**
 * The pure parts of the offer "Set up running and debugging on <host>" (SSH mode): which
 * server PyCharm's SSH interpreter connects to, the command that guesses the Python on the
 * server, and the checks of the path the user confirms.
 *
 *     plugin settings ──sshDestination──► alias, user?      `ssh -G` output ──parseSshDump──► user, port
 *     ssh <host> pythonGuessCommand(dir) ──parsePythonGuess──► "/home/dev/project/.venv/bin/python"
 *     the user's answer ──pythonPathProblem──► null (accepted) or the reason
 *     ssh <host> pythonVersionCommand(path) ──parsePythonVersion──► "3.12.12"
 */
package io.github.kadeng.calu.logic

/**
 * The server an SSH interpreter connects to, as PyCharm's SSH configuration takes it.
 *
 * @property alias The host as the plugin's settings name it, without a user (an alias of
 *   `~/.ssh/config` or a host name); PyCharm applies the OpenSSH configuration to it.
 * @property user The user on the server.
 * @property port The SSH port.
 */
data class SshEndpoint(val alias: String, val user: String, val port: Int)

/**
 * Split the SSH host setting into the host and the user it names.
 *
 *     "sshbox"          -> ("sshbox", null)
 *     "dev@sshbox"      -> ("sshbox", "dev")
 *     " dev@10.0.0.5 "  -> ("10.0.0.5", "dev")
 *
 * @param setting The "SSH host" of the plugin's settings.
 * @return The host and the user (`null` if the setting names none).
 */
fun sshDestination(setting: String): Pair<String, String?> {
    val s = setting.trim()
    val at = s.lastIndexOf('@')
    return if (at <= 0) s to null else s.substring(at + 1) to s.substring(0, at)
}

/**
 * Read the user and the port from the output of `ssh -G <host>` (the effective configuration,
 * one "keyword value" pair per line, keywords in lower case).
 *
 * @param dump The output.
 * @return The keywords' values that were found: "user", "port", "hostname" (others are left out).
 */
fun parseSshDump(dump: String): Map<String, String> {
    val wanted = setOf("user", "port", "hostname")
    val out = HashMap<String, String>()
    for (line in dump.lineSequence()) {
        val parts = line.trim().split(Regex("\\s+"), limit = 2)
        if (parts.size == 2 && parts[0].lowercase() in wanted && parts[0].lowercase() !in out) out[parts[0].lowercase()] = parts[1].trim()
    }
    return out
}

/**
 * Decide the endpoint of the SSH interpreter from the settings and from `ssh -G`.
 *
 * The user of the setting wins over the configuration's; the port comes from the configuration
 * (which already includes a `-p` of the extra ssh options when they were passed to `ssh -G`).
 *
 * @param setting The "SSH host" of the plugin's settings.
 * @param dump The output of `ssh -G` for that host, or `null` if it could not be run.
 * @param localUser The user name on this machine (ssh's default user).
 * @return The endpoint; the port is 22 when nothing else is known.
 */
fun sshEndpoint(setting: String, dump: String?, localUser: String): SshEndpoint {
    val (host, user) = sshDestination(setting)
    val values = dump?.let { parseSshDump(it) }.orEmpty()
    val port = values["port"]?.toIntOrNull()?.takeIf { it in 1..65535 } ?: 22
    return SshEndpoint(host, user ?: values["user"]?.takeIf { it.isNotEmpty() } ?: localUser, port)
}

/** The first line `pythonGuessCommand` prints in front of its answer. */
const val PYTHON_GUESS_MARK: String = "calu-python:"

/**
 * Build the remote command that guesses the Python of the project on the server: the
 * project's virtual environment (`.venv`, `venv`) if it has one, else `python3`, else `python`
 * on the PATH of a non-interactive SSH command, else the newest `~/.local/bin/python3*` (where
 * uv installs its Pythons, which a non-interactive SSH command does not have on its PATH).
 *
 * @param remoteDir The project directory on the server (absolute).
 * @return One POSIX shell command; it prints [PYTHON_GUESS_MARK] followed by the path, or
 *   nothing after the mark if no Python was found. It changes nothing on the server.
 */
fun pythonGuessCommand(remoteDir: String): String {
    val dir = shellQuote(remoteDir)
    return "if cd $dir 2>/dev/null; then for p in .venv/bin/python venv/bin/python; do " +
        "if [ -x \"\$p\" ]; then echo \"$PYTHON_GUESS_MARK\$PWD/\$p\"; exit 0; fi; done; fi; " +
        "p=\$(command -v python3 || command -v python || ls -1 \"\$HOME\"/.local/bin/python3* 2>/dev/null | sort -V | tail -n 1); " +
        "echo \"$PYTHON_GUESS_MARK\$p\""
}

/**
 * Read the answer of [pythonGuessCommand].
 *
 * @param output The command's standard output (login banners and other lines are skipped).
 * @return The absolute path it found, or `null` if it found none.
 */
fun parsePythonGuess(output: String): String? = output.lineSequence()
    .map { it.trim() }
    .lastOrNull { it.startsWith(PYTHON_GUESS_MARK) }
    ?.removePrefix(PYTHON_GUESS_MARK)
    ?.trim()
    ?.takeIf { it.startsWith("/") }

/**
 * Check a Python path the user typed for the server.
 *
 * @param path The path as typed.
 * @return `null` if it can be used, else the reason ("must be absolute", ...).
 */
fun pythonPathProblem(path: String): String? {
    val p = path.trim()
    return when {
        p.isEmpty() -> "Enter the path of the Python interpreter on the server."
        !p.startsWith("/") -> "The path must be absolute (start with /)."
        p.any { it == '\n' || it == '\r' || it == '\u0000' } -> "The path must be one line."
        p.endsWith("/") -> "The path must name the interpreter, not a folder."
        else -> null
    }
}

/**
 * Build the remote command that runs the chosen Python once and prints its version.
 *
 * @param python The absolute path of the interpreter on the server.
 * @return One POSIX shell command that prints e.g. "calu-python:3.12.12".
 */
fun pythonVersionCommand(python: String): String =
    "${shellQuote(python)} -c 'import platform; print(\"$PYTHON_GUESS_MARK\" + platform.python_version())'"

/**
 * Read the answer of [pythonVersionCommand].
 *
 * @param output The command's standard output.
 * @return The version, e.g. "3.12.12", or `null` if the interpreter did not answer.
 */
fun parsePythonVersion(output: String): String? = output.lineSequence()
    .map { it.trim() }
    .lastOrNull { it.startsWith(PYTHON_GUESS_MARK) }
    ?.removePrefix(PYTHON_GUESS_MARK)
    ?.takeIf { Regex("\\d+\\.\\d+.*").matches(it) }
