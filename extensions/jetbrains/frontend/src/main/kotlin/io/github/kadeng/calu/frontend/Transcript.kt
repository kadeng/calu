/**
 * The native transcript: one Swing component per item of the view state, kept by item id.
 *
 *     TranscriptView.update(state, tab)
 *       items of the tab are the same list, tools and escalations the same maps  ─► nothing
 *       an item whose own object, tool call, escalation and "held" mark are unchanged ─► its row is left alone
 *       a changed item                                    ─► RowView.update (that row only)
 *       new items at the end                              ─► rows appended
 *       items dropped at the start (the 1,000-item bound) ─► those rows removed
 *       any other change of order                         ─► the list is filled again (views reused)
 *
 * The transcript of a session that ended (`transcriptEnded`: the server stopped) stays readable,
 * drawn faded under a line "Session ended"; its open cards are one line each.
 *
 * The reducer copies on write, so "unchanged" is an identity comparison per item: a session of
 * a thousand items costs a thousand reference comparisons per event and one changed row.
 * Components are created once and changed in place; a streamed text changes one text pane.
 * All functions here must be called on the EDT.
 */
package io.github.kadeng.calu.frontend

import com.intellij.icons.AllIcons
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.ui.JBPopupMenu
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.chat.RowAction
import io.github.kadeng.calu.chat.RowModel
import io.github.kadeng.calu.chat.rowOf
import io.github.kadeng.calu.logic.heldTools
import io.github.kadeng.calu.logic.imageBytes
import io.github.kadeng.calu.logic.linkFiles
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import io.github.kadeng.calu.logic.markdownToHtml
import io.github.kadeng.calu.logic.transcriptEnded
import io.github.kadeng.calu.logic.menuFor
import io.github.kadeng.calu.logic.neutraliseBidi
import io.github.kadeng.calu.logic.userMessageMenu
import io.github.kadeng.calu.state.EscalationItem
import io.github.kadeng.calu.state.Item
import io.github.kadeng.calu.state.ItemKind
import io.github.kadeng.calu.state.ToolItem
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAttached
import java.awt.AlphaComposite
import java.awt.BorderLayout
import java.awt.Graphics
import java.awt.Graphics2D
import java.awt.Dimension
import java.awt.FlowLayout
import java.awt.Image
import java.awt.datatransfer.StringSelection
import java.awt.event.ComponentAdapter
import java.awt.event.ComponentEvent
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.imageio.ImageIO
import javax.swing.ImageIcon
import javax.swing.JComponent
import javax.swing.JEditorPane
import javax.swing.JLayer
import javax.swing.JPanel
import javax.swing.JSeparator
import javax.swing.SwingConstants
import javax.swing.SwingUtilities
import javax.swing.plaf.LayerUI

/** Longest detail text a row shows when expanded; the rest is reachable through "Copy details". */
const val MAX_DETAIL_CHARS: Int = 20_000

/** What rows ask their surroundings to do. */
interface RowHost : CardActions {
    /** The view state drawn now. */
    val current: ViewState

    /**
     * Run the action of a clicked row or link.
     *
     * @param action The action ("openTab" is handled by the surface, the rest by the backend).
     */
    fun handle(action: RowAction)

    /**
     * Run an entry of a user message's menu.
     *
     * @param id "rewind", "forkAt" or "copy".
     * @param item The message.
     */
    fun userAction(id: String, item: UserItem)

    /**
     * Run an entry of a tool row's menu.
     *
     * @param id An id of `ItemActionId`.
     * @param toolUseId The tool call.
     */
    fun toolAction(id: String, toolUseId: String)
}

/**
 * The component of one row. `update` changes it in place.
 *
 * @param host Runs the row's actions and creates nothing itself.
 */
class RowView(private val host: RowHost) : JPanel(BorderLayout()) {
    /** The model drawn now; `null` before the first `update`. */
    var model: RowModel? = null
        private set
    private var item: Item? = null
    private var held = false
    private var expanded = false
    private var body: JEditorPane? = null
    private var escalationCard: EscalationCard? = null

    init {
        isOpaque = false
    }

