/**
 * The chat of the tool window, drawn with Swing and JetBrains components.
 *
 *     ┌ header: session ▾   activity (what Claude does, since when)      honesty ┐
 *     │ setup cards: what is missing, each with its button; the server command  │
 *     │ attention: decisions that wait in other tabs, steps that calu holds     │
 *     ├ tabs: Main | subagent … (wraps; finished ones fold away beyond six) ────┤
 *     │ transcript of the tab (TranscriptView)                                  │
 *     ├ messages that wait to be read ──────────────────────────────────────────┤
 *     │ composer (Composer)                                                     │
 *     └ model ▾  effort ▾  level ▾                              context usage ──┘
 *
 * `ChatSurface` is the seam for a second way to draw the chat (an embedded browser that reuses
 * the VSCode webviews, for local mode, after a spike); the native surface is the one that also
 * works in remote development. Everything here runs on the EDT; requests go through `ChatHost`.
 * What is shown is decided by the pure functions of `shared` (`sessionMenu`, `setupCards`,
 * `tabList`, `usageMeter`, `userMessageMenu`, ...); this file only draws their results.
 */
package io.github.kadeng.calu.frontend

import com.intellij.icons.AllIcons
import com.intellij.ide.BrowserUtil
import com.intellij.openapi.Disposable
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.DefaultActionGroup
import com.intellij.openapi.actionSystem.Separator
import com.intellij.openapi.actionSystem.impl.SimpleDataContext
import com.intellij.openapi.application.EDT
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.JBPopupMenu
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.ui.AnimatedIcon
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.components.JBTextField
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.chat.RowAction
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.CLAUDE_EFFORTS
import io.github.kadeng.calu.logic.CLAUDE_MODELS
import io.github.kadeng.calu.logic.CardAction
import io.github.kadeng.calu.logic.OutgoingImage
import io.github.kadeng.calu.logic.REWIND_CONFIRM_DETAIL
import io.github.kadeng.calu.logic.REWIND_CONFIRM_TITLE
import io.github.kadeng.calu.logic.ResolvedAction
import io.github.kadeng.calu.logic.SessionChoice
import io.github.kadeng.calu.logic.SessionMenuEntry
import io.github.kadeng.calu.logic.SessionPick
import io.github.kadeng.calu.logic.SetupCard
import io.github.kadeng.calu.logic.TabView
import io.github.kadeng.calu.logic.activityLine
import io.github.kadeng.calu.logic.buildSessionPicks
import io.github.kadeng.calu.logic.heldByAgent
import io.github.kadeng.calu.logic.neutraliseBidi
import io.github.kadeng.calu.logic.otherOpenEscalations
import io.github.kadeng.calu.logic.rateLimitLine
import io.github.kadeng.calu.logic.resolveItemAction
import io.github.kadeng.calu.logic.sessionLabel
import io.github.kadeng.calu.logic.sessionMenu
import io.github.kadeng.calu.logic.setupCards
import io.github.kadeng.calu.logic.tabList
import io.github.kadeng.calu.logic.usageCostLine
import io.github.kadeng.calu.logic.usageMeter
import io.github.kadeng.calu.logic.validateSessionName
import io.github.kadeng.calu.state.LEVELS
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.honestyChip
import io.github.kadeng.calu.state.honestyStoredNote
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.levelLabel
import io.github.kadeng.calu.state.levelOf
import io.github.kadeng.calu.state.sessionTitle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import com.intellij.ui.components.JBScrollPane
import java.awt.BorderLayout
import java.awt.CardLayout
import java.awt.Dimension
import java.awt.Graphics
import java.awt.datatransfer.StringSelection
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.Timer

private val LOG = com.intellij.openapi.diagnostic.Logger.getInstance(NativeChatSurface::class.java)

/** A way to draw the chat. `render` is called on the EDT with every new state. */
interface ChatSurface : Disposable {
    /** The component to put into the tool window. */
    val component: JComponent

    /**
     * Draw a state.
     *
     * @param state The view state.
     */
    fun render(state: ViewState)
}

/** Number of tabs up to which every tab has its own chip; beyond it finished subagents fold away. */
const val MAX_OPEN_TABS: Int = 6

/**
 * The chip of one tab in the tab strip: a status mark, the label, and what waits there.
 *
 *     ● Review the greeter          running
 *     ✓ Watch the test run          finished
 *     ⚠ Run the migration           an open decision (warning colour, bold; "· 2" for two)
 *     ● Lint · held                 a step calu holds back until the open decision is made
 *
 * @param onSelect Called with the tab's key on a click.
 */
private class TabChip(private val onSelect: (String) -> Unit) : JBLabel() {
    private var key: String = MAIN
    private var selected = false

    init {
        border = JBUI.Borders.empty(3, 8)
        clickable(this) { onSelect(key) }
    }

