package io.github.kadeng.calu.frontend

import com.intellij.openapi.project.Project
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager

/**
 * Opens the IDE's terminal with a command (the one use of the terminal plugin's API: the
 * Reworked Terminal API of 2025.3 and later, which belongs to the IDE's window and therefore
 * to the frontend).
 */
object TerminalOpener {
    /**
     * Open a new terminal tab named "calu server" in the project directory, type a command into
     * it and run it. Must be called on the EDT.
     *
     * @param project The project.
     * @param command The command line to run.
     * @throws RuntimeException If the terminal cannot be opened (the terminal plugin is
     *   disabled, or the classic terminal is selected).
     */
    fun open(project: Project, command: String) {
        val tab = TerminalToolWindowTabsManager.getInstance(project).createTabBuilder()
            .workingDirectory(project.basePath)
            .tabName("calu server")
            .requestFocus(true)
            .createTab()
        tab.view.createSendTextBuilder().shouldExecute().send(command)
    }
}
