/**
 * How the bridge process `calu ide` is started ("How calu is reached"). Everything above the
 * launcher is the same in every mode:
 *
 *     BridgeSupervisor ── launch() ──► BridgeLauncher ──► BridgeProcess (stdin, stdout, stderr)
 *
 *     LocalLauncher     calu ide                                   in the project directory
 *     WslLauncher       wsl.exe -d <distro> --cd <dir> -- calu ide  Windows IDE, project in WSL
 *     SshLauncher       ssh -T -o BatchMode=yes ... <host> 'calu ide --cwd <dir>'
 *     ScriptedLauncher  no process: a scripted bridge in this JVM (tests, demo)
 *
 * In JetBrains remote development the backend runs on the remote host, so `LocalLauncher` is
 * what runs there. No IntelliJ Platform dependency: the processes are started with
 * `ProcessBuilder`, so that the launchers are tested without an IDE.
 */
package io.github.kadeng.calu.bridge

import io.github.kadeng.calu.logic.ResolveResult
import io.github.kadeng.calu.logic.buildIdeArgv
import io.github.kadeng.calu.logic.shellJoin
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.TimeUnit

/**
 * The bridge could not be started.
 *
 * @property missing True if calu itself was not found (status "missing"), false for every
 *   other failure (status "error").
 * @param message What the user is told.
 */
class LaunchException(val missing: Boolean, message: String) : Exception(message)

/** A started bridge: three byte streams and the end of the process. */
interface BridgeProcess {
    /** The bridge's standard input (NDJSON requests, UTF-8). */
    val stdin: OutputStream

    /** The bridge's standard output (NDJSON responses and notifications, UTF-8). */
    val stdout: InputStream

    /** The bridge's standard error (log text, UTF-8). */
    val stderr: InputStream

    /**
     * Wait for the process to end.
     *
     * @param timeoutMs Longest wait in milliseconds; 0 or less waits without limit.
     * @return The exit code, or `null` if the process still runs after the timeout.
     * @throws InterruptedException if the waiting thread is interrupted.
     */
    fun waitFor(timeoutMs: Long): Int?

    /**
     * End the process and its descendants. Does nothing if it has ended.
     *
     * @param force True to kill at once; false to ask the process to terminate.
     */
    fun destroy(force: Boolean)
}

/** One way to start the bridge. Implementations hold their settings and no running state. */
interface BridgeLauncher {
    /** Short name of the mode for messages: "local", "wsl", "ssh" or "scripted". */
    val kind: String

    /**
     * True if an unexpected end of the process is followed by new starts with a growing wait
     * (a network connection that can drop); false if the user restarts the bridge.
     */
    val reconnects: Boolean

    /** One line that says what is started, for the log; never contains a secret. */
    fun describe(): String

    /**
     * Start the bridge.
     *
     * @return The started process.
     * @throws LaunchException if the command cannot be found or started.
     */
    fun launch(): BridgeProcess

    /**
     * Explain an unexpected end of the process in the user's terms.
     *
     * @param exitCode The exit code.
     * @param stderrTail The last text the process wrote to stderr (at most a few thousand characters).
     * @return The diagnosis, or `null` if this launcher has nothing to add to "exited with code N".
     */
    fun diagnose(exitCode: Int, stderrTail: String): Diagnosis? = null

    /**
     * What the user reads while the bridge starts.
     *
     * @return The sentence, or `null` for the default "Starting calu…".
     */
    fun startingText(): String? = null
}

/**
 * What a launcher says about an ended process.
 *
 * @property message What the user is told, with what to do.
 * @property retry False if starting again cannot help before the user changed something (e.g.
 *   the key is not in the SSH agent); true if a later start may succeed.
 */
data class Diagnosis(val message: String, val retry: Boolean)

/**
 * A bridge that is an operating system process.
 *
 * @param process The started process.
 */
class OsBridgeProcess(private val process: Process) : BridgeProcess {
    override val stdin: OutputStream get() = process.outputStream
    override val stdout: InputStream get() = process.inputStream
    override val stderr: InputStream get() = process.errorStream

