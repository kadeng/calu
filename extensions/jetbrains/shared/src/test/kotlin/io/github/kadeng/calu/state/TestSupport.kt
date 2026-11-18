/**
 * Builders for the JSON values and reducer events of the tests in this package.
 *
 *     jo("a" to 1, "b" to listOf("x", null))  ->  {"a":1,"b":["x",null]}
 *     ev("tool.use", "tool_use_id" to "t1")   ->  ViewEvent("tool.use", {"tool_use_id":"t1"})
 */
package io.github.kadeng.calu.state

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Convert a Kotlin value to a JSON value.
 *
 * @param v `null`, a `JsonElement`, a `String`, `Number` or `Boolean`, a `ViewEvent` (written as
 *   `{method, params}`), a `Map` with string keys, or an `Iterable`; nested values likewise.
 * @return The JSON value.
 * @throws IllegalArgumentException for a value of another type.
 */
fun json(v: Any?): JsonElement = when (v) {
    null -> JsonNull
    is JsonElement -> v
    is String -> JsonPrimitive(v)
    is Number -> JsonPrimitive(v)
    is Boolean -> JsonPrimitive(v)
    is ViewEvent -> jo("method" to v.method, "params" to v.params)
    is Map<*, *> -> JsonObject(v.entries.associateTo(LinkedHashMap()) { (k, x) -> k as String to json(x) })
    is Iterable<*> -> JsonArray(v.map { json(it) })
    else -> throw IllegalArgumentException("no JSON form for ${v::class}")
}

/**
 * Build a JSON object.
 *
 * @param pairs The members in order; the values are converted with `json`.
 * @return The object.
 */
fun jo(vararg pairs: Pair<String, Any?>): JsonObject =
    JsonObject(pairs.associateTo(LinkedHashMap()) { (k, v) -> k to json(v) })

/**
 * Build a JSON array.
 *
 * @param items The elements in order; converted with `json`.
 * @return The array.
 */
fun ja(vararg items: Any?): JsonArray = JsonArray(items.map { json(it) })

/**
 * Shorthand for a reducer event.
 *
 * @param method The event's method.
 * @param pairs The members of the params; the values are converted with `json`.
 * @return The event.
 */
fun ev(method: String, vararg pairs: Pair<String, Any?>): ViewEvent = ViewEvent(method, jo(*pairs))

/**
 * Return a copy of an event with more params.
 *
 * @param event The event (not modified).
 * @param pairs Members that are added to the params or replace members of the same name.
 * @return The new event.
 */
fun withParams(event: ViewEvent, vararg pairs: Pair<String, Any?>): ViewEvent =
    ViewEvent(event.method, JsonObject(event.params + jo(*pairs)))

/**
 * Return the kinds of the items of a list.
 *
 * @param state The state.
 * @param key The message list key.
 * @return `Item.kind` of every item of `items[key]`, in order; empty if the list is missing.
 */
fun kinds(state: ViewState, key: String = MAIN): List<String> = state.items[key].orEmpty().map { it.kind }

/**
 * Return a message list that must exist.
 *
 * @param state The state.
 * @param key The message list key.
 * @return `items[key]`.
 * @throws IllegalStateException if the state has no such list.
 */
fun list(state: ViewState, key: String = MAIN): List<Item> = state.items[key] ?: error("no list $key")