    /**
     * Draw a tab.
     *
     * @param tab The tab.
     * @param isSelected True for the visible tab.
     * @param held Number of the tab's steps that calu holds back.
     */
    fun show(tab: TabView, isSelected: Boolean, held: Int) {
        key = tab.key
        selected = isSelected
        val mark = when {
            tab.openEscalations > 0 -> "⚠ "
            tab.key == MAIN -> ""
            tab.status == "running" -> "● "
            tab.status == "failed" -> "✕ "
            else -> "✓ "
        }
        val label = neutraliseBidi(tab.label)
        val suffix = when {
            tab.openEscalations > 1 -> " · ${tab.openEscalations}"
            tab.openEscalations == 0 && held > 0 -> " · held"
            else -> ""
        }
        text = mark + (if (label.length > 22) label.take(21) + "…" else label) + suffix
        font = if (tab.openEscalations > 0 || isSelected) JBFont.label().asBold() else JBFont.label()
        foreground = when {
            tab.openEscalations > 0 || held > 0 -> UiStyle.chipColor("warning")
            tab.status == "failed" -> UiStyle.chipColor("error")
            isSelected || tab.key == MAIN || tab.status == "running" -> JBUI.CurrentTheme.Label.foreground()
            else -> UiStyle.dim
        }
        toolTipText = listOfNotNull(
            label,
            tab.status.takeIf { it.isNotEmpty() }?.let { if (tab.background) "$it, background" else it },
            if (tab.openEscalations > 0) "${tab.openEscalations} decision(s) wait for you here" else null,
            if (held > 0) "$held step(s) held until the open decision is made" else null,
            if (tab.attention && tab.openEscalations == 0) "new output" else null,
        ).joinToString(" · ")
        repaint()
    }

    override fun paintComponent(g: Graphics) {
        val g2 = UiStyle.smooth(g.create())
        try {
            val arc = JBUI.scale(UiStyle.ARC)
            if (selected) {
                g2.color = UiStyle.selection
                g2.fillRoundRect(0, 0, width, height, arc, arc)
            } else {
                g2.color = UiStyle.outline
                g2.drawRoundRect(0, 0, width - 1, height - 1, arc, arc)
            }
        } finally {
            g2.dispose()
        }
        super.paintComponent(g)
    }
}

/**
 * The native Swing surface.
 *
 * @param project The project (parent of dialogs and popups).
 * @param host State and actions.
 */
class NativeChatSurface(private val project: Project, private val host: ChatHost) : ChatSurface, RowHost, ComposerHost {
    private var state: ViewState = host.state.value
    private var active: String = MAIN
    private var handledFocus = 0
    private var focusComposerOnAttach = false
    private val drafts = HashMap<String?, String>()
    private var draftSession: String? = null
    private val dismissed = HashSet<String>()
    private val seen = HashMap<String, Int>()
    private var phaseState = ""
    private var phaseSince = System.currentTimeMillis()
    private var lastDuration: Long? = null
    private var turnSince = System.currentTimeMillis()
    private val seed = (System.nanoTime() % 1000).toInt()
    private var renders = 0
    private var renderTotalMs = 0.0
    private var renderMaxMs = 0.0

    private val sessionLink = ActionLink("No session") { sessionMenu() }.apply {
        setDropDownLinkIcon()
        font = JBFont.label().asBold()
    }
    private val activity = JBLabel().apply { foreground = UiStyle.dim }
    private val honesty = Chip().apply { isVisible = false }
    private val cards = WidthTrackingPanel(VerticalLayout(JBUI.scale(6))).apply { border = JBUI.Borders.empty(0, 10, 6, 10); isOpaque = false }

    /**
     * The setup cards in a scrolling area. Its preferred height is the cards' height, capped by
     * [cardsRoom], so that the composer and the footer below stay visible in a short tool window.
     */
    private val cardsScroll = object : JBScrollPane(cards, VERTICAL_SCROLLBAR_AS_NEEDED, HORIZONTAL_SCROLLBAR_NEVER) {
        override fun getPreferredSize(): Dimension {
            val wanted = super.getPreferredSize()
            val room = cardsRoom()
            return if (room == null || wanted.height <= room) wanted else Dimension(wanted.width, room)
        }
    }.apply { border = JBUI.Borders.empty(); isOpaque = false; viewport.isOpaque = false; isVisible = false }
    private var cardsShown: List<Any?> = emptyList()
    private val attention = JPanel(WrapLayout(8, 2)).apply { border = JBUI.Borders.empty(0, 10, 4, 10); isOpaque = false; isVisible = false }
    private var attentionShown: List<Any?> = emptyList()
    private val tabStrip = JPanel(WrapLayout(4, 4)).apply { border = JBUI.Borders.empty(0, 10, 6, 10); isOpaque = false; isVisible = false }
    private val chips = ArrayList<TabChip>()
    private val folded = ActionLink("") { foldedMenu() }.apply { setDropDownLinkIcon() }
    private var foldedTabs: List<TabView> = emptyList()
    private val deck = CardLayout()
    private val transcriptsPanel = JPanel(deck)
    private val transcripts = LinkedHashMap<String, TranscriptView>()
    private val queue = JPanel(VerticalLayout(JBUI.scale(2))).apply { border = JBUI.Borders.empty(2, 12); isOpaque = false; isVisible = false }
    private var queueShown: Any? = null
    private val composer = Composer(project, this)
    private val model = ActionLink("model") { modelMenu() }.apply { setDropDownLinkIcon(); toolTipText = "Model of the session" }
    private val effort = ActionLink("effort") { effortMenu() }.apply { setDropDownLinkIcon(); toolTipText = "Thinking effort of sessions started from now on" }
    private val level = ActionLink("level") { levelMenu() }.apply { setDropDownLinkIcon() }
    private val usage = Chip().apply { toolTipText = "" }
    private val root = JPanel(BorderLayout())
    private val header = JPanel(BorderLayout(JBUI.scale(10), 0))
    private val south = JPanel(VerticalLayout(JBUI.scale(4)))
    private val clock = Timer(1000) { renderActivity() }

