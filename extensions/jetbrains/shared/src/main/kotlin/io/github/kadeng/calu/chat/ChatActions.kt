/**
 * Further actions of the chat controller, and the dispatch of an action by its name.
 *
 *     act("rewind", {turn, uuid?})   ──► session.history (find the conversation's turn of `uuid`)
 *                                    ──► ide.start {kind: "resume", claude_session_id, fork_before_turn, name: "rewind of …"}
 *                                    ──► event `ui.restoreInput {text}` (the message's text, for the input field)
 *     act("forkAt", {turn, uuid?})   ──► the same with the name "fork of …", without `ui.restoreInput`
 *     act("forkConversation", {})    ──► ide.start {kind: "fork", claude_session_id, name: "fork of …"}
 *     act("sideAsk", {question})     ──► side.ask {question, history}  (state: `ViewState.side`)
 *     act("honestyReview" | "honestyTrust" | "honestyClear" {mark_id})  ──► honesty.review / .trust / .clear
 *     act("comment", <params of comment.add>)                           ──► comment.add
 *
 * They are extension functions so that `ChatController.kt` stays readable; they behave like
 * its own actions: a failed request is reported as a notice in the chat and nothing is thrown.
 * Port of the host side of the VSCode extension (`extension.ts`: `forkBefore`,
 * `forkConversation`, `handleWebviewMessage`).
 */
package io.github.kadeng.calu.chat

import io.github.kadeng.calu.bridge.errorText
import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.CommentInput
import io.github.kadeng.calu.logic.buildCommentParams
import io.github.kadeng.calu.logic.derivedSessionName
import io.github.kadeng.calu.logic.parseOutgoingImages
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.isAttached
import io.github.kadeng.calu.state.own
import io.github.kadeng.calu.state.sessionTitle
import io.github.kadeng.calu.state.sideHistory
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject

/** Turns asked from `session.history` to find the conversation's number of a message's turn. */
const val TURN_LOOKUP_LIMIT: Int = 1000

/**
 * Find the conversation's number of the turn that a user message started.
 *
 * The view counts the turns it saw; a resumed conversation has earlier ones. calu's record
 * (`session.history`) holds the number of every turn with the id of its prompt.
 *
 * @param turn The turn as the view counted it (`UserItem.turn`).
 * @param uuid Claude Code's id of the message (`UserItem.uuid`), or `null`.
 * @return The `index` of the recorded turn whose `prompt_uuid` is `uuid`; `turn` if `uuid` is
 *   `null`, no such turn is recorded or the record cannot be read (logged).
 */
suspend fun ChatController.conversationTurn(turn: Int, uuid: String?): Int {
    if (uuid.isNullOrEmpty()) return turn
    return try {
        val history = supervisor.session("session.history", buildJsonObject { put("limit", JsonPrimitive(TURN_LOOKUP_LIMIT)) }, false).asObj()
        history?.arr("turns")?.mapNotNull { it.asObj() }?.firstOrNull { it.str("prompt_uuid") == uuid }?.int("index") ?: turn
    } catch (error: CancellationException) {
        throw error
    } catch (_: Exception) {
        turn
    }
}

/**
 * Build the name of a session that is cut from the attached one.
 *
 * @param how "fork" or "rewind".
 * @return "fork of <name>" (see `derivedSessionName`), or `null` if no session is attached or
 *   the session list does not hold it.
 */
private fun ChatController.derivedName(how: String): String? {
    val s = state
    val entry = s.sessions.list.firstOrNull { it.id == s.sessions.attachedId } ?: return null
    return derivedSessionName(how, sessionTitle(entry)).takeIf { it.isNotEmpty() }
}

/**
 * Start a session that continues the attached conversation from before one of its turns
 * ("Rewind to before this message" and "Fork from here"). The session it is cut from keeps
 * running; files are not reverted.
 *
 *     not attached, or no conversation id      -> notice, false
 *     the turn is the conversation's first     -> notice (nothing would be kept), false
 *     else  ide.start {kind: "resume", claude_session_id, fork_before_turn: N, name?}
 *           and, for a rewind that started, the event `ui.restoreInput {text: <the message>}`
 *
 * The caller asks the confirmation of a rewind first (`REWIND_CONFIRM_TITLE`,
 * `REWIND_CONFIRM_DETAIL` in `logic/ItemMenu.kt`).
 *
 * @param turn The turn as the view counted it (`UserItem.turn`, `UserMessageMenu.turn`).
 * @param uuid Claude Code's id of the user message, or `null` if unknown.
 * @param rewind True for "Rewind" (named "rewind of <session>", the message's text is offered
 *   to the input field), false for "Fork from here" ("fork of <session>").
 * @return True if the new session started and is attached.
 */
