/**
 * The dialog of calu's own settings. The settings are in calu's database; the dialog is
 * generated from the answer of `ide.settings.get` and `ide.models.presets` by the pure
 * functions of `shared` (`buildSections`, `settingEdit`, `parseModels`, ...).
 *
 *     ┌ [search…]  [x] Modified only                      Scope: [Global ▾] ┐
 *     │ Models            │ Honesty                                         │
 *     │ Checkers          │   Mode ●                          Reset         │
 *     │ ───────────────── │   What the setting does …                       │
 *     │ General           │   [enforce ▾]                 Default: off      │
 *     │ Claude  ●2        │   ...                                           │
 *     │ Honesty ●1        │                                                 │
 *     └ what was saved, or calu's refusal ──────────────────────── [Close] ─┘
 *
 * A change is stored at once (a switch or a choice when it is made, a text when Enter is
 * pressed or the field loses the focus, a long text with its "Save" button); calu validates
 * and its refusal is shown under the control. A marker shows what differs from the default,
 * "Reset" removes the stored value. The page "Models" has the presets and "one model for
 * everything"; the page "Checkers" opens a checker's instructions in an editor tab.
 */
package io.github.kadeng.calu.frontend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.application.asContextElement
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.DialogWrapper
import com.intellij.ui.CollectionListModel
import com.intellij.ui.DocumentAdapter
import com.intellij.ui.SearchTextField
import com.intellij.ui.components.ActionLink
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.components.JBTextField
import com.intellij.ui.components.panels.VerticalLayout
import com.intellij.util.ui.JBFont
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.BridgeRequest
import io.github.kadeng.calu.logic.CheckerRow
import io.github.kadeng.calu.logic.ControlKind
import io.github.kadeng.calu.logic.ModelsInfo
import io.github.kadeng.calu.logic.RolesState
import io.github.kadeng.calu.logic.SettingEdit
import io.github.kadeng.calu.logic.SettingRow
import io.github.kadeng.calu.logic.SettingsRowView
import io.github.kadeng.calu.logic.SettingsSection
import io.github.kadeng.calu.logic.buildSections
import io.github.kadeng.calu.logic.effectiveSearch
import io.github.kadeng.calu.logic.formatControlValue
import io.github.kadeng.calu.logic.groupPresets
import io.github.kadeng.calu.logic.modelsPresetsRequest
import io.github.kadeng.calu.logic.modelsSetAllRequest
import io.github.kadeng.calu.logic.modelsSetRequest
import io.github.kadeng.calu.logic.parseCheckers
import io.github.kadeng.calu.logic.parseModels
import io.github.kadeng.calu.logic.parseSettingProjects
import io.github.kadeng.calu.logic.parseSettingRows
import io.github.kadeng.calu.logic.presetRequest
import io.github.kadeng.calu.logic.roleModelText
import io.github.kadeng.calu.logic.rolesState
import io.github.kadeng.calu.logic.selectedPresetId
import io.github.kadeng.calu.logic.settingEdit
import io.github.kadeng.calu.logic.settingsGetRequest
import io.github.kadeng.calu.logic.settingsUnsetRequest
import io.github.kadeng.calu.remote.CallResultDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.event.FocusAdapter
import java.awt.event.FocusEvent
import javax.swing.Action
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.ListSelectionModel
import javax.swing.event.DocumentEvent

/** Ids of the dialog's pages that are no settings section. */
object SettingsPages {
    /** The page with the model presets and the roles. */
    const val MODELS: String = "@models"

    /** The page with the checkers. */
    const val CHECKERS: String = "@checkers"

    /** The page with what `ide.status` found about the setup. */
    const val SETUP: String = "@setup"
}

/**
 * The dialog (see the file comment). Must be created and shown on the EDT.
 *
 * @param project The project.
 * @param host State and requests.
 * @param firstPage The page to show first; "" for the first page.
 */
class CaluSettingsDialog(private val project: Project, private val host: ChatHost, firstPage: String = "") : DialogWrapper(project) {
    /** One entry of the page list. */
    private data class Page(val id: String, val label: String, val modified: Int = 0)

