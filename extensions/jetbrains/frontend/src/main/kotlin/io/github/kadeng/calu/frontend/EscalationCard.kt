package io.github.kadeng.calu.frontend

import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.chat.RowModel
import io.github.kadeng.calu.state.AnswerChoice
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.EscalationView
import io.github.kadeng.calu.state.formatAnswer
import io.github.kadeng.calu.state.literalToolInput
import java.awt.FlowLayout
import javax.swing.BorderFactory
import javax.swing.JButton
import javax.swing.JPanel

/** What a card asks its surroundings to do. */
interface CardActions {
    /**
     * Decide an escalation.
     *
     * @param key The escalation's key in the state.
     * @param allow True to allow the step, false to deny it.
     * @param note The note typed into the card; "" for none.
     */
    fun decide(key: String, allow: Boolean, note: String)

    /**
     * Answer the agent's questions.
     *
     * @param key The escalation's key.
     * @param answer The answer text (not empty).
     */
    fun answer(key: String, answer: String)

    /**
     * Open the context of the escalation ("Why?").
     *
     * @param escalationId calu's id of the escalation.
     */
    fun why(escalationId: String)
}

/**
 * The card of one escalation: the tool, the reason, the step as literal text, a note field and
 * Allow / Deny / Why?; for a question of the agent its options and an answer field. A closed
 * escalation shows one line with the outcome. No button takes the keyboard focus by itself, so
 * that typing in the composer never decides a step. Must be used on the EDT.
 *
 * @param actions Receives the user's choices.
 * @param attached Says whether a session is attached now (decisions need one).
 */
class EscalationCard(private val actions: CardActions, private val attached: () -> Boolean) : JPanel(VerticalLayout(JBUI.scale(4))) {
    private var shown: EscalationView? = null
    private var shownAttached = false
    private val note = JBTextField().apply { emptyText.text = "Note for Claude (optional)" }
    private val answerField = JBTextField().apply { emptyText.text = "Your answer, or an addition to the chosen options" }
    private var choices: List<List<JBCheckBox>> = emptyList()

    /** The Allow button of an open card, `null` otherwise (for tests). */
    var allowButton: JButton? = null
        private set

    /** The Deny button of an open card, `null` otherwise (for tests). */
    var denyButton: JButton? = null
        private set

    init {
        isOpaque = false
    }

    /**
     * Draw the escalation of a row. The typed note and the chosen options are kept while the
     * same escalation stays open.
     *
     * @param row An "escalation" row; a row without escalation empties the card.
     */
    fun update(row: RowModel) {
        val e = row.escalation
        val nowAttached = attached()
        if (e == shown && nowAttached == shownAttached) return
        val sameOpen = e != null && shown?.key == e.key && shown?.status == EscalationStatus.OPEN && e.status == EscalationStatus.OPEN && choices.size == e.questions.size
        shown = e
        shownAttached = nowAttached
        removeAll()
        allowButton = null
        denyButton = null
        if (e == null) return
        val color = if (e.status == EscalationStatus.OPEN) UiStyle.chipColor("warning") else UiStyle.dim
        border = BorderFactory.createCompoundBorder(JBUI.Borders.customLine(color, 1), JBUI.Borders.empty(6, 8))
        if (e.status != EscalationStatus.OPEN) {
            val outcome = when (e.status) {
                EscalationStatus.ALLOW -> "Allowed"
                EscalationStatus.DENY -> "Denied"
                EscalationStatus.ANSWERED -> "Answered: ${e.answer.orEmpty()}"
                else -> "Cancelled"
            }
            add(JBLabel("${e.toolName}: $outcome" + (e.note?.takeIf { it.isNotEmpty() }?.let { " ($it)" } ?: "")).apply { foreground = UiStyle.dim })
            if (e.reason.isNotEmpty()) add(textArea(e.reason).apply { foreground = UiStyle.dim })
            revalidate()
            return
        }
        val question = e.questions.isNotEmpty()
        add(JBLabel(if (question) "Claude asks" else "Waiting for your decision: ${e.toolName}").apply { font = JBFont.label().asBold(); foreground = color })
        if (e.reason.isNotEmpty()) add(textArea(e.reason))
        if (question) {
            if (!sameOpen) choices = e.questions.map { q -> q.options.map { o -> JBCheckBox(o.label).apply { toolTipText = o.description; isOpaque = false } } }
            e.questions.forEachIndexed { i, q ->
                add(JBLabel(listOf(q.header, q.question).filter { it.isNotEmpty() }.joinToString(": ")))
                for (box in choices[i]) add(box)
            }
            add(answerField)
        } else {
            for ((label, text) in literalToolInput(e.toolInput).dropLast(1)) {
                add(JBLabel(label).apply { foreground = UiStyle.dim; font = JBFont.small() })
                add(textArea(text, monospaced = true))
            }
            add(note)
        }
        val buttons = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), 0)).apply { isOpaque = false }
        val enabled = nowAttached && e.pending != true
        if (question) {
            buttons.add(JButton("Answer").apply {
                isEnabled = enabled
                addActionListener {
                    val picked = e.questions.indices.map { i -> AnswerChoice(choices[i].filter { it.isSelected }.map { it.text }, if (i == e.questions.size - 1) answerField.text else "") }
                    val text = formatAnswer(e.questions, picked)
                    if (text.isNotEmpty()) actions.answer(e.key, text)
                }
            })
        } else {
            allowButton = JButton("Allow").apply { isEnabled = enabled; addActionListener { actions.decide(e.key, true, note.text.trim()) } }
            denyButton = JButton("Deny").apply { isEnabled = enabled; addActionListener { actions.decide(e.key, false, note.text.trim()) } }
            buttons.add(allowButton)
            buttons.add(denyButton)
        }
        buttons.add(JButton("Why?").apply { toolTipText = "Open what the checker knew and said"; addActionListener { actions.why(e.escalationId) } })
        if (e.pending == true) buttons.add(JBLabel("sending…").apply { foreground = UiStyle.dim })
        if (!nowAttached) buttons.add(JBLabel("connection lost").apply { foreground = UiStyle.chipColor("error") })
        for (b in buttons.components) (b as? JButton)?.isDefaultCapable = false
        add(buttons)
        revalidate()
        repaint()
    }
}
