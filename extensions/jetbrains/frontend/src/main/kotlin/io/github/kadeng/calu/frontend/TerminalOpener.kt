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
     * Open a new terminal tab named "calu server" in the terminal's start directory (by default
     * the project directory), type a command into it and run it. Must be called on the EDT.
     *
     * No working directory is passed: in remote development the client's project path is a
     * `/$tcp.ij/...` path, and a tab given that path is started as a process of the client,
     * which the platform refuses; without it the tab's shell runs on the host like the
     * terminal's own "+" tab.
     *
     * @param project The project.
     * @param command The command line to run.
     * @throws RuntimeException If the terminal cannot be opened (the terminal plugin is
     *   disabled, or the classic terminal is selected).
     */
    fun open(project: Project, command: String) {
        val tab = TerminalToolWindowTabsManager.getInstance(project).createTabBuilder()
            .tabName("calu server")
            .requestFocus(true)
            .createTab()
        tab.view.createSendTextBuilder().shouldExecute().send(command)
    }
}
