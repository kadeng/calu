/**
 * Images attached to a chat message (pure): the limits, the checks before an image is attached
 * and before a message is sent, and the shape a session takes.
 *
 *     a file, a pasted or a dropped image
 *        ──sniffImageType(bytes) / the file's type──► media type
 *        ──checkImage(type, size, attached, toMain)──► null (may be attached) or the reason
 *        ──outgoingImage(type, bytes)──► OutgoingImage {data: base64, mediaType}
 *     Enter
 *        ──checkSend(text, to, images)──► null or the reason
 *        ──chatSendParams(text, delivery, to, images)──► params of `chat.send`
 *                                                        {text, delivery, to, images: [{data, media_type}]}
 *     transcript
 *        UserItem.images: [ImageView {mediaType, data (base64), omitted}]
 *        ──imageBytes(view)──► the image file's bytes for a thumbnail, or null
 *
 * Limits (the session's own, `calu.protocol.messages`):
 *
 * - At most `MAX_IMAGES` images per message.
 * - Each image is at most 5 MiB (`MAX_IMAGE_BASE64` characters of base64).
 * - Only PNG, JPEG, GIF and WebP. An SVG can carry scripts and is refused.
 * - Images only in a message to the main agent.
 *
 * Port of the image parts of `extensions/vscode/src/shared/messages.ts` and `chatModel.ts`.
 */
package io.github.kadeng.calu.logic

import io.github.kadeng.calu.state.ImageView
import io.github.kadeng.calu.state.MAIN
import io.github.kadeng.calu.state.MAX_IMAGE_BASE64
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import java.util.Base64
import java.util.Locale

/** Raster formats an attached image may have (an SVG can carry scripts and is refused). */
val IMAGE_MEDIA_TYPES: List<String> = listOf("image/png", "image/jpeg", "image/gif", "image/webp")

/** Most images per chat message (limit of `chat.send`). */
const val MAX_IMAGES: Int = 4

/** Largest attached image in bytes: 5 MiB (`MAX_IMAGE_BYTES` in `calu.protocol.messages`). */
const val MAX_IMAGE_BYTES: Long = 5L * 1024 * 1024

/** Longest chat message, side question, note or answer a front end sends, in characters. */
const val MAX_TEXT: Int = 200_000

/**
 * An image attached to a chat message.
 *
 * @property data The image file, base64 without a `data:` prefix.
 * @property mediaType One of `IMAGE_MEDIA_TYPES`.
 */
@Serializable
data class OutgoingImage(val data: String, val mediaType: String)

private val BASE64_TEXT = Regex("^[A-Za-z0-9+/]+={0,2}$")

/**
 * Tell whether a media type is one of the raster formats that may be attached or shown.
 *
 * @param mediaType Any string.
 * @return True for PNG, JPEG, GIF and WebP (exact, lower-case names).
 */
fun isImageType(mediaType: String): Boolean = mediaType in IMAGE_MEDIA_TYPES

/**
 * Check whether an image may be attached to the message being written.
 *
 * @param mediaType The file's media type.
 * @param bytes The file's size in bytes.
 * @param attached Number of images already attached.
 * @param toMain True if the message goes to the main agent.
 * @return `null` if the image may be attached, else the reason as a sentence.
 */
fun checkImage(mediaType: String, bytes: Long, attached: Int, toMain: Boolean): String? {
    if (!toMain) return "Images can only be sent to the main agent."
    if (!isImageType(mediaType)) return "Only PNG, JPEG, GIF and WebP images can be attached."
    if (attached >= MAX_IMAGES) return "At most $MAX_IMAGES images fit into one message."
    // base64 needs 4 characters per 3 bytes.
    if ((bytes + 2) / 3 * 4 > MAX_IMAGE_BASE64) {
        val size = String.format(Locale.ROOT, "%.1f", bytes / (1024.0 * 1024.0))
        val limit = MAX_IMAGE_BASE64 / 4L * 3 / (1024 * 1024)
        return "The image is too large: $size MB; a message takes images of at most $limit MB. Nothing was attached."
    }
    return null
}

/**
 * Recognise the raster format of an image file from its first bytes (a pasted or dropped image
 * often has no reliable name or type).
 *
 * @param bytes The file's content, or at least its first 12 bytes.
 * @return "image/png", "image/jpeg", "image/gif" or "image/webp"; `null` for anything else.
 */
fun sniffImageType(bytes: ByteArray): String? {
    fun at(i: Int): Int = if (i < bytes.size) bytes[i].toInt() and 0xff else -1
    fun ascii(from: Int, text: String): Boolean = text.indices.all { at(from + it) == text[it].code }
    return when {
        at(0) == 0x89 && ascii(1, "PNG") && at(4) == 0x0d && at(5) == 0x0a && at(6) == 0x1a && at(7) == 0x0a -> "image/png"
        at(0) == 0xff && at(1) == 0xd8 && at(2) == 0xff -> "image/jpeg"
        ascii(0, "GIF87a") || ascii(0, "GIF89a") -> "image/gif"
        ascii(0, "RIFF") && ascii(8, "WEBP") -> "image/webp"
        else -> null
    }
}

/**
 * Build the attachment of an image file.
 *
 * @param mediaType The file's media type (checked by the caller with `checkImage`).
 * @param bytes The file's content.
 * @return The attachment with the content as base64.
 */
fun outgoingImage(mediaType: String, bytes: ByteArray): OutgoingImage = OutgoingImage(Base64.getEncoder().encodeToString(bytes), mediaType)

private val DATA_URL = Regex("^data:([a-z0-9.+/-]+);base64,([A-Za-z0-9+/]+={0,2})$", RegexOption.IGNORE_CASE)