    override val component: JComponent get() = root
    override val current: ViewState get() = state
    override val activeTab: String get() = active

    /** The text area of the composer (for tests). */
    val inputArea: JBTextArea get() = composer.inputArea

    /**
     * Return the transcript of a tab.
     *
     * @param tab The tab's key.
     * @return The transcript, or `null` if the tab does not exist (for tests).
     */
    fun transcript(tab: String): TranscriptView? = transcripts[tab]

    /** The keys of the tabs, in order (for tests). */
    val tabKeys: List<String> get() = transcripts.keys.toList()

    /** The texts of the setup cards shown now (for tests). */
    var hintTexts: List<String> = emptyList()
        private set

    init {
        header.apply {
            isOpaque = false
            border = JBUI.Borders.empty(6, 10)
            add(sessionLink, BorderLayout.WEST)
            add(activity, BorderLayout.CENTER)
            add(honesty, BorderLayout.EAST)
        }
        root.add(JPanel(VerticalLayout(0)).apply { isOpaque = false; add(header); add(cardsScroll); add(attention); add(tabStrip) }, BorderLayout.NORTH)
        root.add(transcriptsPanel, BorderLayout.CENTER)
        val footer = JPanel(WrapLayout(12, 2)).apply { isOpaque = false; add(model); add(effort); add(level); add(usage) }
        south.apply {
            isOpaque = false
            border = JBUI.Borders.empty(4, 8, 6, 8)
            add(queue); add(composer.component); add(footer)
        }
        root.add(south, BorderLayout.SOUTH)
        // A new height of the tool window changes the room of the cards: lay them out again.
        root.addComponentListener(object : java.awt.event.ComponentAdapter() {
            override fun componentResized(e: java.awt.event.ComponentEvent) = cardsScroll.revalidate()
        })
        clickable(honesty, "Code honesty: what is verified to say what it does. Click for the mode, the marks and a review.") { honestyMenu() }
        clickable(usage) { usageMenu() }
        host.onUi { method, params ->
            when (method) {
                "ui.restoreInput" -> params.str("text")?.let { if (composer.text.isBlank()) composer.text = it }
                // `ide.attach` found several sessions of the project directory: show them.
                "ui.pickSession" -> sessionMenu()
            }
        }
        render(state)
    }

    override fun dispose() {
        clock.stop()
    }

    override fun render(state: ViewState) {
        val started = System.nanoTime()
        draw(state)
        val ms = (System.nanoTime() - started) / 1_000_000.0
        renders++
        renderTotalMs += ms
        renderMaxMs = maxOf(renderMaxMs, ms)
        if (renders % 500 == 0 || ms > 100) {
            LOG.info("[calu] render ${"%.1f".format(ms)} ms; $renders renders, mean ${"%.2f".format(renderTotalMs / renders)} ms, max ${"%.1f".format(renderMaxMs)} ms, ${state.items[active]?.size ?: 0} rows in the tab")
        }
    }

    /**
     * Draw a state (the body of `render`, which measures it).
     *
     * @param state The view state.
     */
    private fun draw(state: ViewState) {
        val previous = this.state
        this.state = state
        if (state.session.state != phaseState) {
            val now = System.currentTimeMillis()
            val idle = setOf("", "idle", "stopped", "failed")
            if (phaseState in idle && state.session.state !in idle) turnSince = now
            if (phaseState !in idle && state.session.state == "idle") lastDuration = now - turnSince
            phaseState = state.session.state
            phaseSince = now
        }
        if (previous.session.sessionId != state.session.sessionId) {
            switchDraft()
            seen.clear()
            lastDuration = null
            // Another session: start from Main with fresh rows (ids of items and of subagents repeat between sessions).
            active = MAIN
            for (view in transcripts.values) transcriptsPanel.remove(view.component)
            transcripts.clear()
        }
        renderHeader()
        renderCards()
        renderTabs()
        renderAttention()
        renderQueue()
        renderFooter()
        composer.render(state, active)
        if (focusComposerOnAttach && isAttached(state)) {
            focusComposerOnAttach = false
            composer.focus()
        }
    }

    /** Keep the composer's text per session: store the old session's draft, show the new one's. */
    private fun switchDraft() {
        drafts[draftSession] = composer.text
        draftSession = state.session.sessionId
        composer.clear()
        composer.text = drafts[draftSession].orEmpty()
    }

    /** Draw the session link, the activity line and the honesty chip. */
    private fun renderHeader() {
        val s = state
        val label = sessionLabel(s)
        sessionLink.text = neutraliseBidi(label.label)
        sessionLink.toolTipText = "<html>" + io.github.kadeng.calu.logic.escapeHtml(neutraliseBidi(label.title)).replace("\n", "<br>") + "<br>Click to switch, start, resume, fork or rename a session.</html>"
        renderActivity()
        val chip = honestyChip(s.honesty)
        val stored = honestyStoredNote(s.honesty, s.honestyStored)
        honesty.isVisible = isAttached(s)
        honesty.text = listOf(chip.label.ifEmpty { "Honesty: off" }, stored).filter { it.isNotEmpty() }.joinToString(" · ")
        honesty.kind = when (chip.kind) { "clean" -> "ok"; "open" -> "warning"; "review" -> "info"; else -> "" }
    }

