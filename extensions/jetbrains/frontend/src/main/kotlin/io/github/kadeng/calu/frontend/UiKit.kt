/**
 * The small set of Swing parts the chat is built from, all in the IDE's look: colours from the
 * theme, fonts from the label font and the editor scheme, sizes scaled with `JBUI`.
 *
 *     UiStyle       colours and fonts
 *     RoundedPanel  a card: rounded background, optional outline and a coloured stripe on the left
 *     Chip          a small rounded badge (a decision, a count, a state)
 *     WrapLayout    a flow layout whose preferred height follows the width it is given
 *     htmlPane / textArea   read-only texts that wrap to their width
 *
 * Everything here must be used on the EDT.
 */
package io.github.kadeng.calu.frontend

import com.intellij.ide.BrowserUtil
import com.intellij.openapi.editor.colors.EditorColorsManager
import com.intellij.openapi.editor.colors.EditorFontType
import com.intellij.ui.ColorUtil
import com.intellij.ui.JBColor
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextArea
import com.intellij.util.ui.HTMLEditorKitBuilder
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import com.intellij.util.ui.UIUtil
import java.awt.Color
import java.awt.Component
import java.awt.Container
import java.awt.Cursor
import java.awt.Dimension
import java.awt.Font
import java.awt.Graphics
import java.awt.Graphics2D
import java.awt.LayoutManager
import java.awt.Rectangle
import java.awt.RenderingHints
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.JComponent
import javax.swing.JEditorPane
import javax.swing.JPanel
import javax.swing.Scrollable
import javax.swing.SwingConstants
import javax.swing.event.HyperlinkEvent
import javax.swing.text.html.HTMLEditorKit

/** Theme-derived colours, fonts and spacings of the chat. */
object UiStyle {
    /** Space between rows, unscaled pixels. */
    const val GAP: Int = 4

    /** Corner arc of cards, unscaled pixels. */
    const val ARC: Int = 10

    /** Text colour of less prominent rows. */
    val dim: Color get() = UIUtil.getContextHelpForeground()

    /** Background of cards and of the user's own messages: the theme's text field background. */
    val cardBackground: Color get() = JBColor.namedColor("TextField.background", UIUtil.getTextFieldBackground())

    /** Background of code and command boxes: the editor's background. */
    val codeBackground: Color get() = EditorColorsManager.getInstance().globalScheme.defaultBackground

    /** Outline of cards. */
    val outline: Color get() = JBColor.border()

    /** Background of the selected tab chip. */
    val selection: Color get() = JBColor.namedColor("ActionButton.pressedBackground", JBColor(0xCFDEFC, 0x2E436E))

    /** The editor's fixed-width font in the size of the label font. */
    val mono: Font
        get() = EditorColorsManager.getInstance().globalScheme.getFont(EditorFontType.PLAIN).deriveFont(JBFont.label().size2D)

    /**
     * Colour of a chip or notice.
     *
     * @param kind "ok", "warning" (also "warn"), "error" (also "high"), "info" or anything else.
     * @return The theme's success, warning, error or link colour; the context help colour otherwise.
     */
    fun chipColor(kind: String): Color = when (kind) {
        "ok" -> JBColor.namedColor("Label.successForeground", JBColor(0x368746, 0x5FAD65))
        "warning", "warn" -> JBColor.namedColor("Label.warningForeground", JBColor(0xA46704, 0xD6AE58))
        "error", "high" -> JBColor.namedColor("Label.errorForeground", JBColor(0xDB3B4B, 0xF75464))
        "info" -> JBUI.CurrentTheme.Link.Foreground.ENABLED
        else -> dim
    }

    /**
     * Paint with smooth edges.
     *
     * @param g The graphics of a `paintComponent` call.
     * @return The same graphics as `Graphics2D`, with antialiasing switched on.
     */
    fun smooth(g: Graphics): Graphics2D = (g as Graphics2D).also {
        it.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON)
    }
}

