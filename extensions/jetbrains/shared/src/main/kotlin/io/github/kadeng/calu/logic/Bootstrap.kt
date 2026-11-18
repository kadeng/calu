/**
 * Pure logic of installing calu: where uv is, which commands install uv and calu, what the
 * user is asked before each of them, and how `claude` is reported. Running the commands and
 * showing the questions is the front end's part.
 *
 *     calu missing ──► ask ──► uv missing? ask, install uv (astral.sh script, ~/.local/bin)
 *                          ──► uv tool install --force --python 3.14 <install source>
 *     update       ──► uv tool upgrade calu
 *     claude       ──► only checked (`claude --version`) and reported with CLAUDE_DOCS_URL;
 *                      never installed by the plugin
 *
 * `nextInstallAction` is the whole flow as one decision function: the front end calls it with
 * what happened so far and does what it returns, until it returns `Done` or `Failed`:
 *
 *     InstallProgress()                               -> Ask(calu)      "Install calu on this machine?"
 *       declined                                      -> Done(installed = false)
 *       confirmed, uv found                           -> Run("Installing calu", [uv, tool, install, ...])
 *       confirmed, uv missing                         -> Ask(uv)        "uv is needed ... Install uv now?"
 *         confirmed                                   -> Run("Installing uv", [sh, -c, ...])
 *           exit 0, uv found now                      -> Run("Installing calu", ...)
 *           exit 0, uv still missing                  -> Failed("uv was installed, but cannot be found ...")
 *       a step ended with another exit code           -> Failed("<title> failed (exit code N).")
 *       the calu step ended with exit code 0          -> Done(installed = true, "calu: installed. Next: ...")
 *
 * Nothing is installed without the user's answer. The root installation that
 * `calu serve --sudo` needs is only shown as a command (`rootInstallCommand` in
 * `ServerCommand.kt`): it needs sudo and is never run by the plugin.
 *
 * Port of `extensions/vscode/src/shared/bootstrap.ts` and of the decisions of
 * `extensions/vscode/src/bootstrap.ts` (`Installer`).
 */
package io.github.kadeng.calu.logic

/** Default of the install source: what `uv tool install` gets when the setting is empty. */
const val DEFAULT_INSTALL_SOURCE: String = "git+https://github.com/kadeng/calu"

/** The official uv installer, as shown to the user before it runs. */
const val UV_INSTALL_URL: String = "https://astral.sh/uv/install.sh"

/** The Python version uv installs calu with. */
const val CALU_PYTHON: String = "3.14"

/**
 * Find the `uv` executable: PATH (on WSL without Windows entries under /mnt/), then
 * ~/.local/bin/uv (default of the uv installer), then ~/.cargo/bin/uv (older installers).
 *
 * @param facts Facts about the machine.
 * @return The path of `uv`, or `null`.
 */
fun findUv(facts: HostFacts): String? = findExecutable("uv", facts, listOf(listOf(".local", "bin"), listOf(".cargo", "bin")))?.path

/**
 * Return the shell command that installs uv with the official installer.
 *
 * It downloads the installer with curl (or, without curl, with wget) and runs it with `sh`.
 * The installer puts `uv` into ~/.local/bin; `UV_NO_MODIFY_PATH=1` keeps it from editing shell
 * start-up files.
 *
 * @return A command for `sh -c`.
 */
fun uvInstallCommand(): String {
    val fetch = "if command -v curl >/dev/null 2>&1; then curl -LsSf $UV_INSTALL_URL; else wget -qO- $UV_INSTALL_URL; fi"
    return "($fetch) | env UV_NO_MODIFY_PATH=1 sh"
}

/**
 * Check the install source setting.
 *
 * @param source The setting's value, or `null`.
 * @return The trimmed source, or `DEFAULT_INSTALL_SOURCE` for `null` and for a blank value.
 * @throws IllegalArgumentException if the source starts with "-" (uv would read it as an option).
 */
fun installSource(source: String?): String {
    val text = source.orEmpty().trim()
    if (text.isEmpty()) return DEFAULT_INSTALL_SOURCE
    require(!text.startsWith("-")) { "The install source must not start with \"-\": $text" }
    return text
}

/**
 * Return the argv that installs (or replaces) calu as a uv tool.
 *
 * uv downloads Python 3.14 if needed and the dependencies from PyPI; `calu` lands in uv's tool
 * bin directory (default ~/.local/bin).
 *
 * @param uv Path of the uv executable.
 * @param source What to install (see `installSource`).
 * @return `[uv, "tool", "install", "--force", "--python", "3.14", source]`.
 */
