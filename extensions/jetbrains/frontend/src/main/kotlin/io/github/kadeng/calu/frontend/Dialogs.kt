/**
 * The checker picker and the entry to the dialog of calu's own settings (`SettingsDialog.kt`).
 */
package io.github.kadeng.calu.frontend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.application.asContextElement
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.DialogWrapper
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextField
import com.intellij.ui.table.JBTable
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.awt.BorderLayout
import java.awt.Dimension
import javax.swing.Action
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.table.DefaultTableModel

/**
 * List the checkers (`ide.settings.get`) in a popup; choosing one opens its instructions in an
 * editor tab. A failure is shown in a message box.
 *
 * @param project The project.
 * @param host State and requests.
 */
fun chooseChecker(project: Project, host: ChatHost) {
    host.launch {
        val answer = host.call("ide.settings.get", buildJsonObject { put("project", JsonNull) })
        val checkers = parseObjectOrNull(answer.resultJson)?.arr("checkers")?.mapNotNull { it.asObj() }.orEmpty()
        withContext(Dispatchers.EDT) {
            if (!answer.ok || checkers.isEmpty()) {
                Messages.showInfoMessage(project, if (answer.ok) "calu's database lists no checker." else answer.message, "calu: Checker Instructions")
                return@withContext
            }
            JBPopupFactory.getInstance().createPopupChooserBuilder(checkers)
                .setTitle("Edit Checker Instructions")
                .setRenderer(textRenderer<JsonObject> { "${it.str("name")}  —  route ${it.str("route")}, ${it.str("model")?.ifEmpty { "default model" }}${if (it.bool("enabled")) "" else ", disabled"}" })
                .setItemChosenCallback { c -> host.act("editChecker", buildJsonObject { put("name", JsonPrimitive(c.str("name").orEmpty())) }) }
                .createPopup().showCenteredInCurrentWindow(project)
        }
    }
}

/**
 * Open the dialog with all of calu's settings.
 *
 * @param project The project.
 * @param host State and requests.
 * @param page The page to show first: `SettingsPages.MODELS`, `SettingsPages.CHECKERS`, a
 *   section id, or "" for the first page.
 */
fun showCaluSettings(project: Project, host: ChatHost, page: String = "") {
    CaluSettingsDialog(project, host, page).show()
}

/**
 * The side-question panel: questions about the session that are answered beside the
 * conversation and do not enter it (`side.ask`). The exchanges are those of `ViewState.side`;
 * the dialog stays open while the chat goes on.
 *
 * @param project The project.
 * @param host State and requests.
 */
class SideQuestionDialog(project: Project, private val host: ChatHost) : com.intellij.openapi.ui.DialogWrapper(project) {
    private val answers = htmlPane()
    private val input = com.intellij.ui.components.JBTextField().apply { emptyText.text = "A question about this session (it does not enter the conversation)" }
    private val watcher: kotlinx.coroutines.Job

    init {
        title = "calu: Side Question"
        isModal = false
        setOKButtonText("Ask")
        setCancelButtonText("Close")
        init()
        val modality = com.intellij.openapi.application.ModalityState.any().asContextElement()
        var job: kotlinx.coroutines.Job? = null
        host.launch {
            job = kotlin.coroutines.coroutineContext[kotlinx.coroutines.Job]
            host.state.collect { state ->
                val html = sideHtml(state.side)
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.EDT + modality) { if (answers.text != html) answers.text = html }
            }
        }
        watcher = kotlinx.coroutines.Job().also { it.invokeOnCompletion { job?.cancel() } }
    }

    override fun createCenterPanel(): javax.swing.JComponent = javax.swing.JPanel(java.awt.BorderLayout(0, com.intellij.util.ui.JBUI.scale(8))).apply {
        preferredSize = java.awt.Dimension(com.intellij.util.ui.JBUI.scale(560), com.intellij.util.ui.JBUI.scale(420))
        add(com.intellij.ui.components.JBScrollPane(answers), java.awt.BorderLayout.CENTER)
        add(input, java.awt.BorderLayout.SOUTH)
    }

    override fun getPreferredFocusedComponent(): javax.swing.JComponent = input

    /** "Ask": send the question and keep the dialog open for the answer. */
    override fun doOKAction() {
        val question = input.text.trim()
        if (question.isEmpty()) return
        input.text = ""
        host.act("sideAsk", kotlinx.serialization.json.buildJsonObject { put("question", kotlinx.serialization.json.JsonPrimitive(question)) })
    }

    override fun dispose() {
        watcher.cancel()
        super.dispose()
    }
}

/**
 * Write the side questions and their answers as HTML, newest last.
 *
 * @param side `ViewState.side`.
 * @return One block per exchange: the question in bold, then the answer (Markdown), the reason
 *   it failed, or "…" while it runs; a hint if there is none yet.
 */
fun sideHtml(side: List<io.github.kadeng.calu.state.SideExchangeView>): String {
    if (side.isEmpty()) return "<p>Ask a question about what the session is doing. The answer appears here; Claude's conversation is not interrupted and does not see it.</p>"
    return side.joinToString("<hr>") { e ->
        "<p><b>" + io.github.kadeng.calu.logic.escapeHtml(e.question) + "</b></p>" + when {
            e.error != null -> "<p><i>Not answered: " + io.github.kadeng.calu.logic.escapeHtml(e.error!!) + "</i></p>"
            e.answer != null -> io.github.kadeng.calu.logic.markdownToHtml(e.answer!!)
            else -> "<p>…</p>"
        }
    }
}
