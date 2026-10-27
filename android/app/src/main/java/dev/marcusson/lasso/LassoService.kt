package dev.marcusson.lasso

import android.annotation.SuppressLint
import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.os.Build
import android.os.IBinder
import android.os.ParcelUuid
import android.provider.Settings
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketException
import java.util.concurrent.ConcurrentHashMap

/**
 * Foreground service: GATT server + BLE advertising + UDP heartbeat listener, driving a [SessionMachine].
 * Machine calls happen on the main thread (app.scope); binder and IO threads hop over with launch/withContext.
 */
@SuppressLint("MissingPermission") // checked in hasBlePermissions() before any BLE call
class LassoService : Service(), SessionListener {
    private val app get() = application as LassoApp
    private val nm by lazy { getSystemService(NotificationManager::class.java) }
    private lateinit var machine: SessionMachine

    private var gatt: BluetoothGattServer? = null
    private var events: BluetoothGattCharacteristic? = null
    private var senderJob: Job? = null
    private var udpJob: Job? = null
    @Volatile private var udpSocket: DatagramSocket? = null
    private val connected = ConcurrentHashMap<String, BluetoothDevice>()
    private val outbox = Channel<Pair<BluetoothDevice, ByteArray>>(Channel.UNLIMITED)
    private val notificationSent = Channel<Unit>(Channel.CONFLATED)