    private val modality = ModalityState.any().asContextElement()
    private val search = SearchTextField(false)
    private val onlyModified = JBCheckBox("Modified only")
    private val scope = ComboBox(arrayOf("Global"))
    private val pagesModel = CollectionListModel<Page>()
    private val pages = JBList(pagesModel)
    private val content = WidthTrackingPanel(VerticalLayout(JBUI.scale(14))).apply { border = JBUI.Borders.empty(8, 14) }
    private val contentScroll = JBScrollPane(content).apply { border = JBUI.Borders.empty() }
    private val status = JBLabel(" ").apply { foreground = UiStyle.dim }
    private var rows: List<SettingRow> = emptyList()
    private var checkers: List<CheckerRow> = emptyList()
    private var models: ModelsInfo = ModelsInfo()
    private var modelsProblem: String = ""
    private var selected: String = firstPage.ifEmpty { SettingsPages.MODELS }
    private var filling = false
    private val errors = HashMap<String, String>()

    init {
        title = "calu Settings"
        isModal = false
        pages.selectionMode = ListSelectionModel.SINGLE_SELECTION
        pages.cellRenderer = textRenderer<Page> { it.label + if (it.modified > 0) "   ●${it.modified}" else "" }
        pages.addListSelectionListener {
            val page = pages.selectedValue
            if (!filling && page != null && page.id != selected) {
                selected = page.id
                drawContent()
            }
        }
        search.textEditor.emptyText.text = "Search settings"
        search.addDocumentListener(object : DocumentAdapter() {
            override fun textChanged(e: DocumentEvent) = redraw()
        })
        onlyModified.addActionListener { redraw() }
        scope.addActionListener { if (!filling) reload("") }
        init()
        reload("")
    }

    override fun createActions(): Array<Action> = arrayOf(cancelAction.apply { putValue(Action.NAME, "Close") })

    override fun getDimensionServiceKey(): String = "calu.settings"

    override fun createCenterPanel(): JComponent = JPanel(BorderLayout(0, JBUI.scale(6))).apply {
        preferredSize = Dimension(JBUI.scale(900), JBUI.scale(600))
        val top = JPanel(BorderLayout(JBUI.scale(10), 0))
        top.add(search, BorderLayout.CENTER)
        top.add(JPanel(java.awt.FlowLayout(java.awt.FlowLayout.RIGHT, JBUI.scale(8), 0)).apply {
            add(onlyModified)
            add(JBLabel("Scope:"))
            add(scope.apply { toolTipText = "Global settings hold for every project; a project's own value wins over the global one." })
        }, BorderLayout.EAST)
        add(top, BorderLayout.NORTH)
        add(JBScrollPane(pages).apply { preferredSize = Dimension(JBUI.scale(190), 0) }, BorderLayout.WEST)
        add(contentScroll, BorderLayout.CENTER)
        add(status, BorderLayout.SOUTH)
    }

    /**
     * Return the scope's project name.
     *
     * @return `null` for the global scope, else the project's name.
     */
    private fun project(): String? = (scope.selectedItem as? String)?.takeIf { scope.selectedIndex > 0 }

    /**
     * Send a bridge request built by `shared`.
     *
     * @param request The request.
     * @return The result or the error.
     */
    private suspend fun send(request: BridgeRequest): CallResultDto = host.call(request.method, request.params)

    /**
     * Load the settings, the checkers and the models of the scope, then draw the dialog again.
     *
     * @param message What to show in the status line afterwards; "" keeps a load error only.
     */
    private fun reload(message: String) {
        val scopeProject = project()
        host.launch {
            val settings = send(settingsGetRequest(scopeProject))
            val presets = send(modelsPresetsRequest(scopeProject))
            val result = parseObjectOrNull(settings.resultJson)
            withContext(Dispatchers.EDT + modality) {
                if (isDisposed) return@withContext
                if (settings.ok) {
                    rows = parseSettingRows(result)
                    checkers = parseCheckers(result)
                    fillScopes(parseSettingProjects(result))
                }
                models = if (presets.ok) parseModels(parseObjectOrNull(presets.resultJson)) else ModelsInfo()
                modelsProblem = if (presets.ok) "" else presets.message
                setStatus(if (!settings.ok) settings.message else message, if (settings.ok) "" else "error")
                redraw()
            }
        }
    }

    /**
     * Fill the scope selector with "Global" and the projects calu knows, keeping the selection.
     *
     * @param projects The project names.
     */
    private fun fillScopes(projects: List<String>) {
        val wanted = listOf("Global") + projects
        if ((0 until scope.itemCount).map { scope.getItemAt(it) } == wanted) return
        filling = true
        try {
            val before = scope.selectedItem
            scope.removeAllItems()
            for (name in wanted) scope.addItem(name)
            scope.selectedItem = if (before in wanted) before else "Global"
        } finally {
            filling = false
        }
    }