    /**
     * Draw an item. A streaming Markdown row only gets new text; an escalation row updates its
     * card; every other change builds the row's components again.
     *
     * @param next The row's model (same id as before).
     * @param source The item the model was built from.
     * @param isHeld True if calu holds this tool call back while a decision is open.
     */
    fun update(next: RowModel, source: Item, isHeld: Boolean) {
        val old = model
        item = source
        if (old == next && held == isHeld && next.kind != ItemKind.ESCALATION) return
        model = next
        held = isHeld
        if (old != null && old.kind == next.kind) {
            val pane = body
            if (next.kind == ItemKind.ASSISTANT && pane != null) {
                pane.text = html(next.text)
                return
            }
            val card = escalationCard
            if (next.kind == ItemKind.ESCALATION && card != null) {
                card.update(next)
                return
            }
        }
        rebuild(next)
    }

    /**
     * Convert Claude's Markdown to HTML, with links for the files the session's tool calls named.
     *
     * @param markdown The text.
     * @return The HTML for the row's pane.
     */
    private fun html(markdown: String): String = linkFiles(markdownToHtml(markdown), fileIndexOf(host.current.tools))

    /**
     * Create the row's components from a model.
     *
     * @param m The model.
     */
    private fun rebuild(m: RowModel) {
        removeAll()
        body = null
        border = JBUI.Borders.empty(1, 10)
        when (m.kind) {
            ItemKind.ASSISTANT -> add(
                htmlPane { path, line -> host.handle(RowAction("openFile", buildJsonObject { put("path", JsonPrimitive(path)); line?.let { put("line", JsonPrimitive(it)) } })) }
                    .also { it.text = html(m.text); body = it; if (m.dim) it.foreground = UiStyle.dim },
                BorderLayout.CENTER,
            )
            ItemKind.USER -> add(userBubble(m), BorderLayout.CENTER)
            ItemKind.ESCALATION -> {
                border = JBUI.Borders.empty(4, 10)
                add(EscalationCard(host, { isAttached(host.current) }, { transcriptEnded(host.current) }).also { escalationCard = it; it.update(m) }, BorderLayout.CENTER)
            }
            ItemKind.TURN, ItemKind.DIVIDER -> add(separatorLine(m), BorderLayout.CENTER)
            ItemKind.NOTICE -> add(notice(m), BorderLayout.CENTER)
            ItemKind.CHECKER -> add(checkerLine(m), BorderLayout.CENTER)
            else -> {
                add(line(m), BorderLayout.NORTH)
                if (expanded && m.detail.isNotEmpty()) add(detailBox(m), BorderLayout.CENTER)
            }
        }
        revalidate()
        repaint()
    }

    /**
     * Build the bubble of a user message: who wrote it, the text, thumbnails of its images, and
     * a menu button (rewind, fork, copy).
     *
     * @param m The model of a "user" row.
     * @return The bubble.
     */
    private fun userBubble(m: RowModel): JComponent {
        val user = item as? UserItem
        val bubble = RoundedPanel(BorderLayout(0, JBUI.scale(2)), fill = UiStyle.cardBackground, line = UiStyle.outline)
        bubble.border = JBUI.Borders.empty(6, 10)
        val head = JPanel(BorderLayout()).apply { isOpaque = false }
        val who = if (user?.to != null) "${m.title} → subagent" else m.title
        head.add(JBLabel(who + if (user?.stored == true) " (recorded)" else "").apply { foreground = UiStyle.dim; font = JBFont.small() }, BorderLayout.WEST)
        if (user != null) {
            val more = JBLabel(AllIcons.Actions.More)
            clickable(more, "Rewind, fork, copy") { userMenu(user).show(more, 0, more.height) }
            head.add(more, BorderLayout.EAST)
        }
        bubble.add(head, BorderLayout.NORTH)
        val text = if (user != null) neutraliseBidi(user.text) else m.text
        if (text.isNotEmpty()) bubble.add(textArea(text).also { if (m.dim) it.foreground = UiStyle.dim }, BorderLayout.CENTER)
        if (user != null && user.images.isNotEmpty()) {
            val strip = JPanel(WrapLayout()).apply { isOpaque = false }
            for (image in user.images) strip.add(thumbnail(imageBytes(image), JBUI.scale(96)) ?: JBLabel("[image not kept]").apply { foreground = UiStyle.dim })
            bubble.add(strip, BorderLayout.SOUTH)
        }
        return bubble
    }

