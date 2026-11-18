package io.github.kadeng.calu.sync

import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.fail
import org.junit.jupiter.api.Test
import kotlin.random.Random

/** [threeWayMerge], its line alignment, [mergeContents] and [isProbablyBinary]. */
class MergeTest {
    /**
     * Merges three texts and expects a clean result.
     *
     * @param base the common ancestor.
     * @param local the local text.
     * @param remote the remote text.
     * @return the merged text; the test fails if the merge has a conflict.
     */
    private fun clean(base: String, local: String, remote: String): String =
        when (val result = threeWayMerge(base.toByteArray(), local.toByteArray(), remote.toByteArray())) {
            is MergeResult.Clean -> String(result.bytes)
            is MergeResult.Conflicted -> fail("unexpected conflict in ${result.regions} region(s)")
        }

    /**
     * Merges three texts and expects a conflict.
     *
     * @param base the common ancestor.
     * @param local the local text.
     * @param remote the remote text.
     * @return the number of conflicting regions; the test fails if the merge is clean.
     */
    private fun conflicts(base: String, local: String, remote: String): Int =
        when (val result = threeWayMerge(base.toByteArray(), local.toByteArray(), remote.toByteArray())) {
            is MergeResult.Clean -> fail("unexpected clean merge: ${String(result.bytes)}")
            is MergeResult.Conflicted -> result.regions
        }

    private val base = "one\ntwo\nthree\nfour\nfive\nsix\n"

    @Test fun `changes in disjoint regions are combined`() {
        assertEquals(
            "ONE\ntwo\nthree\nfour\nfive\nSIX\n",
            clean(base, "ONE\ntwo\nthree\nfour\nfive\nsix\n", "one\ntwo\nthree\nfour\nfive\nSIX\n"),
        )
        assertEquals(
            "one\ntwo\nnew\nthree\nfive\nsix\n",
            clean(base, "one\ntwo\nnew\nthree\nfour\nfive\nsix\n", "one\ntwo\nthree\nfive\nsix\n"),
        )
    }

    @Test fun `a change of one side only is taken`() {
        val changed = "one\n2\nthree\nfour\nsix\nseven\n"
        assertEquals(changed, clean(base, changed, base))
        assertEquals(changed, clean(base, base, changed))
        assertEquals(base, clean(base, base, base))
    }

    @Test fun `overlapping changes conflict`() {
        assertEquals(1, conflicts(base, "one\nTWO\nthree\nfour\nfive\nsix\n", "one\nzwei\nthree\nfour\nfive\nsix\n"))
        assertEquals(
            2,
            conflicts(base, "ONE\ntwo\nthree\nfour\nfive\nSIX\n", "eins\ntwo\nthree\nfour\nfive\nsechs\n"),
        )
    }

    @Test fun `changes of adjacent lines conflict, as in git`() {
        assertEquals(1, conflicts(base, "one\nTWO\nthree\nfour\nfive\nsix\n", "one\ntwo\nTHREE\nfour\nfive\nsix\n"))
    }

    @Test fun `a deletion against a change of the same line conflicts`() {
        assertEquals(1, conflicts(base, "one\nthree\nfour\nfive\nsix\n", "one\nTWO\nthree\nfour\nfive\nsix\n"))
    }

    @Test fun `identical changes on both sides are taken once`() {
        val same = "one\nTWO\nthree\nfour\nfive\nsix\nseven\n"
        assertEquals(same, clean(base, same, same))
        assertEquals(
            "one\nTWO\nthree\nfour\nFIVE\nsix\n",
            clean(base, "one\nTWO\nthree\nfour\nfive\nsix\n", "one\nTWO\nthree\nfour\nFIVE\nsix\n"),
        )
    }

    @Test fun `insertions at the start and at the end are combined`() {
        assertEquals("zero\n$base" + "seven\n", clean(base, "zero\n$base", base + "seven\n"))
        assertEquals("zero\n$base" + "seven\n", clean(base, base + "seven\n", "zero\n$base"))
    }

