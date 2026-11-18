/**
 * Dialogs over calu's own settings (stored in calu's database, read and written through the
 * bridge): the checker picker, "Models: use one model for everything", and the list of all
 * settings as a generated table.
 */
package io.github.kadeng.calu.frontend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.application.asContextElement
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.DialogWrapper
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextField
import com.intellij.ui.table.JBTable
import com.intellij.util.ui.JBUI
import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.awt.BorderLayout
import java.awt.Dimension
import javax.swing.Action
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.table.DefaultTableModel

/**
 * List the checkers (`ide.settings.get`) in a popup; choosing one opens its instructions in an
 * editor tab. A failure is shown in a message box.
 *
 * @param project The project.
 * @param host State and requests.
 */
fun chooseChecker(project: Project, host: ChatHost) {
    host.launch {
        val answer = host.call("ide.settings.get", buildJsonObject { put("project", JsonNull) })
        val checkers = parseObjectOrNull(answer.resultJson)?.arr("checkers")?.mapNotNull { it.asObj() }.orEmpty()
        withContext(Dispatchers.EDT) {
            if (!answer.ok || checkers.isEmpty()) {
                Messages.showInfoMessage(project, if (answer.ok) "calu's database lists no checker." else answer.message, "calu: Checker Instructions")
                return@withContext
            }
            JBPopupFactory.getInstance().createPopupChooserBuilder(checkers)
                .setTitle("Edit Checker Instructions")
                .setRenderer(textRenderer<JsonObject> { "${it.str("name")}  —  route ${it.str("route")}, ${it.str("model")?.ifEmpty { "default model" }}${if (it.bool("enabled")) "" else ", disabled"}" })
                .setItemChosenCallback { c -> host.act("editChecker", buildJsonObject { put("name", JsonPrimitive(c.str("name").orEmpty())) }) }
                .createPopup().showCenteredInCurrentWindow(project)
        }
    }
}

/**
 * "Models: use one model for everything": the presets of `ide.models.presets` and a custom
 * model string; OK sets every role with `ide.models.set_all`.
 *
 * @param project The project.
 * @param host State and requests.
 */
class ModelsDialog(private val project: Project, private val host: ChatHost) : DialogWrapper(project) {
    private val presets = JBList<JsonObject>()
    private val custom = JBTextField().apply { emptyText.text = "or a model string, e.g. anthropic:claude-haiku-5-5" }
    private val roles = JBLabel("Loading the presets…")

    init {
        title = "calu: Use One Model for Everything"
        setOKButtonText("Use for All Roles")
        presets.cellRenderer = textRenderer<JsonObject> { "${it.str("label")}  —  ${it.str("note").orEmpty()}" }
        init()
        val modality = ModalityState.any().asContextElement()
        host.launch {
            val answer = host.call("ide.models.presets")
            val result = parseObjectOrNull(answer.resultJson)
            withContext(Dispatchers.EDT + modality) {
                presets.setListData(result?.arr("presets")?.mapNotNull { it.asObj() }.orEmpty().toTypedArray())
                roles.text = if (!answer.ok) answer.message else "<html>" + (result?.arr("roles")?.mapNotNull { it.asObj() }.orEmpty()
                    .joinToString("<br>") { "${it.str("label")}: ${it.str("model")?.ifEmpty { "default" }}" }.ifEmpty { result?.str("problem") ?: "No roles." }) + "</html>"
            }
        }
    }

    override fun createCenterPanel(): JComponent = JPanel(BorderLayout(0, JBUI.scale(8))).apply {
        preferredSize = Dimension(JBUI.scale(640), JBUI.scale(420))
        add(roles, BorderLayout.NORTH)
        add(JBScrollPane(presets), BorderLayout.CENTER)
        add(custom, BorderLayout.SOUTH)
    }

    override fun doOKAction() {
        val model = custom.text.trim().ifEmpty { presets.selectedValue?.str("id").orEmpty() }
        if (model.isEmpty()) {
            setErrorText("Choose a preset or enter a model string.")
            return
        }
        host.launch {
            val answer = host.call("ide.models.set_all", buildJsonObject { put("model", JsonPrimitive(model)) })
            withContext(Dispatchers.EDT) {
                if (answer.ok) Messages.showInfoMessage(project, "Every role now uses $model.", "calu: Models")
                else Messages.showErrorDialog(project, answer.message, "calu: Models")
            }
        }
        super.doOKAction()
    }
}