    override fun waitFor(timeoutMs: Long): Int? =
        if (timeoutMs <= 0) process.waitFor() else if (process.waitFor(timeoutMs, TimeUnit.MILLISECONDS)) process.exitValue() else null

    override fun destroy(force: Boolean) {
        // A wrapper such as `uv run` starts calu as a child: end the children first.
        val handles = process.toHandle().descendants().toList() + process.toHandle()
        for (h in handles) if (force) h.destroyForcibly() else h.destroy()
    }
}

/**
 * Start a process with pipes for all three streams.
 *
 * @param argv The command line; `argv[0]` is the program.
 * @param cwd The working directory, or `null` for this process's own.
 * @param what Name of the thing started, for the error message (e.g. "calu ide").
 * @return The started process.
 * @throws LaunchException (not `missing`) if the program cannot be started.
 */
fun startProcess(argv: List<String>, cwd: String?, what: String): BridgeProcess {
    if (cwd != null && !File(cwd).isDirectory) throw LaunchException(false, "Cannot start $what: the directory $cwd does not exist.")
    try {
        val builder = ProcessBuilder(argv)
        if (cwd != null) builder.directory(File(cwd))
        return OsBridgeProcess(builder.start())
    } catch (error: Exception) {
        throw LaunchException(false, "Cannot start $what (${argv.firstOrNull() ?: ""}): ${error.message}")
    }
}

/**
 * `calu ide` as a child process in the project directory.
 *
 * @param resolve Finds the calu command when the bridge is started (setting, PATH, ~/.local/bin).
 * @param cwd The project directory.
 */
class LocalLauncher(private val resolve: () -> ResolveResult, private val cwd: String) : BridgeLauncher {
    override val kind: String = "local"
    override val reconnects: Boolean = false

    override fun describe(): String = when (val r = resolve()) {
        is ResolveResult.Found -> "${buildIdeArgv(r.command).joinToString(" ")} (in $cwd, found by ${r.source})"
        is ResolveResult.NotFound -> "calu ide (calu not found)"
    }

    override fun launch(): BridgeProcess = when (val r = resolve()) {
        is ResolveResult.NotFound -> throw LaunchException(true, r.message)
        is ResolveResult.Found -> startProcess(buildIdeArgv(r.command), cwd, "calu ide")
    }
}

/**
 * Build the command line that runs the bridge inside a WSL distribution from Windows.
 *
 * @param distro Name of the distribution, e.g. "Ubuntu".
 * @param linuxDir The project directory as the distribution sees it, e.g. "/home/me/project".
 * @param calu The calu command inside the distribution; empty stands for `calu`.
 * @param wslExe The WSL launcher program.
 * @return `[wslExe, "-d", distro, "--cd", linuxDir, "--", ...calu, "ide"]`.
 */
fun wslArgv(distro: String, linuxDir: String, calu: List<String>, wslExe: String = "wsl.exe"): List<String> =
    listOf(wslExe, "-d", distro, "--cd", linuxDir, "--") + buildIdeArgv(calu.filter { it.isNotEmpty() }.ifEmpty { listOf("calu") })

/**
 * A Windows path of a directory inside WSL, split into its parts.
 *
 * @property distro Name of the distribution.
 * @property linuxPath The path inside the distribution, with "/" separators.
 */
data class WslLocation(val distro: String, val linuxPath: String)

/**
 * Read a Windows path that names a directory inside a WSL distribution.
 *
 * @param path A path as the Windows IDE has it, e.g. `\\wsl.localhost\Ubuntu\home\me\p`,
 *   `\\wsl$\Ubuntu\home\me\p` or the same with "/" separators.
 * @return The distribution and the Linux path ("/" for the distribution's root), or `null` if
 *   the path is not below `\\wsl.localhost\` or `\\wsl$\`.
 *
 * Example: `parseWslPath("//wsl.localhost/Ubuntu/home/me") == WslLocation("Ubuntu", "/home/me")`.
 */