/**
 * Split a `data:` URL of an image into its parts.
 *
 * @param dataUrl e.g. "data:image/png;base64,iVBOR…".
 * @return The attachment (media type in lower case, base64 text), or `null` if the URL is not
 *   a base64 data URL of an allowed image type.
 */
fun parseImageDataUrl(dataUrl: String): OutgoingImage? {
    val m = DATA_URL.find(dataUrl) ?: return null
    val type = m.groupValues[1].lowercase(Locale.ROOT)
    return if (isImageType(type)) OutgoingImage(m.groupValues[2], type) else null
}

/**
 * Build the `data:` URL of an image of the transcript (for a surface that draws HTML).
 *
 * @param mediaType The image's media type (from calu, untrusted).
 * @param data The image file, base64.
 * @return A `data:` URL, or `null` if the type is not an allowed raster format or the data is
 *   not base64 (the caller then shows a placeholder).
 */
fun imageSrc(mediaType: String, data: String): String? =
    if (!isImageType(mediaType) || data.isEmpty() || !BASE64_TEXT.matches(data)) null else "data:$mediaType;base64,$data"

/**
 * Decode an image of the transcript for a thumbnail.
 *
 * @param image An image of `UserItem.images` (from calu, untrusted).
 * @return The image file's bytes; `null` if the image was omitted because of its size, its
 *   type is not an allowed raster format, or its data is not base64 (the caller then shows a
 *   placeholder: "[image too large to show]" for `omitted`, else "[image]").
 */
fun imageBytes(image: ImageView): ByteArray? {
    if (image.omitted || !isImageType(image.mediaType) || image.data.isEmpty() || !BASE64_TEXT.matches(image.data)) return null
    return runCatching { Base64.getDecoder().decode(image.data) }.getOrNull()
}

/**
 * Check the images of a message that is about to be sent (the rules of the session, applied
 * before anything is sent).
 *
 * @param images The attached images.
 * @return `null` if a session accepts them, else the reason as a sentence: more than
 *   `MAX_IMAGES`, a type outside `IMAGE_MEDIA_TYPES`, empty data, data that is not base64, or
 *   data longer than `MAX_IMAGE_BASE64` characters.
 */
fun checkOutgoingImages(images: List<OutgoingImage>): String? {
    if (images.size > MAX_IMAGES) return "At most $MAX_IMAGES images fit into one message."
    for (image in images) {
        if (!isImageType(image.mediaType)) return "Only PNG, JPEG, GIF and WebP images can be attached."
        if (image.data.isEmpty() || !BASE64_TEXT.matches(image.data)) return "An attached image could not be read."
        if (image.data.length > MAX_IMAGE_BASE64) {
            return "An attached image is too large; a message takes images of at most ${MAX_IMAGE_BYTES / (1024 * 1024)} MB."
        }
    }
    return null
}

/**
 * Check a chat message before it is sent.
 *
 *     no text (only white space) and no image    -> "Write a message first."
 *     text longer than MAX_TEXT                  -> refused
 *     images in a message to a subagent          -> "Images can only be sent to the main agent."
 *     an image a session would refuse            -> the reason of `checkOutgoingImages`
 *
 * @param text The message text; may be empty if there is an image.
 * @param to "main" or the id of a running background subagent.
 * @param images The attached images.
 * @return `null` if the message can be sent, else the reason as a sentence.
 */
fun checkSend(text: String, to: String, images: List<OutgoingImage> = emptyList()): String? {
    if (text.isBlank() && images.isEmpty()) return "Write a message first."
    if (text.length > MAX_TEXT) return "The message is too long: at most $MAX_TEXT characters fit into one message."
    if (images.isNotEmpty() && to != MAIN) return "Images can only be sent to the main agent."
    return checkOutgoingImages(images)
}

/**
 * Build the params of `chat.send`.
 *
 * Example: `chatSendParams("look", "queued", "main", listOf(OutgoingImage("aGk=", "image/png")))`
 * is `{"text":"look","delivery":"queued","to":"main","images":[{"data":"aGk=","media_type":"image/png"}]}`.
 *
 * @param text The message text.
 * @param delivery "queued" (the next turn) or "inject" (into the running turn).
 * @param to "main" or the id of a running background subagent.
 * @param images The attached images (checked with `checkSend`); the member `images` is left
 *   out if there is none.
 * @return The params.
 */
fun chatSendParams(text: String, delivery: String, to: String, images: List<OutgoingImage> = emptyList()): JsonObject = buildJsonObject {
    put("text", JsonPrimitive(text))
    put("delivery", JsonPrimitive(delivery))
    put("to", JsonPrimitive(to))
    if (images.isNotEmpty()) {
        put("images", buildJsonArray {
            for (i in images) add(buildJsonObject { put("data", JsonPrimitive(i.data)); put("media_type", JsonPrimitive(i.mediaType)) })
        })
    }
}

/**
 * Read the `images` argument of the chat action "send".
 *
 * @param raw The argument: missing, `null`, or an array of `{data, mediaType}` objects
 *   (`media_type` is accepted as well).
 * @return The images, unchecked (see `checkSend`); `null` if `raw` is neither missing nor an
 *   array of objects with the two string members.
 */
fun parseOutgoingImages(raw: JsonElement?): List<OutgoingImage>? {
    if (raw == null || raw is JsonNull) return emptyList()
    val array = raw as? JsonArray ?: return null
    return array.map { entry ->
        val o = entry as? JsonObject ?: return null
        val data = (o["data"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        val type = ((o["mediaType"] ?: o["media_type"]) as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        OutgoingImage(data, type)
    }
}
