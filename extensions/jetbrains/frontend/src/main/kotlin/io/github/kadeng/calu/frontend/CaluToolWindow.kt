package io.github.kadeng.calu.frontend

import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.EDT
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.swing.Icon

/**
 * Creates the tool window "calu" (anchored right, see `calu.frontend.xml`): the chat surface
 * and the title actions New session, Checkers, Context, Models, calu settings, Plugin settings.
 */
class CaluToolWindowFactory : ToolWindowFactory, DumbAware {
    override fun shouldBeAvailable(project: Project): Boolean = true

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val service = CaluFrontendService.of(project)
        val surface = NativeChatSurface(project, service)
        Disposer.register(toolWindow.disposable, surface)
        toolWindow.contentManager.addContent(ContentFactory.getInstance().createContent(surface.component, "", false))
        toolWindow.setTitleActions(
            listOf(
                action("New Session", AllIcons.General.Add) { surface.newSession() },
                action("Checker Instructions", AllIcons.Actions.Edit) { chooseChecker(project, service) },
                action("Checker Context", AllIcons.Actions.Preview) { service.act("showContext") },
                action("Models: Use One Model for Everything", AllIcons.Actions.Lightning) { ModelsDialog(project, service).show() },
                action("calu Settings (All)", AllIcons.General.Settings) { showCaluSettings(project, service) },
                action("Plugin Settings: How calu Is Reached", AllIcons.General.GearPlain) { ShowSettingsUtil.getInstance().showSettingsDialog(project, "calu") },
            ),
        )
        service.start()
        service.launch {
            service.state.collect { state -> withContext(Dispatchers.EDT) { if (!project.isDisposed) surface.render(state) } }
        }
    }

    /**
     * Create a title action.
     *
     * @param text The action's name (its tooltip).
     * @param icon The action's icon.
     * @param run What the action does, on the EDT.
     * @return The action.
     */
    private fun action(text: String, icon: Icon, run: () -> Unit): AnAction = object : DumbAwareAction(text, null, icon) {
        override fun actionPerformed(e: AnActionEvent) = run()
    }
}
