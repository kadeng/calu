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
import io.github.kadeng.calu.backend.ChoiceDialog
import io.github.kadeng.calu.backend.HeldFilesRunner
import io.github.kadeng.calu.backend.chooseLauncher
import io.github.kadeng.calu.backend.heldFilesOf
import io.github.kadeng.calu.backend.nextRunner
import io.github.kadeng.calu.remote.CALU_DIFF_REF
import io.github.kadeng.calu.sync.SIDE_CANCEL
import io.github.kadeng.calu.sync.heldRunQuestion
import io.github.kadeng.calu.sync.sideChoice
import com.intellij.execution.RunManager
import com.intellij.execution.configurations.ConfigurationType
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.execution.runners.ProgramRunner
import com.intellij.openapi.ui.DialogWrapper
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
        assertEquals("edit:t", request.getUserData(CALU_DIFF_REF))
    }

    fun `test a comment in the diff of an edit finds the edit's ref through the backend`() {
        val backend = CaluProjectService.of(project)
        val file = "${project.basePath}/src/a.py"
        backend.controller.diffs.record(buildJsonObject {
            put("tool_use_id", JsonPrimitive("toolu_9"))
            put("path", JsonPrimitive(file))
            put("before", JsonPrimitive("x = 1\n"))
            put("after", JsonPrimitive("x = 2\n"))
        })
        val found = EditorActions(project).diffOfText("x = 1\n")!!
        assertEquals("src/a.py", (found["path"] as JsonPrimitive).content)
        assertEquals("before", (found["side"] as JsonPrimitive).content)
        assertEquals("edit:toolu_9", (found["ref"] as JsonPrimitive).content)
    }

    fun `test the held-files runner comes first and claims nothing while nothing is held`() {
        val runners = ProgramRunner.PROGRAM_RUNNER_EP.extensionList
        val held = runners.filterIsInstance<HeldFilesRunner>().single()
        assertSame(held, runners.first())
        assertEquals(emptyList<String>(), heldFilesOf(project))
        val factory = ConfigurationType.CONFIGURATION_TYPE_EP.extensionList.flatMap { it.configurationFactories.toList() }.first()
        val settings = RunManager.getInstance(project).createConfiguration("t", factory)
        assertFalse(held.canRun(DefaultRunExecutor.EXECUTOR_ID, settings.configuration))
    }

    fun `test the next runner skips the held-files runner`() {
        val self = "calu"
        assertEquals("python", nextRunner(listOf("calu", "other", "python"), self) { it == "python" })
        assertEquals(null, nextRunner(listOf("calu"), self) { true })
    }

    fun `test the conflict dialog has no default button and starts with Cancel chosen`() {
        val side = ChoiceDialog(project, sideChoice("k_b.txt", "changed on the server, deleted here", mineExists = false, theirsExists = true))
        try {
            assertEquals(listOf("Keep Mine", "Take Theirs", "Cancel"), side.buttonActions.map { it.getValue(javax.swing.Action.NAME) })
            assertTrue(side.buttonActions.none { it.getValue(DialogWrapper.DEFAULT_ACTION) == true })
            assertFalse(side.isOKActionEnabled)
            assertEquals(SIDE_CANCEL, side.chosen)
        } finally {
            side.close(DialogWrapper.CANCEL_EXIT_CODE)
        }
        val run = ChoiceDialog(project, heldRunQuestion(listOf("greeter.py")))
        try {
            assertEquals(listOf(true, null, null), run.buttonActions.map { it.getValue(DialogWrapper.DEFAULT_ACTION) })
        } finally {
            run.close(DialogWrapper.CANCEL_EXIT_CODE)
        }
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
