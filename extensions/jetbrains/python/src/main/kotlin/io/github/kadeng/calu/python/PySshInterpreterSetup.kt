/**
 * PyCharm's SSH interpreter for the SSH mode, made with the IDE's own objects.
 *
 *     SshConfigManager          an SSH configuration for user@host:port (OpenSSH config and agent;
 *                               an existing one of the same host, user and port is reused)
 *     GroupedServersConfigManager  a deployment server "user@host:port (calu)" over SFTP
 *     PublishConfig             its path mapping: local folder -> remote directory
 *     WebDeploymentTargetEnvironmentConfiguration
 *                               the interpreter's target: that server, automatic upload off
 *     ProjectJdkTable           a Python SDK on that target; the project's and its modules' SDK
 *
 * This is the shape PyCharm 2026.3 itself creates in Add Interpreter | On SSH ("ssh/web-deployment"
 * target with a deployment server). Automatic upload stays off: calu's synchronisation (or the
 * user's own) keeps the two copies equal, and a second writer could overwrite the agent's edits.
 *
 * API status (PyCharm 2026.3, javap and the Plugin Verifier): the Python plugin has no public
 * API that creates an SSH interpreter.
 *
 * - Internal API (`@ApiStatus.Internal`): `PyTargetAwareAdditionalData` (the data of such an
 *   SDK), its constructors and its loader, also its package and the "add interpreter" packages.
 * - What this file uses instead: the persistence methods `SdkType.loadAdditionalData` and
 *   `saveAdditionalData` of `PythonSdkType`. They read an element that the platform's own
 *   `saveTargetConfiguration` writes, so no XML is written by hand. The data is then read and
 *   changed through the platform interfaces `TargetBasedSdkAdditionalData` and
 *   `RemoteSdkProperties`.
 * - The cost: this depends on PyCharm's stored format of SSH interpreters, which PyCharm itself
 *   must keep readable across versions. The deployment and SSH configuration classes are public
 *   but have no compatibility promise either.
 */
package io.github.kadeng.calu.python

import com.intellij.execution.target.TargetBasedSdkAdditionalData
import com.intellij.execution.target.saveTargetConfiguration
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.EDT
import com.intellij.openapi.module.ModuleManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.projectRoots.ProjectJdkTable
import com.intellij.openapi.projectRoots.Sdk
import com.intellij.openapi.roots.ModuleRootManager
import com.intellij.openapi.roots.ModuleRootModificationUtil
import com.intellij.openapi.roots.ProjectRootManager
import com.intellij.openapi.util.io.FileUtil
import com.intellij.remote.AuthType
import com.intellij.remote.RemoteSdkProperties
import com.intellij.ssh.config.unified.SshConfig
import com.intellij.ssh.config.unified.SshConfigManager
import com.intellij.ssh.ui.unified.SshUiData
import com.jetbrains.plugins.remotesdk.target.ssh.target.SshTargetEnvironmentConfiguration
import com.jetbrains.plugins.remotesdk.target.ssh.target.SshTargetEnvironmentConfigurationBase
import com.jetbrains.plugins.remotesdk.target.webDeployment.WebDeploymentTargetEnvironmentConfiguration
import com.jetbrains.plugins.webDeployment.config.AccessType
import com.jetbrains.plugins.webDeployment.config.DeploymentPathMapping
import com.jetbrains.plugins.webDeployment.config.FileTransferConfig
import com.jetbrains.plugins.webDeployment.config.GroupedServersConfigManager
import com.jetbrains.plugins.webDeployment.config.PublishConfig
import com.jetbrains.plugins.webDeployment.config.WebServerConfig
import com.jetbrains.python.sdk.PythonSdkType
import io.github.kadeng.calu.backend.SshInterpreterSetup
import io.github.kadeng.calu.backend.SshInterpreterTarget
import io.github.kadeng.calu.logic.SshEndpoint
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.jdom.Element

/** See [SshInterpreterSetup] for the contract; the file comment for what is created. */
class PySshInterpreterSetup : SshInterpreterSetup {
    override fun findMatching(project: Project, target: SshInterpreterTarget): String? {
        val modules = ModuleManager.getInstance(project).modules.mapNotNull { ModuleRootManager.getInstance(it).sdk }
        val sdks = (listOfNotNull(ProjectRootManager.getInstance(project).projectSdk) + modules).distinct()
        return sdks.firstOrNull { matches(project, it, target) }?.name
    }

