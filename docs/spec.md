# Lasso: laptop-to-phone on-demand hotspot

Draft spec for brainstorming. No implementation yet. Working name: **Lasso**.

## 1. Goal

From the laptop, one action (a hotkey, a Quickshell button, or a CLI call) asks the phone over Bluetooth to start a private Wi-Fi hotspot. The laptop joins that hotspot by itself. The two devices exchange a slow heartbeat, and when the heartbeat stops the phone turns the hotspot off. The user never touches the phone.

Target phone: **Samsung Galaxy S25 Ultra, One UI 9**, not rooted. The bootloader stays locked and Knox must stay intact.

### Non-goals (v1)

- Mobile data usage warnings.
- KDE Connect integration.
- More than one laptop per phone, or more than one phone per laptop.
- iOS. Third-party iOS apps cannot toggle Personal Hotspot. Apple's Instant Hotspot only works between Apple devices.
- Bluetooth PAN tethering as the data path. It is too slow, at about 1 to 2 Mbit/s.

## 2. Prior art

- **Samsung Auto Hotspot** and **Google Instant Tethering** (Pixel to Chromebook or Android tablet) do this flow natively, but only inside their own ecosystems. Neither works with a Linux laptop.
- **KDE Connect / GSConnect** already handle pairing, battery and notifications over LAN or Bluetooth, but they have no hotspot control. Adding a KDE Connect plugin is a possible alternative to a standalone app, but it adds a large dependency for a single feature.

## 3. The hard constraint: starting a hotspot on Android

A normal Android app **cannot** start the system tethered hotspot:

| API | Gives internet? | Usable by a normal app? |
|---|---|---|
| `WifiManager.startLocalOnlyHotspot` | **No** (local network only, random SSID) | Yes |
| `TetheringManager.startTethering(TETHERING_WIFI)` | Yes | No, requires `TETHER_PRIVILEGED` |
| `cmd wifi start-softap ...` (shell) | Needs verification | Shell UID only |

Root is ruled out (locked bootloader, Knox must stay intact). That leaves two options.

### Option A: Shizuku (main path)