    /**
     * Build the menu of a user message from `userMessageMenu`.
     *
     * @param user The message.
     * @return The popup menu; disabled entries carry their reason as tooltip.
     */
    private fun userMenu(user: UserItem): JBPopupMenu {
        val menu = JBPopupMenu()
        val entries = userMessageMenu(host.current, user)
        for (a in entries.actions) {
            menu.add(a.label).apply {
                isEnabled = a.enabled
                toolTipText = a.title
                addActionListener { host.userAction(a.id, user) }
            }
        }
        entries.note?.let { menu.add(it).isEnabled = false }
        return menu
    }

    /**
     * Build a centred, dim line between two rules (the end of a turn, a divider).
     *
     * @param m The model.
     * @return The component.
     */
    private fun separatorLine(m: RowModel): JComponent {
        val p = JPanel(BorderLayout(JBUI.scale(8), 0)).apply { isOpaque = false; border = JBUI.Borders.empty(4, 0) }
        val label = JBLabel(m.text).apply { foreground = if (m.chipKind == "error") UiStyle.chipColor("error") else UiStyle.dim; font = JBFont.small() }
        p.add(centred(JSeparator(SwingConstants.HORIZONTAL)), BorderLayout.CENTER)
        p.add(label, BorderLayout.WEST)
        return p
    }

    /**
     * Put a component into the vertical middle of a panel.
     *
     * @param c The component.
     * @return The panel.
     */
    private fun centred(c: JComponent): JComponent = JPanel(java.awt.GridBagLayout()).apply {
        isOpaque = false
        add(c, java.awt.GridBagConstraints().apply { fill = java.awt.GridBagConstraints.HORIZONTAL; weightx = 1.0 })
    }

    /**
     * Build a notice: an icon of its level and the wrapped text. A resolved notice (chip
     * "resolved") keeps its text, drawn dim with a check mark and a "RESOLVED" badge instead of
     * the warning colour.
     *
     * @param m The model of a "notice" row.
     * @return The component.
     */
    private fun notice(m: RowModel): JComponent {
        val p = JPanel(BorderLayout(JBUI.scale(6), 0)).apply { isOpaque = false; border = JBUI.Borders.empty(2, 0) }
        if (m.chip == "resolved") {
            p.add(JBLabel(AllIcons.General.InspectionsOK).apply { verticalAlignment = SwingConstants.TOP }, BorderLayout.WEST)
            p.add(textArea(m.text).apply { foreground = UiStyle.dim }, BorderLayout.CENTER)
            p.add(JPanel(BorderLayout()).apply { isOpaque = false; add(Chip("RESOLVED", "ok"), BorderLayout.NORTH) }, BorderLayout.EAST)
            return p
        }
        val icon = when (m.chipKind) {
            "error" -> AllIcons.General.Error
            "warning" -> AllIcons.General.Warning
            else -> AllIcons.General.Information
        }
        p.add(JBLabel(icon).apply { verticalAlignment = SwingConstants.TOP }, BorderLayout.WEST)
        p.add(textArea(m.text).apply { foreground = if (m.chipKind.isEmpty()) UiStyle.dim else UiStyle.chipColor(m.chipKind) }, BorderLayout.CENTER)
        return p
    }