    /** Draw the activity line; while Claude works a timer calls this every second. */
    private fun renderActivity() {
        val s = state
        val attached = isAttached(s)
        // A turn passes through several states (running, checking, tool_call): the time shown is the turn's,
        // except while a decision waits, where it is the time waited, counted from the session's own time
        // for the oldest open step (`waitingSince`), so that a restarted bridge or a reattach does not
        // start it again.
        val open = io.github.kadeng.calu.logic.decisionsWaiting(s)
        val now = System.currentTimeMillis()
        val since = when {
            open > 0 || s.session.state == "escalated" -> io.github.kadeng.calu.logic.waitingSince(
                s.escalations.values.filter { it.status == io.github.kadeng.calu.state.EscalationStatus.OPEN }.map { it.openedAt },
                if (s.session.state == "escalated") phaseSince else turnSince,
                now,
            )
            else -> turnSince
        }
        val a = activityLine(attached, s.session.state, since, lastDuration, now, seed)
        val ended = io.github.kadeng.calu.logic.transcriptEnded(s)
        activity.text = when {
            s.link.status == LinkStatus.RECONNECTING -> "Reconnecting to the session…"
            ended -> "Session ended"
            s.link.status == LinkStatus.LOST -> "The session's connection is lost"
            !attached -> ""
            open > 0 -> "$open waiting for you · ${a.elapsed}"
            a.active -> listOf(a.label, a.elapsed).filter { it.isNotEmpty() }.joinToString(" ")
            else -> a.label
        }
        activity.icon = if (attached && a.kind == "busy") AnimatedIcon.Default() else null
        activity.foreground = when {
            ended -> UiStyle.dim
            s.link.status == LinkStatus.LOST -> UiStyle.chipColor("error")
            attached && open > 0 -> UiStyle.chipColor("warning")
            else -> UiStyle.dim
        }
        if (attached && a.active) { if (!clock.isRunning) clock.start() } else clock.stop()
    }

    /** Draw the setup cards (`setupCards`): what is missing and the button that fixes it. */
    private fun renderCards() {
        val list = setupCards(state, dismissed)
        val key = listOf(list, state.serverHelp, if (list.any { it.sessionList }) state.sessions.list else null)
        if (key == cardsShown) return
        cardsShown = key
        hintTexts = list.map { it.message } + listOfNotNull(state.serverHelp?.command?.takeIf { list.any { c -> c.serverHelp } })
        cards.removeAll()
        for (card in list) cards.add(if (card.compact) compactCard(card) else setupCard(card))
        cardsScroll.isVisible = list.isNotEmpty()
        cards.revalidate()
        cardsScroll.revalidate()
        cards.repaint()
    }

    /**
     * Compute the height the setup cards may take: the tool window's height minus the header,
     * the attention line, the tabs, the composer with its footer and a strip of the transcript.
     *
     * In a very short window the transcript's strip is given up first, then the cards; the
     * composer and the footer are never covered.
     *
     * @return The height in pixels (0 or more), or `null` before the window is laid out.
     */
    private fun cardsRoom(): Int? {
        if (root.height <= 0) return null
        val taken = header.preferredSize.height + south.preferredSize.height +
            (if (attention.isVisible) attention.preferredSize.height else 0) +
            (if (tabStrip.isVisible) tabStrip.preferredSize.height else 0)
        val free = root.height - taken
        return maxOf(0, if (free > JBUI.scale(100)) free - JBUI.scale(40) else free)
    }

    /**
     * Build a setup card: the sentence, the server command or the running sessions if the card
     * has them, and its buttons.
     *
     * @param card The card.
     * @return The component.
     */
    private fun setupCard(card: SetupCard): JComponent {
        val color = if (card.level == "info") null else UiStyle.chipColor(card.level)
        val panel = RoundedPanel(VerticalLayout(JBUI.scale(6)), fill = UiStyle.cardBackground, line = UiStyle.outline, stripe = color)
        panel.border = JBUI.Borders.empty(8, 12, 8, 10)
        val head = JPanel(BorderLayout(JBUI.scale(6), 0)).apply { isOpaque = false }
        if (card.busy) head.add(JBLabel(AnimatedIcon.Default()), BorderLayout.WEST)
        head.add(textArea(card.message), BorderLayout.CENTER)
        panel.add(head)
        val help = state.serverHelp
        if (card.serverHelp && help != null) {
            panel.add(textArea(help.note + if (help.detach.isNotEmpty()) " Detach with ${help.detach}." else "").apply { foreground = UiStyle.dim })
            panel.add(JBTextField(help.command).apply { isEditable = false; font = UiStyle.mono })
        }
        if (card.sessionList) {
            for (e in state.sessions.list.take(8)) {
                val marks = listOfNotNull(e.state, if (e.openEscalations > 0) "${e.openEscalations} waiting" else null, if (e.stale) "stale" else null).joinToString(", ")
                panel.add(ActionLink("${neutraliseBidi(sessionTitle(e))}  ($marks) · ${e.id.take(6)}") { host.act("attach", args("session" to e.id)) }.apply { toolTipText = e.directory })
            }
            if (state.sessions.list.size > 8) panel.add(ActionLink("All ${state.sessions.list.size} sessions…") { sessionMenu() })
        }
        val buttons = JPanel(WrapLayout(8, 4)).apply { isOpaque = false }
        for (a in listOfNotNull(card.primary) + card.secondary) {
            buttons.add(JButton(a.label).apply { isDefaultCapable = false; addActionListener { cardAction(a) } })
        }
        if (buttons.componentCount > 0) panel.add(buttons)
        return panel
    }

