package io.github.kadeng.calu.logic

import io.github.kadeng.calu.json.CaluJson
import kotlinx.serialization.json.JsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests of the session chooser's rows and of the setup hints, ported from
 * `extensions/vscode/test/unit/sessionPick.test.ts` and from the cases of `secondRun.test.ts`
 * that use only `buildHints` and `derivedSessionName`.
 */
class SessionPickTest {
    /**
     * Parse a JSON object.
     *
     * @param text The JSON text of an object.
     * @return The object.
     */
    private fun obj(text: String): JsonObject = CaluJson.parseToJsonElement(text) as JsonObject

    /** An `ide.sessions` result: one session elsewhere, one in the workspace, two stored conversations. */
    private val sessions = obj(
        """
        {
          "running": [
            {"id": "aaaaaaaa-1", "name": "", "directory": "/other/proj", "state": "idle", "claude_session_id": "c0",
             "open_escalations": 0, "started_at": "", "stale": true, "here": false},
            {"id": "bbbbbbbb-2", "name": "refactor", "directory": "/w", "state": "escalated", "claude_session_id": "c1",
             "open_escalations": 2, "started_at": "", "stale": false, "here": true}
          ],
          "stored": [
            {"claude_session_id": "c1", "last_input": "in use", "turns": 3, "age_s": 10, "running": true, "outside": false},
            {"claude_session_id": "c2", "last_input": "Fix the\n  flaky test", "turns": 1, "age_s": 7200, "running": false,
             "outside": true}
          ]
        }
        """,
    )

    /**
     * Build an `ide.status` result.
     *
     * @param findings JSON text of the `findings` array.
     * @param database JSON text of the `database` object.
     * @return The status with these members and no running sessions.
     */
    private fun status(
        findings: String = """
            [{"name": "claude", "ok": true, "level": "ok", "message": "claude 2.1.80", "advice": ""},
             {"name": "tmux", "ok": true, "level": "ok", "message": "tmux 3.4", "advice": ""},
             {"name": "screen", "ok": false, "level": "note", "message": "not installed", "advice": ""}]
        """,
        database: String = """{"reachable": true, "place": "local", "problem": ""}""",
    ): JsonObject = obj("""{"findings": $findings, "database": $database, "server": {"sessions_running": 0}}""")

    /**
     * Build a `findings` array that reports a missing `claude`.
     *
     * @param message The finding's message (without characters that JSON escapes).
     * @return The JSON text of the array.
     */
    private fun claudeMissing(message: String): String =
        """[{"name": "claude", "ok": false, "level": "missing", "message": "$message", "advice": ""}]"""

    // ------------------------------------------------------------------ buildSessionPicks

    @Test
    fun `lists new, workspace sessions, other sessions, then stored conversations`() {
        val picks = buildSessionPicks(sessions, "bbbbbbbb-2")
        assertEquals(
            listOf(
                "New session…",
                "--Running in this workspace",
                "refactor",
                "--Other running sessions",
                "proj",
                "--Stored conversations (resume or fork)",
                "Fix the flaky test",
            ),
            picks.map {
                when (it) {
                    is SessionPick.Separator -> "--${it.label}"
                    is SessionPick.Entry -> it.label
                }
            },
        )
        assertEquals(SessionChoice.New, (picks[0] as SessionPick.Entry).choice)
        val here = picks[2] as SessionPick.Entry
        assertEquals("escalated · 2 waiting for a decision · attached", here.description)
        assertEquals("/w · bbbbbbbb", here.detail)
        assertEquals(SessionChoice.Attach("bbbbbbbb-2"), here.choice)
        assertTrue(here.current)
        val other = picks[4] as SessionPick.Entry
        assertTrue(other.description.contains("stale"))
        assertFalse(other.current)
        val stored = picks[6] as SessionPick.Entry
        assertEquals("1 turn · 2 h ago", stored.description)
        assertEquals("c2 · started without calu", stored.detail)
        assertEquals(SessionChoice.Stored("c2"), stored.choice)
    }

    @Test
    fun `offers only a new session when nothing is listed`() {
        assertEquals(1, buildSessionPicks(obj("{}"), null).size)
        assertEquals(1, buildSessionPicks(obj("""{"running": "x", "stored": null}"""), null).size)
    }

    @Test
    fun `skips malformed entries and tolerates missing members`() {
        // Not in the TypeScript tests: the result is untrusted JSON.
        val picks = buildSessionPicks(
            obj("""{"running": [null, 3, {"id": 7}, {"id": "cccccccc-3"}], "stored": [{"claude_session_id": "c9"}, "x"]}"""),
            null,
        )
        assertEquals(5, picks.size)
        val running = picks[2] as SessionPick.Entry
        assertEquals("cccccccc", running.label)
        assertEquals("", running.description)
        val stored = picks[4] as SessionPick.Entry
        assertEquals("(no user input)", stored.label)
        assertEquals("0 turns · just now", stored.description)
    }

    // ------------------------------------------------------------------ texts

    @Test
    fun `formats ages and one-line texts`() {
        assertEquals("just now", formatAge(-3))
        assertEquals("just now", formatAge(59))
        assertEquals("1 min ago", formatAge(60))
        assertEquals("2 d ago", formatAge(86400L * 2 + 5))
        assertEquals("a b", oneLine(" a\n\tb "))
        assertEquals("xxxxxxxxx…", oneLine("x".repeat(200), 10))
    }

