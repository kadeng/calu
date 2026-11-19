package io.github.kadeng.calu.frontend

import com.intellij.diff.requests.ContentDiffRequest
import com.intellij.diff.tools.util.DiffDataKeys
import com.intellij.diff.contents.DocumentContent
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.application.EDT
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.platform.project.projectId
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.remote.CaluRemoteApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import io.github.kadeng.calu.logic.CommentDiff
import io.github.kadeng.calu.logic.CommentInput
import io.github.kadeng.calu.logic.PlainSelection
import io.github.kadeng.calu.logic.buildCommentParams
import io.github.kadeng.calu.logic.describeCommentTarget
import io.github.kadeng.calu.logic.commentPathFromShown
import io.github.kadeng.calu.logic.pathFromDiffSideTitle
import io.github.kadeng.calu.state.isAttached

private val LOG = logger<CommentAction>()

/** Prefix of the title of the diffs the plugin opens for an edit ("calu: <relative path>"). */
const val CALU_DIFF_TITLE_PREFIX: String = "calu: "

/**
 * "Add Comment for Claude" (editor popup menu, Ctrl+Alt+K): sends a comment on the selected
 * lines to the attached session (`comment.add`), which relays it to Claude and stores it.
 *
 *     normal editor   path relative to the project, the selected lines, the whole file text
 *     diff viewer     the same, plus the side of the selection and the text of both sides;
 *                     in a diff the plugin opened for an edit the path comes from its title,
 *                     or in remote development from its side titles
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
        // The diff of an edit names the file in its title ("calu: <path on this machine>") and
        // in each side's title (`diffSideTitle`). In remote development only the side titles
        // reach the client. calu takes a comment's path relative to the project directory.
        val titled = request?.title?.takeIf { it.startsWith(CALU_DIFF_TITLE_PREFIX) }?.removePrefix(CALU_DIFF_TITLE_PREFIX)
        val sideTitled = request?.contentTitles?.firstNotNullOfOrNull { pathFromDiffSideTitle(it) }
        val path = listOfNotNull(titled, sideTitled).firstNotNullOfOrNull { commentPathFromShown(base, it) }
            ?: file?.takeIf { it.isInLocalFileSystem }?.let { commentPathFromShown(base, it.path) }
        if (!path.isNullOrEmpty()) {
            ask(project, path, selection, diff, document.text)
            return
        }
        // In remote development a diff opened by the backend reaches the client without its
        // request; the backend finds the edit by the editor's text.
        val text = document.text
        val service = CaluFrontendService.of(project)
        service.launch {
            val found = parseObjectOrNull(CaluRemoteApi.getInstance().diffOfText(project.projectId(), text))
            withContext(Dispatchers.EDT) {
                val foundPath = found?.str("path")
                if (found == null || foundPath == null) {
                    LOG.info("[calu] comment: no project file (request ${request?.javaClass?.simpleName}, titles ${request?.contentTitles}, file ${file?.path})")
                    Messages.showInfoMessage(project, "This editor shows no file of the project, so calu cannot name the commented lines.", "calu: Add Comment for Claude")
                } else {
                    val sides = CommentDiff(found.str("side") ?: "after", found.str("before").orEmpty(), found.str("after").orEmpty())
                    ask(project, foundPath, selection, sides, text)
                }
            }
        }
    }

    /**
     * Ask for the comment's text and send it. Must be called on the EDT.
     *
     * @param project The project.
     * @param path The commented file relative to the project directory.
     * @param selection The selection (0-based).
     * @param diff Both sides of a diff, or `null` for a normal editor.
     * @param fileText The editor's whole text (sent for a normal editor only).
     */
    private fun ask(project: Project, path: String, selection: PlainSelection, diff: CommentDiff?, fileText: String) {
        val text = Messages.showMultilineInputDialog(
            project, "Comment on ${describeCommentTarget(path, selection, diff?.side)}:", "calu: Add Comment for Claude", "", Messages.getQuestionIcon(), null,
        ) ?: return
        if (text.isBlank()) return
        val params = try {
            buildCommentParams(CommentInput(path, selection, text, if (diff == null) fileText else null, diff))
        } catch (error: IllegalArgumentException) {
            Messages.showErrorDialog(project, error.message.orEmpty(), "calu: Add Comment for Claude")
            return
        }
        CaluFrontendService.of(project).act("comment", params)
    }
}
