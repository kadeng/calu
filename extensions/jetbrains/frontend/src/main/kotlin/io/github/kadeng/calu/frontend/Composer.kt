/**
 * The composer of the chat: where the user writes to Claude.
 *
 *     ┌──────────────────────────────────────────────────────────┐
 *     │ [thumbnail ×] [thumbnail ×]            attached images   │
 *     │ /mo                                    the text          │
 *     │ ┌ /model   Change the model of the session ┐ completions │
 *     │ Add image                  [■ Stop] [Send now] [Send]    │
 *     └──────────────────────────────────────────────────────────┘
 *       model ▾   effort ▾   level ▾                 42% · 84k
 *
 *     Enter          send for the next turn (while Claude works the message waits in the queue)
 *     Ctrl+Enter     send into the running turn
 *     Shift+Enter    a new line
 *     /…             slash commands: Up/Down choose, Tab or Enter take the completion
 *     Ctrl+V         an image on the clipboard is attached (PNG, JPEG, GIF, WebP; 5 MiB; four per message)
 *
 * The composer decides nothing about the session: it reports through `ComposerHost`. Must be
 * used on the EDT.
 */
package io.github.kadeng.calu.frontend

import com.intellij.icons.AllIcons
import com.intellij.openapi.fileChooser.FileChooser
import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory
import com.intellij.openapi.project.Project
import com.intellij.ui.CollectionListModel
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.logic.Completion
import io.github.kadeng.calu.logic.MAX_IMAGES
import io.github.kadeng.calu.logic.OutgoingImage
import io.github.kadeng.calu.logic.SlashResult
import io.github.kadeng.calu.logic.allCommands
import io.github.kadeng.calu.logic.checkImage
import io.github.kadeng.calu.logic.completions
import io.github.kadeng.calu.logic.outgoingImage
import io.github.kadeng.calu.logic.resolveSlash
import io.github.kadeng.calu.logic.sniffImageType
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.messageTarget
import kotlinx.serialization.json.JsonObject
import java.awt.BorderLayout
import java.awt.FlowLayout
import java.awt.Toolkit
import java.awt.datatransfer.DataFlavor
import java.awt.event.InputEvent
import java.awt.event.KeyAdapter
import java.awt.event.KeyEvent
import java.awt.image.BufferedImage
import java.io.ByteArrayOutputStream
import java.io.File
import javax.imageio.ImageIO
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.event.DocumentEvent
import javax.swing.event.DocumentListener

/** What the composer asks its surroundings to do. */
interface ComposerHost {
    /** The view state drawn now. */
    val current: ViewState

    /** The key of the visible tab ("main" or a subagent's id). */
    val activeTab: String

    /**
     * Send a message.
     *
     * @param text The text (may be empty if images are attached).
     * @param intoTurn True to deliver into the running turn, false for the next turn.
     * @param images The attached images.
     */
    fun send(text: String, intoTurn: Boolean, images: List<OutgoingImage>)

    /**
     * Run an action of the backend (a slash command that changes something).
     *
     * @param action The action's name.
     * @param args Its arguments.
     */
    fun act(action: String, args: JsonObject)

    /**
     * Open a piece of user interface a slash command asks for.
     *
     * @param action "model", "effort", "level", "honesty", "sessions", "checkers", "rename" or "settings".
     */
    fun slashUi(action: String)

    /** Interrupt Claude's running turn. */
    fun interrupt()
}

/**
 * The composer.
 *
 * @param project The project (parent of the file chooser).
 * @param host Receives what the user submits.
 */
class Composer(private val project: Project, private val host: ComposerHost) {
    private class Attached(val image: OutgoingImage, val bytes: ByteArray)

