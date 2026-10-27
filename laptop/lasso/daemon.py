"""lassod: session state machine, auto-connect gating, sleep handling, control socket."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lasso import config as config_mod
from lasso import control, protocol
from lasso.ble import Ble
from lasso.config import Config
from lasso.heartbeat import Heartbeat, PingFn, send_udp, udp_ping
from lasso.nm import NM, known_connections
from lasso.notify import Notifier
from lasso.protocol import LassoError

try:
    from lasso.demand import DemandWatcher
except ImportError:  # the daemon works without auto-connect
    DemandWatcher = None  # type: ignore[assignment,misc]

log = logging.getLogger("lasso")

POLL_S = 5
MANUAL_GUARD_S = 5      # ignore a stale "full" right after a manual disconnect
BACKOFF_STEPS_S = (60, 300, 900)
RESUME_DELAY_S = 3      # let Bluetooth and Wi-Fi come back before reconnecting


class State(StrEnum):
    IDLE = "IDLE"
    REQUESTING = "REQUESTING"
    JOINING = "JOINING"
    ACTIVE = "ACTIVE"
    PAIRING = "PAIRING"


# ---- gating: small pure pieces, spec section 7 ----

@dataclass
class Observation:
    connectivity: str = "unknown"
    ethernet_up: bool = False
    known_wifi: list[str] = field(default_factory=list)  # saved autoconnect connections whose SSID is visible
    user_active: bool = True


class OfflineTracker:
    """Tracks the current offline period (condition 1) and a manual disconnect inside it (condition 3)."""

    def __init__(self) -> None:
        self.offline_since: float | None = None
        self.manual = False
        self._manual_at = -MANUAL_GUARD_S

    def mark_manual(self, now: float) -> None:
        self.manual = True
        self._manual_at = now

    def update(self, connectivity: str, now: float, lasso_active: bool) -> bool:
        """Feed one connectivity sample. Returns True when a real (non-Lasso) network just came back."""
        if connectivity != "full":
            if self.offline_since is None:
                self.offline_since = now
            return False
        was_offline = self.offline_since is not None
        self.offline_since = None
        if lasso_active:
            return False
        if now - self._manual_at >= MANUAL_GUARD_S:
            self.manual = False  # network state changed, the manual disconnect is over
        return was_offline


class Backoff:
    """After a failed auto attempt: wait 1 min, then 5 min, then 15 min (stays at 15)."""

    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0

    def failed(self, now: float) -> None:
        self.until = now + BACKOFF_STEPS_S[min(self.failures, len(BACKOFF_STEPS_S) - 1)]
        self.failures += 1

    def allowed(self, now: float) -> bool:
        return now >= self.until

    def reset(self) -> None:
        self.failures = 0
        self.until = 0.0


def auto_connect_block(obs: Observation, tracker: OfflineTracker, backoff: Backoff, now: float,
                       grace_s: float, state: State) -> str | None:
    """Why an auto-connect must not happen right now (conditions 1-4), or None if it may."""
    if state is not State.IDLE:
        return f"busy ({state})"
    if tracker.offline_since is None or now - tracker.offline_since < grace_s:
        return "not offline long enough"
    if obs.ethernet_up:
        return "ethernet is up"
    if obs.known_wifi:
        return "known Wi-Fi in range"
    if tracker.manual:
        return "manual disconnect"
    if not obs.user_active:
        return "user idle or locked"
    if not backoff.allowed(now):
        return "backing off"
    return None


def user_active(props: dict[str, str], now_us: int, idle_minutes: float) -> bool:
    """From logind session properties: unlocked, and not idle for longer than idle_minutes."""
    if props.get("LockedHint") == "yes":
        return False
    if props.get("IdleHint") == "yes":
        since = int(props.get("IdleSinceHint") or 0)
        return since == 0 or now_us - since <= idle_minutes * 60_000_000
    return True


async def _loginctl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "loginctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode()


async def read_user_active(idle_minutes: float) -> bool:
    """Ask logind about our session. If anything is unavailable, assume the user is active."""
    try:
        sid = os.environ.get("XDG_SESSION_ID")
        if not sid:
            out = await _loginctl("show-user", str(os.getuid()), "-p", "Display", "--value")
            sid = out.strip()
        if not sid:
            return True
        out = await _loginctl("show-session", sid, "-p", "IdleHint", "-p", "LockedHint", "-p", "IdleSinceHint")
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return user_active(props, int(time.time() * 1_000_000), idle_minutes)
    except (OSError, ValueError):
        return True


# ---- the daemon ----

class Daemon:
    def __init__(self, config: Config, *, ble: Any = None, nm: Any = None, notifier: Any = None,
                 make_ping: Callable[[str, int, str], PingFn] = udp_ping,
                 udp_send: Callable[[str, int, bytes], Awaitable[None]] = send_udp,
                 hb_interval: float = protocol.HEARTBEAT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic,
                 read_active: Callable[[float], Awaitable[bool]] = read_user_active) -> None:
        self.config = config
        self.ble = ble or Ble()
        self.nm = nm or NM()
        self.notifier = notifier or Notifier()
        self.make_ping = make_ping
        self.udp_send = udp_send
        self.hb_interval = hb_interval
        self.clock = clock
        self.read_active = read_active

        self.state = State.IDLE
        self.detail = ""
        self.session: protocol.Ready | None = None
        self.gateway: str | None = None
        self.tracker = OfflineTracker()
        self.backoff = Backoff()
        self._connect_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._was_active_before_sleep = False
        self._inhibit_fd: int | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- small helpers --

    def _set(self, state: State, detail: str = "") -> None:
        self.state, self.detail = state, detail
        log.info("state %s %s", state, detail)

    async def _notify(self, text: str) -> None:
        await self.notifier.show(text)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- control commands --

    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        try:
            if cmd == "status":
                pass
            elif cmd == "connect":
                self.tracker.manual = False  # an explicit connect overrides an earlier manual disconnect
                await self.connect()
            elif cmd == "disconnect":
                self.tracker.mark_manual(self.clock())
                await self.disconnect()
            elif cmd == "pair":
                await self.pair(req.get("address"))
            else:
                return {"ok": False, "error": f"unknown command {cmd!r}"}
        except LassoError as e:
            return {"ok": False, "error": str(e)}
        except asyncio.CancelledError:
            if cmd == "connect" and self.state is State.IDLE:
                return {"ok": False, "error": "cancelled"}
            raise
        return {"ok": True, "state": str(self.state), "detail": self.detail}

    # -- connect / disconnect --

    async def connect(self, auto: bool = False) -> None:
        if self.state is State.ACTIVE:
            return
        if self._connect_task is None or self._connect_task.done():
            if self.state is not State.IDLE:
                raise LassoError(f"busy: {self.state}")
            self._connect_task = asyncio.ensure_future(self._connect(auto))
        await asyncio.shield(self._connect_task)

    async def _connect(self, auto: bool) -> None:
        address = self.config.phone_address
        if not address:
            raise LassoError("not paired yet: run `lasso pair <address>` (see `lasso scan`)")
        phone = self.config.phone_name
        self.notifier.new_attempt()
        await self._notify(f"No network, connecting to {phone}… retry in a moment" if auto
                           else f"Connecting to {phone}…")
        self._set(State.REQUESTING)
        try:
            self.session = await self.ble.request(address)
            self._set(State.JOINING, self.session.ssid)
            self.gateway = await self.nm.join(self.session.ssid, self.session.psk, self.session.sec)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, end up in IDLE with a message
            if not isinstance(e, LassoError):
                log.exception("unexpected error while connecting")
                e = LassoError(f"{type(e).__name__}: {e}")
            # Tell the phone to stop its hotspot (if it started one), undo our profile.
            await self._teardown()
            if auto:
                self.backoff.failed(self.clock())
            self._set(State.IDLE, str(e))
            await self._notify(f"Hotspot failed: {e}")
            raise e
        self.backoff.reset()
        self._set(State.ACTIVE, f"via {phone}, gateway {self.gateway}")
        self._hb_task = asyncio.ensure_future(self._heartbeat(self.session, self.gateway))
        await self._notify(f"Connected via {phone}")

    async def disconnect(self) -> None:
        was_busy = self.state is not State.IDLE
        task = self._connect_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, LassoError):
                await task
        await self._teardown()
        self._set(State.IDLE, "disconnected")
        if was_busy:
            await self._notify(f"Disconnected from {self.config.phone_name}")

    async def _teardown(self, *, udp: bool = True, ble: bool = True) -> None:
        """Stop the heartbeat, RELEASE the session, take the `lasso` connection down."""
        hb, self._hb_task = self._hb_task, None
        if hb and hb is not asyncio.current_task():
            hb.cancel()
            with suppress(asyncio.CancelledError):
                await hb
        session, gateway = self.session, self.gateway
        self.session = self.gateway = None
        jobs = [self.nm.down()]
        if session:
            if udp and gateway:
                await self.udp_send(gateway, session.port, protocol.encode("RELEASE", sid=session.sid))
            if ble and self.config.phone_address:
                jobs.append(self.ble.release(self.config.phone_address, session.sid))
        await asyncio.gather(*jobs, return_exceptions=True)

    # -- heartbeat --

    async def _heartbeat(self, session: protocol.Ready, gateway: str) -> None:
        await Heartbeat(self.make_ping(gateway, session.port, session.sid), self.hb_interval).run()
        # run() only returns when the session is dead.
        log.warning("heartbeat lost")
        await self._notify("Hotspot connection lost")
        await self._teardown(udp=False, ble=False)
        # Same offline period: demand may retry only after the backoff.
        self.backoff.failed(self.clock())
        self._set(State.IDLE, "heartbeat lost")

    # -- pairing --

    async def pair(self, address: str | None) -> None:
        address = address or self.config.phone_address
        if not address:
            raise LassoError("pairing needs a device: lasso pair <address> (see `lasso scan`)")
        if self.state is not State.IDLE:
            raise LassoError(f"busy: {self.state}")
        self._set(State.PAIRING, address)
        self.notifier.new_attempt()
        await self._notify("Pairing: open the pairing screen in the Lasso app and confirm the numeric code")
        try:
            name = await self.ble.pair(address, socket.gethostname())
        except LassoError as e:
            await self._notify(str(e))
            raise
        finally:
            self._set(State.IDLE)
        config_mod.save_phone(address, name)
        self.config.phone_address, self.config.phone_name = address, name
        await self._notify(f"Paired with {name}")

    # -- network observation and auto-connect --

    async def observe(self) -> Observation:
        connectivity, eth, visible, saved, active = await asyncio.gather(
            self.nm.connectivity(), self.nm.ethernet_up(), self.nm.visible_ssids(), self.nm.saved_wifi(),
            self.read_active(self.config.idle_minutes))
        return Observation(connectivity, eth, known_connections(visible, saved), active)

    async def poll_once(self) -> None:
        obs = await self.observe()
        now = self.clock()
        active = self.state is State.ACTIVE
        if self.tracker.update(obs.connectivity, now, active):
            self.backoff.reset()
        if active and (obs.known_wifi or obs.ethernet_up):
            await self._switch_to_known(obs)

    async def _switch_to_known(self, obs: Observation) -> None:
        """A better network showed up while on the hotspot: join it and release the phone."""
        if obs.known_wifi and not obs.ethernet_up:
            log.info("known Wi-Fi %s in range, leaving the hotspot", obs.known_wifi[0])
            if not await self.nm.up(obs.known_wifi[0]):
                return
        await self._teardown()
        self._set(State.IDLE, "switched to a known network")
        await self._notify(f"Switched to a known network, released {self.config.phone_name}")

    async def watch(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - a bad poll must not kill the daemon
                log.exception("poll failed")
            await asyncio.sleep(POLL_S)

    def on_demand(self, reason: str) -> None:
        """Called by DemandWatcher for each qualifying packet."""
        self._spawn(self._demand(reason))

    async def _demand(self, reason: str) -> None:
        if not self.config.auto_connect or not self.config.phone_address or self.state is not State.IDLE:
            return
        obs = await self.observe()
        now = self.clock()
        self.tracker.update(obs.connectivity, now, False)
        why = auto_connect_block(obs, self.tracker, self.backoff, now, self.config.offline_grace_s, self.state)
        if why:
            log.debug("demand %s ignored: %s", reason, why)
            return
        log.info("auto-connect for %s", reason)
        with suppress(LassoError):
            await self.connect(auto=True)

    # -- logind sleep --

    async def watch_sleep(self) -> None:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()

        def on_message(msg: Message) -> None:
            if msg.message_type is MessageType.SIGNAL and msg.member == "PrepareForSleep":
                self._spawn(self._on_sleep(bool(msg.body[0]), bus))

        bus.add_message_handler(on_message)
        await bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
            member="AddMatch", signature="s",
            body=["type='signal',sender='org.freedesktop.login1',member='PrepareForSleep'"]))
        await self._take_inhibitor(bus)
        await bus.wait_for_disconnect()

    async def _take_inhibitor(self, bus: Any) -> None:
        """A delay lock gives us the few seconds logind allows to send RELEASE before sleeping."""
        from dbus_fast import Message
        try:
            reply = await bus.call(Message(
                destination="org.freedesktop.login1", path="/org/freedesktop/login1",
                interface="org.freedesktop.login1.Manager", member="Inhibit", signature="ssss",
                body=["sleep", "Lasso", "Release the phone hotspot before sleep", "delay"]))
            self._inhibit_fd = reply.body[0]
        except Exception as e:  # noqa: BLE001
            log.warning("no sleep inhibitor: %s", e)

    def _drop_inhibitor(self) -> None:
        fd, self._inhibit_fd = self._inhibit_fd, None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)

    async def _on_sleep(self, sleeping: bool, bus: Any) -> None:
        if sleeping:
            self._was_active_before_sleep = self.state is State.ACTIVE
            try:
                task = self._connect_task
                if task and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, LassoError):
                        await task
                # UDP RELEASE only: BLE would eat into logind's delay budget.
                await self._teardown(ble=False)
                self._set(State.IDLE, "sleeping")
            finally:
                self._drop_inhibitor()
            return
        await self._take_inhibitor(bus)
        if self._was_active_before_sleep:
            self._was_active_before_sleep = False
            await asyncio.sleep(RESUME_DELAY_S)
            with suppress(LassoError):
                await self.connect()


async def _logged(name: str, coro: Awaitable[None]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("%s stopped", name)


async def run(config: Config) -> None:
    daemon = Daemon(config)
    server = await control.serve(daemon.handle)
    background = [asyncio.ensure_future(_logged("poll", daemon.watch())),
                  asyncio.ensure_future(_logged("sleep watcher", daemon.watch_sleep()))]
    if DemandWatcher is not None and config.auto_connect:
        background.append(asyncio.ensure_future(_logged("demand watcher", DemandWatcher(config).run(daemon.on_demand))))
    elif DemandWatcher is None:
        log.warning("lasso.demand is not available, auto-connect is off")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("lassod ready, socket %s", control.socket_path())
    await stop.wait()

    for task in background:
        task.cancel()
    server.close()
    await daemon.disconnect()  # release the phone before exiting
    control.socket_path().unlink(missing_ok=True)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LASSO_LOG", "INFO"), format="%(name)s: %(levelname)s %(message)s")
    asyncio.run(run(config_mod.load()))
