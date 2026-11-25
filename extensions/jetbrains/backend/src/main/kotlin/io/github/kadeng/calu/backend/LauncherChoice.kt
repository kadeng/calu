/**
 * Which launcher the settings select ("How calu is reached"):
 *
 *     mode    condition                                        launcher
 *     auto    Windows IDE and the project is below \\wsl...    WslLauncher
 *     auto    otherwise                                        LocalLauncher
 *     local                                                    LocalLauncher
 *     wsl     the project is below \\wsl.localhost\<distro>    WslLauncher (else an error that says so)
 *     ssh                                                      SshLauncher
 *
 * The Eel API would cover local, WSL and Docker with one call, but it is marked experimental in
 * the target builds; the plugin uses `wsl.exe` directly and can switch later behind this function.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.util.SystemInfo
import io.github.kadeng.calu.bridge.BridgeLauncher
import io.github.kadeng.calu.bridge.BridgeProcess
import io.github.kadeng.calu.bridge.LaunchException
import io.github.kadeng.calu.bridge.LocalLauncher
import io.github.kadeng.calu.bridge.SshLauncher
import io.github.kadeng.calu.bridge.SshSettings
import io.github.kadeng.calu.bridge.WslLauncher
import io.github.kadeng.calu.bridge.parseWslPath
import io.github.kadeng.calu.logic.HostFacts
import io.github.kadeng.calu.logic.looksLikeWsl
import io.github.kadeng.calu.logic.resolveCaluCommand
import io.github.kadeng.calu.logic.splitArgs
import io.github.kadeng.calu.remote.PluginSettingsDto
import java.io.File

/**
 * The launcher that the settings select, with the facts other code needs about it.
 *
 * @property launcher Starts the bridge.
 * @property mode The effective mode: "local", "wsl" or "ssh" (never "auto").
 * @property wslDistro The WSL distribution (mode "wsl"), else "".
 * @property target Key of the calu installation that is reached, for the password memory:
 *   "local", "wsl:<distro>" or "ssh:<host>".
 */
data class LauncherChoice(val launcher: BridgeLauncher, val mode: String, val wslDistro: String, val target: String)

/**
 * Collect the facts about this machine that the lookup of the calu command needs.
 *
 * @return PATH, home directory, WSL and Windows flags, and a check for executable files.
 */
fun hostFacts(): HostFacts = HostFacts(
    pathEnv = System.getenv("PATH").orEmpty(),
    pathDelimiter = File.pathSeparator,
    home = System.getProperty("user.home").orEmpty(),
    isWsl = SystemInfo.isLinux && (!System.getenv("WSL_DISTRO_NAME").isNullOrEmpty() || looksLikeWsl(System.getProperty("os.version").orEmpty())),
    isWindows = SystemInfo.isWindows,
    isExecutable = { path -> File(path).let { it.isFile && (SystemInfo.isWindows || it.canExecute()) } },
)

/** A launcher that cannot start anything and says why. */
internal class RefusingLauncher(private val why: String) : BridgeLauncher {
    override val kind: String = "none"
    override val reconnects: Boolean = false
    override fun describe(): String = why
    override fun launch(): BridgeProcess = throw LaunchException(false, why)
}

/**
 * Select the launcher for a project.
 *
 * @param settings The editor-side settings.
 * @param basePath The project directory as the IDE has it; `null` if the project has none.
 * @param isWindows Whether the IDE runs on Windows (a parameter for tests).
 * @return The choice. A project without a directory, and mode "wsl" for a project outside WSL,
 *   give a launcher whose start fails with a message that says what to change.
 */
fun chooseLauncher(settings: PluginSettingsDto, basePath: String?, isWindows: Boolean = SystemInfo.isWindows): LauncherChoice {
    val calu = splitArgs(settings.caluCommand)
    if (settings.mode == "ssh") {
        val ssh = SshSettings(settings.sshHost, settings.sshDirectory, splitArgs(settings.sshOptions), calu)
        return LauncherChoice(SshLauncher(ssh), "ssh", "", "ssh:${settings.sshHost.trim()}")
    }
    if (basePath == null) return LauncherChoice(RefusingLauncher("Open a project folder to use calu."), "local", "", "local")
    val wsl = parseWslPath(basePath)
    if (settings.mode == "wsl" || (settings.mode == "auto" && isWindows && wsl != null)) {
        if (wsl == null) {
            return LauncherChoice(
                RefusingLauncher("The mode \"WSL\" needs a project below \\\\wsl.localhost\\<distribution>; this project is at $basePath. Change \"How calu is reached\" in Settings | Tools | calu."),
                "wsl", "", "local",
            )
        }
        return LauncherChoice(WslLauncher(wsl.distro, wsl.linuxPath, calu), "wsl", wsl.distro, "wsl:${wsl.distro}")
    }
    return LauncherChoice(LocalLauncher({ resolveCaluCommand(calu, hostFacts()) }, basePath), "local", "", "local")
}
