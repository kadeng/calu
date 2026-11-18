/**
 * Saving of checker instruction files (the `LightVirtualFile`s of `EditorActions.editChecker`):
 * a banner in the editor with "Save to calu", and the IDE's "Save All" (Ctrl+S).
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileDocumentManagerListener
import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.ProjectManager
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.ui.EditorNotificationPanel
import com.intellij.ui.EditorNotificationProvider
import java.util.function.Function
import javax.swing.JComponent

/**
 * Store the text of one open checker file, in the background.
 *
 * @param project The project whose bridge stores it.
 * @param file A file marked with `CHECKER_ORIGIN`.
 */
fun saveCheckerFile(project: Project, file: VirtualFile) {
    val text = FileDocumentManager.getInstance().getDocument(file)?.text ?: return
    CaluProjectService.of(project).launch { EditorActions(project).saveChecker(file, text) }
}

/** Shows the banner "Checker instructions of calu" with its save link above a checker file. */
class CheckerBannerProvider : EditorNotificationProvider {
    override fun collectNotificationData(project: Project, file: VirtualFile): Function<in FileEditor, out JComponent?>? {
        val origin = file.getUserData(CHECKER_ORIGIN) ?: return null
        return Function { editor ->
            EditorNotificationPanel(editor, EditorNotificationPanel.Status.Info).apply {
                text = "Instructions of the checker \"${origin.name}\" in calu's database. Save All (Ctrl+S) or the link stores them."
                createActionLabel("Save to calu") { saveCheckerFile(project, file) }
            }
        }
    }
}

/** Stores every changed checker file when the IDE saves all documents. */
class CheckerSaveListener : FileDocumentManagerListener {
    override fun beforeAllDocumentsSaving() {
        for (project in ProjectManager.getInstance().openProjects) {
            if (project.isDisposed) continue
            for (file in FileEditorManager.getInstance(project).openFiles) {
                if (file.getUserData(CHECKER_ORIGIN) != null) saveCheckerFile(project, file)
            }
        }
    }
}
