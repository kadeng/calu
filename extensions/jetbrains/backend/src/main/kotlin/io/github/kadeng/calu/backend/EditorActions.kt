/**
 * The actions of the chat that open something in the IDE: the diff of an edit, a file at a line,
 * a checker's instructions, a context document, a terminal with the server command.
 *
 *     click on an edit row   ──► openDiff(tool_use_id)  ──► DiffManager (scrolled to the changed line)
 *     click on a file name   ──► openFile(path, line)   ──► OpenFileDescriptor
 *     "Checkers"             ──► editChecker(name)      ──► LightVirtualFile "<name>.md", saved with ide.checker.save
 *     "Context" / "Why?"     ──► showContext(id?)       ──► read-only LightVirtualFile
 *
 * They run in the backend, where the project's files are; in remote development the platform
 * shows the editors they open in the client. Paths that calu names are translated to the IDE's
 * machine at this edge (`Paths.kt`).
 */
package io.github.kadeng.calu.backend

import com.intellij.diff.DiffContentFactory
import com.intellij.diff.DiffManager
import com.intellij.diff.requests.SimpleDiffRequest
import com.intellij.diff.util.DiffUserDataKeys
import com.intellij.diff.util.Side
import com.intellij.openapi.application.EDT
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileTypes.FileTypeManager
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Key
import com.intellij.openapi.util.Pair
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.testFramework.LightVirtualFile
import io.github.kadeng.calu.bridge.errorText
import io.github.kadeng.calu.bridge.wslToWindowsPath
import io.github.kadeng.calu.chat.DiffData
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.absoluteRemotePath
import io.github.kadeng.calu.logic.formatCheckerContext
import io.github.kadeng.calu.logic.formatEscalationContext
import io.github.kadeng.calu.logic.splitArgs
import io.github.kadeng.calu.logic.sshLocalPath
import io.github.kadeng.calu.logic.terminalCommand
import io.github.kadeng.calu.protocol.BridgeError
import io.github.kadeng.calu.protocol.checkerConflict
import io.github.kadeng.calu.rpc.RpcRequestException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.awt.datatransfer.StringSelection
import java.time.LocalTime
import java.time.format.DateTimeFormatter

/**
 * What a checker file knows about the stored checker it shows.
 *
 * @property name The checker's name.
 * @property version The version the text was read from (the base of the next save).
 * @property savedText The text as it was read or last saved.
 */
data class CheckerOrigin(val name: String, val version: Int?, val savedText: String)

/** Marks a `LightVirtualFile` that holds a checker's instructions. */
val CHECKER_ORIGIN: Key<CheckerOrigin> = Key.create("calu.checker.origin")

/**
 * The editor actions of one project.
 *
 * @param project The project.
 */
class EditorActions(private val project: Project) {
    private val service get() = CaluProjectService.of(project)

    /**
     * Translate a path that calu names into the path this IDE opens.
     *
     * @param path A path from calu: absolute on calu's machine, or relative to the session's directory.
     * @return The path on the IDE's machine, or `null` if the file has no counterpart here (SSH
     *   mode: a file outside the project directory of the server).
     */
    fun localPath(path: String): String? {
        val wiring = service.wiring()
        val remote = absoluteRemotePath(path, wiring.controller.state.session.workspace)
        return when (wiring.choice.mode) {
            "wsl" -> if (remote.startsWith("/")) wslToWindowsPath(wiring.choice.wslDistro, remote) else remote
            "ssh" -> {
                // The server names resolved paths; the setting may name the same directory
                // through a link. Both roots map to the local copy.
                val base = project.basePath ?: return null
                val roots = listOfNotNull(wiring.sync?.sync?.remotePaths?.root, CaluSettings.of(project).toDto().sshDirectory)
                roots.firstNotNullOfOrNull { sshLocalPath(remote, it, base) }
            }
            else -> remote
        }
    }

    /**
     * Open a file at a line. A file that does not exist on this machine is reported in the chat.
     *
     * @param path A path from calu (see `localPath`).
     * @param line The 1-based line to put the caret on; `null` or less than 1 for the file's start.
     * @return True if the file was opened.
     */
    suspend fun openFile(path: String, line: Int?): Boolean {
        val local = localPath(path)
        val file: VirtualFile? = local?.let { withContext(Dispatchers.IO) { LocalFileSystem.getInstance().refreshAndFindFileByPath(it.replace('\\', '/')) } }
        if (file == null) {
            service.controller.notice("warning", "The file $path is not on this machine${if (local != null) " (looked for $local)" else " (it is outside the project directory of the server)"}.")
            return false
        }
        withContext(Dispatchers.EDT) {
            OpenFileDescriptor(project, file, maxOf(0, (line ?: 1) - 1), 0).navigate(true)
        }
        return true
    }

