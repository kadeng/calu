package io.github.kadeng.calu

import com.intellij.diff.contents.DocumentContent
import com.intellij.diff.util.DiffUserDataKeys
import com.intellij.diff.util.Side
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.testFramework.HeavyPlatformTestCase
import com.intellij.testFramework.PlatformTestUtil
import io.github.kadeng.calu.backend.CHECKER_ORIGIN
import io.github.kadeng.calu.backend.CaluProjectService
import io.github.kadeng.calu.backend.EditorActions
import io.github.kadeng.calu.backend.chooseLauncher
import io.github.kadeng.calu.chat.DiffData
import io.github.kadeng.calu.frontend.CaluFrontendService
import io.github.kadeng.calu.frontend.NativeChatSurface
import io.github.kadeng.calu.frontend.statusText
import io.github.kadeng.calu.remote.PluginSettingsDto
import io.github.kadeng.calu.state.EscalationStatus
import io.github.kadeng.calu.state.ItemKind
import io.github.kadeng.calu.state.LinkStatus
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.TurnItem
import io.github.kadeng.calu.state.openEscalationCount
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.io.File

/**
 * The whole plugin in a headless IDE, all three modules in one process, against the scripted
 * bridge process `shared/src/test/resources/fake_bridge.py`:
 *
 *     NativeChatSurface ── CaluFrontendService ──RPC──► BackendCaluRemoteApi ── ChatController ── fake_bridge.py
 */
class PluginFlowTest : HeavyPlatformTestCase() {
    private val fakeBridge = File("shared/src/test/resources/fake_bridge.py").absolutePath

    /**
     * Pump the EDT until a condition holds.
     *
     * @param what What is waited for, for the failure message.
     * @param condition The condition.
     */
    private fun waitFor(what: String, condition: () -> Boolean) {
        val end = System.currentTimeMillis() + 30_000
        while (!condition()) {
            if (System.currentTimeMillis() > end) fail("timed out waiting for: $what")
            PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
            Thread.sleep(10)
        }
    }

    /**
     * Run a suspending block off the EDT and pump the EDT until it is done (the block may
     * switch to the EDT itself, so the test must not block it).
     *
     * @param block The work.
     * @return The block's result.
     */
    private fun <T> await(block: suspend () -> T): T {
        val done = java.util.concurrent.atomic.AtomicReference<Result<T>?>(null)
        CaluProjectService.of(project).launch { done.set(runCatching { block() }) }
        waitFor("a background call") { done.get() != null }
        return done.get()!!.getOrThrow()
    }

    override fun setUp() {
        super.setUp()
        assertTrue("the scripted bridge is missing: $fakeBridge", File(fakeBridge).isFile)
        File(project.basePath!!).mkdirs()
        CaluProjectService.of(project).applySettings(PluginSettingsDto(mode = "local", caluCommand = "python3 '$fakeBridge'"))
    }

    fun `test a scripted conversation is rendered and its escalation is decided from the card`() {
        val service = CaluFrontendService.of(project)
        service.start()
        waitFor("the attach") { service.state.value.link.status == LinkStatus.ATTACHED }
        val surface = NativeChatSurface(project, service)
        surface.render(service.state.value)
        assertEquals(listOf(MAIN), surface.tabKeys)
        assertTrue(surface.hintTexts.isEmpty())

        surface.inputArea.text = "run the tests"
        service.act("send", buildJsonObject { put("text", JsonPrimitive("run the tests")); put("inject", JsonPrimitive(false)); put("tab", JsonPrimitive(MAIN)) })
        waitFor("the escalation") { openEscalationCount(service.state.value) == 1 }
        surface.render(service.state.value)
        val rows = surface.transcript(MAIN)!!.rows
        assertEquals(
            listOf(ItemKind.USER, ItemKind.ASSISTANT, ItemKind.TOOL, ItemKind.TOOL, ItemKind.ESCALATION),
            rows.map { it.model!!.kind },
        )
        assertEquals("Open diff", rows[2].model!!.tooltip)
        assertEquals("calu: 1 waiting", statusText(openEscalationCount(service.state.value)))

        val card = com.intellij.util.ui.UIUtil.findComponentOfType(rows[4], io.github.kadeng.calu.frontend.EscalationCard::class.java)!!
        assertFalse("no button of a new card has the focus", card.allowButton!!.isFocusOwner)
        card.allowButton!!.doClick()
        waitFor("the end of the turn") { service.state.value.items[MAIN]!!.any { it is TurnItem } }
        surface.render(service.state.value)
        assertEquals(EscalationStatus.ALLOW, service.state.value.escalations.values.single().status)
        assertNull(card.allowButton)
        assertEquals("", statusText(openEscalationCount(service.state.value)))
        surface.dispose()
    }

    fun `test the diff of an edit opens before and after, scrolled to the changed line`() {
        val request = EditorActions(project).diffRequest(DiffData("t", "src/a.py", "x = 1\n", "x = 2\n", 7))
        assertEquals("calu: src/a.py", request.title)
        assertEquals(listOf("x = 1\n", "x = 2\n"), request.contents.map { (it as DocumentContent).document.text })
        val scroll = request.getUserData(DiffUserDataKeys.SCROLL_TO_LINE)!!
        assertEquals(Side.RIGHT, scroll.first)
        assertEquals(6, scroll.second)
    }

    fun `test checker instructions open in an editor and are saved with the version check`() {
        val backend = CaluProjectService.of(project)
        val editor = EditorActions(project)
        waitFor("the bridge") { backend.controller.state.bridge.status == "running" }
        assertTrue(await { editor.editChecker("command-default") })
        PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
        val file = FileEditorManager.getInstance(project).openFiles.single { it.getUserData(CHECKER_ORIGIN) != null }
        assertEquals("command-default.md", file.name)
        assertEquals("Deny what deletes files.", FileDocumentManager.getInstance().getDocument(file)!!.text)
        assertEquals(3, file.getUserData(CHECKER_ORIGIN)!!.version)
        assertTrue(await { editor.saveChecker(file, "Deny what deletes files.\nEscalate force pushes.") })
        assertEquals(4, file.getUserData(CHECKER_ORIGIN)!!.version)
        val stored = await {
            backend.controller.call("ide.checker.get", buildJsonObject { put("name", JsonPrimitive("command-default")) }, session = false)
        }
        assertTrue(stored.toString().contains("Escalate force pushes."))
        FileEditorManager.getInstance(project).closeFile(file)
    }

    fun `test the settings select the launcher`() {
        val base = PluginSettingsDto()
        assertEquals("local", chooseLauncher(base, "/home/me/p", isWindows = false).mode)
        val wsl = chooseLauncher(base, "//wsl.localhost/Ubuntu/home/me/p", isWindows = true)
        assertEquals("wsl", wsl.mode)
        assertEquals("wsl:Ubuntu", wsl.target)
        assertEquals("wsl.exe -d Ubuntu --cd /home/me/p -- calu ide", wsl.launcher.describe())
        assertEquals("local", chooseLauncher(base, "//wsl.localhost/Ubuntu/home/me/p", isWindows = false).mode)
        val ssh = chooseLauncher(base.copy(mode = "ssh", sshHost = "box", sshDirectory = "/srv/p"), "/home/me/copy", isWindows = false)
        assertEquals("ssh:box", ssh.target)
        assertTrue(ssh.launcher.reconnects)
        assertTrue(ssh.launcher.describe().endsWith("box calu ide --cwd /srv/p"))
        assertTrue(chooseLauncher(base.copy(mode = "wsl"), "C:/p", isWindows = true).launcher.describe().contains("needs a project below"))
    }
}