    private var state = SessionState.IDLE
    private var peer: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        nm.createNotificationChannel(NotificationChannel(CH_STATUS, "Status", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_ALERT, "Alerts", NotificationManager.IMPORTANCE_DEFAULT))
        machine = SessionMachine(app.hotspotController, app.scope, this)
        registerReceiver(btReceiver, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED), RECEIVER_EXPORTED)
        startUdp()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_STATUS, statusNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        if (intent?.action == ACTION_STOP) machine.onUserStop()
        startBle()
        return START_STICKY
    }

    override fun onDestroy() {
        unregisterReceiver(btReceiver)
        udpJob?.cancel()
        udpSocket?.close()
        machine.shutdown() // teardown continues on app.scope; ENDED is lost once BLE is closed
        stopBle()
        super.onDestroy()
    }

    // ---- SessionListener ----

    override fun send(peer: String, msg: Message) {
        val device = connected[peer] ?: return
        val bytes = try {
            Codec.encode(msg)
        } catch (e: IllegalArgumentException) {
            Log.e(TAG, "dropping unsendable message", e)
            return
        }
        outbox.trySend(device to bytes)
    }

    override fun onState(state: SessionState, peer: String?) {
        this.state = state
        this.peer = peer
        app.sessionState.value = state
        if (state == SessionState.STARTING) nm.cancel(NOTIF_ALERT)
        nm.notify(NOTIF_STATUS, statusNotification())
    }

    override fun onNoPrivilege() {
        val open = packageManager.getLaunchIntentForPackage(SHIZUKU_PACKAGE)
            ?: Intent(this, MainActivity::class.java)
        val n = Notification.Builder(this, CH_ALERT)
            .setSmallIcon(android.R.drawable.stat_sys_warning)
            .setContentTitle("Lasso needs Shizuku")
            .setContentText("Shizuku is not running. Tap to open it and restart.")
            .setContentIntent(PendingIntent.getActivity(this, 1, open, PendingIntent.FLAG_IMMUTABLE))
            .setAutoCancel(true)
            .build()
        nm.notify(NOTIF_ALERT, n)
    }

    // ---- Notifications ----

    private fun statusNotification(): Notification {
        val b = Notification.Builder(this, CH_STATUS)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .setContentIntent(
                PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE),
            )
        when (state) {
            SessionState.IDLE -> {
                b.setContentTitle("Lasso ready")
                if (!hasBlePermissions()) b.setContentText("Open Lasso and grant Bluetooth permission")
            }
            SessionState.STARTING -> b.setContentTitle("Starting hotspot...")
            SessionState.STOPPING -> b.setContentTitle("Stopping hotspot...")
            SessionState.WAIT_JOIN, SessionState.ACTIVE -> {
                val stop = PendingIntent.getService(
                    this, 2, Intent(this, LassoService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
                )
                b.setContentTitle("Sharing hotspot with ${peer?.let(app.allowlist::nameOf) ?: "laptop"}")
                b.addAction(
                    Notification.Action.Builder(
                        Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel), "Stop", stop,
                    ).build(),
                )
            }
        }
        return b.build()
    }

    // ---- BLE ----

    private fun hasBlePermissions() =
        checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_ADVERTISE) == PackageManager.PERMISSION_GRANTED

    private val btReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)) {
                BluetoothAdapter.STATE_ON -> startBle()
                BluetoothAdapter.STATE_TURNING_OFF -> stopBle()
            }
        }
    }

    private fun startBle() {
        if (gatt != null) return
        if (!hasBlePermissions()) {
            nm.notify(NOTIF_STATUS, statusNotification())
            return
        }
        val manager = getSystemService(BluetoothManager::class.java)
        if (manager.adapter?.isEnabled != true) return
        val server = manager.openGattServer(this, gattCallback) ?: return
        gatt = server

        val control = BluetoothGattCharacteristic(
            Protocol.CONTROL_UUID,
            BluetoothGattCharacteristic.PROPERTY_WRITE,
            BluetoothGattCharacteristic.PERMISSION_WRITE_ENCRYPTED_MITM,
        )
        val eventsChar = BluetoothGattCharacteristic(
            Protocol.EVENTS_UUID,
            BluetoothGattCharacteristic.PROPERTY_NOTIFY,
            BluetoothGattCharacteristic.PERMISSION_READ_ENCRYPTED_MITM,
        )
        eventsChar.addDescriptor(
            BluetoothGattDescriptor(
                Protocol.CCC_UUID,
                BluetoothGattDescriptor.PERMISSION_READ_ENCRYPTED_MITM or
                    BluetoothGattDescriptor.PERMISSION_WRITE_ENCRYPTED_MITM,
            ),
        )
        events = eventsChar
        val service = BluetoothGattService(Protocol.SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY)
        service.addCharacteristic(control)
        service.addCharacteristic(eventsChar)
        server.addService(service) // advertising starts in onServiceAdded

        // One notification in flight at a time, or notifyCharacteristicChanged returns BUSY.
        senderJob = app.scope.launch {
            for ((device, bytes) in outbox) {
                val ch = events ?: continue
                val s = gatt ?: continue
                notificationSent.tryReceive()
                val status = s.notifyCharacteristicChanged(device, ch, false, bytes)
                if (status == BluetoothStatusCodes.SUCCESS) withTimeoutOrNull(2_000) { notificationSent.receive() }
                else Log.w(TAG, "notify failed: $status")
            }
        }
    }

    private fun stopBle() {
        senderJob?.cancel()
        senderJob = null
        getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeAdvertiser?.stopAdvertising(advCallback)
        gatt?.close()
        gatt = null
        events = null
        connected.clear()
    }

    private val advCallback = object : AdvertiseCallback() {
        override fun onStartFailure(errorCode: Int) {
            Log.w(TAG, "advertising failed: $errorCode")
        }
    }

    private fun startAdvertising() {
        val advertiser = getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeAdvertiser ?: return
        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_POWER)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
            .setConnectable(true)
            .setTimeout(0)
            .build()
        val data = AdvertiseData.Builder()
            .setIncludeDeviceName(false)
            .addServiceUuid(ParcelUuid(Protocol.SERVICE_UUID))
            .build()
        advertiser.stopAdvertising(advCallback) // no-op unless already running
        advertiser.startAdvertising(settings, data, advCallback)
    }

    private val gattCallback = object : BluetoothGattServerCallback() {
        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected[device.address] = device
            } else {
                connected.remove(device.address)
                app.scope.launch { if (gatt != null) startAdvertising() }
            }
        }

        override fun onServiceAdded(status: Int, service: BluetoothGattService) {
            if (status == BluetoothGatt.GATT_SUCCESS) app.scope.launch { startAdvertising() }
        }

        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice, requestId: Int, characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            val ok = characteristic.uuid == Protocol.CONTROL_UUID && !preparedWrite && offset == 0 && value != null
            if (responseNeeded) {
                gatt?.sendResponse(
                    device, requestId, if (ok) BluetoothGatt.GATT_SUCCESS else BluetoothGatt.GATT_REQUEST_NOT_SUPPORTED, 0, null,
                )
            }
            if (ok) app.scope.launch { handleWrite(device, value!!) }
        }

        override fun onDescriptorWriteRequest(
            device: BluetoothDevice, requestId: Int, descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            // Notifications are sent to any connected laptop; the CCC write only needs to succeed.
            if (responseNeeded) gatt?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
        }

        override fun onDescriptorReadRequest(
            device: BluetoothDevice, requestId: Int, offset: Int, descriptor: BluetoothGattDescriptor,
        ) {
            gatt?.sendResponse(
                device, requestId, BluetoothGatt.GATT_SUCCESS, offset, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE,
            )
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            notificationSent.trySend(Unit)
        }
    }

    private fun handleWrite(device: BluetoothDevice, bytes: ByteArray) {
        val addr = device.address
        val msg = Codec.decode(bytes)
        if (msg is Message.PairReq) return pair(device, msg)
        if (!app.allowlist.contains(addr)) return send(addr, Message.ErrorMsg(ErrorCode.NOT_PAIRED))
        when (msg) {
            Message.Request -> machine.onRequest(addr)
            is Message.Release -> machine.onRelease(msg.sid)
            else -> send(addr, Message.ErrorMsg(ErrorCode.BAD_REQUEST))
        }
    }

    private fun pair(device: BluetoothDevice, msg: Message.PairReq) {
        val addr = device.address
        if (!app.pairingOpen.value || device.bondState != BluetoothDevice.BOND_BONDED) {
            return send(addr, Message.ErrorMsg(ErrorCode.NOT_PAIRED))
        }
        app.allowlist.add(addr, msg.name.take(MAX_NAME))
        app.pairingOpen.value = false
        val phone = Settings.Global.getString(contentResolver, "device_name") ?: Build.MODEL
        send(addr, Message.Paired(phone.take(MAX_NAME)))
    }

    // ---- UDP heartbeat ----

    private fun startUdp() {
        udpJob = app.scope.launch(Dispatchers.IO) {
            val socket = try {
                DatagramSocket(Protocol.UDP_PORT)
            } catch (e: SocketException) {
                Log.e(TAG, "cannot bind UDP ${Protocol.UDP_PORT}", e)
                return@launch
            }
            udpSocket = socket
            val buf = ByteArray(512)
            val packet = DatagramPacket(buf, buf.size)
            while (isActive) {
                packet.length = buf.size
                try {
                    socket.receive(packet)
                } catch (e: IOException) {
                    break // closed in onDestroy
                }
                if (packet.length > Protocol.MAX_MESSAGE_BYTES) continue
                val reply = when (val msg = Codec.decode(buf.copyOf(packet.length))) {
                    is Message.Ping ->
                        if (withContext(Dispatchers.Main) { machine.onPing(msg.sid) }) Message.Pong(msg.sid, msg.seq) else null
                    is Message.Release -> {
                        withContext(Dispatchers.Main) { machine.onRelease(msg.sid) }
                        null
                    }
                    else -> null
                }
                if (reply != null) {
                    val out = Codec.encode(reply)
                    try {
                        socket.send(DatagramPacket(out, out.size, packet.socketAddress))
                    } catch (e: IOException) {
                        Log.w(TAG, "pong send failed", e)
                    }
                }
            }
            socket.close()
        }
    }

    companion object {
        const val ACTION_STOP = "dev.marcusson.lasso.STOP"
        private const val TAG = "Lasso"
        private const val CH_STATUS = "status"
        private const val CH_ALERT = "alert"
        private const val NOTIF_STATUS = 1
        private const val NOTIF_ALERT = 2
        private const val MAX_NAME = 40
        const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"
    }
}
