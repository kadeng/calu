package io.github.kadeng.calu.frontend

import com.intellij.diff.requests.ContentDiffRequest
import com.intellij.diff.tools.util.DiffDataKeys
import com.intellij.diff.contents.DocumentContent
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.ui.Messages
import io.github.kadeng.calu.logic.CommentDiff
import io.github.kadeng.calu.logic.CommentInput
import io.github.kadeng.calu.logic.PlainSelection
import io.github.kadeng.calu.logic.buildCommentParams
import io.github.kadeng.calu.logic.describeCommentTarget
import io.github.kadeng.calu.logic.relativePathWithin
import io.github.kadeng.calu.state.isAttached

/** Prefix of the title of the diffs the plugin opens for an edit ("calu: <relative path>"). */
const val CALU_DIFF_TITLE_PREFIX: String = "calu: "

/**
 * "Add Comment for Claude" (editor popup menu, Ctrl+Alt+K): sends a comment on the selected
 * lines to the attached session (`comment.add`), which relays it to Claude and stores it.
 *
 *     normal editor   path relative to the project, the selected lines, the whole file text
 *     diff viewer     the same, plus the side of the selection and the text of both sides;
 *                     in a diff the plugin opened for an edit the path comes from its title
 *
 * Without a selection the caret's line is commented. The action is disabled while no session
 * is attached or the editor shows no file of the project.
 */
class CommentAction : DumbAwareAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        val project = e.project
        e.presentation.isEnabled = project != null && e.getData(CommonDataKeys.EDITOR) != null && isAttached(CaluFrontendService.of(project).state.value)
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val document = editor.document
        val model = editor.selectionModel
        val start = editor.offsetToLogicalPosition(if (model.hasSelection()) model.selectionStart else editor.caretModel.offset)
        val end = editor.offsetToLogicalPosition(if (model.hasSelection()) model.selectionEnd else editor.caretModel.offset)
        val selection = PlainSelection.of(start.line, start.column, end.line, end.column)

        val request = e.getData(DiffDataKeys.DIFF_REQUEST) as? ContentDiffRequest
        val sides = request?.contents?.filterIsInstance<DocumentContent>().orEmpty()
        val diff = if (request != null && sides.size == 2) {
            CommentDiff(if (sides[0].document === document) "before" else "after", sides[0].document.text, sides[1].document.text)
        } else {
            null
        }
        val base = project.basePath.orEmpty()
        val file = FileDocumentManager.getInstance().getFile(document) ?: sides.firstNotNullOfOrNull { it.highlightFile }
        val titled = request?.title?.takeIf { it.startsWith(CALU_DIFF_TITLE_PREFIX) }?.removePrefix(CALU_DIFF_TITLE_PREFIX)
        val path = titled ?: file?.takeIf { it.isInLocalFileSystem }?.let { runCatching { relativePathWithin(base, it.path) }.getOrNull() }
        if (path.isNullOrEmpty()) {
            Messages.showInfoMessage(project, "This editor shows no file of the project, so calu cannot name the commented lines.", "calu: Add Comment for Claude")
            return
        }
        val text = Messages.showMultilineInputDialog(
            project, "Comment on ${describeCommentTarget(path, selection, diff?.side)}:", "calu: Add Comment for Claude", "", Messages.getQuestionIcon(), null,
        ) ?: return
        if (text.isBlank()) return
        val params = try {
            buildCommentParams(CommentInput(path, selection, text, if (diff == null) document.text else null, diff))
        } catch (error: IllegalArgumentException) {
            Messages.showErrorDialog(project, error.message.orEmpty(), "calu: Add Comment for Claude")
            return
        }
        CaluFrontendService.of(project).act("comment", params)
    }
}