    /**
     * Build the line of a checker's remark: the checker's name as a link to its instructions,
     * its decision as a chip, and the wrapped text below.
     *
     * @param m The model of a "checker" row.
     * @return The component, indented below the step it belongs to.
     */
    private fun checkerLine(m: RowModel): JComponent {
        val p = JPanel(BorderLayout(0, JBUI.scale(1))).apply { isOpaque = false; border = JBUI.Borders.emptyLeft(16) }
        val head = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), 0)).apply { isOpaque = false }
        val action = m.action
        if (action != null) {
            head.add(ActionLink(m.title) { host.handle(action) }.apply { toolTipText = m.tooltip; font = JBFont.small() })
        } else {
            head.add(JBLabel(m.title).apply { font = JBFont.small(); foreground = UiStyle.dim })
        }
        if (m.chip.isNotEmpty()) head.add(Chip(m.chip, m.chipKind))
        p.add(head, BorderLayout.NORTH)
        if (m.text.isNotEmpty()) p.add(textArea(m.text).apply { foreground = UiStyle.dim; border = JBUI.Borders.emptyLeft(6) }, BorderLayout.CENTER)
        return p
    }

    /**
     * Build the one-line form of a tool, thinking, launch or relay row:
     *
     *     [▸] Title  file-link  summary that is cut with an ellipsis ...      [held] [DECISION]
     *
     * A left click runs the row's action, or expands the detail if there is none; the arrow
     * always expands. A right click opens the menu (expand, copy, the tool's own entries).
     *
     * @param m The model.
     * @return The line's component.
     */
    private fun line(m: RowModel): JComponent {
        val p = JPanel(BorderLayout(JBUI.scale(6), 0)).apply { isOpaque = false; border = JBUI.Borders.empty(2, 0) }
        val left = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(5), 0)).apply { isOpaque = false }
        val arrow = JBLabel(if (m.detail.isEmpty()) AllIcons.Empty else if (expanded) AllIcons.General.ArrowDown else AllIcons.General.ArrowRight)
        if (m.detail.isNotEmpty()) clickable(arrow, if (expanded) "Hide details" else "Show details") { toggle() }
        left.add(arrow)
        if (m.title.isNotEmpty()) left.add(JBLabel(m.title).apply { font = JBFont.label().asBold(); if (m.dim) foreground = UiStyle.dim })
        val link = m.link
        if (m.linkText.isNotEmpty() && link != null) {
            left.add(ActionLink(m.linkText) { host.handle(link) }.apply { toolTipText = "Open file" + (link.args["line"]?.let { l -> " at line $l" } ?: "") })
        }
        p.add(left, BorderLayout.WEST)
        val summary = JBLabel(m.text.lineSequence().firstOrNull().orEmpty().take(300)).apply {
            foreground = UiStyle.dim
            if (m.kind == ItemKind.TOOL && m.linkText.isEmpty()) font = UiStyle.mono
            // A label in the centre of a border layout is cut with an ellipsis instead of pushing the chips out.
            minimumSize = Dimension(0, 0)
        }
        p.add(summary, BorderLayout.CENTER)
        val right = JPanel(FlowLayout(FlowLayout.RIGHT, JBUI.scale(4), 0)).apply { isOpaque = false }
        if (m.streaming) right.add(JBLabel(com.intellij.ui.AnimatedIcon.Default()))
        if (held) right.add(Chip("HELD", "warning").apply { toolTipText = "calu holds this step until the open decision is made" })
        if (m.chip.isNotEmpty()) right.add(Chip(m.chip, m.chipKind))
        if (m.failed) right.add(Chip("FAILED", "error").apply { toolTipText = "The tool call ended with an error" })
        if (right.componentCount > 0) p.add(right, BorderLayout.EAST)
        if (m.honesty.isNotEmpty()) {
            // The honesty checker's status per file: small lines under the row, no decision chip.
            val list = JPanel(VerticalLayout(0)).apply { isOpaque = false; border = JBUI.Borders.emptyLeft(26) }
            for (h in m.honesty) {
                list.add(JBLabel(h.text.take(400)).apply {
                    font = JBFont.small()
                    foreground = if (h.level == "ok") UiStyle.dim else UiStyle.chipColor(h.level)
                    if (h.tooltip.isNotEmpty()) toolTipText = h.tooltip
                    minimumSize = Dimension(0, 0)
                })
            }
            p.add(list, BorderLayout.SOUTH)
        }

        val action = m.action
        val click = { if (action != null) host.handle(action) else if (m.detail.isNotEmpty()) toggle() }
        if (action != null || m.detail.isNotEmpty()) {
            val tip = m.tooltip.ifEmpty { "Show details" }
            clickable(p, tip, click)
            for (c in listOf<JComponent>(summary, right)) clickable(c, tip, click)
            for (c in left.components) if (c !is ActionLink && c !== arrow) clickable(c as JComponent, tip, click)
        }
        val popup = object : MouseAdapter() {
            override fun mousePressed(e: MouseEvent) = show(e)
            override fun mouseReleased(e: MouseEvent) = show(e)

            /**
             * Open the row's menu for a popup trigger.
             *
             * @param e The mouse event.
             */
            private fun show(e: MouseEvent) {
                if (!e.isPopupTrigger) return
                val menu = rowMenu(m)
                if (menu.componentCount > 0) menu.show(e.component, e.x, e.y)
            }
        }
        for (c in listOf<JComponent>(p, summary, left, right) + left.components.filterIsInstance<JBLabel>()) c.addMouseListener(popup)
        return p
    }

    /**
     * Build the context menu of a row: expand or collapse, copy the details, and for a tool
     * call the entries of `menuFor` (open file, show edit, copy path, copy command).
     *
     * @param m The model.
     * @return The menu; empty if the row offers nothing.
     */
    private fun rowMenu(m: RowModel): JBPopupMenu {
        val menu = JBPopupMenu()
        if (m.detail.isNotEmpty()) {
            menu.add(if (expanded) "Collapse" else "Expand").addActionListener { toggle() }
            menu.add("Copy Details").addActionListener { CopyPasteManager.getInstance().setContents(StringSelection(m.detail)) }
        }
        val toolItem = item as? ToolItem
        val tool = toolItem?.let { host.current.tools[it.toolUseId] }
        val entries = tool?.let { menuFor(it) }
        if (entries != null && entries.actions.isNotEmpty()) {
            if (menu.componentCount > 0) menu.addSeparator()
            for (a in entries.actions) menu.add(a.label).addActionListener { host.toolAction(a.id, tool.toolUseId) }
        }
        return menu
    }

    /**
     * Build the expanded detail of a row: a box in the editor's colours with the text in the
     * editor's font for tool calls, at most `MAX_DETAIL_CHARS` characters of it.
     *
     * @param m The model.
     * @return The box.
     */
    private fun detailBox(m: RowModel): JComponent {
        val cut = m.detail.length > MAX_DETAIL_CHARS
        val shown = if (cut) m.detail.take(MAX_DETAIL_CHARS) + "\n… (${m.detail.length - MAX_DETAIL_CHARS} more characters: right click, Copy Details)" else m.detail
        val box = RoundedPanel(BorderLayout(), fill = UiStyle.codeBackground, line = UiStyle.outline)
        box.border = JBUI.Borders.empty(6, 8)
        box.add(textArea(shown, monospaced = m.kind == ItemKind.TOOL), BorderLayout.CENTER)
        return JPanel(BorderLayout()).apply { isOpaque = false; border = JBUI.Borders.empty(2, 20, 4, 0); add(box, BorderLayout.CENTER) }
    }

    /** Show or hide the row's detail. */
    private fun toggle() {
        expanded = !expanded
        model?.let(::rebuild)
    }
}