    /**
     * Open the IDE's diff viewer with the before and after of one edit, scrolled to the first
     * changed line. If the diff is no longer held (only the newest 300 are), the file is opened.
     *
     * @param toolUseId The tool call that made the edit.
     * @return True if a diff or the file was opened.
     */
    suspend fun openDiff(toolUseId: String): Boolean {
        val diff = service.controller.diffs.get(toolUseId)
        if (diff == null) {
            val tool = service.controller.state.tools[toolUseId]
            val path = tool?.diffPath ?: tool?.input?.str("file_path") ?: return false
            return openFile(path, tool?.editRange?.start)
        }
        withContext(Dispatchers.EDT) { DiffManager.getInstance().showDiff(project, diffRequest(diff)) }
        return true
    }

    /**
     * Build the diff request of one edit.
     *
     * @param diff Before and after.
     * @return A two-sided request titled with the file's path, highlighted by the file's type,
     *   with the scroll position at `diff.startLine` on the right side if it is known.
     */
    fun diffRequest(diff: DiffData): SimpleDiffRequest {
        val type = FileTypeManager.getInstance().getFileTypeByFileName(diff.path.substringAfterLast('/'))
        val factory = DiffContentFactory.getInstance()
        val request = SimpleDiffRequest(
            "calu: ${localPath(diff.path) ?: diff.path}",
            factory.create(project, diff.before, type),
            factory.create(project, diff.after, type),
            "Before the edit",
            "After the edit",
        )
        diff.startLine?.takeIf { it >= 1 }?.let { request.putUserData(DiffUserDataKeys.SCROLL_TO_LINE, Pair.create(Side.RIGHT, it - 1)) }
        return request
    }

    /**
     * Open a checker's instructions in an editor tab (`ide.checker.get`). The tab's banner and
     * "Save All" store the text with `ide.checker.save` (see `saveChecker`).
     *
     * @param name The checker's name.
     * @return True if the editor was opened; a failure is reported in the chat.
     */
    suspend fun editChecker(name: String): Boolean {
        val checker = try {
            service.controller.call("ide.checker.get", buildJsonObject { put("name", JsonPrimitive(name)) }, session = false).asObj() ?: EMPTY_OBJECT
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            service.controller.notice("error", "The instructions of the checker $name were not read: ${errorText(error)}")
            return false
        }
        val text = checker.str("instructions").orEmpty()
        val file = LightVirtualFile("$name.md", FileTypeManager.getInstance().getFileTypeByExtension("md"), text)
        file.putUserData(CHECKER_ORIGIN, CheckerOrigin(name, checker.int("version"), text))
        withContext(Dispatchers.EDT) { FileEditorManager.getInstance(project).openFile(file, true) }
        return true
    }

    /**
     * Open the instructions of the first checker that judges a route (the click on a checker's
     * remark in the transcript; several checkers can serve one route, the picker lists all).
     *
     * @param route "command", "edit", "changeset" or "subagent".
     * @return True if an editor was opened; a failure is reported in the chat.
     */
    suspend fun editCheckerOfRoute(route: String): Boolean {
        val name = try {
            val settings = service.controller.call("ide.settings.get", buildJsonObject { put("project", JsonNull) }, session = false).asObj()
            settings?.get("checkers").let { (it as? kotlinx.serialization.json.JsonArray).orEmpty() }.mapNotNull { it.asObj() }.firstOrNull { it.str("route") == route }?.str("name")
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            service.controller.notice("error", "The checkers were not listed: ${errorText(error)}")
            return false
        }
        if (name == null) {
            service.controller.notice("warning", "No checker is stored for the route $route.")
            return false
        }
        return editChecker(name)
    }

