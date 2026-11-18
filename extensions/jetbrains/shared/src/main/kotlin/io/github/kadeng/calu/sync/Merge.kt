/**
 * Three-way text merge (line-based diff3) and the binary check of the file synchronisation.
 *
 * Contents are byte arrays and stay byte-exact: a line is a run of bytes up to and including its
 * line feed, so "\r\n" line ends, a missing final line end and bytes that are not valid UTF-8
 * are carried through unchanged.
 */
package io.github.kadeng.calu.sync

/** The result of [threeWayMerge]. */
sealed interface MergeResult {
    /**
     * All changes of both sides were combined.
     *
     * @property bytes the merged content.
     */
    class Clean(val bytes: ByteArray) : MergeResult

    /**
     * At least one region was changed differently by both sides; nothing was merged.
     *
     * @property regions number of such regions (at least 1).
     */
    data class Conflicted(val regions: Int) : MergeResult
}

/** The result of [mergeContents]. */
sealed interface MergeOutcome {
    /**
     * The merge succeeded.
     *
     * @property bytes the merged content, to be written to both sides.
     */
    class Merged(val bytes: ByteArray) : MergeOutcome

    /**
     * The user has to decide.
     *
     * @property kind [ConflictKind.BINARY] or [ConflictKind.BOTH_MODIFIED].
     */
    data class Unresolved(val kind: ConflictKind) : MergeOutcome
}

/** How many leading bytes [isProbablyBinary] inspects. */
const val BINARY_PROBE_BYTES: Int = 8000

/**
 * Guesses whether a content is binary, by the rule git uses. No side effects.
 *
 * @param bytes the content.
 * @return true if one of the first [BINARY_PROBE_BYTES] bytes is a NUL byte; false for an empty
 *   content.
 */
fun isProbablyBinary(bytes: ByteArray): Boolean {
    val end = minOf(bytes.size, BINARY_PROBE_BYTES)
    for (i in 0 until end) {
        if (bytes[i] == 0.toByte()) return true
    }
    return false
}

/**
 * Merges the contents of a [SyncAction.Merge], or says which conflict it is. No side effects.
 *
 * @param base the last synchronised content, or null if the caller no longer has it.
 * @param local the local content.
 * @param remote the remote content.
 * @return [MergeOutcome.Merged] if [threeWayMerge] is clean; `Unresolved(BINARY)` if one of the
 *   three contents is binary; `Unresolved(BOTH_MODIFIED)` if the base is missing or the changes
 *   overlap.
 */
fun mergeContents(base: ByteArray?, local: ByteArray, remote: ByteArray): MergeOutcome {
    if (isProbablyBinary(local) || isProbablyBinary(remote) || (base != null && isProbablyBinary(base))) {
        return MergeOutcome.Unresolved(ConflictKind.BINARY)
    }
    if (base == null) return MergeOutcome.Unresolved(ConflictKind.BOTH_MODIFIED)
    return when (val merged = threeWayMerge(base, local, remote)) {
        is MergeResult.Clean -> MergeOutcome.Merged(merged.bytes)
        is MergeResult.Conflicted -> MergeOutcome.Unresolved(ConflictKind.BOTH_MODIFIED)
    }
}

/**
 * Merges two contents that both derive from [base], line by line (diff3). No side effects.
 *
 * The base is aligned with each side by a longest common subsequence of lines. A base line that
 * both sides kept separates two regions. In each region between such lines:
 *
 *     local == remote          -> that content (both made the same change)
 *     local == base            -> remote (only the remote side changed)
 *     remote == base           -> local (only the local side changed)
 *     otherwise                -> conflict
 *
 * Changes of the two sides that touch adjacent lines without an unchanged line between them
 * are one region and therefore a conflict, as in `git merge-file`.
 *
 * Example: base "a\nb\nc\n", local "A\nb\nc\n", remote "a\nb\nC\n" gives "A\nb\nC\n".
 *
 * @param base the common ancestor; empty for a file without content.
 * @param local the content of the local side.
 * @param remote the content of the remote side.
 * @return [MergeResult.Clean] with the merged bytes, or [MergeResult.Conflicted]. If
 *   `local == base` the result is `remote` byte for byte, and the reverse.
 */
