/**
 * The backend's implementation of `CaluRemoteApi`: each call finds the project and delegates
 * to `CaluProjectService`, `ChatController` or `EditorActions`.
 */
@file:Suppress("UnstableApiUsage")

package io.github.kadeng.calu.backend

import com.intellij.platform.project.ProjectId
import com.intellij.platform.project.findProjectOrNull
import com.intellij.platform.rpc.backend.RemoteApiProvider
import fleet.rpc.remoteApiDescriptor
import io.github.kadeng.calu.bridge.errorText
import io.github.kadeng.calu.chat.ChatUpdate
import io.github.kadeng.calu.json.CaluJson
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.parseObjectOrNull
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.remote.CallResultDto
import io.github.kadeng.calu.remote.CaluRemoteApi
import io.github.kadeng.calu.remote.ChatUpdateDto
import io.github.kadeng.calu.remote.PasswordRequestDto
import io.github.kadeng.calu.remote.PluginSettingsDto
import io.github.kadeng.calu.rpc.RpcRequestException
import io.github.kadeng.calu.state.ViewState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/** Registers the RPC implementation with the platform. */
internal class BackendRpcApiProvider : RemoteApiProvider {
    override fun RemoteApiProvider.Sink.remoteApis() {
        remoteApi(remoteApiDescriptor<CaluRemoteApi>()) { BackendCaluRemoteApi() }
    }
}

/** See `CaluRemoteApi` for the contract of each method. */
class BackendCaluRemoteApi : CaluRemoteApi {
    @OptIn(ExperimentalCoroutinesApi::class)
    override suspend fun updates(projectId: ProjectId): Flow<ChatUpdateDto> {
        val project = projectId.findProjectOrNull() ?: return emptyFlow()
        val service = CaluProjectService.of(project)
        // A change of "How calu is reached" replaces the controller: start again with its snapshot.
        return service.restarted.flatMapLatest {
            service.controller.updates().map { update ->
                when (update) {
                    is ChatUpdate.Snapshot -> ChatUpdateDto(stateJson = CaluJson.encodeToString(ViewState.serializer(), update.state))
                    is ChatUpdate.Event -> ChatUpdateDto(method = update.event.method, paramsJson = CaluJson.encodeToString(JsonObject.serializer(), update.event.params))
                }
            }
        }
    }

    override suspend fun act(projectId: ProjectId, action: String, argsJson: String): Boolean {
        val project = projectId.findProjectOrNull() ?: return false
        val c = CaluProjectService.of(project).controller
        val a = parseObjectOrNull(argsJson) ?: EMPTY_OBJECT
        val editor = EditorActions(project)
        return when (action) {
            "attach" -> c.attach(a.str("session"))
            "start" -> c.startSession(a.str("kind") ?: "new", a.str("claude_session_id"), a.int("fork_before_turn"), a.str("prompt"), a.str("name"))
            "cancelStart" -> c.cancelStart()
            "detach" -> { c.detach(); true }
            "refreshSessions" -> { c.refreshStatus(); c.refreshSessions(true) != null }
            "send" -> c.send(a.str("text").orEmpty(), a.bool("inject"), a.str("tab") ?: "main")
            "decide" -> c.decide(a.str("key") ?: return false, a.bool("allow"), a.str("note"), a.str("answer"))
            "interrupt" -> c.interrupt()
            "setModel" -> { c.setModel(a.str("model")); true }
            "setEffort" -> { c.setEffort(a.str("effort") ?: "default"); true }
            "setLevel" -> c.setLevel(a.str("level").orEmpty())
            "rename" -> { c.rename(a.str("session_id") ?: return false, a.str("name").orEmpty()); true }
            "restartBridge" -> { c.restartBridge(); true }
            "unlock" -> c.unlock()
            "openDiff" -> editor.openDiff(a.str("tool_use_id") ?: return false)
            "openFile" -> editor.openFile(a.str("path") ?: return false, a.int("line"))
            "editChecker" -> editor.editChecker(a.str("name") ?: return false)
            "editCheckerRoute" -> editor.editCheckerOfRoute(a.str("route") ?: return false)
            "showContext" -> editor.showContext(a.str("escalation_id"))
            "openTerminal" -> editor.openServerTerminal()
            "setHonesty" -> setHonesty(c, a.str("mode") ?: return false)
            else -> false
        }
    }

    /**
     * Store `honesty.mode` in calu's global settings and note it in the chat's state.
     *
     * @param c The chat controller.
     * @param mode "off", "track" or "enforce".
     * @return True if the setting was stored; a failure is reported in the chat.
     */
    private suspend fun setHonesty(c: io.github.kadeng.calu.chat.ChatController, mode: String): Boolean = try {
        c.call("ide.settings.set", buildJsonObject { put("key", JsonPrimitive("honesty.mode")); put("value", JsonPrimitive(mode)); put("project", JsonNull) }, session = false)
        c.dispatch("ui.honestyStored", buildJsonObject { put("mode", JsonPrimitive(mode)) })
        c.notice("info", "Honesty mode \"$mode\" is stored in calu's global settings; a running session reports when it uses it.")
        true
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        c.notice("error", "The honesty mode was not stored: ${errorText(error)}")
        false
    }

    override suspend fun call(projectId: ProjectId, method: String, paramsJson: String, session: Boolean): CallResultDto {
        val project = projectId.findProjectOrNull() ?: return CallResultDto(false, message = "The project is not open.")
        return try {
            val result = CaluProjectService.of(project).controller.call(method, parseObjectOrNull(paramsJson) ?: EMPTY_OBJECT, session)
            CallResultDto(true, CaluJson.encodeToString(JsonElement.serializer(), result))
        } catch (error: CancellationException) {
            throw error
        } catch (error: RpcRequestException) {
            CallResultDto(false, code = error.code, message = error.bridgeMessage, dataJson = error.data?.let { CaluJson.encodeToString(JsonElement.serializer(), it) })
        } catch (error: Exception) {
            CallResultDto(false, message = errorText(error))
        }
    }

    override suspend fun passwordRequests(projectId: ProjectId): Flow<PasswordRequestDto?> {
        val project = projectId.findProjectOrNull() ?: return flowOf(null)
        return CaluProjectService.of(project).passwordRequests
    }

    override suspend fun answerPassword(projectId: ProjectId, id: Int, password: String?) {
        projectId.findProjectOrNull()?.let { CaluProjectService.of(it).answerPassword(id, password) }
    }

    override suspend fun getSettings(projectId: ProjectId): PluginSettingsDto =
        projectId.findProjectOrNull()?.let { CaluSettings.of(it).toDto() } ?: PluginSettingsDto()

    override suspend fun setSettings(projectId: ProjectId, settings: PluginSettingsDto) {
        projectId.findProjectOrNull()?.let { CaluProjectService.of(it).applySettings(settings) }
    }
}
