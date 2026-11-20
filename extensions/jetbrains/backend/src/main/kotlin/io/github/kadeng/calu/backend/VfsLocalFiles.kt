/**
 * The local project copy of the SSH mode as the IDE holds it: files on disk plus the editors'
 * documents.
 *
 *     write   file open in an editor ──► document.setText in one undoable command, saved
 *             (if the saved bytes differ from the wanted ones, e.g. other line ends: the bytes
 *             are written to the file and the document is reloaded)
 *             otherwise ──► the file is replaced on disk and the VFS is refreshed
 *     delete  the file is removed on disk and the VFS is refreshed
 *     unsaved the document's text as a save would write it, if it differs from the file
 *
 * Listing, hashing and reading come from `DiskLocalFiles` (the file on disk is the local side;
 * unsaved text is reported separately).
 */
package io.github.kadeng.calu.backend

import com.intellij.openapi.application.EDT
import com.intellij.openapi.application.readAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.editor.Document
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.text.StringUtil
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import io.github.kadeng.calu.sync.DiskLocalFiles
import io.github.kadeng.calu.sync.isProbablyBinary
import io.github.kadeng.calu.sync.sha256Hex
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.nio.file.Files
import java.nio.file.Path

/**
 * [io.github.kadeng.calu.sync.LocalFiles] over the IDE's project.
 *
 * @param project the project.
 * @param root the project directory.
 */
class VfsLocalFiles(private val project: Project, root: Path) : DiskLocalFiles(root) {
    /** While a batch runs: true, and the VFS is refreshed once at its end instead of per file. */
    @Volatile private var deferred = false

    /**
     * Run a batch of writes (a clone, a full comparison) with one VFS refresh at its end. A
     * refresh per file costs about 20 ms each, which is most of the time of a first copy.
     * Files that are open in an editor are still changed through their documents at once.
     *
     * @param block the batch.
     */
    suspend fun batched(block: suspend () -> Unit) {
        deferred = true
        try {
            block()
        } finally {
            deferred = false
            withContext(Dispatchers.IO) {
                LocalFileSystem.getInstance().refreshAndFindFileByNioFile(root)?.let { com.intellij.openapi.vfs.VfsUtil.markDirtyAndRefresh(false, true, true, it) }
            }
        }
    }

    /**
     * Find the open document of a file.
     *
     * @param path relative path.
     * @return the virtual file and its document if an editor (or another part of the IDE) has
     *   the file loaded; null if not.
     */
    private suspend fun loaded(path: String): Pair<VirtualFile, Document>? {
        val file = try {
            resolve(path)
        } catch (_: java.io.IOException) {
            return null
        }
        val vf = LocalFileSystem.getInstance().findFileByNioFile(file) ?: return null
        return readAction { FileDocumentManager.getInstance().getCachedDocument(vf)?.let { vf to it } }
    }

    override suspend fun write(path: String, bytes: ByteArray, mode: Int?) {
        val file = resolve(path)
        val open = loaded(path)
        if (open != null && !isProbablyBinary(bytes)) {
            val (vf, document) = open
            val text = StringUtil.convertLineSeparators(String(bytes, vf.charset))
            withContext(Dispatchers.EDT) {
                WriteCommandAction.runWriteCommandAction(project, "calu Sync: ${path.substringAfterLast('/')}", null, {
                    document.setText(text)
                    // Not the user's save: it is left out of the recorded human diffs (D253).
                    OwnSaves.during(document) { FileDocumentManager.getInstance().saveDocument(document) }
                })
            }
            val saved = withContext(Dispatchers.IO) { runCatching { Files.readAllBytes(file) }.getOrNull() }
            if (saved != null && sha256Hex(saved) == sha256Hex(bytes)) {
                if (mode != null) applyMode(file, mode)
                return
            }
        }
        withContext(Dispatchers.IO) { super.write(path, bytes, mode) }
        refresh(file)
        if (open != null) {
            withContext(Dispatchers.EDT) {
                com.intellij.openapi.application.runWriteAction { FileDocumentManager.getInstance().reloadFromDisk(open.second) }
            }
        }
    }

    override suspend fun delete(path: String) {
        val file = resolve(path)
        withContext(Dispatchers.IO) { super.delete(path) }
        refresh(file)
    }

    override suspend fun unsaved(path: String): ByteArray? {
        val (vf, document) = loaded(path) ?: return null
        return readAction {
            val manager = FileDocumentManager.getInstance()
            if (!manager.isDocumentUnsaved(document)) return@readAction null
            val separator = manager.getLineSeparator(vf, project)
            StringUtil.convertLineSeparators(document.text, separator).toByteArray(vf.charset)
        }
    }

    /**
     * Tell the VFS that a path changed on disk, so that the project view and open editors follow.
     *
     * @param file the absolute path (it may no longer exist).
     */
    private suspend fun refresh(file: Path) {
        if (deferred) return
        withContext(Dispatchers.IO) {
            var at: Path? = file
            // The nearest path that exists is refreshed with its children: a new file in a new
            // directory, or a directory that vanished with its last file.
            while (at != null && !Files.exists(at) && at != root) at = at.parent
            val fs = LocalFileSystem.getInstance()
            fs.refreshAndFindFileByNioFile(file)
            if (at != null) fs.refreshNioFiles(listOf(at, file))
        }
    }
}
