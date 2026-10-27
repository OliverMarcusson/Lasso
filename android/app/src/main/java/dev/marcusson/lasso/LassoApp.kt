package dev.marcusson.lasso

import android.app.Application
import dev.marcusson.lasso.hotspot.HotspotController
import dev.marcusson.lasso.hotspot.StubHotspotController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow

class LassoApp : Application() {
    /** Process-wide scope on the main thread; outlives the service so teardown can finish. */
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    // Swap in the Shizuku-backed controller here.
    val hotspotController: HotspotController = StubHotspotController()

    val allowlist by lazy { Allowlist(this) }

    /** True while the pairing screen accepts PAIR. */
    val pairingOpen = MutableStateFlow(false)
    val sessionState = MutableStateFlow(SessionState.IDLE)
}
