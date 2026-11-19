package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** PyCharm's Deployment settings and the warning about a second writer. */
class DeploymentTest {
    private fun xml(auto: String?) = """<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="PublishConfigData" ${auto?.let { "autoUpload=\"$it\" serverName=\"dev@sshbox:22 agent\"" } ?: ""} remoteFilesAllowedToDisappearOnAutoupload="false">
    <serverData><paths name="dev@sshbox:22 agent"><serverdata><mappings>
      <mapping deploy="/home/dev/project" local="${'$'}PROJECT_DIR${'$'}" />
    </mappings></serverdata></paths></serverData>
  </component>
</project>"""

    @Test fun `the file as PyCharm writes it for an SSH interpreter without automatic upload`() {
        val info = parseDeployment(xml(null))!!
        assertNull(info.autoUpload)
        assertEquals(listOf("/home/dev/project"), info.remoteDirs)
        assertNull(deploymentWarning(info, "/home/dev/project"))
    }

    @Test fun `automatic upload into the synchronised directory is warned about`() {
        val info = parseDeployment(xml("Always"))!!
        val text = deploymentWarning(info, "/home/dev/project/")
        assertNotNull(text)
        assertTrue(text!!.contains("\"Always\" to dev@sshbox:22 agent"))
        assertNotNull(deploymentWarning(parseDeployment(xml("On explicit save action")), "/home/dev"), "a parent directory overlaps too")
        assertNull(deploymentWarning(parseDeployment(xml("Never")), "/home/dev/project"))
        assertNull(deploymentWarning(info, "/srv/other"), "another directory is not a second writer")
    }

    @Test fun `what is not a Deployment file gives nothing, and a DTD is refused`() {
        assertNull(parseDeployment("not xml"))
        assertNull(parseDeployment("<project/>"))
        assertNull(parseDeployment("<!DOCTYPE x [<!ENTITY e SYSTEM \"file:///etc/passwd\">]><project><component name=\"PublishConfigData\" autoUpload=\"&e;\"/></project>"))
        assertNull(deploymentWarning(null, "/x"))
    }
}
