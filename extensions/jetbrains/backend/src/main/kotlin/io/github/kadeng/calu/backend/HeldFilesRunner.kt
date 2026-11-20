/**
 * Run or debug while local saves are held (SSH mode with "Sync: through calu"): a save of a
 * file that Claude edits in the running turn waits on this machine until the turn ends, so the
 * server would run the old version. This runner asks first.
 *
 *     Run / Debug / gutter / Shift+F10 ──► ExecutionEnvironmentBuilder picks the first runner whose
 *                                          canRun is true (programRunner order="first": this one,
 *                                          but only while files are held and another runner can run it)
 *                                              │
 *     HeldFilesRunner.execute ── dialog ─┬─ Upload now and run ─► upload, wait ─► the other runner
 *                                        ├─ Run anyway ──────────────────────────► the other runner
 *                                        └─ Cancel ──────────────────────────────► nothing starts
 *
 * Why a `ProgramRunner`: it is the public extension point every start of a run configuration
 * passes through, for every executor (Run, Debug, Profile, ...), and its `execute` may decide
 * not to start or to start later. `ExecutionListener` only reports a start and cannot stop it;
 * a `BeforeRunTaskProvider` acts only for the configurations whose "Before launch" list holds
 * its task, so existing configurations would be missed. The other runner gets an environment
 * of its own (`ExecutionEnvironmentBuilder(...).runnerAndSettings`), so the Run or Debug tab it
 * opens belongs to it; a Rerun from that tab therefore starts without this question, and
 * `HeldFilesStartListener` shows a notification with "Upload now" for such a start instead.
 *
 * While nothing is held (every other mode, "Sync: none", no running turn) `canRun` is false
 * and the platform chooses the runners exactly as without this plugin.
 */
package io.github.kadeng.calu.backend

import com.intellij.execution.ExecutionException
import com.intellij.execution.ExecutionListener
import com.intellij.execution.configurations.RunProfile
import com.intellij.execution.configurations.RunnerSettings
import com.intellij.execution.runners.ExecutionEnvironment
import com.intellij.execution.runners.ExecutionEnvironmentBuilder
import com.intellij.execution.runners.ExecutionUtil
import com.intellij.execution.runners.ProgramRunner
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.EDT
import com.intellij.openapi.diagnostic.logger
import com.intellij.notification.Notification
import com.intellij.notification.NotificationAction
import com.intellij.notification.NotificationType
import com.intellij.openapi.project.Project
import com.intellij.platform.ide.progress.withBackgroundProgress
import io.github.kadeng.calu.sync.HeldRunChoice
import io.github.kadeng.calu.sync.heldRunChoice
import io.github.kadeng.calu.sync.heldRunQuestion
import io.github.kadeng.calu.sync.notUploadedQuestion
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private val LOG = logger<HeldFilesRunner>()

/** The runner's id (also its `id` in `calu.backend.xml`). */
const val HELD_FILES_RUNNER_ID: String = "caluHeldFiles"

/**
 * Return the files of a project whose local save is held until Claude's turn ends.
 *
 * Does not create the plugin's service or start a bridge: a project in which the plugin never
 * ran has nothing held.
 *
 * @param project The project.
 * @return The held files relative to the project ("/" separators); empty outside the SSH mode
 *   with "Sync: through calu" and while no turn runs.
 */
fun heldFilesOf(project: Project): List<String> {
    if (project.isDisposed) return emptyList()
    val service = project.getServiceIfCreated(CaluProjectService::class.java) ?: return emptyList()
    return service.currentSync()?.sync?.status?.value?.held.orEmpty()
}

/**
 * Pick the runner that would start a profile if this plugin's runner did not exist.
 *
 * @param runners The registered runners in the platform's order.
 * @param self This plugin's runner (skipped).
 * @param canRun Whether a runner can start the profile with the executor.
 * @return The first other runner that can, or `null` if none can.
 */
fun <R> nextRunner(runners: List<R>, self: R, canRun: (R) -> Boolean): R? = runners.firstOrNull { it !== self && canRun(it) }

/**
 * True while `HeldFilesRunner` hands an answered start to the other runner (on the EDT; the
 * platform reports `processStartScheduled` inside that call).
 */
@Volatile
internal var delegating: Boolean = false

/**
 * Reports a start that did not pass `HeldFilesRunner` while files are held (the Rerun button
 * of a Run or Debug tab keeps the runner of the first start): a notification says that the
 * server runs the old version and offers "Upload now". It cannot stop the start;
 * `ExecutionListener` only reports.
 *
 * @param project The project whose starts are reported (the platform passes it).
 */
class HeldFilesStartListener(private val project: Project) : ExecutionListener {
    /**
     * Show the notification for a start while files are held, unless `HeldFilesRunner` asked.
     *
     * @param executorId The executor ("Run", "Debug", ...).
     * @param env The start.
     */
    override fun processStartScheduled(executorId: String, env: ExecutionEnvironment) {
        if (delegating || env.runner is HeldFilesRunner || env.project != project) return
        val held = heldFilesOf(project)
        if (held.isEmpty()) return
        val sync = CaluProjectService.of(project).currentSync() ?: return
        val what = if (held.size == 1) "1 file is held" else "${held.size} files are held"
        try {
            val note = Notification("calu", "calu: ${env.runProfile.name} started with held files", "$what until Claude's turn ends; the server runs the old version.", NotificationType.WARNING)
            note.addAction(NotificationAction.createSimpleExpiring("Upload now") { sync.uploadHeldNow() })
            note.notify(project)
        } catch (error: Exception) {
            LOG.info("[calu] the held-files notice could not be shown: ${error.message}")
        }
    }
}

