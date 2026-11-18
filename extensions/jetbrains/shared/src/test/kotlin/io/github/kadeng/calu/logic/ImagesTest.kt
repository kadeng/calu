package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ImageView
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.MAX_IMAGE_BASE64
import io.github.kadeng.calu.state.MAX_IMAGE_DATA
import io.github.kadeng.calu.state.UserItem
import io.github.kadeng.calu.state.applyEvent
import io.github.kadeng.calu.state.ev
import io.github.kadeng.calu.state.initialViewState
import io.github.kadeng.calu.state.ja
import io.github.kadeng.calu.state.jo
import kotlinx.serialization.json.JsonNull
import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** Images of chat messages (port of the image tests of `chatModel.test.ts`, `secondRun.test.ts` and `messages.test.ts`). */
class ImagesTest {
    private val mb = 1024L * 1024

    @Test
    fun `accepts raster images within the limits`() {
        assertNull(checkImage("image/png", 1000, 0, true))
        assertTrue(checkImage("image/svg+xml", 1000, 0, true)!!.contains("PNG"))
        assertTrue(checkImage("image/png", 1000, MAX_IMAGES, true)!!.contains(MAX_IMAGES.toString()))
        assertTrue(checkImage("image/png", 8_000_000, 0, true)!!.contains("too large"))
        assertTrue(checkImage("image/png", 1000, 0, false)!!.contains("main agent"))
    }

    @Test
    fun `accepts what a session accepts (5 MB) and refuses more before anything is sent`() {
        assertEquals(4, MAX_IMAGES)
        assertEquals(6_990_508, MAX_IMAGE_BASE64)
        assertNull(checkImage("image/png", 5 * mb, 0, true))
        // base64 counts in groups of 3 bytes.
        assertTrue(checkImage("image/png", 5 * mb + 3, 0, true)!!.contains("too large"))
        assertEquals(
            "The image is too large: 6.0 MB; a message takes images of at most 5 MB. Nothing was attached.",
            checkImage("image/jpeg", 6 * mb, 0, true),
        )
        // The check before sending has the same limit.
        assertNull(checkSend("x", MAIN, listOf(OutgoingImage("A".repeat(MAX_IMAGE_BASE64), "image/png"))))
        assertNotNull(checkSend("x", MAIN, listOf(OutgoingImage("A".repeat(MAX_IMAGE_BASE64 + 4), "image/png"))))
    }

    @Test
    fun `shows every image that could be sent`() {
        assertEquals(MAX_IMAGE_BASE64, MAX_IMAGE_DATA)
        val data = "A".repeat(MAX_IMAGE_BASE64)
        val s = applyEvent(initialViewState(), ev("user.input", "text" to "see", "images" to listOf(mapOf("data" to data, "media_type" to "image/png"))))
        val image = (s.items[MAIN]!![0] as UserItem).images.single()
        assertFalse(image.omitted)
        assertNotNull(imageBytes(image))
    }

    @Test
    fun `parses data URLs and builds safe image sources`() {
        assertEquals(OutgoingImage("aGk=", "image/png"), parseImageDataUrl("data:image/png;base64,aGk="))
        assertNull(parseImageDataUrl("data:image/svg+xml;base64,aGk="))
        assertNull(parseImageDataUrl("data:image/png,plain"))
        assertEquals("data:image/jpeg;base64,aGk=", imageSrc("image/jpeg", "aGk="))
        assertNull(imageSrc("image/svg+xml", "aGk="))
        assertNull(imageSrc("image/png", "\"><script>"))
        assertNull(imageSrc("image/png", ""))
    }

    @Test
    fun `decodes the images of the transcript for thumbnails, and nothing it should not draw`() {
        assertArrayEquals("hi".toByteArray(), imageBytes(ImageView("image/png", "aGk=", false)))
        assertNull(imageBytes(ImageView("image/png", "", true)))
        assertNull(imageBytes(ImageView("image/svg+xml", "aGk=", false)))
        assertNull(imageBytes(ImageView("image/png", "\"><script>", false)))
    }

    @Test
    fun `recognises the four formats by their first bytes`() {
        fun bytes(vararg v: Int) = ByteArray(v.size) { v[it].toByte() }
        assertEquals("image/png", sniffImageType(bytes(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0)))
        assertEquals("image/jpeg", sniffImageType(bytes(0xff, 0xd8, 0xff, 0xe0)))
        assertEquals("image/gif", sniffImageType("GIF89a...".toByteArray()))
        assertEquals("image/webp", sniffImageType("RIFF\u0001\u0002\u0003\u0004WEBPVP8 ".toByteArray(Charsets.ISO_8859_1)))
        assertNull(sniffImageType("<svg xmlns=".toByteArray()))
        assertNull(sniffImageType(ByteArray(0)))
        assertEquals(OutgoingImage("AQID", "image/png"), outgoingImage("image/png", byteArrayOf(1, 2, 3)))
    }

    @Test
    fun `accepts chat messages with and without images`() {
        val image = OutgoingImage("aGk=", "image/png")
        assertNull(checkSend("hello", MAIN))
        assertNull(checkSend("hello", "A1"))
        assertNull(checkSend("", MAIN, listOf(image)))
        assertEquals("Write a message first.", checkSend("  \n", MAIN))
        assertEquals("Images can only be sent to the main agent.", checkSend("x", "A1", listOf(image)))
        assertNotNull(checkSend("x", MAIN, List(MAX_IMAGES + 1) { image }))
        assertNull(checkSend("x", MAIN, List(MAX_IMAGES) { image }))
        assertNotNull(checkSend("x", MAIN, listOf(image.copy(mediaType = "image/svg+xml"))))
        assertNotNull(checkSend("x", MAIN, listOf(image.copy(data = ""))))
        assertNotNull(checkSend("x", MAIN, listOf(image.copy(data = "not base64!"))))
        assertNotNull(checkSend("x".repeat(MAX_TEXT + 1), MAIN))
        assertNull(checkSend("x".repeat(MAX_TEXT), MAIN))
    }

    @Test
    fun `builds the params of chat send and reads the images of the send action`() {
        assertEquals(jo("text" to "hi", "delivery" to "queued", "to" to "main"), chatSendParams("hi", "queued", "main"))
        assertEquals(
            jo("text" to "look", "delivery" to "inject", "to" to "main", "images" to listOf(mapOf("data" to "aGk=", "media_type" to "image/png"))),
            chatSendParams("look", "inject", "main", listOf(OutgoingImage("aGk=", "image/png"))),
        )
        assertEquals(emptyList<OutgoingImage>(), parseOutgoingImages(null))
        assertEquals(emptyList<OutgoingImage>(), parseOutgoingImages(JsonNull))
        assertEquals(
            listOf(OutgoingImage("aGk=", "image/png"), OutgoingImage("aGk=", "image/gif")),
            parseOutgoingImages(ja(mapOf("data" to "aGk=", "mediaType" to "image/png"), mapOf("data" to "aGk=", "media_type" to "image/gif"))),
        )
        assertNull(parseOutgoingImages(ja("x")))
        assertNull(parseOutgoingImages(ja(mapOf("data" to 1, "mediaType" to "image/png"))))
        assertNull(parseOutgoingImages(jo("data" to "aGk=")))
    }
}