fun threeWayMerge(base: ByteArray, local: ByteArray, remote: ByteArray): MergeResult {
    val b = splitLines(base)
    val l = splitLines(local)
    val r = splitLines(remote)
    val toLocal = lcsMatches(b, l)
    val toRemote = lcsMatches(b, r)
    val out = ArrayList<String>(maxOf(l.size, r.size))
    var conflicts = 0
    var ib = 0
    var il = 0
    var ir = 0
    while (true) {
        var next = ib
        while (next < b.size && (toLocal[next] < 0 || toRemote[next] < 0)) next += 1
        val endLocal = if (next < b.size) toLocal[next] else l.size
        val endRemote = if (next < b.size) toRemote[next] else r.size
        val baseRegion = b.subList(ib, next)
        val localRegion = l.subList(il, endLocal)
        val remoteRegion = r.subList(ir, endRemote)
        when {
            localRegion == remoteRegion -> out.addAll(localRegion)
            localRegion == baseRegion -> out.addAll(remoteRegion)
            remoteRegion == baseRegion -> out.addAll(localRegion)
            else -> conflicts += 1
        }
        if (next >= b.size) break
        out.add(b[next])
        ib = next + 1
        il = endLocal + 1
        ir = endRemote + 1
    }
    if (conflicts > 0) return MergeResult.Conflicted(conflicts)
    return MergeResult.Clean(out.joinToString("").toByteArray(Charsets.ISO_8859_1))
}

/**
 * Splits a content into lines without losing a byte. No side effects.
 *
 * @param bytes the content.
 * @return the lines, each including its "\n" (a preceding "\r" stays part of the line); a last
 *   line without "\n" is kept as it is; an empty content gives no line. Each byte is one
 *   character (ISO-8859-1), so joining the lines and encoding them the same way returns [bytes].
 */
internal fun splitLines(bytes: ByteArray): List<String> {
    val text = String(bytes, Charsets.ISO_8859_1)
    val lines = ArrayList<String>()
    var start = 0
    while (start < text.length) {
        val lf = text.indexOf('\n', start)
        val end = if (lf < 0) text.length else lf + 1
        lines.add(text.substring(start, end))
        start = end
    }
    return lines
}

/** Above this edit distance of the differing middle part, [lcsMatches] stops searching. */
private const val MAX_EDIT_DISTANCE = 2000

/**
 * Aligns two line lists by a common subsequence (Myers' O(ND) algorithm). No side effects.
 *
 * @param a the first list.
 * @param b the second list.
 * @return an array `m` of size `a.size`: `m[i]` is the index in [b] of the line that `a[i]` is
 *   matched with, or -1. Matched pairs are equal lines and strictly ascending in both lists.
 *   The alignment is a longest common subsequence unless the parts between the common prefix and
 *   the common suffix differ by more than [MAX_EDIT_DISTANCE] line edits; then only prefix and
 *   suffix are matched, which is a valid, coarser alignment.
 */
internal fun lcsMatches(a: List<String>, b: List<String>): IntArray {
    val match = IntArray(a.size) { -1 }
    var prefix = 0
    while (prefix < a.size && prefix < b.size && a[prefix] == b[prefix]) {
        match[prefix] = prefix
        prefix += 1
    }
    var suffix = 0
    while (suffix < a.size - prefix && suffix < b.size - prefix &&
        a[a.size - 1 - suffix] == b[b.size - 1 - suffix]
    ) {
        match[a.size - 1 - suffix] = b.size - 1 - suffix
        suffix += 1
    }
    val n = a.size - prefix - suffix
    val m = b.size - prefix - suffix
    if (n == 0 || m == 0) return match

    val limit = minOf(n + m, MAX_EDIT_DISTANCE)
    val offset = limit + 1
    val v = IntArray(2 * limit + 3)
    val trace = ArrayList<IntArray>()
    var found = -1
    var d = 0
    while (d <= limit && found < 0) {
        trace.add(v.copyOfRange(offset - d, offset + d + 1))
        var k = -d
        while (k <= d) {
            var x = if (k == -d || (k != d && v[offset + k - 1] < v[offset + k + 1])) {
                v[offset + k + 1]
            } else {
                v[offset + k - 1] + 1
            }
            var y = x - k
            while (x < n && y < m && a[prefix + x] == b[prefix + y]) {
                x += 1
                y += 1
            }
            v[offset + k] = x
            if (x >= n && y >= m) {
                found = d
                break
            }
            k += 2
        }
        d += 1
    }
    if (found < 0) return match

    var x = n
    var y = m
    for (step in found downTo 1) {
        val before = trace[step] // v as it was before round `step`; index k + step
        val k = x - y
        val prevK = if (k == -step || (k != step && before[k - 1 + step] < before[k + 1 + step])) k + 1 else k - 1
        val prevX = before[prevK + step]
        val prevY = prevX - prevK
        while (x > prevX && y > prevY) {
            x -= 1
            y -= 1
            match[prefix + x] = prefix + y
        }
        x = prevX
        y = prevY
    }
    while (x > 0 && y > 0) {
        x -= 1
        y -= 1
        match[prefix + x] = prefix + y
    }
    return match
}