    override suspend fun create(project: Project, target: SshInterpreterTarget, python: String, version: String?): String = withContext(Dispatchers.EDT) {
        val ssh = sshConfig(project, target.endpoint)
        val (server, newServer) = deploymentServer(project, ssh, target.endpoint)
        try {
            mapFolder(project, server, target)
            val existing = ProjectJdkTable.getInstance().getSdksOfType(PythonSdkType.getInstance()).firstOrNull {
                it.homePath == python && matches(project, it, target)
            }
            val sdk = existing ?: newSdk(project, server, target, python, version)
            ApplicationManager.getApplication().runWriteAction {
                if (existing == null) ProjectJdkTable.getInstance().addJdk(sdk)
                ProjectRootManager.getInstance(project).projectSdk = sdk
            }
            for (module in ModuleManager.getInstance(project).modules) ModuleRootModificationUtil.setSdkInherited(module)
            sdk.name
        } catch (error: Exception) {
            if (newServer) GroupedServersConfigManager.getInstance(project).removeServer(server.id)
            throw error
        }
    }

    /**
     * Find or create PyCharm's SSH configuration for the endpoint: authentication through the
     * OpenSSH configuration and agent (the same key the plugin's `ssh` uses), no password stored.
     *
     * @param project The project.
     * @param endpoint Host, user and port.
     * @return The registered configuration.
     */
    private fun sshConfig(project: Project, endpoint: SshEndpoint): SshConfig {
        val manager = SshConfigManager.getInstance(project)
        manager.configs.firstOrNull { sameEndpoint(it, endpoint) }?.let { return it }
        val config = SshConfig(false).apply {
            setHost(endpoint.alias)
            setPort(endpoint.port)
            setUsername(endpoint.user)
            setAuthType(AuthType.OPEN_SSH)
            setOpenSshConfigUsageForced(true)
        }
        return manager.register(SshUiData.create(config)).config
    }

    /**
     * Find or create the deployment server (SFTP) on an SSH configuration.
     *
     * @param project The project.
     * @param ssh The SSH configuration.
     * @param endpoint For the server's name.
     * @return The server and whether it was created now.
     */
    private fun deploymentServer(project: Project, ssh: SshConfig, endpoint: SshEndpoint): Pair<WebServerConfig, Boolean> {
        val manager = GroupedServersConfigManager.getInstance(project)
        manager.flattenedServers.firstOrNull { it.fileTransferConfig?.accessType == AccessType.SFTP && it.findSshConfig(project)?.id == ssh.id }
            ?.let { return it to false }
        val server = WebServerConfig().apply {
            setName("${endpoint.user}@${endpoint.alias}:${endpoint.port} (calu)")
            setIsProjectLevel(false)
            fileTransferConfig = FileTransferConfig().apply {
                accessType = AccessType.SFTP
                setSshConfig(ssh)
            }
        }
        manager.setUniqueName(server)
        manager.addServer(server)
        return server to true
    }

    /**
     * Give the deployment server the path mapping local folder -> remote directory (other
     * mappings of the server stay; one of the same local folder is replaced).
     *
     * @param project The project.
     * @param server The deployment server.
     * @param target The folders.
     */
    private fun mapFolder(project: Project, server: WebServerConfig, target: SshInterpreterTarget) {
        val publish = PublishConfig.getInstance(project)
        val mapping = DeploymentPathMapping().apply {
            localPath = FileUtil.toSystemIndependentName(target.localDir)
            deployPath = target.remoteDir
            webPath = "/"
        }
        val others = publish.getPathMappings(server.id).filter { !sameDir(it.localPath, target.localDir) }
        publish.setPathMappings(server.id, others + mapping)
    }