fun caluInstallArgv(uv: String, source: String): List<String> = listOf(uv, "tool", "install", "--force", "--python", CALU_PYTHON, source)

/**
 * Return the argv that updates a calu installed as a uv tool.
 *
 * @param uv Path of the uv executable.
 * @return `[uv, "tool", "upgrade", "calu"]`.
 */
fun caluUpgradeArgv(uv: String): List<String> = listOf(uv, "tool", "upgrade", "calu")

/**
 * Return one shell script that installs calu on a machine whose files the plugin cannot look
 * at (the SSH host, a WSL distribution): it installs uv first if `uv` is neither on PATH nor
 * in ~/.local/bin or ~/.cargo/bin, then calu as a uv tool. The user is asked before it runs
 * (`installQuestion`, and `uvQuestion` because it may install uv).
 *
 * @param source What to install (see `installSource`).
 * @return A script for `sh -c` on that machine; it stops at the first failing command.
 */
fun remoteInstallScript(source: String): String =
    "set -e; PATH=\"\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH\"; " +
        "if ! command -v uv >/dev/null 2>&1; then ${uvInstallCommand()}; fi; " +
        "uv tool install --force --python $CALU_PYTHON ${shellQuote(source)}"

/** What is known about `claude` on the machine. */
sealed interface ClaudeCheck {
    /**
     * Claude Code is installed.
     *
     * @property path Path of `claude`.
     * @property version The first line of `claude --version`; "unknown version" if it printed none.
     */
    data class Found(val path: String, val version: String) : ClaudeCheck

    /**
     * Claude Code was not found.
     *
     * @property message A sentence for the user with the address of the install instructions.
     */
    data class Missing(val message: String) : ClaudeCheck
}

/**
 * Return the argv that asks Claude Code for its version.
 *
 * @param path Path of `claude`.
 * @return `[path, "--version"]`.
 */
fun claudeVersionArgv(path: String): List<String> = listOf(path, "--version")

/**
 * Build the report about `claude` from the lookup and the output of `claude --version`.
 *
 * @param path Path of `claude` (`findExecutable("claude", facts)`), or `null` if it was not found.
 * @param versionOutput Standard output of `claude --version`; "" if it could not be run.
 * @return `Found` with the version, or `Missing` with the address of the install instructions.
 *   The plugin never installs Claude Code.
 */
fun claudeCheck(path: String?, versionOutput: String): ClaudeCheck {
    if (path.isNullOrEmpty()) {
        return ClaudeCheck.Missing(
            "Claude Code (`claude`) was not found on this machine. Install it as described at $CLAUDE_DOCS_URL " +
                "and run `claude` once in a terminal in the project folder to sign in.",
        )
    }
    return ClaudeCheck.Found(path, versionOutput.split("\n")[0].trim().ifEmpty { "unknown version" })
}

// ------------------------------------------------------------------ the install flow

/**
 * A question the user answers before something is installed.
 *
 * @property what "calu" or "uv": what would be installed.
 * @property question The question (the dialog's title or first line).
 * @property detail What will happen, in sentences.
 * @property button Text of the button that says yes.
 */
data class InstallQuestion(val what: String, val question: String, val detail: String, val button: String)

/**
 * One command of the installation.
 *
 * @property what "calu" or "uv".
 * @property title What the step does, for a progress text and the log, e.g. "Installing uv".
 * @property argv The command; its output goes to the plugin's log.
 */
data class InstallStep(val what: String, val title: String, val argv: List<String>)

/**
 * Build the question asked before calu is installed.
 *
 * @param source What will be installed (see `installSource`).
 * @param where Where, as words after "Install calu", e.g. "on this machine" or "on the SSH
 *   host box".
 * @return The question with the button "Install".
 */
fun installQuestion(source: String, where: String = "on this machine"): InstallQuestion = InstallQuestion(
    "calu", "Install calu $where?",
    "uv installs calu as a tool (`uv tool install --python $CALU_PYTHON $source`), with Python $CALU_PYTHON and its " +
        "dependencies from PyPI (internet access needed). The command `calu` goes to ~/.local/bin.",
    "Install",
)

/**
 * Build the question asked before uv is installed.
 *
 * @return The question with the button "Install uv".
 */
fun uvQuestion(): InstallQuestion = InstallQuestion(
    "uv", "uv is needed to install calu, but it was not found. Install uv now?",
    "This downloads and runs the official uv installer from $UV_INSTALL_URL " +
        "(`curl -LsSf $UV_INSTALL_URL | sh`, or wget without curl). It puts uv into ~/.local/bin and " +
        "does not change your shell start-up files.",
    "Install uv",
)

