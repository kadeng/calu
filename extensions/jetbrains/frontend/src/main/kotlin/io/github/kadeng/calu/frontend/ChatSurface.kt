/**
 * The chat of the tool window, drawn with Swing and JetBrains components.
 *
 *     ┌ header: [session ▾] state · level · honesty            ┐
 *     │ hints (what is missing, with its button), server command│
 *     ├ tabs: Main | subagent … ────────────────────────────────┤
 *     │ transcript of the tab (TranscriptView)                  │
 *     ├ queued messages ────────────────────────────────────────┤
 *     │ composer: Enter = next turn, Ctrl+Enter = into the turn │
 *     └ [Send] [Into turn] [Interrupt]  model effort level usage┘
 *
 * `ChatSurface` is the seam for a second way to draw the chat (an embedded browser that reuses
 * the VSCode webviews, for local mode, after a spike); the native surface is the one that also
 * works in remote development. Everything here runs on the EDT; requests go through `ChatHost`.
 */
package io.github.kadeng.calu.frontend

import com.intellij.openapi.Disposable
import com.intellij.openapi.application.EDT
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTabbedPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.components.JBTextField
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.chat.RowAction
import io.github.kadeng.calu.chat.rowsOf
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.SessionChoice
import io.github.kadeng.calu.logic.SessionPick
import io.github.kadeng.calu.logic.buildSessionPicks
import io.github.kadeng.calu.logic.derivedSessionName
import io.github.kadeng.calu.logic.validateSessionName
import io.github.kadeng.calu.state.BridgeStatus
import io.github.kadeng.calu.state.LEVELS
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.SetupHint
import io.github.kadeng.calu.state.ViewState
import io.github.kadeng.calu.state.honestyChip
import io.github.kadeng.calu.state.honestyStoredNote
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.levelLabel
import io.github.kadeng.calu.state.levelOf
import io.github.kadeng.calu.state.messageTarget
import io.github.kadeng.calu.state.openEscalationCount
import io.github.kadeng.calu.state.sessionTitle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.awt.BorderLayout
import java.awt.FlowLayout
import java.awt.datatransfer.StringSelection
import java.awt.event.InputEvent
import java.awt.event.KeyAdapter
import java.awt.event.KeyEvent
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.JPopupMenu

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

/** Models offered in the composer's selector ("default" = Claude Code's own default). */
val MODEL_CHOICES: List<String> = listOf("default", "fable", "opus", "sonnet", "haiku")

/** Efforts offered in the composer's selector ("default" removes the stored value). */
val EFFORT_CHOICES: List<String> = listOf("default", "low", "medium", "high", "xhigh", "max")

/**
 * The native Swing surface.
 *
 * @param project The project (parent of dialogs and popups).
 * @param host State and actions.
 */
class NativeChatSurface(private val project: Project, private val host: ChatHost) : ChatSurface, CardActions {
    private var state: ViewState = host.state.value
    private var updating = false
    private var handledFocus = 0
    private val drafts = HashMap<String?, String>()
    private var draftSession: String? = null

    private val sessionButton = JButton("No session").apply { toolTipText = "Switch, start, resume, fork or rename a session" }
    private val status = JBLabel()
    private val honesty = JButton().apply { isVisible = false; toolTipText = "Code honesty: click to change the mode" }
    private val hints = JPanel(VerticalLayout(JBUI.scale(4))).apply { border = JBUI.Borders.empty(0, 8, 4, 8) }
    private val tabs = JBTabbedPane()
    private val transcripts = LinkedHashMap<String, TranscriptView>()
    private val queue = JBLabel().apply { foreground = UiStyle.dim; border = JBUI.Borders.empty(2, 8) }
    private val input = JBTextArea(3, 20).apply { lineWrap = true; wrapStyleWord = true; border = JBUI.Borders.empty(4) }
    private val send = JButton("Send").apply { toolTipText = "Send for the next turn (Enter)" }
    private val inject = JButton("Into turn").apply { toolTipText = "Send into the running turn (Ctrl+Enter)" }
    private val interrupt = JButton("Interrupt").apply { toolTipText = "Interrupt Claude's running turn" }
    private val model = ComboBox(MODEL_CHOICES.toTypedArray()).apply { isEditable = true; toolTipText = "Model of the session" }
    private val effort = ComboBox(EFFORT_CHOICES.toTypedArray()).apply { toolTipText = "Effort of sessions started from now on" }
    private val level = ComboBox((LEVELS.map { it.name } + "Custom").toTypedArray()).apply { toolTipText = "Supervision level: what waits for you" }
    private val usage = JBLabel().apply { foreground = UiStyle.dim }
    private val root = JPanel(BorderLayout())

