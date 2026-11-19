package io.github.kadeng.calu.sync

import io.github.kadeng.calu.bridge.BridgeLauncher
import io.github.kadeng.calu.bridge.BridgeProcess
import io.github.kadeng.calu.bridge.BridgeSupervisor
import io.github.kadeng.calu.bridge.PasswordPrompt
import io.github.kadeng.calu.bridge.SshLauncher
import io.github.kadeng.calu.bridge.SshSettings
import io.github.kadeng.calu.bridge.startProcess
import io.github.kadeng.calu.bridge.waitUntil
import io.github.kadeng.calu.json.strings
import io.github.kadeng.calu.state.BridgeStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.TimeUnit

/**
 * Runs shell lines where the remote project is: on this machine, or on the SSH box.
 *
 * @property prefix the command line a shell line is appended to (`sh -c` or `ssh ... host`).
 */
class RemoteShell(private val prefix: List<String>) {
    /**
     * Run one shell line and return its output.
     *
     * @param line the POSIX shell line.
     * @param stdin bytes for its standard input.
     * @return what it wrote to standard output.
     * @throws AssertionError if the line exits with another code than 0.
     */
    fun run(line: String, stdin: ByteArray = ByteArray(0)): ByteArray {
        val process = ProcessBuilder(prefix + line).redirectError(ProcessBuilder.Redirect.INHERIT).start()
        process.outputStream.use { it.write(stdin) }
        val out = process.inputStream.readAllBytes()
        assertTrue(process.waitFor(60, TimeUnit.SECONDS), "timed out: $line")
        assertEquals(0, process.exitValue(), "failed: $line")
        return out
    }

    /**
     * Run one shell line and return its output as trimmed text.
     *
     * @param line the POSIX shell line.
     * @return the text.
     */
    fun text(line: String): String = String(run(line)).trim()
}

/**
 * The file synchronisation against REAL bridges: `calu ide` as a child process of this test,
 * and `calu ide` on the SSH box over real SSH. Both run the same walk through a full sync in
 * both directions (see [walk]).
 */
class RealBridgeSyncTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val lines = (1..9).joinToString("") { "line $it\n" }

    /**
     * Find the calu checkout this plugin is part of.
     *
     * @return the directory with calu's `pyproject.toml`, or null.
     */
    private fun repo(): File? {
        System.getProperty("calu.repo")?.let { return File(it).takeIf { f -> File(f, "pyproject.toml").isFile } }
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val project = File(dir, "pyproject.toml")
            if (project.isFile && File(dir, "src/calu/ide").isDirectory) return dir
            dir = dir.parentFile
        }
        return null
    }

    /**
     * Find a program on PATH.
     *
     * @param name the program's name.
     * @return true if an executable file of that name is in a directory of PATH.
     */
    private fun onPath(name: String): Boolean =
        System.getenv("PATH").orEmpty().split(File.pathSeparator).any { File(it, name).canExecute() }

    @Test
    fun `local process - a full sync both ways against the repository's own calu ide`() = runBlocking {
        val repo = repo()
        assumeTrue(repo != null && onPath("uv") && onPath("git"), "skipped: needs `uv`, `git` and the calu checkout (-PcaluRepo=<dir>) to start `uv run calu ide`")
        val remoteDir = Files.createTempDirectory("calu-remote").toRealPath()
        val launcher = object : BridgeLauncher {
            override val kind = "local"
            override val reconnects = false
            override fun describe() = "uv run calu ide --cwd $remoteDir"
            override fun launch(): BridgeProcess = startProcess(listOf("uv", "run", "--quiet", "--project", repo!!.path, "calu", "ide", "--cwd", remoteDir.toString()), null, "calu ide")
        }
        val shell = RemoteShell(listOf("sh", "-c"))
        walk(launcher, shell, remoteDir.toString(), "local process") {
            shell.run("pkill -f '[c]alu ide --cwd $remoteDir' || true")
        }
    }

    @Test
    @Tag("sshbox")
    fun `ssh - a full sync both ways against calu ide on the SSH box`() = runBlocking {
        val config = System.getProperty("calu.sshbox").orEmpty()
        assumeTrue(config.isNotEmpty() && File(config).isFile, "skipped: start uitest/sshbox/run.sh and pass -Psshbox=\$HOME/.cache/calu-uitest/ssh/config (or CALU_SSHBOX)")
        val shell = RemoteShell(listOf("ssh", "-F", config, "-o", "BatchMode=yes", "sshbox"))
        val remoteDir = shell.text("mktemp -d /home/dev/synctest-XXXXXX")
        try {
            val launcher = SshLauncher(SshSettings("sshbox", remoteDir, listOf("-F", config)))
            walk(launcher, shell, remoteDir, "ssh") {
                shell.run("pkill -f '[c]alu ide --cwd $remoteDir' || true")
            }
        } finally {
            shell.run("rm -rf '$remoteDir'")
        }
    }

    /**
     * The walk both tests share. Each step is checked on both sides.
     *
     *     1  the remote project: Python files, .gitignore with an ignored directory, a binary file,
     *        300 small files, a link inside and one that points outside, as a git repository
     *     2  clone into the empty local folder: every listed file with equal hashes; links and
     *        ignored files are not copied; the links are noted
     *     3  a remote edit arrives locally (the bridge's watcher); a local save reaches the server
     *     4  new file, deletion and rename on each side
     *     5  a file above the request limit goes up in chunks and comes down in pieces
     *     6  both sides change different lines: merged on both sides
     *     7  both sides change the same line: a conflict, both kept, then resolved
     *     8  a held file: the save waits, and goes up when the hold ends
     *     9  the bridge dies: saves are queued; after the new start the queue is replayed
     *     10 stop: ide.shutdown, the process ends
     *
     * @param launcher starts the bridge.
     * @param shell runs shell lines where the remote project is.
     * @param remoteDir the remote project directory (exists, empty).
     * @param label name of the variant, for the measurements this prints.
     * @param kill ends the bridge process from outside.
     */
    private suspend fun walk(launcher: BridgeLauncher, shell: RemoteShell, remoteDir: String, label: String, kill: () -> Unit) {
        val q = "'$remoteDir'"
        // 1
        shell.run(
            "cd $q && mkdir -p pkg many build assets && printf 'a = 1\\n' > a.py && printf 'b = 1\\n' > pkg/b.py && " +
                "printf 'build/\\n*.log\\n' > .gitignore && echo ignored > build/out.log && head -c 200000 /dev/urandom > assets/blob.bin && " +
                "for n in \$(seq 1 300); do printf 'file %03d\\n' \$n > many/f\$(printf %03d \$n).txt; done && " +
                "ln -s a.py link-inside.py && ln -s /etc/hostname link-outside && " +
                "git init -q -b main && git -c user.name=t -c user.email=t@example.org add -A && git -c user.name=t -c user.email=t@example.org commit -q -m init",
        )
        shell.run("cat > $q/merge.txt", lines.toByteArray())
        shell.run("cat > $q/conflict.txt", lines.toByteArray())
        shell.run("cat > $q/held.txt", lines.toByteArray())
        val localDir: Path = Files.createTempDirectory("calu-local").toRealPath()
        val local = DiskLocalFiles(localDir)
        val ui = RecordingUi()
        val held = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
        val supervisor = BridgeSupervisor(launcher, scope, "0.0.0-test", PasswordPrompt { _, _ -> null })
        val remote = RemoteFileStore({ method, params -> supervisor.request(method, params, interactive = false) })
        val sync = Synchronizer(remote, local, SyncStore(Files.createTempDirectory("calu-store")), { held }, ui, label)
        val driver = SyncDriver(sync, supervisor, scope)
        fun get(path: String): String? = localDir.resolve(path).takeIf { Files.isRegularFile(it) }?.let { Files.readString(it) }
        fun put(path: String, text: String) {
            Files.createDirectories(localDir.resolve(path).parent)
            Files.writeString(localDir.resolve(path), text)
            driver.post { sync.localChanged(listOf(path)) }
        }
        fun remoteText(path: String): String = String(shell.run("cat $q/'$path' 2>/dev/null || true"))
        fun idle() = sync.status.value.phase == SyncPhase.IDLE
        try {
            // 2
            val started = System.nanoTime()
            driver.start()
            val init = supervisor.ensureStarted()
            assertTrue("fs" in init.strings("capabilities"))
            waitUntil("the clone") { idle() && get("a.py") != null }
            val cloneMs = (System.nanoTime() - started) / 1_000_000
            assertTrue(ui.asked.single().isClone)
            assertEquals(307, ui.asked.single().downloads.size, "a.py, pkg/b.py, .gitignore, the binary file, three text files, 300 small files")
            assertEquals("file 300\n", get("many/f300.txt"))
            assertEquals(shell.text("sha256sum $q/assets/blob.bin | cut -d' ' -f1"), sha256Hex(Files.readAllBytes(localDir.resolve("assets/blob.bin"))))
            assertFalse(Files.exists(localDir.resolve("build")), "ignored files are not copied")
            assertFalse(Files.exists(localDir.resolve("link-inside.py"), java.nio.file.LinkOption.NOFOLLOW_LINKS))
            assertFalse(Files.exists(localDir.resolve("link-outside"), java.nio.file.LinkOption.NOFOLLOW_LINKS))
            assertFalse(Files.exists(localDir.resolve(".git")))
            assertEquals(setOf("link-inside.py", "link-outside"), sync.status.value.notes.keys)
            assertEquals(remoteDir, sync.remotePaths!!.root)
            // 3
            val editAt = System.nanoTime()
            shell.run("printf 'a = 2\\n' > $q/a.py")
            waitUntil("the remote edit of a.py arrives") { get("a.py") == "a = 2\n" }
            val downMs = (System.nanoTime() - editAt) / 1_000_000
            val saveAt = System.nanoTime()
            put("pkg/b.py", "b = 2\n")
            waitUntil("the local save of pkg/b.py is on the server") { remoteText("pkg/b.py") == "b = 2\n" }
            val upMs = (System.nanoTime() - saveAt) / 1_000_000
            // 4
            put("new-local.py", "n = 1\n")
            shell.run("printf 'r = 1\\n' > $q/new-remote.py && rm $q/many/f001.txt && mv $q/many/f002.txt $q/many/renamed.txt")
            waitUntil("new files, the deletion and the rename of the server arrive") {
                get("new-remote.py") == "r = 1\n" && get("many/f001.txt") == null && get("many/f002.txt") == null && get("many/renamed.txt") == "file 002\n"
            }
            waitUntil("the new local file is on the server") { remoteText("new-local.py") == "n = 1\n" }
            Files.delete(localDir.resolve("many/f003.txt"))
            driver.post { sync.localChanged(listOf("many/f003.txt")) }
            Files.move(localDir.resolve("many/f004.txt"), localDir.resolve("many/moved.txt"))
            driver.post { sync.localRenamed("many/f004.txt", "many/moved.txt") }
            waitUntil("the local deletion and rename are on the server") {
                shell.text("cd $q/many && ls f003.txt f004.txt moved.txt 2>/dev/null | tr '\\n' ' '") == "moved.txt"
            }
            // 5
            val big = ByteArray(3 * 1024 * 1024 + 17) { (it * 31 % 251).toByte() }
            Files.write(localDir.resolve("big-local.bin"), big)
            driver.post { sync.localChanged(listOf("big-local.bin")) }
            waitUntil("the chunked upload") { shell.text("sha256sum $q/big-local.bin 2>/dev/null | cut -d' ' -f1") == sha256Hex(big) }
            shell.run("head -c 2500000 /dev/urandom > $q/big-remote.bin")
            waitUntil("the download in pieces") {
                Files.isRegularFile(localDir.resolve("big-remote.bin")) && Files.size(localDir.resolve("big-remote.bin")) == 2_500_000L && idle()
            }
            assertEquals(shell.text("sha256sum $q/big-remote.bin | cut -d' ' -f1"), sha256Hex(Files.readAllBytes(localDir.resolve("big-remote.bin"))))
            // 6
            shell.run("cat > $q/merge.txt", lines.replace("line 8\n", "line eight\n").toByteArray())
            put("merge.txt", lines.replace("line 2\n", "line two\n"))
            val merged = lines.replace("line 2\n", "line two\n").replace("line 8\n", "line eight\n")
            waitUntil("the merge on both sides") { get("merge.txt") == merged && remoteText("merge.txt") == merged }
            // 7
            shell.run("cat > $q/conflict.txt", lines.replace("line 5\n", "theirs\n").toByteArray())
            put("conflict.txt", lines.replace("line 5\n", "mine\n"))
            waitUntil("the conflict") { sync.status.value.conflicts == listOf("conflict.txt") }
            assertEquals(lines.replace("line 5\n", "mine\n"), get("conflict.txt"))
            assertEquals(lines.replace("line 5\n", "theirs\n"), remoteText("conflict.txt"))
            val contents = sync.conflictContents("conflict.txt")!!
            assertEquals(lines, String(contents.base!!))
            assertTrue(sync.resolve("conflict.txt", contents.local))
            assertEquals(lines.replace("line 5\n", "mine\n"), remoteText("conflict.txt"))
            // 8
            held.add("held.txt")
            put("held.txt", lines.replace("line 1\n", "user\n"))
            waitUntil("the held save is queued") { sync.status.value.held == listOf("held.txt") }
            assertEquals(lines, remoteText("held.txt"))
            held.clear()
            driver.post { sync.holdsChanged() }
            waitUntil("the held save goes up after the turn") { remoteText("held.txt") == lines.replace("line 1\n", "user\n") }
            // 9
            waitUntil("idle before the connection is cut") { idle() }
            kill()
            waitUntil("the lost bridge is noticed") { supervisor.status.value.status != BridgeStatus.RUNNING || !idle() }
            put("offline.py", "o = 1\n")
            shell.run("printf 'while = 1\\n' > $q/while-away.py")
            if (!launcher.reconnects) {
                waitUntil("offline") { sync.status.value.phase == SyncPhase.OFFLINE && sync.status.value.queued == 1 }
                assertEquals("calu sync: offline: 1 change queued", syncStatusText(sync.status.value))
                supervisor.ensureStarted()
            }
            waitUntil("the queue is replayed and the comparison finds the server's change") {
                remoteText("offline.py") == "o = 1\n" && get("while-away.py") == "while = 1\n" && idle()
            }
            assertEquals(0, sync.status.value.queued)
            assertEquals(1, ui.asked.size, "only the clone was asked for")
            // 10
            supervisor.stop()
            assertEquals(BridgeStatus.STOPPED, supervisor.status.value.status)
            assertNull(String(shell.run("pgrep -f '[c]alu ide --cwd $remoteDir' || true")).trim().takeIf { it.isNotEmpty() }, "the bridge process ended")
            println("[calu sync measurements, $label] clone of 307 files: $cloneMs ms; remote edit to local file: $downMs ms; local save to server: $upMs ms")
        } finally {
            driver.stop()
            supervisor.dispose()
            scope.cancel()
            localDir.toFile().deleteRecursively()
            if (launcher.kind == "local") File(remoteDir).deleteRecursively()
        }
    }
}
