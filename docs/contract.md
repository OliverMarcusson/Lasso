# Lasso implementation contract

This file freezes the interfaces between components so they can be built in parallel. `docs/spec.md` is the design; this file wins where the two differ. Change this file only through the orchestrator.

## Repository layout and ownership

```
docs/                      spec.md, contract.md (orchestrator)
laptop/                    Python package, managed with uv (Python >= 3.12)
  pyproject.toml           deps: bleak; entry points `lasso` (CLI) and `lassod` (daemon)
  lasso/
    config.py              Config dataclass + loader            (laptop core)
    protocol.py            message encode/decode, constants      (laptop core)
    ble.py nm.py notify.py heartbeat.py daemon.py cli.py control.py   (laptop core)
    demand/                catch-all TUN demand detection        (demand)
  tests/                   pytest; test_demand_*.py belong to demand, the rest to laptop core
packaging/                 systemd units, NetworkManager + nftables setup, install script
android/                   Gradle project, package `dev.marcusson.lasso`
```

## Constants

| Name | Value |
|---|---|
| GATT service UUID | `210ce96b-a4ff-4f59-9bd3-b9a8dc850af8` |
| Control characteristic (laptop writes, write-with-response, encrypted + MITM) | `7470fd02-45dc-4fda-a006-2c2cb7a6426c` |
| Events characteristic (phone notifies, encrypted + MITM) | `3d83b6c3-38f5-48da-8f70-53b160cebbc9` |
| Heartbeat UDP port on phone | `47123` |
| Protocol version `v` | `1` |
| Heartbeat interval (laptop sends PING) | 30 s |
| Phone: no PING timeout while ACTIVE | 180 s |
| Phone: first PING deadline after READY | 90 s |
| Laptop: missed PONGs before session dead | 3 |
| Laptop timeouts: BLE connect / READY / Wi-Fi join | 15 s / 20 s / 30 s |

## Wire messages

UTF-8 JSON objects with no whitespace, one message per GATT write, GATT notification or UDP datagram. Every message has `"t"` (type) and `"v": 1`. Unknown fields are ignored. Unknown `t` values are ignored, except that the phone answers an unknown BLE request with `ERROR BAD_REQUEST`. Messages must stay at or below 180 bytes so they fit the default negotiated ATT MTU (185) on Android and BlueZ. The laptop requests a larger MTU but must not depend on getting it.

| t | Direction | Fields | Notes |
|---|---|---|---|
| `PAIR` | L→P BLE | `name` (laptop hostname) | Accepted only while the phone's pairing screen is open; the phone adds the bonded device address to its allowlist. |
| `PAIRED` | P→L BLE | `name` (phone name) | |
| `REQUEST` | L→P BLE | | |
| `STARTING` | P→L BLE | | |
| `READY` | P→L BLE | `ssid`, `psk`, `sec` (`wpa3t` WPA2/WPA3 transition, `wpa3`, `wpa2`), `sid` (32 lowercase hex chars), `port`, `owned` (bool) | |
| `ERROR` | P→L BLE | `code` | Codes: `NOT_PAIRED`, `NO_PRIVILEGE`, `NO_MOBILE_DATA`, `AIRPLANE`, `TETHER_FAILED`, `BUSY`, `BAD_REQUEST` |
| `RELEASE` | L→P BLE or UDP | `sid` | |
| `ENDED` | P→L BLE | `sid`, `reason` (`timeout`, `no_join`, `released`, `user_stop`, `system`) | Best effort; the laptop may not be connected. |
| `PING` | L→P UDP | `sid`, `seq` (int) | Sent to the Wi-Fi gateway address, `port`. |
| `PONG` | P→L UDP | `sid`, `seq` | Echoes `seq`. A PING with an unknown `sid` gets no reply. |

The SSID is `Lasso-` plus 6 random characters from `[a-z0-9]`. The PSK is 24 random characters from `[A-Za-z0-9]`. The phone generates both per session. A `REQUEST` while a session is active returns `READY` for the existing session, which makes retries idempotent.

## Laptop internals

### Config (`lasso/config.py`, laptop core)

`~/.config/lasso/config.toml`, read with `tomllib`. Every key is optional.

```python
@dataclass
class Config:
    phone_address: str | None = None      # set by `lasso pair`
    auto_connect: bool = True
    idle_minutes: int = 5
    offline_grace_s: int = 20
    tun_name: str = "lasso0"
    deny_names: list[str] = field(default_factory=list)   # extra demand denylist entries (suffix match)
    deny_units: list[str] = field(default_factory=list)   # user units whose traffic never counts as demand
```

### Demand watcher (`lasso/demand/`, demand)

```python
class DemandWatcher:
    def __init__(self, config: Config) -> None: ...
    async def run(self, on_demand: Callable[[str], None]) -> None:
        """Read packets from the TUN device forever. Call on_demand(reason) for each qualifying
        packet, rate-limited to at most once per 5 s. reason is human readable, for example
        "dns:example.com" or "tcp:93.184.216.34:443". Never raises because of malformed packets."""
```

`deny_units` is enforced by the watcher via /proc attribution of the packet's source socket to a cgroup path component; an unknown owner counts as demand.

The daemon owns the gating (offline, no known Wi-Fi, user active, no manual disconnect, backoff). The watcher only reports demand. If the TUN device does not exist, `run` logs one warning and returns, and the daemon then works without auto-connect.

Catch-all network: TUN `lasso0`, owned by the user, address `10.211.0.2/30` with peer and fake DNS server `10.211.0.1`, default route metric `30000`. IPv6: `fd6c:6173:736f::2/64` with a `::/0` route at metric `30000`.

### Local control (`lasso/control.py`, laptop core)

A Unix socket at `$XDG_RUNTIME_DIR/lasso.sock`, one JSON request line followed by one JSON response line:

- `{"cmd":"connect"}`, `{"cmd":"disconnect"}`, `{"cmd":"status"}`, `{"cmd":"pair"}`
- Response: `{"ok":true,"state":"ACTIVE","detail":"..."}`, or `{"ok":false,"error":"..."}`
- States: `IDLE`, `REQUESTING`, `JOINING`, `ACTIVE`, `PAIRING`

## Android internals

`HotspotController` (package `dev.marcusson.lasso.hotspot`) is the only boundary between the app and the privileged hotspot code.

```kotlin
enum class Security { WPA3_TRANSITION, WPA3, WPA2 }
data class HotspotConfig(val ssid: String, val psk: String, val security: Security)
data class HotspotInfo(val ssid: String, val psk: String, val security: Security, val owned: Boolean)
class HotspotException(val code: String, message: String? = null) : Exception(message)  // code is a wire ERROR code

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
```

Android targets: `minSdk 33`, `targetSdk 36`, Kotlin, coroutines, no Compose (plain Views or a minimal activity are fine). Shizuku dependency: `dev.rikka.shizuku:api` and `dev.rikka.shizuku:provider`, version 13.1.5.
