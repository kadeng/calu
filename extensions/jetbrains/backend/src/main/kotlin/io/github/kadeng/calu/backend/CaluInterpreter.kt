/**
 * The offer "Set up running and debugging on <host>" of the SSH mode: PyCharm's SSH interpreter
 * with the path mapping local copy <-> server folder.
 *
 *     bridge running, SSH mode, no matching interpreter ──► ui.interpreter {phase: "offer"} (a card)
 *     the user clicks the card
 *       ssh <host> '<guess>'          the project's .venv on the server, else python3 on its PATH
 *       dialog: "Python on <host>:"   prefilled with the guess; the user confirms or corrects it
 *       ssh <host> '<python> -c ...'  the interpreter runs and tells its version
 *       SshInterpreterSetup.create    (module calu.python) SSH configuration, deployment server
 *                                     with the mapping, SDK; the project's interpreter
 *     ──► the card goes away (a matching interpreter exists now), a line in the chat says so
 *
 * The work that needs PyCharm's Python classes is in the optional module `calu.python` behind
 * [SshInterpreterSetup]; without it (an IDE without Python support) no card is shown. The
 * `ssh` runs here use the plugin's own settings (host, extra options) with `BatchMode`, as the
 * bridge's connection does, so they never ask for a passphrase.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.extensions.ExtensionPointName
import com.intellij.openapi.project.Project
import com.intellij.openapi.projectRoots.ProjectJdkTable
import com.intellij.openapi.roots.ModuleRootEvent
import com.intellij.openapi.roots.ModuleRootListener
import com.intellij.openapi.ui.InputValidator
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.Disposer
import io.github.kadeng.calu.bridge.SSH_CONNECT_TIMEOUT_S
import io.github.kadeng.calu.chat.ChatController
import io.github.kadeng.calu.chat.ChatUpdate
import io.github.kadeng.calu.logic.SshEndpoint
import io.github.kadeng.calu.logic.parsePythonGuess
import io.github.kadeng.calu.logic.parsePythonVersion
import io.github.kadeng.calu.logic.pythonGuessCommand
import io.github.kadeng.calu.logic.pythonPathProblem
import io.github.kadeng.calu.logic.pythonVersionCommand
import io.github.kadeng.calu.logic.splitArgs
import io.github.kadeng.calu.logic.sshEndpoint
import io.github.kadeng.calu.remote.PluginSettingsDto
import io.github.kadeng.calu.state.BridgeStatus
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.util.concurrent.TimeUnit

/**
 * Where an SSH interpreter runs and which folders it maps.
 *
 * @property endpoint Host (as the settings name it), user and port of the server.
 * @property remoteDir The project directory on the server.
 * @property localDir The project folder on this machine (the local copy).
 */
data class SshInterpreterTarget(val endpoint: SshEndpoint, val remoteDir: String, val localDir: String)

/**
 * PyCharm's side of the SSH interpreter, implemented by the optional module `calu.python`
 * (registered at the extension point `io.github.kadeng.calu.sshInterpreterSetup`).
 */
interface SshInterpreterSetup {
    /**
     * Find the interpreter of the project (the project's or a module's) that runs on the target's
     * server and maps the local folder to the target's directory.
     *
     * Runs in a read action of the caller's choosing; it changes nothing.
     *
     * @param project The project.
     * @param target The server and the folders.
     * @return The interpreter's name, or `null` if the project has none that matches.
     */
    fun findMatching(project: Project, target: SshInterpreterTarget): String?

    /**
     * Create the SSH interpreter and make it the project's: PyCharm's SSH configuration for the
     * server (an existing one of the same host, user and port is reused), a deployment server
     * with the path mapping local folder <-> remote directory and automatic upload off, and the
     * Python SDK on that server; the project and its modules use it from now on.
     *
     * It can be called from any coroutine: the implementation switches to the EDT itself and
     * takes the write actions it needs.
     *
     * @param project The project.
     * @param target The server and the folders.
     * @param python The absolute path of the interpreter on the server, as the user confirmed it.
     * @param version The interpreter's version ("3.12.12"), or `null` if unknown.
     * @return The name of the new (or reused) interpreter.
     * @throws Exception if the IDE refused a step. A deployment server that this call created is
     *   removed again. An SSH configuration that this call created and path mappings it changed
     *   on an existing deployment server stay.
     */
    suspend fun create(project: Project, target: SshInterpreterTarget, python: String, version: String?): String

    companion object {
        /** The extension point. */
        val EP_NAME: ExtensionPointName<SshInterpreterSetup> = ExtensionPointName.create("io.github.kadeng.calu.sshInterpreterSetup")
    }
}