/** The file index of the tools map it was built from; rebuilt when the map is another object (EDT only). */
private var indexedTools: Map<String, io.github.kadeng.calu.state.ToolView>? = null
private var indexOfTools: io.github.kadeng.calu.logic.FileIndex? = null

/**
 * Return the index of the files that tool calls named, building it once per state.
 *
 * @param tools `ViewState.tools`.
 * @return The index (see `buildFileIndex`).
 */
fun fileIndexOf(tools: Map<String, io.github.kadeng.calu.state.ToolView>): io.github.kadeng.calu.logic.FileIndex {
    val known = indexOfTools
    if (known != null && indexedTools === tools) return known
    return io.github.kadeng.calu.logic.buildFileIndex(tools).also { indexedTools = tools; indexOfTools = it }
}

/**
 * Show an image in a dialog at its own size, scaled down only to fit the screen.
 *
 * @param image The decoded image.
 */
fun showImage(image: java.awt.image.BufferedImage) {
    val screen = java.awt.Toolkit.getDefaultToolkit().screenSize
    val scale = minOf(1.0, 0.85 * screen.width / image.width, 0.8 * screen.height / image.height)
    val shown = if (scale < 1.0) image.getScaledInstance(maxOf(1, (image.width * scale).toInt()), maxOf(1, (image.height * scale).toInt()), Image.SCALE_SMOOTH) else image
    val builder = com.intellij.openapi.ui.DialogBuilder()
    builder.setTitle("calu: Image (${image.width} × ${image.height} pixels)")
    builder.setCenterPanel(JBScrollPane(JBLabel(ImageIcon(shown))).apply { border = JBUI.Borders.empty() })
    builder.removeAllActions()
    builder.addCloseButton()
    builder.show()
}

