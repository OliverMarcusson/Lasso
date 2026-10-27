package dev.marcusson.lasso

import dev.marcusson.lasso.hotspot.HotspotConfig
import dev.marcusson.lasso.hotspot.HotspotController
import dev.marcusson.lasso.hotspot.HotspotException
import dev.marcusson.lasso.hotspot.Security
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.security.MessageDigest

enum class SessionState { IDLE, STARTING, WAIT_JOIN, ACTIVE, STOPPING }

enum class EndReason(val wire: String) {
    TIMEOUT("timeout"), NO_JOIN("no_join"), RELEASED("released"), USER_STOP("user_stop"), SYSTEM("system"),
}

interface SessionListener {
    /** Best effort BLE notify to [peer]. */
    fun send(peer: String, msg: Message)
    fun onState(state: SessionState, peer: String?)
    fun onNoPrivilege()
}

/**
 * Phone-side session logic (spec section 9). Not thread safe: call everything from one thread
 * (the service uses the main thread). Timers run on [scope], so tests drive them with virtual time.
 */
class SessionMachine(
    private val hotspot: HotspotController,
    private val scope: CoroutineScope,
    private val listener: SessionListener,
    private val creds: CredentialGenerator = CredentialGenerator(),
) {
    var state = SessionState.IDLE
        private set

    private var peer: String? = null
    private var ready: Message.Ready? = null
    private var owned = false
    private var mayOwn = false // set before start(): a cancelled start may still leave a hotspot to stop
    private var startJob: Job? = null
    private var watchdog: Job? = null

    fun onRequest(from: String) {
        val r = ready
        when {
            state == SessionState.STARTING && from == peer -> listener.send(from, Message.Starting)
            r != null && from == peer &&
                (state == SessionState.WAIT_JOIN || state == SessionState.ACTIVE) -> listener.send(from, r)
            state != SessionState.IDLE -> listener.send(from, Message.ErrorMsg(ErrorCode.BUSY))
            !hotspot.isAvailable() -> {
                listener.send(from, Message.ErrorMsg(ErrorCode.NO_PRIVILEGE))
                listener.onNoPrivilege()
            }
            else -> begin(from)
        }
    }

    /** True when [sid] belongs to the live session; the caller then answers PONG. */
    fun onPing(sid: String): Boolean {
        if (!isLive(sid)) return false
        if (state == SessionState.WAIT_JOIN) setState(SessionState.ACTIVE, peer)
        arm(Protocol.PING_TIMEOUT_MS, EndReason.TIMEOUT)
        return true
    }

    fun onRelease(sid: String): Boolean {
        if (!isLive(sid)) return false
        end(EndReason.RELEASED)
        return true
    }

    fun onUserStop() = end(EndReason.USER_STOP)

    fun shutdown() = end(EndReason.SYSTEM)

    private fun isLive(sid: String): Boolean {
        val r = ready ?: return false
        return (state == SessionState.WAIT_JOIN || state == SessionState.ACTIVE) &&
            MessageDigest.isEqual(r.sid.toByteArray(), sid.toByteArray())
    }

    private fun begin(from: String) {
        setState(SessionState.STARTING, from)
        listener.send(from, Message.Starting)
        startJob = scope.launch {
            try {
                val existing = hotspot.currentUserHotspot()
                val info = existing ?: run {
                    mayOwn = true
                    hotspot.start(HotspotConfig(creds.ssid(), creds.psk(), Security.WPA3_TRANSITION))
                }
                becomeReady(from, Message.Ready(
                    info.ssid, info.psk, info.security.wire(), creds.sid(), Protocol.UDP_PORT, owned = existing == null,
                ))
            } catch (e: CancellationException) {
                throw e
            } catch (e: HotspotException) {
                fail(from, e.code)
            } catch (e: Exception) {
                fail(from, ErrorCode.TETHER_FAILED)
            }
        }
    }

    private suspend fun becomeReady(from: String, msg: Message.Ready) {
        // A user hotspot with long credentials may not fit in one GATT message.
        if (!Codec.fits(msg)) return fail(from, ErrorCode.TETHER_FAILED)
        ready = msg
        owned = msg.owned
        setState(SessionState.WAIT_JOIN, from)
        arm(Protocol.FIRST_PING_TIMEOUT_MS, EndReason.NO_JOIN)
        listener.send(from, msg)
    }

    private suspend fun fail(from: String, code: String) {
        if (mayOwn) stopQuietly()
        reset()
        listener.send(from, Message.ErrorMsg(code))
        setState(SessionState.IDLE, null)
    }

    private fun arm(ms: Long, reason: EndReason) {
        watchdog?.cancel()
        watchdog = scope.launch {
            delay(ms)
            end(reason)
        }
    }

    private fun end(reason: EndReason) {
        if (state == SessionState.IDLE || state == SessionState.STOPPING) return
        val from = peer
        val sid = ready?.sid
        val stopHotspot = owned || mayOwn // never stop a hotspot the user started
        val starting = startJob
        watchdog?.cancel()
        setState(SessionState.STOPPING, from)
        scope.launch {
            starting?.cancelAndJoin()
            if (stopHotspot) stopQuietly()
            reset()
            if (from != null && sid != null) listener.send(from, Message.Ended(sid, reason.wire))
            setState(SessionState.IDLE, null)
        }
    }

    private suspend fun stopQuietly() {
        try {
            hotspot.stop()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // best effort
        }
    }

    private fun reset() {
        ready = null
        owned = false
        mayOwn = false
        startJob = null
        watchdog = null
    }

    private fun setState(s: SessionState, p: String?) {
        state = s
        peer = p
        listener.onState(s, p)
    }
}