fun parseWslPath(path: String): WslLocation? {
    val p = path.replace('\\', '/')
    val rest = listOf("//wsl.localhost/", "//wsl$/").firstNotNullOfOrNull { prefix ->
        if (p.startsWith(prefix, ignoreCase = true)) p.substring(prefix.length) else null
    } ?: return null
    val parts = rest.split('/').filter { it.isNotEmpty() }
    if (parts.isEmpty()) return null
    return WslLocation(parts[0], "/" + parts.drop(1).joinToString("/"))
}

/**
 * Write a Linux path of a WSL distribution the way Windows opens it.
 *
 * @param distro Name of the distribution.
 * @param linuxPath An absolute path inside the distribution.
 * @return `\\wsl.localhost\<distro>\<path with backslashes>`.
 */
fun wslToWindowsPath(distro: String, linuxPath: String): String =
    "\\\\wsl.localhost\\" + distro + linuxPath.replace('/', '\\')

/**
 * `calu ide` inside a WSL distribution, started from a Windows IDE.
 *
 * @param distro Name of the distribution.
 * @param linuxDir The project directory inside the distribution.
 * @param calu The calu command inside the distribution; empty stands for `calu` on its PATH.
 * @param wslExe The WSL launcher program (replaced in tests).
 */
class WslLauncher(
    private val distro: String,
    private val linuxDir: String,
    private val calu: List<String>,
    private val wslExe: String = "wsl.exe",
) : BridgeLauncher {
    override val kind: String = "wsl"
    override val reconnects: Boolean = false
    override fun describe(): String = wslArgv(distro, linuxDir, calu, wslExe).joinToString(" ")
    override fun launch(): BridgeProcess = startProcess(wslArgv(distro, linuxDir, calu, wslExe), null, "calu ide in WSL")

    override fun diagnose(exitCode: Int, stderrTail: String): Diagnosis? =
        if (stderrTail.contains("not found", ignoreCase = true) || exitCode == 127) {
            Diagnosis(
                "calu was not found inside the WSL distribution $distro. Install it there with `uv tool install`, " +
                    "or set the calu command (Settings | Tools | calu) to its full path inside the distribution.",
                false,
            )
        } else {
            null
        }
}

/**
 * Settings of the SSH mode.
 *
 * @property host The SSH destination as `ssh` takes it: a host alias of `~/.ssh/config`, or `user@host`.
 * @property remoteDir The project directory on the server (absolute).
 * @property extraOptions Further arguments for `ssh`, placed before the host (e.g. `-p`, `2222`).
 * @property calu The calu command on the server; empty stands for `calu` on the login shell's PATH.
 * @property sshExecutable The OpenSSH client program.
 */
data class SshSettings(
    val host: String,
    val remoteDir: String,
    val extraOptions: List<String> = emptyList(),
    val calu: List<String> = emptyList(),
    val sshExecutable: String = "ssh",
)

/** Seconds `ssh` waits for the server to accept the connection. */
const val SSH_CONNECT_TIMEOUT_S: Int = 15

/**
 * Build the command line of the SSH mode.
 *
 * `-T`: no terminal, the streams carry the protocol. `BatchMode=yes`: ssh never asks for a
 * passphrase or password on the protocol streams; it fails instead, and `SshLauncher.diagnose`
 * says what to do. `ConnectTimeout`: a host that does not answer fails after
 * [SSH_CONNECT_TIMEOUT_S] seconds instead of the system's two minutes. The two `ServerAlive` options end a dead connection after about 45 seconds,
 * so that the supervisor can connect again. The user's `~/.ssh/config` applies as for any `ssh`.
 * The remote command is one argument, quoted for the server's POSIX shell.
 *
 * @param s The settings.
 * @return `[ssh, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o",
 *   "ServerAliveCountMax=3", ...extraOptions, host, "<calu> ide --cwd <remoteDir>"]`.
 * @throws IllegalArgumentException if the host is empty or starts with "-", or the directory is empty.
 */
