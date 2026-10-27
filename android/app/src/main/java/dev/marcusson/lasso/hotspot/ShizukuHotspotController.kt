package dev.marcusson.lasso.hotspot

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.TetheringManager
import android.net.wifi.SoftApConfiguration
import android.net.wifi.WifiSsid
import android.os.Binder
import android.os.IBinder
import android.os.Parcel
import android.util.Base64
import android.provider.Settings
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.lsposed.hiddenapibypass.HiddenApiBypass
import rikka.shizuku.Shizuku
import rikka.shizuku.ShizukuBinderWrapper
import rikka.shizuku.SystemServiceHelper
import java.lang.reflect.InvocationTargetException
import kotlin.coroutines.cancellation.CancellationException

/**
 * Real hotspot backend: calls the tethering and wifi system services with shell identity through Shizuku.
 * Checked against AOSP android-16.0.0_r1 (API 36).
 *
 * Start path: ITetheringConnector.startTethering(TetheringRequestParcel{softApConfig}, "com.android.shell", ...).
 * On API 36 Tethering hands the request's SoftApConfiguration to WifiManager.startTetheredHotspot(request, ...),
 * but WifiServiceImpl only honours it when the aconfig flag com.android.net.flags.tethering_with_soft_ap_config
 * is on (otherwise it uses the stored config), and Samsung ships its own Wifi module. So the controller also
 * writes Lasso's config as the stored config (IWifiManager.setSoftApConfiguration) and restores the user's
 * original on stop. The original is persisted first, so a crash mid-session is repaired on the next call.
 *
 * Fallback: `cmd wifi start-softap`. On API 36 Tethering ignores the AP-state broadcast for tethered mode
 * (handleWifiApAction returns early when isTetheringWithSoftApConfigEnabled), so that AP most likely has no
 * DHCP/NAT and no internet. It only runs when the binder path itself breaks (reflection, dead service).
 */
class ShizukuHotspotController(private val context: Context) : HotspotController {
    /** True from the moment Lasso asks for a hotspot until it stopped it. */
    @Volatile private var owned = false

    override fun isAvailable(): Boolean = try {
        val alive = Shizuku.pingBinder()
        val granted = alive && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        val uid = if (granted) Shizuku.getUid() else -1
        val ok = granted && (uid == 2000 || uid == 0)
        if (!ok) Log.i(TAG, "isAvailable=false binder=$alive granted=$granted uid=$uid")
        ok
    } catch (e: Throwable) {
        Log.w(TAG, "isAvailable: $e")
        false
    }

    override suspend fun currentUserHotspot(): HotspotInfo? = withContext(Dispatchers.IO) {
        if (owned) return@withContext null
        restoreUserConfig()
        val state = apState()
        Log.i(TAG, "currentUserHotspot: apState=$state")
        if (state != AP_ENABLED) return@withContext null
        // Stored config; matches the running AP unless another app started it with a per-request config.
        val cfg = call(wifi(), WIFI, "getSoftApConfiguration") as SoftApConfiguration
        val ssid = cfg.wifiSsid?.bytes?.let { String(it, Charsets.UTF_8) }
        val psk = cfg.passphrase
        val security = when (cfg.securityType) {
            SoftApConfiguration.SECURITY_TYPE_WPA2_PSK -> Security.WPA2
            SoftApConfiguration.SECURITY_TYPE_WPA3_SAE_TRANSITION -> Security.WPA3_TRANSITION
            SoftApConfiguration.SECURITY_TYPE_WPA3_SAE -> Security.WPA3
            else -> null
        }
        Log.i(TAG, "currentUserHotspot: ssid=$ssid securityType=${cfg.securityType} hasPsk=${psk != null}")
        if (ssid == null || psk == null || security == null) {
            Log.w(TAG, "currentUserHotspot: user hotspot not representable (open/OWE?), ignoring it")
            return@withContext null
        }
        HotspotInfo(ssid, psk, security, owned = false)
    }

