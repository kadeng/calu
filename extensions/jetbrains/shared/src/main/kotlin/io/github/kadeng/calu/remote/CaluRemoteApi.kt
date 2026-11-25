/**
 * What the frontend asks the backend, over the platform's RPC.
 *
 *     frontend (tool window, thin client in remote development)
 *        │  CaluRemoteApi: updates(), act(), call(), passwordRequests(), approvalRequests(), settings
 *        ▼
 *     backend (next to the project: starts `calu ide`, owns the ChatController)
 *
 * In a local IDE both modules run in one process and each call is an in-process suspend call.
 * Rules this plugin follows (README, "Rules"): only `suspend` methods; arguments and results
 * are `@Serializable` types of this module made of strings, numbers and booleans, with JSON
 * carried as text, so that no module ever asks another module's class for its serializer;
 * calls are made from coroutines, never from the EDT; long-lived flows are collected inside
 * `durable {}` by the frontend.
 */
@file:Suppress("UnstableApiUsage")

package io.github.kadeng.calu.remote

import com.intellij.platform.project.ProjectId
import com.intellij.platform.rpc.RemoteApiProviderService
import fleet.rpc.RemoteApi
import fleet.rpc.Rpc
import fleet.rpc.remoteApiDescriptor
import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.Serializable

/**
 * One message of the chat's update stream.
 *
 * Exactly one of the two shapes is set: a snapshot (`stateJson`: the whole `ViewState` as JSON,
 * always the first message of a stream) or an event (`method` and `paramsJson`, to apply with
 * `applyEvent`).
 *
 * @property stateJson The whole view state, or `null` for an event.
 * @property method The event's method, or `null` for a snapshot.
 * @property paramsJson The event's params as a JSON object text, or `null` for a snapshot.
 */
@Serializable
data class ChatUpdateDto(val stateJson: String? = null, val method: String? = null, val paramsJson: String? = null)

/**
 * The answer to `call`.
 *
 * @property ok True if the bridge answered with a result.
 * @property resultJson The result as JSON text (`ok` true); "null" otherwise.
 * @property code The JSON-RPC error code (`ok` false); 0 if the failure has none (bridge not running).
 * @property message The error message (`ok` false); "" otherwise.
 * @property dataJson The error's `data` as JSON text, or `null`.
 */
@Serializable
data class CallResultDto(
    val ok: Boolean,
    val resultJson: String = "null",
    val code: Int = 0,
    val message: String = "",
    val dataJson: String? = null,
)

/**
 * The calu user password, which the backend needs from the user.
 *
 * @property id Number of the request; the answer names it.
 * @property text The prompt text (with the reason the previous entry was refused, if any).
 */
@Serializable
data class PasswordRequestDto(val id: Int, val text: String)

/**
 * The approval of a project's calu settings, which the backend needs before it runs anything
 * they name (D279, S4).
 *
 * @property id Number of the request; the answer names it.
 * @property text What the dialog shows (`approvalText`): the command and the settings.
 */
@Serializable
data class ApprovalRequestDto(val id: Int, val text: String)

/**
 * The editor-side settings of the plugin for one project (stored by the backend, because the
 * backend starts the bridge).
 *
 * @property mode How calu is reached: "auto" (WSL for a Windows IDE on a WSL project, else
 *   local), "local", "wsl" or "ssh".
 * @property caluCommand The calu command line, arguments separated by spaces (quotes group);
 *   "" to find `calu` on PATH and in ~/.local/bin.
 * @property sshHost SSH destination of the SSH mode (an alias of ~/.ssh/config, or user@host).
 * @property sshDirectory The project directory on the server (SSH mode).
 * @property sshOptions Further `ssh` arguments, separated by spaces.
 * @property syncMode "calu" (files are synchronised through the bridge) or "none" (the user
 *   synchronises the copy himself, e.g. with the IDE's Deployment); used in SSH mode only.
 * @property notifications Show a notification with Allow / Deny when a step waits and the tool
 *   window is hidden.
 * @property serverSudo Add `--sudo` to the server command that is shown.
 * @property serverInsecure Add `--insecure` to the server command that is shown.
 * @property serverExtraArgs Further arguments of `calu serve`, separated by spaces.
 */
@Serializable
data class PluginSettingsDto(
    val mode: String = "auto",
    val caluCommand: String = "",
    val sshHost: String = "",
    val sshDirectory: String = "",
    val sshOptions: String = "",
    val syncMode: String = "calu",
    val notifications: Boolean = true,
    val serverSudo: Boolean = false,
    val serverInsecure: Boolean = false,
    val serverExtraArgs: String = "",
)

/** The backend's side of the plugin, as the frontend calls it. */
@Rpc
interface CaluRemoteApi : RemoteApi<Unit> {
    companion object {
        /**
         * Resolve the API (the backend's implementation, in-process or over the network).
         *
         * @return The API.
         */
        suspend fun getInstance(): CaluRemoteApi = RemoteApiProviderService.resolve(remoteApiDescriptor<CaluRemoteApi>())
    }