/**
 * Decode an image and scale it into a thumbnail.
 *
 * @param bytes The image file's bytes; `null` gives `null`.
 * @param size Longest side of the thumbnail in pixels.
 * @return A label with the thumbnail (a click shows the image at its own size), or `null` if
 *   the bytes are no image Java can read.
 */
fun thumbnail(bytes: ByteArray?, size: Int): JBLabel? {
    if (bytes == null) return null
    val image = runCatching { ImageIO.read(bytes.inputStream()) }.getOrNull() ?: return null
    val scale = minOf(1.0, size.toDouble() / maxOf(image.width, image.height))
    val scaled = image.getScaledInstance(maxOf(1, (image.width * scale).toInt()), maxOf(1, (image.height * scale).toInt()), Image.SCALE_SMOOTH)
    return JBLabel(ImageIcon(scaled)).apply { clickable(this, "${image.width} × ${image.height} pixels. Click to enlarge.") { showImage(image) } }
}

/**
 * The transcript of one tab: the rows in a scroll pane and a "Jump to latest" bar that appears
 * while the view is not at its end.
 *
 * @param host Runs the rows' actions.
 */
class TranscriptView(private val host: RowHost) {
    /** What a row was last drawn from; all compared by identity except the two flags. */
    private class Drawn(val item: Item, val tool: Any?, val escalation: Any?, val held: Boolean, val attached: Boolean, val ended: Boolean)

    /** Paints the transcript faded while `faded` is true (the session ended); events pass through. */
    private class FadeUi : LayerUI<JComponent>() {
        /** True to paint the view at reduced opacity. */
        var faded = false

        /**
         * Paint the view, faded if `faded`.
         *
         * @param g The graphics.
         * @param c The layer.
         */
        override fun paint(g: Graphics, c: JComponent) {
            if (!faded) return super.paint(g, c)
            val g2 = g.create() as Graphics2D
            try {
                g2.composite = AlphaComposite.getInstance(AlphaComposite.SRC_OVER, 0.5f)
                super.paint(g2, c)
            } finally {
                g2.dispose()
            }
        }
    }

    private val list = WidthTrackingPanel(VerticalLayout(JBUI.scale(UiStyle.GAP))).apply {
        border = JBUI.Borders.empty(6, 0)
        isOpaque = false
    }
    private val views = HashMap<Int, RowView>()
    private val drawn = HashMap<Int, Drawn>()
    private var order: List<Int> = emptyList()
    private var lastItems: List<Item>? = null
    private var lastTools: Any? = null
    private var lastEscalations: Any? = null
    private var lastAttached = false
    private var lastEnded = false
    private var stick = true
    private val scroll = JBScrollPane(list).apply { border = JBUI.Borders.empty() }
    private val jump = ActionLink("↓ Jump to latest") { toEnd() }
    private val jumpBar = JPanel(FlowLayout(FlowLayout.CENTER, 0, JBUI.scale(2))).apply { isVisible = false; add(jump) }

    private val fade = FadeUi()

    /** "Session ended" above the faded transcript of a session that ended; hidden otherwise. */
    val endedBanner: JBLabel = JBLabel("Session ended").apply {
        isVisible = false
        font = JBFont.label().asBold()
        foreground = UiStyle.dim
        border = JBUI.Borders.compound(JBUI.Borders.customLineBottom(UiStyle.outline), JBUI.Borders.empty(3, 10))
        toolTipText = "The session no longer runs; its transcript stays readable until another session is attached."
    }

    /** The component to show: the banner, the scroll pane, and the jump bar below it. */
    val component: JComponent = JPanel(BorderLayout()).apply {
        add(endedBanner, BorderLayout.NORTH)
        add(JLayer(scroll, fade), BorderLayout.CENTER)
        add(jumpBar, BorderLayout.SOUTH)
    }

    /** The row components now, in order (for tests). */
    val rows: List<RowView> get() = order.mapNotNull { views[it] }

    /** How many rows `update` built or changed in its last call (for tests and measurements). */
    var lastChanged: Int = 0
        private set

