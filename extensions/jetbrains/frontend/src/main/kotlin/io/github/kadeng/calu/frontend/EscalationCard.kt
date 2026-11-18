package io.github.kadeng.calu.frontend

import com.intellij.icons.AllIcons
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBRadioButton
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextField
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.chat.RowModel
import io.github.kadeng.calu.logic.neutraliseBidi
import io.github.kadeng.calu.state.AnswerChoice
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.EscalationView
import io.github.kadeng.calu.state.formatAnswer
import io.github.kadeng.calu.state.literalToolInput
import java.awt.BorderLayout
import java.awt.Dimension
import javax.swing.ButtonGroup
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.JToggleButton

/** Lines of a step's text a card shows before it offers "Show all lines". */
const val CARD_PREVIEW_LINES: Int = 12

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
 * The card of one escalation.
 *
 *     open decision   ┃ ⚠ Bash waits for your decision
 *                     ┃ the checker's reason
 *                     ┃ ┌ Command ───────────────────────┐   at most CARD_PREVIEW_LINES lines,
 *                     ┃ │ git push origin main           │   then "Show all N lines"
 *                     ┃ └────────────────────────────────┘
 *                     ┃ [ note for Claude (optional)     ]
 *                     ┃ [Allow] [Deny]  Why?
 *     open question   ┃ ? Claude asks ... options (one choice: radio buttons, several: check boxes) ... [Answer]
 *     decided         one dim line: "Bash: allowed · note" and the reason
 *
 * The buttons stay below a long step, because the step's text is cut. No button is the default
 * button or takes the keyboard focus by itself, so typing in the composer never decides a step.
 * Texts of the session are shown with bidirectional control characters replaced
 * (`neutraliseBidi`). Must be used on the EDT.
 *
 * @param actions Receives the user's choices.
 * @param attached Says whether a session is attached now (decisions need one).
 */
