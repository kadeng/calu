/**
 * The user's saves in the editor, sent to the attached session for the recorded human diffs
 * (agent/records/PLAN.md D253). The same in all three modes: local, WSL, and SSH, where the
 * save goes to the local copy and the synchronisation uploads it separately.
 *
 *     fileContentLoaded / fileContentReloaded ──► SaveBases: base = the document's text
 *     FileDocumentManager.beforeDocumentSaving(document)   (the base becomes the saved text)
 *        ├─ a save of the synchronisation itself (OwnSaves) ──► nothing
 *        ├─ no wiring, no attached session, gate paused ─────► nothing
 *        └─ before: the base before this save (if unknown: the file as the VFS holds it),
 *           after: the document's text ──► session.call human_diffs.add {path, before, after}
 *
 * The base is the text the user's buffer started from, so a save that overwrites a version
 * Claude wrote meanwhile ("Keep Memory Changes" in the IDE's conflict dialog) records only the
 * user's own edits; after "Load File System Changes" the base is Claude's version.
 *
 * Claude's edits, other programs and the terminal change the file on disk; the IDE reloads it
 * without a save, so they never reach this listener. The logic without the IDE is in
 * `logic/HumanDiffs.kt`.
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.editor.Document
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileDocumentManagerListener
import com.intellij.openapi.project.ProjectManager
import com.intellij.openapi.project.guessProjectDir
import com.intellij.openapi.vfs.VfsUtilCore
import com.intellij.openapi.vfs.VirtualFile
import io.github.kadeng.calu.logic.HUMAN_DIFF_MAX_CHARS
import io.github.kadeng.calu.logic.SaveBases
import io.github.kadeng.calu.logic.humanDiffEnabled
import io.github.kadeng.calu.logic.humanDiffParams
import io.github.kadeng.calu.state.isAttached
import java.util.Collections
import java.util.WeakHashMap

/** The documents the synchronisation of the SSH mode is saving right now: not the user's saves. */
object OwnSaves {
    private val saving: MutableSet<Document> = Collections.synchronizedSet(Collections.newSetFromMap(WeakHashMap()))

    /**
     * Run a save of the synchronisation; a save of `document` inside `block` is not the user's.
     *
     * @param document the document the block saves.
     * @param block the save.
     */
    fun <T> during(document: Document, block: () -> T): T {
        saving.add(document)
        try {
            return block()
        } finally {
            saving.remove(document)
        }
    }

    /**
     * Tell whether the synchronisation is saving a document.
     *
     * @param document the document.
     * @return true while `during` runs for it.
     */
    fun contains(document: Document): Boolean = saving.contains(document)
}

/** The text each loaded document started from (one tracker for the application). */
private val saveBases = SaveBases<Document>()

/** Sends each save of a project file in the editor to the project's attached session. */
class HumanDiffSaveListener : FileDocumentManagerListener {
    override fun fileContentLoaded(file: VirtualFile, document: Document) {
        saveBases.loaded(document, document.immutableCharSequence)
    }

    override fun fileContentReloaded(file: VirtualFile, document: Document) {
        saveBases.loaded(document, document.immutableCharSequence)
    }

    override fun beforeDocumentSaving(document: Document) {
        // Every save moves the base, also one that is not sent (no session, feature off).
        val base = saveBases.saving(document, document.immutableCharSequence)
        if (OwnSaves.contains(document)) return
        val file = FileDocumentManager.getInstance().getFile(document) ?: return
        if (!file.isInLocalFileSystem || file.length > HUMAN_DIFF_MAX_CHARS) return
        for (project in ProjectManager.getInstance().openProjects) {
            if (project.isDisposed) continue
            val root = project.guessProjectDir() ?: continue
            if (!VfsUtilCore.isAncestor(root, file, true)) continue
            val service = CaluProjectService.of(project)
            val wiring = service.currentWiring() ?: return
            val now = System.currentTimeMillis()
            if (!service.humanDiffGate.wanted(isAttached(wiring.controller.state), now)) return
            val before = base?.toString() ?: try {
                String(file.contentsToByteArray(), file.charset)
            } catch (_: Exception) {
                return
            }
            val params = humanDiffParams(root.path, file.path, before, document.text) ?: return
            service.launch {
                try {
                    val answer = wiring.supervisor.session("human_diffs.add", params, interactive = false)
                    service.humanDiffGate.heard(humanDiffEnabled(answer), System.currentTimeMillis())
                } catch (error: Exception) {
                    com.intellij.openapi.diagnostic.Logger.getInstance(HumanDiffSaveListener::class.java)
                        .info("[calu] human_diffs.add failed: ${error.message}")
                }
            }
            return
        }
    }
}
