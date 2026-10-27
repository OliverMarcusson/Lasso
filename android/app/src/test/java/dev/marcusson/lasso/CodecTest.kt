package dev.marcusson.lasso

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CodecTest {
    private val sid = "0123456789abcdef0123456789abcdef"

    private fun roundTrip(m: Message) = assertEquals(m, Codec.decode(Codec.encode(m)))

    @Test fun roundTripsEveryMessage() {
        roundTrip(Message.PairReq("laptop"))
        roundTrip(Message.Paired("Galaxy S25 Ultra"))
        roundTrip(Message.Request)
        roundTrip(Message.Starting)
        roundTrip(Message.Ready("Lasso-ab12cd", "A".repeat(24), "wpa3t", sid, 47123, true))
        roundTrip(Message.ErrorMsg("BUSY"))
        roundTrip(Message.Release(sid))
        roundTrip(Message.Ended(sid, "no_join"))
        roundTrip(Message.Ping(sid, 7))
        roundTrip(Message.Pong(sid, 7))
    }

    @Test fun readyFitsInOneGattMessage() {
        val m = Message.Ready("Lasso-ab12cd", "A".repeat(24), "wpa3t", sid, 47123, false)
        assertTrue(Codec.encode(m).size <= Protocol.MAX_MESSAGE_BYTES)
    }

    @Test fun encodedMessageHasNoWhitespaceAndCarriesTypeAndVersion() {
        val s = String(Codec.encode(Message.Ping(sid, 1)))
        assertFalse(s.contains(' ') || s.contains('\n'))
        assertTrue(s.contains("\"t\":\"PING\"") && s.contains("\"v\":1"))
    }

    @Test fun oversizedMessageIsRejected() {
        val big = Message.Ready("s".repeat(32), "p".repeat(63), "wpa3t", sid, 47123, false)
        assertFalse(Codec.fits(big))
        try {
            Codec.encode(big)
            org.junit.Assert.fail("expected MessageTooLargeException")
        } catch (e: MessageTooLargeException) {
            // expected
        }
    }

    @Test fun unknownFieldsAreIgnored() {
        val m = Codec.decode("""{"t":"RELEASE","v":1,"sid":"$sid","extra":[1,2]}""".toByteArray())
        assertEquals(Message.Release(sid), m)
    }

    @Test fun unknownTypeAndMissingFieldsBecomeUnknown() {
        assertEquals(Message.Unknown("FROB"), Codec.decode("""{"t":"FROB","v":1}""".toByteArray()))
        assertEquals(Message.Unknown("PING"), Codec.decode("""{"t":"PING","v":1,"sid":"$sid"}""".toByteArray()))
        assertEquals(Message.Unknown("REQUEST"), Codec.decode("""{"t":"REQUEST","v":2}""".toByteArray()))
    }

    @Test fun garbageDecodesToNull() {
        assertNull(Codec.decode("not json".toByteArray()))
        assertNull(Codec.decode("""{"v":1}""".toByteArray()))
        assertNull(Codec.decode(ByteArray(0)))
    }
}
