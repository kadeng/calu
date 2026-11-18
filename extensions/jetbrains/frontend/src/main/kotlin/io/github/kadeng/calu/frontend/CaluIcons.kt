package io.github.kadeng.calu.frontend

import com.intellij.openapi.util.IconLoader
import javax.swing.Icon

/** The plugin's icons (calu's own mark, the same drawing as the VSCode extension's). */
object CaluIcons {
    /** The tool window icon: the segmented ring with the check, in one colour (13 px; 20 px in the new UI). */
    @JvmField
    val ToolWindow: Icon = IconLoader.getIcon("/icons/toolWindow.svg", CaluIcons::class.java)
}