    override suspend fun start(config: HotspotConfig): HotspotInfo = withContext(Dispatchers.IO) {
        Log.i(TAG, "start: ssid=${config.ssid} security=${config.security}")
        if (!isAvailable()) throw fail(NO_PRIVILEGE, "Shizuku not running, not granted, or not shell/root")
        if (Settings.Global.getInt(context.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) != 0) {
            throw fail(AIRPLANE, "airplane mode is on")
        }
        if (!hasValidatedUpstream()) throw fail(NO_MOBILE_DATA, "no validated internet network to share")
        val before = apState()
        Log.i(TAG, "start: apState=$before")
        if (before == AP_ENABLED || before == AP_ENABLING) throw fail(TETHER_FAILED, "soft AP already running")

        restoreUserConfig() // leftover from a crashed session, before reading the user's config again
        owned = true // set before the call so a failed or cancelled start still gets stopped
        try {
            startViaTethering(config)
        } catch (e: CancellationException) {
            throw e
        } catch (e: HotspotException) {
            throw e
        } catch (e: Exception) {
            Log.e(TAG, "start: AOSP tethering path broke, falling back to cmd wifi start-softap", unwrap(e))
            startViaCmd(config)
        }

        val up = withTimeoutOrNull(START_TIMEOUT_MS) {
            var s = apState()
            while (s != AP_ENABLED) {
                if (s == AP_FAILED) throw fail(TETHER_FAILED, "soft AP state FAILED")
                Log.d(TAG, "start: waiting, apState=$s")
                delay(POLL_MS)
                s = apState()
            }
            true
        }
        if (up != true) throw fail(TETHER_FAILED, "soft AP not enabled after ${START_TIMEOUT_MS / 1000} s")
        Log.i(TAG, "start: hotspot up")
        HotspotInfo(config.ssid, config.psk, config.security, owned = true)
    }

