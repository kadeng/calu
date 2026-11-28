/**
 * Defensive readers for JSON values that come from `calu ide` (untrusted shape).
 *
 * Every reader returns `null` (or the stated default) for a missing member or a member of another
 * type; none throws. The code of this plugin reads the bridge's JSON only through these functions,
 * so a malformed message can change what is shown but cannot raise an exception.
 *
 *     val p: JsonObject = ...
 *     p.str("tool_use_id")      // String? : the member if it is a JSON string
 *     p.int("seq")              // Int?    : the member if it is a JSON number with an integer value
 *     p.bool("is_error")        // true only for the JSON literal `true`
 *     p.obj("snapshot")         // JsonObject? ; p.objOrEmpty("snapshot") never null
 *     p.arr("events")           // List<JsonElement>, empty if missing or not an array
 */
package io.github.kadeng.calu.json

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.longOrNull

/** The JSON codec of the plugin: unknown keys are ignored, defaults are written. */
val CaluJson: Json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    isLenient = false
}

/** The same codec with two-space indentation, for text shown to the user. */
val CaluJsonPretty: Json = Json(CaluJson) {
    prettyPrint = true
    prettyPrintIndent = "  "
}

/** An empty JSON object (shared instance). */
val EMPTY_OBJECT: JsonObject = JsonObject(emptyMap())

/**
 * Read a JSON value as a string.
 *
 * @receiver Any JSON value or `null`.
 * @return The text if the value is a JSON string (not a number, boolean or null), else `null`.
 */
fun JsonElement?.asStr(): String? = (this as? JsonPrimitive)?.takeIf { it.isString }?.content

/**
 * Read a JSON value as a number.
 *
 * @receiver Any JSON value or `null`.
 * @return The value if it is a JSON number (a string holding digits is not one), else `null`.
 */
fun JsonElement?.asNum(): Double? =
    (this as? JsonPrimitive)?.takeIf { !it.isString && it !is JsonNull }?.doubleOrNull

/** 2^63 as a double: a whole double at or above it, or below its negative, does not fit a `Long`. */
private const val LONG_LIMIT: Double = 9.223372036854775807E18

/**
 * Read a JSON value as a whole number.
 *
 * @receiver Any JSON value or `null`.
 * @return The value if it is a JSON number without a fraction that fits a `Long`, else `null`.
 */
fun JsonElement?.asLong(): Long? =
    (this as? JsonPrimitive)?.takeIf { !it.isString && it !is JsonNull }?.let { p ->
        p.longOrNull ?: p.doubleOrNull
            ?.takeIf { it == Math.floor(it) && it >= -LONG_LIMIT && it < LONG_LIMIT }
            ?.toLong()
    }

/**
 * Read a JSON value as a boolean.
 *
 * @receiver Any JSON value or `null`.
 * @return `true` or `false` if the value is that JSON literal, else `null`.
 */
fun JsonElement?.asBool(): Boolean? =
    (this as? JsonPrimitive)?.takeIf { !it.isString && it !is JsonNull }?.booleanOrNull

/**
 * Read a JSON value as an object.
 *
 * @receiver Any JSON value or `null`.
 * @return The value if it is a JSON object (not an array), else `null`.
 */
fun JsonElement?.asObj(): JsonObject? = this as? JsonObject

/**
 * Read a JSON value as a list.
 *
 * @receiver Any JSON value or `null`.
 * @return The elements if the value is a JSON array, else an empty list.
 */
fun JsonElement?.asArr(): List<JsonElement> = (this as? JsonArray) ?: emptyList()

/**
 * Tell whether a JSON value is an array.
 *
 * @receiver Any JSON value or `null`.
 * @return True for a JSON array.
 */
fun JsonElement?.isArr(): Boolean = this is JsonArray

/**
 * Read a string member.
 *
 * @param key The member's name.
 * @return The member's text if it is a JSON string, else `null`.
 */
fun JsonObject.str(key: String): String? = this[key].asStr()

/**
 * Read a number member.
 *
 * @param key The member's name.
 * @return The member if it is a JSON number, else `null`.
 */
fun JsonObject.num(key: String): Double? = this[key].asNum()

/**
 * Read a whole-number member as an `Int`.
 *
 * @param key The member's name.
 * @return The member if it is a JSON number without a fraction inside the `Int` range, else `null`.
 */
fun JsonObject.int(key: String): Int? =
    this[key].asLong()?.takeIf { it in Int.MIN_VALUE..Int.MAX_VALUE }?.toInt()

/**
 * Read a whole-number member as a `Long`.
 *
 * @param key The member's name.
 * @return The member if it is a JSON number without a fraction, else `null`.
 */
fun JsonObject.long(key: String): Long? = this[key].asLong()

/**
 * Read a boolean member strictly.
 *
 * @param key The member's name.
 * @return True only if the member is the JSON literal `true`.
 */
fun JsonObject.bool(key: String): Boolean = this[key].asBool() == true

/**
 * Read an object member.
 *
 * @param key The member's name.
 * @return The member if it is a JSON object, else `null`.
 */
fun JsonObject.obj(key: String): JsonObject? = this[key].asObj()

/**
 * Read an object member, with an empty object for anything else.
 *
 * @param key The member's name.
 * @return The member if it is a JSON object, else `EMPTY_OBJECT`.
 */
fun JsonObject.objOrEmpty(key: String): JsonObject = this[key].asObj() ?: EMPTY_OBJECT

/**
 * Read an array member.
 *
 * @param key The member's name.
 * @return The member's elements if it is a JSON array, else an empty list.
 */
fun JsonObject.arr(key: String): List<JsonElement> = this[key].asArr()

/**
 * Read the string elements of an array member.
 *
 * @param key The member's name.
 * @return The elements that are JSON strings, in order; empty if the member is not an array.
 */
fun JsonObject.strings(key: String): List<String> = arr(key).mapNotNull { it.asStr() }

/**
 * Parse a text as a JSON object.
 *
 * @param text Any text.
 * @return The object, or `null` if the text is not JSON or not a JSON object.
 */
fun parseObjectOrNull(text: String): JsonObject? =
    try {
        CaluJson.parseToJsonElement(text) as? JsonObject
    } catch (_: Exception) {
        null
    }

/**
 * Convert a value to the text JavaScript's `String(value)` gives for it, as the reducer's
 * TypeScript original does for members of unknown type.
 *
 * @receiver Any JSON value or `null` (a missing member).
 * @return The string's text; "undefined" for a missing member, "null" for JSON null; numbers
 *   without a fraction are written without ".0"; objects and arrays as compact JSON.
 */
fun JsonElement?.jsString(): String = when (this) {
    null -> "undefined"
    is JsonNull -> "null"
    is JsonPrimitive -> if (isString) content else asLong()?.toString() ?: content
    else -> toString()
}

/**
 * Tell whether JavaScript's `Boolean(value)` is true for a value, as the reducer's TypeScript
 * original does for members of unknown type (e.g. `is_error`).
 *
 * @receiver Any JSON value or `null` (a missing member).
 * @return False for a missing member, JSON null, `false`, the number 0 and the empty string;
 *   true for every other value (also for the string "false" and for empty objects and arrays).
 */
fun JsonElement?.jsTruthy(): Boolean = when (this) {
    null, is JsonNull -> false
    is JsonPrimitive ->
        if (isString) content.isNotEmpty()
        else booleanOrNull ?: doubleOrNull?.let { it != 0.0 && !it.isNaN() } ?: true
    else -> true
}
