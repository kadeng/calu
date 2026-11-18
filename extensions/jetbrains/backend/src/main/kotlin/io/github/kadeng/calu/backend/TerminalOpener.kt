package io.github.kadeng.calu.backend

import com.intellij.openapi.project.Project
import org.jetbrains.plugins.terminal.TerminalToolWindowManager

/** Opens the IDE's terminal with a command (the one use of the terminal plugin's API). */
object TerminalOpener {
    /**
     * Open a new terminal tab named "calu server" in the project directory and run a command in it.
     * Must be called on the EDT.
     *
     * @param project The project.
     * @param command The command line to run.
     * @return True if the tab was opened and the command was sent.
     */
    fun open(project: Project, command: String): Boolean {
        val widget = TerminalToolWindowManager.getInstance(project).createShellWidget(project.basePath, "calu server", true, true)
        widget.sendCommandToExecute(command)
        return true
    }
}
