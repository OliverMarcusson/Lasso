package dev.marcusson.lasso

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import rikka.shizuku.Shizuku

class MainActivity : Activity() {
    private val app get() = application as LassoApp
    private lateinit var status: TextView
    private lateinit var shizukuStatus: TextView
    private lateinit var pairButton: Button
    private lateinit var pairedList: LinearLayout
    private var uiScope: CoroutineScope? = null

    private val shizukuPermission = Shizuku.OnRequestPermissionResultListener { _, _ -> runOnUiThread { render() } }
    private val binderReceived = Shizuku.OnBinderReceivedListener { runOnUiThread { render() } }
    private val binderDead = Shizuku.OnBinderDeadListener { runOnUiThread { render() } }

    private val runtimePermissions = arrayOf(
        Manifest.permission.BLUETOOTH_CONNECT,
        Manifest.permission.BLUETOOTH_ADVERTISE,
        Manifest.permission.POST_NOTIFICATIONS,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
            // targetSdk 35+ is edge-to-edge
            setOnApplyWindowInsetsListener { v, insets ->
                val bars = insets.getInsets(WindowInsets.Type.systemBars())
                v.setPadding(48 + bars.left, 48 + bars.top, 48 + bars.right, 48 + bars.bottom)
                insets
            }
        }
        status = TextView(this).apply { textSize = 18f }
        shizukuStatus = TextView(this)
        pairButton = Button(this).apply { setOnClickListener { app.pairingOpen.value = !app.pairingOpen.value } }
        val shizukuButton = Button(this).apply {
            text = "Request Shizuku permission"
            setOnClickListener { requestShizuku() }
        }
        pairedList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        listOf(
            status, shizukuStatus, shizukuButton, pairButton,
            TextView(this).apply { text = "Paired laptops"; setPadding(0, 48, 0, 0) }, pairedList,
        ).forEach { root.addView(it) }
        setContentView(root)

        if (missingPermissions().isNotEmpty()) requestPermissions(missingPermissions(), REQ_PERMISSIONS) else startLasso()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        startLasso() // restarts BLE if Bluetooth permission was just granted
        render()
    }

    override fun onStart() {
        super.onStart()
        Shizuku.addBinderReceivedListener(binderReceived)
        Shizuku.addBinderDeadListener(binderDead)
        Shizuku.addRequestPermissionResultListener(shizukuPermission)
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        uiScope = scope
        fun <T> StateFlow<T>.onChange() = scope.launch { collect { render() } }
        app.sessionState.onChange()
        app.pairingOpen.onChange()
        app.allowlist.paired.onChange()
    }

    override fun onStop() {
        uiScope?.cancel()
        Shizuku.removeBinderReceivedListener(binderReceived)
        Shizuku.removeBinderDeadListener(binderDead)
        Shizuku.removeRequestPermissionResultListener(shizukuPermission)
        app.pairingOpen.value = false // PAIR is only accepted while this screen is open
        super.onStop()
    }

    private fun startLasso() = startForegroundService(Intent(this, LassoService::class.java))

    private fun missingPermissions() =
        runtimePermissions.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }.toTypedArray()

    private fun shizukuState(): Int = try {
        when {
            !Shizuku.pingBinder() -> 0
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED -> 2
            else -> 1
        }
    } catch (e: Throwable) {
        0
    }

    private fun requestShizuku() {
        when (shizukuState()) {
            0 -> {
                Toast.makeText(this, "Shizuku is not running", Toast.LENGTH_SHORT).show()
                packageManager.getLaunchIntentForPackage(LassoService.SHIZUKU_PACKAGE)?.let { startActivity(it) }
            }
            1 -> Shizuku.requestPermission(REQ_SHIZUKU)
        }
        render()
    }

    private fun render() {
        val missing = missingPermissions()
        status.text = when {
            missing.isNotEmpty() -> "Missing permissions: " + missing.joinToString { it.substringAfterLast('.') }
            else -> when (app.sessionState.value) {
                SessionState.IDLE -> "Lasso ready"
                SessionState.STARTING -> "Starting hotspot..."
                SessionState.WAIT_JOIN -> "Hotspot up, waiting for laptop"
                SessionState.ACTIVE -> "Sharing hotspot"
                SessionState.STOPPING -> "Stopping hotspot..."
            }
        }
        shizukuStatus.text = when (shizukuState()) {
            0 -> "Shizuku: not running"
            1 -> "Shizuku: running, permission not granted"
            else -> "Shizuku: ready"
        }
        pairButton.text = if (app.pairingOpen.value) "Pairing open (tap to close)" else "Pair laptop"
        pairedList.removeAllViews()
        for ((address, name) in app.allowlist.paired.value) {
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            row.addView(TextView(this).apply { text = "$name\n$address" }, LinearLayout.LayoutParams(0, -2, 1f))
            row.addView(Button(this).apply {
                text = "Remove"
                setOnClickListener { app.allowlist.remove(address) }
            })
            pairedList.addView(row)
        }
        pairedList.visibility = if (app.allowlist.paired.value.isEmpty()) View.GONE else View.VISIBLE
    }

    private companion object {
        const val REQ_PERMISSIONS = 1
        const val REQ_SHIZUKU = 2
    }
}