    /**
     * Build the one-line form of a card that matters less while a session works.
     *
     * @param card A compact card.
     * @return The line: the short text, its button as a link, and "Dismiss".
     */
    private fun compactCard(card: SetupCard): JComponent {
        val line = JPanel(WrapLayout(8, 0)).apply { isOpaque = false }
        line.add(JBLabel(card.short ?: card.message, when (card.level) { "error" -> AllIcons.General.Error; "info" -> AllIcons.General.Information; else -> AllIcons.General.Warning }, JBLabel.LEADING).apply { toolTipText = card.message })
        card.primary?.let { a -> line.add(ActionLink(a.label) { cardAction(a) }) }
        line.add(ActionLink("Dismiss") { dismissed.add(card.id); cardsShown = emptyList(); renderCards() })
        return line
    }

    /**
     * Run the button of a setup card.
     *
     * @param a The button.
     */
    private fun cardAction(a: CardAction) {
        when (a.action) {
            "openLink" -> a.href?.let { BrowserUtil.browse(it) }
            "openSettings" -> ShowSettingsUtil.getInstance().showSettingsDialog(project, "calu")
            "install" -> host.act("install")
            "startServer" -> host.act("openTerminal")
            "copyServerCommand" -> state.serverHelp?.let { CopyPasteManager.getInstance().setContents(StringSelection(it.command)) }
            "newSession" -> newSession()
            "attach" -> if (state.sessions.list.isEmpty()) resumeMenu() else sessionMenu()
            else -> if (a.action.startsWith("dismiss:")) {
                // "Not now" on a card: hide it until the IDE restarts.
                dismissed.add(a.action.removePrefix("dismiss:"))
                cardsShown = emptyList()
                renderCards()
            } else {
                host.act(a.action)
            }
        }
    }

    /** Draw the tab strip (Main and one chip per subagent) and the transcript of the visible tab. */
    private fun renderTabs() {
        val wanted = listOf(MAIN) + state.agents.map { it.agentId }.filter { it != MAIN }
        if (wanted != transcripts.keys.toList()) {
            for (gone in transcripts.keys - wanted.toSet()) transcriptsPanel.remove(transcripts[gone]!!.component)
            val ordered = LinkedHashMap<String, TranscriptView>()
            for (key in wanted) ordered[key] = transcripts[key] ?: TranscriptView(this).also { transcriptsPanel.add(it.component, key) }
            transcripts.clear()
            transcripts.putAll(ordered)
            if (active !in transcripts) active = MAIN
            deck.show(transcriptsPanel, active)
        }
        val focus = state.focus
        if (focus != null && focus.seq > handledFocus) {
            handledFocus = focus.seq
            if (focus.tab in transcripts) select(focus.tab, render = false)
        }
        // Only the visible tab is drawn; a hidden one is drawn when it is selected.
        transcripts[active]?.update(state, active)
        state.items[active]?.lastOrNull()?.let { seen[active] = it.id }
        val tabs = tabList(state, active, seen)
        val held = heldByAgent(state)
        val fold = tabs.size > MAX_OPEN_TABS
        val shown = tabs.filter { !fold || it.key == MAIN || it.key == active || it.status == "running" || it.openEscalations > 0 || (held[it.key] ?: 0) > 0 }
        foldedTabs = tabs - shown.toSet()
        while (chips.size < shown.size) chips.add(TabChip { select(it) })
        val components = shown.indices.map { chips[it] as JComponent } + listOfNotNull(folded.takeIf { foldedTabs.isNotEmpty() })
        if (tabStrip.components.toList() != components) {
            tabStrip.removeAll()
            for (c in components) tabStrip.add(c)
        }
        shown.forEachIndexed { i, tab -> chips[i].show(tab, tab.key == active, held[tab.key] ?: 0) }
        folded.text = "${foldedTabs.size} finished"
        tabStrip.isVisible = tabs.size > 1
        tabStrip.revalidate()
        tabStrip.repaint()
    }

    /**
     * Show a tab.
     *
     * @param tab The tab's key; an unknown key is ignored.
     * @param render True to draw the tabs again now (false inside `renderTabs`).
     */
    private fun select(tab: String, render: Boolean = true) {
        if (tab !in transcripts) return
        active = tab
        deck.show(transcriptsPanel, tab)
        if (render) {
            renderTabs()
            renderAttention()
            composer.render(state, active)
            // The user chose the tab to write there: the text field gets the keyboard (if the
            // tab takes messages), so that typing never reaches another control.
            composer.focus()
        }
    }

    /** Offer the folded (finished) subagents in a popup; choosing one shows its tab. */
    private fun foldedMenu() {
        JBPopupFactory.getInstance().createPopupChooserBuilder(foldedTabs)
            .setTitle("Finished Subagents")
            .setRenderer(textRenderer<TabView> { "${neutraliseBidi(it.label)}  (${it.status})" })
            .setNamerForFiltering { it.label }
            .setItemChosenCallback { select(it.key) }
            .createPopup().showUnderneathOf(folded)
    }