suspend fun ChatController.forkBefore(turn: Int, uuid: String?, rewind: Boolean): Boolean {
    val before = state
    val conversation = before.session.claudeSessionId
    if (!isAttached(before) || conversation.isNullOrEmpty()) {
        notice("info", "Attach to a session with a conversation first.")
        return false
    }
    val message = own(before.items, MAIN).orEmpty().filterIsInstance<UserItem>()
        .firstOrNull { if (!uuid.isNullOrEmpty()) it.uuid == uuid else it.turn == turn }
    val number = conversationTurn(turn, uuid)
    if (number < 2) {
        notice("info", "This is the first message of the conversation, so nothing would be kept. Start a new session instead.")
        return false
    }
    val started = startSessionAs(
        if (rewind) "Rewinding the conversation" else "Forking the conversation",
        "resume", conversation, number, name = derivedName(if (rewind) "rewind" else "fork"),
    )
    if (started && rewind && message != null) dispatch("ui.restoreInput", buildJsonObject { put("text", JsonPrimitive(message.text)) })
    return started
}

/**
 * "Rewind to before this message": see `forkBefore` with `rewind` true.
 *
 * @param turn The turn as the view counted it.
 * @param uuid Claude Code's id of the user message, or `null`.
 * @return True if the new session started and is attached.
 */
suspend fun ChatController.rewind(turn: Int, uuid: String? = null): Boolean = forkBefore(turn, uuid, rewind = true)

/**
 * "Fork from here": see `forkBefore` with `rewind` false.
 *
 * @param turn The turn as the view counted it.
 * @param uuid Claude Code's id of the user message, or `null`.
 * @return True if the new session started and is attached.
 */
suspend fun ChatController.forkAt(turn: Int, uuid: String? = null): Boolean = forkBefore(turn, uuid, rewind = false)

/**
 * Start a new session from a copy of the attached conversation (`ide.start {kind: "fork"}`);
 * the original stays as it is.
 *
 * @return True if the session started and is attached; false (with a notice) if no
 *   conversation is attached or the start failed.
 */
suspend fun ChatController.forkConversation(): Boolean {
    val conversation = state.session.claudeSessionId
    if (conversation.isNullOrEmpty()) {
        notice("info", "Attach to a session with a conversation first.")
        return false
    }
    return startSession("fork", conversation, name = derivedName("fork"))
}

/**
 * Ask a side question about the session (`side.ask`): it is answered beside the conversation
 * and does not enter it. The question and its answer (or the failure) are put into
 * `ViewState.side` (`ui.sideAsk`, `ui.sideAnswer`).
 *
 * @param question The question; blank text is not sent.
 * @return True if an answer arrived.
 */
suspend fun ChatController.sideAsk(question: String): Boolean {
    if (question.isBlank()) return false
    val id = sideIds.getAndIncrement()
    val history = sideHistory(state)
    dispatch("ui.sideAsk", buildJsonObject { put("id", JsonPrimitive(id)); put("question", JsonPrimitive(question)) })
    return try {
        val result = supervisor.session("side.ask", buildJsonObject {
            put("question", JsonPrimitive(question))
            put("history", buildJsonArray {
                for (h in history) add(buildJsonObject { put("question", JsonPrimitive(h.question)); put("answer", JsonPrimitive(h.answer)) })
            })
        }).asObj()
        dispatch("ui.sideAnswer", buildJsonObject { put("id", JsonPrimitive(id)); put("answer", JsonPrimitive(result?.str("answer").orEmpty())) })
        true
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        dispatch("ui.sideAnswer", buildJsonObject { put("id", JsonPrimitive(id)); put("error", JsonPrimitive(errorText(error))) })
        false
    }
}

/**
 * Run an honesty review of the attached session now (`honesty.review`).
 *
 * @return True if the request was answered; if no review ran, a warning with the session's
 *   reason is put into the chat.
 */
suspend fun ChatController.honestyReview(): Boolean = run("The honesty review") {
    val result = supervisor.session("honesty.review").asObj() ?: EMPTY_OBJECT
    if (!result.bool("ran")) {
        notice("warning", "No honesty review ran" + (result.str("problem")?.takeIf { it.isNotEmpty() }?.let { ": $it" } ?: "."))
    }
}

/**
 * Record the commit that git HEAD names now as honest (`honesty.trust {rev: "HEAD"}`); it
 * becomes the baseline of the honesty state. The caller asks the confirmation first.
 *
 * @return True if the session accepted the request.
 */
suspend fun ChatController.honestyTrust(): Boolean =
    run("Trusting HEAD") { supervisor.session("honesty.trust", buildJsonObject { put("rev", JsonPrimitive("HEAD")) }) }

/**
 * Clear one honesty mark (`honesty.clear`).
 *
 * @param markId The mark's id (`HonestyMarkInfo.id`).
 * @return True if the session accepted the request.
 */
suspend fun ChatController.honestyClear(markId: Int): Boolean =
    run("Clearing the mark") { supervisor.session("honesty.clear", buildJsonObject { put("mark_id", JsonPrimitive(markId)) }) }