    /**
     * Show a line below the pages.
     *
     * @param text The line; "" empties it.
     * @param kind "error", "ok" or "" for the colour.
     */
    private fun setStatus(text: String, kind: String = "") {
        status.text = text.ifEmpty { " " }
        status.foreground = if (kind.isEmpty()) UiStyle.dim else UiStyle.chipColor(kind)
    }

    /**
     * Return the sections that match the search field and "Modified only".
     *
     * @return The sections in display order.
     */
    private fun sections(): List<SettingsSection> = buildSections(rows, effectiveSearch(search.text, onlyModified.isSelected))

    /** Fill the page list and draw the selected page. */
    private fun redraw() {
        val found = sections()
        val searching = search.text.isNotBlank() || onlyModified.isSelected
        val list = (if (searching) emptyList() else listOf(Page(SettingsPages.MODELS, "Models"), Page(SettingsPages.CHECKERS, "Checkers"), Page(SettingsPages.SETUP, "Setup"))) +
            found.map { Page(it.id, it.label, it.modifiedCount) }
        filling = true
        try {
            pagesModel.replaceAll(list)
            if (list.none { it.id == selected }) selected = list.firstOrNull()?.id.orEmpty()
            pages.selectedIndex = list.indexOfFirst { it.id == selected }
        } finally {
            filling = false
        }
        drawContent()
    }

    /** Draw the selected page. While a search is active every matching section is shown. */
    private fun drawContent() {
        val position = contentScroll.verticalScrollBar.value
        content.removeAll()
        val found = sections()
        val searching = search.text.isNotBlank() || onlyModified.isSelected
        when {
            selected == SettingsPages.MODELS && !searching -> drawModels()
            selected == SettingsPages.CHECKERS && !searching -> drawCheckers()
            selected == SettingsPages.SETUP && !searching -> drawSetup()
            else -> {
                val shown = if (searching) found else found.filter { it.id == selected }
                if (shown.isEmpty()) content.add(JBLabel(if (rows.isEmpty()) "Loading…" else "No setting matches.").apply { foreground = UiStyle.dim })
                for (section in shown) {
                    content.add(heading(section.label, section.description))
                    for (row in section.rows) content.add(settingRow(row))
                }
            }
        }
        content.revalidate()
        content.repaint()
        javax.swing.SwingUtilities.invokeLater { contentScroll.verticalScrollBar.value = position }
    }

    /**
     * Build a page heading.
     *
     * @param title The heading.
     * @param description A sentence below it; "" for none.
     * @return The component.
     */
    private fun heading(title: String, description: String): JComponent = JPanel(VerticalLayout(JBUI.scale(2))).apply {
        add(JBLabel(title).apply { font = JBFont.h3().asBold() })
        if (description.isNotEmpty()) add(textArea(description).apply { foreground = UiStyle.dim })
    }