    /**
     * Subscribe to the chat of a project. Side effect: starts the bridge of the project if it
     * was not started yet.
     *
     * @param projectId The project.
     * @return A flow of a snapshot followed by every later event, in order; empty if the
     *   project is not open in the backend.
     */
    suspend fun updates(projectId: ProjectId): Flow<ChatUpdateDto>

    /**
     * Run one action of the chat. The action reports its own failures in the chat (a notice).
     *
     * Actions and their arguments (`argsJson` is a JSON object):
     *
     *     attach {session?}            start {kind, claude_session_id?, fork_before_turn?, prompt?, name?}
     *     cancelStart {}               detach {}                    refreshSessions {}
     *     send {text, inject, tab}     decide {key, allow, note?, answer?}
     *     interrupt {}                 setModel {model}             setEffort {effort}
     *     setLevel {level}             rename {session_id, name}    restartBridge {}
     *     unlock {}                    openDiff {tool_use_id}       openFile {path, line?}
     *     editChecker {name}           showContext {escalation_id?} 
     *     setHonesty {mode}            syncNow {}                   syncResolve {path?}
     *     syncUploadNow {}             syncShowSkipped {}           setupInterpreter {}
     *     setPermissionMode {mode}
     *
     * @param projectId The project.
     * @param action The action's name.
     * @param argsJson Its arguments as JSON object text.
     * @return True if the action did what it names; false if it failed or is unknown.
     */
    suspend fun act(projectId: ProjectId, action: String, argsJson: String): Boolean

    /**
     * Send one bridge request and return its answer, for features that keep no state in the
     * backend (settings form, model presets, lists of checkers and stored conversations).
     *
     * @param projectId The project.
     * @param method An `ide.*` method, or with `session` true a method of the session protocol.
     * @param paramsJson The params as JSON object text.
     * @param session True to send it to the attached session (`session.call`).
     * @return The result or the error; never throws for a failed request.
     */
    suspend fun call(projectId: ProjectId, method: String, paramsJson: String, session: Boolean): CallResultDto

    /**
     * The password requests of a project's bridge.
     *
     * @param projectId The project.
     * @return A flow that yields the open request (if any) at once and each later one; `null`
     *   when none is open.
     */
    suspend fun passwordRequests(projectId: ProjectId): Flow<PasswordRequestDto?>

    /**
     * Answer a password request.
     *
     * @param projectId The project.
     * @param id The request's number; an answer to another number is ignored.
     * @param password The entered password (possibly empty), or `null` if the user cancelled.
     *   It is passed to the bridge and kept in the backend's memory; never stored or logged.
     */
    suspend fun answerPassword(projectId: ProjectId, id: Int, password: String?)

    /**
     * The approval requests of a project: the backend starts nothing for the project's
     * settings until one is answered with yes.
     *
     * @param projectId The project.
     * @return A flow that yields the open request (if any) at once and each later one; `null`
     *   when none is open.
     */
    suspend fun approvalRequests(projectId: ProjectId): Flow<ApprovalRequestDto?>

    /**
     * Answer an approval request.
     *
     * @param projectId The project.
     * @param id The request's number; an answer to another number is ignored.
     * @param approved True to approve the shown settings (kept for this IDE) and start calu.
     */
    suspend fun answerApproval(projectId: ProjectId, id: Int, approved: Boolean)

    /**
     * Read the editor-side settings.
     *
     * @param projectId The project.
     * @return The settings; the defaults if the project is not open in the backend.
     */
    suspend fun getSettings(projectId: ProjectId): PluginSettingsDto

    /**
     * Store the editor-side settings. Side effect: if what decides how calu is reached changed,
     * the bridge is stopped and started with the new settings.
     *
     * @param projectId The project.
     * @param settings The new settings.
     */
    suspend fun setSettings(projectId: ProjectId, settings: PluginSettingsDto)

    /**
     * Return the command that starts the calu server from a terminal of the IDE (for the SSH
     * mode through `ssh -t`, for WSL through `wsl.exe`). The frontend opens the terminal tab,
     * because the terminal's tabs belong to the IDE's window.
     *
     * @param projectId The project.
     * @return The command line; "" if the project is not open in the backend.
     */
    suspend fun serverTerminalCommand(projectId: ProjectId): String

    /**
     * Tell which edit's diff shows a text, for a comment made in a diff editor of the client
     * (in remote development the client's diff editor does not know its file).
     *
     * @param projectId The project.
     * @param text The whole text of the editor the selection was made in.
     * @return JSON object text `{"path", "side", "before", "after", "ref"}` with `path`
     *   relative to the project directory and `ref` the edit's reference ("edit:<tool_use_id>"),
     *   or "" if no held diff has that text or its file is outside the project.
     */
    suspend fun diffOfText(projectId: ProjectId, text: String): String

    /**
     * Name a file of the project relative to the project directory, for a comment made in a
     * normal editor of the client (in remote development the client's files are not in its
     * local file system, and its project directory need not be the backend's).
     *
     * @param projectId The project.
     * @param path The file's path as the client shows it: absolute, on the backend's machine.
     * @return The path relative to the project directory ("/" separators), or "" if the path
     *   is not absolute, names no existing file on the backend's machine, or lies outside the
     *   project directory.
     */
    suspend fun projectPathOf(projectId: ProjectId, path: String): String
}