/**
 * What happened so far in one run of the installation.
 *
 * @property source The install source setting (unchecked); `null` for the default.
 * @property where Where calu is installed, for the question (see `installQuestion`).
 * @property uv Path of `uv` as found now (`findUv`); `null` if it is missing. The front end
 *   looks again after the uv step ran.
 * @property caluAnswer The user's answer to the calu question: `null` not asked yet, true
 *   "Install", false declined.
 * @property uvAnswer The user's answer to the uv question, likewise.
 * @property uvExit Exit code of the uv step; `null` if it did not run (-1: it could not be started).
 * @property caluExit Exit code of the calu step; `null` if it did not run.
 */
data class InstallProgress(
    val source: String? = null,
    val where: String = "on this machine",
    val uv: String? = null,
    val caluAnswer: Boolean? = null,
    val uvAnswer: Boolean? = null,
    val uvExit: Int? = null,
    val caluExit: Int? = null,
)

/** What the front end does next in the installation. */
sealed interface InstallAction {
    /**
     * Ask the user; put the answer into `caluAnswer` or `uvAnswer` (by `question.what`).
     *
     * @property question The question.
     */
    data class Ask(val question: InstallQuestion) : InstallAction

    /**
     * Run a command; put its exit code into `uvExit` or `caluExit` (by `step.what`), and after
     * the uv step look for uv again.
     *
     * @property step The command.
     */
    data class Run(val step: InstallStep) : InstallAction

    /**
     * The run is over without a failure.
     *
     * @property installed True if calu was installed; false if the user declined.
     * @property message What to tell the user; "" after a decline.
     */
    data class Done(val installed: Boolean, val message: String) : InstallAction

    /**
     * The run failed; nothing more is done.
     *
     * @property message What failed (also for the log).
     */
    data class Failed(val message: String) : InstallAction
}

/** What the user is told after calu was installed. */
const val INSTALLED_MESSAGE: String =
    "calu: installed. Next: run `calu setup` once in a terminal if this machine has no calu database yet."

/** What the user is told after calu was updated. */
const val UPDATED_MESSAGE: String = "calu: updated. Restart the bridge to use the new version."

/**
 * Decide the next step of installing calu (the table in the file comment).
 *
 * @param progress What happened so far.
 * @return The next action. The function never returns `Run` before the user confirmed what the
 *   step installs.
 */
fun nextInstallAction(progress: InstallProgress): InstallAction {
    val source = try {
        installSource(progress.source)
    } catch (error: IllegalArgumentException) {
        return InstallAction.Failed(error.message.orEmpty())
    }
    when (progress.caluAnswer) {
        null -> return InstallAction.Ask(installQuestion(source, progress.where))
        false -> return InstallAction.Done(false, "")
        true -> Unit
    }
    progress.caluExit?.let { code ->
        return if (code == 0) InstallAction.Done(true, INSTALLED_MESSAGE) else InstallAction.Failed("Installing calu failed (exit code $code).")
    }
    progress.uvExit?.let { code ->
        if (code != 0) return InstallAction.Failed("Installing uv failed (exit code $code).")
        if (progress.uv == null) return InstallAction.Failed("uv was installed, but cannot be found in ~/.local/bin.")
    }
    val uv = progress.uv
    if (uv == null) {
        return when (progress.uvAnswer) {
            null -> InstallAction.Ask(uvQuestion())
            false -> InstallAction.Done(false, "")
            true -> InstallAction.Run(InstallStep("uv", "Installing uv", listOf("sh", "-c", uvInstallCommand())))
        }
    }
    return InstallAction.Run(InstallStep("calu", "Installing calu", caluInstallArgv(uv, source)))
}

/**
 * Decide how calu is updated ("Update calu").
 *
 * @param uv Path of `uv` (`findUv`), or `null` if it is missing.
 * @param exit Exit code of the update step; `null` if it did not run yet.
 * @return `Run` with `uv tool upgrade calu`; after the step `Done` (exit code 0) or `Failed`;
 *   `Failed` at once if uv is missing (calu was then not installed with uv).
 */
fun nextUpdateAction(uv: String?, exit: Int? = null): InstallAction {
    if (uv == null) return InstallAction.Failed("uv was not found; calu was not installed with uv on this machine.")
    if (exit == null) return InstallAction.Run(InstallStep("calu", "Updating calu", caluUpgradeArgv(uv)))
    return if (exit == 0) InstallAction.Done(true, UPDATED_MESSAGE) else InstallAction.Failed("Updating calu failed (exit code $exit).")
}
