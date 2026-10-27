package dev.marcusson.lasso

import dev.marcusson.lasso.hotspot.HotspotConfig
import dev.marcusson.lasso.hotspot.HotspotController
import dev.marcusson.lasso.hotspot.HotspotException
import dev.marcusson.lasso.hotspot.HotspotInfo
import dev.marcusson.lasso.hotspot.Security
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random

internal class FakeHotspot : HotspotController {
    var available = true
    var user: HotspotInfo? = null
    var failWith: Exception? = null
    var starts = 0
    var stops = 0

    override fun isAvailable() = available
    override suspend fun currentUserHotspot() = user
    override suspend fun start(config: HotspotConfig): HotspotInfo {
        starts++
        failWith?.let { throw it }
        return HotspotInfo(config.ssid, config.psk, config.security, owned = true)
    }
    override suspend fun stop() { stops++ }
}

internal class RecordingListener : SessionListener {
    val sent = mutableListOf<Pair<String, Message>>()
    var noPrivilege = 0
    override fun send(peer: String, msg: Message) { sent += peer to msg }
    override fun onState(state: SessionState, peer: String?) {}
    override fun onNoPrivilege() { noPrivilege++ }

    fun messages(peer: String? = null) = sent.filter { peer == null || it.first == peer }.map { it.second }
    val readies get() = messages().filterIsInstance<Message.Ready>()
    val ended get() = messages().filterIsInstance<Message.Ended>()
}

@OptIn(ExperimentalCoroutinesApi::class)
class SessionMachineTest {
    private class Fixture(scope: CoroutineScope, val hotspot: FakeHotspot) {
        val listener = RecordingListener()
        val machine = SessionMachine(hotspot, scope, listener, CredentialGenerator(Random(1)))
    }

    private fun TestScope.fixture(configure: FakeHotspot.() -> Unit = {}) =
        Fixture(backgroundScope, FakeHotspot().apply(configure))

    private fun TestScope.requestReady(f: Fixture, peer: String = "AA"): Message.Ready {
        f.machine.onRequest(peer)
        runCurrent()
        return f.listener.readies.last()
    }

    @Test fun requestStartsHotspotAndRepliesStartingThenReady() = runTest {
        val f = fixture()
        val ready = requestReady(f)
        assertEquals(listOf(Message.Starting, ready), f.listener.messages("AA"))
        assertTrue(ready.owned)
        assertTrue(ready.ssid.matches(Regex("Lasso-[a-z0-9]{6}")))
        assertEquals(24, ready.psk.length)
        assertEquals(32, ready.sid.length)
        assertEquals("wpa3t", ready.sec)
        assertEquals(Protocol.UDP_PORT, ready.port)
        assertEquals(SessionState.WAIT_JOIN, f.machine.state)
        assertEquals(1, f.hotspot.starts)
    }

    @Test fun repeatedRequestFromSamePeerReturnsSameReady() = runTest {
        val f = fixture()
        val ready = requestReady(f)
        f.machine.onRequest("AA")
        assertEquals(ready, f.listener.messages().last())
        assertEquals(1, f.hotspot.starts)
    }

    @Test fun requestFromOtherPeerIsBusy() = runTest {
        val f = fixture()
        requestReady(f)
        f.machine.onRequest("BB")
        assertEquals(Message.ErrorMsg(ErrorCode.BUSY), f.listener.messages("BB").last())
    }

    @Test fun unavailableBackendRepliesNoPrivilege() = runTest {
        val f = fixture { available = false }
        f.machine.onRequest("AA")
        runCurrent()
        assertEquals(listOf<Message>(Message.ErrorMsg(ErrorCode.NO_PRIVILEGE)), f.listener.messages())
        assertEquals(1, f.listener.noPrivilege)
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(0, f.hotspot.starts)
    }

    @Test fun hotspotExceptionCodeIsForwarded() = runTest {
        val f = fixture { failWith = HotspotException("NO_MOBILE_DATA") }
        f.machine.onRequest("AA")
        runCurrent()
        assertEquals(Message.ErrorMsg("NO_MOBILE_DATA"), f.listener.messages().last())
        assertEquals(SessionState.IDLE, f.machine.state)
        // a later request may try again
        f.hotspot.failWith = null
        f.machine.onRequest("AA")
        runCurrent()
        assertEquals(SessionState.WAIT_JOIN, f.machine.state)
    }