    private val attached = ArrayList<Attached>()
    private val input = object : JBTextArea(3, 20) {
        // The IDE's Paste action ends here (not in a key listener): an image on the clipboard is attached instead.
        override fun paste() {
            if (!pasteImage()) super.paste()
        }
    }.apply {
        lineWrap = true
        wrapStyleWord = true
        font = JBFont.label()
        border = JBUI.Borders.empty(2, 2, 4, 2)
        isOpaque = false
    }
    private val thumbs = JPanel(WrapLayout()).apply { isOpaque = false; isVisible = false }
    private val completionModel = CollectionListModel<Completion>()
    private val completionList = JBList(completionModel).apply {
        cellRenderer = object : com.intellij.ui.ColoredListCellRenderer<Completion?>() {
            override fun customizeCellRenderer(list: javax.swing.JList<out Completion?>, value: Completion?, index: Int, selected: Boolean, hasFocus: Boolean) {
                if (value == null) return
                append(value.label, com.intellij.ui.SimpleTextAttributes.REGULAR_BOLD_ATTRIBUTES)
                append("   " + value.description, com.intellij.ui.SimpleTextAttributes.GRAYED_ATTRIBUTES)
            }
        }
        isFocusable = false
        visibleRowCount = 6
    }
    private val completionPane = JBScrollPane(completionList).apply {
        isVisible = false
        border = JBUI.Borders.customLine(UiStyle.outline, 1)
        horizontalScrollBarPolicy = javax.swing.ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER
    }
    private val problem = JBLabel().apply { foreground = UiStyle.chipColor("error"); font = JBFont.small(); isVisible = false }
    private val attach = ActionLink("Add image") { chooseImages() }.apply {
        toolTipText = "Attach an image (PNG, JPEG, GIF or WebP; up to 5 MiB; $MAX_IMAGES per message). Paste attaches an image from the clipboard."
    }
    // None of the three takes the keyboard focus: while the text field is disabled (a finished
    // subagent's tab) the focus would move to "Stop", and a typed space would interrupt the turn.
    private val stop = JButton("Stop", AllIcons.Actions.Suspend).apply { toolTipText = "Interrupt Claude's running turn"; isFocusable = false }
    private val inject = JButton("Send Now").apply { toolTipText = "Send into the running turn: Claude reads it after the running tool calls (Ctrl+Enter)"; isFocusable = false }
    private val send = JButton("Send").apply { isFocusable = false }
    private var suppressCompletion = false
    private var canSend = false
    private var working = false

    /** The component to show. */
    val component: JComponent

    /** The text area (for tests and for the keyboard focus). */
    val inputArea: JBTextArea get() = input

    /** The images attached to the message being written (for tests). */
    val images: List<OutgoingImage> get() = attached.map { it.image }