    /**
     * Build the block of one setting: label with the "modified" marker and "Reset", the
     * description, the control of its kind, the default, and calu's refusal if there was one.
     *
     * @param row The setting.
     * @return The component.
     */
    private fun settingRow(row: SettingsRowView): JComponent {
        val block = JPanel(VerticalLayout(JBUI.scale(3)))
        val head = JPanel(BorderLayout(JBUI.scale(8), 0))
        val left = JPanel(java.awt.FlowLayout(java.awt.FlowLayout.LEFT, JBUI.scale(6), 0))
        left.add(JBLabel(row.label).apply { font = JBFont.label().asBold(); toolTipText = "${row.key} (${row.type})" })
        if (row.modified) left.add(Chip("MODIFIED", "info").apply { toolTipText = "A value is stored in this scope" })
        left.add(JBLabel(row.key).apply { foreground = UiStyle.dim; font = JBFont.small() })
        head.add(left, BorderLayout.WEST)
        if (row.modified) {
            head.add(ActionLink("Reset") { settingsUnsetRequest(row.key, project())?.let { store(row.key, it, "${row.label} is back at its ${if (row.inherits == "global") "global value" else "default"}.") } }, BorderLayout.EAST)
        }
        block.add(head)
        if (row.description.isNotEmpty()) block.add(textArea(row.description).apply { foreground = UiStyle.dim })
        val text = formatControlValue(row.control, row.value)
        val commit = { input: String ->
            if (input != text) {
                when (val edit = settingEdit(row, input, project())) {
                    is SettingEdit.Send -> store(row.key, edit.request, "${row.label} is stored.")
                    is SettingEdit.Refused -> { errors[row.key] = edit.message; drawContent() }
                }
            }
        }
        val control: JComponent = when (row.control) {
            ControlKind.TOGGLE -> JBCheckBox("On", text == "true").apply { addActionListener { commit(if (isSelected) "true" else "false") } }
            ControlKind.SELECT -> ComboBox(row.choices.map { it.ifEmpty { "(none)" } }.toTypedArray()).apply {
                selectedIndex = maxOf(0, row.choices.indexOf(text))
                addActionListener { commit(row.choices[selectedIndex]) }
            }
            ControlKind.MARKDOWN, ControlKind.LIST, ControlKind.JSON -> {
                val area = JBTextArea(text, minOf(10, maxOf(3, text.lines().size)), 40).apply { font = UiStyle.mono; lineWrap = row.control == ControlKind.MARKDOWN; wrapStyleWord = true }
                JPanel(BorderLayout(0, JBUI.scale(4))).apply {
                    add(JBScrollPane(area), BorderLayout.CENTER)
                    val hint = when (row.control) { ControlKind.LIST -> "One entry per line."; ControlKind.JSON -> "A JSON value."; else -> "" }
                    add(JPanel(java.awt.FlowLayout(java.awt.FlowLayout.LEFT, JBUI.scale(8), 0)).apply {
                        add(JButton("Save").apply { addActionListener { commit(area.text) } })
                        if (hint.isNotEmpty()) add(JBLabel(hint).apply { foreground = UiStyle.dim })
                    }, BorderLayout.SOUTH)
                }
            }
            else -> JBTextField(text, if (row.control == ControlKind.NUMBER) 12 else 36).apply {
                if (row.control == ControlKind.MODEL) emptyText.text = "provider:model, e.g. anthropic:claude-haiku-5-5"
                addActionListener { commit(this.text) }
                addFocusListener(object : FocusAdapter() {
                    override fun focusLost(e: FocusEvent) = commit(this@apply.text)
                })
            }
        }
        val line = JPanel(BorderLayout(JBUI.scale(10), 0))
        line.add(if (row.control in setOf(ControlKind.MARKDOWN, ControlKind.LIST, ControlKind.JSON)) control else JPanel(java.awt.FlowLayout(java.awt.FlowLayout.LEFT, 0, 0)).apply { add(control) }, BorderLayout.CENTER)
        block.add(line)
        val from = if (row.modified) "Default: ${row.defaultText.ifEmpty { "(empty)" }}" else if (row.inherits == "global") "From the global settings" else ""
        if (from.isNotEmpty()) block.add(JBLabel(from.take(200)).apply { foreground = UiStyle.dim; font = JBFont.small() })
        errors[row.key]?.let { block.add(textArea(it).apply { foreground = UiStyle.chipColor("error") }) }
        return block
    }

    /**
     * Send a change, show calu's answer, and load everything again.
     *
     * @param key The setting's key (for the place of an error); "" for a request that belongs to no row.
     * @param request The request.
     * @param done The status line after success.
     */
    private fun store(key: String, request: BridgeRequest, done: String) {
        setStatus("Saving…")
        host.launch {
            val answer = send(request)
            withContext(Dispatchers.EDT + modality) {
                if (isDisposed) return@withContext
                if (answer.ok) errors.remove(key) else errors[key] = answer.message
                if (!answer.ok) {
                    setStatus(answer.message, "error")
                    drawContent()
                }
            }
            if (answer.ok) reload(done)
        }
    }