    /**
     * Store the text of a checker file with `ide.checker.save`, with the version it was read
     * from as the base. If the stored version differs (error -32016), nothing is stored: a diff
     * of the stored text against the editor's text is opened, the file's base becomes the stored
     * version, and the chat says that saving again replaces the stored text.
     *
     * @param file A file opened by `editChecker`.
     * @param text The editor's text.
     * @return True if the text was stored (or equals the stored text).
     */
    suspend fun saveChecker(file: VirtualFile, text: String): Boolean {
        val origin = file.getUserData(CHECKER_ORIGIN) ?: return false
        if (text == origin.savedText) return true
        val controller = service.controller
        try {
            val result = controller.call("ide.checker.save", buildJsonObject {
                put("name", JsonPrimitive(origin.name))
                put("instructions", JsonPrimitive(text))
                put("base_version", origin.version?.let { JsonPrimitive(it) } ?: JsonNull)
            }, session = false).asObj()
            file.putUserData(CHECKER_ORIGIN, CheckerOrigin(origin.name, result?.int("version") ?: origin.version, text))
            controller.notice("info", "The instructions of the checker ${origin.name} are stored (version ${result?.int("version") ?: "?"}).")
            return true
        } catch (error: CancellationException) {
            throw error
        } catch (error: RpcRequestException) {
            val stored = if (error.code == BridgeError.CHECKER_CONFLICT) checkerConflict(error.data) else null
            if (stored == null) {
                controller.notice("error", "The instructions of the checker ${origin.name} were not stored: ${error.bridgeMessage}")
                return false
            }
            file.putUserData(CHECKER_ORIGIN, CheckerOrigin(origin.name, stored.version, stored.instructions))
            controller.notice("warning", "The checker ${origin.name} was changed elsewhere (version ${stored.version}); nothing was stored. The diff shows the stored text against yours. Saving again replaces the stored text.")
            val factory = DiffContentFactory.getInstance()
            val request = SimpleDiffRequest("calu: checker ${origin.name}", factory.create(stored.instructions), factory.create(text), "Stored (version ${stored.version})", "Yours")
            withContext(Dispatchers.EDT) { DiffManager.getInstance().showDiff(project, request) }
            return false
        } catch (error: Exception) {
            controller.notice("error", "The instructions of the checker ${origin.name} were not stored: ${errorText(error)}")
            return false
        }
    }

    /**
     * Open a read-only Markdown document: the checker context of the session (`context.get`),
     * or with an id the context of that escalation (`escalation.context`, "Why?").
     *
     * @param escalationId calu's escalation id, or `null` for the checker context.
     * @return True if the document was opened; a failure is reported in the chat.
     */
    suspend fun showContext(escalationId: String?): Boolean {
        val controller = service.controller
        val (title, text) = try {
            if (escalationId == null) {
                val now = LocalTime.now().format(DateTimeFormatter.ofPattern("HH:mm:ss"))
                "Checker context.md" to formatCheckerContext(controller.call("context.get", EMPTY_OBJECT, session = true).asObj() ?: EMPTY_OBJECT, now)
            } else {
                val params = buildJsonObject { put("escalation_id", JsonPrimitive(escalationId)) }
                "Escalation ${escalationId.take(8)}.md" to formatEscalationContext(controller.call("escalation.context", params, session = true).asObj() ?: EMPTY_OBJECT)
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            controller.notice("error", "The context was not read: ${errorText(error)}")
            return false
        }
        val file = LightVirtualFile(title, FileTypeManager.getInstance().getFileTypeByExtension("md"), text)
        file.isWritable = false
        withContext(Dispatchers.EDT) { FileEditorManager.getInstance(project).openFile(file, true) }
        return true
    }

    /**
     * Return the command that starts the calu server from a terminal of this machine (for the
     * SSH mode through `ssh -t`, for WSL through `wsl.exe`).
     *
     * @return The command line for the IDE's terminal.
     */
    fun serverTerminalCommand(): String {
        val wiring = service.wiring()
        val settings = CaluSettings.of(project).toDto()
        return terminalCommand(wiring.controller.serverHelp().command, wiring.choice.mode, wiring.choice.wslDistro, settings.sshHost, splitArgs(settings.sshOptions))
    }

    /**
     * Open the IDE's log file in an editor tab and say in the chat how to find the plugin's lines.
     *
     * @return True if the log file was opened.
     */
    suspend fun showLog(): Boolean {
        val path = com.intellij.openapi.application.PathManager.getLogPath() + "/idea.log"
        val file = withContext(Dispatchers.IO) { LocalFileSystem.getInstance().refreshAndFindFileByPath(path) }
        service.controller.notice("info", "The plugin's lines in the IDE's log contain \"[calu]\" (Help | Show Log).")
        if (file == null) return false
        withContext(Dispatchers.EDT) { OpenFileDescriptor(project, file).navigate(true) }
        return true
    }
}
