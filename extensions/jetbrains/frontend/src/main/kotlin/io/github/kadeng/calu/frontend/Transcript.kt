/**
 * The native transcript: one Swing component per `RowModel`, kept by item id.
 *
 *     rowsOf(state, tab) ──TranscriptView.update──► same id, changed model ─► RowView.update (that row only)
 *                                                   new id at the end      ─► one RowView appended
 *                                                   anything else          ─► the list is laid out again (views reused)
 *
 * Swing is retained-mode: components are created once and changed in place; a streamed text
 * changes one text pane. All functions here must be called on the EDT.
 */
package io.github.kadeng.calu.frontend

import com.intellij.ide.BrowserUtil
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.ui.JBColor
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.HTMLEditorKitBuilder
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import com.intellij.util.ui.UIUtil
import io.github.kadeng.calu.chat.RowAction
import io.github.kadeng.calu.chat.RowModel
import io.github.kadeng.calu.logic.markdownToHtml
import io.github.kadeng.calu.state.ItemKind
import java.awt.BorderLayout
import java.awt.Color
import java.awt.Cursor
import java.awt.Dimension
import java.awt.FlowLayout
import java.awt.Rectangle
import java.awt.datatransfer.StringSelection
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.BorderFactory
import javax.swing.JComponent
import javax.swing.JEditorPane
import javax.swing.JPanel
import javax.swing.JPopupMenu
import javax.swing.Scrollable
import javax.swing.SwingConstants
import javax.swing.event.HyperlinkEvent

/** Theme-derived colours and spacings of the chat. */
object UiStyle {
    /** Space between rows, unscaled pixels. */
    const val GAP: Int = 6

    /** Background of the user's own messages. */
    val userBackground: Color get() = JBColor.namedColor("Editor.SearchField.background", UIUtil.getTextFieldBackground())

    /** Text colour of less prominent rows. */
    val dim: Color get() = UIUtil.getContextHelpForeground()

    /**
     * Colour of a chip or notice.
     *
     * @param kind "ok", "warning", "error" or anything else.
     * @return The theme's success, warning or error colour; the context help colour otherwise.
     */
    fun chipColor(kind: String): Color = when (kind) {
        "ok" -> JBColor.namedColor("Label.successForeground", JBColor(0x368746, 0x5FAD65))
        "warning" -> JBColor.namedColor("Label.warningForeground", JBColor(0xA46704, 0xD6AE58))
        "error" -> JBColor.namedColor("Label.errorForeground", JBColor.RED)
        else -> dim
    }
}

/** A panel that takes the width of the scroll pane's viewport, so that its texts wrap. */
open class WidthTrackingPanel(layout: java.awt.LayoutManager) : JPanel(layout), Scrollable {
    override fun getPreferredScrollableViewportSize(): Dimension = preferredSize
    override fun getScrollableUnitIncrement(visibleRect: Rectangle, orientation: Int, direction: Int): Int = JBUI.scale(16)
    override fun getScrollableBlockIncrement(visibleRect: Rectangle, orientation: Int, direction: Int): Int =
        if (orientation == SwingConstants.VERTICAL) visibleRect.height else visibleRect.width
    override fun getScrollableTracksViewportWidth(): Boolean = true
    override fun getScrollableTracksViewportHeight(): Boolean = false
}

/**
 * Create a read-only HTML pane for Markdown that wraps to its width and opens links in the browser.
 *
 * @return The pane (transparent, with the label font).
 */
fun htmlPane(): JEditorPane = JEditorPane().apply {
    editorKit = HTMLEditorKitBuilder.simple()
    isEditable = false
    isOpaque = false
    border = JBUI.Borders.empty()
    putClientProperty(JEditorPane.HONOR_DISPLAY_PROPERTIES, true)
    font = JBFont.label()
    addHyperlinkListener { e -> if (e.eventType == HyperlinkEvent.EventType.ACTIVATED) e.url?.let { BrowserUtil.browse(it) } }
}

/**
 * Create a read-only, wrapping plain-text area.
 *
 * @param text The text.
 * @param monospaced True for the editor's fixed-width font (commands, output).
 * @return The area (transparent).
 */
