/**
 * Paths and command lines at the edge between the machine of the IDE and the machine of calu.
 *
 *     mode     calu's path                 the IDE opens
 *     local    /home/me/p/src/a.py         the same path
 *     wsl      /home/me/p/src/a.py         \\wsl.localhost\Ubuntu\home\me\p\src\a.py
 *     ssh      /srv/p/src/a.py             <local project copy>/src/a.py   (only below the remote root)
 *
 * Pure: no file system access.
 */
package io.github.kadeng.calu.logic

/**
 * Split a settings text into arguments.
 *
 * @param text Arguments separated by white space; single or double quotes group white space
 *   into one argument (the quotes are removed; no escape characters).
 * @return The arguments; empty for blank text.
 *
 * Example: `splitArgs("uv run --project '/my dir' calu") == ["uv", "run", "--project", "/my dir", "calu"]`.
 */
fun splitArgs(text: String): List<String> {
    val out = ArrayList<String>()
    val current = StringBuilder()
    var quote: Char? = null
    var started = false
    for (c in text) {
        when {
            quote != null -> if (c == quote) quote = null else current.append(c)
            c == '"' || c == '\'' -> { quote = c; started = true }
            c.isWhitespace() -> if (started || current.isNotEmpty()) { out.add(current.toString()); current.setLength(0); started = false }
            else -> current.append(c)
        }
    }
    if (started || current.isNotEmpty()) out.add(current.toString())
    return out
}

/**
 * Resolve a path that calu names against the session's directory.
 *
 * @param path A path from calu: absolute, or relative to the session's directory.
 * @param workspace The session's directory on calu's machine; `null` if unknown.
 * @return The absolute path on calu's machine with "/" separators; the path itself if it is
 *   absolute or no workspace is known.
 */
fun absoluteRemotePath(path: String, workspace: String?): String =
    if (path.startsWith("/") || workspace.isNullOrEmpty()) path else workspace.trimEnd('/') + "/" + path

/**
 * Translate an absolute path on calu's machine into the path the IDE's machine opens in SSH mode.
 *
 * @param remotePath An absolute path on the server.
 * @param remoteRoot The project directory on the server.
 * @param localRoot The directory of the local copy.
 * @return The local path, or `null` if `remotePath` is not the remote root or below it (such a
 *   file has no local copy) or contains a ".." segment.
 */
fun sshLocalPath(remotePath: String, remoteRoot: String, localRoot: String): String? {
    val root = remoteRoot.trimEnd('/')
    if (root.isEmpty() || remotePath.split('/').any { it == ".." }) return null
    if (remotePath == root) return localRoot
    if (!remotePath.startsWith("$root/")) return null
    val rest = remotePath.substring(root.length + 1)
    val sep = if (localRoot.contains('\\') && !localRoot.contains('/')) '\\' else '/'
    return localRoot.trimEnd('/', '\\') + sep + rest.replace('/', sep)
}

/**
 * Build the command for a terminal on the IDE's machine that starts the calu server where calu runs.
 *
 * @param serverCommand The POSIX shell command of `buildServerHelp` (tmux, screen or plain).
 * @param mode "local", "wsl" or "ssh".
 * @param wslDistro The distribution (mode "wsl").
 * @param sshHost The SSH destination (mode "ssh").
 * @param sshOptions Further `ssh` arguments (mode "ssh").
 * @return For "local" the command itself; for "wsl" `wsl.exe -d <distro> -- sh -lc '<command>'`;
 *   for "ssh" `ssh -t <options> <host> '<command>'` (`-t`: tmux, screen and sudo need a terminal).
 */
fun terminalCommand(serverCommand: String, mode: String, wslDistro: String = "", sshHost: String = "", sshOptions: List<String> = emptyList()): String =
    when (mode) {
        "wsl" -> "wsl.exe -d ${shellQuote(wslDistro)} -- sh -lc ${shellQuote(serverCommand)}"
        "ssh" -> shellJoin(listOf("ssh", "-t") + sshOptions.filter { it.isNotEmpty() } + listOf(sshHost, serverCommand))
        else -> serverCommand
    }