/** A panel that takes the width of the scroll pane's viewport, so that its texts wrap. */
open class WidthTrackingPanel(layout: LayoutManager) : JPanel(layout), Scrollable {
    override fun getPreferredScrollableViewportSize(): Dimension = preferredSize
    override fun getScrollableUnitIncrement(visibleRect: Rectangle, orientation: Int, direction: Int): Int = JBUI.scale(24)
    override fun getScrollableBlockIncrement(visibleRect: Rectangle, orientation: Int, direction: Int): Int =
        if (orientation == SwingConstants.VERTICAL) visibleRect.height else visibleRect.width
    override fun getScrollableTracksViewportWidth(): Boolean = true
    override fun getScrollableTracksViewportHeight(): Boolean = false
}

/**
 * A card: a transparent panel that paints a rounded background, an optional outline and an
 * optional coloured stripe along its left edge.
 *
 * @param layout The layout of the card's content.
 * @property fill The background colour; `null` paints none.
 * @property line The outline colour; `null` paints none.
 * @property stripe The colour of the stripe on the left; `null` paints none.
 */
open class RoundedPanel(layout: LayoutManager, var fill: Color? = null, var line: Color? = null, var stripe: Color? = null) : JPanel(layout) {
    init {
        isOpaque = false
    }

    override fun paintComponent(g: Graphics) {
        val g2 = UiStyle.smooth(g.create())
        try {
            val arc = JBUI.scale(UiStyle.ARC)
            fill?.let { g2.color = it; g2.fillRoundRect(0, 0, width, height, arc, arc) }
            stripe?.let {
                val clip = g2.clip
                g2.clipRect(0, 0, JBUI.scale(3), height)
                g2.color = it
                g2.fillRoundRect(0, 0, arc * 2, height, arc, arc)
                g2.clip = clip
            }
            line?.let { g2.color = it; g2.drawRoundRect(0, 0, width - 1, height - 1, arc, arc) }
        } finally {
            g2.dispose()
        }
    }
}

/**
 * A small rounded badge: bold small text in a colour, on the same colour at low opacity.
 *
 * @param text The badge's text.
 * @param kind The colour class of `UiStyle.chipColor`.
 */
class Chip(text: String = "", kind: String = "") : JBLabel(text) {
    /** The colour class; setting it repaints the badge. */
    var kind: String = kind
        set(value) {
            field = value
            foreground = UiStyle.chipColor(value)
            repaint()
        }

    init {
        font = JBFont.small().asBold()
        border = JBUI.Borders.empty(1, 6)
        foreground = UiStyle.chipColor(kind)
    }

    override fun paintComponent(g: Graphics) {
        val g2 = UiStyle.smooth(g.create())
        try {
            g2.color = ColorUtil.withAlpha(foreground, 0.16)
            g2.fillRoundRect(0, 0, width, height, height, height)
        } finally {
            g2.dispose()
        }
        super.paintComponent(g)
    }
}

/**
 * A left-aligned flow layout that wraps: its preferred height is the height its components need
 * at the container's current width. Invisible components take no space.
 *
 * @param hgap Horizontal space between components, unscaled pixels.
 * @param vgap Vertical space between lines, unscaled pixels.
 */
class WrapLayout(private val hgap: Int = 6, private val vgap: Int = 4) : LayoutManager {
    override fun addLayoutComponent(name: String?, comp: Component?) {}
    override fun removeLayoutComponent(comp: Component?) {}
    override fun preferredLayoutSize(parent: Container): Dimension = arrange(parent, false)
    override fun minimumLayoutSize(parent: Container): Dimension = Dimension(0, arrange(parent, false).height)
    private var requested = -1

    override fun layoutContainer(parent: Container) {
        val needed = arrange(parent, true).height
        // The parent asked for the preferred height before it set the width. If the lines need
        // another height at this width, ask once for a second layout pass.
        if (needed != parent.height && needed != requested) {
            requested = needed
            javax.swing.SwingUtilities.invokeLater { parent.revalidate() }
        } else if (needed == parent.height) {
            requested = -1
        }
    }