    /** Draw the attention strip: decisions that wait in other tabs, and the steps calu holds back. */
    private fun renderAttention() {
        val others = otherOpenEscalations(state, active)
        val held = heldByAgent(state).values.sum()
        val key = listOf(others, held)
        if (key == attentionShown) return
        attentionShown = key
        attention.removeAll()
        // One line, however many wait: the tab chips name the tabs, this line leads to the next one.
        if (others.isNotEmpty()) {
            val n = others.size
            attention.add(JBLabel("$n decision${if (n == 1) " waits" else "s wait"} in ${if (n == 1) "another tab" else "other tabs"}", AllIcons.General.Warning, JBLabel.LEADING).apply {
                foreground = UiStyle.chipColor("warning")
                toolTipText = "<html>" + others.joinToString("<br>") { io.github.kadeng.calu.logic.escapeHtml(neutraliseBidi(it.label)) } + "</html>"
            })
            attention.add(ActionLink("Show next") { select(others.first().tab) })
        }
        if (held > 0) {
            attention.add(JBLabel("$held step${if (held == 1) "" else "s"} held until you decide").apply {
                foreground = UiStyle.dim
                toolTipText = "While a decision is open, calu lets no other checked step run. A step held for five minutes asks for a decision of its own."
            })
        }
        attention.isVisible = attention.componentCount > 0
        attention.revalidate()
        attention.repaint()
    }

    /** Draw the messages that wait to be read by Claude. */
    private fun renderQueue() {
        if (state.queue === queueShown) return
        queueShown = state.queue
        queue.removeAll()
        for (q in state.queue) {
            val how = if (q.delivery == "inject") "into the running turn" else "for the next turn"
            queue.add(JBLabel("Waiting ($how): " + neutraliseBidi(q.text.lineSequence().first()).take(120), AllIcons.General.ArrowRight, JBLabel.LEADING).apply {
                foreground = UiStyle.dim
                toolTipText = "Claude has not read this message yet."
            })
        }
        queue.isVisible = state.queue.isNotEmpty()
        queue.revalidate()
    }

    /** Draw the selectors below the composer and the context usage. */
    private fun renderFooter() {
        val s = state
        val attached = isAttached(s)
        model.text = s.session.model?.takeIf { it.isNotEmpty() } ?: "default model"
        model.isVisible = attached
        level.isVisible = attached
        effort.text = "effort: " + (s.session.effort ?: "default")
        level.text = levelLabel(s.autoEscalate)
        level.isEnabled = attached
        level.toolTipText = "Supervision level: what waits for you. " + (LEVELS.firstOrNull { it.id == levelOf(s.autoEscalate) }?.description ?: "")
        val working = attached && s.session.state !in setOf("idle", "stopped", "failed")
        val meter = usageMeter(s.usage, working)
        usage.isVisible = attached
        usage.text = if (meter.percent == null) meter.label else "ctx ${meter.label}"
        usage.kind = when (meter.level) { "ok" -> "ok"; "warn" -> "warning"; "high" -> "error"; else -> "" }
        usage.toolTipText = meter.title
    }

    // ------------------------------------------------------------------ ComposerHost

    override fun send(text: String, intoTurn: Boolean, images: List<OutgoingImage>) {
        host.act("send", buildJsonObject {
            put("text", JsonPrimitive(text)); put("inject", JsonPrimitive(intoTurn)); put("tab", JsonPrimitive(active))
            put("images", JsonArray(images.map { buildJsonObject { put("data", JsonPrimitive(it.data)); put("mediaType", JsonPrimitive(it.mediaType)) } }))
        })
        transcripts[active]?.toEnd()
    }

    override fun act(action: String, args: JsonObject) {
        if (action == "start" && args.str("kind") == "new") newSession() else host.act(action, args)
    }

    override fun slashUi(action: String) {
        when (action) {
            "model" -> modelMenu()
            "effort" -> effortMenu()
            "level" -> levelMenu()
            "honesty" -> honestyMenu()
            "sessions" -> sessionMenu()
            "checkers" -> chooseChecker(project, host)
            "rename" -> rename()
            "settings" -> showCaluSettings(project, host)
        }
    }

    override fun interrupt() = host.act("interrupt")

    // ------------------------------------------------------------------ RowHost

    override fun handle(action: RowAction) {
        when (action.name) {
            "openTab" -> action.args.str("tab")?.let { select(it) }
            else -> host.act(action.name, action.args)
        }
    }

    override fun userAction(id: String, item: UserItem) {
        val where = buildJsonObject { put("turn", JsonPrimitive(item.turn)); item.uuid?.let { put("uuid", JsonPrimitive(it)) } }
        when (id) {
            "copy" -> CopyPasteManager.getInstance().setContents(StringSelection(item.text))
            "forkAt" -> host.act("forkAt", where)
            "rewind" -> if (Messages.showOkCancelDialog(project, REWIND_CONFIRM_DETAIL, REWIND_CONFIRM_TITLE, "Rewind", "Cancel", Messages.getQuestionIcon()) == Messages.OK) {
                focusComposerOnAttach = true
                host.act("rewind", where)
            }
        }
    }