fun textArea(text: String, monospaced: Boolean = false): JBTextArea = JBTextArea(text).apply {
    isEditable = false
    lineWrap = true
    wrapStyleWord = true
    isOpaque = false
    border = JBUI.Borders.empty()
    font = if (monospaced) JBFont.create(java.awt.Font(java.awt.Font.MONOSPACED, java.awt.Font.PLAIN, JBFont.label().size)) else JBFont.label()
}

/**
 * The component of one row. `update` changes it in place.
 *
 * @param handle Runs a row action (a click).
 * @param card Creates the component of an escalation row; called once per row.
 */
class RowView(private val handle: (RowAction) -> Unit, private val card: (RowModel) -> EscalationCard) : JPanel(BorderLayout()) {
    /** The model drawn now; `null` before the first `update`. */
    var model: RowModel? = null
        private set
    private var expanded = false
    private var body: JEditorPane? = null
    private var escalationCard: EscalationCard? = null

    init {
        isOpaque = false
    }

    /**
     * Draw a model. A row whose model is equal to the drawn one is left alone; a streaming
     * Markdown row only gets new text; an escalation row updates its card.
     *
     * @param next The model (same id as before).
     */
    fun update(next: RowModel) {
        val old = model
        // An escalation card also depends on whether a session is attached; it checks for itself.
        if (old == next && next.kind != ItemKind.ESCALATION) return
        model = next
        if (old != null && old.kind == next.kind) {
            if (next.kind == ItemKind.ASSISTANT && body != null) {
                body!!.text = markdownToHtml(next.text)
                return
            }
            if (next.kind == ItemKind.ESCALATION && escalationCard != null) {
                escalationCard!!.update(next)
                return
            }
        }
        rebuild(next)
    }

    /**
     * Create the row's components from a model.
     *
     * @param m The model.
     */
    private fun rebuild(m: RowModel) {
        removeAll()
        body = null
        border = JBUI.Borders.empty(2, 8)
        when (m.kind) {
            ItemKind.ASSISTANT -> add(htmlPane().also { it.text = markdownToHtml(m.text); body = it; if (m.dim) it.foreground = UiStyle.dim }, BorderLayout.CENTER)
            ItemKind.USER -> {
                val bubble = JPanel(BorderLayout()).apply {
                    isOpaque = true
                    background = UiStyle.userBackground
                    border = JBUI.Borders.empty(6, 8)
                    add(JBLabel(m.title).apply { foreground = UiStyle.dim; font = JBFont.small() }, BorderLayout.NORTH)
                    add(textArea(m.text).also { if (m.dim) it.foreground = UiStyle.dim }, BorderLayout.CENTER)
                }
                add(bubble, BorderLayout.CENTER)
            }
            ItemKind.ESCALATION -> add(card(m).also { escalationCard = it; it.update(m) }, BorderLayout.CENTER)
            ItemKind.TURN, ItemKind.DIVIDER, ItemKind.RELAY -> add(line(m), BorderLayout.CENTER)
            ItemKind.NOTICE -> add(textArea(m.text).apply { foreground = UiStyle.chipColor(m.chipKind) }, BorderLayout.CENTER)
            else -> {
                add(line(m), BorderLayout.NORTH)
                if (expanded && m.detail.isNotEmpty()) {
                    add(textArea(m.detail, monospaced = m.kind == ItemKind.TOOL).apply { border = JBUI.Borders.empty(2, 16, 4, 0) }, BorderLayout.CENTER)
                }
            }
        }
        revalidate()
        repaint()
    }