    override val component: JComponent get() = root

    /** The text area of the composer (for tests). */
    val inputArea: JBTextArea get() = input

    /** The transcript of a tab, `null` if the tab does not exist (for tests). */
    fun transcript(tab: String): TranscriptView? = transcripts[tab]

    /** The keys of the tabs, in order (for tests). */
    val tabKeys: List<String> get() = transcripts.keys.toList()

    /** The texts of the hints shown now (for tests). */
    var hintTexts: List<String> = emptyList()
        private set

    init {
        val header = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), JBUI.scale(4))).apply { add(sessionButton); add(status); add(honesty) }
        root.add(JPanel(VerticalLayout(0)).apply { add(header); add(hints) }, BorderLayout.NORTH)
        root.add(tabs, BorderLayout.CENTER)
        val footer = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), JBUI.scale(2)))
        for (c in listOf(send, inject, interrupt, model, effort, level, usage)) footer.add(c)
        val south = JPanel(BorderLayout())
        south.add(queue, BorderLayout.NORTH)
        south.add(JBScrollPane(input), BorderLayout.CENTER)
        south.add(footer, BorderLayout.SOUTH)
        root.add(south, BorderLayout.SOUTH)

        sessionButton.addActionListener { sessionMenu() }
        honesty.addActionListener { honestyMenu() }
        send.addActionListener { submit(false) }
        inject.addActionListener { submit(true) }
        interrupt.addActionListener { host.act("interrupt") }
        input.addKeyListener(object : KeyAdapter() {
            override fun keyPressed(e: KeyEvent) {
                if (e.keyCode != KeyEvent.VK_ENTER || e.isShiftDown) return
                e.consume()
                submit(e.modifiersEx and (InputEvent.CTRL_DOWN_MASK or InputEvent.META_DOWN_MASK) != 0)
            }
        })
        model.addActionListener { if (!updating) host.act("setModel", args("model" to (model.editor.item?.toString() ?: "default"))) }
        effort.addActionListener { if (!updating) host.act("setEffort", args("effort" to effort.selectedItem.toString())) }
        level.addActionListener {
            val i = level.selectedIndex
            if (!updating && i in LEVELS.indices) host.act("setLevel", args("level" to LEVELS[i].id))
        }
        tabs.addChangeListener { if (!updating) renderComposer() }
        render(state)
    }

    override fun dispose() {}

    override fun render(state: ViewState) {
        val previous = this.state
        this.state = state
        updating = true
        try {
            renderHeader()
            renderHints()
            renderTabs()
            if (previous.session.sessionId != state.session.sessionId) switchDraft()
            renderComposer()
        } finally {
            updating = false
        }
    }

    /** Keep the composer's text per session: store the old session's draft, show the new one's. */
    private fun switchDraft() {
        drafts[draftSession] = input.text
        draftSession = state.session.sessionId
        input.text = drafts[draftSession].orEmpty()
    }

    /** Draw the session button, the connection state and the honesty chip. */
    private fun renderHeader() {
        val s = state
        val attached = s.sessions.list.find { it.id == s.sessions.attachedId }
        sessionButton.text = when {
            attached != null -> sessionTitle(attached) + " ▾"
            s.sessions.attachedId != null -> s.sessions.attachedId!!.take(8) + " ▾"
            else -> "No session ▾"
        }
        val open = openEscalationCount(s)
        status.text = when {
            s.bridge.status == BridgeStatus.STARTING -> s.bridge.message ?: "starting calu…"
            s.bridge.status != BridgeStatus.RUNNING -> s.bridge.message ?: "calu is ${s.bridge.status}"
            s.starting != null -> s.starting!!.title + if (s.starting!!.slow) " (still waiting: is a calu server running?)" else "…"
            s.link.status == LinkStatus.RECONNECTING -> "reconnecting to the session…"
            s.link.status == LinkStatus.LOST -> "the session's connection is lost"
            !isAttached(s) -> "not attached"
            else -> listOfNotNull(s.session.state, levelLabel(s.autoEscalate), if (open > 0) "$open waiting for you" else null).joinToString(" · ")
        }
        status.foreground = if (s.bridge.status in setOf(BridgeStatus.ERROR, BridgeStatus.MISSING, BridgeStatus.EXITED) || s.link.status == LinkStatus.LOST) UiStyle.chipColor("error") else UiStyle.dim
        val chip = honestyChip(s.honesty)
        val stored = honestyStoredNote(s.honesty, s.honestyStored)
        honesty.isVisible = isAttached(s)
        honesty.text = listOf(chip.label.ifEmpty { "Honesty: off" }, stored).filter { it.isNotEmpty() }.joinToString(" · ")
        honesty.foreground = UiStyle.chipColor(when (chip.kind) { "clean" -> "ok"; "open" -> "warning"; else -> "" })
    }

    /** Draw the setup hints and, while no session is attached, the server command. */
    private fun renderHints() {
        val shown = state.hints.map { it.message } + listOfNotNull(state.serverHelp?.command)
        if (shown == hintTexts && hints.componentCount > 0 == shown.isNotEmpty()) return
        hintTexts = shown
        hints.removeAll()
        for (h in state.hints) hints.add(hintRow(h))
        state.serverHelp?.let { help ->
            hints.add(textArea(help.note + if (help.detach.isNotEmpty()) " Detach with ${help.detach}." else "").apply { foreground = UiStyle.dim })
            hints.add(JBTextField(help.command).apply { isEditable = false; font = JBFont.create(java.awt.Font(java.awt.Font.MONOSPACED, java.awt.Font.PLAIN, JBFont.label().size)) })
            hints.add(JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), 0)).apply {
                add(JButton("Open terminal with this command").apply { addActionListener { host.act("openTerminal") } })
                add(JButton("Copy command").apply { addActionListener { CopyPasteManager.getInstance().setContents(StringSelection(help.command)) } })
            })
        }
        hints.revalidate()
        hints.repaint()
    }

    /**
     * Build the line of one hint: its message and the button that fixes it.
     *
     * @param h The hint.
     * @return The component.
     */
    private fun hintRow(h: SetupHint): JComponent {
        val row = JPanel(BorderLayout(JBUI.scale(6), 0))
        row.add(textArea(h.message).apply { foreground = UiStyle.chipColor(h.level) }, BorderLayout.CENTER)
        if (h.action.isNotEmpty()) {
            row.add(JButton(h.actionLabel).apply {
                addActionListener {
                    when (h.action) {
                        "openLink" -> h.href?.let { com.intellij.ide.BrowserUtil.browse(it) }
                        "openSettings", "install" -> com.intellij.openapi.options.ShowSettingsUtil.getInstance().showSettingsDialog(project, "calu")
                        "startServer" -> host.act("openTerminal")
                        "newSession" -> newSession()
                        "attach" -> host.act("attach")
                        else -> host.act(h.action)
                    }
                }
            }, BorderLayout.EAST)
        }
        return row
    }

    /** Draw the tabs (Main and one per subagent) and their transcripts; follow a focus request. */
    private fun renderTabs() {
        val wanted = listOf(MAIN) + state.agents.map { it.agentId }.filter { it != MAIN }
        if (wanted != transcripts.keys.toList()) {
            val selected = selectedTab()
            transcripts.keys.retainAll(wanted.toSet())
            tabs.removeAll()
            for (key in wanted) {
                val view = transcripts.getOrPut(key) { TranscriptView(::handle) { EscalationCard(this) { isAttached(state) } } }
                tabs.addTab(key, view.component)
            }
            // LinkedHashMap keeps the insertion order of the first creation; rebuild it in tab order.
            val ordered = wanted.associateWith { transcripts[it]!! }
            transcripts.clear()
            transcripts.putAll(ordered)
            tabs.selectedIndex = maxOf(0, wanted.indexOf(selected))
        }
        wanted.forEachIndexed { i, key ->
            val agent = state.agents.find { it.agentId == key }
            val waiting = state.escalations.values.count { it.agentKey == key && it.status == "open" }
            val title = if (key == MAIN) "Main" else (agent?.description?.takeIf { it.isNotEmpty() } ?: key.take(8)).take(24) + (agent?.status?.takeIf { it != "running" }?.let { " ($it)" } ?: "")
            tabs.setTitleAt(i, title + if (waiting > 0) " ●" else "")
            transcripts[key]!!.update(rowsOf(state, key))
        }
        val focus = state.focus
        if (focus != null && focus.seq > handledFocus) {
            handledFocus = focus.seq
            wanted.indexOf(focus.tab).takeIf { it >= 0 }?.let { tabs.selectedIndex = it }
        }
    }

    /**
     * Return the key of the visible tab.
     *
     * @return "main" or a subagent's id.
     */
    private fun selectedTab(): String = transcripts.keys.elementAtOrNull(tabs.selectedIndex) ?: MAIN

    /** Draw the composer: who receives the message, the buttons, the selectors and the usage. */
    private fun renderComposer() {
        val s = state
        val attached = isAttached(s)
        val target = messageTarget(s, selectedTab())
        val can = attached && target.to != null
        input.isEnabled = can
        input.emptyText.text = if (!attached) "Attach to a session to write to Claude" else target.reason ?: "Message to ${target.label}: Enter sends, Ctrl+Enter sends into the running turn, Shift+Enter starts a new line"
        send.isEnabled = can
        val working = s.session.state !in setOf("idle", "stopped", "failed")
        inject.isEnabled = can && working
        interrupt.isEnabled = attached && working
        queue.isVisible = s.queue.isNotEmpty()
        queue.text = "Waiting to be read: " + s.queue.joinToString(" | ") { (if (it.delivery == "inject") "(into turn) " else "") + it.text.lineSequence().first().take(60) }
        model.isEnabled = attached
        level.isEnabled = attached
        model.editor.item = s.session.model ?: "default"
        effort.selectedItem = s.session.effort?.takeIf { it in EFFORT_CHOICES } ?: "default"
        val lv = levelOf(s.autoEscalate)
        level.selectedIndex = LEVELS.indexOfFirst { it.id == lv }.takeIf { it >= 0 } ?: LEVELS.size
        val c = s.usage.context
        usage.text = if (c == null) "" else "context ${"%.0f".format(c.percent)}% (${c.tokens / 1000}k of ${c.maxTokens / 1000}k)"
        usage.toolTipText = (listOfNotNull(s.usage.costUsd?.let { "cost \$%.2f".format(it) }, "${s.usage.numTurns} turns") +
            s.usage.rateLimits.map { "${it.type}: ${it.status}" + (it.utilization?.let { u -> " ${(u * 100).toInt()}%" } ?: "") }).joinToString(" · ")
    }

    /**
     * Send the composer's text and clear it.
     *
     * @param intoTurn True to deliver into the running turn, false for the next turn.
     */
    private fun submit(intoTurn: Boolean) {
        val text = input.text
        if (text.isBlank() || !send.isEnabled) return
        host.act("send", buildJsonObject {
            put("text", JsonPrimitive(text)); put("inject", JsonPrimitive(intoTurn && inject.isEnabled)); put("tab", JsonPrimitive(selectedTab()))
        })
        input.text = ""
    }

    /**
     * Run the action of a clicked row.
     *
     * @param action The action; "openTab" switches the tab here, "editCheckerRoute" opens the
     *   checker of a route, everything else goes to the backend.
     */
    private fun handle(action: RowAction) {
        when (action.name) {
            "openTab" -> transcripts.keys.indexOf(action.args.str("tab")).takeIf { it >= 0 }?.let { tabs.selectedIndex = it }
            else -> host.act(action.name, action.args)
        }
    }

    override fun decide(key: String, allow: Boolean, note: String) =
        host.act("decide", buildJsonObject { put("key", JsonPrimitive(key)); put("allow", JsonPrimitive(allow)); if (note.isNotEmpty()) put("note", JsonPrimitive(note)) })

    override fun answer(key: String, answer: String) =
        host.act("decide", buildJsonObject { put("key", JsonPrimitive(key)); put("allow", JsonPrimitive(false)); put("answer", JsonPrimitive(answer)) })

    override fun why(escalationId: String) = host.act("showContext", args("escalation_id" to escalationId))

    /** Ask for an optional first message and start a new session. */
    fun newSession() {
        val prompt = Messages.showInputDialog(project, "First message for Claude (optional):", "calu: New Session", null) ?: return
        host.act("start", buildJsonObject { put("kind", JsonPrimitive("new")); if (prompt.isNotBlank()) put("prompt", JsonPrimitive(prompt)) })
    }

    /** Show the session menu: running sessions, new, resume, fork, rename, detach. */
    private fun sessionMenu() {
        val menu = JPopupMenu()
        val s = state
        for (e in s.sessions.list) {
            val marks = listOfNotNull(e.state, if (e.openEscalations > 0) "${e.openEscalations} waiting" else null, if (e.stale) "stale" else null, if (e.id == s.sessions.attachedId) "attached" else null)
            menu.add("${sessionTitle(e)}  (${marks.joinToString(", ")})").apply {
                toolTipText = "${e.directory} · ${e.id.take(8)}"
                addActionListener { host.act("attach", args("session" to e.id)) }
            }
        }
        if (s.sessions.list.isEmpty()) menu.add(s.sessions.error ?: if (s.sessions.loaded) "No session is running" else "Loading the sessions…").isEnabled = false
        menu.addSeparator()
        menu.add("New session…").addActionListener { newSession() }
        menu.add("Resume a stored conversation…").addActionListener { resumeMenu() }
        val conversation = s.session.claudeSessionId
        val attached = s.sessions.list.find { it.id == s.sessions.attachedId }
        menu.add("Fork conversation").apply {
            isEnabled = isAttached(s) && conversation != null
            addActionListener {
                host.act("start", buildJsonObject {
                    put("kind", JsonPrimitive("fork")); put("claude_session_id", JsonPrimitive(conversation.orEmpty()))
                    put("name", JsonPrimitive(derivedSessionName("fork", attached?.let { sessionTitle(it) }.orEmpty())))
                })
            }
        }
        menu.add("Rename session…").apply {
            isEnabled = attached != null
            addActionListener {
                val name = Messages.showInputDialog(project, "Name of the session (empty removes it):", "calu: Rename Session", null, attached?.name.orEmpty(), object : com.intellij.openapi.ui.InputValidatorEx {
                    override fun getErrorText(inputString: String): String? = validateSessionName(inputString)
                    override fun checkInput(inputString: String): Boolean = validateSessionName(inputString) == null
                    override fun canClose(inputString: String): Boolean = checkInput(inputString)
                }) ?: return@addActionListener
                host.act("rename", buildJsonObject { put("session_id", JsonPrimitive(attached!!.id)); put("name", JsonPrimitive(name)) })
            }
        }
        menu.add("Detach").apply { isEnabled = isAttached(s); addActionListener { host.act("detach") } }
        menu.addSeparator()
        menu.add("Refresh").addActionListener { host.act("refreshSessions") }
        menu.add("Restart bridge").addActionListener { host.act("restartBridge") }
        menu.show(sessionButton, 0, sessionButton.height)
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
                    .setRenderer(com.intellij.ui.SimpleListCellRenderer.create("") { "${it.label}  —  ${it.description}" })
                    .setItemChosenCallback { pick ->
                        host.act("start", buildJsonObject { put("kind", JsonPrimitive("resume")); put("claude_session_id", JsonPrimitive((pick.choice as SessionChoice.Stored).claudeSessionId)) })
                    }
                    .createPopup().showUnderneathOf(sessionButton)
            }
        }
    }

    /** Show the honesty menu: the mode switch and the open marks. */
    private fun honestyMenu() {
        val menu = JPopupMenu()
        val h = state.honesty
        for (mode in listOf("off", "track", "enforce")) {
            menu.add((if ((h?.mode ?: "off") == mode) "● " else "   ") + "Mode: $mode").addActionListener { host.act("setHonesty", args("mode" to mode)) }
        }
        if (h != null && h.marks.isNotEmpty()) {
            menu.addSeparator()
            for (m in h.marks.take(15)) menu.add("${m.path}${if (m.symbol.isNotEmpty()) " (${m.symbol})" else ""}: ${m.reason.take(80)}").isEnabled = false
        }
        menu.addSeparator()
        menu.add("Review now").addActionListener { host.launch { host.call("honesty.review", session = true) } }
        menu.add("Trust HEAD").addActionListener { host.launch { host.call("honesty.trust", buildJsonObject { put("rev", JsonPrimitive("HEAD")) }, session = true) } }
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