    override fun toolAction(id: String, toolUseId: String) {
        val resolved = try {
            resolveItemAction(state.tools[toolUseId], id, state.session.workspace.orEmpty())
        } catch (error: IllegalArgumentException) {
            composer.showProblem(error.message.orEmpty())
            return
        }
        when (resolved) {
            is ResolvedAction.Copy -> CopyPasteManager.getInstance().setContents(StringSelection(resolved.text))
            is ResolvedAction.ShowEdit -> host.act("openDiff", args("tool_use_id" to resolved.toolUseId))
            is ResolvedAction.OpenFile -> host.act(
                if (resolved.action == io.github.kadeng.calu.logic.ItemActionId.SHOW_UNCOMMITTED_CHANGES) "showChanges" else "openFile",
                buildJsonObject { put("path", JsonPrimitive(resolved.path)); put("line", JsonPrimitive(resolved.line)) },
            )
        }
    }

    override fun decide(key: String, allow: Boolean, note: String) =
        host.act("decide", buildJsonObject { put("key", JsonPrimitive(key)); put("allow", JsonPrimitive(allow)); if (note.isNotEmpty()) put("note", JsonPrimitive(note)) })

    override fun answer(key: String, answer: String) =
        host.act("decide", buildJsonObject { put("key", JsonPrimitive(key)); put("allow", JsonPrimitive(false)); put("answer", JsonPrimitive(answer)) })

    override fun showDiff(toolUseId: String) = host.act("openDiff", args("tool_use_id" to toolUseId))

    override fun why(escalationId: String) = host.act("showContext", args("escalation_id" to escalationId))

    // ------------------------------------------------------------------ menus

    /** Start a new session at once; the composer gets the keyboard focus when it is attached. */
    fun newSession() {
        focusComposerOnAttach = true
        host.act("start", args("kind" to "new"))
    }

    /** Ask for the attached session's name and store it. */
    private fun rename() {
        val attached = state.sessions.list.find { it.id == state.sessions.attachedId } ?: return
        val name = Messages.showInputDialog(project, "Name of the session (empty removes it):", "calu: Rename Session", null, attached.name, object : com.intellij.openapi.ui.InputValidatorEx {
            override fun getErrorText(inputString: String): String? = validateSessionName(inputString)
            override fun checkInput(inputString: String): Boolean = validateSessionName(inputString) == null
            override fun canClose(inputString: String): Boolean = checkInput(inputString)
        }) ?: return
        host.act("rename", buildJsonObject { put("session_id", JsonPrimitive(attached.id)); put("name", JsonPrimitive(name)) })
    }

    /**
     * Show the session menu (`sessionMenu`): the running sessions under their headings, then the
     * actions. The popup filters by typing, so that a session is found among many.
     */
    private fun sessionMenu() {
        val group = DefaultActionGroup()
        var actionsStarted = false
        for (entry in sessionMenu(state)) {
            when (entry) {
                is SessionMenuEntry.Heading -> group.add(Separator.create(entry.label))
                is SessionMenuEntry.Note -> group.add(menuAction(entry.label, enabled = false) {})
                is SessionMenuEntry.Session -> {
                    val marks = listOfNotNull(
                        entry.state, if (entry.openEscalations > 0) "${entry.openEscalations} waiting" else null, if (entry.stale) "stale" else null,
                    ).joinToString(", ")
                    val icon = when {
                        entry.attached -> AllIcons.Actions.Checked
                        entry.openEscalations > 0 -> AllIcons.General.Warning
                        else -> AllIcons.Empty
                    }
                    // Sessions of one directory share their title until they are named: the id tells them apart.
                    group.add(menuAction("${neutraliseBidi(entry.title)}   $marks · ${entry.directory} · ${entry.id.take(6)}", icon = icon, description = entry.directoryFull) {
                        if (!entry.attached) host.act("attach", args("session" to entry.id))
                    })
                }
                is SessionMenuEntry.Action -> {
                    if (!actionsStarted) { group.add(Separator.create()); actionsStarted = true }
                    val label = when (entry.id) {
                        "new" -> "New Session"
                        "resume" -> "Resume a Stored Conversation…"
                        "fork" -> "Fork Conversation"
                        "rename" -> "Rename Session…"
                        else -> entry.label
                    }
                    group.add(menuAction(label, enabled = entry.enabled) {
                        when (entry.id) {
                            "new" -> newSession()
                            "resume" -> resumeMenu()
                            "fork" -> host.act("forkConversation")
                            "rename" -> rename()
                            "refresh" -> host.act("refreshSessions")
                            "detach" -> host.act("detach")
                        }
                    })
                }
            }
        }
        group.add(menuAction("Restart Bridge") { host.act("restartBridge") })
        JBPopupFactory.getInstance()
            .createActionGroupPopup(null, group, SimpleDataContext.getProjectContext(project), JBPopupFactory.ActionSelectionAid.SPEEDSEARCH, true)
            .showUnderneathOf(sessionLink)
    }

    /**
     * Create an action for a popup.
     *
     * @param text The entry's text.
     * @param enabled False for an entry that is shown but cannot be chosen.
     * @param icon The entry's icon; `null` for none.
     * @param description The entry's description (shown in the status bar); `null` for none.
     * @param run What choosing the entry does, on the EDT.
     * @return The action.
     */
    private fun menuAction(text: String, enabled: Boolean = true, icon: javax.swing.Icon? = null, description: String? = null, run: () -> Unit): AnAction =
        object : DumbAwareAction(text, description, icon) {
            init {
                templatePresentation.setText(text, false)
            }

            override fun actionPerformed(e: AnActionEvent) = run()
            override fun update(e: AnActionEvent) {
                e.presentation.isEnabled = enabled
            }

