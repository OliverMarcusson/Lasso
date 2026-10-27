package dev.marcusson.lasso.hotspot

import kotlinx.coroutines.delay

/** Simulates a hotspot until the Shizuku-backed controller exists. */
class StubHotspotController : HotspotController {
    @Volatile private var running = false

    override fun isAvailable() = true

    override suspend fun currentUserHotspot(): HotspotInfo? = null

    override suspend fun start(config: HotspotConfig): HotspotInfo {
        delay(1500)
        running = true
        return HotspotInfo(config.ssid, config.psk, config.security, owned = true)
    }

    override suspend fun stop() {
        if (running) delay(300)
        running = false
    }
}