    /**
     * Compute the lines for the container's current width and optionally place the components.
     *
     * @param parent The container.
     * @param place True to set the components' bounds.
     * @return The size the lines need (width of the widest line, height of all lines) with insets.
     */
    private fun arrange(parent: Container, place: Boolean): Dimension {
        val insets = parent.insets
        val h = JBUI.scale(hgap)
        val v = JBUI.scale(vgap)
        val available = (if (parent.width > 0) parent.width else Int.MAX_VALUE / 2) - insets.left - insets.right
        var x = 0
        var y = 0
        var lineHeight = 0
        var widest = 0
        for (c in parent.components) {
            if (!c.isVisible) continue
            val d = c.preferredSize
            val w = minOf(d.width, available)
            if (x > 0 && x + w > available) {
                x = 0
                y += lineHeight + v
                lineHeight = 0
            }
            if (place) c.setBounds(insets.left + x, insets.top + y, w, d.height)
            x += w + h
            widest = maxOf(widest, x - h)
            lineHeight = maxOf(lineHeight, d.height)
        }
        return Dimension(widest + insets.left + insets.right, y + lineHeight + insets.top + insets.bottom)
    }
}

/**
 * Create a read-only HTML pane for Markdown that wraps to its width (long words included) and
 * opens http, https and mailto links in the browser. Code uses the editor's font.
 *
 * @param onFile Called for a click on a file link of `linkFiles` (path, 1-based line or `null`);
 *   `null` if the pane shows no such links.
 * @return The pane (transparent, with the label font).
 */
fun htmlPane(onFile: ((path: String, line: Int?) -> Unit)? = null): JEditorPane = JEditorPane().apply {
    val kit = HTMLEditorKitBuilder().withWordWrapViewFactory().build()
    val mono = UiStyle.mono
    val code = ColorUtil.toHtmlColor(UiStyle.codeBackground)
    (kit as? HTMLEditorKit)?.styleSheet?.apply {
        addRule("code { font-family: '${mono.family}'; }")
        // A code block is `div.pre` (lines that wrap), not `pre`: Swing cuts a `pre` line that is wider than the view.
        addRule("div.pre { font-family: '${mono.family}'; background-color: $code; padding: 6px; margin-top: 4px; margin-bottom: 4px; }")
        addRule("p { margin-top: 2px; margin-bottom: 6px; }")
        addRule("th { border-bottom: 1px solid ${ColorUtil.toHtmlColor(UiStyle.outline)}; }")
        addRule("blockquote { margin-left: 8px; color: ${ColorUtil.toHtmlColor(UiStyle.dim)}; }")
    }
    editorKit = kit
    isEditable = false
    isOpaque = false
    border = JBUI.Borders.empty()
    putClientProperty(JEditorPane.HONOR_DISPLAY_PROPERTIES, true)
    font = JBFont.label()
    addHyperlinkListener { e ->
        val url = e.url
        if (e.eventType != HyperlinkEvent.EventType.ACTIVATED) return@addHyperlinkListener
        val file = e.description?.let { io.github.kadeng.calu.logic.parseFileLink(it) }
        if (file != null) onFile?.invoke(file.path, file.line)
        else if (url != null && url.protocol in setOf("http", "https", "mailto")) BrowserUtil.browse(url)
    }
}

/**
 * Create a read-only, wrapping plain-text area.
 *
 * @param text The text.
 * @param monospaced True for the editor's fixed-width font (commands, output).
 * @return The area (transparent, selectable).
 */
fun textArea(text: String, monospaced: Boolean = false): JBTextArea = JBTextArea(text).apply {
    isEditable = false
    lineWrap = true
    wrapStyleWord = !monospaced
    isOpaque = false
    border = JBUI.Borders.empty()
    font = if (monospaced) UiStyle.mono else JBFont.label()
}

/**
 * Make a component and its children react to a left click like a button.
 *
 * @param component The component; it gets the hand cursor.
 * @param tooltip The tooltip that names what the click does; "" for none.
 * @param onClick Called on the EDT for every left click.
 */
fun clickable(component: JComponent, tooltip: String = "", onClick: () -> Unit) {
    val listener = object : MouseAdapter() {
        override fun mouseClicked(e: MouseEvent) {
            if (e.button == MouseEvent.BUTTON1 && !e.isPopupTrigger) onClick()
        }
    }
    component.cursor = Cursor.getPredefinedCursor(Cursor.HAND_CURSOR)
    if (tooltip.isNotEmpty()) component.toolTipText = tooltip
    component.addMouseListener(listener)
}