    @Test fun `different insertions at the same place conflict`() {
        assertEquals(1, conflicts(base, base + "seven\n", base + "sieben\n"))
        assertEquals(1, conflicts(base, "zero\n$base", "null\n$base"))
    }

    @Test fun `empty contents are handled`() {
        assertEquals("", clean("", "", ""))
        assertEquals("new\n", clean("", "new\n", ""))
        assertEquals("new\n", clean("", "", "new\n"))
        assertEquals("same\n", clean("", "same\n", "same\n"))
        assertEquals(1, conflicts("", "a\n", "b\n"))
        assertEquals("", clean(base, "", base))
        assertEquals("", clean(base, "", ""))
        assertEquals(1, conflicts(base, "", "one\nTWO\nthree\nfour\nfive\nsix\n"))
    }

    @Test fun `line ends and a missing final line end are preserved byte for byte`() {
        val crlf = "one\r\ntwo\r\nthree\r\nfour\r\nfive"
        assertEquals(
            "ONE\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\r\n",
            clean(crlf, "ONE\r\ntwo\r\nthree\r\nfour\r\nfive", "one\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\r\n"),
        )
        // One side converts a line end: that is a change of that line, and it is kept.
        assertEquals(
            "one\ntwo\r\nthree\r\nfour\r\nFIVE",
            clean(crlf, "one\ntwo\r\nthree\r\nfour\r\nfive", "one\r\ntwo\r\nthree\r\nfour\r\nFIVE"),
        )
    }

    @Test fun `bytes that are not UTF-8 pass through unchanged`() {
        val odd = byteArrayOf(0xC3.toByte(), 0x28, 0xFF.toByte(), '\n'.code.toByte())
        val baseBytes = "a\n".toByteArray() + odd + "z\n".toByteArray()
        val local = "A\n".toByteArray() + odd + "z\n".toByteArray()
        val remote = "a\n".toByteArray() + odd + "Z\n".toByteArray()
        val result = threeWayMerge(baseBytes, local, remote) as MergeResult.Clean
        assertArrayEquals("A\n".toByteArray() + odd + "Z\n".toByteArray(), result.bytes)
    }

    @Test fun `splitLines keeps every byte`() {
        assertEquals(emptyList<String>(), splitLines(ByteArray(0)))
        assertEquals(listOf("a\n", "\n", "b\r\n", "c"), splitLines("a\n\nb\r\nc".toByteArray()))
        assertEquals(listOf("\n"), splitLines("\n".toByteArray()))
    }

    @Test fun `lcsMatches pairs equal lines in ascending order`() {
        assertArrayEquals(intArrayOf(0, -1, 2), lcsMatches(listOf("a", "b", "c"), listOf("a", "x", "c")))
        assertArrayEquals(intArrayOf(-1, -1), lcsMatches(listOf("a", "b"), listOf("x", "y", "z")))
        assertArrayEquals(intArrayOf(), lcsMatches(emptyList(), listOf("x")))
        assertArrayEquals(intArrayOf(1, 2), lcsMatches(listOf("a", "b"), listOf("x", "a", "b", "y")))
        // "abcabba" against "cbabac": a longest common subsequence has 4 lines.
        val a = "abcabba".map { it.toString() }
        val b = "cbabac".map { it.toString() }
        val match = lcsMatches(a, b)
        val pairs = match.withIndex().filter { it.value >= 0 }
        assertEquals(4, pairs.size)
        assertTrue(pairs.all { a[it.index] == b[it.value] })
        assertEquals(pairs.map { it.value }.sorted(), pairs.map { it.value })
    }

    @Test fun `lcsMatches finds a longest common subsequence on random inputs`() {
        val random = Random(20261118)
        repeat(300) {
            val a = List(random.nextInt(0, 12)) { random.nextInt(0, 4).toString() }
            val b = List(random.nextInt(0, 12)) { random.nextInt(0, 4).toString() }
            val match = lcsMatches(a, b)
            val pairs = match.withIndex().filter { it.value >= 0 }
            assertTrue(pairs.all { a[it.index] == b[it.value] }, "$a $b")
            assertTrue(pairs.zipWithNext().all { (x, y) -> x.value < y.value }, "$a $b")
            assertEquals(lcsLength(a, b), pairs.size, "$a $b")
        }
    }

