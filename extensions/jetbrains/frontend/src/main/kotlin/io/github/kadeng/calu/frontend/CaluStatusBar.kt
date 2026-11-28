package io.github.kadeng.calu.frontend

import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.util.Consumer
import io.github.kadeng.calu.logic.decisionsStatus
import io.github.kadeng.calu.state.ViewState
import java.awt.Component
import java.awt.event.MouseEvent

/** Id of the status bar widget. */
const val STATUS_WIDGET_ID: String = "calu.decisions"

/**
 * Build the widget's text.
 *
 * @param open Number of steps that wait for a decision.
 * @return "" for none (the widget then takes no room), else "calu: N waiting".
 */
fun statusText(open: Int): String = if (open <= 0) "" else "calu: $open waiting"

/**
 * Build the widget's whole text: the open decisions (or "server stopped" / "session ended",
 * `decisionsStatus`) and, in SSH mode, the file synchronisation.
 *
 * @param state The view state.
 * @return The parts that are not empty, joined by " · ".
 */
fun statusLine(state: ViewState): String = listOf(decisionsStatus(state), state.sync?.text.orEmpty()).filter { it.isNotEmpty() }.joinToString(" · ")

/** Registers the status bar widget that counts the open decisions. */
class CaluStatusBarWidgetFactory : StatusBarWidgetFactory {
    override fun getId(): String = STATUS_WIDGET_ID
    override fun getDisplayName(): String = "calu: Open Decisions"
    override fun isAvailable(project: Project): Boolean = true
    override fun createWidget(project: Project): StatusBarWidget = CaluStatusBarWidget(project)
}

/**
 * Shows how many steps wait for the user's decision; a click opens the tool window.
 * In the SSH mode the text also shows the state of the file synchronisation, and the tooltip
 * lists its message, conflicts, held files and files that are not synchronised.
 * `CaluFrontendService` asks the status bar to update it when the count changes.
 *
 * @param project The project.
 */
class CaluStatusBarWidget(private val project: Project) : StatusBarWidget, StatusBarWidget.TextPresentation {
    override fun ID(): String = STATUS_WIDGET_ID
    override fun getPresentation(): StatusBarWidget.WidgetPresentation = this
    override fun install(statusBar: StatusBar) {}
    override fun dispose() {}
    override fun getText(): String = statusLine(CaluFrontendService.of(project).state.value)
    override fun getAlignment(): Float = Component.CENTER_ALIGNMENT
    override fun getTooltipText(): String = CaluFrontendService.of(project).state.value.sync?.let { s ->
        (listOfNotNull(s.message) + s.conflicts.map { "conflict: $it" } + s.held.map { "held: $it" } + s.notes.map { "not synchronised: $it" }).take(12).joinToString("<br>").ifEmpty { s.text } + "<br>Click to open the chat."
    } ?: if (decisionsStatus(CaluFrontendService.of(project).state.value) == "calu: server stopped") {
        "The calu server stopped; the session ended with it. Click to open the chat."
    } else {
        "Steps of Claude that wait for your decision in calu. Click to open the chat."
    }
    override fun getClickConsumer(): Consumer<MouseEvent> = Consumer { ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.activate(null) }
}