fun sshArgv(s: SshSettings): List<String> {
    val host = s.host.trim()
    require(host.isNotEmpty()) { "The SSH host is not set (Settings | Tools | calu)." }
    require(!host.startsWith("-")) { "The SSH host must not start with \"-\"." }
    require(s.remoteDir.trim().isNotEmpty()) { "The remote project directory is not set (Settings | Tools | calu)." }
    val calu = s.calu.filter { it.isNotEmpty() }.ifEmpty { listOf("calu") }
    val remote = shellJoin(buildIdeArgv(calu) + listOf("--cwd", s.remoteDir.trim()))
    return listOf(s.sshExecutable, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=$SSH_CONNECT_TIMEOUT_S", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3") +
        s.extraOptions.filter { it.isNotEmpty() } + listOf(host, remote)
}

/**
 * Explain why `ssh` ended, from its exit code and its last messages.
 *
 * @param host The SSH destination, for the message.
 * @param exitCode The exit code of `ssh` (255 for its own failures, else the remote command's).
 * @param stderrTail The last text on stderr.
 * @return The diagnosis for the known cases (sign-in needs a passphrase or password, unknown
 *   host key, name not resolved, connection refused or timed out, calu missing on the server,
 *   a bridge without `--cwd`), or `null`.
 */
fun diagnoseSsh(host: String, exitCode: Int, stderrTail: String): Diagnosis? {
    val t = stderrTail.lowercase()
    return when {
        "permission denied" in t || "passphrase" in t || "no more authentication methods" in t -> Diagnosis(
            "SSH could not sign in to $host without asking for a passphrase or password. The plugin never " +
                "types one: add the key to your SSH agent (`ssh-add`), then choose \"Restart bridge\". " +
                "Check with `ssh -o BatchMode=yes $host true` in a terminal.",
            false,
        )
        "host key verification failed" in t -> Diagnosis(
            "SSH does not know the host key of $host. Connect once in a terminal (`ssh $host`) and accept " +
                "the key, then choose \"Restart bridge\".",
            false,
        )
        "could not resolve hostname" in t -> Diagnosis("SSH could not resolve the host name $host. Check the SSH host in Settings | Tools | calu and your network.", true)
        "connection refused" in t || "connection timed out" in t || "operation timed out" in t || "no route to host" in t || "network is unreachable" in t ->
            Diagnosis("SSH could not connect to $host (${stderrTail.trim().lines().lastOrNull().orEmpty()}).", true)
        exitCode == 127 || "command not found" in t -> Diagnosis(
            "calu was not found on $host. Install it there with `uv tool install`, or set the calu command " +
                "(Settings | Tools | calu) to its full path on the server.",
            false,
        )
        "unrecognized arguments: --cwd" in t || "no such option: --cwd" in t -> Diagnosis(
            "The calu on $host does not know `calu ide --cwd` yet. Update calu on the server.",
            false,
        )
        exitCode == 255 -> Diagnosis("The SSH connection to $host ended (${stderrTail.trim().lines().lastOrNull().orEmpty()}).", true)
        else -> null
    }
}

/**
 * The SSH mode: the IDE and a local copy of the project are on this machine; calu runs on a
 * server that is reached with the system's OpenSSH client only.
 *
 * @param settings Host, remote directory and options.
 */
class SshLauncher(private val settings: SshSettings) : BridgeLauncher {
    override val kind: String = "ssh"
    override val reconnects: Boolean = true

    override fun describe(): String = try {
        sshArgv(settings).joinToString(" ")
    } catch (error: IllegalArgumentException) {
        "ssh (not configured: ${error.message})"
    }

    override fun launch(): BridgeProcess {
        val argv = try {
            sshArgv(settings)
        } catch (error: IllegalArgumentException) {
            throw LaunchException(false, error.message ?: "The SSH mode is not configured.")
        }
        return startProcess(argv, null, "ssh")
    }

    override fun diagnose(exitCode: Int, stderrTail: String): Diagnosis? = diagnoseSsh(settings.host, exitCode, stderrTail)

    override fun startingText(): String = "Connecting to ${settings.host.trim()} over SSH…"
}

/**
 * How long to wait before start number `attempt` after an unexpected end of the bridge.
 *
 * @param attempt 1 for the first new start, 2 for the second, ...
 * @return 500 ms, doubled per attempt, at most 30 000 ms.
 */
fun reconnectDelayMs(attempt: Int): Long = minOf(30_000L, 500L shl (attempt - 1).coerceIn(0, 10))