    init {
        // Only the user's own scrolling decides whether the view follows new rows: the wheel,
        // the scroll bar, the keys. A changed height of rows or of the view never does.
        val bar = scroll.verticalScrollBar
        val byUser = { SwingUtilities.invokeLater { follow(atEnd()) } }
        scroll.addMouseWheelListener { byUser() }
        bar.addMouseListener(object : MouseAdapter() {
            override fun mouseReleased(e: MouseEvent) = byUser()
        })
        bar.addAdjustmentListener { if (bar.valueIsAdjusting) follow(atEnd()) }
        val resized = object : ComponentAdapter() {
            override fun componentResized(e: ComponentEvent) {
                if (stick) toEnd()
            }
        }
        list.addComponentListener(resized)
        scroll.viewport.addComponentListener(resized)
    }

    /**
     * Set whether the view follows new rows, and show the jump bar while it does not.
     *
     * @param on True to follow.
     */
    private fun follow(on: Boolean) {
        stick = on
        if (jumpBar.isVisible == on) jumpBar.isVisible = !on
    }

    /**
     * Tell whether the view shows its last row.
     *
     * @return True if the scroll bar is within a line of its end.
     */
    private fun atEnd(): Boolean {
        val bar = scroll.verticalScrollBar
        return bar.value + bar.visibleAmount >= bar.maximum - JBUI.scale(24)
    }

    /** Scroll to the last row and follow new rows again. */
    fun toEnd() {
        follow(true)
        SwingUtilities.invokeLater {
            val bar = scroll.verticalScrollBar
            bar.value = bar.maximum
        }
    }

    /**
     * Draw the rows of a tab (see the file comment for what is left alone). Keeps the scroll
     * position, except that a view at its end stays at the end.
     *
     * @param state The view state.
     * @param tab The tab's key.
     */
    fun update(state: ViewState, tab: String) {
        val items = state.items[tab].orEmpty()
        val attached = isAttached(state)
        val ended = transcriptEnded(state)
        lastChanged = 0
        if (items === lastItems && state.tools === lastTools && state.escalations === lastEscalations && attached == lastAttached && ended == lastEnded) return
        lastItems = items
        lastTools = state.tools
        lastEscalations = state.escalations
        lastAttached = attached
        if (ended != lastEnded) {
            lastEnded = ended
            endedBanner.isVisible = ended
            fade.faded = ended
            component.revalidate()
            component.repaint()
        }
        val held = heldTools(state)
        val ids = items.map { it.id }
        var structure = false
        if (ids != order) {
            structure = true
            val dropped = if (ids.isEmpty()) -1 else order.indexOf(ids[0])
            val kept = if (dropped >= 0) order.size - dropped else -1
            if (dropped >= 0 && kept <= ids.size && order.subList(dropped, order.size) == ids.subList(0, kept)) {
                // Rows left at the start (the bound on items), rows were added at the end.
                repeat(dropped) { list.remove(0) }
                for (id in order.subList(0, dropped)) { views.remove(id); drawn.remove(id) }
                for (id in ids.subList(kept, ids.size)) list.add(view(id))
            } else {
                val keep = ids.toSet()
                views.keys.retainAll(keep)
                drawn.keys.retainAll(keep)
                list.removeAll()
                for (id in ids) list.add(view(id))
            }
            order = ids
        }
        for (item in items) {
            val toolId = (item as? ToolItem)?.toolUseId
            val tool = toolId?.let { state.tools[it] }
            val escalation = (item as? EscalationItem)?.let { state.escalations[it.escalationKey] }
            val isHeld = toolId != null && toolId in held
            val before = drawn[item.id]
            val userMenuChanged = item is UserItem && before?.attached != attached
            if (before != null && before.item === item && before.tool === tool && before.escalation === escalation && before.held == isHeld &&
                !userMenuChanged && (escalation == null || (before.attached == attached && before.ended == ended))
            ) continue
            drawn[item.id] = Drawn(item, tool, escalation, isHeld, attached, ended)
            views[item.id]!!.update(rowOf(state, item), item, isHeld)
            lastChanged++
        }
        if (structure || lastChanged > 0) {
            list.revalidate()
            list.repaint()
            if (stick) toEnd()
        }
    }

    /**
     * Return the view of an item id, creating it if needed.
     *
     * @param id The item id.
     * @return The view.
     */
    private fun view(id: Int): RowView = views.getOrPut(id) { RowView(host) }
}