    /**
     * Create the SDK (not yet registered): home path, version and the target-aware data.
     *
     * @param project The project.
     * @param server The deployment server of the target.
     * @param target The folders.
     * @param python The interpreter's path on the server.
     * @param version Its version, or `null`.
     * @return The SDK.
     */
    private fun newSdk(project: Project, server: WebServerConfig, target: SshInterpreterTarget, python: String, version: String?): Sdk {
        val configuration = WebDeploymentTargetEnvironmentConfiguration().apply {
            displayName = "${target.endpoint.user}@${target.endpoint.alias}:${target.endpoint.port}"
            setWebServerConfig(server)
            projectRootOnTarget = target.remoteDir
            autoUploadProjectFiles = false
        }
        val name = uniqueName("Python ${version ?: ""} on ${target.endpoint.alias} ($python)".replace("  ", " "))
        val sdk = ProjectJdkTable.getInstance().createSdk(name, PythonSdkType.getInstance())
        ApplicationManager.getApplication().runWriteAction {
            sdk.sdkModificator.apply {
                homePath = python
                if (version != null) versionString = "Python $version"
            }.commitChanges()
        }
        // The data's class is internal API: PythonSdkType's public loader builds it from the target
        // configuration as the platform saves it; a second round trip carries the interpreter path
        // (set through the public RemoteSdkProperties) into the target's Python runtime.
        val type = PythonSdkType.getInstance()
        val first = Element("additional").also { saveTargetConfiguration(it, configuration) }
        val draft = type.loadAdditionalData(sdk, first) as? RemoteSdkProperties ?: error("PyCharm could not read the SSH target.")
        draft.interpreterPath = python
        draft.isValid = true
        val second = Element("additional").also { type.saveAdditionalData(draft as com.intellij.openapi.projectRoots.SdkAdditionalData, it) }
        val data = type.loadAdditionalData(sdk, second) ?: error("PyCharm could not read the SSH interpreter.")
        check((data as? TargetBasedSdkAdditionalData)?.targetEnvironmentConfiguration is WebDeploymentTargetEnvironmentConfiguration) { "PyCharm did not accept the SSH target." }
        check((data as? RemoteSdkProperties)?.interpreterPath == python) { "PyCharm did not keep the interpreter path." }
        ApplicationManager.getApplication().runWriteAction {
            sdk.sdkModificator.apply { sdkAdditionalData = data }.commitChanges()
        }
        check(project.isOpen) { "The project was closed." }
        return sdk
    }

    /**
     * Decide whether an SDK runs on the target's server and maps the target's folders.
     *
     * @param project The project.
     * @param sdk The SDK.
     * @param target The server and folders.
     * @return True for an SSH SDK of the same endpoint whose mapping (deployment server, the SDK's
     *   own mappings, or the target's project root) joins the local folder and the remote directory.
     */
    private fun matches(project: Project, sdk: Sdk, target: SshInterpreterTarget): Boolean {
        if (sdk.sdkType != PythonSdkType.getInstance()) return false
        val data = sdk.sdkAdditionalData
        val configuration = (data as? TargetBasedSdkAdditionalData)?.targetEnvironmentConfiguration as? SshTargetEnvironmentConfigurationBase ?: return false
        val ssh = configuration.findSshConfig(project) ?: return false
        if (!sameEndpoint(ssh, target.endpoint)) return false
        val ownMapping = (data as? RemoteSdkProperties)?.pathMappings?.pathMappings.orEmpty().any { sameDir(it.localRoot, target.localDir) && sameDir(it.remoteRoot, target.remoteDir) }
        return ownMapping || when (configuration) {
            is WebDeploymentTargetEnvironmentConfiguration -> {
                val server = configuration.findWebServerConfig(project) ?: return false
                PublishConfig.getInstance(project).getPathMappings(server.id).any { sameDir(it.localPath, target.localDir) && sameDir(it.deployPath, target.remoteDir) }
            }
            is SshTargetEnvironmentConfiguration -> sameDir(configuration.projectRootOnTarget, target.remoteDir)
            else -> false
        }
    }

    /**
     * Compare an SSH configuration with the endpoint.
     *
     * @param config PyCharm's SSH configuration.
     * @param endpoint Host, user and port.
     * @return True if host, user and port are the same.
     */
    private fun sameEndpoint(config: SshConfig, endpoint: SshEndpoint): Boolean =
        config.host == endpoint.alias && config.username == endpoint.user && config.port == endpoint.port

    /**
     * Compare two directory paths, ignoring separators and a trailing slash.
     *
     * @param a A path (may be null).
     * @param b A path.
     * @return True if both name the same directory.
     */
    private fun sameDir(a: String?, b: String): Boolean {
        if (a.isNullOrEmpty()) return false
        fun norm(p: String) = FileUtil.toSystemIndependentName(p).trimEnd('/').ifEmpty { "/" }
        return norm(a) == norm(b)
    }

    /**
     * Make an SDK name unique in the IDE's table.
     *
     * @param wanted The name.
     * @return The name, with " (2)", " (3)", ... if it is taken.
     */
    private fun uniqueName(wanted: String): String {
        val table = ProjectJdkTable.getInstance()
        if (table.findJdk(wanted) == null) return wanted
        var i = 2
        while (table.findJdk("$wanted ($i)") != null) i++
        return "$wanted ($i)"
    }
}
