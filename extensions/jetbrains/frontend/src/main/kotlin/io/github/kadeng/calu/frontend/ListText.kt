package io.github.kadeng.calu.frontend

import com.intellij.ui.ColoredListCellRenderer
import javax.swing.JList
import javax.swing.ListCellRenderer

/**
 * Create a list renderer that shows one line of text per value.
 *
 * @param text Returns the line of a value.
 * @return The renderer; a `null` value is shown as an empty line.
 */
fun <T> textRenderer(text: (T) -> String): ListCellRenderer<T?> = object : ColoredListCellRenderer<T?>() {
    override fun customizeCellRenderer(list: JList<out T?>, value: T?, index: Int, selected: Boolean, hasFocus: Boolean) {
        append(if (value == null) "" else text(value))
    }
}
