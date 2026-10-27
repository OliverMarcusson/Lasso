package dev.marcusson.lasso.hotspot

enum class Security { WPA3_TRANSITION, WPA3, WPA2 }

data class HotspotConfig(val ssid: String, val psk: String, val security: Security)

data class HotspotInfo(val ssid: String, val psk: String, val security: Security, val owned: Boolean)

/** [code] is a wire ERROR code (NO_PRIVILEGE, NO_MOBILE_DATA, AIRPLANE, TETHER_FAILED, ...). */
class HotspotException(val code: String, message: String? = null) : Exception(message)

/** The only boundary between the app and the privileged hotspot code. */
interface HotspotController {
    /** True when the privileged backend (Shizuku) is running and permission is granted. */
    fun isAvailable(): Boolean

    /** The hotspot the user enabled themselves, or null if none is on. Returned with owned = false. */
    suspend fun currentUserHotspot(): HotspotInfo?

    /** Start a tethered hotspot with internet, max 1 client. Returns once it is up. Throws HotspotException. */
    suspend fun start(config: HotspotConfig): HotspotInfo

    /** Stop the hotspot Lasso started. No-op if it is not running. */
    suspend fun stop()
}