/**
 * All of calu's settings as a table generated from `ide.settings.get` (global scope): key,
 * value, default, description. "Edit" changes the selected setting (`ide.settings.set`; the
 * text is read as JSON, and as a plain string if it is not JSON), "Reset" removes the stored
 * value (`ide.settings.unset`). calu validates; its message is shown.
 *
 * @param project The project.
 * @param host State and requests.
 */
class CaluSettingsDialog(private val project: Project, private val host: ChatHost) : DialogWrapper(project) {
    private val model = object : DefaultTableModel(arrayOf("Setting", "Value", "Set", "Default", "Description"), 0) {
        override fun isCellEditable(row: Int, column: Int): Boolean = false
    }
    private val table = JBTable(model)
    private val modality = ModalityState.any().asContextElement()

    init {
        title = "calu Settings (Global)"
        init()
        reload()
    }

    override fun createActions(): Array<Action> = arrayOf(okAction)

    override fun createCenterPanel(): JComponent = JPanel(BorderLayout(0, JBUI.scale(6))).apply {
        preferredSize = Dimension(JBUI.scale(900), JBUI.scale(520))
        add(JBScrollPane(table), BorderLayout.CENTER)
        add(JPanel().apply {
            add(JButton("Edit…").apply { addActionListener { edit() } })
            add(JButton("Reset to Default").apply { addActionListener { change(null) } })
            add(JButton("Reload").apply { addActionListener { reload() } })
        }, BorderLayout.SOUTH)
    }

    /** Load the settings and fill the table; rows of kind "section" are left out. */
    private fun reload() {
        host.launch {
            val answer = host.call("ide.settings.get", buildJsonObject { put("project", JsonNull) })
            val rows = parseObjectOrNull(answer.resultJson)?.arr("rows")?.mapNotNull { it.asObj() }.orEmpty().filter { it.str("kind") != "section" }
            withContext(Dispatchers.EDT + modality) {
                model.rowCount = 0
                for (r in rows) {
                    val value = r["value"]
                    model.addRow(arrayOf(r.str("key"), (value as? JsonPrimitive)?.takeIf { it.isString }?.content ?: value?.toString().orEmpty(), if (r.bool("is_set")) "yes" else "", r.str("default"), r.str("description")))
                }
                if (!answer.ok) setErrorText(answer.message)
            }
        }
    }

    /** Ask for the new value of the selected setting and store it. */
    private fun edit() {
        val row = table.selectedRow.takeIf { it >= 0 } ?: return
        val text = Messages.showInputDialog(project, "${model.getValueAt(row, 0)}\n${model.getValueAt(row, 4)}", "calu: Change Setting", null, model.getValueAt(row, 1)?.toString(), null) ?: return
        change(text)
    }

    /**
     * Store or remove the value of the selected setting, then reload the table.
     *
     * @param text The new value as typed; `null` removes the stored value.
     */
    private fun change(text: String?) {
        val row = table.selectedRow.takeIf { it >= 0 } ?: return
        val key = model.getValueAt(row, 0).toString()
        host.launch {
            val answer = if (text == null) {
                host.call("ide.settings.unset", buildJsonObject { put("key", JsonPrimitive(key)); put("project", JsonNull) })
            } else {
                val value: JsonElement = try {
                    CaluJson.parseToJsonElement(text)
                } catch (_: Exception) {
                    JsonPrimitive(text)
                }
                host.call("ide.settings.set", buildJsonObject { put("key", JsonPrimitive(key)); put("value", value); put("project", JsonNull) })
            }
            withContext(Dispatchers.EDT + modality) { setErrorText(if (answer.ok) null else answer.message) }
            if (answer.ok) reload()
        }
    }
}

/**
 * Open the dialog with all of calu's settings.
 *
 * @param project The project.
 * @param host State and requests.
 */
fun showCaluSettings(project: Project, host: ChatHost) {
    CaluSettingsDialog(project, host).show()
}
