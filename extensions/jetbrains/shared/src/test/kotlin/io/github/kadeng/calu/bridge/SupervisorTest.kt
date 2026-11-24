package io.github.kadeng.calu.bridge

import io.github.kadeng.calu.json.EMPTY_OBJECT
import io.github.kadeng.calu.json.asObj
import io.github.kadeng.calu.json.str
import io.github.kadeng.calu.logic.ResolveResult
import io.github.kadeng.calu.rpc.RpcRequestException
import io.github.kadeng.calu.state.BridgeStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.fail
import org.junit.jupiter.api.Test
import java.nio.file.Files
import java.util.concurrent.CopyOnWriteArrayList

/** The supervisor against scripted bridges: in this JVM, and as real child processes. */
class SupervisorTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val supervisors = ArrayList<BridgeSupervisor>()
    private val tmp = Files.createTempDirectory("calu-sup").toFile().absolutePath

    @AfterEach
    fun cleanup() {
        supervisors.forEach { it.dispose() }
        scope.cancel()
    }

    /** A password prompt that answers from a list and records the text it would show. */
    private class ListPrompt(vararg answers: String?) : PasswordPrompt {
        val answers = ArrayDeque(answers.toList())
        val asked = CopyOnWriteArrayList<String>()
        override suspend fun ask(problem: String?, left: Int?): String? {
            asked.add(io.github.kadeng.calu.logic.promptText(problem, left))
            return if (answers.isEmpty()) null else answers.removeFirst()
        }
    }

    private fun supervisor(launcher: BridgeLauncher, prompt: PasswordPrompt = ListPrompt(), memory: PasswordMemory = PasswordMemory()) =
        BridgeSupervisor(launcher, scope, "test", prompt, memory, delayMs = { 20 }).also { supervisors.add(it) }

    /** A scripted bridge whose database requests need the password "secret". */
    private fun lockedHandler(): ScriptedHandler {
        var unlocked = false
        return ScriptedHandler { _, method, params ->
            when (method) {
                "ide.initialize" -> buildJsonObject { put("bridge_version", JsonPrimitive("9.9")); put("protocol_version", JsonPrimitive(1)) }
                "ide.unlock" -> if (params.str("password") == "secret") {
                    unlocked = true
                    buildJsonObject { put("unlocked", JsonArray(listOf(JsonPrimitive("user")))); put("reading", JsonPrimitive(true)); put("changing", JsonPrimitive(true)) }
                } else {
                    throw ScriptedError(-32010, "That is not the calu user password.")
                }
                "ide.sessions" -> if (unlocked) buildJsonObject { put("running", JsonArray(emptyList())) } else {
                    throw ScriptedError(-32010, "calu needs the calu user password", buildJsonObject { put("needs", JsonPrimitive("user")) })
                }
                else -> EMPTY_OBJECT
            }
        }
    }

    @Test
    fun `local launcher - a real child process initializes, answers and stops`() = runBlocking {
        val s = supervisor(fakeLocalLauncher(tmp))
        val init = s.ensureStarted()
        assertEquals("0.0-fake", init.str("bridge_version"))
        assertEquals(BridgeStatus.RUNNING, s.status.value.status)
        assertEquals("0.0-fake", s.status.value.version)
        val status = s.request("ide.status").asObj()!!
        assertEquals("fake", status["database"].asObj()!!.str("place"))
        s.stop()
        assertEquals(BridgeStatus.STOPPED, s.status.value.status)
    }

    @Test
    fun `local launcher - calu not found gives the status missing with what to do`() = runBlocking {
        val s = supervisor(LocalLauncher({ ResolveResult.NotFound("calu was not found on this machine.") }, tmp))
        try {
            s.ensureStarted()
            fail("expected a LaunchException")
        } catch (error: LaunchException) {
            assertTrue(error.missing)
        }
        assertEquals(BridgeStatus.MISSING, s.status.value.status)
        assertTrue(s.status.value.message!!.contains("not found"))
    }

    @Test
    fun `local launcher - an unexpected exit is reported and pending requests fail`() = runBlocking {
        val s = supervisor(fakeLocalLauncher(tmp, "--exit-after", "2"))
        s.ensureStarted()
        s.request("ide.status")
        waitUntil("status exited") { s.status.value.status == BridgeStatus.EXITED }
        assertTrue(s.status.value.message!!.contains("exited with code 3"))
        try {
            s.request("ide.status")
            fail("expected a failure")
        } catch (_: IllegalStateException) {
        }
    }

    @Test
    fun `ssh launcher - runs the ssh client with the batch options and speaks to the remote bridge`() = runBlocking {
        val record = java.io.File(tmp, "args.txt")
        // A stand-in for the OpenSSH client: records its arguments and runs the last one (the
        // remote command) with the scripted bridge in place of `calu`.
        val ssh = script(
            "ssh",
            "printf '%s\\n' \"\$@\" > '${record.absolutePath}'\n" +
                "for last; do :; done\n" +
                "calu() { exec python3 '${fakeBridgePath()}' \"\$@\"; }\n" +
                "eval \"\$last\"",
        )
        val s = supervisor(SshLauncher(SshSettings("build-box", "/srv/my project", sshExecutable = ssh)))
        val init = s.ensureStarted()
        assertEquals("/srv/my project", init.str("workspace"))
        val args = record.readLines()
        assertEquals(listOf("-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "build-box"), args.take(10))
        s.stop()
    }

    @Test
    fun `ssh launcher - a key that needs a passphrase gives a clear message, no hang and no retry`() = runBlocking {
        val ssh = script("ssh", "echo 'me@box: Permission denied (publickey).' >&2\nexit 255")
        val s = supervisor(SshLauncher(SshSettings("box", "/p", sshExecutable = ssh)))
        try {
            s.ensureStarted()
            fail("expected a LaunchException")
        } catch (error: LaunchException) {
            assertTrue(error.message!!.contains("add the key to your SSH agent"), error.message)
        }
        assertEquals(BridgeStatus.ERROR, s.status.value.status)
        assertTrue(s.status.value.message!!.contains("SSH agent"))
    }

    @Test
    fun `ssh launcher - a dropped connection is started again with a wait`() = runBlocking {
        val counter = java.io.File(tmp, "count")
        counter.writeText("")
        // The first connection ends after three answers; later ones stay.
        val ssh = script(
            "ssh",
            "echo x >> '${counter.absolutePath}'\n" +
                "if [ \$(wc -l < '${counter.absolutePath}') -le 1 ]; then echo 'client_loop: send disconnect: Broken pipe' >&2; exec python3 '${fakeBridgePath()}' ide --exit-after 2; fi\n" +
                "exec python3 '${fakeBridgePath()}' ide",
        )
        val s = supervisor(SshLauncher(SshSettings("box", "/p", sshExecutable = ssh)))
        s.ensureStarted()
        s.request("ide.status")
        waitUntil("second connection") { counter.readLines().size >= 2 && s.status.value.status == BridgeStatus.RUNNING }
        assertEquals("fake", s.request("ide.status").asObj()!!["database"].asObj()!!.str("place"))
    }

    @Test
    fun `wsl launcher - runs the wsl program with distribution and directory`() = runBlocking {
        val record = java.io.File(tmp, "wsl.txt")
        val wsl = script("wsl.exe", "printf '%s\\n' \"\$@\" > '${record.absolutePath}'\nexec python3 '${fakeBridgePath()}' ide --cwd \"\$4\"")
        val s = supervisor(WslLauncher("Ubuntu", "/home/me/p", emptyList(), wsl))
        assertEquals("/home/me/p", s.ensureStarted().str("workspace"))
        assertEquals(listOf("-d", "Ubuntu", "--cd", "/home/me/p", "--", "calu", "ide"), record.readLines())
        s.stop()
    }

    @Test
    fun `one password - asked once for concurrent requests, wrong entries are asked again`() = runBlocking {
        val prompt = ListPrompt("wrong", "secret")
        val s = supervisor(ScriptedLauncher { lockedHandler() }, prompt)
        s.ensureStarted()
        val a = scope.async { s.request("ide.sessions") }
        val b = scope.async { s.request("ide.sessions") }
        a.await(); b.await()
        assertEquals(2, prompt.asked.size)
        assertEquals("calu user password (asked once)", prompt.asked[0])
        assertEquals("That is not the calu user password. (2 tries left) — calu user password (asked once)", prompt.asked[1])
        assertFalse(s.locked)
    }

    @Test
    fun `one password - a background request never prompts, and cancel stops the asking`() = runBlocking {
        val prompt = ListPrompt(null)
        val s = supervisor(ScriptedLauncher { lockedHandler() }, prompt)
        s.ensureStarted()
        try {
            s.request("ide.sessions", interactive = false)
            fail("expected -32010")
        } catch (error: RpcRequestException) {
            assertEquals(-32010, error.code)
        }
        assertTrue(prompt.asked.isEmpty())
        assertTrue(s.locked)
        try {
            s.request("ide.sessions")
            fail("expected -32010 after the cancel")
        } catch (error: RpcRequestException) {
            assertEquals(-32010, error.code)
            assertEquals("Not done: no password was given. The next action that needs the database asks again.", error.bridgeMessage)
        }
        assertEquals(1, prompt.asked.size)
    }

    @Test
    fun `one password - three refused passwords fail the action with what to do next`() = runBlocking {
        val prompt = ListPrompt("a", "b", "c")
        val s = supervisor(ScriptedLauncher { lockedHandler() }, prompt)
        s.ensureStarted()
        try {
            s.request("ide.sessions")
            fail("expected -32010")
        } catch (error: RpcRequestException) {
            assertEquals(-32010, error.code)
            assertEquals("Not done: the password was refused 3 times. The next action that needs the database asks again.", error.bridgeMessage)
        }
        assertEquals("That is not the calu user password. (1 try left) — calu user password (asked once)", prompt.asked[2])
        assertEquals(3, prompt.asked.size)
    }

    @Test
    fun `one password - a restarted bridge is unlocked from memory without a prompt`() = runBlocking {
        val prompt = ListPrompt("secret")
        val memory = PasswordMemory()
        val launcher = ScriptedLauncher { lockedHandler() }
        val s = supervisor(launcher, prompt, memory)
        s.ensureStarted()
        s.request("ide.sessions")
        assertEquals(1, prompt.asked.size)
        s.restart()
        assertEquals(2, launcher.launched.size)
        s.request("ide.sessions", interactive = false)
        assertEquals(1, prompt.asked.size)
    }

    @Test
    fun `one password - an empty password is sent to the bridge like any other`() = runBlocking {
        val seen = CopyOnWriteArrayList<String?>()
        val handler = ScriptedHandler { _, method, params ->
            when (method) {
                "ide.unlock" -> { seen.add(params.str("password")); buildJsonObject { put("unlocked", JsonArray(listOf(JsonPrimitive("user")))); put("reading", JsonPrimitive(true)); put("changing", JsonPrimitive(true)) } }
                "ide.sessions" -> if (seen.isEmpty()) throw ScriptedError(-32010, "needs a password") else EMPTY_OBJECT
                else -> EMPTY_OBJECT
            }
        }
        val s = supervisor(ScriptedLauncher { handler }, ListPrompt(""))
        s.ensureStarted()
        s.request("ide.sessions")
        assertEquals(listOf<String?>(""), seen)
    }

    @Test
    fun `a refused password in memory is forgotten`() = runBlocking {
        val memory = PasswordMemory().apply { password = "old" }
        val s = supervisor(ScriptedLauncher { lockedHandler() }, ListPrompt(), memory)
        s.ensureStarted()
        assertNull(memory.password)
        assertTrue(s.locked)
    }

    @Test
    fun `notifications arrive in order`() = runBlocking {
        val got = CopyOnWriteArrayList<String>()
        val launcher = ScriptedLauncher {
            ScriptedHandler { bridge, method, _ ->
                if (method == "go") repeat(50) { i -> bridge.notify("n", buildJsonObject { put("i", JsonPrimitive(i)) }) }
                EMPTY_OBJECT
            }
        }
        val s = supervisor(launcher)
        s.onNotification = { got.add(it.params["i"].toString()) }
        s.ensureStarted()
        s.request("go")
        waitUntil("50 notifications") { got.size == 50 }
        assertEquals((0 until 50).map { it.toString() }, got.toList())
    }
}
