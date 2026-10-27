package dev.marcusson.lasso

import dev.marcusson.lasso.hotspot.Security
import org.json.JSONException
import org.json.JSONObject
import java.util.UUID

object Protocol {
    val SERVICE_UUID: UUID = UUID.fromString("210ce96b-a4ff-4f59-9bd3-b9a8dc850af8")
    val CONTROL_UUID: UUID = UUID.fromString("7470fd02-45dc-4fda-a006-2c2cb7a6426c")
    val EVENTS_UUID: UUID = UUID.fromString("3d83b6c3-38f5-48da-8f70-53b160cebbc9")
    val CCC_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val UDP_PORT = 47123
    const val VERSION = 1
    const val MAX_MESSAGE_BYTES = 180
    const val FIRST_PING_TIMEOUT_MS = 90_000L
    const val PING_TIMEOUT_MS = 180_000L
}

object ErrorCode {
    const val NOT_PAIRED = "NOT_PAIRED"
    const val NO_PRIVILEGE = "NO_PRIVILEGE"
    const val TETHER_FAILED = "TETHER_FAILED"
    const val BUSY = "BUSY"
    const val BAD_REQUEST = "BAD_REQUEST"
}

fun Security.wire(): String = when (this) {
    Security.WPA3_TRANSITION -> "wpa3t"
    Security.WPA3 -> "wpa3"
    Security.WPA2 -> "wpa2"
}

sealed interface Message {
    data class PairReq(val name: String) : Message
    data class Paired(val name: String) : Message
    data object Request : Message
    data object Starting : Message
    data class Ready(
        val ssid: String,
        val psk: String,
        val sec: String,
        val sid: String,
        val port: Int,
        val owned: Boolean,
    ) : Message
    data class ErrorMsg(val code: String) : Message
    data class Release(val sid: String) : Message
    data class Ended(val sid: String, val reason: String) : Message
    data class Ping(val sid: String, val seq: Long) : Message
    data class Pong(val sid: String, val seq: Long) : Message

    /** Well-formed JSON with an unknown type or missing required fields. */
    data class Unknown(val t: String) : Message
}

class MessageTooLargeException(size: Int) : IllegalArgumentException("message is $size bytes")

object Codec {
    private fun obj(t: String) = JSONObject().put("t", t).put("v", Protocol.VERSION)

    fun encode(m: Message): ByteArray {
        val o = when (m) {
            is Message.PairReq -> obj("PAIR").put("name", m.name)
            is Message.Paired -> obj("PAIRED").put("name", m.name)
            Message.Request -> obj("REQUEST")
            Message.Starting -> obj("STARTING")
            is Message.Ready -> obj("READY").put("ssid", m.ssid).put("psk", m.psk).put("sec", m.sec)
                .put("sid", m.sid).put("port", m.port).put("owned", m.owned)
            is Message.ErrorMsg -> obj("ERROR").put("code", m.code)
            is Message.Release -> obj("RELEASE").put("sid", m.sid)
            is Message.Ended -> obj("ENDED").put("sid", m.sid).put("reason", m.reason)
            is Message.Ping -> obj("PING").put("sid", m.sid).put("seq", m.seq)
            is Message.Pong -> obj("PONG").put("sid", m.sid).put("seq", m.seq)
            is Message.Unknown -> throw IllegalArgumentException("cannot encode Unknown")
        }
        val bytes = o.toString().toByteArray(Charsets.UTF_8)
        if (bytes.size > Protocol.MAX_MESSAGE_BYTES) throw MessageTooLargeException(bytes.size)
        return bytes
    }

    fun fits(m: Message): Boolean =
        try { encode(m); true } catch (e: MessageTooLargeException) { false }

    /** Null when the bytes are not a JSON object with a string "t". */
    fun decode(bytes: ByteArray): Message? {
        val o = try {
            JSONObject(String(bytes, Charsets.UTF_8))
        } catch (e: JSONException) {
            return null
        }
        val t = o.opt("t") as? String ?: return null
        if (o.optInt("v", Protocol.VERSION) != Protocol.VERSION) return Message.Unknown(t)

        fun s(k: String) = o.opt(k) as? String
        fun n(k: String) = o.opt(k) as? Number

        val m: Message? = when (t) {
            "PAIR" -> s("name")?.let { Message.PairReq(it) }
            "PAIRED" -> s("name")?.let { Message.Paired(it) }
            "REQUEST" -> Message.Request
            "STARTING" -> Message.Starting
            "READY" -> {
                val ssid = s("ssid"); val psk = s("psk"); val sec = s("sec"); val sid = s("sid")
                val port = n("port"); val owned = o.opt("owned") as? Boolean
                if (ssid != null && psk != null && sec != null && sid != null && port != null && owned != null) {
                    Message.Ready(ssid, psk, sec, sid, port.toInt(), owned)
                } else null
            }
            "ERROR" -> s("code")?.let { Message.ErrorMsg(it) }
            "RELEASE" -> s("sid")?.let { Message.Release(it) }
            "ENDED" -> {
                val sid = s("sid"); val reason = s("reason")
                if (sid != null && reason != null) Message.Ended(sid, reason) else null
            }
            "PING" -> {
                val sid = s("sid"); val seq = n("seq")
                if (sid != null && seq != null) Message.Ping(sid, seq.toLong()) else null
            }
            "PONG" -> {
                val sid = s("sid"); val seq = n("seq")
                if (sid != null && seq != null) Message.Pong(sid, seq.toLong()) else null
            }
            else -> null
        }
        return m ?: Message.Unknown(t)
    }
}