    /**
     * Build the one-line form of a row: title, link, text, chip; clickable if the row has an
     * action or a detail to expand.
     *
     * @param m The model.
     * @return The line's component.
     */
    private fun line(m: RowModel): JComponent {
        val p = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), 0)).apply { isOpaque = false }
        val clickable = m.action != null || m.detail.isNotEmpty()
        if (m.title.isNotEmpty()) p.add(JBLabel(m.title).apply { font = JBFont.label().asBold(); if (m.dim) foreground = UiStyle.dim })
        if (m.linkText.isNotEmpty()) {
            p.add(ActionLink(m.linkText) { m.link?.let(handle) }.apply { toolTipText = m.link?.let { "Open file" + (it.args["line"]?.let { l -> " at line $l" } ?: "") } })
        }
        if (m.text.isNotEmpty()) {
            p.add(JBLabel(m.text.lineSequence().first().take(160)).apply { if (m.dim || m.kind == ItemKind.TURN) foreground = UiStyle.dim })
        }
        if (m.chip.isNotEmpty()) p.add(JBLabel(m.chip).apply { foreground = UiStyle.chipColor(m.chipKind); font = JBFont.small().asBold() })
        if (m.streaming) p.add(JBLabel("…").apply { foreground = UiStyle.dim })
        if (clickable) {
            p.cursor = Cursor.getPredefinedCursor(Cursor.HAND_CURSOR)
            p.toolTipText = m.tooltip.ifEmpty { "Show details" }
            val listener = object : MouseAdapter() {
                override fun mouseClicked(e: MouseEvent) {
                    if (e.isPopupTrigger || e.button != MouseEvent.BUTTON1) return
                    val action = m.action
                    if (action != null) handle(action) else toggle()
                }
                override fun mousePressed(e: MouseEvent) = popup(e)
                override fun mouseReleased(e: MouseEvent) = popup(e)
                private fun popup(e: MouseEvent) {
                    if (!e.isPopupTrigger) return
                    val menu = JPopupMenu()
                    if (m.detail.isNotEmpty()) {
                        menu.add(if (expanded) "Collapse" else "Expand").addActionListener { toggle() }
                        menu.add("Copy details").addActionListener { CopyPasteManager.getInstance().setContents(StringSelection(m.detail)) }
                    }
                    if (menu.componentCount > 0) menu.show(e.component, e.x, e.y)
                }
            }
            p.addMouseListener(listener)
            for (c in p.components) if (c !is ActionLink) c.addMouseListener(listener)
        }
        return p
    }

    /** Show or hide the row's detail. */
    private fun toggle() {
        expanded = !expanded
        model?.let(::rebuild)
    }
}

/**
 * The transcript of one tab.
 *
 * @param handle Runs a row action.
 * @param card Creates the card of an escalation row.
 */
class TranscriptView(private val handle: (RowAction) -> Unit, private val card: (RowModel) -> EscalationCard) {
    private val list = WidthTrackingPanel(VerticalLayout(JBUI.scale(UiStyle.GAP))).apply {
        border = BorderFactory.createEmptyBorder(JBUI.scale(6), 0, JBUI.scale(6), 0)
        isOpaque = false
    }
    private val views = LinkedHashMap<Int, RowView>()
    private var order: List<Int> = emptyList()

    /** The scroll pane that holds the rows. */
    val component: JBScrollPane = JBScrollPane(list).apply { border = JBUI.Borders.empty() }

    /** The row components now, in order (for tests). */
    val rows: List<RowView> get() = order.mapNotNull { views[it] }

    /**
     * Draw the rows of the tab. Keeps the scroll position, except that a view that was at its
     * bottom stays at the bottom.
     *
     * @param models The rows of `rowsOf`, in order.
     */
    fun update(models: List<RowModel>) {
        val bar = component.verticalScrollBar
        val atBottom = bar.value + bar.visibleAmount >= bar.maximum - JBUI.scale(24)
        val ids = models.map { it.id }
        val appendOnly = ids.size >= order.size && ids.subList(0, order.size) == order
        if (appendOnly) {
            for (m in models.subList(order.size, models.size)) list.add(view(m.id))
        } else {
            views.keys.retainAll(ids.toSet())
            list.removeAll()
            for (id in ids) list.add(view(id))
        }
        order = ids
        for (m in models) views[m.id]!!.update(m)
        list.revalidate()
        list.repaint()
        if (atBottom) javax.swing.SwingUtilities.invokeLater { bar.value = bar.maximum }
    }

    /**
     * Return the view of an item id, creating it if needed.
     *
     * @param id The item id.
     * @return The view.
     */
    private fun view(id: Int): RowView = views.getOrPut(id) { RowView(handle, card) }
}