/**
 * Send a comment on a selection to Claude (`comment.add`): calu cuts the commented excerpt,
 * relays the comment to Claude and stores it.
 *
 * @param params The params of `comment.add` as `buildCommentParams` builds them: `kind`, `path`,
 *   `side`, `start_line`, `end_line`, `after_text`, `text`, and for a diff `before_text` and
 *   optionally `ref`.
 * @return True if the session accepted the comment; false (with a notice in the chat) if the
 *   params lack `path`, `text` or `after_text`, no session is attached, or the session refused.
 */
suspend fun ChatController.comment(params: JsonObject): Boolean {
    if (params.str("path").isNullOrEmpty() || params.str("text").isNullOrBlank() || params.str("after_text") == null) {
        notice("warning", "The comment was not sent: it needs a file of the project and a text.")
        return false
    }
    return run("Sending the comment") { supervisor.session("comment.add", params) }
}

/**
 * Build the request of a comment from what the editor gave and send it (see the other overload).
 *
 * @param input The path, the selection, the document texts and the user's comment.
 * @return True if the session accepted the comment; an input that `buildCommentParams` refuses
 *   is reported as a notice.
 */
suspend fun ChatController.comment(input: CommentInput): Boolean {
    val params = try {
        buildCommentParams(input)
    } catch (error: IllegalArgumentException) {
        notice("warning", "The comment was not sent: ${error.message}")
        return false
    }
    return comment(params)
}

/**
 * Run one chat action by its name: the actions that need nothing of the IDE. A backend calls
 * this first and handles what it returns `null` for (opening editors, diffs and terminals).
 *
 *     attach {session?}             start {kind, claude_session_id?, fork_before_turn?, prompt?, name?}
 *     cancelStart {}                detach {}                     refreshSessions {}
 *     send {text, inject, tab, images?: [{data: base64, mediaType}]}
 *     decide {key, allow, note?, answer?}
 *     interrupt {}                  setModel {model}              setEffort {effort}
 *     setLevel {level}              rename {session_id, name}     restartBridge {}
 *     setPermissionMode {mode}
 *     unlock {}                     refreshUsage {}               usagePanel {}
 *     retryServer {}                lookForServer {}    (after "The calu server stopped": Retry, the timer)
 *     rewind {turn, uuid?}          forkAt {turn, uuid?}          forkConversation {}
 *     sideAsk {question}            honestyReview {}              honestyTrust {}
 *     honestyClear {mark_id}        comment {kind, path, side, start_line, end_line, after_text, text, before_text?, ref?}
 *
 * @param action The action's name.
 * @param args Its arguments (missing members count as absent; a missing required member makes
 *   the action fail).
 * @return True if the action did what it names, false if it failed (the failure is in the
 *   chat) or a required argument is missing; `null` if this function does not know the action.
 */
suspend fun ChatController.act(action: String, args: JsonObject = EMPTY_OBJECT): Boolean? = when (action) {
    "attach" -> attach(args.str("session"))
    "start" -> startSession(args.str("kind") ?: "new", args.str("claude_session_id"), args.int("fork_before_turn"), args.str("prompt"), args.str("name"))
    "cancelStart" -> cancelStart()
    "detach" -> { detach(); true }
    "refreshSessions" -> { refreshStatus(); refreshSessions(true) != null }
    "send" -> parseOutgoingImages(args["images"])?.let { sendMessage(args.str("text").orEmpty(), args.bool("inject"), args.str("tab") ?: MAIN, it) } ?: false
    "decide" -> args.str("key")?.let { decide(it, args.bool("allow"), args.str("note"), args.str("answer")) } ?: false
    "interrupt" -> interrupt()
    "setModel" -> { setModel(args.str("model")); true }
    "setEffort" -> { setEffort(args.str("effort") ?: "default"); true }
    "setLevel" -> setLevel(args.str("level").orEmpty())
    "setPermissionMode" -> setPermissionMode(args.str("mode").orEmpty())
    "rename" -> args.str("session_id")?.let { rename(it, args.str("name").orEmpty()); true } ?: false
    "restartBridge" -> { restartBridge(); true }
    "unlock" -> unlock()
    "refreshUsage" -> { refreshUsage(); true }
    "usagePanel" -> usagePanel()
    "retryServer" -> { lookForServer(true); true }
    "lookForServer" -> { lookForServer(false); true }
    "rewind" -> args.int("turn")?.let { rewind(it, args.str("uuid")) } ?: false
    "forkAt" -> args.int("turn")?.let { forkAt(it, args.str("uuid")) } ?: false
    "forkConversation" -> forkConversation()
    "sideAsk" -> sideAsk(args.str("question").orEmpty())
    "honestyReview" -> honestyReview()
    "honestyTrust" -> honestyTrust()
    "honestyClear" -> args.int("mark_id")?.let { honestyClear(it) } ?: false
    "comment" -> comment(args)
    else -> null
}
