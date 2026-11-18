package io.github.kadeng.calu.logic

import io.github.kadeng.calu.logic.ControlKind.JSON
import io.github.kadeng.calu.logic.ControlKind.LIST
import io.github.kadeng.calu.logic.ControlKind.MARKDOWN
import io.github.kadeng.calu.logic.ControlKind.MODEL
import io.github.kadeng.calu.logic.ControlKind.NUMBER
import io.github.kadeng.calu.logic.ControlKind.SELECT
import io.github.kadeng.calu.logic.ControlKind.TEXT
import io.github.kadeng.calu.logic.ControlKind.TOGGLE
import io.github.kadeng.calu.state.ja
import io.github.kadeng.calu.state.jo
import io.github.kadeng.calu.state.json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The form model of calu's settings (port of `settings.test.ts`). */
class SettingsModelTest {
    /** Build one row of `ide.settings.get` as JSON, with the defaults of the TypeScript test. */
    private fun row(key: String, kind: String, vararg extra: Pair<String, Any?>) = jo(
        "key" to key, "type" to "text", "default" to "", "description" to "", "value" to null, "is_set" to false, "kind" to kind,
        "choices" to emptyList<String>(), *extra,
    )

    private val result = jo(
        "schema" to emptyMap<String, Any>(),
        "rows" to listOf(
            row("claude", "section", "description" to "How Claude Code is run."),
            row("claude.model", "text", "type" to "text (or empty)", "default" to "\"opus\"", "value" to "opus", "description" to "Claude Code model alias."),
            row("claude.effort", "choice", "type" to "one of `low`, `high` (or empty)", "choices" to listOf("low", "high"), "is_set" to true, "value" to "high"),
            row("claude.stream_partial", "bool", "type" to "true/false", "value" to true, "default_value" to true),
            row("checkers", "section", "description" to "The checker agents."),
            row("checkers.command", "section", "description" to "nested"),
            row("checkers.command.request_limit", "int", "type" to "whole number", "value" to 8, "default_value" to 8),
            row("checkers.model", "model", "value" to "", "inherits" to "global"),
            row("honesty.rules", "markdown", "value" to "# Rules"),
            row("sandbox.extra_paths", "lines", "value" to listOf("/a", "/b")),
            row("sandbox.env", "json", "value" to mapOf("A" to "1")),
            row("budget_usd", "number", "type" to "number (or empty)", "value" to null, "default_value" to null),
            "not a row", mapOf("kind" to "text"),
        ),
        "checkers" to listOf(
            mapOf("name" to "command-default", "route" to "command", "model" to "", "enabled" to true, "version" to 3, "request_limit" to 8),
            mapOf("route" to "edit"),
        ),
        "projects" to listOf("demo", 7),
    )
    private val rows = parseSettingRows(result)
    private fun view(index: Int) = rowView(rows[index])

    @Test
    fun `reads the rows, the checkers and the projects defensively`() {
        assertEquals(12, rows.size)
        assertEquals(emptyList<SettingRow>(), parseSettingRows(null))
        assertEquals(listOf(CheckerRow("command-default", "command", "", true, 3, 8)), parseCheckers(result))
        assertEquals(listOf("demo"), parseSettingProjects(result))
        assertEquals(JsonPrimitive(8), rows[6].defaultValue)
        assertEquals(JsonNull, rows[11].defaultValue)
        assertNull(rows[1].defaultValue)
    }

    @Test
    fun `labels keys and sections`() {
        assertEquals("claude", sectionOf("claude.model"))
        assertEquals("general", sectionOf("budget_usd"))
        assertEquals("Auto escalate", sectionLabel("auto_escalate"))
        assertEquals("General", sectionLabel("general"))
        assertEquals("Stream partial", labelOf("claude.stream_partial"))
        assertEquals("Command › request limit", labelOf("checkers.command.request_limit"))
        assertEquals("Budget USD", labelOf("budget_usd"))
        assertEquals("Timeout seconds", labelOf("escalation.timeout_s"))
    }

    @Test
    fun `chooses a control per kind`() {
        val kinds = listOf("bool", "choice", "int", "number", "text", "model", "markdown", "lines", "json", "future-kind")
        assertEquals(
            listOf(TOGGLE, SELECT, NUMBER, NUMBER, TEXT, MODEL, MARKDOWN, LIST, JSON, JSON),
            kinds.map { controlKind(it, listOf("a")) },
        )
        assertEquals(TEXT, controlKind("choice", emptyList()))
    }

