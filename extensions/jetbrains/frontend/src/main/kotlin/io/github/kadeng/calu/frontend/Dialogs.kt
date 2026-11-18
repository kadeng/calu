/**
 * The checker picker and the entry to the dialog of calu's own settings (`SettingsDialog.kt`).
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
 * Open the dialog with all of calu's settings.
 *
 * @param project The project.
 * @param host State and requests.
 * @param page The page to show first: `SettingsPages.MODELS`, `SettingsPages.CHECKERS`, a
 *   section id, or "" for the first page.
 */
fun showCaluSettings(project: Project, host: ChatHost, page: String = "") {
    CaluSettingsDialog(project, host, page).show()
}
