/**
 * A modal question with buttons whose default and focus are set exactly as a `Question` says
 * (`shared/sync/Questions.kt`), including "no default button".
 *
 * The platform's `Messages.showDialog` always makes one button the default, so a question
 * where every answer can lose data (Keep Mine / Take Theirs) needs this dialog of its own.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.DialogWrapper
import com.intellij.openapi.ui.Messages
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.sync.Question
import java.awt.BorderLayout
import java.awt.event.ActionEvent
import javax.swing.AbstractAction
import javax.swing.Action
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * The dialog of one question. Use `ask`.
 *
 * @param project The project the dialog belongs to (its window is the parent).
 * @param question What to ask.
 */
class ChoiceDialog(project: Project?, private val question: Question) : DialogWrapper(project, true) {
    /** The pressed button's index; `question.cancelIndex` until a button is pressed. */
    var chosen: Int = question.cancelIndex
        private set

    /** One action per option, in the question's order; only `defaultIndex` (if any) carries `DEFAULT_ACTION`. */
    val buttonActions: List<Action> = question.options.mapIndexed { index, label ->
        object : AbstractAction(label) {
            override fun actionPerformed(e: ActionEvent?) {
                chosen = index
                if (index == question.cancelIndex) close(CANCEL_EXIT_CODE) else close(NEXT_USER_EXIT_CODE + index)
            }
        }.also { if (index == question.defaultIndex) it.putValue(DEFAULT_ACTION, true) }
    }

    init {
        title = question.title
        // No button of the platform's own: the OK action would be the default button otherwise.
        isOKActionEnabled = false
        init()
    }

    /** @return The question's text with the warning icon. */
    override fun createCenterPanel(): JComponent = JPanel(BorderLayout(JBUI.scale(12), 0)).apply {
        add(JBLabel(Messages.getWarningIcon()), BorderLayout.WEST)
        add(JBLabel("<html>" + question.message.escapeHtml().replace("\n", "<br>") + "</html>"), BorderLayout.CENTER)
    }

    /** @return One action per option, in the question's order; only `defaultIndex` (if any) is the default. */
    override fun createActions(): Array<Action> = buttonActions.toTypedArray()

    /** @return The button of `focusIndex`. */
    override fun getPreferredFocusedComponent(): JComponent? = getButton(buttonActions[question.focusIndex])

    /** Escape and the window's close box answer with `cancelIndex`. */
    override fun doCancelAction() {
        chosen = question.cancelIndex
        super.doCancelAction()
    }

    companion object {
        /**
         * Show a question and wait for the answer. Must be called on the EDT.
         *
         * @param project The project, or `null`.
         * @param question What to ask.
         * @return The index of the pressed button; `question.cancelIndex` for Escape or a closed window.
         */
        fun ask(project: Project?, question: Question): Int {
            val dialog = ChoiceDialog(project, question)
            dialog.show()
            return dialog.chosen
        }
    }
}

/**
 * Escape the characters HTML gives a meaning to.
 *
 * @return The text with `&`, `<` and `>` replaced by entities.
 */
private fun String.escapeHtml(): String = replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