    @Test
    fun `limits session names to 80 characters`() {
        assertNull(validateSessionName(""))
        assertNull(validateSessionName(" ${"n".repeat(80)} "))
        assertTrue(validateSessionName("n".repeat(81))!!.contains("80"))
    }

    // ------------------------------------------------------------------ derivedSessionName (secondRun.test.ts)

    @Test
    fun `names a fork and a rewind after the session they are cut from`() {
        assertEquals("fork of parser work", derivedSessionName("fork", "parser work"))
        assertEquals("rewind of demo", derivedSessionName("rewind", " demo "))
        assertEquals("", derivedSessionName("fork", "  "))
    }

    @Test
    fun `stays within the limit of a session name`() {
        val name = derivedSessionName("rewind", "x".repeat(200))
        assertEquals(MAX_SESSION_NAME, name.length)
        assertTrue(name.endsWith("…"))
    }

    // ------------------------------------------------------------------ buildHints

    @Test
    fun `shows only the install hint while calu is missing`() {
        val hints = buildHints(HintInput(caluMissing = true, status = null, attached = false, noServer = true))
        assertEquals(listOf("calu-missing" to "openSettings"), hints.map { it.id to it.action })
        assertEquals(
            "calu is not installed on this machine. Install it with uv (`uv tool install`), see the plugin's " +
                "README, or set the calu command in Settings | Tools | calu.",
            hints[0].message,
        )
        assertEquals("Open settings", hints[0].actionLabel)
    }

    @Test
    fun `names Claude Code in the hint about a missing claude, with the finding as the detail`() {
        val missing = status(findings = claudeMissing("not on PATH"))
        val hint = buildHints(HintInput(caluMissing = false, status = missing, attached = true, noServer = false))[0]
        assertEquals("claude-missing", hint.id)
        assertEquals("openLink", hint.action)
        assertTrue(
            hint.message.startsWith(
                "Claude Code (`claude`) was not found on this machine (not on PATH). The plugin does not install",
            ),
        )
    }

    @Test
    fun `gives the hint about a missing claude a short text and a short action`() {
        // From secondRun.test.ts ("hints while a session is attached"): the part that needs only buildHints.
        val missing = status(findings = claudeMissing("not on PATH"), database = """{"reachable": true, "place": "", "problem": ""}""")
        val hints = buildHints(HintInput(caluMissing = false, status = missing, attached = true, noServer = false))
        assertEquals(1, hints.size)
        assertEquals("How to install Claude Code", hints[0].actionLabel)
        assertEquals("Claude Code not found", hints[0].short)
        assertEquals("Guide", hints[0].actionShort)
    }

    @Test
    fun `offers a new session, and the server command when no server answered`() {
        assertEquals(
            listOf("no-session"),
            buildHints(HintInput(caluMissing = false, status = status(), attached = false, noServer = false)).map { it.id },
        )
        assertEquals(
            listOf("no-server" to "startServer", "no-session" to "newSession"),
            buildHints(HintInput(caluMissing = false, status = status(), attached = false, noServer = true))
                .map { it.id to it.action },
        )
        assertEquals(
            emptyList<Any>(),
            buildHints(HintInput(caluMissing = false, status = status(), attached = true, noServer = true)),
        )
    }

    @Test
    fun `reports a missing claude with a link, never with an install action`() {
        val missing = status(findings = claudeMissing("claude was not found."))
        val hint = buildHints(HintInput(caluMissing = false, status = missing, attached = true, noServer = false))[0]
        assertEquals("claude-missing", hint.id)
        assertEquals("openLink", hint.action)
        assertEquals("https://docs.anthropic.com/en/docs/claude-code", hint.href)
        assertTrue(hint.message.contains("does not install Claude Code"))
    }

    @Test
    fun `reports an unreachable database`() {
        val down = status(database = """{"reachable": false, "place": "local", "problem": "connection refused"}""")
        val hint = buildHints(HintInput(caluMissing = false, status = down, attached = true, noServer = false))[0]
        assertEquals("database", hint.id)
        assertEquals("openSettings", hint.action)
        assertEquals("calu's database is not reachable: connection refused.", hint.message)
    }

    @Test
    fun `asks for the password instead of reporting the database while locked`() {
        // Not in the TypeScript tests: the `locked` input.
        val down = status(database = """{"reachable": false, "place": "local", "problem": ""}""")
        val locked = buildHints(HintInput(caluMissing = false, status = down, attached = true, noServer = false, locked = true))
        assertEquals(listOf("locked" to "unlock"), locked.map { it.id to it.action })
        val open = buildHints(HintInput(caluMissing = false, status = down, attached = true, noServer = false))
        assertEquals("calu's database is not reachable: unknown problem.", open[0].message)
        val malformed = buildHints(HintInput(caluMissing = false, status = obj("""{"findings": 3}"""), attached = true, noServer = false))
        assertEquals(emptyList<Any>(), malformed)
    }

    @Test
    fun `reads tmux and screen from the findings`() {
        assertTrue(findingOk(status(), "tmux"))
        assertFalse(findingOk(status(), "screen"))
        assertFalse(findingOk(status(), "absent"))
        assertFalse(findingOk(null, "tmux"))
    }
}
