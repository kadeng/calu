package io.github.kadeng.calu.backend

import com.intellij.ide.projectView.PresentationData
import com.intellij.ide.projectView.ProjectViewNode
import com.intellij.ide.projectView.ProjectViewNodeDecorator
import com.intellij.ui.JBColor
import io.github.kadeng.calu.sync.SyncMark

/**
 * Marks the files of a synchronised copy (SSH mode, "Sync: through calu") in the project view:
 * a file in conflict in red with "sync conflict: …", a folder that contains one with "contains a
 * sync conflict", a file whose local change waits for the agent's turn with "held until the turn
 * ends". It reads the synchronisation's current status only; it never creates the plugin's
 * wiring or starts a bridge. `CaluSync` refreshes the project view when the marks change.
 */
class SyncProjectViewDecorator : ProjectViewNodeDecorator {
    override fun decorate(node: ProjectViewNode<*>, data: PresentationData) {
        val project = node.project ?: return
        if (project.isDisposed) return
        val file = node.virtualFile ?: return
        val sync = project.getServiceIfCreated(CaluProjectService::class.java)?.currentSync() ?: return
        val mark = sync.markOf(file.path, file.isDirectory) ?: return
        data.locationString = mark.text
        if (mark == SyncMark.CONFLICT || mark == SyncMark.CONFLICT_BELOW) data.forcedTextForeground = JBColor.RED
    }
}