            override fun getActionUpdateThread() = com.intellij.openapi.actionSystem.ActionUpdateThread.BGT
        }

    /** Load the stored conversations (`ide.sessions`) and offer them in a popup; choosing one resumes it. */
    private fun resumeMenu() {
        host.launch {
            val answer = host.call("ide.sessions")
            val picks = if (answer.ok) buildSessionPicks(parseObjectOrNull(answer.resultJson) ?: JsonObject(emptyMap()), state.sessions.attachedId) else emptyList()
            val stored = picks.filterIsInstance<SessionPick.Entry>().filter { it.choice is SessionChoice.Stored }
            withContext(Dispatchers.EDT) {
                if (stored.isEmpty()) {
                    Messages.showInfoMessage(project, if (answer.ok) "No stored conversation of this project was found." else answer.message, "calu: Resume")
                    return@withContext
                }
                JBPopupFactory.getInstance().createPopupChooserBuilder(stored)
                    .setTitle("Resume a Stored Conversation")
                    .setRenderer(textRenderer<SessionPick.Entry> { "${neutraliseBidi(it.label)}  —  ${neutraliseBidi(it.description)}" })
                    .setNamerForFiltering { it.label + " " + it.description }
                    .setItemChosenCallback { pick ->
                        focusComposerOnAttach = true
                        host.act("start", buildJsonObject { put("kind", JsonPrimitive("resume")); put("claude_session_id", JsonPrimitive((pick.choice as SessionChoice.Stored).claudeSessionId)) })
                    }
                    .createPopup().showUnderneathOf(sessionLink)
            }
        }
    }

    /**
     * Show a popup of choices below a link.
     *
     * @param anchor The link.
     * @param title The popup's title.
     * @param items The choices.
     * @param text The line of a choice.
     * @param chosen Called with the chosen item.
     */
    private fun <T> choose(anchor: JComponent, title: String, items: List<T>, text: (T) -> String, chosen: (T) -> Unit) {
        JBPopupFactory.getInstance().createPopupChooserBuilder(items)
            .setTitle(title)
            .setRenderer(textRenderer<T> { text(it) })
            .setItemChosenCallback { chosen(it) }
            .createPopup().showUnderneathOf(anchor)
    }

    /** Offer the models; "Other…" asks for a model name. */
    private fun modelMenu() {
        choose(model, "Model of the Session", CLAUDE_MODELS + "Other…", { if (it == "default") "default (Claude Code's own)" else it }) { m ->
            val name = if (m == "Other…") Messages.showInputDialog(project, "Model name or alias:", "calu: Model", null, state.session.model.orEmpty(), null)?.trim() else m
            if (!name.isNullOrEmpty()) host.act("setModel", args("model" to name))
        }
    }

    /** Offer the efforts. */
    private fun effortMenu() {
        choose(effort, "Effort (Sessions Started from Now On)", CLAUDE_EFFORTS, { it }) { host.act("setEffort", args("effort" to it)) }
    }

    /** Offer the supervision levels with their descriptions. */
    private fun levelMenu() {
        choose(level, "Supervision Level: What Waits for You", LEVELS, { "${it.name}  —  ${it.description}" }) { host.act("setLevel", args("level" to it.id)) }
    }

    /** Show the usage: context, cost and limits; a click on the line asks the session again. */
    private fun usageMenu() {
        val menu = JBPopupMenu()
        val meter = usageMeter(state.usage)
        menu.add(meter.title.substringBefore(" Click")).isEnabled = false
        usageCostLine(state.usage.costUsd, state.usage.numTurns).takeIf { it.isNotEmpty() }?.let { menu.add(it).isEnabled = false }
        for (limit in state.usage.rateLimits) menu.add(rateLimitLine(limit)).isEnabled = false
        menu.addSeparator()
        menu.add("Ask Again").addActionListener { host.act("refreshUsage") }
        menu.show(usage, 0, -menu.preferredSize.height)
    }

    /** Show the honesty menu: the mode switch, the open marks (a click clears one), review and trust. */
    private fun honestyMenu() {
        val menu = JBPopupMenu()
        val h = state.honesty
        for (mode in listOf("off", "track", "enforce")) {
            menu.add((if ((h?.mode ?: "off") == mode) "● " else "    ") + "Mode: $mode").addActionListener { host.act("setHonesty", args("mode" to mode)) }
        }
        if (h != null && h.marks.isNotEmpty()) {
            menu.addSeparator()
            for (m in h.marks.take(15)) {
                menu.add("Clear mark: ${neutraliseBidi(m.path)}${if (m.symbol.isNotEmpty()) " (${neutraliseBidi(m.symbol)})" else ""}").apply {
                    toolTipText = neutraliseBidi(m.reason)
                    addActionListener { host.act("honestyClear", buildJsonObject { put("mark_id", JsonPrimitive(m.id)) }) }
                }
            }
        }
        menu.addSeparator()
        menu.add("Review Now").addActionListener { host.act("honestyReview") }
        menu.add("Trust HEAD").addActionListener { host.act("honestyTrust") }
        menu.show(honesty, 0, honesty.height)
    }

    /**
     * Build a JSON object of string members.
     *
     * @param pairs Name and value of each member.
     * @return The object.
     */
    private fun args(vararg pairs: Pair<String, String>): JsonObject = JsonObject(pairs.associate { it.first to JsonPrimitive(it.second) })
}