    /** Draw the page "Models": the roles, "one model for everything" with the presets, and a custom model. */
    private fun drawModels() {
        content.add(heading("Models", "Which model each of calu's agents uses. A preset sets every role at once."))
        if (modelsProblem.isNotEmpty()) content.add(textArea(modelsProblem).apply { foreground = UiStyle.chipColor("error") })
        val state = rolesState(models.roles)
        val summary = when (state) {
            is RolesState.Uniform -> "Every role uses ${roleModelText(state.model, models.presets, models.defaultModel)}."
            RolesState.Mixed -> "The roles use different models."
            RolesState.None -> "No role is known yet."
        }
        content.add(JBLabel(summary))
        val chosen = selectedPresetId(models.presets, models.roles)
        val card = RoundedPanel(VerticalLayout(JBUI.scale(6)), fill = UiStyle.cardBackground, line = UiStyle.outline)
        card.border = JBUI.Borders.empty(10, 12)
        card.add(JBLabel("One model for everything").apply { font = JBFont.label().asBold() })
        for (group in groupPresets(models.presets)) {
            card.add(JBLabel(group.group).apply { foreground = UiStyle.dim; font = JBFont.small() })
            val line = JPanel(WrapLayout(8, 6)).apply { isOpaque = false }
            for (preset in group.presets) {
                line.add(JButton(preset.label + if (preset.id == chosen) "  ✓" else "").apply {
                    toolTipText = listOf(preset.model, preset.note).filter { it.isNotEmpty() }.joinToString(" — ")
                    addActionListener { presetRequest(preset, null, project())?.let { store("", it, "Every role now uses ${preset.label}.") } }
                })
            }
            card.add(line)
        }
        val custom = JBTextField(30).apply { emptyText.text = "or a model string, e.g. anthropic:claude-haiku-5-5" }
        val useCustom = {
            val request = modelsSetAllRequest(custom.text, null, project())
            if (request == null) setStatus("Enter a model string.", "error") else store("", request, "Every role now uses ${custom.text.trim()}.")
        }
        custom.addActionListener { useCustom() }
        card.add(JPanel(WrapLayout(8, 4)).apply { isOpaque = false; add(custom); add(JButton("Use for All Roles").apply { addActionListener { useCustom() } }) })
        content.add(card)
        content.add(JBLabel("Roles").apply { font = JBFont.label().asBold() })
        for (role in models.roles) {
            val line = JPanel(BorderLayout(JBUI.scale(10), 0))
            line.add(JBLabel(role.label).apply { preferredSize = Dimension(JBUI.scale(200), preferredSize.height) }, BorderLayout.WEST)
            val field = JBTextField(role.model).apply { emptyText.text = "default: ${models.defaultModel}" }
            val setRole = {
                if (field.text.trim() != role.model) modelsSetRequest(role.key, field.text, project())?.let { store("", it, "${role.label} uses ${field.text.trim().ifEmpty { "the default" }}.") }
            }
            field.addActionListener { setRole() }
            field.addFocusListener(object : FocusAdapter() {
                override fun focusLost(e: FocusEvent) {
                    setRole()
                }
            })
            line.add(field, BorderLayout.CENTER)
            content.add(line)
        }
        val claude = listOfNotNull(
            models.claudeModel?.let { "Claude Code's own model: $it" }, models.claudeEffort?.let { "effort: $it" }, if (models.useBedrock) "through AWS Bedrock" else null,
        ).joinToString(" · ")
        if (claude.isNotEmpty()) content.add(JBLabel(claude).apply { foreground = UiStyle.dim })
    }

    /** Draw the page "Checkers": one line per checker with "Edit Instructions". */
    /** What `ide.status` found; `null` until the answer arrived. */
    private var findings: List<SetupFinding>? = null
    private var findingsProblem = ""

    /** One finding of `ide.status` (what `calu doctor` checks). */
    private data class SetupFinding(val name: String, val level: String, val message: String, val advice: String)

    /** Ask `ide.status` and draw the Setup page again when the answer is there. */
    private fun loadSetup() {
        host.launch {
            val answer = host.call("ide.status")
            val list = if (answer.ok) {
                (parseObjectOrNull(answer.resultJson)?.arr("findings") ?: emptyList()).mapNotNull { it.asObj() }.map {
                    SetupFinding(it.str("name").orEmpty(), it.str("level") ?: if (it.bool("ok")) "ok" else "warning", it.str("message").orEmpty(), it.str("advice").orEmpty())
                }
            } else {
                null
            }
            withContext(Dispatchers.EDT + modality) {
                findings = list ?: emptyList()
                findingsProblem = if (answer.ok) "" else answer.message
                if (selected == SettingsPages.SETUP) drawContent()
            }
        }
    }

