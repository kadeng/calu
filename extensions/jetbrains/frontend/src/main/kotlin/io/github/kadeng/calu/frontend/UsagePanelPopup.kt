/**
 * The usage panel: a popup above the context chip with the subscription limits, the plan's
 * limits, Claude Code's `/usage` text and the session's cost (`usage.panel`, PLAN D248). What
 * it says comes from `usagePanelSections` (shared, unit-tested); this file only draws it.
 *
 *     click on the chip ──► act("usagePanel") ──► ui.usagePanel asking ──► answer ──► update()
 */
package io.github.kadeng.calu.frontend

import com.intellij.openapi.ui.popup.JBPopup
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.ui.awt.RelativePoint
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.logic.UsagePanelLine
import io.github.kadeng.calu.logic.neutraliseBidi
import io.github.kadeng.calu.logic.usagePanelSections
import io.github.kadeng.calu.state.UsagePanelView
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.Point
import java.time.Instant
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * One usage panel of a chat surface; shown on request, drawn again on every new state while open.
 *
 * @param askAgain Called by "Ask again" (sends the `usagePanel` action once more).
 */
class UsagePanelPopup(private val askAgain: () -> Unit) {
    private val body = JPanel(VerticalLayout(JBUI.scale(4))).apply {
        isOpaque = false
        border = JBUI.Borders.empty(8, 10)
    }
    private var popup: JBPopup? = null
    private var shown: UsagePanelView? = null

    /** True while the popup is on the screen. */
    val isOpen: Boolean get() = popup?.isVisible == true

    /**
     * Open the panel above a component, with "asking" until the answer arrives.
     *
     * @param anchor The context chip.
     */
    fun show(anchor: JComponent) {
        popup?.cancel()
        update(UsagePanelView("asking"), force = true)
        val scroll = JBScrollPane(body).apply {
            border = JBUI.Borders.empty()
            preferredSize = Dimension(JBUI.scale(440), JBUI.scale(400))
        }
        val content = JPanel(BorderLayout()).apply {
            add(scroll, BorderLayout.CENTER)
            add(
                JPanel(BorderLayout()).apply {
                    border = JBUI.Borders.empty(4, 10, 8, 10)
                    add(ActionLink("Ask again") { askAgain() }, BorderLayout.WEST)
                },
                BorderLayout.SOUTH,
            )
        }
        val created = JBPopupFactory.getInstance().createComponentPopupBuilder(content, null)
            .setTitle("Usage")
            .setResizable(true)
            .setMovable(true)
            .setRequestFocus(true)
            .setCancelOnClickOutside(true)
            .createPopup()
        popup = created
        // Above the chip, right edges aligned: the chip sits at the right of a narrow tool window.
        val height = content.preferredSize.height + JBUI.scale(28)
        val x = minOf(0, anchor.width - content.preferredSize.width)
        created.show(RelativePoint(anchor, Point(x, -height)))
    }

    /**
     * Draw the panel's state if the panel is open and the state changed.
     *
     * @param panel The usage panel of the view state; `null` before the first request.
     * @param force True to draw although the panel is not open yet (used by [show]).
     */
    fun update(panel: UsagePanelView?, force: Boolean = false) {
        if ((!force && !isOpen) || panel == null || panel == shown) return
        shown = panel
        body.removeAll()
        when (panel.phase) {
            "asking" -> body.add(line(UsagePanelLine("Asking the session…", "muted")))
            "failed" -> body.add(line(UsagePanelLine("The session did not answer: ${panel.error.orEmpty()}", "problem")))
            else -> for (section in usagePanelSections(panel, Instant.now())) {
                body.add(JBLabel(section.title).apply { font = JBFont.label().asBold(); border = JBUI.Borders.emptyTop(6) })
                if (section.note.isNotEmpty()) body.add(line(UsagePanelLine(section.note, "muted"), small = true))
                for (l in section.lines) body.add(line(l))
                section.preformatted?.let { text ->
                    body.add(
                        textArea(neutraliseBidi(text), monospaced = true).apply {
                            isOpaque = true
                            background = UiStyle.codeBackground
                            border = JBUI.Borders.empty(6)
                        },
                    )
                }
            }
        }
        body.revalidate()
        body.repaint()
    }

    /** Close the panel if it is open. */
    fun close() {
        popup?.cancel()
        popup = null
    }

    /**
     * Draw one line, wrapping, in its tone's colour.
     *
     * @param l The line.
     * @param small True for the smaller font of a section's note.
     * @return The component.
     */
    private fun line(l: UsagePanelLine, small: Boolean = false): JComponent = textArea(neutraliseBidi(l.text)).apply {
        if (small) font = JBFont.small()
        when (l.tone) {
            "muted" -> foreground = UiStyle.dim
            "problem" -> foreground = UiStyle.chipColor("warning")
        }
    }
}