    @Test
    fun `groups rows by the first key segment, in calu's order, without the section rows`() {
        val sections = buildSections(rows)
        assertEquals(
            listOf(
                "claude" to listOf("claude.model", "claude.effort", "claude.stream_partial"),
                "checkers" to listOf("checkers.command.request_limit", "checkers.model"),
                "honesty" to listOf("honesty.rules"),
                "sandbox" to listOf("sandbox.extra_paths", "sandbox.env"),
                "general" to listOf("budget_usd"),
            ),
            sections.map { s -> s.id to s.rows.map { it.key } },
        )
        assertEquals(listOf("Claude", "How Claude Code is run.", 1), sections[0].let { listOf(it.label, it.description, it.modifiedCount) })
        assertEquals("The checker agents.", sections[1].description)
    }

    @Test
    fun `marks modified rows and where the others get their value`() {
        val effort = view(2)
        assertTrue(effort.modified)
        assertEquals(SELECT, effort.control)
        assertEquals(listOf("", "low", "high"), effort.choices)
        assertTrue(effort.nullable)
        assertEquals("default", effort.inherits)
        assertEquals(JsonPrimitive("high"), effort.value)
        val model = view(7)
        assertEquals(listOf(false, "global", MODEL), listOf(model.modified, model.inherits, model.control))
        assertEquals("\"opus\"", view(1).defaultText)
        assertEquals(listOf(true, false), view(6).let { listOf(it.integer, it.nullable) })
        assertEquals(listOf(false, true), view(11).let { listOf(it.integer, it.nullable) })
        // A default that calu sent as another JSON value than a string is shown as JSON text.
        assertEquals("[\"a\"]", rowView(parseSettingRows(jo("rows" to listOf(row("x.y", "lines", "default" to listOf("a")))))[0]).defaultText)
    }

    @Test
    fun `filters by words in key, label and description, and by @modified`() {
        assertEquals(listOf(listOf("claude.model")), buildSections(rows, "ALIAS").map { s -> s.rows.map { it.key } })
        assertEquals("checkers.command.request_limit", buildSections(rows, "request limit")[0].rows[0].key)
        assertEquals(listOf("claude.effort"), buildSections(rows, "@modified").flatMap { s -> s.rows.map { it.key } })
        assertEquals(1, buildSections(rows, "claude @modified").flatMap { it.rows }.size)
        assertEquals(emptyList<SettingsSection>(), buildSections(rows, "zzz"))
        assertTrue(matchesSearch(view(1), "   "))
    }

    @Test
    fun `formats values for their control`() {
        assertEquals("true", formatControlValue(TOGGLE, JsonPrimitive(true)))
        assertEquals("false", formatControlValue(TOGGLE, JsonNull))
        assertEquals("/a\n/b", formatControlValue(LIST, ja("/a", "/b")))
        assertEquals("{\n  \"A\": \"1\"\n}", formatControlValue(JSON, jo("A" to "1")))
        assertEquals("null", formatControlValue(JSON, JsonNull))
        assertEquals("", formatControlValue(JSON, null))
        assertEquals("", formatControlValue(TEXT, JsonNull))
        assertEquals("8", formatControlValue(NUMBER, JsonPrimitive(8)))
        assertEquals("opus", formatControlValue(MODEL, JsonPrimitive("opus")))
    }

    private fun ok(value: Any?) = ParsedValue.Ok(json(value))

    @Test
    fun `parses what the user entered`() {
        assertEquals(ok(true), parseControlValue(view(3), true))
        assertEquals(ok(false), parseControlValue(view(3), "false"))
        assertEquals(ok(12), parseControlValue(view(6), " 12 "))
        assertEquals(ParsedValue.Error("Enter a whole number."), parseControlValue(view(6), "1.5"))
        assertEquals(ParsedValue.Error("Enter a number."), parseControlValue(view(6), ""))
        assertEquals(ParsedValue.Error("\"many\" is not a number."), parseControlValue(view(6), "many"))
        assertTrue(parseControlValue(view(6), "NaN") is ParsedValue.Error)
        assertTrue(parseControlValue(view(6), "12d") is ParsedValue.Error)
        assertEquals(ok(2.5), parseControlValue(view(11), "2.5"))
        assertEquals(ok(3), parseControlValue(view(11), "3.0"))
        assertEquals(ok(null), parseControlValue(view(11), ""))
        assertEquals(ok("high"), parseControlValue(view(2), "high"))
        assertEquals(ok(null), parseControlValue(view(2), ""))
        assertEquals(ParsedValue.Error("Choose one of: low, high."), parseControlValue(view(2), "extreme"))
        assertEquals(ok(listOf("/a", "/b")), parseControlValue(view(9), " /a \n\n/b\n"))
        assertEquals(ok(emptyList<String>()), parseControlValue(view(9), ""))
        assertEquals(ok(mapOf("B" to 2)), parseControlValue(view(10), "{\"B\": 2}"))
        assertTrue((parseControlValue(view(10), "{B: 2}") as ParsedValue.Error).message.startsWith("Not valid JSON"))
        assertEquals(ParsedValue.Error("Enter a JSON value."), parseControlValue(view(10), " "))
        assertEquals(ok("# New\n"), parseControlValue(view(8), "# New\n"))
        assertEquals(ok("sonnet"), parseControlValue(view(1), " sonnet "))
        assertEquals(ok(null), parseControlValue(view(1), ""))
    }

