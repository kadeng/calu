package io.github.kadeng.calu.bridge

import io.github.kadeng.calu.logic.ResolveResult
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import java.io.File
import java.nio.file.Files

/** Longest time a test waits for an asynchronous state before it fails. */
const val WAIT_MS: Long = 15_000

/**
 * Wait until a condition holds.
 *
 * @param what What is waited for, for the failure message.
 * @param condition Checked every 10 ms.
 * @throws kotlinx.coroutines.TimeoutCancellationException after `WAIT_MS`.
 */
suspend fun waitUntil(what: String, condition: () -> Boolean) {
    try {
        withTimeout(WAIT_MS) { while (!condition()) delay(10) }
    } catch (error: kotlinx.coroutines.TimeoutCancellationException) {
        throw AssertionError("timed out waiting for: $what")
    }
}

/**
 * Locate the scripted bridge of the test resources.
 *
 * @return The absolute path of `fake_bridge.py`.
 */
fun fakeBridgePath(): String =
    File(TestBridgesMarker::class.java.getResource("/fake_bridge.py")!!.toURI()).absolutePath

/** Anchor for the resource lookup. */
private object TestBridgesMarker

/**
 * A launcher that runs the scripted bridge as a real child process.
 *
 * @param cwd The directory the bridge runs in.
 * @param options Options of `fake_bridge.py`.
 * @return The launcher.
 */
fun fakeLocalLauncher(cwd: String, vararg options: String): LocalLauncher =
    LocalLauncher({ ResolveResult.Found(listOf("python3", fakeBridgePath(), *options).let { it.dropLast(0) }, "setting") }, cwd)

/**
 * Write an executable shell script into a new temporary directory.
 *
 * @param name The file name, e.g. "ssh".
 * @param body The script's lines after the `#!/bin/sh` line.
 * @return The script's absolute path.
 */
fun script(name: String, body: String): String {
    val file = File(Files.createTempDirectory("calu-test").toFile(), name)
    file.writeText("#!/bin/sh\n$body\n")
    file.setExecutable(true)
    return file.absolutePath
}
