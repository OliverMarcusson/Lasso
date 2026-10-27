# Android hotspot spike (Shizuku, S25 Ultra, One UI 9)

Goal: prove that `ShizukuHotspotController` starts a tethered hotspot with internet, with Lasso's own SSID and passphrase, and stops it cleanly. Spec section 3, spike 1.

## How it works

- Primary path: `ITetheringConnector.startTethering(TetheringRequestParcel, "com.android.shell", null, IIntResultListener)` on the `tethering` service, called through `ShizukuBinderWrapper` so the call has shell identity (uid 2000, holds `TETHER_PRIVILEGED`). The request carries a `SoftApConfiguration` (SSID, passphrase, WPA3 transition, max 1 client, 2.4/5 GHz). On Android 16 Tethering passes that config to `WifiManager.startTetheredHotspot(request, ...)`, but `WifiServiceImpl` only uses it when the aconfig flag `com.android.net.flags.tethering_with_soft_ap_config` is on, and Samsung ships its own Wifi module. So before starting, the controller also saves the user's stored config (to app prefs) and writes Lasso's config with `IWifiManager.setSoftApConfiguration`. On stop it writes the user's config back. If the app dies mid-session, the next `start`/`stop`/`currentUserHotspot` call restores it.
- Sources checked (tag `android-16.0.0_r1`): `ITetheringConnector.aidl`, `TetheringRequestParcel.aidl`, `IIntResultListener.aidl`, `TetheringManager.java`, `TetheringService.java`, `Tethering.java`, `TetheringDependencies.java` (packages/modules/Connectivity); `IWifiManager.aidl`, `SoftApConfiguration.java`, `WifiServiceImpl.java`, `WifiShellCommand.java` (packages/modules/Wifi); `packages/Shell/AndroidManifest.xml` (shell holds `TETHER_PRIVILEGED`, `NETWORK_SETTINGS` and `OVERRIDE_WIFI_CONFIG`).
- Then it polls `IWifiManager.getWifiApEnabledState()` until it reads `ENABLED (13)`, with a 15 s limit.
- Stop: `ITetheringConnector.stopTethering(TETHERING_WIFI, ...)`, then it waits for `DISABLED (11)`.
- Fallback, used only if the binder path throws (reflection or a dead service): `cmd wifi start-softap` / `cmd wifi stop-softap`. On Android 16 that AP probably has **no internet**, because Tethering ignores tethered-mode AP broadcasts on B+. If logcat shows the fallback ran, treat the spike as failed.

## Steps

1. Build and install the debug APK:
   `adb install -r app-debug.apk` (from the Artifex `android-gradle` job output, or `./gradlew assembleDebug`).
2. Start Shizuku: open the Shizuku app and tap Start via Wireless Debugging, or over USB run
   `adb shell sh /sdcard/Android/data/moe.shizuku.privileged.api/start.sh`.
   Shizuku should show "running" with **adb** (not root) as the mode.
3. Open Lasso, grant the Bluetooth and notification permissions, then tap **Request Shizuku permission** and allow it. The status line should read `Shizuku: ready`.
4. Start the log in a terminal:
   `adb logcat -c && adb logcat -s LassoHotspot`
5. Make sure that mobile data is on and the system hotspot is **off**. If you want to be sure the upstream is cellular, turn Wi-Fi off too.
6. Trigger a start. Either:
   - tap **Test hotspot (debug, 60 s)** in Lasso (debug builds only; it starts `Lasso-Test` / `lasso-test-1234`, then stops after 60 s), or
   - run a real `REQUEST` from the paired laptop with the Lasso CLI.
7. Expected log, in order:
   - `hidden API exemption: true`
   - `upstream candidate ... validated=true`
   - `start: apState=11`
   - `startViaTethering: softAp=...`
   - `overwriteStoredConfig: saved user ssid=..., setSoftApConfiguration=true`
   - `startTethering: callerPkg=com.android.shell`
   - `startTethering result=0`
   - `start: hotspot up`
8. Check on the phone: the hotspot tile is on. During the session Settings > Mobile Hotspot may show `Lasso-Test` (Lasso overwrote the saved config). After stop it must show your own name and password again.
9. Connect the laptop to `Lasso-Test` (or let the Lasso CLI connect), then:
   - `nmcli -f GENERAL.CONNECTION,IP4.GATEWAY dev show <wifi-iface>`: you should get a 10.x / 192.168.x gateway address from DHCP.
   - `curl -sS https://ifconfig.me`: it should print the phone's cellular IP.
   - Try to connect a second device. It should be refused (max 1 client).
10. Stop: wait for the 60 s timer, or `RELEASE` from the laptop. Expected log: `stopTethering result=0`, `stop: hotspot down`, then `restoreUserConfig: setSoftApConfiguration=true`. The hotspot tile turns off and the laptop drops.
11. Tap the test button a second time: it must work again (this checks that stop leaves clean state).
12. User-hotspot check: turn the system hotspot on by hand, then trigger a `REQUEST`. The log should show `currentUserHotspot: ssid=<yours>`, the laptop gets your credentials with `owned=false`, and when the session ends the hotspot stays on.

## What to look at first if it fails

| Log line | Meaning | Next step |
|---|---|---|
| `isAvailable=false ... uid=-1` | Shizuku not running or not granted | Repeat steps 2 and 3 |
| `hidden API exemption: false`, or `ClassNotFoundException` / `NoSuchMethodException` | Reflection is blocked | Check the HiddenApiBypass version against Android 16 |
| `startTethering result=14` | Package/uid check failed, or the permission was refused | Check the uid in Shizuku: root mode sends `root` as the caller package |
| `startTethering result=11` or `13` | Carrier entitlement (provisioning) check failed | Carrier blocks tethering; check that the system hotspot works by hand |
| `startTethering result=3` | Tethering unsupported or disallowed (user restriction / Knox policy) | Check device policies |
| `soft AP state FAILED` | Wifi rejected the config | Run `adb logcat -s WifiService SoftApManager WifiApConfigStore` and check whether WPA3 transition or the band is rejected; try `Security.WPA2` |
| Hotspot up but SSID is the user's saved one | Samsung ignores both the per-request config and `setSoftApConfiguration` (it may keep its own store) | Check `adb shell cmd wifi status` and `dumpsys wifi \| grep -i softap`; this needs a Samsung-specific path |
| `overwriteStoredConfig: ... setSoftApConfiguration=false` | Wifi rejected the config (WPA3 transition, max clients or band not supported) | Check `WifiApConfigStore` in logcat; try `Security.WPA2` |
| `restoreUserConfig failed` or saved name is still `Lasso-...` after stop | The restore did not run or was rejected | Re-open Lasso (it retries) or reset the hotspot name by hand |
| Hotspot up, laptop connects, no IP or no internet | No IP serving (fallback path ran) or no upstream | Check whether `startViaCmd` appears in the log; run `adb shell dumpsys tethering` |
| `runCmd: cmd wifi start-softap` | The AOSP path threw | Read the stack trace logged just before it |

Useful extra checks: `adb shell dumpsys tethering | head -60` (tethering state and upstream), and `adb shell cmd wifi status`.
