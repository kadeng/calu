/**
 * PyCharm's own Deployment settings (`.idea/deployment.xml`) as far as the SSH mode's
 * synchronisation needs them: whether PyCharm uploads changed files by itself, and to which
 * remote directories. Two writers into one server directory (calu's synchronisation and
 * PyCharm's automatic upload) can overwrite each other's changes, so the plugin warns.
 *
 *     <component name="PublishConfigData" autoUpload="Always" serverName="dev@box">
 *       <serverData><paths name="dev@box"><serverdata><mappings>
 *         <mapping deploy="/home/dev/project" local="$PROJECT_DIR$" />
 */
package io.github.kadeng.calu.sync

import org.w3c.dom.Element
import java.io.StringReader
import javax.xml.XMLConstants
import javax.xml.parsers.DocumentBuilderFactory
import org.xml.sax.InputSource

/**
 * What the Deployment settings say.
 *
 * @property autoUpload the value of `autoUpload` ("Always", "On explicit save action"), or
 *   null if PyCharm does not upload by itself.
 * @property serverName the default server the automatic upload goes to, or null.
 * @property remoteDirs the remote directories of every mapping (`deploy`), in file order.
 */
data class DeploymentInfo(val autoUpload: String?, val serverName: String?, val remoteDirs: List<String>)

/**
 * Read `.idea/deployment.xml`. External entities and DTDs are refused.
 *
 * @param xml the file's text.
 * @return what it says, or null if the text is not a Deployment file (no `PublishConfigData`,
 *   or not XML).
 */
fun parseDeployment(xml: String): DeploymentInfo? {
    val doc = try {
        val factory = DocumentBuilderFactory.newInstance()
        factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true)
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
        factory.isExpandEntityReferences = false
        factory.newDocumentBuilder().parse(InputSource(StringReader(xml)))
    } catch (_: Exception) {
        return null
    }
    val components = doc.getElementsByTagName("component")
    for (i in 0 until components.length) {
        val c = components.item(i) as Element
        if (c.getAttribute("name") != "PublishConfigData") continue
        val auto = c.getAttribute("autoUpload").takeIf { it.isNotBlank() && it != "Never" }
        val server = c.getAttribute("serverName").takeIf { it.isNotBlank() }
        val mappings = c.getElementsByTagName("mapping")
        val dirs = (0 until mappings.length).mapNotNull { (mappings.item(it) as Element).getAttribute("deploy").takeIf { d -> d.isNotBlank() } }
        return DeploymentInfo(auto, server, dirs)
    }
    return null
}

/**
 * The warning for PyCharm's automatic upload next to calu's synchronisation.
 *
 * @param info the Deployment settings.
 * @param remoteDir the remote project directory of the plugin's settings.
 * @return the text, or null if PyCharm does not upload by itself into that directory (an
 *   automatic upload whose mappings name other directories only is not a second writer).
 */
fun deploymentWarning(info: DeploymentInfo?, remoteDir: String): String? {
    if (info?.autoUpload == null) return null
    val target = remoteDir.trimEnd('/')
    val overlaps = info.remoteDirs.isEmpty() || info.remoteDirs.any { d ->
        val dir = d.trimEnd('/')
        dir == target || target.startsWith("$dir/") || dir.startsWith("$target/")
    }
    if (!overlaps) return null
    return "PyCharm's Deployment uploads changed files by itself (\"${info.autoUpload}\"" +
        (info.serverName?.let { " to $it" } ?: "") + ") into $remoteDir, which calu synchronises too. " +
        "Two writers can overwrite each other's changes. Turn one off: Settings | Build, Execution, Deployment | " +
        "Deployment | Options, \"Upload changed files automatically to the default server\" = Never, or calu's Sync = None."
}