    override suspend fun stop(): Unit = withContext(Dispatchers.IO) {
        if (!owned) {
            Log.i(TAG, "stop: Lasso owns no hotspot, no-op")
            restoreUserConfig()
            return@withContext
        }
        try {
            try {
                stopViaTethering()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "stop: AOSP tethering path failed, falling back to cmd wifi stop-softap", unwrap(e))
                runCmd("cmd", "wifi", "stop-softap")
            }
            val down = withTimeoutOrNull(STOP_TIMEOUT_MS) {
                while (apState().let { it != AP_DISABLED && it != AP_FAILED }) delay(POLL_MS)
                true
            }
            Log.i(TAG, if (down == true) "stop: hotspot down" else "stop: hotspot still not disabled")
        } finally {
            owned = false
            restoreUserConfig()
        }
    }

    // ---- AOSP binder path ----

    @SuppressLint("MissingPermission", "NewApi")
    private suspend fun startViaTethering(config: HotspotConfig) {
        hiddenApiExempt // the Builder setters below are @SystemApi and getParcel() is @hide
        val builder = SoftApConfiguration.Builder()
        val b = SoftApConfiguration.Builder::class.java
        b.getMethod("setWifiSsid", WifiSsid::class.java)
            .invoke(builder, WifiSsid.fromBytes(config.ssid.toByteArray(Charsets.UTF_8)))
        b.getMethod("setPassphrase", String::class.java, Int::class.javaPrimitiveType)
            .invoke(builder, config.psk, config.security.softApType())
        b.getMethod("setMaxNumberOfClients", Int::class.javaPrimitiveType).invoke(builder, 1)
        b.getMethod("setBand", Int::class.javaPrimitiveType).invoke(builder, BAND_2GHZ or BAND_5GHZ)
        val softAp = builder.build()
        Log.i(TAG, "startViaTethering: softAp=$softAp")
        overwriteStoredConfig(softAp)

        val request = TetheringManager.TetheringRequest.Builder(TetheringManager.TETHERING_WIFI)
            .setSoftApConfiguration(softAp)
            .build()
        val parcel = request.javaClass.getMethod("getParcel").invoke(request)
        val code = tetheringCall("startTethering", parcel)
        Log.i(TAG, "startTethering result=$code")
        when (code) {
            TETHER_ERROR_NO_ERROR -> Unit
            TETHER_ERROR_NO_CHANGE_TETHERING_PERMISSION, TETHER_ERROR_NO_ACCESS_TETHERING_PERMISSION ->
                throw fail(NO_PRIVILEGE, "tethering refused caller ${callerPkg()} (code $code)")
            else -> throw fail(TETHER_FAILED, "startTethering error $code")
        }
    }

    private suspend fun stopViaTethering() {
        val code = tetheringCall("stopTethering", TetheringManager.TETHERING_WIFI)
        Log.i(TAG, "stopTethering result=$code")
        if (code != TETHER_ERROR_NO_ERROR) error("stopTethering error $code")
    }

    /** Calls a oneway ITetheringConnector method shaped (arg, callerPkg, attributionTag, IIntResultListener). */
    private suspend fun tetheringCall(method: String, arg: Any?): Int {
        val result = CompletableDeferred<Int>()
        val listenerBinder = object : Binder() {
            override fun onTransact(code: Int, data: Parcel, reply: Parcel?, flags: Int): Boolean {
                if (code != FIRST_CALL_TRANSACTION) return super.onTransact(code, data, reply, flags)
                data.enforceInterface(LISTENER)
                result.complete(data.readInt())
                return true
            }
        }
        val listener = asInterface(LISTENER, listenerBinder)
        val pkg = callerPkg()
        Log.i(TAG, "$method: callerPkg=$pkg")
        call(service(CONNECTOR, "tethering"), CONNECTOR, method, arg, pkg, null, listener)
        return withTimeoutOrNull(START_TIMEOUT_MS) { result.await() }
            ?: throw fail(TETHER_FAILED, "$method: no result within ${START_TIMEOUT_MS / 1000} s")
    }

    // ---- stored config overwrite / restore ----

    private val prefs get() = context.getSharedPreferences("lasso_hotspot", Context.MODE_PRIVATE)

    /** Saves the user's stored config (to disk, before touching it), then stores Lasso's. */
    private fun overwriteStoredConfig(lasso: SoftApConfiguration) {
        val user = call(wifi(), WIFI, "getSoftApConfiguration") as SoftApConfiguration
        prefs.edit().putString(KEY_USER_CONFIG, encode(user)).commit()
        val ok = call(wifi(), WIFI, "setSoftApConfiguration", lasso, callerPkg()) as Boolean
        Log.i(TAG, "overwriteStoredConfig: saved user ssid=${user.wifiSsid}, setSoftApConfiguration=$ok")
        if (!ok) {
            prefs.edit().remove(KEY_USER_CONFIG).commit()
            throw fail(TETHER_FAILED, "wifi rejected Lasso's SoftApConfiguration")
        }
    }

    /** Puts the user's saved config back if Lasso overwrote it. Idempotent; keeps the copy if it fails. */
    private fun restoreUserConfig() {
        val saved = prefs.getString(KEY_USER_CONFIG, null) ?: return
        val ok = try {
            call(wifi(), WIFI, "setSoftApConfiguration", decode(saved), callerPkg()) as Boolean
        } catch (e: Exception) {
            Log.e(TAG, "restoreUserConfig failed, will retry on the next call", unwrap(e))
            return
        }
        Log.i(TAG, "restoreUserConfig: setSoftApConfiguration=$ok")
        // ponytail: a config wifi rejects is dropped rather than retried forever; the user re-enters it then.
        prefs.edit().remove(KEY_USER_CONFIG).commit()
    }

    private fun encode(cfg: SoftApConfiguration): String {
        val p = Parcel.obtain()
        try {
            cfg.writeToParcel(p, 0)
            return Base64.encodeToString(p.marshall(), Base64.NO_WRAP)
        } finally {
            p.recycle()
        }
    }

    private fun decode(s: String): SoftApConfiguration {
        val bytes = Base64.decode(s, Base64.NO_WRAP)
        val p = Parcel.obtain()
        try {
            p.unmarshall(bytes, 0, bytes.size)
            p.setDataPosition(0)
            return SoftApConfiguration.CREATOR.createFromParcel(p)
        } finally {
            p.recycle()
        }
    }

    private fun apState(): Int = call(wifi(), WIFI, "getWifiApEnabledState") as Int

    private fun wifi(): Any = service(WIFI, "wifi")

    private fun service(iface: String, name: String): Any {
        hiddenApiExempt
        val binder = SystemServiceHelper.getSystemService(name) ?: error("system service '$name' not found")
        return asInterface(iface, ShizukuBinderWrapper(binder))
    }

    private fun asInterface(iface: String, binder: IBinder): Any {
        hiddenApiExempt
        return Class.forName("$iface\$Stub").getMethod("asInterface", IBinder::class.java).invoke(null, binder)!!
    }

    /** Looks methods up by name only, so a changed parameter type fails loudly here instead of at lookup. */
    private fun call(target: Any, iface: String, method: String, vararg args: Any?): Any? = try {
        Class.forName(iface).methods.first { it.name == method }.invoke(target, *args)
    } catch (e: InvocationTargetException) {
        Log.e(TAG, "$iface.$method threw", e.targetException)
        throw e.targetException
    }

    private fun callerPkg() = if (Shizuku.getUid() == 0) "root" else "com.android.shell"

    // ---- cmd fallback ----

    private fun startViaCmd(config: HotspotConfig) {
        val type = when (config.security) {
            Security.WPA2 -> "wpa2"
            Security.WPA3 -> "wpa3"
            Security.WPA3_TRANSITION -> "wpa3_transition"
        }
        Log.w(TAG, "startViaCmd: this AP probably has no internet on Android 16 (no Tethering IP serving)")
        runCmd("cmd", "wifi", "start-softap", config.ssid, type, config.psk)
    }

    private fun runCmd(vararg argv: String): String {
        Log.i(TAG, "runCmd: ${argv.take(3).joinToString(" ")} ...") // never log the passphrase
        // Shizuku 13 made newProcess private; it is still the simplest way to run a shell command.
        val newProcess = Shizuku::class.java.getDeclaredMethod(
            "newProcess", Array<String>::class.java, Array<String>::class.java, String::class.java,
        ).apply { isAccessible = true }
        val p = newProcess.invoke(null, argv, null, null) as Process
        val out = p.inputStream.bufferedReader().readText() + p.errorStream.bufferedReader().readText()
        val exit = p.waitFor()
        Log.i(TAG, "runCmd: exit=$exit output=${out.trim()}")
        return out
    }

    // ---- preconditions ----

    // ponytail: any validated internet network counts as upstream. Wi-Fi-only with mobile data off passes
    // and gives a hotspot without internet; tighten to TRANSPORT_CELLULAR if the spike shows that matters.
    @Suppress("DEPRECATION")
    private fun hasValidatedUpstream(): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        return cm.allNetworks.any { n ->
            val caps = cm.getNetworkCapabilities(n) ?: return@any false
            val ok = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
            Log.i(TAG, "upstream candidate $n cellular=${caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)} " +
                "wifi=${caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)} validated=$ok")
            ok
        }
    }

    private fun Security.softApType() = when (this) {
        Security.WPA2 -> SoftApConfiguration.SECURITY_TYPE_WPA2_PSK
        Security.WPA3 -> SoftApConfiguration.SECURITY_TYPE_WPA3_SAE
        Security.WPA3_TRANSITION -> SoftApConfiguration.SECURITY_TYPE_WPA3_SAE_TRANSITION
    }

    private fun fail(code: String, msg: String): HotspotException {
        Log.e(TAG, "$code: $msg")
        return HotspotException(code, msg)
    }

    private fun unwrap(e: Throwable) = (e as? InvocationTargetException)?.targetException ?: e

    private companion object {
        const val TAG = "LassoHotspot"
        const val KEY_USER_CONFIG = "user_softap_config"
        const val WIFI = "android.net.wifi.IWifiManager"
        const val CONNECTOR = "android.net.ITetheringConnector"
        const val LISTENER = "android.net.IIntResultListener"

        const val NO_PRIVILEGE = "NO_PRIVILEGE"
        const val AIRPLANE = "AIRPLANE"
        const val NO_MOBILE_DATA = "NO_MOBILE_DATA"
        const val TETHER_FAILED = "TETHER_FAILED"

        // WifiManager.WIFI_AP_STATE_* and TetheringManager.TETHER_ERROR_* (system API, values from AOSP 16).
        const val AP_DISABLED = 11
        const val AP_ENABLING = 12
        const val AP_ENABLED = 13
        const val AP_FAILED = 14
        const val TETHER_ERROR_NO_ERROR = 0
        const val TETHER_ERROR_NO_CHANGE_TETHERING_PERMISSION = 14
        const val TETHER_ERROR_NO_ACCESS_TETHERING_PERMISSION = 15
        const val BAND_2GHZ = 1
        const val BAND_5GHZ = 2

        const val START_TIMEOUT_MS = 15_000L
        const val STOP_TIMEOUT_MS = 5_000L
        const val POLL_MS = 250L

        /** Lets this process reflect on hidden framework classes (IWifiManager, ITetheringConnector, ...). */
        val hiddenApiExempt: Boolean by lazy {
            HiddenApiBypass.addHiddenApiExemptions("L").also { Log.i(TAG, "hidden API exemption: $it") }
        }
    }
}