class EscalationCard(private val actions: CardActions, private val attached: () -> Boolean) :
    RoundedPanel(VerticalLayout(JBUI.scale(6)), fill = UiStyle.cardBackground) {
    private var shown: EscalationView? = null
    private var shownAttached = false
    private var showAll = false
    private val note = JBTextField().apply { emptyText.text = "Note for Claude (optional)" }
    private val answerField = JBTextField().apply { emptyText.text = "Your answer, or an addition to the chosen options" }
    private var choices: List<List<JToggleButton>> = emptyList()

    /** The Allow button of an open card, `null` otherwise (for tests). */
    var allowButton: JButton? = null
        private set

    /** The Deny button of an open card, `null` otherwise (for tests). */
    var denyButton: JButton? = null
        private set

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
        if (!sameOpen) showAll = false
        shown = e
        shownAttached = nowAttached
        build(e, nowAttached, sameOpen)
    }

    /**
     * Create the card's components.
     *
     * @param e The escalation; `null` empties the card.
     * @param nowAttached Whether a session is attached.
     * @param keepChoices True to keep the option buttons of the questions (the same open card).
     */
    private fun build(e: EscalationView?, nowAttached: Boolean, keepChoices: Boolean) {
        removeAll()
        allowButton = null
        denyButton = null
        if (e == null) {
            revalidate()
            return
        }
        val open = e.status == EscalationStatus.OPEN
        val question = e.questions.isNotEmpty()
        val reason = neutraliseBidi(e.reason)
        if (!open) {
            fill = null
            line = UiStyle.outline
            stripe = null
            border = JBUI.Borders.empty(5, 10)
            val outcome = when (e.status) {
                EscalationStatus.ALLOW -> "allowed"
                EscalationStatus.DENY -> "denied"
                EscalationStatus.ANSWERED -> "answered"
                else -> "cancelled"
            }
            val kind = when (e.status) {
                EscalationStatus.ALLOW, EscalationStatus.ANSWERED -> "ok"
                EscalationStatus.DENY -> "error"
                else -> ""
            }
            val head = JPanel(WrapLayout()).apply { isOpaque = false }
            head.add(JBLabel(if (question) "Claude's question" else neutraliseBidi(e.toolName)).apply { foreground = UiStyle.dim })
            head.add(Chip(outcome.uppercase(), kind))
            add(head)
            val said = listOfNotNull(e.answer?.takeIf { it.isNotEmpty() }, e.note?.takeIf { it.isNotEmpty() && it != e.answer }).joinToString("\n")
            if (said.isNotEmpty()) add(textArea(neutraliseBidi(said)))
            if (reason.isNotEmpty() && !question) add(textArea(reason).apply { foreground = UiStyle.dim })
            revalidate()
            repaint()
            return
        }
        val color = UiStyle.chipColor(if (question) "info" else "warning")
        fill = UiStyle.cardBackground
        line = color
        stripe = color
        border = JBUI.Borders.empty(8, 12, 8, 10)
        add(JBLabel(if (question) "Claude asks" else "${neutraliseBidi(e.toolName)} waits for your decision", if (question) AllIcons.General.QuestionDialog else AllIcons.General.Warning, JBLabel.LEADING).apply {
            font = JBFont.label().asBold()
        })
        if (question) {
            if (!keepChoices) {
                choices = e.questions.map { q ->
                    val group = if (q.multiSelect) null else ButtonGroup()
                    q.options.map { o ->
                        val button: JToggleButton = if (q.multiSelect) JBCheckBox(neutraliseBidi(o.label)) else JBRadioButton(neutraliseBidi(o.label))
                        button.apply { toolTipText = neutraliseBidi(o.description).ifEmpty { null }; isOpaque = false; group?.add(this) }
                    }
                }
            }
            e.questions.forEachIndexed { i, q ->
                if (q.header.isNotEmpty()) add(JBLabel(neutraliseBidi(q.header)).apply { foreground = UiStyle.dim; font = JBFont.small() })
                add(textArea(neutraliseBidi(q.question)))
                if (q.multiSelect) add(JBLabel("Several can be chosen").apply { foreground = UiStyle.dim; font = JBFont.small() })
                for ((n, box) in choices[i].withIndex()) {
                    add(box)
                    val description = neutraliseBidi(q.options[n].description)
                    if (description.isNotEmpty()) add(textArea(description).apply { foreground = UiStyle.dim; font = JBFont.small(); border = JBUI.Borders.emptyLeft(24) })
                }
            }
            add(answerField)
        } else {
            if (reason.isNotEmpty()) add(textArea(reason))
            for ((label, text) in literalToolInput(e.toolInput).dropLast(1)) add(stepBox(label, neutraliseBidi(text), e, nowAttached))
            add(note)
        }
        val buttons = JPanel(WrapLayout(8, 4)).apply { isOpaque = false }
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
            allowButton = JButton("Allow").apply { isEnabled = enabled; toolTipText = "Let this step run once"; addActionListener { actions.decide(e.key, true, note.text.trim()) } }
            denyButton = JButton("Deny").apply { isEnabled = enabled; toolTipText = "Refuse this step; the note tells Claude why"; addActionListener { actions.decide(e.key, false, note.text.trim()) } }
            buttons.add(allowButton)
            buttons.add(denyButton)
        }
        buttons.add(ActionLink("Why?") { actions.why(e.escalationId) }.apply { toolTipText = "Open what the checker knew and said" })
        if (e.pending == true) buttons.add(JBLabel("sending…", com.intellij.ui.AnimatedIcon.Default(), JBLabel.LEADING).apply { foreground = UiStyle.dim })
        if (!nowAttached) buttons.add(JBLabel("connection lost").apply { foreground = UiStyle.chipColor("error") })
        for (b in buttons.components) (b as? JButton)?.isDefaultCapable = false
        add(buttons)
        revalidate()
        repaint()
    }

    /**
     * Build the box of one part of the step (the command, the new text of an edit): its label
     * and the text in the editor's font, cut to `CARD_PREVIEW_LINES` lines with a link that
     * shows everything in a box of bounded height that scrolls.
     *
     * @param label The part's name ("Command", "File", ...).
     * @param text The part's text, already neutralised.
     * @param e The escalation (to draw the card again after "Show all").
     * @param nowAttached Whether a session is attached.
     * @return The box.
     */
    private fun stepBox(label: String, text: String, e: EscalationView, nowAttached: Boolean): JComponent {
        val lines = text.lines()
        val long = lines.size > CARD_PREVIEW_LINES
        val box = RoundedPanel(BorderLayout(0, JBUI.scale(2)), fill = UiStyle.codeBackground, line = UiStyle.outline)
        box.border = JBUI.Borders.empty(5, 8)
        val head = JPanel(BorderLayout()).apply { isOpaque = false }
        head.add(JBLabel(label + if (long) " (${lines.size} lines)" else "").apply { foreground = UiStyle.dim; font = JBFont.small() }, BorderLayout.WEST)
        if (long) {
            head.add(ActionLink(if (showAll) "Show less" else "Show all ${lines.size} lines") {
                showAll = !showAll
                build(e, nowAttached, true)
            }.apply { font = JBFont.small() }, BorderLayout.EAST)
        }
        box.add(head, BorderLayout.NORTH)
        if (long && showAll) {
            val area = textArea(text, monospaced = true)
            val pane = JBScrollPane(area).apply {
                border = JBUI.Borders.empty()
                isOpaque = false
                viewport.isOpaque = false
                // A bounded height keeps Allow and Deny on the screen below any step.
                preferredSize = Dimension(0, area.getFontMetrics(area.font).height * 22)
            }
            box.add(pane, BorderLayout.CENTER)
        } else {
            val shown = if (long) lines.take(CARD_PREVIEW_LINES).joinToString("\n") + "\n…" else text
            box.add(textArea(shown, monospaced = true), BorderLayout.CENTER)
        }
        return box
    }
}