    @Test fun unexpectedExceptionBecomesTetherFailed() = runTest {
        val f = fixture { failWith = IllegalStateException("boom") }
        f.machine.onRequest("AA")
        runCurrent()
        assertEquals(Message.ErrorMsg(ErrorCode.TETHER_FAILED), f.listener.messages().last())
    }

    @Test fun noFirstPingWithin90sEndsWithNoJoin() = runTest {
        val f = fixture()
        requestReady(f)
        advanceTimeBy(89_000)
        runCurrent()
        assertEquals(SessionState.WAIT_JOIN, f.machine.state)
        advanceTimeBy(2_000)
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(1, f.hotspot.stops)
        assertEquals("no_join", f.listener.ended.single().reason)
    }

    @Test fun pingsKeepSessionAliveUntil180sSilence() = runTest {
        val f = fixture()
        val sid = requestReady(f).sid
        advanceTimeBy(60_000)
        assertTrue(f.machine.onPing(sid))
        assertEquals(SessionState.ACTIVE, f.machine.state)
        repeat(3) {
            advanceTimeBy(170_000)
            runCurrent()
            assertEquals(SessionState.ACTIVE, f.machine.state)
            assertTrue(f.machine.onPing(sid))
        }
        advanceTimeBy(181_000)
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(1, f.hotspot.stops)
        assertEquals("timeout", f.listener.ended.single().reason)
    }

    @Test fun pingWithWrongSidIsRejected() = runTest {
        val f = fixture()
        requestReady(f)
        assertFalse(f.machine.onPing("0".repeat(32)))
        assertEquals(SessionState.WAIT_JOIN, f.machine.state)
    }

    @Test fun releaseEndsSessionOnlyWithRightSid() = runTest {
        val f = fixture()
        val sid = requestReady(f).sid
        assertFalse(f.machine.onRelease("0".repeat(32)))
        assertEquals(SessionState.WAIT_JOIN, f.machine.state)
        assertTrue(f.machine.onRelease(sid))
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(1, f.hotspot.stops)
        assertEquals("released", f.listener.ended.single().reason)
        assertFalse(f.machine.onPing(sid))
    }

    @Test fun userStopEndsActiveSession() = runTest {
        val f = fixture()
        val sid = requestReady(f).sid
        f.machine.onPing(sid)
        f.machine.onUserStop()
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals("user_stop", f.listener.ended.single().reason)
    }

    @Test fun existingUserHotspotIsReusedAndNeverStopped() = runTest {
        val f = fixture { user = HotspotInfo("MyPhone", "secret123", Security.WPA2, owned = false) }
        val ready = requestReady(f)
        assertFalse(ready.owned)
        assertEquals("MyPhone", ready.ssid)
        assertEquals("wpa2", ready.sec)
        assertEquals(0, f.hotspot.starts)

        f.machine.onPing(ready.sid)
        f.machine.onUserStop()
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(0, f.hotspot.stops)
        assertNotNull(f.listener.ended.singleOrNull())
    }

    @Test fun userHotspotTimeoutNeverStopsIt() = runTest {
        val f = fixture { user = HotspotInfo("MyPhone", "secret123", Security.WPA2, owned = false) }
        requestReady(f)
        advanceTimeBy(91_000)
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(0, f.hotspot.stops)
    }

    @Test fun userHotspotWithOversizedCredentialsFails() = runTest {
        val f = fixture { user = HotspotInfo("s".repeat(32), "p".repeat(63), Security.WPA2, owned = false) }
        f.machine.onRequest("AA")
        runCurrent()
        assertEquals(Message.ErrorMsg(ErrorCode.TETHER_FAILED), f.listener.messages().last())
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(0, f.hotspot.stops)
        assertNull(f.listener.readies.firstOrNull())
    }

    @Test fun idleStopIsNoOp() = runTest {
        val f = fixture()
        f.machine.onUserStop()
        runCurrent()
        assertEquals(SessionState.IDLE, f.machine.state)
        assertEquals(0, f.hotspot.stops)
        assertTrue(f.listener.sent.isEmpty())
    }
}