    init {
        val card = RoundedPanel(BorderLayout(0, JBUI.scale(2)), fill = UiStyle.cardBackground, line = UiStyle.outline)
        card.border = JBUI.Borders.empty(6, 8, 4, 6)
        card.add(thumbs, BorderLayout.NORTH)
        val scroll = JBScrollPane(input).apply { border = JBUI.Borders.empty(); isOpaque = false; viewport.isOpaque = false }
        card.add(scroll, BorderLayout.CENTER)
        val bar = JPanel(BorderLayout(JBUI.scale(6), 0)).apply { isOpaque = false }
        val left = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), 0)).apply { isOpaque = false; add(attach); add(problem) }
        val right = JPanel(FlowLayout(FlowLayout.RIGHT, JBUI.scale(4), 0)).apply { isOpaque = false; add(stop); add(inject); add(send) }
        bar.add(left, BorderLayout.CENTER)
        bar.add(right, BorderLayout.EAST)
        card.add(bar, BorderLayout.SOUTH)
        component = JPanel(BorderLayout(0, JBUI.scale(2))).apply {
            isOpaque = false
            add(completionPane, BorderLayout.NORTH)
            add(card, BorderLayout.CENTER)
        }
        for (b in listOf(stop, inject, send)) b.isDefaultCapable = false
        send.addActionListener { submit(false) }
        inject.addActionListener { submit(true) }
        stop.addActionListener { host.interrupt() }
        input.addKeyListener(object : KeyAdapter() {
            override fun keyPressed(e: KeyEvent) = onKey(e)
        })
        input.document.addDocumentListener(object : DocumentListener {
            override fun insertUpdate(e: DocumentEvent) = textChanged()
            override fun removeUpdate(e: DocumentEvent) = textChanged()
            override fun changedUpdate(e: DocumentEvent) {}
        })
    }

    /** The composer's text. Setting it does not send anything. */
    var text: String
        get() = input.text
        set(value) {
            input.text = value
            input.caretPosition = value.length
        }

    /**
     * Draw what depends on the session: who receives the message, which buttons work.
     *
     * @param state The view state.
     * @param tab The visible tab.
     */
    fun render(state: ViewState, tab: String) {
        val isAttached = isAttached(state)
        val target = messageTarget(state, tab)
        canSend = isAttached && target.to != null
        working = isAttached && state.session.state !in setOf("idle", "stopped", "failed")
        input.isEnabled = canSend
        input.emptyText.text = when {
            !isAttached -> "Start or attach a session to chat"
            target.reason != null -> target.reason!!
            else -> "Message to ${target.label}  (/ for commands)"
        }
        send.isEnabled = canSend
        send.text = if (working) "Queue" else "Send"
        send.toolTipText = if (working) "Send for the next turn: the message waits until Claude's turn ends (Enter)" else "Send (Enter). Shift+Enter starts a new line."
        inject.isVisible = working
        inject.isEnabled = canSend
        stop.isVisible = working
        attach.isEnabled = canSend
    }

    /**
     * Show a problem below the text (a refused image, a slash command that cannot run).
     *
     * @param text The sentence; "" hides the line.
     */
    fun showProblem(text: String) {
        problem.text = text
        problem.toolTipText = text
        problem.isVisible = text.isNotEmpty()
    }

    /** Give the keyboard focus to the text area. */
    fun focus() {
        input.requestFocusInWindow()
    }

    /**
     * Handle a key in the text area (see the table in the file comment).
     *
     * @param e The key event; consumed if it was handled here.
     */
    private fun onKey(e: KeyEvent) {
        val completing = completionPane.isVisible && !completionModel.isEmpty
        val control = e.modifiersEx and (InputEvent.CTRL_DOWN_MASK or InputEvent.META_DOWN_MASK) != 0
        when {
            completing && e.keyCode == KeyEvent.VK_DOWN -> { move(1); e.consume() }
            completing && e.keyCode == KeyEvent.VK_UP -> { move(-1); e.consume() }
            completing && e.keyCode == KeyEvent.VK_ESCAPE -> { hideCompletions(); suppressCompletion = true; e.consume() }
            completing && (e.keyCode == KeyEvent.VK_TAB || (e.keyCode == KeyEvent.VK_ENTER && !control && !e.isShiftDown)) -> {
                e.consume()
                val chosen = completionList.selectedValue ?: completionModel.getElementAt(0)
                // Enter on a command that is already typed in full submits it.
                if (e.keyCode == KeyEvent.VK_ENTER && chosen.insert.trim() == input.text.trim()) submit(false) else take(chosen)
            }
            e.keyCode == KeyEvent.VK_ENTER && !e.isShiftDown -> { e.consume(); submit(control) }
        }
    }

    /**
     * Move the selection of the completion list.
     *
     * @param by +1 for the next entry, -1 for the previous one; the selection wraps around.
     */
    private fun move(by: Int) {
        val n = completionModel.size
        if (n == 0) return
        completionList.selectedIndex = ((maxOf(0, completionList.selectedIndex) + by) % n + n) % n
        completionList.ensureIndexIsVisible(completionList.selectedIndex)
    }

    /**
     * Take a completion: put its text into the input and submit it if it is complete.
     *
     * @param c The completion.
     */
    private fun take(c: Completion) {
        text = c.insert
        if (c.submit) submit(false)
    }

    /** Refresh the completion list after the text changed. */
    private fun textChanged() {
        if (problem.isVisible) showProblem("")
        val current = input.text
        if (!current.startsWith("/")) suppressCompletion = false
        val found = if (suppressCompletion) emptyList() else completions(current, allCommands())
        if (found.isEmpty()) {
            hideCompletions()
            return
        }
        completionModel.replaceAll(found)
        completionList.selectedIndex = 0
        completionList.visibleRowCount = minOf(6, found.size)
        if (!completionPane.isVisible) {
            completionPane.isVisible = true
            component.revalidate()
        } else {
            completionPane.revalidate()
        }
    }

    /** Hide the completion list. */
    private fun hideCompletions() {
        if (!completionPane.isVisible) return
        completionPane.isVisible = false
        component.revalidate()
    }

    /**
     * Submit the text: a slash command of calu runs here, everything else is sent to Claude.
     * The text is kept if nothing could be sent.
     *
     * @param intoTurn True to deliver into the running turn.
     */
    private fun submit(intoTurn: Boolean) {
        val current = input.text
        if (current.isBlank() && attached.isEmpty()) return
        hideCompletions()
        if (current.startsWith("/") && attached.isEmpty()) {
            when (val r = resolveSlash(current, host.current.sessions.attachedId)) {
                is SlashResult.Act -> { host.act(r.action, r.args); clear(); return }
                is SlashResult.Ui -> { clear(); host.slashUi(r.action); return }
                is SlashResult.Error -> { showProblem(r.text); return }
                SlashResult.Chat -> {}
            }
        }
        if (!canSend) {
            showProblem("Nothing is attached that could read this message.")
            return
        }
        host.send(current, intoTurn && working, attached.map { it.image })
        clear()
    }

    /** Empty the text and the attached images. */
    fun clear() {
        input.text = ""
        attached.clear()
        drawThumbs()
    }

    /** Let the user choose image files and attach them. */
    private fun chooseImages() {
        if (!attach.isEnabled) return
        val descriptor = FileChooserDescriptorFactory.createMultipleFilesNoJarsDescriptor().withTitle("Attach Images")
        for (file in FileChooser.chooseFiles(descriptor, project, null)) {
            val bytes = runCatching { File(file.path).readBytes() }.getOrNull()
            if (bytes == null) showProblem("${file.name} could not be read.") else addImage(bytes, file.name)
        }
    }

    /**
     * Attach the clipboard's image or image files, if it holds any.
     *
     * @return True if the clipboard held an image (attached or refused); false lets the text paste happen.
     */
    private fun pasteImage(): Boolean {
        val content = runCatching { Toolkit.getDefaultToolkit().systemClipboard.getContents(null) }.getOrNull() ?: return false
        if (content.isDataFlavorSupported(DataFlavor.javaFileListFlavor)) {
            val files = (runCatching { content.getTransferData(DataFlavor.javaFileListFlavor) }.getOrNull() as? List<*>).orEmpty().filterIsInstance<File>()
            val pictures = files.mapNotNull { f -> runCatching { f.readBytes() }.getOrNull()?.takeIf { sniffImageType(it) != null }?.let { f.name to it } }
            if (pictures.isNotEmpty()) {
                for ((name, bytes) in pictures) addImage(bytes, name)
                return true
            }
        }
        if (content.isDataFlavorSupported(DataFlavor.imageFlavor) && !content.isDataFlavorSupported(DataFlavor.stringFlavor)) {
            val image = runCatching { content.getTransferData(DataFlavor.imageFlavor) }.getOrNull() as? java.awt.Image ?: return false
            val buffered = image as? BufferedImage ?: BufferedImage(image.getWidth(null), image.getHeight(null), BufferedImage.TYPE_INT_ARGB).also {
                val g = it.createGraphics()
                g.drawImage(image, 0, 0, null)
                g.dispose()
            }
            val out = ByteArrayOutputStream()
            ImageIO.write(buffered, "png", out)
            addImage(out.toByteArray(), "the pasted image")
            return true
        }
        return false
    }

    /**
     * Attach one image after the checks of `checkImage`; a refused image is reported below the text.
     *
     * @param bytes The image file's bytes.
     * @param name How to name the image in a refusal.
     */
    fun addImage(bytes: ByteArray, name: String) {
        val type = sniffImageType(bytes)
        if (type == null) {
            showProblem("$name is not a PNG, JPEG, GIF or WebP image.")
            return
        }
        val refusal = checkImage(type, bytes.size.toLong(), attached.size, messageTarget(host.current, host.activeTab).to == MAIN)
        if (refusal != null) {
            showProblem(refusal)
            return
        }
        attached.add(Attached(outgoingImage(type, bytes), bytes))
        drawThumbs()
    }

    /** Draw the strip of attached images, each with a button that removes it. */
    private fun drawThumbs() {
        thumbs.removeAll()
        for (a in attached.toList()) {
            val cell = JPanel(BorderLayout()).apply { isOpaque = false }
            cell.add(thumbnail(a.bytes, JBUI.scale(56)) ?: JBLabel("image"), BorderLayout.CENTER)
            val remove = JBLabel(AllIcons.Actions.Close)
            clickable(remove, "Remove this image") { attached.remove(a); drawThumbs() }
            cell.add(remove, BorderLayout.EAST)
            thumbs.add(cell)
        }
        thumbs.isVisible = attached.isNotEmpty()
        component.revalidate()
        component.repaint()
    }
}
