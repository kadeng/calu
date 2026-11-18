package io.github.kadeng.calu.frontend

import com.intellij.ui.SimpleListCellRenderer
import javax.swing.ListCellRenderer

/**
 * Create a list renderer that shows one line of text per value.
 *
 * @param text Returns the line of a value.
 * @return The renderer; a `null` value is shown as an empty line.
 */
fun <T> textRenderer(text: (T) -> String): ListCellRenderer<T?> =
    SimpleListCellRenderer.create { label, value, _ -> label.text = if (value == null) "" else text(value) }