    @Test fun `merging random one-sided edits returns the edited side`() {
        val random = Random(35)
        repeat(200) {
            val baseText = List(random.nextInt(0, 15)) { "l${random.nextInt(0, 5)}\n" }.joinToString("")
            val edited = List(random.nextInt(0, 15)) { "l${random.nextInt(0, 6)}\n" }.joinToString("")
            assertEquals(edited, clean(baseText, edited, baseText))
            assertEquals(edited, clean(baseText, baseText, edited))
            assertEquals(edited, clean(baseText, edited, edited))
        }
    }

    @Test fun `a large file with distant edits merges`() {
        val lines = List(20_000) { "line $it\n" }
        val local = lines.toMutableList().also { it[10] = "local\n"; it.removeAt(5000) }
        val remote = lines.toMutableList().also { it[19_990] = "remote\n"; it.add(12_000, "inserted\n") }
        val merged = clean(lines.joinToString(""), local.joinToString(""), remote.joinToString(""))
        val expected = lines.toMutableList().also {
            it[19_990] = "remote\n"
            it.add(12_000, "inserted\n")
            it[10] = "local\n"
            it.removeAt(5000)
        }
        assertEquals(expected.joinToString(""), merged)
    }

    @Test fun `binary contents are recognised by a NUL byte in the first 8000 bytes`() {
        assertFalse(isProbablyBinary(ByteArray(0)))
        assertFalse(isProbablyBinary("text\r\n".toByteArray()))
        assertTrue(isProbablyBinary(byteArrayOf(1, 0, 2)))
        assertTrue(isProbablyBinary(ByteArray(8000) { 65 }.also { it[7999] = 0 }))
        assertFalse(isProbablyBinary(ByteArray(8001) { 65 }.also { it[8000] = 0 }))
    }

    @Test fun `mergeContents classifies the outcome`() {
        val merged = mergeContents("a\nb\nc\n".toByteArray(), "A\nb\nc\n".toByteArray(), "a\nb\nC\n".toByteArray())
        assertEquals("A\nb\nC\n", String((merged as MergeOutcome.Merged).bytes))
        assertEquals(
            MergeOutcome.Unresolved(ConflictKind.BOTH_MODIFIED),
            mergeContents("a\n".toByteArray(), "b\n".toByteArray(), "c\n".toByteArray()),
        )
        assertEquals(
            MergeOutcome.Unresolved(ConflictKind.BOTH_MODIFIED),
            mergeContents(null, "b\n".toByteArray(), "c\n".toByteArray()),
        )
        assertEquals(
            MergeOutcome.Unresolved(ConflictKind.BINARY),
            mergeContents("a\n".toByteArray(), byteArrayOf(1, 0), "c\n".toByteArray()),
        )
        assertEquals(
            MergeOutcome.Unresolved(ConflictKind.BINARY),
            mergeContents(null, "b\n".toByteArray(), byteArrayOf(0)),
        )
    }

    @Test fun `sha256Hex matches known values`() {
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", sha256Hex(ByteArray(0)))
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", sha256Hex("abc".toByteArray()))
        assertTrue(isSha256(sha256Hex("abc".toByteArray())))
        assertFalse(isSha256("ABC"))
    }

    /**
     * Length of a longest common subsequence by the textbook table, as the reference for [lcsMatches].
     *
     * @param a the first list.
     * @param b the second list.
     * @return the length.
     */
    private fun lcsLength(a: List<String>, b: List<String>): Int {
        val table = Array(a.size + 1) { IntArray(b.size + 1) }
        for (i in a.indices) {
            for (j in b.indices) {
                table[i + 1][j + 1] = if (a[i] == b[j]) table[i][j] + 1 else maxOf(table[i][j + 1], table[i + 1][j])
            }
        }
        return table[a.size][b.size]
    }
}
