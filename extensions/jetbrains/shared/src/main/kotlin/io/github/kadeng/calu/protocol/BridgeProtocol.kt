/**
 * The bridge protocol as far as the plugin names it: error codes and readers for error data
 * (`docs/notes/vscode-extension-spec.md`, sections 2 and 6, and "Deviations").
 *
 *     plugin --request--> calu ide            ide.* and session.call {method, params}
 *     plugin <--notification-- calu ide       session.event {method, params}, ide.link, ide.log
 *
 * Results are read as JSON with the helpers of `io.github.kadeng.calu.json` where they are used;
 * the plugin holds no second copy of the result shapes. Ported from
 * `extensions/vscode/src/bridgeProtocol.ts`.
 */
package io.github.kadeng.calu.protocol

import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.int
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.json.strings
import kotlinx.serialization.json.JsonElement

/** Version of the session protocol this plugin was written against (`calu protocol`). */
const val PROTOCOL_VERSION: Int = 1

/** Name the plugin sends as `client` in `ide.initialize`. */
const val CLIENT_NAME: String = "jetbrains"

/** Error codes of the bridge (JSON-RPC `error.code`). */
object BridgeError {
    /** A value was refused by calu's validation; the message says why. */
    const val INVALID_PARAMS: Int = -32602

    /**
     * A credential needs a password: `data.needs` names it, `data.unlocked` lists the logins the
     * bridge holds. Send `ide.unlock`, then repeat the request. Also the answer of `ide.unlock`
     * to a wrong password.
     */
    const val NEEDS_PASSWORD: Int = -32010

    /** `ide.attach`: no session matches. */
    const val NO_SESSION: Int = -32011

    /** `ide.attach`: several sessions match (`data.sessions`). */
    const val AMBIGUOUS_SESSION: Int = -32012

    /** `ide.start`: no `calu serve` answered the request. */
    const val NO_SERVER: Int = -32013

    /** `ide.start`: the server refused the request (its message). */
    const val SERVER_REFUSED: Int = -32014

    /** `session.call`: no session is attached. */
    const val NOT_ATTACHED: Int = -32015

    /** `ide.checker.save`: the stored version differs (`data.version`, `data.instructions`). */
    const val CHECKER_CONFLICT: Int = -32016

    /** No usable database credential is stored, or the database cannot be reached. */
    const val NO_DATABASE: Int = -32017

    /** `ide.start`: the editor took the request back with `ide.start.cancel`. */
    const val START_CANCELLED: Int = -32018
}

/**
 * The stored checker that error -32016 reports.
 *
 * @property version The version stored now.
 * @property instructions The instructions stored now.
 */
data class CheckerConflictData(val version: Int, val instructions: String)

/**
 * Read the credential name from the `data` of error -32010.
 *
 * @param data The error's `data` member (any JSON value).
 * @return The non-empty string in `data.needs`, or `null`.
 */
fun neededCredential(data: JsonElement?): String? = data.asObj()?.str("needs")?.takeIf { it.isNotEmpty() }

/**
 * Read the names of the held logins from the `data` of error -32010, from an `ide.unlock` result
 * or from an `ide.status` result.
 *
 * @param data Any JSON value; an object with a member `unlocked` is expected.
 * @return The non-empty strings in `data.unlocked`; empty if the member is missing or no array.
 */
fun unlockedLogins(data: JsonElement?): List<String> =
    data.asObj()?.strings("unlocked")?.filter { it.isNotEmpty() } ?: emptyList()

/**
 * Read the stored checker from the `data` of error -32016.
 *
 * @param data The error's `data` member (any JSON value).
 * @return Version and instructions, or `null` if a member is missing or has another type.
 */
fun checkerConflict(data: JsonElement?): CheckerConflictData? {
    val o = data.asObj() ?: return null
    return CheckerConflictData(o.int("version") ?: return null, o.str("instructions") ?: return null)
}
