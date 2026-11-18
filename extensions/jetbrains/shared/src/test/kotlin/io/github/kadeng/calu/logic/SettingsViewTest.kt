package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ServerHelp
import io.github.kadeng.calu.state.jo
import io.github.kadeng.calu.state.json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The view logic of the settings dialog (port of `settingsView.test.ts` and of `saveSummary` in `secondRun.test.ts`). */
class SettingsViewTest {
    private val presets = listOf(
        ModelPreset("default", "calu's default", "Default", "", "Stores no model."),
        ModelPreset("anthropic-haiku", "Anthropic · Haiku", "Anthropic", "anthropic:haiku", "key"),
        ModelPreset("bedrock-haiku", "Bedrock · Haiku", "AWS Bedrock", "bedrock:haiku", "aws"),
        ModelPreset("anthropic-sonnet", "Anthropic · Sonnet", "Anthropic", "anthropic:sonnet", "key"),
    )

    private fun roles(vararg models: String) = models.mapIndexed { index, model -> ModelRole("role$index.model", "Role $index", model) }

    @Test
    fun `reads the presets, the roles and Claude Code's own model defensively`() {
        val info = parseModels(
            jo(
                "presets" to listOf(
                    mapOf("id" to "default", "label" to "calu's default", "group" to "Default", "model" to null, "note" to "Stores no model."),
                    mapOf("id" to "x", "label" to "X", "group" to "", "model" to "a:b"), emptyMap<String, Any>(), 3,
                ),
                "roles" to listOf(mapOf("key" to "checkers.model", "label" to "Checkers", "model" to ""), mapOf("label" to "no key")),
                "claude" to mapOf("model" to null, "effort" to "high", "use_bedrock" to true),
                "default_model" to "anthropic:haiku",
            ),
        )
        assertEquals(listOf(ModelPreset("default", "calu's default", "Default", "", "Stores no model."), ModelPreset("x", "X", "", "a:b", "")), info.presets)
        assertEquals(listOf(ModelRole("checkers.model", "Checkers", "")), info.roles)
        assertEquals(listOf(null, "high", true, "anthropic:haiku"), listOf(info.claudeModel, info.claudeEffort, info.useBedrock, info.defaultModel))
        assertEquals(ModelsInfo(), parseModels(null))
        assertEquals("Other", groupPresets(info.presets)[1].group)
    }

    @Test
    fun `groups presets by provider in the order of first appearance`() {
        assertEquals(
            listOf("Default" to listOf("default"), "Anthropic" to listOf("anthropic-haiku", "anthropic-sonnet"), "AWS Bedrock" to listOf("bedrock-haiku")),
            groupPresets(presets).map { g -> g.group to g.presets.map { it.id } },
        )
        assertEquals(emptyList<PresetGroup>(), groupPresets(emptyList()))
    }

    @Test
    fun `tells whether all roles use the same model`() {
        assertEquals(RolesState.None, rolesState(emptyList()))
        assertEquals(RolesState.Uniform(""), rolesState(roles("", "")))
        assertEquals(RolesState.Uniform("a:b"), rolesState(roles("a:b", " a:b ")))
        assertEquals(RolesState.Mixed, rolesState(roles("a:b", "")))
    }

    @Test
    fun `marks the preset every role stores, default when none is stored`() {
        assertEquals("default", selectedPresetId(presets, roles("", "", "")))
        assertEquals("bedrock-haiku", selectedPresetId(presets, roles("bedrock:haiku", "bedrock:haiku")))
        assertNull(selectedPresetId(presets, roles("bedrock:haiku", "anthropic:haiku")))
        assertNull(selectedPresetId(presets, roles("custom:model")))
        assertNull(selectedPresetId(presets, emptyList()))
    }

    @Test
    fun `finds a preset by model and describes a role model`() {
        assertEquals("anthropic-sonnet", presetForModel(presets, "anthropic:sonnet")?.id)
        assertNull(presetForModel(presets, "nope"))
        assertEquals("default → anthropic:haiku", roleModelText("", presets, "anthropic:haiku"))
        assertEquals("default", roleModelText("", presets, ""))
        assertEquals("Bedrock · Haiku", roleModelText("bedrock:haiku", presets, "x"))
        assertEquals("custom:model", roleModelText("custom:model", presets, "x"))
    }

