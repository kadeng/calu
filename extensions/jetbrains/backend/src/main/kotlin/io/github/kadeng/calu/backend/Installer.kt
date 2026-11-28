/**
 * Installing calu on the machine of the backend (a local IDE, or the host of JetBrains remote
 * development). The decisions and the commands are `nextInstallAction` of `shared`; this file
 * asks, runs and reports.
 *
 *     ask "Install calu?" ─no─► nothing
 *            │ yes
 *     uv found? ─no─► ask "Install uv?" ─yes─► sh -c '<uv installer>' ─► look for uv again
 *            │ yes
 *     uv tool install --force --python 3.14 <source> ─► notice in the chat, bridge restarted
 *
 * Claude Code is never installed: the chat shows a hint with a link while `claude` is missing.
 * In the SSH and WSL modes calu runs on another machine; there the chat names the command to
 * run in a terminal of that machine.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.platform.ide.progress.withBackgroundProgress
import io.github.kadeng.calu.logic.DEFAULT_INSTALL_SOURCE
import io.github.kadeng.calu.logic.InstallAction
import io.github.kadeng.calu.logic.InstallProgress
import io.github.kadeng.calu.logic.InstallQuestion
import io.github.kadeng.calu.logic.findUv
import io.github.kadeng.calu.logic.nextInstallAction
import io.github.kadeng.calu.logic.remoteInstallScript
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.TimeUnit

/** Longest time one install step may take before it is stopped. */
private const val STEP_TIMEOUT_MINUTES: Long = 15

/**
 * Runs the install flow for one project.
 *
 * @param project The project.
 */
class Installer(private val project: Project) {
    private val log = Logger.getInstance(Installer::class.java)

    /**
     * Ask a yes/no question in a dialog.
     *
     * @param q The question.
     * @return True for the question's button, false for "Cancel".
     */
    private suspend fun ask(q: InstallQuestion): Boolean = withContext(Dispatchers.EDT) {
        Messages.showOkCancelDialog(project, q.detail, "calu: ${q.question}", q.button, "Cancel", Messages.getQuestionIcon()) == Messages.OK
    }

    /**
     * Run one command, with a progress line in the status bar. Its output goes to the IDE's log.
     *
     * @param title The progress text.
     * @param argv The command and its arguments.
     * @return The exit code and the last lines of the output; exit code -1 if the command could
     *   not be started or ran longer than `STEP_TIMEOUT_MINUTES`.
     */
    private suspend fun run(title: String, argv: List<String>): Pair<Int, String> = withBackgroundProgress(project, title) {
        withContext(Dispatchers.IO) {
            try {
                val process = ProcessBuilder(argv).redirectErrorStream(true).directory(File(System.getProperty("user.home"))).start()
                process.outputStream.close()
                // The timer runs beside the read: a command that keeps its output open is stopped too.
                val timedOut = java.util.concurrent.atomic.AtomicBoolean(false)
                val timer = Thread {
                    if (!process.waitFor(STEP_TIMEOUT_MINUTES, TimeUnit.MINUTES)) {
                        timedOut.set(true)
                        process.destroyForcibly()
                    }
                }
                timer.isDaemon = true
                timer.start()
                val output = process.inputStream.bufferedReader().readText()
                process.waitFor()
                val finished = !timedOut.get()
                log.info("[calu] $title: exit ${if (finished) process.exitValue() else -1}\n${output.takeLast(4000)}")
                (if (finished) process.exitValue() else -1) to output.lines().filter { it.isNotBlank() }.takeLast(6).joinToString("\n")
            } catch (error: java.io.IOException) {
                -1 to (error.message ?: "the command could not be started")
            }
        }
    }

    /**
     * Run the whole flow (see the file comment). Side effects: dialogs, downloads, files below
     * the user's home directory, a notice in the chat, and a restart of the bridge after a
     * successful installation.
     *
     * @return True if calu was installed.
     */
    suspend fun install(): Boolean {
        val service = CaluProjectService.of(project)
        val chat = service.controller
        val mode = service.wiring().choice.mode
        if (mode == "ssh" || mode == "wsl") {
            chat.notice("info", "calu runs on another machine in this mode. Run this in a terminal of that machine:\n" + remoteInstallScript(DEFAULT_INSTALL_SOURCE))
            return false
        }
        var progress = InstallProgress(uv = findUv(hostFacts()))
        var tail = ""
        while (true) {
            when (val next = nextInstallAction(progress)) {
                is InstallAction.Ask -> {
                    val yes = ask(next.question)
                    progress = if (next.question.what == "uv") progress.copy(uvAnswer = yes) else progress.copy(caluAnswer = yes)
                }
                is InstallAction.Run -> {
                    val (exit, output) = run(next.step.title, next.step.argv)
                    tail = output
                    progress = if (next.step.what == "uv") progress.copy(uvExit = exit, uv = findUv(hostFacts())) else progress.copy(caluExit = exit)
                }
                is InstallAction.Done -> {
                    if (next.installed) {
                        chat.notice("info", next.message)
                        chat.restartBridge()
                    }
                    return next.installed
                }
                is InstallAction.Failed -> {
                    chat.notice("error", next.message + if (tail.isNotEmpty()) "\n$tail" else "")
                    return false
                }
            }
        }
    }
}