/**
 * The offer of an SSH interpreter for one wiring of the SSH mode.
 *
 * @param project The project.
 * @param controller The chat, which shows the card (`ui.interpreter`) and the result.
 * @param settings The plugin's settings of this wiring (mode "ssh").
 * @param scope The project service's scope.
 * @param log Writes a line to the IDE's log.
 */
class CaluInterpreter(
    private val project: Project,
    private val controller: ChatController,
    private val settings: PluginSettingsDto,
    private val scope: CoroutineScope,
    private val log: (String) -> Unit,
) {
    private val disposable = Disposer.newDisposable("calu interpreter offer")
    private val checks = Channel<Unit>(Channel.CONFLATED)
    private var job: Job? = null

    @Volatile private var busy = false

    @Volatile private var endpoint: SshEndpoint? = null

    @Volatile private var shown: String? = null

    /** Start: check again when the bridge's status changes and when the project's interpreters change. */
    fun start() {
        val bus = project.messageBus.connect(disposable)
        bus.subscribe(ModuleRootListener.TOPIC, object : ModuleRootListener {
            override fun rootsChanged(event: ModuleRootEvent) { checks.trySend(Unit) }
        })
        bus.subscribe(ProjectJdkTable.JDK_TABLE_TOPIC, object : ProjectJdkTable.Listener {
            override fun jdkAdded(jdk: com.intellij.openapi.projectRoots.Sdk) { checks.trySend(Unit) }
            override fun jdkRemoved(jdk: com.intellij.openapi.projectRoots.Sdk) { checks.trySend(Unit) }
        })
        job = scope.launch {
            launch {
                var status: String? = null
                controller.updates().collect { update ->
                    val now = when (update) {
                        is ChatUpdate.Snapshot -> update.state.bridge.status
                        is ChatUpdate.Event -> if (update.event.method == "bridge.status") controller.state.bridge.status else status
                    }
                    if (now != status) { status = now; checks.trySend(Unit) }
                }
            }
            for (tick in checks) {
                try {
                    check()
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Exception) {
                    log("[calu] interpreter offer: the check failed: ${error.message}")
                }
            }
        }
    }

    /** Stop listening. The card is left as it is; the next wiring replaces the chat. */
    fun dispose() {
        job?.cancel()
        Disposer.dispose(disposable)
    }

    /**
     * Decide whether the card is shown and dispatch `ui.interpreter` if that changed.
     */
    private suspend fun check() {
        if (busy) return
        val setup = SshInterpreterSetup.EP_NAME.extensionList.firstOrNull()
        val running = controller.state.bridge.status == BridgeStatus.RUNNING
        val target = if (setup != null && running) target() else null
        val matching = if (setup != null && target != null) {
            com.intellij.openapi.application.readAction { setup.findMatching(project, target) }
        } else {
            null
        }
        show(if (setup != null && target != null && matching == null) "offer" else null)
    }

    /**
     * Set the card's phase.
     *
     * @param phase "offer", "working", "failed", or `null` to remove the card.
     * @param message For "failed": the reason.
     */
    private fun show(phase: String?, message: String? = null) {
        val key = phase?.let { "$it:$message" }
        if (key == shown) return
        shown = key
        controller.dispatch(
            "ui.interpreter",
            if (phase == null) {
                buildJsonObject { }
            } else {
                buildJsonObject {
                    put("host", JsonPrimitive(settings.sshHost.trim()))
                    put("directory", JsonPrimitive(settings.sshDirectory.trim()))
                    put("phase", JsonPrimitive(phase))
                    if (message != null) put("message", JsonPrimitive(message))
                }
            },
        )
    }

    /**
     * Return the server and folders of the interpreter; the endpoint is asked from `ssh -G`
     * once per wiring.
     *
     * @return The target, or `null` if the project has no local folder or the settings no directory.
     */
    private suspend fun target(): SshInterpreterTarget? {
        val local = project.basePath ?: return null
        val remote = settings.sshDirectory.trim().takeIf { it.isNotEmpty() } ?: return null
        val known = endpoint ?: withContext(Dispatchers.IO) {
            val dump = runSsh(listOf("ssh", "-G") + splitArgs(settings.sshOptions) + listOf(settings.sshHost.trim()), 10)?.second
            sshEndpoint(settings.sshHost, dump, System.getProperty("user.name") ?: "root")
        }.also { endpoint = it }
        return SshInterpreterTarget(known, remote, local)
    }

    /**
     * The card's button: guess the Python on the server, let the user confirm the path, check
     * that it runs, and create the interpreter. Every failure ends on the card ("failed") and in
     * the chat; a cancel of the dialog leaves the offer as it was.
     */
    fun setUp() {
        if (busy) return
        val setup = SshInterpreterSetup.EP_NAME.extensionList.firstOrNull() ?: run {
            controller.notice("warning", "This IDE has no Python support: an SSH interpreter cannot be created here.")
            return
        }
        busy = true
        scope.launch {
            try {
                show("working")
                val target = target() ?: throw IllegalStateException("The remote project directory is not set (Settings | Tools | calu).")
                val host = settings.sshHost.trim()
                val guess = withContext(Dispatchers.IO) { remote(pythonGuessCommand(target.remoteDir), 30) }?.let { parsePythonGuess(it) }
                val python = withContext(Dispatchers.EDT) { askPython(host, target.remoteDir, guess) }
                if (python == null) {
                    show("offer")
                    return@launch
                }
                val version = withContext(Dispatchers.IO) { remote(pythonVersionCommand(python), 30) }?.let { parsePythonVersion(it) }
                    ?: throw IllegalStateException("$python did not run on $host (checked with `ssh $host '$python -V'`).")
                val name = setup.create(project, target, python, version)
                controller.notice(
                    "info",
                    "The SSH interpreter \"$name\" (Python $version on $host) is the project's interpreter now. " +
                        "Its path mapping joins ${target.localDir} and ${target.remoteDir}; automatic upload is off, because calu keeps both copies equal. " +
                        "Run and Debug start the code on $host.",
                )
                // The card was "working": the check replaces it (no card when the new
                // interpreter matches, which is expected).
                busy = false
                check()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                log("[calu] interpreter: ${error.javaClass.simpleName}: ${error.message}")
                show("failed", error.message ?: error.javaClass.simpleName)
                controller.notice("warning", "The SSH interpreter was not created: ${error.message ?: error.javaClass.simpleName}")
            } finally {
                busy = false
            }
        }
    }

    /**
     * Ask the user for the Python on the server, prefilled with the guess.
     *
     * @param host The SSH host, for the text.
     * @param remoteDir The project directory on the server, for the text.
     * @param guess The guessed path, or `null` if none was found.
     * @return The confirmed absolute path, or `null` if the user cancelled.
     */
    private fun askPython(host: String, remoteDir: String, guess: String?): String? {
        val found = if (guess != null) "Found on the server: $guess." else "No Python was found on the server's PATH or in $remoteDir/.venv."
        val text = "$found\n\nThe Python interpreter on $host that runs and debugs this project:"
        val validator = object : InputValidator {
            override fun checkInput(inputString: String?): Boolean = pythonPathProblem(inputString.orEmpty()) == null
            override fun canClose(inputString: String?): Boolean = checkInput(inputString)
        }
        return Messages.showInputDialog(project, text, "Set up Running and Debugging on $host", Messages.getQuestionIcon(), guess ?: "/usr/bin/python3", validator)?.trim()
    }

    /**
     * Run one command on the server over SSH with the plugin's settings.
     *
     * @param command The remote command (one POSIX shell command line).
     * @param timeoutSeconds How long to wait.
     * @return Its standard output, or `null` if ssh failed or timed out.
     */
    private fun remote(command: String, timeoutSeconds: Long): String? {
        val argv = listOf("ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=$SSH_CONNECT_TIMEOUT_S") +
            splitArgs(settings.sshOptions) + listOf(settings.sshHost.trim(), command)
        val result = runSsh(argv, timeoutSeconds) ?: return null
        return result.second.takeIf { result.first == 0 }
    }

    /**
     * Run a local program and collect its output.
     *
     * @param argv The program and its arguments.
     * @param timeoutSeconds How long to wait before it is killed.
     * @return The exit code and the standard output, or `null` if it could not start or timed out.
     */
    private fun runSsh(argv: List<String>, timeoutSeconds: Long): Pair<Int, String>? = try {
        val process = ProcessBuilder(argv).redirectError(ProcessBuilder.Redirect.DISCARD).start().also { it.outputStream.close() }
        val out = StringBuilder()
        val reader = Thread { out.append(process.inputStream.bufferedReader().readText()) }.apply { isDaemon = true; start() }
        if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            log("[calu] interpreter: ${argv.first()} timed out")
            null
        } else {
            reader.join(2000)
            process.exitValue() to out.toString()
        }
    } catch (error: java.io.IOException) {
        log("[calu] interpreter: ${argv.first()} could not start: ${error.message}")
        null
    }
}