    @Test
    fun `builds the model requests - one model for everything, and one role`() {
        assertEquals(BridgeRequest("ide.models.presets", jo("project" to null)), modelsPresetsRequest(null))
        assertEquals(
            BridgeRequest("ide.models.set_all", jo("model" to "default", "effort" to "high", "project" to null)),
            modelsSetAllRequest(" default ", "high", null),
        )
        assertEquals(
            BridgeRequest("ide.models.set_all", jo("model" to "anthropic:claude-haiku-5-5", "project" to "demo")),
            modelsSetAllRequest("anthropic:claude-haiku-5-5", null, "demo"),
        )
        assertEquals(BridgeRequest("ide.models.set_all", jo("model" to "default", "project" to null)), presetRequest(presets[0], null, null))
        assertEquals(BridgeRequest("ide.models.set_all", jo("model" to "bedrock:haiku", "project" to null)), presetRequest(presets[2], null, ""))
        // An empty model of a role stores none: the role then uses the default model.
        assertEquals(
            BridgeRequest("ide.models.set", jo("role" to "checkers.model", "model" to "", "project" to null)),
            modelsSetRequest("checkers.model", "", null),
        )
        assertEquals(BridgeRequest("ide.models.set", jo("role" to "r", "model" to "m", "project" to "demo")), modelsSetRequest("r", " m ", "demo"))
        assertNull(modelsSetAllRequest("", null, null))
        assertNull(modelsSetAllRequest("m", " ", null))
        assertNull(modelsSetRequest(" ", "m", null))
        assertNull(modelsSetRequest("r", "m".repeat(MAX_SETTING_NAME + 1), null))
    }

    @Test
    fun `splits the search into words without filter words`() {
        assertEquals(listOf("stream", "part"), searchWords("  Stream @modified  PART "))
        assertEquals(emptyList<String>(), searchWords(""))
    }

    @Test
    fun `finds and merges highlight ranges`() {
        assertEquals(listOf(0 until 3, 8 until 11), highlightRanges("Stream partial", "art str"))
        assertEquals(listOf(0 until 4), highlightRanges("aaaa", "aa"))
        assertEquals(listOf(0 until 5, 6 until 11), highlightRanges("model models", "model"))
        assertEquals(listOf(0 until 5), highlightRanges("abcdef", "abc cde"))
        assertEquals(emptyList<IntRange>(), highlightRanges("Label", "@modified"))
        assertEquals(emptyList<IntRange>(), highlightRanges("Label", ""))
    }

    @Test
    fun `adds @modified for the chip once`() {
        assertEquals("model", effectiveSearch(" model ", false))
        assertEquals("model @modified", effectiveSearch("model", true))
        assertEquals("@Modified x", effectiveSearch("@Modified x", true))
        assertEquals("@modified", effectiveSearch("", true))
    }

    @Test
    fun `keeps a card for an empty search or matching words, never for a filter word`() {
        assertTrue(cardMatches("models presets bedrock", ""))
        assertTrue(cardMatches("models presets bedrock", "Bedrock model"))
        assertFalse(cardMatches("models presets bedrock", "sandbox"))
        assertFalse(cardMatches("models presets bedrock", "model @modified"))
    }

    @Test
    fun `splits descriptions into paragraphs`() {
        assertEquals(listOf("one two", "three"), paragraphs("one\ntwo\n\n  three  \n\n\n"))
        assertEquals(emptyList<String>(), paragraphs(""))
    }

    @Test
    fun `summarises the settings one by one`() {
        assertEquals(SaveSummary("", "idle"), saveSummary(0, 0, 0))
        assertEquals(SaveSummary("Saving…", "saving"), saveSummary(2, 1, 1))
        assertEquals(SaveSummary("Saved", "saved"), saveSummary(0, 1, 0))
        assertEquals("Saved", saveSummary(0, 3, 0).text)
        assertEquals(SaveSummary("1 setting not saved", "error"), saveSummary(0, 0, 1))
        assertEquals("2 settings not saved", saveSummary(0, 0, 2).text)
        // One field was refused while another was saved: both are said.
        assertEquals(SaveSummary("1 saved · 1 not saved", "error"), saveSummary(0, 1, 1))
    }

    private val status = jo(
        "findings" to listOf(
            mapOf("name" to "claude", "ok" to false, "level" to "missing", "message" to "not found", "advice" to "install it"),
            mapOf("name" to "uv", "ok" to true, "level" to "ok", "message" to "uv 0.9", "advice" to ""),
            mapOf("name" to "tmux", "ok" to false, "level" to "note", "message" to "not installed", "advice" to ""),
        ),
        "database" to mapOf("reachable" to true, "place" to "localhost/calu", "problem" to ""),
        "server" to mapOf("sessions_running" to 0),
    )
    private val serverHelp = ServerHelp("calu serve", "plain", "", "")

    @Test
    fun `lists findings, database and server, claude never gets an install fix`() {
        val rows = setupRows(status, serverHelp)
        assertEquals(
            listOf("claude" to "missing", "uv" to "ok", "tmux" to "note", "database" to "ok", "server" to "note"),
            rows.map { it.name to it.level },
        )
        assertEquals("Claude Code", rows[0].title)
        assertEquals(listOf(SetupFix("openClaudeDocs", "How to install Claude Code")), rows[0].fixes)
        assertEquals("Reachable (localhost/calu).", rows[3].message)
        assertEquals(listOf("startServer"), rows[4].fixes.map { it.action })
        assertFalse(rows.flatMap { it.fixes }.any { it.action == "installCalu" })
        assertEquals(emptyList<SetupFix>(), setupRows(status, null)[4].fixes)
    }