/** The runner that asks before a start while files are held; see the file's comment. */
class HeldFilesRunner : ProgramRunner<RunnerSettings> {
    override fun getRunnerId(): String = HELD_FILES_RUNNER_ID

    /**
     * Claim a start only while the profile's project has held files and another runner can
     * start it. Called often (also to enable actions), so it reads only in-memory state.
     *
     * @param executorId The executor ("Run", "Debug", ...).
     * @param profile The configuration to start.
     * @return True if this runner should ask before the start.
     */
    override fun canRun(executorId: String, profile: RunProfile): Boolean {
        val project = projectOf(profile) ?: return false
        if (heldFilesOf(project).isEmpty()) return false
        return delegateFor(executorId, profile) != null
    }

    /**
     * Ask, then start with the other runner, start after the upload, or start nothing. Called
     * by the platform on the EDT after the configuration's "Before launch" tasks.
     *
     * @param environment The start the platform prepared for this runner.
     * @throws ExecutionException if no other runner can start the profile, or the other runner
     *   fails to start it at once.
     */
    override fun execute(environment: ExecutionEnvironment) {
        val project = environment.project
        val delegate = delegateFor(environment.executor.id, environment.runProfile)
            ?: throw ExecutionException("No runner can start ${environment.runProfile.name} with ${environment.executor.actionName}.")
        val held = heldFilesOf(project)
        if (held.isEmpty()) {
            start(delegate, forDelegate(environment, delegate))
            return
        }
        val answer = heldRunChoice(ChoiceDialog.ask(project, heldRunQuestion(held)))
        LOG.info("[calu] run while ${held.size} file(s) held: $answer")
        when (answer) {
            HeldRunChoice.CANCEL -> return
            HeldRunChoice.RUN_ANYWAY -> start(delegate, forDelegate(environment, delegate))
            HeldRunChoice.UPLOAD_AND_RUN -> {
                val sync = CaluProjectService.of(project).currentSync()
                if (sync == null) {
                    start(delegate, forDelegate(environment, delegate))
                    return
                }
                CaluProjectService.of(project).launch {
                    val left = withBackgroundProgress(project, "calu: uploading held files", false) { sync.uploadHeldAndWait() }
                    withContext(Dispatchers.EDT) {
                        if (project.isDisposed) return@withContext
                        if (left.isNotEmpty() && ChoiceDialog.ask(project, notUploadedQuestion(left)) != 0) return@withContext
                        val next = forDelegate(environment, delegate)
                        try {
                            start(delegate, next)
                        } catch (error: ExecutionException) {
                            ExecutionUtil.handleExecutionError(next, error)
                        }
                    }
                }
            }
        }
    }

    /**
     * Start with the other runner. While it runs, `delegating` is true, so that
     * `HeldFilesStartListener` does not report this start (it was asked about).
     *
     * @param delegate The runner that starts the profile.
     * @param environment The start prepared for it (`forDelegate`).
     * @throws ExecutionException if the runner fails to start it.
     */
    private fun start(delegate: ProgramRunner<*>, environment: ExecutionEnvironment) {
        delegating = true
        try {
            delegate.execute(environment)
        } finally {
            delegating = false
        }
    }

    /**
     * Find the runner the platform would choose without this one.
     *
     * @param executorId The executor's id.
     * @param profile The configuration.
     * @return The runner, or `null` if no other runner can start it.
     */
    private fun delegateFor(executorId: String, profile: RunProfile): ProgramRunner<*>? =
        nextRunner(ProgramRunner.PROGRAM_RUNNER_EP.extensionList, this) { it.canRun(executorId, profile) }

    /**
     * Copy a start for another runner, with that runner's own settings of the configuration.
     *
     * @param environment The start prepared for this runner.
     * @param delegate The runner that starts it.
     * @return A new environment with the same executor, target, profile, data context and callback.
     */
    private fun forDelegate(environment: ExecutionEnvironment, delegate: ProgramRunner<*>): ExecutionEnvironment {
        val builder = ExecutionEnvironmentBuilder(environment)
        val settings = environment.runnerAndConfigurationSettings
        if (settings != null) builder.runnerAndSettings(delegate, settings) else builder.runner(delegate)
        return builder.build(environment.callback)
    }

    /**
     * Find the project of a run profile.
     *
     * @param profile The configuration.
     * @return Its project (a `RunConfiguration` names one); `null` for other profiles, which
     *   this runner leaves alone.
     */
    private fun projectOf(profile: RunProfile): Project? {
        val configuration = profile as? com.intellij.execution.configurations.RunConfiguration ?: return null
        if (ApplicationManager.getApplication().isDisposed) return null
        return configuration.project
    }
}