    /**
     * Draw the page "Setup": what is missing, each with the button that fixes it (the chat's
     * setup hints), then every finding of `ide.status` with its advice.
     */
    private fun drawSetup() {
        content.add(heading("Setup", "What calu found on the machine it runs on (the checks of `calu doctor`)."))
        for (hint in host.state.value.hints) {
            val card = RoundedPanel(BorderLayout(JBUI.scale(10), 0), fill = UiStyle.cardBackground, line = UiStyle.outline, stripe = UiStyle.chipColor(hint.level))
            card.border = JBUI.Borders.empty(8, 12)
            card.add(textArea(hint.message), BorderLayout.CENTER)
            io.github.kadeng.calu.logic.hintAction(hint)?.let { a ->
                card.add(JPanel(java.awt.GridBagLayout()).apply {
                    isOpaque = false
                    add(JButton(a.label).apply {
                        addActionListener {
                            when (a.action) {
                                "openLink" -> a.href?.let { com.intellij.ide.BrowserUtil.browse(it) }
                                "openSettings" -> com.intellij.openapi.options.ShowSettingsUtil.getInstance().showSettingsDialog(project, "calu")
                                "startServer" -> host.act("openTerminal")
                                else -> host.act(a.action)
                            }
                        }
                    })
                }, BorderLayout.EAST)
            }
            content.add(card)
        }
        val list = findings
        if (list == null) {
            content.add(JBLabel("Checking…").apply { foreground = UiStyle.dim })
            loadSetup()
            return
        }
        if (findingsProblem.isNotEmpty()) content.add(textArea("The setup was not checked: $findingsProblem").apply { foreground = UiStyle.chipColor("error") })
        for (f in list) {
            val line = RoundedPanel(VerticalLayout(JBUI.scale(2)), fill = UiStyle.cardBackground, line = UiStyle.outline)
            line.border = JBUI.Borders.empty(8, 12)
            val head = JPanel(WrapLayout(8, 0)).apply { isOpaque = false }
            head.add(JBLabel(f.name).apply { font = JBFont.label().asBold() })
            head.add(Chip(f.level.uppercase(), when (f.level) { "ok" -> "ok"; "error" -> "error"; "warning" -> "warning"; else -> "" }))
            line.add(head)
            if (f.message.isNotEmpty()) line.add(textArea(f.message))
            if (f.advice.isNotEmpty()) line.add(textArea(f.advice).apply { foreground = UiStyle.dim })
            content.add(line)
        }
        content.add(JButton("Check Again").apply {
            addActionListener {
                findings = null
                host.act("refreshSessions")
                drawContent()
            }
        })
    }

    private fun drawCheckers() {
        content.add(heading("Checkers", "The agents that decide about each command and edit. Their instructions are Markdown texts in calu's database."))
        if (checkers.isEmpty()) content.add(JBLabel("calu's database lists no checker.").apply { foreground = UiStyle.dim })
        for (c in checkers) {
            val line = RoundedPanel(BorderLayout(JBUI.scale(10), 0), fill = UiStyle.cardBackground, line = UiStyle.outline)
            line.border = JBUI.Borders.empty(8, 12)
            val left = JPanel(VerticalLayout(JBUI.scale(2))).apply { isOpaque = false }
            val head = JPanel(WrapLayout(8, 0)).apply { isOpaque = false }
            head.add(JBLabel(c.name).apply { font = JBFont.label().asBold() })
            head.add(Chip(if (c.enabled) "ENABLED" else "DISABLED", if (c.enabled) "ok" else ""))
            left.add(head)
            left.add(JBLabel("route ${c.route} · ${c.model.ifEmpty { "default model" }} · version ${c.version}").apply { foreground = UiStyle.dim })
            line.add(left, BorderLayout.CENTER)
            line.add(JPanel(java.awt.GridBagLayout()).apply {
                isOpaque = false
                add(JButton("Edit Instructions").apply {
                    addActionListener { host.act("editChecker", buildJsonObject { put("name", JsonPrimitive(c.name)) }) }
                })
            }, BorderLayout.EAST)
            content.add(line)
        }
        content.add(JBLabel("A checker's model and limits are settings: search for \"checkers\".").apply { foreground = UiStyle.dim })
    }
}

/**
 * Build a JSON object of string members.
 *
 * @param pairs Name and value of each member.
 * @return The object.
 */
internal fun jsonOf(vararg pairs: Pair<String, String>): JsonObject = JsonObject(pairs.associate { it.first to JsonPrimitive(it.second) })