    @Test
    fun `reports an unreachable database with calu's problem text and a running server as ok`() {
        val changed = JsonObject(
            status + mapOf(
                "database" to jo("reachable" to false, "place" to "db", "problem" to "password needed"),
                "server" to jo("sessions_running" to 2),
            ),
        )
        val rows = setupRows(changed, serverHelp)
        assertEquals(listOf("missing", "password needed"), rows.first { it.name == "database" }.let { listOf(it.level, it.message) })
        val server = rows.first { it.name == "server" }
        assertEquals(listOf("ok", "2 sessions running on this machine.", emptyList<SetupFix>()), listOf(server.level, server.message, server.fixes))
    }

    @Test
    fun `offers install and restart when there is no status`() {
        val rows = setupRows(null, null, listOf("calu: command not found", "Settings: x"))
        assertEquals(1, rows.size)
        assertEquals(listOf("calu", "missing", "command not found"), rows[0].let { listOf(it.name, it.level, it.message) })
        assertEquals(listOf("installCalu", "restartBridge"), rows[0].fixes.map { it.action })
    }

    @Test
    fun `summarises - collapsed when nothing is missing`() {
        val rows = setupRows(status, serverHelp)
        assertEquals(SetupSummary(false, "1 thing needs attention"), setupSummary(rows))
        val fine = rows.filter { it.level != "missing" }
        assertEquals(SetupSummary(true, "Everything is in place (2 notes)"), setupSummary(fine))
        assertEquals(SetupSummary(true, "Everything is in place"), setupSummary(fine.filter { it.level == "ok" }))
    }

    @Test
    fun `splits a description at the choice markers, with or without backticks`() {
        val text = "`off`: nothing of this runs. `track`: the state is kept; a `git commit` is certified. " +
            "`enforce`: additionally, a command needs a clean state. Default `off`."
        assertEquals(
            mapOf(
                "off" to "nothing of this runs.",
                "track" to "the state is kept; a git commit is certified.",
                "enforce" to "additionally, a command needs a clean state.",
            ),
            modeSentences(text, listOf("off", "track", "enforce")),
        )
        assertEquals(mapOf("off" to "a.", "track" to "b."), modeSentences("off: a. track: b.", listOf("off", "track", "enforce")))
        assertEquals(emptyMap<String, String>(), modeSentences("No markers here.", listOf("off")))
    }

    private fun state(value: Any?, isSet: Boolean) = RowState(json(value), isSet)
    private val before = state("old", false)

    @Test
    fun `settles one edit with the value the bridge reports`() {
        val pending = PendingEdits()
        pending.begin("k", before, state("new", true))
        assertTrue(pending.isPending("k"))
        assertEquals(1, pending.size)
        assertEquals(state("new", true), pending.optimistic("k"))
        assertEquals(state("NEW", true), pending.settle("k", state("NEW", true)))
        assertFalse(pending.isPending("k"))
        assertEquals(0, pending.size)
    }

    @Test
    fun `rolls a refused edit back to what was shown before`() {
        val pending = PendingEdits()
        pending.begin("k", before, state("bad", true))
        assertEquals(before, pending.fail("k"))
        assertFalse(pending.isPending("k"))
        assertNull(pending.fail("k"))
    }

    @Test
    fun `keeps the latest optimistic value while a second edit is open`() {
        val pending = PendingEdits()
        pending.begin("k", before, state("a", true))
        pending.begin("k", state("a", true), state("b", true))
        assertEquals(state("b", true), pending.settle("k", state("a", true)))
        assertTrue(pending.isPending("k"))
        // The second edit is refused: back to the value the bridge confirmed for the first.
        assertEquals(state("a", true), pending.fail("k"))
    }

    @Test
    fun `does not roll back while another edit of the key is open, and settles unknown keys as given`() {
        val pending = PendingEdits()
        pending.begin("k", before, state("a", true))
        pending.begin("k", state("a", true), state("b", true))
        assertNull(pending.fail("k"))
        assertEquals(state("b", true), pending.settle("k", state("b", true)))
        assertEquals(state(1, true), pending.settle("other", state(1, true)))
        pending.begin("x", before, before)
        pending.clear()
        assertEquals(0, pending.size)
    }

    @Test
    fun `replaces one row state and keeps the other rows`() {
        val rows = listOf(SettingRow("a", value = JsonPrimitive(1), kind = "int"), SettingRow("b", value = JsonPrimitive(2), kind = "int"))
        val next = withRowState(rows, "b", state(5, true))
        assertSame(rows[0], next[0])
        assertEquals(SettingRow("b", value = JsonPrimitive(5), isSet = true, kind = "int"), next[1])
        assertEquals(JsonPrimitive(2), rows[1].value)
    }

    @Test
    fun `compares JSON values`() {
        assertTrue(sameJson(jo("a" to listOf(1)), jo("a" to listOf(1))))
        assertTrue(sameJson(null, kotlinx.serialization.json.JsonNull))
        assertFalse(sameJson(JsonPrimitive("1"), JsonPrimitive(1)))
    }
}
