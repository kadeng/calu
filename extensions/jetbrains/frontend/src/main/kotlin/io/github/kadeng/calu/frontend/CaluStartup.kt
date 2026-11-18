package io.github.kadeng.calu.frontend

import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity

/**
 * Starts the chat's connection when a project opens, so that the status bar count and the
 * notifications about waiting steps work before the tool window was opened once. Side effect in
 * the backend: the bridge of the project is started (in a project without calu this ends with
 * the status "missing" and nothing else happens).
 */
class CaluStartup : ProjectActivity {
    override suspend fun execute(project: Project) {
        CaluFrontendService.of(project).start()
    }
}