    @Test
    fun `turns an edit into the request that stores it, or into a message for the row`() {
        assertEquals(
            SettingEdit.Send(BridgeRequest("ide.settings.set", jo("key" to "checkers.command.request_limit", "value" to 12, "project" to null)), JsonPrimitive(12)),
            settingEdit(view(6), "12", null),
        )
        assertEquals(
            SettingEdit.Send(BridgeRequest("ide.settings.set", jo("key" to "claude.stream_partial", "value" to false, "project" to "demo")), JsonPrimitive(false)),
            settingEdit(view(3), false, "demo"),
        )
        assertEquals(
            BridgeRequest("ide.settings.set", jo("key" to "claude.effort", "value" to null, "project" to null)),
            (settingEdit(view(2), "", "") as SettingEdit.Send).request,
        )
        assertEquals(SettingEdit.Refused("Enter a whole number."), settingEdit(view(6), "1.5", null))
        assertEquals(SettingEdit.Refused("The value is too large to store."), settingEdit(view(8), "x".repeat(MAX_VALUE_JSON), null))
    }

    @Test
    fun `builds the requests of the dialog and refuses unusable names`() {
        assertEquals(BridgeRequest("ide.settings.get", jo("project" to null)), settingsGetRequest(null))
        assertEquals(BridgeRequest("ide.settings.get", jo("project" to "demo")), settingsGetRequest("demo"))
        assertEquals(BridgeRequest("ide.settings.unset", jo("key" to "a.b", "project" to null)), settingsUnsetRequest("a.b", ""))
        assertEquals(BridgeRequest("ide.settings.unset", jo("key" to "a.b", "project" to "demo")), settingsUnsetRequest("a.b", "demo"))
        assertEquals(
            BridgeRequest("ide.settings.set", jo("key" to "a.b", "value" to mapOf("x" to listOf(1)), "project" to "demo")),
            settingsSetRequest("a.b", jo("x" to listOf(1)), "demo"),
        )
        assertNull(settingsSetRequest("", JsonPrimitive(1), null))
        assertNull(settingsSetRequest("a.b", JsonPrimitive(1), "x".repeat(MAX_SETTING_NAME + 1)))
        assertNull(settingsUnsetRequest(" ", null))
    }

    @Test
    fun `turns findings into setup items, claude gets a link, never an install`() {
        val items = setupItems(
            ja(
                mapOf("name" to "claude", "ok" to false, "level" to "missing", "message" to "not found", "advice" to "install it"),
                mapOf("name" to "git", "ok" to true, "level" to "ok", "message" to "git 2.43", "advice" to ""),
                mapOf("name" to "tmux", "ok" to false, "level" to "note", "message" to "not installed", "advice" to ""),
                mapOf("name" to "calu", "ok" to false, "level" to "note", "message" to "old", "advice" to ""),
                mapOf("bogus" to true),
            ),
        )
        assertEquals(
            listOf(
                SetupItem("claude", "missing", "not found", "install it", SetupFix("openClaudeDocs", "How to install Claude Code")),
                SetupItem("git", "ok", "git 2.43", ""),
                SetupItem("tmux", "note", "not installed", ""),
                SetupItem("calu", "note", "old", "", SetupFix("updateCalu", "Update calu")),
            ),
            items,
        )
        assertEquals(emptyList<SetupItem>(), setupItems(null))
        assertFalse(items.any { it.fix?.action == "installCalu" })
        assertTrue(FIX_ACTIONS.containsAll(items.mapNotNull { it.fix?.action }))
    }
}