[Shizuku](https://shizuku.rikka.app/) is an app that starts a small helper process through Android's own ADB debugging channel. The helper runs with the same identity as `adb shell`, and approved apps can call system APIs through it. That identity holds `TETHER_PRIVILEGED`, so the Lasso app can start and stop the real tethered hotspot.

- No root, no bootloader unlock, and no effect on Knox. It uses the same mechanism as plugging the phone into a computer and running `adb`.
- **Cost:** the helper dies on every reboot. Restarting it takes about 10 seconds: open Shizuku, then tap Start via Wireless Debugging. This needs the phone to be on some Wi-Fi network at that moment, or a USB cable to the laptop (`adb` can start it too).
  - Turn off Samsung's scheduled **Auto restart** (Device care), so this only happens after manual reboots and updates.
  - Developer options and Wireless Debugging must be enabled. A few banking or security apps complain when Developer options are on, so check the apps you depend on.
- Lasso should detect when Shizuku is not running, reply `ERROR NO_PRIVILEGE`, and show a phone notification that opens Shizuku.

### Option B: Samsung Modes and Routines (zero-code fallback)

One UI's built-in Routines has a "Mobile Hotspot" action and a "Bluetooth device connected" condition. Routines also undo their actions when the condition ends. That gives a zero-app version:

- Condition: Bluetooth connected to the laptop. Action: Mobile Hotspot on. When the condition ends, the hotspot turns off.
- Credentials are fixed and set once in Samsung's hotspot settings, with **Allowed devices only** set to the laptop's MAC address. The laptop keeps a saved NetworkManager profile.
- The "heartbeat" is the Bluetooth link itself, so there is no app, no Shizuku and no reboot problem.
- Downsides:
  - The laptop has to hold a Bluetooth connection to the phone, and a short Bluetooth drop kills the hotspot immediately (no grace period).
  - Credentials never rotate.
  - Connecting the laptop to the phone may set it up as a headset or audio device, so the laptop must block the audio profiles (HFP and A2DP) for the phone in BlueZ or WirePlumber.

### Spikes, before anything else

1. **Option A (the main path), about an hour:** with Shizuku running, confirm that a shell-identity call to `TetheringManager.startTethering(TETHERING_WIFI)` on One UI 9 (a) starts a hotspot *with internet*, (b) accepts a custom SSID and passphrase, and (c) stops cleanly.
2. **Option B, about 10 minutes:** build the routine by hand and check two things. Does `bluetoothctl connect <phone>` from the laptop fire the condition? Does the hotspot turn off on disconnect?

If spike 2 works well enough, use Option B as an immediate stopgap while Option A gets built. The rest of this spec describes Option A.

## 4. Components

```
Laptop (Linux)                                Phone (Android)
──────────────────────────────                ──────────────────────────────
lassod  (systemd --user service)              Lasso app
 ├─ BLE central (BlueZ over D-Bus)  ◄─BLE─►    ├─ foreground service
 ├─ NetworkManager (join Wi-Fi)                ├─ BLE GATT server + advertiser
 ├─ desktop notifications                      ├─ hotspot control (via Shizuku)
 ├─ heartbeat client (UDP)        ◄─Wi-Fi─►    ├─ heartbeat listener (UDP)
 └─ control: CLI / D-Bus / Quickshell          └─ persistent "sharing" notification + Stop button
```

- **Laptop:** a single daemon, `lassod`. Suggested stack: Python with `bleak` (BlueZ), `nmcli`, and `notify-send`, or Rust with `bluer` if a single binary matters more. Expose `lasso connect | disconnect | status` as a CLI; Quickshell can call the CLI.
- **Phone:** a Kotlin app with one foreground service of type `connectedDevice`. It acts as the BLE peripheral because Android throttles background scanning hard but handles advertising from a foreground service well.

## 5. Pairing (one time)

1. The laptop runs `lasso pair`, and the phone app shows a "Pair laptop" screen.
2. The devices do a normal Bluetooth LE bond using **LE Secure Connections with numeric comparison**, so both screens show the same six digits.
3. The phone stores the laptop's identity (IRK/address) on its allowlist. The laptop stores the phone's address.
4. All Lasso GATT characteristics require an **encrypted, MITM-protected link** (`PERMISSION_WRITE_ENCRYPTED_MITM`). A device that is not bonded and allowlisted cannot write to them.

There is no app-level crypto in v1. The Bluetooth bond already provides authentication and encryption for the control channel.

## 6. Connect flow

```
User         lassod                         Phone app                    NetworkManager
 │ connect     │                               │                               │
 ├────────────►│ notify "Connecting to <phone>…" (id N)                       │
 │             │── BLE connect + REQUEST ─────►│                               │
 │             │◄──────────── STARTING ────────│ start hotspot (Shizuku)       │
 │             │◄── READY {ssid, psk, session} │                               │
 │             │── RELEASE BLE link ──────────►│                               │
 │             │── join ssid/psk ─────────────────────────────────────────────►│
 │             │◄──────────────────────────────────────────── connected ──────│
 │             │ update notification N: "Connected via <phone>"               │
 │             │── PING (UDP to gateway) ─────►│                               │
 │             │◄──────────── PONG ────────────│                               │
```

- **One notification per attempt**, updated in place (same notification ID, or the `x-canonical-private-synchronous` hint): *Connecting… → Connected* or *Connecting… → Failed: \<reason\>*.
- **Fresh credentials per session.** The phone generates a random SSID suffix and a random 20+ character passphrase for every session, uses WPA3 (or WPA2/WPA3 transition) and allows a maximum of one client. The credentials only travel over the encrypted BLE link. Nobody else can join, and stale credentials are useless.
- **No saved profile clutter on the laptop.** Use a single NetworkManager connection named `lasso`, update its SSID and PSK each session, and set `metered=yes`, `autoconnect=no`.
- **Hotspot already on.** If the user started the hotspot manually, the phone answers `READY` with the existing credentials but marks the session `owned=false`. Lasso then never turns that hotspot off.

### Timeouts

| Step | Limit | On failure |
|---|---|---|
| BLE connect | 15 s | "Phone not reachable (Bluetooth)" |
| `READY` after `REQUEST` | 20 s | "Phone could not start hotspot: \<code\>" |
| Wi-Fi associated + DHCP | 30 s | Send `RELEASE`, then "Could not join hotspot" |

## 7. Heartbeat and teardown

The heartbeat runs **over the hotspot link itself (UDP)**, not over BLE. Reasons:

- It proves the thing that matters: the laptop is actually reachable over the hotspot.
- BLE can disconnect after setup, so there is no second radio link to keep alive and no extra drain on either battery.
- The phone app already sits at the gateway IP, which the laptop learns from DHCP.

### Protocol

- The laptop sends `PING {session_id, seq}` to `gateway:<port>` every **30 s**.
- The phone answers `PONG {session_id, seq}`.
- `session_id` is a random 128-bit value from `READY`. It acts as the bearer token, which is enough because only one client is allowed and the passphrase is per-session.

### Phone-side teardown

The phone stops the hotspot (only if `owned=true`) when any of these happen:

1. **No valid PING for 3 min** (6 missed beats). This is the core rule.
2. **No PING within 90 s of `READY`**, meaning the laptop never joined.
3. **`RELEASE` received** over BLE, or over UDP if the laptop is still on the hotspot.
4. **The user taps Stop** in the phone's persistent notification.
5. **Backstop:** Android's built-in "turn off hotspot when no devices are connected" timeout stays enabled.

After stopping, the phone sends a best-effort `ENDED` over BLE, and the laptop shows a "Hotspot off" notification if the session was active.

Rule 1 is the one Android's native timeout misses. A laptop in s2idle can stay associated with Wi-Fi while doing nothing. Without the heartbeat, the hotspot would stay up all night.

### Laptop-side behaviour

- **3 missed PONGs:** treat the session as dead, show the notification "Hotspot connection lost", and do not retry automatically in v1.
- **`lasso disconnect`:** send `RELEASE`, take the `lasso` connection down, and dismiss the notification.
- **Suspend (logind `PrepareForSleep`):** send `RELEASE` before sleeping.
- **Resume (optional):** if a session was active before sleep, call `connect` again automatically. This is what makes reopening the lid seamless.
- **Switching networks:** if NetworkManager moves to another network (for example, home Wi-Fi), the heartbeat stops and the phone cleans up within 3 min. An explicit `RELEASE` on the NetworkManager state change frees it sooner.

### Auto-connect (only when a connection is actually needed)

Being offline is not enough on its own. Something on the laptop must actually try to reach the network. `lassod` triggers `connect` by itself only when **all** of these are true:

1. NetworkManager has reported connectivity other than `full` for 20 s.
2. Ethernet is down, and the latest Wi-Fi scan contains no SSID with a saved autoconnect profile.
3. The user has not run `lasso disconnect` during this offline period (a manual disconnect wins until the network state changes).
4. The user session is unlocked and active (not idle for more than 5 min, according to logind). This is a cheap filter against background noise.
5. **Demand:** a qualifying outgoing packet reaches the catch-all interface while conditions 1 to 4 hold (see below).

#### Demand signal: a catch-all interface ("dial on demand")

The problem with watching traffic while offline is that no traffic exists. With no route, the kernel fails `connect()` immediately and resolved fails lookups without sending anything. The fix is the old PPP dial-on-demand trick: give the traffic somewhere to go.

- **The catch-all interface.** A permanent TUN device, `lasso0`, owned by the user and managed by NetworkManager (connection type `tun` with an `owner`). It carries:
  - a default route with a very high metric (IPv4 and IPv6), so any real network always wins;
  - a DNS server on the link, using the routing domain `~.` in resolved, so lookups also become packets.

  No dynamic route switching is needed. When the laptop is online, nothing ever reaches `lasso0`. When it is offline, everything does.
- **`lassod` reads the device.** Every packet that arrives on `lasso0` is an attempt to reach the network: any protocol, raw IP addresses, QUIC, DNS-over-HTTPS. `lassod` needs no root for this because it owns the device.
- **Filtering:**
  - *Direct traffic:* an nftables rule in the `output` chain lets packets reach `lasso0` only from the user's UID (`meta skuid 1000`), so system services such as timesyncd and NetworkManager's connectivity check are dropped there. Per-unit `socket cgroupv2` drop rules handle noisy user services (Syncthing, Tailscale, and so on).
  - *DNS:* lookups arrive as packets from systemd-resolved, so they carry no app identity. `lassod` parses the query name from the packet and applies the name filter (single-label names, `.local`/`.lan`, and a denylist of background names).
  - Tune both filters from a log of what triggered each connection.
- **Side effect:** while offline, apps hang until they time out instead of failing instantly with "no network". That is acceptable, and it is what makes the next point possible.
- **Upgrade path if attributing DNS lookups to apps ever matters:** use eBPF on resolved's stub listener. Do not build this up front.

#### The first request may survive (spike #3)

Each socket picks its source address from `lasso0`. Once the hotspot is up, a `masquerade` rule on the Wi-Fi interface rewrites that source address for packets leaving through it. TCP retransmits its first packet (the SYN) at roughly 1, 3, 7 and 15 s, and the hotspot takes about 15 s to come up. So the request that triggered everything can complete instead of failing, and DNS benefits the same way through resolved's retries.

Treat this as a spike. If it does not hold up, the fallback is a "Connecting… retry in a moment" notification, followed by "Connected" as the cue to retry.

The reverse also happens. The `lasso` connection gets a lower NetworkManager `autoconnect-priority` than known networks. When a known Wi-Fi network shows up, `lassod` switches to it and sends `RELEASE`.

A failed auto attempt backs off (1 min, 5 min, then 15 min) so the laptop does not spam the phone or notifications.

## 8. Message format

All messages are small JSON objects with a `v` (protocol version) field. GATT uses one write characteristic (laptop to phone) and one notify characteristic (phone to laptop). UDP uses one datagram per message.

| Message | Direction | Fields |
|---|---|---|
| `REQUEST` | L→P (BLE) | `v` |
| `STARTING` | P→L (BLE) | `v` |
| `READY` | P→L (BLE) | `v, ssid, psk, security, band, session_id, hb_port, owned` |
| `ERROR` | P→L (BLE) | `v, code` (`NO_PRIVILEGE`, `NO_MOBILE_DATA`, `AIRPLANE`, `TETHER_FAILED`, `BUSY`) |
| `RELEASE` | L→P (BLE or UDP) | `v, session_id` |
| `ENDED` | P→L (BLE, best effort) | `v, session_id, reason` |
| `PING` / `PONG` | L↔P (UDP) | `v, session_id, seq` |

## 9. Phone state machine

```
IDLE ──REQUEST──► STARTING ──hotspot up──► WAIT_JOIN ──first PING──► ACTIVE
  ▲                  │ fail                   │ 90 s                  │ 3 min no PING / RELEASE / Stop
  │                  ▼                        ▼                       ▼
  └──────────── ERROR ◄───────────────── STOPPING ◄───────────────────┘
```

The laptop's states mirror these: `IDLE → REQUESTING → JOINING → ACTIVE → IDLE`, with a notification at every transition the user can see.

## 10. Decisions

- Phone: Galaxy S25 Ultra on One UI 9, no root. Option A (Shizuku) is the main path; Option B (Routines) is a possible stopgap.
- Auto-connect: yes, only when outgoing traffic is caught on the `lasso0` catch-all interface while offline. See section 7.
- Data warning: no.
- Heartbeat: 30 s pings, with a 3 min timeout.
- KDE Connect: no. Lasso is a standalone app.

## 11. First steps once implementation starts

1. Run the spikes in section 3.
2. A minimal GATT round trip, with the laptop (`bleak`) writing `REQUEST` and the phone notifying `READY` with a dummy payload, using a bonded and encrypted link.
3. Spike #3: the `lasso0` catch-all route plus masquerade. Does a `curl` started while offline complete once the hotspot comes up?
4. Wire up the full flow without the heartbeat, then add the heartbeat and teardown rules.
