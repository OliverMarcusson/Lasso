package dev.marcusson.lasso

import android.app.Application
import dev.marcusson.lasso.hotspot.HotspotController
import dev.marcusson.lasso.hotspot.ShizukuHotspotController
import dev.marcusson.lasso.hotspot.StubHotspotController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow

class LassoApp : Application() {
    /** Process-wide scope on the main thread; outlives the service so teardown can finish. */
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    val hotspotController: HotspotController =
        if (USE_STUB_HOTSPOT) StubHotspotController() else ShizukuHotspotController(this)

    val allowlist by lazy { Allowlist(this) }

    /** True while the pairing screen accepts PAIR. */
    val pairingOpen = MutableStateFlow(false)
    val sessionState = MutableStateFlow(SessionState.IDLE)
}

/** Set to true to test the BLE/session flow without Shizuku or a real hotspot. */
private const val USE_STUB_HOTSPOT = false
