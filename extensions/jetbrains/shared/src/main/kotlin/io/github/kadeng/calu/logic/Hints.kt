/**
 * Setup hints of the chat view (no IntelliJ Platform import): what is missing for the chat to
 * work and which button fixes it. The host side of the plugin computes them and sends them to
 * the view with `ui.hints`.
 *
 *     calu not found            -> "calu-missing"   button: Open settings (set the calu command)
 *     claude not found          -> "claude-missing" button: How to install (opens the docs)
 *     password not given yet    -> "locked"         button: Unlock (asks the calu user password)
 *     database not reachable    -> "database"       button: Open settings (Setup card)
 *     no session attached and
 *       the server stopped       -> "server-stopped" buttons: Start server, Retry (ServerLoss.kt)
 *       none running / no server -> "no-server"     button: Start calu server
 *       otherwise                -> "no-session"    button: New session
 *
 * The `ide.status` result comes from the bridge and is read defensively: a member that is
 * missing or has another JSON type never raises an exception.
 *
 * Ported from `extensions/vscode/src/shared/hints.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.arr
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.bool
import io.github.kadeng.calu.json.jsTruthy
import io.github.kadeng.calu.json.obj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.state.SetupHint
import kotlinx.serialization.json.JsonObject

/** Where the user reads how to install Claude Code (`CLAUDE_DOCS_URL` of `shared/bootstrap.ts`). */
const val CLAUDE_DOCS_URL: String = "https://docs.anthropic.com/en/docs/claude-code"

/**
 * Inputs of `buildHints`.
 *
 * @property caluMissing True if no calu command was found on the machine.
 * @property status The last `ide.status` result, or `null` if it was not asked yet or failed.
 * @property attached True if a session is attached.
 * @property noServer True if `ide.start` last failed because no server answered (error -32013).
 * @property locked True if calu's user login needs its password and it was not given yet.
 * @property serverStopped True while the link was lost because the calu server stopped
 *   (`serverStoppedAfterLoss`) and no server was found since.
 * @property serverRetriedAt Local time of the last Retry that found no server ("14:02:11");
 *   `null` before it.
 */
data class HintInput(
    val caluMissing: Boolean,
    val status: JsonObject?,
    val attached: Boolean,
    val noServer: Boolean,
    val locked: Boolean = false,
    val serverStopped: Boolean = false,
    val serverRetriedAt: String? = null,
)

/**
 * Compute the setup hints, most important first.
 *
 * @param input What is known about the machine and the session.
 * @return The hints. With calu missing, only that hint. While a session is attached, the
 *   "server-stopped", "no-server" and "no-session" hints are left out; with "server-stopped"
 *   the other two are left out as well. The "database" hint needs a status whose
 *   `database` member is an object with `reachable` not true, and is left out while `locked`.
 */
fun buildHints(input: HintInput): List<SetupHint> {
    if (input.caluMissing) {
        return listOf(
            SetupHint(
                id = "calu-missing",
                level = "error",
                message = "calu is not installed on this machine. Install it with uv (`uv tool install`), see the " +
                    "plugin's README, or set the calu command in Settings | Tools | calu.",
                action = "openSettings",
                actionLabel = "Open settings",
            ),
        )
    }
    val hints = mutableListOf<SetupHint>()
    val status = input.status
    val claude = status?.arr("findings")?.firstNotNullOfOrNull { f -> f.asObj()?.takeIf { it.str("name") == "claude" } }
    if (claude != null && !claude["ok"].jsTruthy() && claude.str("level") == "missing") {
        val found = claude.str("message").orEmpty()
        hints.add(
            SetupHint(
                id = "claude-missing",
                level = "error",
                message = "Claude Code (`claude`) was not found on this machine${if (found.isNotEmpty()) " ($found)" else ""}. " +
                    "The plugin does not install Claude Code: follow the instructions, then run `claude` once " +
                    "in a terminal in the project folder.",
                action = "openLink",
                actionLabel = "How to install Claude Code",
                href = CLAUDE_DOCS_URL,
                short = "Claude Code not found",
                actionShort = "Guide",
            ),
        )
    }
    if (input.locked) {
        hints.add(
            SetupHint(
                id = "locked",
                level = "warning",
                message = "calu needs the calu user password once to list and attach to sessions. " +
                    "It is kept in memory until this window closes.",
                action = "unlock",
                actionLabel = "Unlock",
            ),
        )
    }
    val database = status?.obj("database")
    if (database != null && !database["reachable"].jsTruthy() && !input.locked) {
        val problem = database.str("problem").orEmpty().ifEmpty { "unknown problem" }
        hints.add(
            SetupHint(
                id = "database",
                level = "error",
                message = "calu's database is not reachable: $problem.",
                action = "openSettings",
                actionLabel = "Open settings",
            ),
        )
    }
    if (!input.attached && input.serverStopped) {
        hints.add(
            SetupHint(
                id = SERVER_STOPPED_HINT,
                level = "warning",
                message = serverStoppedMessage(input.serverRetriedAt),
                action = "startServer",
                actionLabel = "Start server",
            ),
        )
    } else if (!input.attached) {
        if (input.noServer) {
            hints.add(
                SetupHint(
                    id = "no-server",
                    level = "warning",
                    message = "No calu server answered. Start `calu serve` in a terminal; it runs the sessions and " +
                        "keeps running after the IDE closes.",
                    action = "startServer",
                    actionLabel = "Start calu server",
                ),
            )
        }
        hints.add(
            SetupHint(
                id = "no-session",
                level = "info",
                message = "No session is attached. Start a new session, or attach to a running one.",
                action = "newSession",
                actionLabel = "New session",
            ),
        )
    }
    return hints
}

/**
 * Tell from the findings of `ide.status` whether a program is installed.
 *
 * @param status The last `ide.status` result, or `null`.
 * @param name The finding's name, e.g. "tmux".
 * @return True if a finding with that name exists and its `ok` is the JSON literal `true`; false
 *   otherwise (also when the status is unknown).
 */
fun findingOk(status: JsonObject?, name: String): Boolean =
    status?.arr("findings")?.any { f -> f.asObj()?.let { it.str("name") == name && it.bool("ok") } == true } == true
